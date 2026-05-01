#!/usr/bin/env python3
"""
tools/discovery/weekly_sweep.py  (v2 — Brave fallback + diagnostic logging)

System A of A+B discovery pipeline. Runs weekly from GitHub Actions.

v2 changes from v1:
  - Added Brave Search API as primary backend (2000 queries/mo free tier,
    much higher rate limits than DDG, works from GitHub IPs)
  - DDG becomes fallback when Brave returns nothing or key missing
  - Detailed rejection-reason logging to diagnose funnel collapse
  - Skip validation+commit entirely when ALL queries return zero raw results
    (DDG block scenario) to avoid committing empty {}
  - Accept query backend via env vars: BRAVE_API_KEY, no arg changes

Pipeline unchanged:
  1. Load existing discovered_companies.json + BUILTIN_CAREER_PAGES
  2. For each of 17 queries: Brave (if key) -> DDG fallback
  3. Filter candidates, reject known aggregators/noise
  4. HEAD + GET validate
  5. Merge new entries, write file
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, quote_plus, urlunparse, unquote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_ROOT        = Path(__file__).resolve().parents[2]
DISCOVERED       = REPO_ROOT / "discovered_companies.json"
FINDER_PY        = REPO_ROOT / "scrapers" / "finder.py"
DRY_RUN          = os.environ.get("DRY_RUN", "false").lower() == "true"
BRAVE_API_KEY    = os.environ.get("BRAVE_API_KEY", "").strip()

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 15
RATE_LIMIT_S = 1.2          # Brave: 1 req/s free tier. Sleep 1.2s to be safe.
DDG_RATE_LIMIT_S = 2.0
MAX_CANDIDATES_PER_QUERY = 15
MIN_CAREERS_KEYWORDS_IN_BODY = 2

QUERIES = [
    "AI Systems Engineer company UK careers hiring",
    "Computer Vision Engineer company UK careers hiring",
    "Machine Learning Engineer company UK careers hiring",
    "Autonomous Systems Engineer company UK careers hiring",
    "robotics AI startup UK careers jobs site",
    "computer vision startup London Manchester hiring",
    "autonomous systems company UK engineering jobs",
    "machine learning startup UK careers page",
    "embedded systems AI company UK careers",
    "perception autonomy startup UK jobs site",
    "UK robotics scaleup hiring 2025 2026",
    "autonomous vehicle startup UK careers",
    "drone robotics company UK engineering roles",
    "medical robotics startup UK jobs",
    "agricultural robotics company UK careers",
    "industrial automation AI UK startup hiring",
    "SLAM navigation robotics startup UK",
]

AGGREGATOR_HOSTS = {
    "linkedin.com", "www.linkedin.com", "uk.linkedin.com", "ca.linkedin.com",
    "indeed.com", "www.indeed.com", "uk.indeed.com",
    "cv-library.co.uk", "www.cv-library.co.uk",
    "reed.co.uk", "www.reed.co.uk",
    "glassdoor.com", "www.glassdoor.com", "www.glassdoor.co.uk",
    "welcometothejungle.com", "www.welcometothejungle.com",
    "otta.com", "app.otta.com",
    "totaljobs.com", "www.totaljobs.com",
    "adzuna.co.uk", "www.adzuna.co.uk",
    "jobs.theguardian.com", "jobserve.com", "monster.co.uk",
    "roboticsjobs.co.uk", "www.roboticsjobs.co.uk",
}

NOISE_HOSTS = {
    "twitter.com", "x.com", "medium.com", "crunchbase.com", "wikipedia.org",
    "en.wikipedia.org", "youtube.com", "www.youtube.com", "duckduckgo.com",
    "google.com", "www.google.com", "bing.com", "www.bing.com",
    "facebook.com", "www.facebook.com", "instagram.com",
    "news.ycombinator.com", "reddit.com", "www.reddit.com", "old.reddit.com",
    "github.com", "www.github.com", "angel.co", "wellfound.com",
    "blog.google", "www.blog.google",
}

ATS_HOSTS = {
    "boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
    "jobs.smartrecruiters.com", "apply.workable.com",
}

CAREERS_PATH_PATTERNS = re.compile(
    r"/(careers?|jobs?|join[-_]us|work[-_]with[-_]us|hiring|join[-_]the[-_]team|"
    r"openings|positions|vacancies|employment)/?(\?|$|#)",
    re.IGNORECASE,
)

CAREERS_BODY_KEYWORDS = [
    "career", "careers", "job", "jobs", "hiring", "positions", "openings",
    "join us", "join our team", "work with us", "vacanc",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("discovery")

# Global reject-reason counter for end-of-run diagnostics
reject_counts: Counter = Counter()


# ---------------------------------------------------------------------------
# State loaders (unchanged from v1)
# ---------------------------------------------------------------------------
def load_discovered() -> dict:
    if not DISCOVERED.exists():
        return {}
    try:
        data = json.loads(DISCOVERED.read_text() or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def load_builtin_pages() -> set[str]:
    if not FINDER_PY.exists():
        return set()
    try:
        src = FINDER_PY.read_text()
        m = re.search(r"BUILTIN_CAREER_PAGES\s*=\s*\[(.*?)\n\]", src, re.DOTALL)
        return set(re.findall(r'"(https?://[^"]+)"', m.group(1))) if m else set()
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------
def brave_search(query: str, max_results: int = 15) -> list[str]:
    """Primary: Brave Search API. Requires BRAVE_API_KEY env var."""
    if not BRAVE_API_KEY:
        return []
    url = "https://api.search.brave.com/res/v1/web/search"
    try:
        resp = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": BRAVE_API_KEY,
            },
            params={"q": query, "count": max_results, "country": "GB"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning(f"[BRAVE] {query!r} -> request error: {e}")
        return []

    if resp.status_code == 429:
        log.warning(f"[BRAVE] {query!r} -> 429 rate-limited")
        return []
    if resp.status_code != 200:
        log.warning(f"[BRAVE] {query!r} -> HTTP {resp.status_code}")
        return []

    try:
        data = resp.json()
        results = data.get("web", {}).get("results", []) or []
        urls = [r.get("url") for r in results if r.get("url")]
        log.info(f"[BRAVE] {query!r} -> {len(urls)} results")
        return urls
    except Exception as e:
        log.warning(f"[BRAVE] {query!r} -> parse error: {e}")
        return []


def ddg_search(query: str, max_results: int = 15) -> list[str]:
    """Fallback: DDG html endpoint."""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        resp = requests.get(
            url, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        log.warning(f"[DDG] {query!r} -> request error: {e}")
        return []

    if resp.status_code != 200:
        log.warning(f"[DDG] {query!r} -> HTTP {resp.status_code}")
        return []

    body = resp.text
    if len(body) < 500 or any(
        k in body.lower() for k in ("captcha", "unusual traffic", "anomal")
    ):
        log.warning(f"[DDG] {query!r} -> blocked-looking ({len(body)} bytes)")
        return []

    soup = BeautifulSoup(body, "lxml")
    urls: list[str] = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                href = unquote(m.group(1))
        if href.startswith("http"):
            urls.append(href)
            if len(urls) >= max_results:
                break

    log.info(f"[DDG] {query!r} -> {len(urls)} results")
    return urls


def search(query: str) -> list[str]:
    """Brave first (if key), DDG fallback."""
    if BRAVE_API_KEY:
        time.sleep(RATE_LIMIT_S)
        results = brave_search(query, MAX_CANDIDATES_PER_QUERY)
        if results:
            return results
        log.info(f"[FALLBACK] Brave returned empty for {query!r}, trying DDG")
    time.sleep(DDG_RATE_LIMIT_S)
    return ddg_search(query, MAX_CANDIDATES_PER_QUERY)


# ---------------------------------------------------------------------------
# URL filter + validate (unchanged logic, added reject-reason tracking)
# ---------------------------------------------------------------------------
def canonical_url(u: str) -> str:
    try:
        p = urlparse(u)
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme, p.netloc.lower(), path, "", "", ""))
    except Exception:
        return u.rstrip("/")


def looks_like_careers_url(u: str) -> tuple[bool, str]:
    p = urlparse(u)
    host = (p.netloc or "").lower()
    if not host:
        return False, "no_host"
    if host in AGGREGATOR_HOSTS:
        return False, "aggregator"
    if host in NOISE_HOSTS:
        return False, "noise"
    for ats in ATS_HOSTS:
        if host == ats and p.path.strip("/"):
            return True, "ats"
    if host == "boards.greenhouse.io" and p.path.strip("/"):
        return True, "greenhouse"
    if CAREERS_PATH_PATTERNS.search(u):
        return True, "careers_path"
    return False, "no_careers_pattern"


def validate_url(u: str) -> tuple[bool, str]:
    try:
        h = requests.head(
            u, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        if h.status_code >= 400:
            return False, f"head_{h.status_code}"
    except requests.RequestException as e:
        return False, f"head_error_{type(e).__name__}"

    try:
        g = requests.get(
            u, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return False, f"get_error_{type(e).__name__}"

    if g.status_code != 200:
        return False, f"get_{g.status_code}"
    ct = g.headers.get("content-type", "").lower()
    if "html" not in ct:
        return False, f"non_html"

    body_lower = g.text.lower()
    hits = sum(1 for kw in CAREERS_BODY_KEYWORDS if kw in body_lower)
    if hits < MIN_CAREERS_KEYWORDS_IN_BODY:
        return False, f"keywords_{hits}"
    return True, "ok"


def extract_company_name(url: str) -> str:
    p = urlparse(url)
    host = p.netloc.lower().replace("www.", "")
    if host in ATS_HOSTS or host == "boards.greenhouse.io":
        slug = p.path.strip("/").split("/")[0] if p.path.strip("/") else host
        return slug.replace("-", " ").title()
    return host.split(".")[0].replace("-", " ").title()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    log.info("=== Discovery A weekly sweep v2 ===")
    log.info(f"Backend: {'Brave (primary) + DDG (fallback)' if BRAVE_API_KEY else 'DDG only (no BRAVE_API_KEY set)'}")
    log.info(f"DRY_RUN: {DRY_RUN}")

    existing = load_discovered()
    builtin = load_builtin_pages()
    known = set(canonical_url(u) for u in existing) | set(canonical_url(u) for u in builtin)
    log.info(f"Known: {len(existing)} discovered + {len(builtin)} builtin = {len(known)} unique")

    all_candidates: list[str] = []
    for q in QUERIES:
        results = search(q)
        all_candidates.extend(results)

    log.info(f"=== TOTALS ===")
    log.info(f"Queries run: {len(QUERIES)}")
    log.info(f"Raw candidates: {len(all_candidates)}")

    if len(all_candidates) == 0:
        log.error("[FATAL] Zero raw results from ALL search backends. "
                  "Likely cause: IP block on BOTH Brave and DDG from GitHub runners. "
                  "Refusing to commit empty file. Set BRAVE_API_KEY repo secret.")
        print("SUMMARY: queries=17 raw=0 filtered=0 validated=0 new=0 total="
              + str(len(existing)) + " FATAL=search_blocked")
        return 2  # non-zero to flag workflow failure in GitHub UI

    filtered: list[str] = []
    seen_canonical = set()
    for u in all_candidates:
        cu = canonical_url(u)
        if cu in seen_canonical:
            reject_counts["dup_in_batch"] += 1
            continue
        if cu in known:
            reject_counts["already_known"] += 1
            continue
        ok, reason = looks_like_careers_url(u)
        if not ok:
            reject_counts[f"filter_{reason}"] += 1
            continue
        seen_canonical.add(cu)
        filtered.append(u)

    log.info(f"Post-filter unique candidates: {len(filtered)}")
    log.info(f"Reject reasons at filter stage:")
    for reason, n in reject_counts.most_common():
        log.info(f"  {n:4d}  {reason}")

    validated: dict[str, dict] = {}
    validate_reject_counts: Counter = Counter()
    for i, u in enumerate(filtered, 1):
        ok, reason = validate_url(u)
        if not ok:
            validate_reject_counts[reason] += 1
            log.info(f"  [{i}/{len(filtered)}] REJECT {u!r} -- {reason}")
            continue
        cu = canonical_url(u)
        validated[cu] = {
            "company": extract_company_name(u),
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source": "discovery-weekly-a",
            "original_url": u,
        }
        log.info(f"  [{i}/{len(filtered)}] ACCEPT {u!r}")

    log.info(f"Validated accepts: {len(validated)}")
    if validate_reject_counts:
        log.info(f"Reject reasons at validation stage:")
        for reason, n in validate_reject_counts.most_common():
            log.info(f"  {n:4d}  {reason}")

    merged = {**existing, **validated}
    added = len(merged) - len(existing)

    if DRY_RUN:
        log.info(f"[DRY_RUN] would write {len(merged)} entries ({added} new)")
    elif added == 0:
        log.info(f"No new entries -- not overwriting existing file")
    else:
        DISCOVERED.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
        log.info(f"Wrote {DISCOVERED} ({len(merged)} entries, {added} new)")

    print(f"SUMMARY: queries={len(QUERIES)} raw={len(all_candidates)} "
          f"filtered={len(filtered)} validated={len(validated)} new={added} "
          f"total={len(merged)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
