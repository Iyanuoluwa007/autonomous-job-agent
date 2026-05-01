"""
Company Discovery — scans the internet each sweep for new companies
related to target roles, extracts their career page URLs, and saves
them to discovered_companies.json for the career page crawler.

Search sources:
  - Google (via DuckDuckGo HTML — no API key needed)
  - LinkedIn company search
  - Crunchbase funding pages (UK AI/robotics startups)
  - AngelList / Wellfound
  - F6S (UK startup directory)

Persists to: discovered_companies.json
Feeds into: scrapers/finder.py BUILTIN_CAREER_PAGES
"""

import json, logging, re, time
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote_plus

log = logging.getLogger("discovery")

DISCOVERED_FILE = Path("discovered_companies.json")

# Search queries — designed to surface startup/scaleup career pages
SEARCH_QUERIES = [
    "robotics AI startup UK careers jobs site",
    "computer vision startup London Manchester hiring",
    "autonomous systems company UK engineering jobs",
    "machine learning startup UK careers page",
    "ROS2 robotics company UK jobs",
    "deep learning startup UK hiring engineers",
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

# Patterns that indicate a career page URL
CAREER_URL_PATTERNS = re.compile(
    r"/(careers?|jobs?|work-with-us|join-us|join-our-team|hiring|vacancies|openings?|opportunities)",
    re.IGNORECASE,
)

# Known ATS subdomain patterns — these are almost always career pages
ATS_PATTERNS = re.compile(
    r"(jobs\.lever\.co|boards\.greenhouse\.io|jobs\.ashbyhq\.com|"
    r"careers\.smartrecruiters\.com|apply\.workable\.com|"
    r"jobs\.workable\.com|recruitee\.com|teamtailor\.com|"
    r"bamboohr\.com/careers|hire\.withgoogle\.com)",
    re.IGNORECASE,
)

# Domains to ignore (job boards, not company career pages)
IGNORE_DOMAINS = {
    "indeed.com", "reed.co.uk", "linkedin.com", "glassdoor.com",
    "totaljobs.com", "cv-library.co.uk", "monster.co.uk", "adzuna.co.uk",
    "cwjobs.co.uk", "jobsite.co.uk", "simplyhired.co.uk", "ziprecruiter.com",
    "google.com", "bing.com", "yahoo.com", "facebook.com", "twitter.com",
    "youtube.com", "wikipedia.org", "reddit.com", "quora.com",
    "techcrunch.com", "wired.com", "bbc.co.uk", "theguardian.com",
}

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sync_playwright = None


def _load_discovered() -> dict:
    """Load previously discovered companies from JSON."""
    if DISCOVERED_FILE.exists():
        try:
            return json.loads(DISCOVERED_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}   # {career_url: {"company": name, "source": query, "added": timestamp}}


def _save_discovered(data: dict):
    DISCOVERED_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _is_career_url(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    domain = urlparse(url).netloc.lower()
    if any(d in domain for d in IGNORE_DOMAINS):
        return False
    # Direct ATS URL = definitely a career page
    if ATS_PATTERNS.search(url):
        return True
    # Path contains career-like segment
    if CAREER_URL_PATTERNS.search(urlparse(url).path):
        return True
    return False


def _extract_company_name(url: str, page_title: str = "") -> str:
    """Best-effort company name from URL or page title."""
    # ATS URLs encode company name
    ats_match = re.search(
        r"(?:jobs\.lever\.co|boards\.greenhouse\.io|jobs\.ashbyhq\.com|"
        r"careers\.smartrecruiters\.com)/([^/?#]+)",
        url, re.IGNORECASE
    )
    if ats_match:
        return ats_match.group(1).replace("-", " ").title()

    # Use page title if available
    if page_title:
        # Strip common suffixes
        name = re.sub(
            r"\s*[-|–]\s*(careers?|jobs?|hiring|work with us).*$",
            "", page_title, flags=re.IGNORECASE
        ).strip()
        if name and len(name) < 60:
            return name

    # Fall back to domain
    domain = urlparse(url).netloc.lower()
    domain = re.sub(r"^(www\.|careers?\.|jobs?\.)", "", domain)
    return domain.split(".")[0].title()


def _search_duckduckgo(query: str, max_results: int = 15) -> list[str]:
    """Search DuckDuckGo HTML (no API key) and return result URLs."""
    if not sync_playwright:
        return []

    urls = []
    search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        try:
            page.goto(search_url, timeout=20_000)
            page.wait_for_load_state("domcontentloaded", timeout=10_000)

            # DuckDuckGo HTML result links
            hrefs = page.eval_on_selector_all(
                "a.result__url, a[href*='uddg='], .result__a",
                "els => els.map(e => e.href)"
            )
            for h in hrefs:
                # DuckDuckGo wraps URLs — extract the real URL
                if "uddg=" in h:
                    match = re.search(r"uddg=([^&]+)", h)
                    if match:
                        from urllib.parse import unquote
                        h = unquote(match.group(1))
                if h.startswith("http"):
                    urls.append(h)
                if len(urls) >= max_results:
                    break
        except PWTimeout:
            log.warning(f"[DISCOVERY] DuckDuckGo timeout for: {query}")
        except Exception as e:
            log.warning(f"[DISCOVERY] Search error: {e}")
        finally:
            browser.close()

    return urls


def _find_career_page(company_url: str) -> str | None:
    """
    Given a company homepage, find their /careers page.
    Returns the career URL or None.
    """
    if not sync_playwright:
        return None

    # If URL already looks like a career page, return it directly
    if _is_career_url(company_url):
        return company_url

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        try:
            page.goto(company_url, timeout=20_000)
            page.wait_for_load_state("domcontentloaded", timeout=10_000)

            # Look for career links on the page
            hrefs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            )

            base = f"{urlparse(company_url).scheme}://{urlparse(company_url).netloc}"

            for h in hrefs:
                if not h:
                    continue
                # Make absolute
                if h.startswith("/"):
                    h = base + h
                if _is_career_url(h):
                    return h

        except Exception:
            pass
        finally:
            browser.close()

    return None


def discover_companies(target_roles: list[str], max_new: int = 30) -> list[str]:
    """
    Main entry point — searches the internet, finds new company career pages,
    saves to discovered_companies.json, returns list of new career URLs.
    """
    discovered = _load_discovered()
    existing_urls = set(discovered.keys())
    new_urls = {}
    found_count = 0

    log.info(f"[DISCOVERY] Starting scan — {len(existing_urls)} already known")

    # Build role-specific queries
    role_queries = [
        f"{role} company UK careers hiring" for role in target_roles[:4]
    ] + SEARCH_QUERIES

    for query in role_queries:
        if found_count >= max_new:
            break

        log.info(f"[DISCOVERY] Searching: {query}")
        try:
            results = _search_duckduckgo(query, max_results=20)
        except Exception as e:
            log.warning(f"[DISCOVERY] Query failed: {e}")
            continue

        for url in results:
            if found_count >= max_new:
                break

            # Already known
            if url in existing_urls or url in new_urls:
                continue

            # Direct career/ATS URL
            if _is_career_url(url):
                company = _extract_company_name(url)
                new_urls[url] = {
                    "company": company,
                    "source" : query,
                    "added"  : time.strftime("%Y-%m-%d"),
                }
                log.info(f"[DISCOVERY] [NEW] {company} => {url}")
                found_count += 1
                continue

            # Company homepage — try to find their careers page
            domain = urlparse(url).netloc.lower()
            if any(d in domain for d in IGNORE_DOMAINS):
                continue

            # Only attempt homepage resolution for clean company URLs
            if re.match(r"https?://[^/]+/?$", url):
                try:
                    career_url = _find_career_page(url)
                    if career_url and career_url not in existing_urls and career_url not in new_urls:
                        company = _extract_company_name(career_url)
                        new_urls[career_url] = {
                            "company": company,
                            "source" : query,
                            "added"  : time.strftime("%Y-%m-%d"),
                        }
                        log.info(f"[DISCOVERY] [NEW via homepage] {company} => {career_url}")
                        found_count += 1
                except Exception as e:
                    log.debug(f"[DISCOVERY] Homepage probe failed {url}: {e}")

        time.sleep(3)   # polite delay between searches

    # Save all (existing + new)
    discovered.update(new_urls)
    _save_discovered(discovered)

    log.info(
        f"[DISCOVERY] Sweep complete — {found_count} new companies found, "
        f"{len(discovered)} total in database"
    )
    return list(new_urls.keys())


def load_all_discovered_urls() -> list[str]:
    """Return all saved career URLs for use by the crawler."""
    data = _load_discovered()
    return list(data.keys())
