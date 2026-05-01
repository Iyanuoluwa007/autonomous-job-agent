"""
Job Scraper — scrapes JD text from any job URL using Playwright.
Handles JS-heavy ATS boards (Greenhouse, Lever, Workday, Indeed, LinkedIn).
"""

import logging, re
log = logging.getLogger("scraper")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sync_playwright = None

# ATS-specific content selectors (tried in order)
CONTENT_SELECTORS = [
    ".jobs-description",          # LinkedIn
    "#job-details",               # LinkedIn alt
    ".content-intro",             # Lever
    "#job_description",           # Greenhouse
    ".job-description",           # generic
    ".description",               # generic
    "article",                    # generic
    "main",                       # fallback
    "body",                       # last resort
]

TITLE_SELECTORS = [
    "h1.posting-headline",        # Lever
    "h1.app-title",               # Greenhouse
    "h1.jobs-unified-top-card__job-title",  # LinkedIn
    "h1",                         # generic
]

# === PATCH 19C: aggregator hosts =========================================
# Domains where the URL host name is NOT the employer. If _extract_company
# falls back to the host for any of these, return "" instead so process_job
# skips the URL entirely rather than shipping letters with the aggregator
# name as the employer. ATS patterns (lever, greenhouse, workday) still
# take priority and extract the real company.
# === PATCH 25 (C): aggregator JSON-LD whitelist ==========================
# Some aggregators serve real schema.org JobPosting JSON-LD that lets patch
# 24 recover a legitimate employer name. CV-Library is NOT one of them in
# practice (inconsistent, sometimes serves but often scraped as aggregator
# junk with rotating session tokens creating duplicate-application storms).
# Reed is confirmed clean. Only hosts in this set are allowed to bypass
# the AGGREGATOR_HOSTS skip via patch 24 override.
AGGREGATOR_JSONLD_WHITELIST = frozenset({
    "reed.co.uk", "reed.com",
})


def _host_in_jsonld_whitelist(url: str) -> bool:
    try:
        from urllib.parse import urlsplit
        netloc = urlsplit(url).netloc.lower().removeprefix("www.")
        return any(netloc == s or netloc.endswith("." + s)
                   for s in AGGREGATOR_JSONLD_WHITELIST)
    except Exception:
        return False
# === END PATCH 25 (C, helpers) ===========================================


AGGREGATOR_HOSTS = {
    "cv-library.co.uk", "cv-library.com",
    "reed.co.uk", "reed.com",
    "indeed.com", "indeed.co.uk", "uk.indeed.com",
    "linkedin.com", "www.linkedin.com",
    "totaljobs.com", "www.totaljobs.com",
    "jobserve.com", "www.jobserve.com",
    "jobsite.co.uk", "www.jobsite.co.uk",
    "monster.co.uk", "monster.com",
    "glassdoor.com", "glassdoor.co.uk",
    "cwjobs.co.uk",
    "adzuna.co.uk", "adzuna.com",
    # Just-the-slug fallbacks that _extract_company produces from URL paths
    # like linkedin.com/jobs/view/.../uk/... -> "uk" / "us" / "ca"
    "uk", "us", "ca",
}
# === END PATCH 19C =======================================================


# === PATCH 21 (O-1): generic subdomains constant =========================
# Subdomains that are typically a "careers landing" prefix, NOT the actual
# brand. When the URL has one of these as the leading subdomain, take the
# next component as the company name. Example: careers.rolls-royce.com
# has "careers" as parts[0], so we use parts[1] = "rolls-royce" -> "Rolls-Royce".
# Curated to match the existing _GENERIC_EMAIL_PREFIXES set further down,
# but kept as separate constant since the email use case is different.
GENERIC_SUBDOMAINS = frozenset({
    "careers", "career",
    "jobs", "job",
    "apply", "application", "applications",
    "work", "join", "talent",
    "hiring", "recruit", "recruiting", "recruitment",
    "hr", "people",
})
# === END PATCH 21 (O-1) ==================================================


def _extract_company(url: str) -> str:
    url = url.lower()
    for pattern in [
        r"jobs\.lever\.co/([^/]+)",
        r"boards\.greenhouse\.io/([^/]+)",
        r"([^.]+)\.workday\.com",
        r"([^.]+)\.greenhouse\.io",
    ]:
        m = re.search(pattern, url)
        if m:
            return m.group(1).replace("-", " ").title()
    # === PATCH 19C: aggregator fallback check ============================
    # Strip host from URL. If it's an aggregator, return "" — agent.py will
    # skip with "unknown_employer:aggregator" rather than ship a broken
    # letter or burn LLM quota generating one.
    host = url.replace("https://", "").replace("http://", "").split("/")[0]
    host_lower = host.lower()
    # Normalise host variants: strip www. prefix so "www.cv-library.co.uk"
    # matches "cv-library.co.uk" in the set.
    host_clean = host_lower.lstrip().removeprefix("www.")
    # Also extract the bare slug (e.g. "cv-library" from "cv-library.co.uk")
    slug = host_clean.split(".")[0]
    if host_clean in AGGREGATOR_HOSTS or host_lower in AGGREGATOR_HOSTS or slug in AGGREGATOR_HOSTS:
        return ""
    # === PATCH 21 (O-1): generic subdomain handling ======================
    # Many companies host their careers site at a generic subdomain like
    # careers.rolls-royce.com or jobs.wayve.ai. Without this fix,
    # _extract_company would return "Careers" / "Jobs" as the company name
    # and ship cover letters that say "at Careers" instead of "at Rolls-Royce".
    # If the first slug is a known generic, fall back to the SECOND domain
    # component (the actual brand).
    parts = host_clean.split(".")
    if slug in GENERIC_SUBDOMAINS and len(parts) >= 3:
        slug = parts[1]
    # === END PATCH 21 (O-1) ==============================================
    # Not an aggregator -- original fallback: title-case the subdomain slug
    return slug.title()
    # === END PATCH 19C ====================================================


# === PATCH 24: JSON-LD JobPosting extraction =================================
# Some aggregator sites (notably Reed.co.uk) embed schema.org JobPosting
# structured data in <script type="application/ld+json"> tags. This often
# contains hiringOrganization.name — the REAL employer behind the listing —
# which lets us recover real company names from URLs that would otherwise hit
# AGGREGATOR_HOSTS and be skipped.
#
# CV-Library does NOT expose JobPosting JSON-LD (only BreadcrumbList) so this
# patch only helps Reed and any future site with proper schema.org JobPosting
# markup. CV-Library extraction is a future patch.
import json as _json

_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.+?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _extract_jsonld_job_posting(html: str) -> dict | None:
    """Return first JobPosting JSON-LD item from page HTML, or None.

    Tolerant of malformed JSON, missing fields, single-item-vs-array variation,
    and @graph wrappers. Never raises.
    """
    if not html:
        return None
    try:
        for raw_block in _JSONLD_RE.findall(html):
            block = raw_block.strip()
            if not block:
                continue
            try:
                data = _json.loads(block)
            except _json.JSONDecodeError:
                continue
            # Normalise to a flat list of items
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                if "@graph" in data and isinstance(data["@graph"], list):
                    items = data["@graph"]
                else:
                    items = [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                at_type = item.get("@type") or item.get("type") or ""
                # @type may be a list or a string
                type_str = " ".join(at_type) if isinstance(at_type, list) else str(at_type)
                if "JobPosting" in type_str:
                    return item
    except Exception:
        # Any unexpected error — silent fallback
        pass
    return None


def _hiring_org_name(job_posting: dict) -> str:
    """Extract hiringOrganization.name from a JobPosting dict.

    hiringOrganization may be:
      - dict with "name" field (common case)
      - string (rare; the name itself)
      - missing
    """
    if not isinstance(job_posting, dict):
        return ""
    org = job_posting.get("hiringOrganization")
    if isinstance(org, dict):
        return str(org.get("name") or "").strip()
    if isinstance(org, str):
        return org.strip()
    return ""


def _name_is_aggregator(name: str) -> bool:
    """True if extracted name matches an aggregator slug (Reed self-tagged
    itself as the employer, etc.) — guard against shipping
    'applying at Reed' even when JSON-LD is present.
    """
    if not name:
        return False
    n = name.lower().strip()
    n_slug = re.sub(r'[^a-z0-9]+', '', n)
    aggregator_slugs = {
        "reed", "cvlibrary", "indeed", "linkedin", "totaljobs",
        "jobserve", "jobsite", "monster", "glassdoor", "cwjobs",
    }
    return n_slug in aggregator_slugs
# === END PATCH 24 (helper) ==================================================


# === PATCH 14: recruiter email extraction ==================================
# Generic email prefixes that usually mean "no named recruiter".
# When a JD has ONLY these, we still keep the first one as fallback.
# When a JD has both, we prefer the personal one.
_GENERIC_EMAIL_PREFIXES = {
    "info", "hello", "contact", "support", "help", "admin",
    "careers", "career", "jobs", "job", "apply", "application",
    "recruiting", "recruitment", "recruiter", "talent", "hr",
    "people", "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "webmaster",
}

_EMAIL_REGEX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)


def _is_personal_email(addr: str) -> bool:
    """True if the local-part (before @) looks like a person, not a team alias."""
    try:
        local = addr.split("@", 1)[0].lower()
    except Exception:
        return False
    # Strip digits / separators to compare against the generic set.
    clean = re.sub(r"[._\-0-9]", "", local)
    return clean not in _GENERIC_EMAIL_PREFIXES


def extract_recruiter_email(jd_text: str) -> str:
    """Extract a contact email from JD text.
    Prefers personal addresses; falls back to careers@/recruiting@ if nothing
    else. Returns empty string if no email is present at all.
    """
    if not jd_text:
        return ""
    matches = _EMAIL_REGEX.findall(jd_text)
    if not matches:
        return ""
    # Dedup while preserving order.
    seen = set()
    uniq = []
    for m in matches:
        low = m.lower()
        if low not in seen:
            seen.add(low)
            uniq.append(m)
    # Prefer first personal-looking email.
    for m in uniq:
        if _is_personal_email(m):
            return m
    # Fallback: first generic email (careers@, recruiting@, etc.)
    return uniq[0]


# === END PATCH 14 =========================================================


class JobScraper:
    def scrape(self, url: str) -> dict:
        if not sync_playwright:
            return {"url": url, "title": "Unknown", "company": _extract_company(url), "description": ""}

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ))
            try:
                page.goto(url, timeout=25_000)
                page.wait_for_load_state("networkidle", timeout=12_000)

                # Title
                title = page.title()
                for sel in TITLE_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            t = el.inner_text().strip()
                            if t and len(t) < 200:
                                title = t
                                break
                    except Exception:
                        pass

                # JD Content
                description = ""
                for sel in CONTENT_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            text = el.inner_text().strip()
                            if len(text) > 200:
                                description = text
                                break
                    except Exception:
                        pass

                recruiter = extract_recruiter_email(description)

                # === PATCH 24: JSON-LD override of company ================
                # Default to URL-based company extraction (which returns ""
                # for aggregator hosts). If JSON-LD JobPosting present and
                # hiringOrganization.name is non-empty AND not itself an
                # aggregator slug, override company. Also adopt cleaner
                # JSON-LD title if present (Reed often has cleaner titles
                # than the page <title> tag).
                company = _extract_company(url)
                try:
                    page_html = page.content()
                except Exception:
                    page_html = ""
                # === PATCH 25 (C, gate): only apply JSON-LD override ===
                # for aggregator URLs if the host is on the whitelist.
                # Otherwise, even if JSON-LD is present, keep company=""
                # so process_job skips the URL. Prevents CV-Library-style
                # duplicate-application storms.
                from urllib.parse import urlsplit as _urlsplit_p25
                _host = _urlsplit_p25(url).netloc.lower().removeprefix("www.")
                _is_aggregator = any(
                    _host == s or _host.endswith("." + s)
                    for s in AGGREGATOR_HOSTS
                )
                _is_whitelisted = _host_in_jsonld_whitelist(url)
                if _is_aggregator and not _is_whitelisted:
                    # Known aggregator, not whitelisted — skip JSON-LD override.
                    if _extract_jsonld_job_posting(page_html):
                        log.info(
                            f"[SCRAPER] PATCH 25: suppressed JSON-LD override "
                            f"for non-whitelisted aggregator host {_host!r}"
                        )
                    jsonld_job = None
                else:
                    jsonld_job = _extract_jsonld_job_posting(page_html)
                # === END PATCH 25 (C, gate) ===========================
                if jsonld_job:
                    jsonld_employer = _hiring_org_name(jsonld_job)
                    if jsonld_employer and not _name_is_aggregator(jsonld_employer):
                        log.info(
                            f"[SCRAPER] JSON-LD employer override: "
                            f"url-extracted={company!r} -> "
                            f"jsonld={jsonld_employer!r} for {url}"
                        )
                        company = jsonld_employer
                    jsonld_title = (jsonld_job.get("title") or "").strip()
                    if jsonld_title and len(jsonld_title) < 200:
                        # Only override if URL-derived title looks generic
                        # (e.g. "Reed | Job Title" style). Keep it simple:
                        # always prefer JSON-LD title — it's curated.
                        title = jsonld_title
                # === END PATCH 24 =========================================

                return {
                    "url"            : url,
                    "title"          : title,
                    "company"        : company,
                    "description"    : description[:8000],
                    "recruiter_email": recruiter,
                }
            except PWTimeout:
                log.warning(f"[SCRAPER] Timeout: {url}")
                return {"url": url, "title": "Timeout", "company": _extract_company(url), "description": ""}
            finally:
                browser.close()
