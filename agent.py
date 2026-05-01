"""
Autonomous Job Application Agent
Free backend: NVIDIA NIM (40 req/min) - kimi-k2-thinking + step-3.5-flash
No human review loop. Hourly email status digest.
"""

import logging, sys, time, sqlite3, json
from pathlib import Path
from datetime import datetime

import yaml
from apscheduler.schedulers.background import BackgroundScheduler

from core.llm         import NIMClient
from core.fit_scorer  import FitScorer
from core.role_filter import is_relevant_role
from core.cover_letter import CoverLetterGenerator
from core.form_filler import FormFiller
from core.notifier    import EmailNotifier

# --- Digest startup floor -----------------------------------------
# Digests should never include activity that predates this run of the
# agent. Set once at import; used as a floor in db_hourly_stats().
from datetime import datetime as _dt_startup
_AGENT_STARTUP_UTC = _dt_startup.utcnow().strftime("%Y-%m-%d %H:%M:%S")
from scrapers.scraper import JobScraper
from scrapers.finder  import JobFinder

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("agent")

# ── Bootstrap ─────────────────────────────────────────────────────────────────
# === PATCH 21: config reload moved to core.config =================
# Was: an inline EMERGENCY block defining _CFG_PATH/_CFG_STATE/
# reload_config(). Extracted to core/config.py so finder.py and any
# other module can import it without creating a circular dependency
# back to agent.py.
from core.config import reload_config, cfg  # === PATCH 21
# === END PATCH 21 =================================================

resume_text = Path(cfg["resume_path"]).read_text(encoding="utf-8")

# ── SQLite ────────────────────────────────────────────────────────────────────
conn = sqlite3.connect("applications.db", check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT UNIQUE,
    company      TEXT,
    title        TEXT,
    fit_score    REAL,
    status       TEXT,
    cover_letter TEXT,
    model_used   TEXT,
    error        TEXT,
    applied_at   TEXT DEFAULT (datetime('now'))
)""")
conn.execute("""
CREATE TABLE IF NOT EXISTS skipped (
    url TEXT UNIQUE, reason TEXT,
    ts  TEXT DEFAULT (datetime('now'))
)""")
conn.commit()

# === URL HELPERS (patches 9+10) ===========================================
import re as _re_url
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# === PATCH 25 (A): expanded tracking params ==============================
# Aggregator sites (CV-Library especially) rotate session/search-context
# params like sid=<UUID> on every URL. Without these in the strip list, the
# same job is canonicalised as 17 distinct URLs, bypassing UNIQUE(url) and
# causing duplicate APPLIED inserts. Conservative: add the common rotators.
_TRACKING_PARAMS = {
    # Standard marketing trackers
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "source", "filter", "ref", "gclid", "fbclid", "mc_cid", "mc_eid",
    "_ga", "mkt_tok",
    # Aggregator-specific session/search-context rotators
    "sid", "hlkw", "featured", "sourceofsearch", "externalorigin",
    "hidesmi", "trackid", "refsrc", "origin", "s_keyword",
    # Reed/Indeed/LinkedIn additions
    "atsreferer", "cmpid", "jk", "from", "savedresume",
}
# === END PATCH 25 (A) =====================================================


# === PATCH 25 (B): aggregator-aware canonicalization =====================
# Some aggregators put the job ID in the URL path (CV-Library, Reed,
# LinkedIn, Monster, etc.), others in a query param (Indeed uses ?jk=NNN).
# Per-aggregator mapping: value is the single job-ID param name to KEEP
# (everything else stripped), or None if job ID is in the path (strip all).
_AGGREGATOR_JOB_ID_PARAM = {
    "cv-library.co.uk":  None,    "cv-library.com":   None,
    "reed.co.uk":        None,    "reed.com":         None,
    "indeed.com":        "jk",    "indeed.co.uk":     "jk",
    "linkedin.com":      None,
    "totaljobs.com":     None,
    "jobserve.com":      None,
    "jobsite.co.uk":     None,
    "monster.co.uk":     None,    "monster.com":      None,
    "glassdoor.com":     None,    "glassdoor.co.uk":  None,
    "cwjobs.co.uk":      None,
    "adzuna.co.uk":      None,    "adzuna.com":       None,
}


def _aggregator_id_param(netloc: str):
    """Return (matched_suffix, id_param) if host is an aggregator,
    else (None, None). id_param is None if job ID is in path."""
    n = netloc.lower().removeprefix("www.")
    for host, id_param in _AGGREGATOR_JOB_ID_PARAM.items():
        if n == host or n.endswith("." + host):
            return host, id_param
    return None, None
# === END PATCH 25 (B, helpers) ===========================================


def _canonical_url(url: str) -> str:
    """Normalise URL for dedup: lowercase host, strip fragment, drop tracking
    params, remove trailing slash. Preserves meaningful path + query.

    PATCH 25: for aggregator hosts, keep ONLY the per-aggregator job-ID
    query param (if any), strip everything else. Covers CV-Library\'s
    rotating sid-based duplicate storm and Indeed\'s jk=<id> pattern."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        scheme = (parts.scheme or "https").lower()
        netloc = parts.netloc.lower()
        path   = parts.path.rstrip("/") or "/"
        # === PATCH 25 (B): aggregator-aware canonicalization =============
        agg_host, id_param = _aggregator_id_param(netloc)
        if agg_host is not None:
            if id_param:
                # Keep only the job-ID param (Indeed: ?jk=NNN)
                kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                        if k.lower() == id_param.lower()]
                query = urlencode(kept, doseq=True)
            else:
                # Job ID is in path; strip all query params
                query = ""
            return urlunsplit((scheme, netloc, path, query, ""))
        # === END PATCH 25 (B) =============================================
        qs     = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                  if k.lower() not in _TRACKING_PARAMS]
        query  = urlencode(qs, doseq=True)
        return urlunsplit((scheme, netloc, path, query, ""))  # fragment dropped
    except Exception:
        return url.strip()


# Careers-index URL patterns (exact path endings that are almost always landing pages)
_CAREERS_INDEX_SUFFIXES = (
    "/careers", "/careers/",
    "/jobs", "/jobs/",
    "/openings", "/openings/",
    "/vacancies", "/vacancies/",
    "/about-us/careers", "/about-us/careers/",
    "/work-with-us", "/work-with-us/",
    "/join-us", "/join-us/",
)

# Marketing-copy markers in titles
_MARKETING_TITLE_SNIPPETS = (
    "impact the future",
    "build the future",
    "shape the future",
    "join us",
    "join our team",
    "work with us",
    "careers at",
    "open positions",
    "current openings",
    "we're hiring",
    "we are hiring",
)

# Engineering-role nouns — a real job title almost always has one of these
_ROLE_NOUNS = (
    "engineer", "developer", "scientist", "researcher", "architect",
    "specialist", "analyst", "programmer", "designer", "lead",
    "manager", "director", "head of", "intern", "consultant", "trader",
    "quant", "operator",
)


def _looks_like_index_page(url: str, title: str) -> tuple[bool, str]:
    """Detect careers-index pages scraped as if they were specific jobs.
    Returns (is_index, reason)."""
    u = (url or "").lower()
    t = (title or "").lower().strip()

    # 1. URL path looks like a careers landing page.
    try:
        path = urlsplit(u).path.rstrip("/")
        path_norm = path + "/"  # normalise to trailing slash for suffix check
        for suf in _CAREERS_INDEX_SUFFIXES:
            if path_norm.endswith(suf):
                return True, f"index_url:{suf.strip('/')}"
    except Exception:
        pass

    # 2. Title contains marketing tagline pattern.
    for snip in _MARKETING_TITLE_SNIPPETS:
        if snip in t:
            return True, f"marketing_title:{snip}"

    # 3. Title is long (>55 chars) AND contains no engineering-role noun.
    #    Short vague titles pass; long taglines without job nouns fail.
    if len(t) > 55 and not any(n in t for n in _ROLE_NOUNS):
        return True, "long_title_no_role_noun"

    return False, ""


# === PATCH 13: sample_reviewed_at migration ================================
# Idempotent ALTER TABLE -- runs at import. Column marks which applications
# have been shown in a preview email so they don't repeat.
try:
    conn.execute("ALTER TABLE applications ADD COLUMN sample_reviewed_at TEXT DEFAULT NULL")
    conn.commit()
except Exception:
    # Column already exists -- sqlite3 raises OperationalError. Ignore.
    pass


# === PATCH 14: recruiter_email migration ===================================
try:
    conn.execute("ALTER TABLE applications ADD COLUMN recruiter_email TEXT DEFAULT NULL")
    conn.commit()
except Exception:
    pass


def already_processed(url):
    # === PATCH 31B: exclude APPLIED_UNVERIFIED from dedup ==============
    # Pre-P31a applications were phantom (_try_fill click_submit bug).
    # Migration relabelled 352 rows from APPLIED to APPLIED_UNVERIFIED.
    # Excluding that status from dedup lets the agent retry them with
    # the fixed submit code. Other statuses (APPLIED, FORM_FAILED,
    # ERROR:cover_letter, etc.) still block re-apply as before.
    # ===================================================================
    canon = _canonical_url(url)
    r = conn.execute(
        "SELECT 1 FROM applications "
        "WHERE url IN (?,?) AND status != 'APPLIED_UNVERIFIED' "
        "UNION SELECT 1 FROM skipped WHERE url IN (?,?)",
        (url, canon, url, canon)
    )
    return r.fetchone() is not None
    # === END PATCH 31B =================================================

def db_log_app(url, company, title, fit_score, status, cover, model, error=None, recruiter_email=None):
    conn.execute(
        "INSERT OR IGNORE INTO applications (url,company,title,fit_score,status,cover_letter,model_used,error,recruiter_email)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (_canonical_url(url), company, title, fit_score, status, cover, model, error, recruiter_email)
    )
    conn.commit()

def db_log_skip(url, reason):
    conn.execute("INSERT OR IGNORE INTO skipped (url,reason) VALUES (?,?)", (_canonical_url(url), reason))
    conn.commit()

def db_hourly_stats():
    """Pull stats for the hourly email digest."""
    row = conn.execute("""
        SELECT
            COUNT(*)                                          AS total,
            SUM(CASE WHEN status='APPLIED' THEN 1 ELSE 0 END) AS applied,
            SUM(CASE WHEN status LIKE 'ERROR%' THEN 1 ELSE 0 END) AS errors,
            SUM(CASE WHEN status='SKIPPED_FIT' THEN 1 ELSE 0 END) AS low_fit,
            ROUND(AVG(fit_score),1)                          AS avg_fit
        FROM applications
        WHERE applied_at >= datetime('now','-1 hour')
          AND applied_at >= ?
    """, (_AGENT_STARTUP_UTC,)).fetchone()
    skip_row = conn.execute(
        "SELECT COUNT(*) FROM skipped WHERE ts >= datetime('now','-1 hour') AND ts >= ?",
        (_AGENT_STARTUP_UTC,)
    ).fetchone()
    recent = conn.execute("""
        SELECT company, title, fit_score, status
        FROM applications
        WHERE applied_at >= datetime('now','-1 hour')
          AND applied_at >= ?
        ORDER BY applied_at DESC LIMIT 10
    """, (_AGENT_STARTUP_UTC,)).fetchall()
    return {
        "total": row[0], "applied": row[1], "errors": row[2],
        "low_fit": row[3], "avg_fit": row[4], "scraped_skip": skip_row[0],
        "recent": recent,
    }

# ── Components ─────────────────────────────────────────────────────────────────
nim     = NIMClient(cfg["nvidia_nim_api_key"], cfg["models"])
scorer  = FitScorer(nim, resume_text)
cl_gen  = CoverLetterGenerator(nim, resume_text, cfg["profile"])
filler  = FormFiller(cfg["profile"])
notifier= EmailNotifier(cfg["email"], models=cfg.get("models"))
scraper = JobScraper()
finder  = JobFinder(cfg)

# ── Hourly Email Digest ───────────────────────────────────────────────────────
def send_hourly_digest():
    # === C1: reload in hourly_digest ==================================
    global cfg
    try:
        cfg = reload_config()
    except Exception:
        pass
    try:
        stats = db_hourly_stats()
        notifier.send_digest(stats)
        log.info("[DIGEST] Hourly email sent")
    except Exception as e:
        log.error(f"[DIGEST ERR] {e}")

def send_sample_form_email():
    """HTML sample review: 3 random recent applications, every form field shown
    so you can audit what actually got submitted on your behalf."""
    # === C1: reload in sample_form_email ==================================
    global cfg
    try:
        cfg = reload_config()
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT id, company, title, fit_score, status, cover_letter, applied_at, url "
            "FROM applications "
            "WHERE applied_at >= datetime('now','-3 hours') "
            "AND applied_at >= ? "
            "AND status IN ('APPLIED','FORM_FAILED','FORM_UNCONFIRMED') "
            "AND sample_reviewed_at IS NULL "
            "ORDER BY RANDOM() LIMIT 3",
            (_AGENT_STARTUP_UTC,),
        ).fetchall()
        if not rows:
            return

        p = cfg.get("profile", {})
        resume_name = Path(cfg.get("resume_pdf_path", "")).name or "(none)"

        # model footer uses the same pretty names as the hourly digest
        from core.notifier import _pretty_model
        primary_short = _pretty_model((cfg.get("models") or {}).get("primary"))
        fast_short    = _pretty_model((cfg.get("models") or {}).get("fast"))

        def _fit_colour(fit):
            try:    f = float(fit or 0)
            except: f = 0
            if f >= 70: return "#22c55e"
            if f >= 50: return "#f59e0b"
            return "#ef4444"

        def _status_colour(status):
            s = str(status or "").upper()
            if s == "APPLIED":     return "#22c55e"
            if s.startswith("ERROR"): return "#ef4444"
            return "#f59e0b"

        def _esc(x):
            """Minimal HTML escaping for user-facing text."""
            return (str(x) if x is not None else "") \
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def _check_cover(cover):
            """Return list of (severity, label) warnings for this cover letter."""
            warnings = []
            c = cover or ""
            cl = c.lower().strip()
            if cl.startswith("<think>") or "<think>" in cl[:50]:
                warnings.append(("red", "REASONING LEAK (&lt;think&gt; prefix)"))
            if len(c.split()) < 100:
                warnings.append(("red", f"TOO SHORT ({len(c.split())} words)"))
            if not cl.startswith("dear"):
                warnings.append(("amber", "MISSING GREETING"))
            for bad in ("leverage", "synergy", "passionate", "stakeholder"):
                if bad in cl:
                    warnings.append(("amber", f"CLICHE WORD: {bad}"))
                    break
            return warnings

        cards_html = []
        cards_text = []
        for i, (_id, company, title, fit, status, cover, ts, url) in enumerate(rows, 1):
            fit_col    = _fit_colour(fit)
            status_col = _status_colour(status)
            word_count = len((cover or "").split())
            warnings   = _check_cover(cover)
            url_display = (url[:60] + "...") if url and len(url) > 60 else (url or "")

            warn_html = ""
            if warnings:
                for sev, label in warnings:
                    bg = "#fef2f2" if sev == "red" else "#fffbeb"
                    fg = "#b91c1c" if sev == "red" else "#b45309"
                    warn_html += (
                        f'<div style="background:{bg};color:{fg};padding:6px 10px;'
                        f'border-radius:4px;margin-bottom:6px;font-size:12px;font-weight:600">'
                        f'[!] {label}</div>'
                    )

            cover_html = _esc(cover or "(empty)").replace("\n", "<br/>")

            cards_html.append(f"""
  <div style="border:1px solid #e2e8f0;border-radius:6px;background:white;padding:16px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,0.04)">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #e2e8f0;padding-bottom:10px;margin-bottom:12px;flex-wrap:wrap">
      <div style="flex:1;min-width:250px">
        <div style="font-size:15px;font-weight:700;color:#0f172a">{_esc(company)} &mdash; {_esc(title)}</div>
        <div style="color:#64748b;font-size:11px;margin-top:4px">Applied {_esc(ts)} &nbsp;|&nbsp; <a href="{_esc(url)}" style="color:#3b82f6;text-decoration:none">{_esc(url_display)}</a></div>
      </div>
      <div style="text-align:right;white-space:nowrap">
        <span style="background:{fit_col};color:white;padding:3px 9px;border-radius:3px;font-size:11px;font-weight:700">FIT {fit}</span>
        <span style="background:{status_col};color:white;padding:3px 9px;border-radius:3px;font-size:11px;font-weight:700;margin-left:4px">{_esc(status)}</span>
      </div>
    </div>

    {warn_html}

    <div style="font-size:11px;color:#64748b;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px">Form fields submitted</div>
    <table style="width:100%;font-size:12px;border-collapse:collapse;margin-bottom:14px">
      <tr><td style="padding:3px 8px;color:#64748b;width:100px">Name</td><td style="padding:3px 8px">{_esc(p.get("full_name",""))}</td></tr>
      <tr><td style="padding:3px 8px;color:#64748b">Email</td><td style="padding:3px 8px">{_esc(p.get("email",""))}</td></tr>
      <tr><td style="padding:3px 8px;color:#64748b">Phone</td><td style="padding:3px 8px">{_esc(p.get("phone",""))}</td></tr>
      <tr><td style="padding:3px 8px;color:#64748b">LinkedIn</td><td style="padding:3px 8px">{_esc(p.get("linkedin",""))}</td></tr>
      <tr><td style="padding:3px 8px;color:#64748b">Website</td><td style="padding:3px 8px">{_esc(p.get("website",""))}</td></tr>
      <tr><td style="padding:3px 8px;color:#64748b">Location</td><td style="padding:3px 8px">{_esc(p.get("location",""))}</td></tr>
      <tr><td style="padding:3px 8px;color:#64748b">Resume file</td><td style="padding:3px 8px">{_esc(resume_name)}</td></tr>
    </table>

    <div style="font-size:11px;color:#64748b;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px">Cover letter ({word_count} words)</div>
    <div style="background:#f8fafc;border-left:3px solid #3b82f6;padding:12px 14px;font-size:12px;line-height:1.55;color:#1e293b;white-space:pre-wrap">{cover_html}</div>
  </div>
""")

            # plain-text fallback
            warn_text = "\n".join(f"  [!] {lbl}" for _, lbl in warnings)
            cards_text.append(
                f"\n{'='*60}\nApplication {i}/{len(rows)}\n{'='*60}\n"
                f"Company:    {company}\n"
                f"Role:       {title}\n"
                f"Fit:        {fit}/100\n"
                f"Status:     {status}\n"
                f"Applied:    {ts}\n"
                f"URL:        {url}\n"
                + (f"\nWARNINGS:\n{warn_text}\n" if warnings else "")
                + f"\nForm fields submitted:\n"
                f"  Name:     {p.get('full_name','')}\n"
                f"  Email:    {p.get('email','')}\n"
                f"  Phone:    {p.get('phone','')}\n"
                f"  LinkedIn: {p.get('linkedin','')}\n"
                f"  Website:  {p.get('website','')}\n"
                f"  Location: {p.get('location','')}\n"
                f"  Resume:   {resume_name}\n"
                f"\nCover Letter ({word_count} words):\n{cover or '(empty)'}\n"
            )

        html = f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#1e293b">
<div style="max-width:720px;margin:0 auto">
  <div style="background:#0f172a;color:white;padding:16px 20px;border-radius:6px 6px 0 0">
    <div style="font-size:11px;color:#94a3b8;letter-spacing:2px;text-transform:uppercase">FORM SAMPLE REVIEW</div>
    <div style="font-size:16px;font-weight:700;margin-top:4px">Last 2 hours &mdash; {len(rows)} random {('application' if len(rows)==1 else 'applications')}</div>
  </div>
  <div style="background:white;padding:16px;border-radius:0 0 6px 6px;border:1px solid #e2e8f0;border-top:none;font-size:13px;color:#475569">
    Audit trail of what the agent submitted on your behalf. Verify each field is correct and the cover letter reads naturally. Warnings above any card flag likely regressions.
  </div>

  <div style="margin-top:20px">
  {''.join(cards_html)}
  </div>

  <div style="text-align:center;color:#64748b;font-size:11px;margin-top:20px">
    LLM: NVIDIA NIM, {primary_short} (cover letters) + {fast_short} (scoring)
  </div>
</div>
</body></html>"""

        plain = "FORM SAMPLE REVIEW -- Last 2 Hours\n" + ("="*60) + "".join(cards_text)
        subject = f"[REVIEW] Job Agent | {len(rows)} application samples | {datetime.now().strftime('%a %d %b %H:%M')}"

        notifier._send(subject, html, plain)
        # === PATCH 13: mark rows as reviewed =================================
        try:
            ids_to_mark = [r[0] for r in rows]
            placeholders = ",".join("?" * len(ids_to_mark))
            conn.execute(
                f"UPDATE applications SET sample_reviewed_at = CURRENT_TIMESTAMP "
                f"WHERE id IN ({placeholders})",
                ids_to_mark,
            )
            conn.commit()
            log.info(f"[SAMPLE] Marked {len(ids_to_mark)} rows as reviewed")
        except Exception as e:
            log.warning(f"[SAMPLE] Failed to mark reviewed rows: {e}")
        log.info(f"[SAMPLE] Sent {len(rows)} application samples for review")
    except Exception as e:
        log.error(f"[SAMPLE ERR] {e}")

# === PATCH 15: weekly high-value targets ===================================
def send_weekly_high_value_targets():
    """Sunday 09:00 UK digest: top 10 applications from last 7 days with
    fit >= 80 and status=APPLIED. Silent skip if empty."""
    # === C1: reload in weekly_high_value ==================================
    global cfg
    try:
        cfg = reload_config()
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT company, title, fit_score, applied_at, url, recruiter_email "
            "FROM applications "
            "WHERE applied_at >= datetime('now','-7 days') "
            "  AND status = 'APPLIED' "
            "  AND fit_score >= 80 "
            "ORDER BY fit_score DESC, applied_at DESC "
            "LIMIT 10"
        ).fetchall()
        if not rows:
            log.info("[WEEKLY HV] No high-value targets this week, skipping email")
            return
        notifier.send_weekly_high_value(rows)
        log.info(f"[WEEKLY HV] Sent top {len(rows)} high-value targets")
    except Exception as e:
        log.error(f"[WEEKLY HV ERR] {e}")


scheduler = BackgroundScheduler()
scheduler.add_job(send_hourly_digest, "interval", hours=3, id="hourly_digest")
scheduler.add_job(send_sample_form_email, "interval", hours=3, id="sample_review")
scheduler.add_job(send_weekly_high_value_targets, "cron",
                  day_of_week="sun", hour=9, minute=0,
                  timezone="Europe/London", id="weekly_high_value")
scheduler.start()

# ── Core: process one job URL ─────────────────────────────────────────────────
# === PATCH 14: _is_personal_recruiter_email ==================================
# Reuses scraper's heuristic without creating a cyclic import.
_GENERIC_RECRUITER_PREFIXES = {
    "info", "hello", "contact", "support", "help", "admin",
    "careers", "career", "jobs", "job", "apply", "application",
    "recruiting", "recruitment", "recruiter", "talent", "hr",
    "people", "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "webmaster",
}

def _is_personal_recruiter_email(addr: str) -> bool:
    if not addr or "@" not in addr:
        return False
    local = addr.split("@", 1)[0].lower()
    import re as _re_p14
    clean = _re_p14.sub(r"[._\-0-9]", "", local)
    return clean not in _GENERIC_RECRUITER_PREFIXES


# === C3: application rate limiter ==========================================
# Module-level counter tracks applications shipped in the current sweep.
# Reset each time a fresh sweep starts (tracked via a time-bucket identifier).
_SWEEP_APPLICATIONS = {"sweep_id": None, "count": 0}


def _check_rate_limits() -> tuple[bool, str]:
    """Called before each form submission. Returns (ok_to_proceed, reason).
    Checks two independent limits:
      1. max_applications_per_hour (DB-backed, counts APPLIED in last 60 min)
      2. max_applications_per_sweep (in-memory, counts this-sweep shipments)
    Config keys live-reload via reload_config()."""
    hourly_cap = int(cfg.get("max_applications_per_hour", 12))
    sweep_cap  = int(cfg.get("max_applications_per_sweep", 5))

    # Hourly cap from DB (authoritative, survives restarts).
    try:
        hourly_count = conn.execute(
            "SELECT COUNT(*) FROM applications "
            "WHERE status='APPLIED' AND applied_at >= datetime('now','-1 hour')"
        ).fetchone()[0]
    except Exception:
        hourly_count = 0
    if hourly_count >= hourly_cap:
        return False, f"hourly:{hourly_count}/{hourly_cap}"

    # Sweep cap from in-memory counter. Identify sweep by 5-minute bucket.
    import time as _t
    bucket = int(_t.time() // 300)
    if _SWEEP_APPLICATIONS["sweep_id"] != bucket:
        _SWEEP_APPLICATIONS["sweep_id"] = bucket
        _SWEEP_APPLICATIONS["count"]    = 0
    if _SWEEP_APPLICATIONS["count"] >= sweep_cap:
        return False, f"sweep:{_SWEEP_APPLICATIONS['count']}/{sweep_cap}"

    return True, ""


def _record_application_shipped():
    """Increment in-memory sweep counter after a successful APPLIED."""
    import time as _t
    bucket = int(_t.time() // 300)
    if _SWEEP_APPLICATIONS["sweep_id"] != bucket:
        _SWEEP_APPLICATIONS["sweep_id"] = bucket
        _SWEEP_APPLICATIONS["count"]    = 0
    _SWEEP_APPLICATIONS["count"] += 1


def process_job(url: str):
    # === EMERGENCY: reload config per-sweep ==============================
    # Ensures pause-via-config-edit (min_fit_score: 999) takes effect LIVE
    # without requiring a container restart.
    global cfg
    cfg = reload_config()
    if already_processed(url):
        log.info(f"[SKIP] Already done: {url}")
        return

    # === PATCH 30: LinkedIn daily apply cap ==============================
    # Anti-bot on Apr 15 cut us off at 59 apps in 3.5h. This guard enforces
    # a rolling-24h cap on LinkedIn applications BEFORE any scrape or LLM
    # cost. Fails CLOSED if DB query errors -- safer to lose LinkedIn
    # temporarily than risk account flag.
    from urllib.parse import urlparse as _p30_urlparse
    _p30_host = (_p30_urlparse(url).hostname or "").lower()
    if _p30_host == "linkedin.com" or _p30_host.endswith(".linkedin.com"):
        try:
            _p30_cap = int(cfg.get("linkedin_daily_apply_cap", 10))
        except (TypeError, ValueError):
            _p30_cap = 10
        try:
            import sqlite3 as _p30_sq
            with _p30_sq.connect("/app/applications.db") as _p30_c:
                (_p30_count,) = _p30_c.execute(
                    "SELECT COUNT(*) FROM applications "
                    "WHERE url LIKE '%linkedin.com%' "
                    "AND status = 'APPLIED' "
                    "AND applied_at > datetime('now','-24 hours')"
                ).fetchone()
        except Exception as _p30_e:
            log.warning(f"[LINKEDIN_CAP] query error -- failing closed: {_p30_e}")
            db_log_skip(url, "linkedin_cap:query_error")
            return
        if _p30_count >= _p30_cap:
            log.info(f"[SKIP] LinkedIn daily cap reached ({_p30_count}/{_p30_cap}): {url}")
            db_log_skip(url, f"linkedin_cap:{_p30_count}/{_p30_cap}")
            return
    # === END PATCH 30 ====================================================

    log.info(f"[JOB] {url}")

    # 1. Scrape JD
    try:
        job = scraper.scrape(url)
    except Exception as e:
        log.warning(f"[SCRAPE ERR] {e}")
        db_log_skip(url, f"scrape_error:{e}")
        return

    company = job.get("company", url.split("/")[2].replace("www.", ""))
    title   = job.get("title", "Unknown Role")
    jd_text = job.get("description", "")[:6000]
    recruiter_email = job.get("recruiter_email") or None  # === PATCH 14: recruiter passed to db_log_app

    # === PATCH 19C: empty company -> skip =================================
    # Scraper returns "" for aggregator URLs (CV-Library, Reed, LinkedIn,
    # etc.) where the URL host is not the employer. Skip before any LLM
    # call -- shipping letters with aggregator names as employer is always
    # wrong, and letting cover-letter validation catch them downstream
    # burns 3 LLM calls per futile listing.
    if not company or not company.strip():
        log.info(f"[SKIP] Aggregator or unknown employer: {url}")
        db_log_skip(url, "unknown_employer:aggregator")
        return
    # === PATCH 31E: aggregator host pre-CL guard ==========================
    # Some aggregators (Reed, CV-Library) successfully expose the employer
    # in JSON-LD, so _PATCH 19C_ above does not trigger. But the URL itself
    # is still on the aggregator domain - the form has no directly-clickable
    # submit button, so filler.apply will always fail with 'no matching
    # button'. Avoid burning a cover-letter call (~$0.01 devstral) per
    # aggregator URL: skip here before any LLM spend.
    # Proper per-aggregator ATS support (Reed login + form flow, etc.)
    # is planned for a future session.
    # ======================================================================
    try:
        from urllib.parse import urlparse
        _netloc = urlparse(url).netloc
        _agg_host, _ = _aggregator_id_param(_netloc)
        if _agg_host:
            log.info(f"[SKIP] Aggregator URL ({_agg_host}), skipping before CL: {url}")
            db_log_skip(url, f"aggregator_pre_cl:{_agg_host}")
            return
    except Exception as _e:
        log.warning(f"[P31E] aggregator check error: {_e}")
    # === END PATCH 31E ====================================================

    # 1a. Index-page guard (patch 10) -- reject careers landing pages scraped as jobs
    is_idx, idx_reason = _looks_like_index_page(url, title)
    if is_idx:
        log.info(f"[SKIP] Careers-index page {url} / {title!r} -> {idx_reason}")
        db_log_skip(url, f"index_page:{idx_reason}")
        return

    # 1b. Role filter -- reject wrong titles before any NIM call
    accepted, reason = is_relevant_role(title, url)
    if not accepted:
        log.info(f"[SKIP] Irrelevant title {title!r} -> {reason}")
        db_log_skip(url, f"role_filter:{reason}")
        return

    # === PATCH 36: geographic location filter =============================
    # Reject roles that are explicitly located in non-UK regions when
    # policy says no international relocation. Layered detection:
    # title-level non-UK marker, JD "based in" patterns, multi-location
    # awareness (KEEP if UK appears alongside non-UK markers).
    # Saves cover-letter token spend on geo-mismatched roles, and
    # protects against submitting misrepresented applications to roles
    # like "Solutions Engineer India" or "Forward Deployed Engineer Spain".
    # ======================================================================
    try:
        from core.location_filter import is_location_acceptable
        # Read policy fields directly from profile_answers.yaml
        import yaml as _p36_yaml
        try:
            with open("/app/profile_answers.yaml") as _p36_f:
                _p36_pol = _p36_yaml.safe_load(_p36_f).get("profile_answers", {})
        except Exception as _p36_e:
            log.warning(f"[P36] could not load policy ({_p36_e}); skipping geo-filter")
            _p36_pol = None
        if _p36_pol is not None:
            _p36_ok, _p36_reason = is_location_acceptable(title, jd_text, _p36_pol)
            if not _p36_ok:
                log.info(f"[SKIP] Location mismatch -> {_p36_reason}: {url}")
                db_log_skip(url, f"location_skip:{_p36_reason[:80]}")
                return
            log.debug(f"[P36] location OK: {_p36_reason}")
    except Exception as _p36_e:
        log.warning(f"[P36] geo-filter error (continuing): {_p36_e}")
    # === END PATCH 36 ======================================================

    # 2. Fit score — fast model
    try:
        fit = scorer.score(title, company, jd_text)
    except Exception as e:
        log.warning(f"[FIT ERR] {e}")
        fit = 50  # neutral default

    log.info(f"[FIT] {company} / {title} => {fit}/100")

    if fit < cfg.get("min_fit_score", 55):
        log.info(f"[SKIP] Fit too low ({fit})")
        db_log_skip(url, f"low_fit:{fit}")
        return

    # === C3: rate limit check ===============================================
    rl_ok, rl_reason = _check_rate_limits()
    if not rl_ok:
        log.info(f"[SKIP] Rate limited: {rl_reason}")
        db_log_skip(url, f"rate_limit:{rl_reason}")
        return

    # 3. Cover letter — strong model
    try:
        cover, model_used = cl_gen.generate(title, company, jd_text)
    except Exception as e:
        log.error(f"[CL ERR] {e}")
        db_log_app(url, company, title, fit, f"ERROR:cover_letter", "", "none", str(e), recruiter_email=recruiter_email)
        return

    # 4. Fill & submit — fully autonomous (no pause)
    try:
        # === PATCH 33C: pass role_ctx so PolicyAnswers can classify role ===
        # The dispatcher needs title + jd_text to decide short_term vs
        # permanent (affects sponsorship-future answer among others).
        result = filler.apply(
            url=url,
            cover_letter=cover,
            resume_path=cfg["resume_pdf_path"],
            role_ctx={"title": title, "jd_text": jd_text},
        )
        # === END PATCH 33C =================================================
        status = "APPLIED" if result else "FORM_FAILED"
    except Exception as e:
        log.error(f"[FORM ERR] {e}")
        status = f"ERROR:form"
        db_log_app(url, company, title, fit, status, cover, model_used, str(e), recruiter_email=recruiter_email)
        return

    log.info(f"[{status}] {company} / {title}  fit={fit}  model={model_used}")
    db_log_app(url, company, title, fit, status, cover, model_used, recruiter_email=recruiter_email)
    # === C3: increment sweep counter ========================================
    if status == "APPLIED":
        _record_application_shipped()
    # === PATCH 14: high-fit recruiter alert =================================
    try:
        if (status == "APPLIED" and recruiter_email and fit and float(fit) >= 80.0
                and _is_personal_recruiter_email(recruiter_email)):
            notifier.send_recruiter_alert(
                company=company, title=title, fit=fit, url=url,
                recruiter_email=recruiter_email, cover_letter=cover,
            )
            log.info(f"[ALERT] Recruiter alert sent for {company} / {title} -> {recruiter_email}")
    except Exception as e:
        log.warning(f"[ALERT ERR] Failed to send recruiter alert: {e}")


# ── Main Loop ─────────────────────────────────────────────────────────────────
def run():
    log.info("=== Job Agent Starting ===")
    send_hourly_digest()   # immediate startup report

    while True:
        try:
            urls = finder.find_jobs()
            log.info(f"[FINDER] {len(urls)} jobs discovered")
            for url in urls:
                process_job(url)
                time.sleep(cfg.get("delay_between_jobs", 8))
        except KeyboardInterrupt:
            log.info("Shutting down.")
            scheduler.shutdown()
            break
        except Exception as e:
            log.error(f"[LOOP ERR] {e}")

        pause = cfg.get("loop_interval_minutes", 30) * 60
        log.info(f"[LOOP] Sleeping {pause//60}m before next sweep")
        time.sleep(pause)


if __name__ == "__main__":
    run()
