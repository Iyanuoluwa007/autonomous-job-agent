#!/usr/bin/env python3
"""
Job Agent — Anomaly Watchdog

Runs every 2 hours 06:00–22:00 UTC via /etc/cron.d/job-agent-watchdog.

Purpose: catch silent failures that the daily log check (08:00 UK) misses.
The daily log check has a ~23-hour detection latency. This watchdog reduces
that to ~2 hours for the most common failure modes.

Checks:
  1. agent.log stale (>45 min since last write) — scheduler dead?
  2. Zero applications in last 4 active hours (agent stuck / pipeline broken)
  3. NIM 401/403 errors in last 2h (API key issue)
  4. Cover letter hard-fail rate >30% in last 2h (validator regression)
  5. Aggregator-skip count dropped to 0 over last 2h (patch 19 regression)
  6. DB growth anomaly (>30 applications/hour suggests spam)
  7. NIM fallback-model usage >50% in last 2h (primary model broken)
  8. Disk usage >85% on / partition

Silent if all checks pass. Alerts once per check name per 4-hour window
(via state file $JOBAGENT_HOME/watchdog_state.json) to avoid spam.

Dependencies: stdlib only.

Deploy:
  scp watchdog.py user@<your-server>:$JOBAGENT_HOME/watchdog.py
  chmod +x $JOBAGENT_HOME/watchdog.py

Cron drop-in at /etc/cron.d/job-agent-watchdog:
  0 6,8,10,12,14,16,18,20,22 * * * root /usr/bin/python3 $JOBAGENT_HOME/watchdog.py >> $JOBAGENT_HOME/watchdog.log 2>&1

Env file $JOBAGENT_HOME/watchdog.env (600 perms):
  TELEGRAM_BOT_TOKEN=<token>
  TELEGRAM_CHAT_ID=<chat_id>
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ── Config ─────────────────────────────────────────────────────────────────
# Paths are configurable via JOBAGENT_HOME (parent of the cloned repo).
# Defaults assume containerised deploy under /app.
_HOME = Path(os.environ.get("JOBAGENT_HOME", "/app"))
AGENT_LOG        = _HOME / "job-agent/agent.log"
APPLICATIONS_DB  = _HOME / "job-agent/applications.db"
CONFIG_YAML      = _HOME / "job-agent/config.yaml"
STATE_FILE       = _HOME / "watchdog_state.json"
ENV_FILE         = _HOME / "watchdog.env"

# Thresholds
LOG_STALE_MIN            = 45    # agent.log untouched this long = scheduler dead
NO_APPS_HOURS            = 4     # no applications in this many hours = stuck
NIM_AUTH_ERR_WINDOW_H    = 2
NIM_AUTH_ERR_THRESHOLD   = 1     # even 1 is bad
CL_FAIL_RATE_WINDOW_H    = 2
CL_FAIL_RATE_THRESHOLD   = 0.30  # 30% of cover letters failing
CL_FAIL_MIN_SAMPLE       = 10    # don't alert on <10 attempts (not enough data)
AGGREGATOR_WINDOW_H      = 2
DB_SPAM_PER_HOUR         = 30    # more than N applications/hour = spam
FALLBACK_MODEL_RATE      = 0.50  # >50% fallback usage = primary broken
FALLBACK_MIN_SAMPLE      = 10
DISK_PCT_THRESHOLD       = 85

# Cooldown: same check name doesn't re-alert within this window
ALERT_COOLDOWN_H = 4

# Agent-active hours (UTC) — outside these, skip the "no apps" check
# Agent runs 24/7 but sweeps every 30 min; if paused during night shouldn't alert
ACTIVE_HOURS_START = 6   # 06:00 UTC
ACTIVE_HOURS_END   = 23  # 23:00 UTC (exclusive)


# ── Utilities ──────────────────────────────────────────────────────────────

def load_env():
    """Read TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from watchdog.env."""
    if not ENV_FILE.exists():
        return None, None
    creds = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        creds[k.strip()] = v.strip().strip('"').strip("'")
    return creds.get("TELEGRAM_BOT_TOKEN"), creds.get("TELEGRAM_CHAT_ID")


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)
    except Exception as e:
        print(f"[WATCHDOG] state save failed: {e}", file=sys.stderr)


def send_telegram(token, chat_id, message):
    """Send a Telegram message. Returns True on success."""
    if not token or not chat_id:
        return False
    try:
        data = urlencode({
            "chat_id": chat_id,
            "text": message[:4000],  # Telegram 4096-char limit with buffer
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[WATCHDOG] telegram send failed: {e}", file=sys.stderr)
        return False


def is_agent_paused():
    """True if config.yaml has min_fit_score >= 999 (paused state)."""
    if not CONFIG_YAML.exists():
        return False
    try:
        for line in CONFIG_YAML.read_text().splitlines():
            m = re.match(r"^\s*min_fit_score\s*:\s*(\d+)", line)
            if m:
                return int(m.group(1)) >= 999
    except Exception:
        pass
    return False


def is_in_active_hours():
    """True if current UTC hour is within agent-active window."""
    h = datetime.now(timezone.utc).replace(tzinfo=None).hour
    return ACTIVE_HOURS_START <= h < ACTIVE_HOURS_END


def db_query(sql, params=()):
    """Run a SQL query against applications.db, return fetchall."""
    if not APPLICATIONS_DB.exists():
        return None
    try:
        # Use URI mode to open read-only (avoids lock contention with agent)
        uri = f"file:{APPLICATIONS_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[WATCHDOG] db query failed ({sql[:40]}): {e}", file=sys.stderr)
        return None


def grep_log(pattern, hours_back):
    """Return matching log lines from agent.log within last N hours."""
    if not AGENT_LOG.exists():
        return []
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_back)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    matches = []
    try:
        # Read log in binary, decode with replacement for robustness
        with open(AGENT_LOG, "rb") as f:
            # Seek near end: most recent ~5MB (hours of logs)
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 5 * 1024 * 1024)
            f.seek(size - read_size)
            data = f.read().decode("utf-8", errors="replace")
        regex = re.compile(pattern)
        for line in data.splitlines():
            # Extract timestamp from start of line (YYYY-MM-DD HH:MM:SS)
            ts_match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            if not ts_match:
                continue
            if ts_match.group(1) < cutoff_str:
                continue
            if regex.search(line):
                matches.append(line)
    except Exception as e:
        print(f"[WATCHDOG] log grep failed ({pattern[:30]}): {e}", file=sys.stderr)
    return matches


# ── Individual checks ───────────────────────────────────────────────────────

def check_log_mtime_fresh():
    """Check 1: agent.log modified within last LOG_STALE_MIN minutes."""
    if not AGENT_LOG.exists():
        return False, "agent.log does not exist"
    age_sec = time.time() - AGENT_LOG.stat().st_mtime
    age_min = int(age_sec / 60)
    if age_min > LOG_STALE_MIN:
        return False, f"agent.log stale ({age_min} min since last write; scheduler may be dead)"
    return True, f"agent.log fresh ({age_min} min old)"


def check_recent_applications():
    """Check 2: At least one application in last NO_APPS_HOURS, if in active hours."""
    if is_agent_paused():
        return True, "agent paused (min_fit_score >= 999); skipping"
    if not is_in_active_hours():
        return True, f"outside active hours ({ACTIVE_HOURS_START}-{ACTIVE_HOURS_END} UTC); skipping"
    # === PATCH 23 W-Fix-2: tuned skip classification ================
    # Agent filtering out junk aggregator URLs for 4h straight is NORMAL
    # (patch 19 working as designed). Only alert if either:
    #   1. Absolutely no activity (pipeline dead), OR
    #   2. Skips contain reasons we don't recognize (possible new bug)
    # Healthy 100%-aggregator-filter periods should stay SILENT.
    KNOWN_GOOD_SKIP_PREFIXES = (
        "unknown_employer:aggregator",    # patch 19
        "role_filter:no_accept_keyword",  # patch 11 role filter
        "role_filter:",                   # any role filter variant
        "low_fit:",                       # fit scorer (any numeric suffix)
        "junk_url:",                      # patch 20 URL pattern filter
        "index_page",                     # patch 10 careers-landing guard
        "leadgen:",                       # patch 11 leadgen detector
        "already_processed",              # normal dedup
        # === PATCH 36-WD: recognise new safety-filter reasons ============
        # All three appear during normal operation now and should not
        # trigger watchdog alerts.
        "aggregator_pre_cl:",             # patch 31E aggregator pre-CL skip
        "aggregator:",                    # patch 31E generic aggregator skip
        "location_skip:",                 # patch 36 geo-filter
        # === END PATCH 36-WD ==============================================
    )
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=NO_APPS_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db_query(
        "SELECT COUNT(*) FROM applications WHERE status='APPLIED' AND applied_at >= ?",
        (cutoff,),
    )
    if rows is None:
        return False, "db query failed — applications.db unreachable"
    apps = rows[0][0]

    # Get skip reasons with counts
    skip_rows = db_query(
        "SELECT reason, COUNT(*) FROM skipped WHERE ts >= ? GROUP BY reason",
        (cutoff,),
    )
    skip_reasons = skip_rows if skip_rows else []
    total_skips = sum(r[1] for r in skip_reasons)

    # Case 1: Zero everything. Pipeline dead.
    if apps == 0 and total_skips == 0:
        return False, f"no applications AND no skips in last {NO_APPS_HOURS}h — pipeline broken?"

    # Case 2: Zero apps, some skips. Classify skips.
    if apps == 0 and total_skips > 0:
        known_good_count = 0
        unknown_reasons = []
        for reason, cnt in skip_reasons:
            if reason and any(reason.startswith(p) for p in KNOWN_GOOD_SKIP_PREFIXES):
                known_good_count += cnt
            else:
                unknown_reasons.append(f"{cnt} {reason!r}")
        if unknown_reasons:
            # Something filtering we don't recognize
            summary = ", ".join(unknown_reasons[:3])
            return False, f"0 applications, {total_skips} skips in {NO_APPS_HOURS}h — unexpected reasons: {summary}"
        # All skips are known-good filters. Healthy.
        return True, f"0 applications, {total_skips} skips in {NO_APPS_HOURS}h (all known-good filters)"

    # Case 3: Applications happening. Healthy.
    return True, f"{apps} applications in last {NO_APPS_HOURS}h"
    # === END W-Fix-2 ================================================


def check_nim_auth_errors():
    """Check 3: NIM 401/403/unauthorized in recent log."""
    matches = grep_log(r"\[NIM\].*(401|403|[Uu]nauthorized|[Ii]nvalid.*key)", NIM_AUTH_ERR_WINDOW_H)
    if len(matches) >= NIM_AUTH_ERR_THRESHOLD:
        return False, f"{len(matches)} NIM auth errors in last {NIM_AUTH_ERR_WINDOW_H}h — API key issue?"
    return True, f"0 NIM auth errors in last {NIM_AUTH_ERR_WINDOW_H}h"


def check_cover_letter_fail_rate():
    """Check 4: Cover letter hard-fail rate."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=CL_FAIL_RATE_WINDOW_H)).strftime("%Y-%m-%d %H:%M:%S")
    # Count total cover letter attempts in window
    rows_total = db_query(
        "SELECT COUNT(*) FROM applications WHERE applied_at >= ? AND status IN ('APPLIED','ERROR:cover_letter','FORM_FAILED')",
        (cutoff,),
    )
    if rows_total is None:
        return False, "db query failed"
    total = rows_total[0][0]
    if total < CL_FAIL_MIN_SAMPLE:
        return True, f"sample too small ({total} < {CL_FAIL_MIN_SAMPLE})"
    rows_fail = db_query(
        "SELECT COUNT(*) FROM applications WHERE applied_at >= ? AND status='ERROR:cover_letter'",
        (cutoff,),
    )
    fails = rows_fail[0][0] if rows_fail else 0
    rate = fails / total
    if rate > CL_FAIL_RATE_THRESHOLD:
        return False, f"cover letter hard-fail rate {rate:.0%} ({fails}/{total}) exceeds {CL_FAIL_RATE_THRESHOLD:.0%}"
    return True, f"cover letter fail rate {rate:.0%} ({fails}/{total})"


def check_aggregator_skips():
    """Check 5: Aggregator-skip count is nonzero (patch 19 working)."""
    if is_agent_paused():
        return True, "agent paused; skipping"
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=AGGREGATOR_WINDOW_H)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db_query(
        "SELECT COUNT(*) FROM skipped WHERE ts >= ? AND reason='unknown_employer:aggregator'",
        (cutoff,),
    )
    if rows is None:
        return False, "db query failed"
    count = rows[0][0]
    # Also check total skipped — if there's NO activity at all, nothing to compare
    rows_total = db_query(
        "SELECT COUNT(*) FROM skipped WHERE ts >= ?",
        (cutoff,),
    )
    total = rows_total[0][0] if rows_total else 0
    if total == 0:
        return True, f"no activity in last {AGGREGATOR_WINDOW_H}h; skipping"
    if count == 0 and total >= 10:
        return False, f"patch 19 regression? 0 aggregator skips in last {AGGREGATOR_WINDOW_H}h (total skips: {total})"
    return True, f"{count} aggregator skips in last {AGGREGATOR_WINDOW_H}h"


def check_db_growth_spam():
    """Check 6: DB growth rate. >30 applications/hour is spam territory."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db_query(
        "SELECT COUNT(*) FROM applications WHERE applied_at >= ?",
        (cutoff,),
    )
    if rows is None:
        return False, "db query failed"
    count = rows[0][0]
    if count > DB_SPAM_PER_HOUR:
        return False, f"{count} applications in last 1h exceeds {DB_SPAM_PER_HOUR}/h spam threshold"
    return True, f"{count} applications in last 1h (threshold {DB_SPAM_PER_HOUR}/h)"


def check_fallback_model_rate():
    """Check 7: Fallback model usage. >50% means primary model broken."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    # === PATCH 23 W-Fix-1: correct column name ======================
    # Column is model_used (not model). Schema: id, url, company, title,
    # fit_score, status, cover_letter, model_used, error, applied_at,
    # sample_reviewed_at, recruiter_email.
    rows = db_query(
        "SELECT model_used, COUNT(*) FROM applications WHERE applied_at >= ? AND status='APPLIED' GROUP BY model_used",
        (cutoff,),
    )
    # === END W-Fix-1 ================================================
    if rows is None:
        return False, "db query failed"
    if not rows:
        return True, "no successful applications in last 2h; skipping"
    total = sum(r[1] for r in rows)
    if total < FALLBACK_MIN_SAMPLE:
        return True, f"sample too small ({total} < {FALLBACK_MIN_SAMPLE})"
    # Primary model is mistralai/devstral-2-123b-instruct-2512 per config
    primary_count = 0
    for model, cnt in rows:
        if model and "devstral" in model.lower():
            primary_count += cnt
    primary_rate = primary_count / total
    if primary_rate < (1 - FALLBACK_MODEL_RATE):
        return False, f"primary model only {primary_rate:.0%} of {total} apps — primary broken?"
    return True, f"primary model {primary_rate:.0%} of {total} apps"


def check_disk_usage():
    """Check 8: Disk usage on / partition."""
    try:
        stat = shutil.disk_usage("/")
        pct_used = (stat.used / stat.total) * 100
        if pct_used > DISK_PCT_THRESHOLD:
            free_gb = stat.free / (1024 ** 3)
            return False, f"disk usage {pct_used:.0f}% exceeds {DISK_PCT_THRESHOLD}% ({free_gb:.1f} GB free)"
        return True, f"disk usage {pct_used:.0f}%"
    except Exception as e:
        return False, f"disk check failed: {e}"


# ── Main ───────────────────────────────────────────────────────────────────

CHECKS = [
    ("log_mtime_fresh",       check_log_mtime_fresh),
    ("recent_applications",   check_recent_applications),
    ("nim_auth_errors",       check_nim_auth_errors),
    ("cover_letter_fail_rate", check_cover_letter_fail_rate),
    ("aggregator_skips",      check_aggregator_skips),
    ("db_growth_spam",        check_db_growth_spam),
    ("fallback_model_rate",   check_fallback_model_rate),
    ("disk_usage",            check_disk_usage),
]


def main():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    print(f"[WATCHDOG] run at {now.isoformat()}Z")

    state = load_state()
    token, chat_id = load_env()
    if not token or not chat_id:
        print("[WATCHDOG] WARNING: no Telegram creds; alerts will be stdout-only", file=sys.stderr)

    failures = []
    passes = 0

    for name, check_fn in CHECKS:
        try:
            ok, msg = check_fn()
        except Exception as e:
            ok, msg = False, f"check crashed: {type(e).__name__}: {e}"
        print(f"[{name}] {'OK' if ok else 'FAIL'}: {msg}")
        if ok:
            passes += 1
        else:
            failures.append((name, msg))

    # Apply cooldown filter: only keep failures whose last alert was > ALERT_COOLDOWN_H ago
    now_ts = now.timestamp()
    cooldown_sec = ALERT_COOLDOWN_H * 3600
    alerting_failures = []
    for name, msg in failures:
        last = state.get(f"last_alert_{name}", 0)
        if now_ts - last >= cooldown_sec:
            alerting_failures.append((name, msg))
            state[f"last_alert_{name}"] = now_ts
        else:
            mins_left = int((cooldown_sec - (now_ts - last)) / 60)
            print(f"[{name}] in cooldown ({mins_left} min left); not re-alerting")

    state["last_run"] = now_ts
    save_state(state)

    if alerting_failures:
        lines = [f"[JA WATCHDOG] {now.strftime('%d %b %Y %H:%M UTC')}"]
        for name, msg in alerting_failures:
            lines.append(f"[FAIL] {name}: {msg}")
        lines.append("")
        lines.append(f"{len(failures)} failed, {passes} passed "
                     f"({len(alerting_failures)} alerting, {len(failures) - len(alerting_failures)} in cooldown)")
        message = "\n".join(lines)
        print("--- ALERT ---")
        print(message)
        sent = send_telegram(token, chat_id, message)
        print(f"[WATCHDOG] Telegram send: {'OK' if sent else 'FAILED'}")
    else:
        print(f"[WATCHDOG] all clear ({passes}/{len(CHECKS)} passed)")


if __name__ == "__main__":
    main()
