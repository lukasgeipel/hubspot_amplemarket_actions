#!/usr/bin/env python3
"""
find_full_contacts.py  (standalone)

Given a CSV of DOMAINS, find people at those domains matching a title list,
enrich them, keep ONLY the ones that have LinkedIn + Phone + Email, and push
them into HubSpot associated to the company that owns the domain.

This version is fully self-contained:
  * No common.py, no config.yaml.
  * Reads credentials straight from a .env file in the SAME directory.
  * Talks to the Amplemarket + HubSpot REST APIs directly (only needs `requests`).

Behaviour:
  1. Reads DOMAINS from a CSV (default: ./domains.csv, or pass a path).
  2. Uses the editable TITLES list below.
  3. Keeps a contact only if enrichment returns LinkedIn AND Phone AND Email.
  3b. Validates each surviving email via Amplemarket's /email-validations
      endpoint and keeps only contacts whose email is accepted (default:
      "deliverable"; costs 1 email credit per address). Toggle with
      VALIDATE_EMAILS / ACCEPTED_EMAIL_RESULTS near the top.
  4. For each kept contact:
       - NEW  -> create in HubSpot, associated to the domain's company.
       - EXISTING (matched by LinkedIn, then email):
           * fills any BLANK properties (name, title, linkedin),
           * sets ICP flag = true (every gated contact matches the criteria),
           * fills lead batch ONLY when blank — never overwrites an existing
             batch; contacts already tagged with a DIFFERENT batch are left
             untouched and reported in a separate CSV,
           * REPLACES email and/or phone when Amplemarket has a different value,
           * makes sure it is associated to the company.

Setup:
  pip install requests            # python-dotenv NOT required; a built-in .env parser is used
  # put your domains in ./domains.csv (a "domain" column, or one domain per line)
  # make sure your cloned .env is in this directory (see ENV VAR NAMES below)

Usage:
  python find_full_contacts.py --dry-run           # plan only: no credits, no writes
  python find_full_contacts.py                     # uses ./domains.csv
  python find_full_contacts.py mylist.csv
  python find_full_contacts.py --max-per-domain 5
  python find_full_contacts.py --create-missing-companies
  python find_full_contacts.py --resume 12345      # resume an enrichment request
"""

import argparse
import csv
import io
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' package. Install it with:\n  pip install requests")

from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:                       # very old urllib3 fallback
    from requests.packages.urllib3.util.retry import Retry


def _session_with_retries():
    """A requests.Session that automatically retries on 429 / 5xx with
    exponential backoff, honouring the Retry-After header. This is what makes
    the script safe when many company runs hit HubSpot/Amplemarket at once
    (e.g. a bulk property update fanning out into 20 parallel GitHub runs)."""
    retry_kwargs = dict(
        total=6, connect=3, read=3, status=6,
        backoff_factor=2.0,                         # waits ~2,4,8,16,32,... s
        status_forcelist=(429, 500, 502, 503, 504),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    try:                                            # urllib3 >= 1.26
        retry = Retry(allowed_methods=frozenset(
            ["GET", "POST", "PATCH", "PUT", "DELETE"]), **retry_kwargs)
    except TypeError:                               # older urllib3
        retry = Retry(method_whitelist=frozenset(
            ["GET", "POST", "PATCH", "PUT", "DELETE"]), **retry_kwargs)
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ============================================================================
# EDIT ME  --  titles to look up at every domain
# ============================================================================
# These are sent to Amplemarket's search. EXACT_TITLE_MATCH is False so the API
# also returns titles where the term is JOINED with others (e.g. "Co-Founder &
# CEO", "COO/CFO"). Precision is then enforced locally by matches_target()
# below, which (a) keeps only these target roles and (b) drops support staff /
# inactive people via EXCLUDE_TERMS.
TITLES = [
    "CEO", "CFO", "COO", "President", "Founder", "Owner",
    "VP of Finance", "Director of Operations", "Head of Operations",
]

EXACT_TITLE_MATCH = False           # False -> catch target roles joined with other titles

# ===========================================================================
# TITLE MATCHING  --  precision layer (edit the vocab lists to tune)
# ---------------------------------------------------------------------------
# The Amplemarket search (TITLES above) is deliberately broad; real precision is
# enforced here. A headline role (CEO/CFO/COO/President/Owner) is accepted ONLY
# when it appears "clean" — i.e. NOT immediately qualified by:
#   * a SCOPE word   -> a division/region/subsidiary head, not the whole company
#                       ("Group President", "Division President", "President, EMEA")
#   * a FUNCTION word -> a functional head, not the top job
#                       ("President of Sales", "President, Marketing")
#   * a DEMOTION word -> not the actual top holder
#                       ("Deputy CEO", "Vice President", "Assistant to the CEO")
# Combos with another top role still pass ("President & CEO", "Co-Founder &
# President"), because the clean top role is detected first.
# ===========================================================================

# Division / region / subsidiary qualifiers -> NOT the whole-entity top job.
_SCOPE = (r"group|divisions?|divisional|segment|sub|business unit|unit|regions?|"
          r"regional|area|zone|territory|countr(?:y|ies)|national|international|"
          r"global|worldwide|americas?|emea|apac|latam|europe(?:an)?|asian?|"
          r"pacific|middle east|africa|african|north|south|east|west|central|"
          r"us|usa|united states|uk|canad(?:a|ian)|mexico|india|china|germany|"
          r"france|brazil|australia|japan|subsidiary|affiliate|joint venture|jv|"
          r"portfolio|board|club|chapter|student|association|society|chamber")

# Function / department qualifiers -> a functional head, not the top job.
_FUNCTION = (r"sales|marketing|financ(?:e|ial)|operations|operating|engineering|"
             r"technolog(?:y|ies)|technical|products?|people|hr|human resources|"
             r"talent|revenue|growth|commercial|legal|compliance|risk|"
             r"communications?|brand|digital|data|analytics|information|it|"
             r"security|supply chain|procurement|purchasing|manufacturing|quality|"
             r"research|development|strategy|business development|"
             r"corporate development|partnerships?|customers?|accounts?|field|"
             r"retail|wholesale|ecommerce|e commerce|merchandising|creative|design|"
             r"content|media|programs?|projects?|portfolio|investments?|lending|"
             r"credit|underwriting|claims|actuarial|tax|audit|treasury|accounting|"
             r"logistics|transportation|warehous(?:e|ing)|fulfillment|distribution|"
             r"process|scrum|story|community|change")

# Not-the-actual-holder modifiers (checked only right next to the role).
# NOTE: interim/acting are intentionally NOT here — an interim CEO is still the
# person running the company.
_DEMOTE = (r"deputy|vice|assistant|asst|associate|aspiring|former|ex|previous|"
           r"past|outgoing|retired|emeritus|honorary|elect|junior|jr|to the|"
           r"office of the|chief of staff")

_DEMOTE_RE  = re.compile(r"\b(?:%s)$" % _DEMOTE)      # ends the words just before the role
_SCOPE_END  = re.compile(r"\b(?:%s)$" % _SCOPE)
_FUNC_END   = re.compile(r"\b(?:%s)$" % _FUNCTION)
_SCOPE_HEAD = re.compile(r"^(?:%s)\b" % _SCOPE)       # starts the words just after the role
_FUNC_HEAD  = re.compile(r"^(?:%s)\b" % _FUNCTION)
_CONN_HEAD  = re.compile(r"^(?:of|for|to|in|the)\b")  # 'of/for/the' bridge to a qualifier

# Headline roles (order = label priority; any clean match wins).
_ROLE_CEO   = re.compile(r"\b(?:ceo|chief exec(?:utive)?(?: officer)?)\b")
_ROLE_CFO   = re.compile(r"\b(?:cfo|chief financ(?:e|ial)(?: officer)?)\b")
_ROLE_COO   = re.compile(r"\b(?:coo|chief operating(?: officer)?|chief operations(?: officer)?)\b")
_ROLE_PRES  = re.compile(r"\bpresident\b")
_ROLE_FOUND = re.compile(r"\b(?:cofounder|co founder|founder|founding (?:partner|member|team))\b")
_ROLE_OWNER = re.compile(r"\b(?:co owner|coowner|owner|proprietor)\b")

# Specific extra roles you asked for (function-locked; VP/SVP/EVP of Finance,
# Director/Head of Operations). These are NOT top-level-locked.
_ROLE_VPFIN = re.compile(r"\b(?:(?:e|s|a)?vp|vice president)(?: of| for|,)? financ(?:e|ial)\b")
_ROLE_DIROPS = re.compile(r"\b(?:director of operations|operations director|"
                          r"director,? operations|ops director|director of ops)\b")
_ROLE_HEADOPS = re.compile(r"\b(?:head of operations|head of ops|operations head)\b")

# --- Global disqualifiers: if any appears anywhere, skip the person entirely --
EXCLUDE_TERMS = [
    "assistant", "office of the", "chief of staff", "executive support",
    "former", "ex", "retired", "emeritus", "outgoing", "previous", "past",
    "deputy", "elect",
    "aspiring", "student", "intern", "internship", "trainee", "apprentice",
    "seeking", "open to work", "looking for", "unemployed", "job seeker", "candidate",
    "junior", "jr", "analyst", "coordinator", "associate", "specialist", "clerk",
    "advisor", "adviser", "board member", "non executive", "freelance",
    "self employed", "product owner", "process owner", "scrum",
]
_EXCLUDE_RE = re.compile(r"\b(?:" + "|".join(re.escape(t.strip()) for t in EXCLUDE_TERMS if t.strip())
                         + r")\b")


def _norm_title(t):
    """Lowercase, expand '&', drop all other punctuation to spaces (so
    'Co-Founder & CEO', 'COO/CFO', 'President, Sales' all normalise cleanly)."""
    t = (t or "").lower().replace(".", "").replace("&", " and ")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _clean_role(t, role_re, block_scope=True, block_function=True):
    """True if role_re occurs as a *clean* role — no demotion, and (optionally)
    no scope/function qualifier immediately before or after it."""
    for m in role_re.finditer(t):
        pre = " ".join(t[:m.start()].split()[-3:])          # up to 3 words before
        if _DEMOTE_RE.search(pre):
            continue
        if block_scope and _SCOPE_END.search(pre):
            continue
        if block_function and _FUNC_END.search(pre):
            continue
        tail = t[m.end():].lstrip()                          # words after the role
        tail = _CONN_HEAD.sub("", tail).lstrip()             # step over of/for/the
        tail = _CONN_HEAD.sub("", tail).lstrip()             # (e.g. 'of the americas')
        if block_scope and _SCOPE_HEAD.search(tail):
            continue
        if block_function and _FUNC_HEAD.search(tail):
            continue
        return True
    return False


def matches_target(title):
    """Return a canonical role label if the title truly is one of the target
    roles (top-level for the headline roles), else None. First match wins."""
    t = _norm_title(title)
    if not t or _EXCLUDE_RE.search(t):
        return None
    if _clean_role(t, _ROLE_CEO):                       return "CEO"
    if _clean_role(t, _ROLE_CFO):                       return "CFO"
    if _clean_role(t, _ROLE_COO):                       return "COO"
    if _ROLE_FOUND.search(t):                           return "Founder"
    if _clean_role(t, _ROLE_OWNER, block_scope=False,
                   block_function=False):               return "Owner"
    if _clean_role(t, _ROLE_PRES):                      return "President"
    if _ROLE_VPFIN.search(t):                           return "VP of Finance"
    if _ROLE_DIROPS.search(t):                          return "Director of Operations"
    if _ROLE_HEADOPS.search(t):                         return "Head of Operations"
    return None


LOCATIONS = ["United States", "Canada"]   # person_locations search filter; set to [] to disable

# Require contacts to be based in an allowed country (US or Canada). This is a
# second check on the person's actual country (search-side location filters can
# leak), applied at search time and again after enrichment. A blank/unknown
# country trusts the search filter.
REQUIRE_ALLOWED_COUNTRY = True
ALLOWED_COUNTRY_NAMES = {
    "united states", "united states of america", "usa", "us", "u s a",
    "canada", "ca",
}


def is_allowed_country(person):
    """True if the person is in an allowed country (US/Canada), judged by
    location_details.country. Unknown/blank country -> True (trusts the
    person_locations search filter)."""
    if not REQUIRE_ALLOWED_COUNTRY:
        return True
    ld = person.get("location_details") or {}
    country = re.sub(r"[^a-z ]", " ", (ld.get("country") or "").lower())
    country = re.sub(r"\s+", " ", country).strip()
    if country:
        return country in ALLOWED_COUNTRY_NAMES
    return True

MAX_PER_DOMAIN = 10                 # cap kept contacts per domain (0 = no cap) -> controls credit spend
SEARCH_PAGE_SIZE = 25              # people per search page (max 100)
MAX_SEARCH_PAGES = 10             # safety cap on pagination per domain

REVEAL_EMAIL = True
REVEAL_PHONE = True
POLL_MAX_MINUTES = 45

# ---- Email validation (Amplemarket /email-validations) ----------------------
# After the LinkedIn+Phone+Email gate, every surviving email is validated via
# Amplemarket's email-validation endpoint. Only contacts whose email lands in
# ACCEPTED_EMAIL_RESULTS are pushed to HubSpot; the rest are dropped and logged.
# Each validated address costs 1 Amplemarket email credit.
#   result values: "deliverable", "risky", "unknown", "undeliverable"
# "risky" is usually a catch-all/full mailbox — include it only if you want those.
VALIDATE_EMAILS = True
ACCEPTED_EMAIL_RESULTS = {"deliverable"}   # e.g. {"deliverable", "risky"} to keep catch-alls
EMAIL_VALIDATION_POLL_MAX_MINUTES = 30

# ---- HubSpot contact property names (edit to match your portal) -------------
ICP_PROPERTY = "icp_contact"            # boolean contact prop, set to "true" for every gated contact
LINKEDIN_PROPERTY = "hs_linkedin_url"   # <-- MUST match the contact property storing LinkedIn URLs
CONTACT_BATCH_PROPERTY = "lead_batch"   # <-- MUST match the internal name of your Lead Batch contact prop
BATCH_TAG = os.environ.get("LEAD_BATCH_TAG", "")  # <-- set via LEAD_BATCH_TAG env, or hardcode (e.g. "W2a")

# ---- Company-level actions (trigger workflow) -------------------------------
ADD_COMPANY_NOTE = True                 # write a summary Note on each enriched company (count + coverage)
COMPANY_ICP_DONE_PROPERTY = os.environ.get("COMPANY_DONE_PROP", "")  # optional company bool stamped true
                                        # after enrichment (use it to stop the workflow re-triggering). "" = off
NOTE_TO_COMPANY_ASSOC_TYPE_ID = 190     # HubSpot default association: Note -> Company

# ---- .env variable names this script will look for (first match wins) -------
HUBSPOT_TOKEN_ENV_NAMES = [
    "HUBSPOT_ACCESS_TOKEN", "HUBSPOT_TOKEN", "HUBSPOT_PRIVATE_APP_TOKEN",
    "HUBSPOT_API_KEY", "HS_ACCESS_TOKEN", "hubspot_token", "api_key",
]
AMPLEMARKET_TOKEN_ENV_NAMES = [
    "AMPLEMARKET_API_KEY", "AMPLEMARKET_TOKEN", "AMPLEMARKET_KEY",
    "AMPLE_API_KEY", "amplemarket_api_key",
]

INPUT_CSV_DEFAULT = "domains.csv"
HUBSPOT_BASE = "https://api.hubapi.com"
AMPLEMARKET_BASE = "https://api.amplemarket.com"


# ============================================================================
# tiny helpers (self-contained, no common.py)
# ============================================================================
def load_env_file(path=".env"):
    """Populate os.environ from a .env file (does not overwrite already-set vars)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:]
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def pick_env(names, label):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    sys.exit(f"Could not find a {label} in your .env. Looked for: {', '.join(names)}.\n"
             f"Add one of those to your .env (or edit the *_ENV_NAMES list at the top).")


def domain_of(value):
    """Normalize to a bare domain: strip scheme, www, path; handle emails."""
    s = (value or "").strip().lower()
    if not s:
        return ""
    if "@" in s and "//" not in s:            # looks like an email
        s = s.split("@", 1)[1]
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/")[0].split("?")[0].strip()
    return s


def normalize_linkedin(url):
    """Lowercase, strip scheme/www/query/trailing slash for stable comparison."""
    s = (url or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("?")[0].rstrip("/")
    return s


def extract_phone(person):
    """Return the best usable phone number string, preferring a mobile, skipping wrong numbers."""
    nums = person.get("phone_numbers") or []
    good = [p for p in nums if p.get("number")
            and not p.get("is_wrong_number") and not p.get("wrong_number")]
    if not good:
        return ""
    mobiles = [p for p in good if (p.get("type") or p.get("kind")) == "mobile"]
    chosen = mobiles[0] if mobiles else good[0]
    return (chosen.get("number") or "").strip()


def write_log_csv(name, rows):
    os.makedirs("logs", exist_ok=True)
    path = os.path.join("logs", f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    fields = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields or ["info"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def load_domains(spec):
    if not os.path.exists(spec):
        sys.exit(f"Input file not found: {spec}\n"
                 f"Put your domains in ./{INPUT_CSV_DEFAULT} or pass a path as the first argument.")
    with open(spec, newline="", encoding="utf-8") as f:
        text = f.read()
    header = text.splitlines()[0].lower() if text else ""
    if spec.lower().endswith((".csv", ".tsv")) or "domain" in header:
        rdr = csv.DictReader(io.StringIO(text))
        fields = rdr.fieldnames or []
        col = next((c for c in fields if c.strip().lower() == "domain"),
                   (fields[0] if fields else None))
        raw = [row.get(col, "") for row in rdr]
    else:
        raw = text.splitlines()
    out, seen = [], set()
    for d in raw:
        dom = domain_of(d)
        if dom and dom not in seen:
            seen.add(dom)
            out.append(dom)
    return out


# ============================================================================
# Amplemarket client
# ============================================================================
class Amplemarket:
    def __init__(self, api_key):
        self.base = AMPLEMARKET_BASE
        self.s = _session_with_retries()
        self.s.headers.update({"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"})

    def _err(self, r):
        try:
            errs = r.json().get("_errors")
            if errs:
                return "; ".join(f"{e.get('code')}: {e.get('detail') or e.get('title')}" for e in errs)
        except Exception:
            pass
        return f"HTTP {r.status_code}: {r.text[:200]}"

    def search_people(self, payload):
        r = self.s.post(f"{self.base}/people/search", json=payload, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(self._err(r))
        return r.json()

    def start_enrichment(self, leads, reveal_email, reveal_phone_numbers):
        payload = {"leads": leads, "reveal_email": reveal_email,
                   "reveal_phone_numbers": reveal_phone_numbers}
        r = self.s.post(f"{self.base}/people/enrichment-requests", json=payload, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(self._err(r))
        return r.json()

    def _get(self, path):
        url = path if path.startswith("http") else f"{self.base}{path}"
        return self.s.get(url, timeout=60)

    def poll_enrichment(self, request_id, max_wait_s):
        """Poll until terminal, then gather ALL result pages. Returns (status, results|None)."""
        path = f"/people/enrichment-requests/{request_id}"
        waited = 0
        while True:
            r = self._get(path)
            if r.status_code >= 400:
                raise RuntimeError(self._err(r))
            body = r.json()
            status = body.get("status")
            if status in ("completed", "canceled", "error"):
                break
            try:
                wait = max(5, int(r.headers.get("Retry-After", "10")))
            except (TypeError, ValueError):
                wait = 10
            if waited + wait > max_wait_s:
                return status, None
            time.sleep(wait)
            waited += wait
        results = list(body.get("results") or [])
        nxt = ((body.get("_links") or {}).get("next") or {}).get("href")
        while nxt:
            r = self._get(nxt)
            if r.status_code >= 400:
                break
            body = r.json()
            results.extend(body.get("results") or [])
            nxt = ((body.get("_links") or {}).get("next") or {}).get("href")
        return status, results

    def start_email_validation(self, emails):
        """POST /email-validations with up to 100k {"email": ...} entries.
        Returns the created object (has id + _links.self.href)."""
        payload = {"emails": [{"email": e} for e in emails]}
        r = self.s.post(f"{self.base}/email-validations", json=payload, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(self._err(r))
        return r.json()

    def poll_email_validation(self, self_href, max_wait_s):
        """Poll an email-validation operation until terminal, then gather ALL
        result pages. Returns (status, {email_lower: result_obj} | None)."""
        waited = 0
        while True:
            r = self._get(self_href)
            if r.status_code >= 400:
                raise RuntimeError(self._err(r))
            body = r.json()
            status = body.get("status")
            if status in ("completed", "canceled", "error"):
                break
            try:
                wait = max(5, int(r.headers.get("Retry-After", "10")))
            except (TypeError, ValueError):
                wait = 10
            if waited + wait > max_wait_s:
                return status, None
            time.sleep(wait)
            waited += wait
        out = {}
        for row in body.get("results") or []:
            em = (row.get("email") or "").strip().lower()
            if em:
                out[em] = row
        nxt = ((body.get("_links") or {}).get("next") or {}).get("href")
        while nxt:
            r = self._get(nxt)
            if r.status_code >= 400:
                break
            body = r.json()
            for row in body.get("results") or []:
                em = (row.get("email") or "").strip().lower()
                if em:
                    out[em] = row
            nxt = ((body.get("_links") or {}).get("next") or {}).get("href")
        return status, out


# ============================================================================
# HubSpot client
# ============================================================================
class HubSpot:
    def __init__(self, token):
        self.base = HUBSPOT_BASE
        self.s = _session_with_retries()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"})

    def _raise(self, r, ctx):
        raise RuntimeError(f"{ctx} -> HTTP {r.status_code}: {r.text[:300]}")

    def search(self, object_type, filter_groups, properties, limit=100):
        """Return all results across pages for a CRM search."""
        url = f"{self.base}/crm/v3/objects/{object_type}/search"
        out, after = [], None
        while True:
            body = {"filterGroups": filter_groups, "properties": properties, "limit": limit}
            if after:
                body["after"] = after
            r = self.s.post(url, json=body, timeout=60)
            if r.status_code >= 400:
                self._raise(r, f"search {object_type}")
            data = r.json()
            out.extend(data.get("results", []))
            after = (data.get("paging", {}).get("next", {}) or {}).get("after")
            if not after:
                return out

    def companies_by_domain(self, domains):
        """Map bare-domain -> {'id','name'} for domains that exist as companies."""
        mapping = {}
        for i in range(0, len(domains), 100):
            chunk = domains[i:i + 100]
            fg = [{"filters": [{"propertyName": "domain", "operator": "IN", "values": chunk}]}]
            for c in self.search("companies", fg, ["name", "domain"]):
                dom = domain_of(c.get("properties", {}).get("domain"))
                if dom and dom not in mapping:
                    mapping[dom] = {"id": c["id"], "name": c.get("properties", {}).get("name")}
        return mapping

    def batch_read_contacts_by_email(self, emails, properties):
        """Map lowercased email -> contact object (only for emails that exist)."""
        result = {}
        url = f"{self.base}/crm/v3/objects/contacts/batch/read"
        uniq = sorted({(e or "").strip().lower() for e in emails if e})
        for i in range(0, len(uniq), 100):
            chunk = uniq[i:i + 100]
            body = {"idProperty": "email", "properties": properties,
                    "inputs": [{"id": e} for e in chunk]}
            r = self.s.post(url, json=body, timeout=60)
            if r.status_code >= 400 and r.status_code != 207:
                self._raise(r, "batch read contacts by email")
            for obj in (r.json().get("results", []) if (r.ok or r.status_code == 207) else []):
                em = (obj.get("properties", {}).get("email") or "").strip().lower()
                if em:
                    result[em] = obj
        return result

    def find_contact_by_linkedin(self, linkedin_url, properties):
        """Return a contact object matching the LinkedIn URL, 'PROP_MISSING', or None."""
        if not linkedin_url or not LINKEDIN_PROPERTY:
            return None
        variants = {linkedin_url, linkedin_url.rstrip("/"), linkedin_url.rstrip("/") + "/"}
        fg = [{"filters": [{"propertyName": LINKEDIN_PROPERTY, "operator": "EQ", "value": v}]}
              for v in variants]
        try:
            results = self.search("contacts", fg, properties, limit=1)
        except RuntimeError as exc:
            if "not exist" in str(exc).lower() or "PROPERTY_DOESNT_EXIST" in str(exc):
                return "PROP_MISSING"
            raise
        return results[0] if results else None

    def create_contact(self, properties):
        url = f"{self.base}/crm/v3/objects/contacts"
        r = self.s.post(url, json={"properties": properties}, timeout=60)
        if r.status_code == 409:
            return None, "conflict"
        if r.status_code >= 400:
            self._raise(r, "create contact")
        return r.json()["id"], "created"

    def update_contact(self, contact_id, properties):
        url = f"{self.base}/crm/v3/objects/contacts/{contact_id}"
        r = self.s.patch(url, json={"properties": properties}, timeout=60)
        if r.status_code == 409:
            return "conflict"
        if r.status_code >= 400:
            self._raise(r, "update contact")
        return "updated"

    def associate_contact_company(self, contact_id, company_id):
        """Idempotent default (primary) contact->company association."""
        url = (f"{self.base}/crm/v4/objects/contacts/{contact_id}"
               f"/associations/default/companies/{company_id}")
        r = self.s.put(url, timeout=60)
        if r.status_code >= 400:
            self._raise(r, "associate contact->company")

    def create_company(self, properties):
        url = f"{self.base}/crm/v3/objects/companies"
        r = self.s.post(url, json={"properties": properties}, timeout=60)
        if r.status_code >= 400:
            self._raise(r, "create company")
        return r.json()["id"]

    def read_companies(self, company_ids):
        """Trigger mode: map bare-domain -> {'id','name'} for a list of company IDs."""
        mapping = {}
        for cid in company_ids:
            cid = str(cid).strip()
            if not cid:
                continue
            url = f"{self.base}/crm/v3/objects/companies/{cid}"
            r = self.s.get(url, params={"properties": "name,domain"}, timeout=60)
            if r.status_code >= 400:
                print(f"  [warn] could not read company {cid}: HTTP {r.status_code} {r.text[:150]}")
                continue
            props = r.json().get("properties", {})
            dom = domain_of(props.get("domain"))
            if not dom:
                print(f"  [warn] company {cid} has no domain — skipping.")
                continue
            mapping[dom] = {"id": cid, "name": props.get("name") or dom}
        return mapping

    def update_company(self, company_id, properties):
        url = f"{self.base}/crm/v3/objects/companies/{company_id}"
        r = self.s.patch(url, json={"properties": properties}, timeout=60)
        if r.status_code >= 400:
            self._raise(r, "update company")

    def create_note_on_company(self, company_id, html_body):
        """Create a Note engagement and associate it to the company (v3 inline association)."""
        url = f"{self.base}/crm/v3/objects/notes"
        payload = {
            "properties": {"hs_note_body": html_body,
                           "hs_timestamp": int(time.time() * 1000)},
            "associations": [{
                "to": {"id": str(company_id)},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                           "associationTypeId": NOTE_TO_COMPANY_ASSOC_TYPE_ID}],
            }],
        }
        r = self.s.post(url, json=payload, timeout=60)
        if r.status_code >= 400:
            self._raise(r, "create note on company")
        return r.json().get("id")


# ============================================================================
# main
# ============================================================================
CONTACT_PROPS = ["email", "firstname", "lastname", "jobtitle", "phone",
                 LINKEDIN_PROPERTY, ICP_PROPERTY]
if CONTACT_BATCH_PROPERTY:
    CONTACT_PROPS.append(CONTACT_BATCH_PROPERTY)


def split_name(person, search_info):
    first = (person.get("first_name") or "").strip()
    last = (person.get("last_name") or "").strip()
    if first or last:
        return first, last
    full = (person.get("name") or search_info.get("name") or "").strip()
    if not full:
        return "", ""
    parts = full.split()
    return parts[0], " ".join(parts[1:]) if len(parts) > 1 else ""


def main():
    ap = argparse.ArgumentParser(description="Find LinkedIn+Phone+Email contacts for a list of domains")
    ap.add_argument("input", nargs="?", default=INPUT_CSV_DEFAULT,
                    help=f"CSV/txt of domains (default: ./{INPUT_CSV_DEFAULT})")
    ap.add_argument("--company-ids", default=os.environ.get("HS_COMPANY_IDS", ""),
                    help="comma-separated HubSpot company IDs (TRIGGER MODE; ignores the domains CSV). "
                         "Can also be set via the HS_COMPANY_IDS env var.")
    ap.add_argument("--dry-run", action="store_true", help="plan only: no credits, no writes")
    ap.add_argument("--max-per-domain", type=int, default=MAX_PER_DOMAIN)
    ap.add_argument("--create-missing-companies", action="store_true",
                    help="create a HubSpot company for domains that don't have one yet")
    ap.add_argument("--resume", metavar="REQUEST_ID", help="resume a previous enrichment request")
    args = ap.parse_args()

    if not args.dry_run and CONTACT_BATCH_PROPERTY and not BATCH_TAG:
        sys.exit("BATCH_TAG is empty. Set BATCH_TAG at the top of the script to the lead batch "
                 "to stamp on every created/updated contact (or set CONTACT_BATCH_PROPERTY = \"\" "
                 "to disable batch tagging).")

    load_env_file(".env")
    hs_token = pick_env(HUBSPOT_TOKEN_ENV_NAMES, "HubSpot token")
    ample_key = pick_env(AMPLEMARKET_TOKEN_ENV_NAMES, "Amplemarket API key")

    company_ids = [c.strip() for c in (args.company_ids or "").split(",") if c.strip()]
    trigger_mode = bool(company_ids)
    cap = max(0, args.max_per_domain)

    hubspot = HubSpot(hs_token)
    ample = Amplemarket(ample_key)

    # ---- 1. resolve target companies ----------------------------------------
    if trigger_mode:
        print(f"== find_full_contacts | TRIGGER MODE | {len(company_ids)} company id(s) | "
              f"{len(TITLES)} titles | {'DRY RUN' if args.dry_run else 'LIVE'} ==")
        print(f"   gate: Email required (phone/LinkedIn optional) | cap/company: {cap or 'none'}\n")
        print("Reading trigger companies from HubSpot...")
        company_by_domain = hubspot.read_companies(company_ids)
        missing = []
    else:
        domains = load_domains(args.input)
        if not domains:
            sys.exit("No domains found in input.")
        print(f"== find_full_contacts | {len(domains)} domains | {len(TITLES)} titles | "
              f"{'DRY RUN' if args.dry_run else 'LIVE'} ==")
        print(f"   gate: Email required (phone/LinkedIn optional) | cap/domain: {cap or 'none'}\n")
        print("Resolving domains to HubSpot companies...")
        company_by_domain = hubspot.companies_by_domain(domains)
        missing = [d for d in domains if d not in company_by_domain]
        if missing:
            print(f"  {len(missing)} domains have no HubSpot company.")
            if args.create_missing_companies and not args.dry_run:
                print("  creating missing companies...")
                for d in missing:
                    try:
                        cid = hubspot.create_company({"name": d, "domain": d})
                        company_by_domain[d] = {"id": cid, "name": d}
                    except RuntimeError as exc:
                        print(f"    [error] {d}: {exc}")
                missing = [d for d in domains if d not in company_by_domain]
            else:
                print("  (skipping them — pass --create-missing-companies to add them)")

    resolvable = list(company_by_domain.keys())
    print(f"  {len(resolvable)} company/companies will be searched.\n")
    if not resolvable:
        sys.exit("No target companies resolved. Nothing to do.")

    # per-company summary stats (seeded to 0 so a company with 0 hits still gets a note)
    stats = {c["id"]: {"name": c["name"], "found": 0, "email": 0,
                       "linkedin": 0, "phone": 0, "pushed": 0}
             for c in company_by_domain.values()}

    def write_company_summaries():
        """Write the summary Note (and optional done-flag) on every company —
        including companies where 0 contacts were found."""
        if args.dry_run or not (ADD_COMPANY_NOTE or COMPANY_ICP_DONE_PROPERTY):
            return
        for cid, st in stats.items():
            if ADD_COMPANY_NOTE:
                n = st["found"]
                body = (f"<b>Amplemarket enrichment — {time.strftime('%Y-%m-%d %H:%M')} UTC</b><br>"
                        f"ICP contacts found: <b>{n}</b><br>"
                        f"Coverage: email {st['email']}/{n}, "
                        f"LinkedIn {st['linkedin']}/{n}, phone {st['phone']}/{n}<br>"
                        f"Contacts pushed to HubSpot (email required): "
                        f"<b>{st['pushed']}</b>")
                try:
                    hubspot.create_note_on_company(cid, body)
                except RuntimeError as exc:
                    print(f"  [warn] note failed for company {cid}: {exc}")
            if COMPANY_ICP_DONE_PROPERTY:
                try:
                    hubspot.update_company(cid, {COMPANY_ICP_DONE_PROPERTY: "true"})
                except RuntimeError as exc:
                    print(f"  [warn] company flag failed for {cid}: {exc}")

    log_rows = [{"stage": "resolve", "action": "skipped_no_company", "domain": d} for d in missing]

    # ---- 2. search each domain -----------------------------------------------
    print("Searching Amplemarket for matching people (LinkedIn required)...")
    candidates = {}     # normalized linkedin -> info
    leads = []
    filtered_titles = 0
    filtered_location = 0
    for dom in resolvable:
        company = company_by_domain[dom]
        found_here = 0
        for page in range(1, MAX_SEARCH_PAGES + 1):
            payload = {"company_domains": [dom], "person_titles": TITLES,
                       "person_title_exact_match": EXACT_TITLE_MATCH,
                       "page": page, "page_size": min(SEARCH_PAGE_SIZE, 100)}
            if LOCATIONS:
                payload["person_locations"] = LOCATIONS
            try:
                data = ample.search_people(payload)
            except RuntimeError as exc:
                print(f"  [error] search {dom}: {exc}")
                log_rows.append({"stage": "search", "action": "search_failed", "domain": dom,
                                 "detail": str(exc)})
                break
            results = data.get("results") or []
            for person in results:
                if cap and found_here >= cap:
                    break
                lin = normalize_linkedin(person.get("linkedin_url"))
                if not lin or lin in candidates:
                    continue
                if not matches_target(person.get("title")):     # keep target roles, drop the rest
                    filtered_titles += 1
                    continue
                if not is_allowed_country(person):               # US/Canada only
                    filtered_location += 1
                    continue
                candidates[lin] = {"linkedin_url": person.get("linkedin_url"),
                                   "name": person.get("name"), "title": person.get("title"),
                                   "domain": dom, "company_id": company["id"],
                                   "company_name": company["name"]}
                leads.append({"linkedin_url": person.get("linkedin_url")})
                found_here += 1
            pg = data.get("_pagination") or {}
            if (cap and found_here >= cap) or len(results) < payload["page_size"] \
                    or page >= (pg.get("total_pages") or page):
                break
    print(f"  {len(candidates)} on-target people (dropped {filtered_titles} off-target/excluded "
          f"titles, {filtered_location} out-of-region) across {len(resolvable)} domains.\n")

    for _info in candidates.values():                      # per-company: people found
        if _info["company_id"] in stats:
            stats[_info["company_id"]]["found"] += 1

    # ---- dry run stops here ---------------------------------------------------
    if args.dry_run:
        plan = log_rows + [{"stage": "would_enrich", "domain": i["domain"],
                            "company": i["company_name"], "name": i["name"],
                            "title": i["title"], "linkedin": lin}
                           for lin, i in candidates.items()]
        path = write_log_csv("find_full_contacts_plan", plan)
        print(f"Dry run complete — no credits spent, nothing written."
              f"\n  People to enrich (credit events): {len(candidates)}\n  Plan: {path}")
        return

    if not candidates and not args.resume:
        write_log_csv("find_full_contacts", log_rows)
        write_company_summaries()          # still note the company: "0 contacts found"
        print("Nothing to enrich — wrote 0-found note to the company.")
        return

    # ---- 3. enrich ------------------------------------------------------------
    if args.resume:
        request_id = args.resume
        print(f"Resuming enrichment request {request_id}...")
    else:
        print(f"Submitting enrichment for {len(leads)} people "
              f"(reveal email={REVEAL_EMAIL}, phone={REVEAL_PHONE})...")
        created = ample.start_enrichment(leads, REVEAL_EMAIL, REVEAL_PHONE)
        request_id = created.get("id")
        print(f"  request id: {request_id}")

    status, results = ample.poll_enrichment(request_id, POLL_MAX_MINUTES * 60)
    if results is None:
        sys.exit(f"Still '{status}' after {POLL_MAX_MINUTES} min. Resume later with:\n"
                 f"  python find_full_contacts.py {args.input} --resume {request_id}")
    if status == "error":
        sys.exit(f"Enrichment request {request_id} ended in error.")
    if status == "canceled":
        print("  [warn] request canceled — processing partial results")
    print(f"  {len(results)} results returned (status={status})\n")

    # ---- 4. gate + upsert -----------------------------------------------------
    gated = []   # (email, phone, linkedin_url, person, info)
    for res in results:
        person = res.get("result") or {}
        rstatus = res.get("status")
        lin_key = normalize_linkedin(res.get("linkedin_url") or person.get("linkedin_url"))
        info = candidates.get(lin_key)
        if info is None:
            log_rows.append({"stage": "match", "action": "unmatched_result",
                             "linkedin": lin_key, "result_status": rstatus})
            continue
        email = (person.get("email") or "").strip()
        phone = extract_phone(person)
        linkedin_url = person.get("linkedin_url") or info.get("linkedin_url")
        _cid = info["company_id"]                          # per-company coverage
        if _cid in stats:
            if email:
                stats[_cid]["email"] += 1
            if phone:
                stats[_cid]["phone"] += 1
            if linkedin_url:
                stats[_cid]["linkedin"] += 1
        if rstatus != "enriched":
            log_rows.append({"stage": "gate", "action": rstatus, "domain": info["domain"],
                             "company": info["company_name"], "name": info["name"]})
            continue
        if not email:                                     # EMAIL is the only hard requirement
            log_rows.append({"stage": "gate", "action": "skipped_no_email",
                             "domain": info["domain"], "company": info["company_name"],
                             "name": info["name"], "phone": phone, "linkedin": linkedin_url})
            continue
        if not is_allowed_country(person):                # US/Canada only (enriched country)
            log_rows.append({"stage": "gate", "action": "skipped_out_of_region",
                             "domain": info["domain"], "company": info["company_name"],
                             "name": info["name"],
                             "country": (person.get("location_details") or {}).get("country")})
            continue
        gated.append((email, phone, linkedin_url, person, info))

    print(f"Passed the email gate: {len(gated)} of {len(candidates)} "
          f"(phone/LinkedIn optional).\n")

    # ---- 4b. email validation -------------------------------------------------
    # Validate every gated email via Amplemarket, keep only accepted results.
    if VALIDATE_EMAILS and gated:
        emails_to_check = sorted({g[0].strip().lower() for g in gated if g[0]})
        print(f"Validating {len(emails_to_check)} email(s) via Amplemarket "
              f"(accept: {', '.join(sorted(ACCEPTED_EMAIL_RESULTS))})...")
        try:
            created_ev = ample.start_email_validation(emails_to_check)
            self_href = ((created_ev.get("_links") or {}).get("self") or {}).get("href") \
                or f"/email-validations/{created_ev.get('id')}"
            ev_status, ev_map = ample.poll_email_validation(
                self_href, EMAIL_VALIDATION_POLL_MAX_MINUTES * 60)
        except RuntimeError as exc:
            sys.exit(f"Email validation request failed: {exc}")

        if ev_map is None:
            sys.exit(f"Email validation still '{ev_status}' after "
                     f"{EMAIL_VALIDATION_POLL_MAX_MINUTES} min. Re-run later.")
        if ev_status == "error":
            sys.exit("Email validation ended in error.")
        if ev_status == "canceled":
            print("  [warn] email validation canceled — using partial results")

        kept, dropped = [], 0
        for tup in gated:
            email = tup[0]
            info = tup[4]
            row = ev_map.get(email.strip().lower())
            result = (row or {}).get("result")
            catch_all = (row or {}).get("catch_all")
            if result in ACCEPTED_EMAIL_RESULTS:
                kept.append(tup)
            else:
                dropped += 1
                log_rows.append({"stage": "email_validation",
                                 "action": "skipped_email_" + (result or "no_result"),
                                 "domain": info["domain"], "company": info["company_name"],
                                 "name": info["name"], "email": email,
                                 "email_result": result or "", "catch_all": catch_all})
        print(f"  email validation: kept {len(kept)}, dropped {dropped} "
              f"(status={ev_status}).\n")
        gated = kept

    # prefetch existing contacts by email (email-fallback match)
    email_map = hubspot.batch_read_contacts_by_email([g[0] for g in gated], CONTACT_PROPS) if gated else {}
    prop_missing_warned = False
    created_n = updated_n = assoc_n = conflict_n = nochange_n = 0
    batch_conflict_n = 0
    batch_conflicts = []   # existing contacts already tagged with a DIFFERENT lead batch (left untouched)

    if gated:
        print(f"Upserting {len(gated)} contacts in HubSpot...")
    for email, phone, linkedin_url, person, info in gated:
        first, last = split_name(person, info)
        desired = {"firstname": first, "lastname": last,
                   "jobtitle": person.get("title") or info.get("title") or "",
                   LINKEDIN_PROPERTY: linkedin_url}

        # locate existing: LinkedIn first, then email
        existing = hubspot.find_contact_by_linkedin(linkedin_url, CONTACT_PROPS)
        if existing == "PROP_MISSING":
            if not prop_missing_warned:
                print(f"  [warn] contact property '{LINKEDIN_PROPERTY}' doesn't exist — "
                      f"matching by email only. Fix LINKEDIN_PROPERTY at the top of the script.")
                prop_missing_warned = True
            existing = None
        if not existing:
            existing = email_map.get(email.lower())

        if existing:
            cur = existing.get("properties", {})
            update = {}
            for prop, val in desired.items():                     # fill blanks only
                if val and not (cur.get(prop) or "").strip():
                    update[prop] = val
            if email and (cur.get("email") or "").strip().lower() != email.lower():   # replace if different
                update["email"] = email
            if phone and (cur.get("phone") or "").strip() != phone:
                update["phone"] = phone
            # ICP Contact: ensure "true" — every gated contact has passed the title/US/full-contact criteria
            if ICP_PROPERTY and (cur.get(ICP_PROPERTY) or "").strip().lower() != "true":
                update[ICP_PROPERTY] = "true"
            # lead batch: fill ONLY when blank; never overwrite an existing batch. If it's already
            # tagged with a different batch, leave it and record it for review.
            existing_batch = (cur.get(CONTACT_BATCH_PROPERTY) or "").strip() if CONTACT_BATCH_PROPERTY else ""
            batch_kept = ""
            if CONTACT_BATCH_PROPERTY and BATCH_TAG:
                if not existing_batch:
                    update[CONTACT_BATCH_PROPERTY] = BATCH_TAG
                elif existing_batch != BATCH_TAG:
                    batch_kept = existing_batch
                    batch_conflict_n += 1
                    batch_conflicts.append({"contact_id": existing["id"], "email": email,
                                            "name": (info.get("name") or "").strip(),
                                            "domain": info["domain"], "company": info["company_name"],
                                            "existing_batch": existing_batch, "requested_batch": BATCH_TAG})

            action = "no_change"
            if update:
                if hubspot.update_contact(existing["id"], update) == "conflict":
                    conflict_n += 1
                    action = "update_conflict_email_taken"
                else:
                    updated_n += 1
                    action = "updated"
                    if info["company_id"] in stats:
                        stats[info["company_id"]]["pushed"] += 1
            else:
                nochange_n += 1
            try:
                hubspot.associate_contact_company(existing["id"], info["company_id"])
                assoc_n += 1
            except RuntimeError as exc:
                action += f"; assoc_failed({exc})"
            log_rows.append({"stage": "upsert", "action": action, "contact_id": existing["id"],
                             "email": email, "domain": info["domain"], "company": info["company_name"],
                             "changed": ",".join(sorted(update)) if update else "",
                             "existing_batch_kept": batch_kept})
        else:
            props = {"email": email}
            if phone:
                props["phone"] = phone
            for prop, val in desired.items():             # skip any blank (missing linkedin/title/name)
                if val:
                    props[prop] = val
            if ICP_PROPERTY:
                props[ICP_PROPERTY] = "true"
            if CONTACT_BATCH_PROPERTY and BATCH_TAG:
                props[CONTACT_BATCH_PROPERTY] = BATCH_TAG
            cid, res_c = hubspot.create_contact(props)
            if res_c == "conflict":
                # race: exists by email now -> update + associate
                again = hubspot.batch_read_contacts_by_email([email], CONTACT_PROPS).get(email.lower())
                if again:
                    ag_cur = again.get("properties", {})
                    recover = {"email": email}
                    if phone:
                        recover["phone"] = phone
                    if ICP_PROPERTY:
                        recover[ICP_PROPERTY] = "true"
                    ag_batch = (ag_cur.get(CONTACT_BATCH_PROPERTY) or "").strip() if CONTACT_BATCH_PROPERTY else ""
                    ag_batch_kept = ""
                    if CONTACT_BATCH_PROPERTY and BATCH_TAG:
                        if not ag_batch:
                            recover[CONTACT_BATCH_PROPERTY] = BATCH_TAG
                        elif ag_batch != BATCH_TAG:
                            ag_batch_kept = ag_batch
                            batch_conflict_n += 1
                            batch_conflicts.append({"contact_id": again["id"], "email": email,
                                                    "name": (info.get("name") or "").strip(),
                                                    "domain": info["domain"], "company": info["company_name"],
                                                    "existing_batch": ag_batch, "requested_batch": BATCH_TAG})
                    hubspot.update_contact(again["id"], recover)
                    hubspot.associate_contact_company(again["id"], info["company_id"])
                    updated_n += 1
                    assoc_n += 1
                    if info["company_id"] in stats:
                        stats[info["company_id"]]["pushed"] += 1
                    log_rows.append({"stage": "upsert", "action": "existed_then_updated",
                                     "contact_id": again["id"], "email": email,
                                     "domain": info["domain"], "company": info["company_name"],
                                     "existing_batch_kept": ag_batch_kept})
                else:
                    conflict_n += 1
                    log_rows.append({"stage": "upsert", "action": "create_conflict", "email": email,
                                     "domain": info["domain"], "company": info["company_name"]})
                continue
            hubspot.associate_contact_company(cid, info["company_id"])
            created_n += 1
            assoc_n += 1
            if info["company_id"] in stats:
                stats[info["company_id"]]["pushed"] += 1
            log_rows.append({"stage": "upsert", "action": "created", "contact_id": cid,
                             "email": email, "domain": info["domain"], "company": info["company_name"]})

    path = write_log_csv("find_full_contacts", log_rows)

    # ---- 5. per-company summary note + optional "done" flag ------------------
    write_company_summaries()

    batch_conflict_path = ""
    if batch_conflicts:
        batch_conflict_path = write_log_csv("find_full_contacts_batch_conflicts", batch_conflicts)
        print(f"\n[!] {batch_conflict_n} contact(s) already had a DIFFERENT lead batch — left unchanged:")
        for bc in batch_conflicts[:20]:
            who = bc["name"] or bc["email"]
            print(f"      {who} ({bc['email']}) @ {bc['company']}: "
                  f"has '{bc['existing_batch']}', not overwritten with '{bc['requested_batch']}'")
        if batch_conflict_n > 20:
            print(f"      ... and {batch_conflict_n - 20} more (see CSV)")

    print(f"\nDone."
          f"\n  Searched      : {len(resolvable)} domains"
          f"\n  With LinkedIn : {len(candidates)}"
          f"\n  Kept (email)  : {len(gated)} (email required; phone/LinkedIn optional"
          + (f"+validated" if VALIDATE_EMAILS else "") + ")"
          f"\n  Created       : {created_n}"
          f"\n  Updated       : {updated_n}  (blanks filled / email+phone replaced)"
          f"\n  No change     : {nochange_n}"
          f"\n  Associated    : {assoc_n}"
          f"\n  Conflicts     : {conflict_n}"
          f"\n  Batch kept    : {batch_conflict_n}  (already tagged with a different lead batch)"
          f"\n  Log: {path}"
          + (f"\n  Batch conflicts: {batch_conflict_path}" if batch_conflict_path else ""))


if __name__ == "__main__":
    main()
