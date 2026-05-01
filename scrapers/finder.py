"""
Job Finder — discovers job URLs from every possible source:

JOB BOARDS (auto-search by role + location):
  - Indeed UK
  - Reed.co.uk
  - CV-Library
  - Totaljobs
  - Adzuna UK

CAREER PAGE CRAWLER (given homepage, finds every individual job posting):
  - Greenhouse  (boards.greenhouse.io/*)
  - Lever       (jobs.lever.co/*)
  - Workday     (*.myworkdayjobs.com/*)
  - Ashby       (jobs.ashbyhq.com/*)
  - SmartRecruiters
  - Teamtailor
  - Recruitee
  - Generic link-pattern heuristic (works on any custom careers page)

BUILT-IN TARGET COMPANIES:
  50+ robotics/AI/autonomy/CV/ML companies — no manual config needed.

All sources deduplicated. Keyword-filtered against target roles.
"""

import logging, re, time
from urllib.parse import urlparse
from pathlib import Path

from core.config import reload_config  # === PATCH 21
from scrapers.company_discovery import discover_companies, load_all_discovered_urls

log = logging.getLogger("finder")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sync_playwright = None
    log.warning("[FINDER] Playwright not installed — run: playwright install chromium")


# ── Built-in target company career pages ─────────────────────────────────────
BUILTIN_CAREER_PAGES = [
    # Autonomous Vehicles & Robotics
    "https://wayve.ai/careers",
    "https://oxa.tech/careers",
    "https://www.auar.com/careers",
    "https://jobs.lever.co/dyson",
    "https://www.anybotics.com/careers",
    "https://boards.greenhouse.io/agility-robotics",
    "https://jobs.lever.co/figure",
    "https://jobs.lever.co/1x",
    "https://jobs.ashbyhq.com/physical-intelligence",
    "https://jobs.ashbyhq.com/covariant",
    "https://jobs.ashbyhq.com/skydio",
    "https://jobs.lever.co/nuro",
    "https://jobs.lever.co/zoox",
    "https://boards.greenhouse.io/cruise",
    "https://boards.greenhouse.io/motional",
    "https://boards.greenhouse.io/applied-intuition",
    "https://jobs.lever.co/pony-ai",
    "https://jobs.lever.co/samsara",
    # AI Labs / Research
    "https://www.deepmind.com/careers",
    "https://jobs.lever.co/openai",
    "https://www.anthropic.com/careers",
    "https://boards.greenhouse.io/cohere",
    "https://jobs.ashbyhq.com/mistral",
    "https://jobs.ashbyhq.com/perplexityai",
    "https://jobs.ashbyhq.com/field-ai",
    # Computer Vision / ML Platforms
    "https://jobs.lever.co/scale",
    "https://careers.nvidia.com",
    "https://jobs.lever.co/arm",
    # UK Tech / Engineering / Defence
    "https://jobs.lever.co/CMRSurgical",
    "https://careers.rolls-royce.com",
    "https://boards.greenhouse.io/hadrian",
    # Energy / Sustainability
    "https://boards.greenhouse.io/origamienergy",
    # General
    "https://jobs.lever.co/palantir",
    # === PATCH 27: UK AI/ML/robotics refresh (Apr 24 2026) ============
    "https://www.graphcore.ai/careers",
    "https://www.darktrace.com/careers",
    "https://www.synthesia.io/careers",
    "https://elevenlabs.io/careers",
    "https://boards.greenhouse.io/improbable",
    "https://boards.greenhouse.io/tractable",
    "https://jobs.lever.co/polyai",
    "https://faculty.ai/careers",
    "https://www.speechmatics.com/company/careers",
    "https://www.quantexa.com/careers",
    "https://www.featurespace.com/careers",
    "https://humanloop.com/careers",
    "https://onfido.com/careers",
    "https://helsing.ai/jobs",
    "https://boards.greenhouse.io/isomorphiclabs",
    # === END PATCH 27 =================================================
]

# ── Keywords — job URL or title must match at least one to be kept ────────────
JOB_KEYWORDS = [
    "robot", "autonomo", "vision", "machine-learn", "machine_learn",
    "deep-learn", "computer-vision", "computer_vision", "cv-", "-cv-",
    "perception", "sensor", "lidar", "slam", "navigation", "ros",
    "embedded", "controls", "control-system", "firmware", "mechatron",
    "ai-", "-ai-", "engineer", "developer", "scientist", "software",
    "systems", "research", "ml-", "-ml", "nlp", "data-scientist",
]

# === PATCH 20: junk URL patterns =========================================
# URL fragments that indicate leadgen / trainee / apprenticeship / bootcamp
# listings that we never want to apply to. Filtering at URL level (before
# scraping) saves ~3-5s Playwright fetch per URL. Patterns are matched
# case-insensitive as substrings against the full URL.
JUNK_URL_PATTERNS = (
    "placement-programme",
    "placement-program",
    "expiring-soon",
    "-apprenticeship",
    "/apprenticeship-",
    "-bootcamp",
    "/bootcamp-",
    # "trainee-" matches hyphenated trainee roles (cv-library pattern).
    # Bare "trainee" would false-positive on legitimate companies with
    # "trainee" somewhere in a URL path. Using hyphen-bounded form only.
    "trainee-",
    "-trainee-",
)


def _is_junk_url(url: str) -> bool:
    """True if URL contains any junk pattern. Cheap substring match."""
    low = (url or "").lower()
    return any(p in low for p in JUNK_URL_PATTERNS)


# === END PATCH 20 ========================================================


def _keyword_match(url: str, title: str = "") -> bool:
    text = (url + " " + title).lower()
    return any(kw in text for kw in JOB_KEYWORDS)

def _detect_ats(url: str) -> str:
    u = url.lower()
    if "boards.greenhouse.io" in u: return "greenhouse"
    if "jobs.lever.co"        in u: return "lever"
    if "myworkdayjobs.com"    in u: return "workday"
    if "jobs.ashbyhq.com"     in u: return "ashby"
    if "smartrecruiters.com"  in u: return "smartrecruiters"
    if "teamtailor.com"       in u: return "teamtailor"
    if "recruitee.com"        in u: return "recruitee"
    return "generic"


# ── ATS extractors ────────────────────────────────────────────────────────────
def _extract_greenhouse(page, base_url):
    try:
        hrefs = page.eval_on_selector_all(
            "a.posting-title, div.posting a, .job-post a, a[href*='/jobs/']",
            "els => els.map(e => e.href)"
        )
        return [h for h in hrefs if "/jobs/" in h and h.startswith("http")]
    except Exception:
        return []

def _extract_lever(page, base_url):
    try:
        hrefs = page.eval_on_selector_all(
            "a.posting-title, .posting a[href], a[href*='jobs.lever.co']",
            "els => els.map(e => e.href)"
        )
        company = base_url.rstrip("/").split("/")[-1].lower()
        return [h for h in hrefs if "jobs.lever.co" in h and company in h.lower()]
    except Exception:
        return []

def _extract_ashby(page, base_url):
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='ashbyhq.com'], a[class*='job'], a[class*='posting'], a[href*='/jobs/']",
            "els => els.map(e => e.href)"
        )
        return [h for h in hrefs if h.startswith("http")]
    except Exception:
        return []

def _extract_workday(page, base_url):
    try:
        hrefs = page.eval_on_selector_all(
            "a[data-automation-id='jobTitle'], a[href*='/job/'], a[href*='workday.com']",
            "els => els.map(e => e.href)"
        )
        return [h for h in hrefs if h.startswith("http")]
    except Exception:
        return []

def _extract_smartrecruiters(page, base_url):
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='/jobs/'], li.job-item a, a.details-link",
            "els => els.map(e => e.href)"
        )
        return [h for h in hrefs if h.startswith("http")]
    except Exception:
        return []

def _extract_generic(page, base_url):
    try:
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    except Exception:
        return []
    job_pattern = re.compile(
        r"/(job|jobs|careers|opening|openings|position|positions|"
        r"vacancy|vacancies|posting|postings|apply|role|roles)/",
        re.IGNORECASE,
    )
    domain = urlparse(base_url).netloc
    ats_domains = [
        "greenhouse.io", "lever.co", "ashbyhq.com", "workday.com",
        "smartrecruiters.com", "teamtailor.com", "recruitee.com",
    ]
    results = []
    for h in hrefs:
        if not h or not h.startswith("http"):
            continue
        if job_pattern.search(h):
            link_domain = urlparse(h).netloc
            if link_domain == domain or any(a in link_domain for a in ats_domains):
                results.append(h)
    return list(dict.fromkeys(results))

EXTRACTORS = {
    "greenhouse"     : _extract_greenhouse,
    "lever"          : _extract_lever,
    "ashby"          : _extract_ashby,
    "workday"        : _extract_workday,
    "smartrecruiters": _extract_smartrecruiters,
    "generic"        : _extract_generic,
}


def _crawl_career_page(career_url: str) -> list[str]:
    """Visit a company careers homepage, return all individual job posting URLs."""
    if not sync_playwright:
        return []

    ats       = _detect_ats(career_url)
    extractor = EXTRACTORS.get(ats, _extract_generic)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        try:
            page.goto(career_url, timeout=25_000)
            page.wait_for_load_state("networkidle", timeout=15_000)
            raw = extractor(page, career_url)
            log.info(f"[CRAWL] {career_url} ({ats}) => {len(raw)} raw links")

            # Keyword filter
            filtered = [u for u in raw if _keyword_match(u)]

            # If nothing matched keyword filter, keep all (catches generic short URLs)
            result = filtered if filtered else raw[:20]
            return list(dict.fromkeys(result))[:25]

        except PWTimeout:
            log.warning(f"[CRAWL] Timeout: {career_url}")
            return []
        except Exception as e:
            log.warning(f"[CRAWL ERR] {career_url}: {e}")
            return []
        finally:
            browser.close()


# ── Main Finder ───────────────────────────────────────────────────────────────
class JobFinder:
    def __init__(self, cfg: dict):
        self.cfg       = cfg
        self.roles     = cfg.get("target_roles", [])
        self.locations = cfg.get("target_locations", ["Manchester", "London", "Remote"])
        self.blacklist = set(cfg.get("blacklist_companies", []))

        # Merge built-in + any extras from config (supports both key names)
        extra = cfg.get("career_pages", []) or cfg.get("direct_company_urls", [])
        self.career_pages = list(dict.fromkeys(BUILTIN_CAREER_PAGES + (extra or [])))

        self._seen = set()

    # ── Public entry point ────────────────────────────────────────────────────
    def find_jobs(self) -> list[str]:
        # === PATCH 21: hot-reload config at start of every sweep =========
        # Picks up live edits to config.yaml (e.g. max_urls_per_board,
        # junk_url_patterns, target_roles) without requiring container restart.
        # cfg from reload_config() is the same dict used by agent.py -- they
        # share state via core.config._CFG_STATE.
        self.cfg = reload_config()
        # === END PATCH 21 ================================================
        # === PATCH 22: refresh derived state from latest cfg =============
        # Patch 21 refreshed self.cfg but self.roles / self.locations /
        # self.blacklist / self.career_pages were still computed only in
        # __init__. This re-derives them every sweep so edits to
        # target_roles / blacklist_companies / career_pages take effect
        # without container restart.
        self.roles     = self.cfg.get("target_roles", [])
        self.locations = self.cfg.get("target_locations", ["Manchester", "London", "Remote"])
        self.blacklist = set(self.cfg.get("blacklist_companies", []))
        extra = self.cfg.get("career_pages", []) or self.cfg.get("direct_company_urls", [])
        self.career_pages = list(dict.fromkeys(BUILTIN_CAREER_PAGES + (extra or [])))
        # === END PATCH 22 ================================================
        log.info("[FINDER] === Starting discovery sweep ===")
        urls = []

        # 1. Discover new companies from internet search (runs every sweep)
        if self.cfg.get("enable_discovery", True):
            try:
                log.info("[FINDER] Running company discovery scan...")
                discover_companies(self.roles, max_new=20)
            except Exception as e:
                log.warning(f"[FINDER] Discovery scan error: {e}")
        else:
            log.info("[FINDER] Discovery disabled (enable_discovery: false)")

        # 2. Job boards
        if self.cfg.get("enable_indeed", True):
            urls += self._search_indeed()
        else:
            log.info("[FINDER] Indeed disabled (enable_indeed: false)")
        urls += self._search_reed()
        urls += self._search_cvlibrary()
        urls += self._search_totaljobs()
        urls += self._search_adzuna()

        # 3. Career page crawler — built-in + config extras + all discovered
        discovered_pages = load_all_discovered_urls()
        all_pages = list(dict.fromkeys(self.career_pages + discovered_pages))
        log.info(f"[FINDER] Crawling {len(all_pages)} career pages "
                 f"({len(self.career_pages)} built-in + {len(discovered_pages)} discovered)")
        urls += self._crawl_all_career_pages_from(all_pages)

        fresh = [u for u in dict.fromkeys(urls) if u not in self._seen]
        self._seen.update(fresh)
        log.info(f"[FINDER] === {len(fresh)} fresh URLs this sweep ===")
        return fresh

    # ── Job boards ────────────────────────────────────────────────────────────
    def _board_search(self, name, fn, roles_limit=None, locs_limit=None):
        results = []
        roles = self.roles[:roles_limit] if roles_limit else self.roles
        locs  = self.locations[:locs_limit] if locs_limit else self.locations
        for role in roles:
            for loc in locs:
                try:
                    found = fn(role, loc)
                    results += found
                    log.info(f"[{name}] {role} / {loc} => {len(found)}")
                    time.sleep(2)
                except Exception as e:
                    log.warning(f"[{name} ERR] {role}/{loc}: {e}")
        # === PATCH 20: filter and truncate ===================================
        # Dedup within-board first so we don't count duplicates against the
        # per-board limit. Then drop junk URLs. Then truncate.
        before_count = len(results)
        deduped = list(dict.fromkeys(results))
        if len(deduped) < before_count:
            log.info(f"[{name}] dedup: {before_count} -> {len(deduped)}")

        # Junk URL filter (patch 20B)
        non_junk = [u for u in deduped if not _is_junk_url(u)]
        junk_dropped = len(deduped) - len(non_junk)
        if junk_dropped:
            log.info(f"[{name}] junk filter: dropped {junk_dropped} (placement/trainee/bootcamp URLs)")

        # Per-board truncation (patch 20A)
        max_per_board = int(self.cfg.get("max_urls_per_board", 5))
        if max_per_board > 0 and len(non_junk) > max_per_board:
            truncated = non_junk[:max_per_board]
            log.info(f"[{name}] truncate: {len(non_junk)} -> {len(truncated)} (max_urls_per_board)")
            return truncated
        return non_junk
        # === END PATCH 20 ====================================================

    def _search_indeed(self):
        def fn(role, loc):
            q, l = role.replace(" ", "+"), loc.replace(" ", "+")
            return self._pw_links(
                f"https://uk.indeed.com/jobs?q={q}&l={l}&sort=date",
                "a[data-jk], a[href*='viewjob']",
                filter_fn=lambda h: "viewjob" in h or "jk=" in h,
                clean_fn=lambda h: h.split("&")[0],
                limit=15,
            )
        return self._board_search("INDEED", fn)

    def _search_reed(self):
        def fn(role, loc):
            q = role.replace(" ", "-").lower()
            l = loc.replace(" ", "-").lower()
            return self._pw_links(
                f"https://www.reed.co.uk/jobs/{q}-jobs-in-{l}",
                "a[data-qa='job-card-title'], h3.title a",
                limit=12,
            )
        return self._board_search("REED", fn, roles_limit=4, locs_limit=3)

    def _search_cvlibrary(self):
        def fn(role, loc):
            q, l = role.replace(" ", "+"), loc.replace(" ", "+")
            return self._pw_links(
                f"https://www.cv-library.co.uk/search-jobs?q={q}&geo={l}&us=1&submitted=1",
                "a.job-title, h3 a[href*='/job/']",
                filter_fn=lambda h: "/job/" in h,
                limit=10,
            )
        return self._board_search("CVLIB", fn, roles_limit=3, locs_limit=2)

    def _search_totaljobs(self):
        def fn(role, loc):
            q = role.replace(" ", "-").lower()
            l = loc.lower()
            return self._pw_links(
                f"https://www.totaljobs.com/jobs/{q}/in-{l}",
                "a[data-at='job-item-title'], .job-title a",
                filter_fn=lambda h: "/job/" in h or "/jobs/" in h,
                limit=10,
            )
        return self._board_search("TOTALJOBS", fn, roles_limit=3, locs_limit=2)

    def _search_adzuna(self):
        def fn(role, loc):
            q = role.replace(" ", "+")
            l = loc.lower().replace(" ", "-")
            return self._pw_links(
                f"https://www.adzuna.co.uk/search?q={q}&w={l}&sort=date",
                "a[data-aid='job-item-title'], h2 a[href*='/details/']",
                filter_fn=lambda h: "/details/" in h or "/ad/" in h,
                limit=10,
            )
        return self._board_search("ADZUNA", fn, roles_limit=3, locs_limit=2)

    # ── Career page crawler ───────────────────────────────────────────────────
    def _crawl_all_career_pages_from(self, pages: list[str]) -> list[str]:
        all_links = []
        total = len(pages)
        for i, url in enumerate(pages, 1):
            log.info(f"[CRAWL] {i}/{total} — {url}")
            try:
                links = _crawl_career_page(url)
                all_links += links
                log.info(f"[CRAWL] {len(links)} relevant links from {url}")
            except Exception as e:
                log.warning(f"[CRAWL ERR] {url}: {e}")
            time.sleep(3)
        return all_links

    # ── Playwright helper ────────────────────────────────────────────────────
    def _pw_links(self, url, selector, filter_fn=None, clean_fn=None, limit=15):
        if not sync_playwright:
            return []
        links = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ))
            try:
                page.goto(url, timeout=20_000)
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
                hrefs = page.eval_on_selector_all(
                    selector, "els => els.map(e => e.href || '')"
                )
                for h in hrefs:
                    if not h:
                        continue
                    if filter_fn and not filter_fn(h):
                        continue
                    h = clean_fn(h) if clean_fn else h
                    links.append(h)
            except PWTimeout:
                log.warning(f"[BOARD] Timeout: {url}")
            except Exception as e:
                log.warning(f"[BOARD ERR] {url}: {e}")
            finally:
                browser.close()
        return links[:limit]
