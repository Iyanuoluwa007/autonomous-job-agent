"""
Email Notifier — Hourly status digest
Style: Sentinel Quant status reports
Sends via SMTP (Gmail app password or any SMTP server).
"""

import smtplib, logging
from email.mime.text       import MIMEText
from email.mime.multipart  import MIMEMultipart
from datetime              import datetime

log = logging.getLogger("notifier")


def _pretty_model(m):
    """Shorten a NIM model slug for display: mistralai/devstral-2-123b-instruct-2512 -> devstral-2-123b."""
    if not m:
        return "?"
    last = m.split("/")[-1]
    for suffix in ("-instruct-2512", "-instruct"):
        if last.endswith(suffix):
            last = last[: -len(suffix)]
            break
    return last


class EmailNotifier:
    def __init__(self, cfg: dict, models: dict = None):
        self.models = models or {}
        self.smtp_host = cfg.get("smtp_host", "smtp.gmail.com")
        self.smtp_port = cfg.get("smtp_port", 587)
        self.username  = cfg["smtp_user"]
        self.password  = cfg["smtp_pass"]
        self.from_addr = cfg.get("from_addr", cfg["smtp_user"])
        self.to_addrs  = cfg["to_addrs"]   # list of recipient emails

    def _send(self, subject: str, body_html: str, body_text: str):
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = self.from_addr
        msg["To"]      = ", ".join(self.to_addrs)
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(self.smtp_host, self.smtp_port) as s:
            s.starttls()
            s.login(self.username, self.password)
            s.sendmail(self.from_addr, self.to_addrs, msg.as_string())

    def send_digest(self, stats: dict):
        primary_short = _pretty_model(self.models.get("primary"))
        fast_short    = _pretty_model(self.models.get("fast"))
        now     = datetime.now().strftime("%a %d %b %Y — %H:%M")
        applied = stats.get("applied") or 0
        total   = stats.get("total")   or 0
        errors  = stats.get("errors")  or 0
        low_fit = stats.get("low_fit") or 0
        avg_fit = stats.get("avg_fit") or "—"
        skip    = stats.get("scraped_skip") or 0
        recent  = stats.get("recent") or []

        # Status indicator
        if errors == 0 and applied > 0:
            status_icon  = "[OK]"
            status_color = "#22c55e"
            status_label = "HEALTHY"
        elif errors > 0 and applied == 0:
            status_icon  = "[ERR]"
            status_color = "#ef4444"
            status_label = "ERRORS ONLY"
        elif errors > 0:
            status_icon  = "[WARN]"
            status_color = "#f59e0b"
            status_label = "PARTIAL"
        else:
            status_icon  = "[--]"
            status_color = "#94a3b8"
            status_label = "IDLE"

        # Build recent activity table rows
        rows_html = ""
        rows_text = ""
        for company, title, fit, status in recent:
            sc = "#22c55e" if status == "APPLIED" else ("#ef4444" if "ERROR" in str(status) else "#94a3b8")
            rows_html += f"""
            <tr>
              <td style="padding:6px 10px;border-bottom:1px solid #1e293b">{company}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #1e293b">{title}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #1e293b;text-align:center">{fit or '—'}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #1e293b;color:{sc};font-weight:600">{status}</td>
            </tr>"""
            rows_text += f"  {company:<20} {title:<30} fit={fit:<5} {status}\n"

        html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:'Courier New',monospace;color:#e2e8f0">
<div style="max-width:640px;margin:0 auto;padding:24px">

  <!-- Header -->
  <div style="border-left:4px solid {status_color};padding:16px 20px;background:#1e293b;margin-bottom:24px">
    <div style="font-size:11px;color:#64748b;letter-spacing:2px;text-transform:uppercase">JOB APPLICATION AGENT</div>
    <div style="font-size:20px;font-weight:700;color:{status_color};margin:6px 0">{status_icon} {status_label}</div>
    <div style="font-size:12px;color:#94a3b8">{now}</div>
  </div>

  <!-- Stats Grid -->
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:24px">
    <div style="background:#1e293b;padding:16px;text-align:center;border:1px solid #334155">
      <div style="font-size:28px;font-weight:700;color:#22c55e">{applied}</div>
      <div style="font-size:10px;color:#64748b;letter-spacing:1px">APPLIED</div>
    </div>
    <div style="background:#1e293b;padding:16px;text-align:center;border:1px solid #334155">
      <div style="font-size:28px;font-weight:700;color:#f59e0b">{low_fit}</div>
      <div style="font-size:10px;color:#64748b;letter-spacing:1px">LOW FIT SKIP</div>
    </div>
    <div style="background:#1e293b;padding:16px;text-align:center;border:1px solid #334155">
      <div style="font-size:28px;font-weight:700;color:#ef4444">{errors}</div>
      <div style="font-size:10px;color:#64748b;letter-spacing:1px">ERRORS</div>
    </div>
  </div>

  <!-- Secondary Stats -->
  <div style="background:#1e293b;padding:14px 20px;border:1px solid #334155;margin-bottom:24px;font-size:13px">
    <span style="color:#64748b">TOTAL PROCESSED: </span><span style="color:#e2e8f0;font-weight:600">{total}</span>
    &nbsp;&nbsp;&nbsp;
    <span style="color:#64748b">AVG FIT SCORE: </span><span style="color:#e2e8f0;font-weight:600">{avg_fit}/100</span>
    &nbsp;&nbsp;&nbsp;
    <span style="color:#64748b">SCRAPE SKIPPED: </span><span style="color:#e2e8f0;font-weight:600">{skip}</span>
  </div>

  <!-- Recent Activity -->
  {"" if not recent else f'''
  <div style="font-size:10px;color:#64748b;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px">RECENT ACTIVITY</div>
  <table style="width:100%;border-collapse:collapse;background:#1e293b;font-size:12px">
    <thead>
      <tr style="background:#0f172a">
        <th style="padding:8px 10px;text-align:left;color:#64748b;font-weight:normal">COMPANY</th>
        <th style="padding:8px 10px;text-align:left;color:#64748b;font-weight:normal">ROLE</th>
        <th style="padding:8px 10px;text-align:center;color:#64748b;font-weight:normal">FIT</th>
        <th style="padding:8px 10px;text-align:left;color:#64748b;font-weight:normal">STATUS</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  '''}

  <!-- Model Info -->
  <div style="margin-top:24px;font-size:11px;color:#334155;text-align:center">
    LLM: NVIDIA NIM, {primary_short} (cover letters) + {fast_short} (scoring)
  </div>

</div>
</body>
</html>"""

        plain = f"""
JOB APPLICATION AGENT — {status_label}
{now}
{'='*50}
Applied:       {applied}
Low Fit Skip:  {low_fit}
Errors:        {errors}
Total:         {total}
Avg Fit Score: {avg_fit}/100
Scrape Skip:   {skip}

RECENT ACTIVITY:
{rows_text or '  (none this hour)'}

LLM: NVIDIA NIM, {primary_short} (cover letters) + {fast_short} (scoring)
"""

        subject = f"{status_icon} Job Agent | {applied} applied | {errors} errors | {now}"
        try:
            self._send(subject, html, plain)
            log.info(f"[EMAIL] Digest sent: applied={applied} errors={errors}")
        except Exception as e:
            log.error(f"[EMAIL ERR] {e}")

    def send_recruiter_alert(self, company, title, fit, url, recruiter_email, cover_letter):
        """PATCH 14: Immediate alert for high-fit jobs with personal recruiter email.
        Includes the cover letter so the user can paste-and-send in one step."""
        def _esc(x):
            return (str(x) if x is not None else "") \
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        cover_html = _esc(cover_letter or "(empty)").replace("\n", "<br/>")
        subject = f"[RECRUITER] {company} / {title} | fit {fit} | {recruiter_email}"
        html = f"""
<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#1e293b">
<div style="max-width:720px;margin:0 auto">
  <div style="background:#0f172a;color:white;padding:16px 20px;border-radius:6px 6px 0 0">
    <div style="font-size:11px;color:#94a3b8;letter-spacing:2px;text-transform:uppercase">RECRUITER CONTACT FOUND</div>
    <div style="font-size:16px;font-weight:700;margin-top:4px">{_esc(company)} &mdash; {_esc(title)}</div>
  </div>
  <div style="background:white;padding:18px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 6px 6px">
    <table style="width:100%;font-size:13px;border-collapse:collapse">
      <tr><td style="padding:4px 0;color:#64748b;width:120px">Fit score</td>
          <td style="padding:4px 0;font-weight:700;color:#22c55e">{_esc(fit)}/100</td></tr>
      <tr><td style="padding:4px 0;color:#64748b">Recruiter</td>
          <td style="padding:4px 0;font-weight:700"><a href="mailto:{_esc(recruiter_email)}" style="color:#3b82f6;text-decoration:none">{_esc(recruiter_email)}</a></td></tr>
      <tr><td style="padding:4px 0;color:#64748b">Job URL</td>
          <td style="padding:4px 0"><a href="{_esc(url)}" style="color:#3b82f6;text-decoration:none">{_esc(url)}</a></td></tr>
    </table>
    <div style="margin-top:16px;padding:10px 14px;background:#fef3c7;border-left:3px solid #f59e0b;font-size:12px;color:#78350f">
      <strong>Suggested action:</strong> reply to this email thread manually with the cover letter below (edit to taste). Direct email is far more effective than portal submission for high-fit roles.
    </div>
    <div style="font-size:11px;color:#64748b;letter-spacing:1px;text-transform:uppercase;margin:18px 0 6px">Cover letter used</div>
    <div style="background:#f8fafc;border-left:3px solid #3b82f6;padding:12px 14px;font-size:12px;line-height:1.55;color:#1e293b;white-space:pre-wrap">{cover_html}</div>
  </div>
</div>
</body></html>"""
        plain = (
            f"RECRUITER CONTACT FOUND\n\n"
            f"Company:   {company}\n"
            f"Role:      {title}\n"
            f"Fit:       {fit}/100\n"
            f"Recruiter: {recruiter_email}\n"
            f"URL:       {url}\n\n"
            f"Suggested action: email the recruiter directly with the cover letter below.\n\n"
            f"--- COVER LETTER ---\n{cover_letter or '(empty)'}\n"
        )
        try:
            self._send(subject, html, plain)
            log.info(f"[EMAIL] Recruiter alert sent for {company} / {title}")
        except Exception as e:
            log.error(f"[EMAIL ERR] recruiter alert: {e}")

    def send_weekly_high_value(self, rows):
        """PATCH 15: Weekly summary of top-fit applications worth manual follow-up."""
        def _esc(x):
            return (str(x) if x is not None else "") \
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        from urllib.parse import quote_plus
        cards = []
        for company, title, fit, applied_at, url, rec_email in rows:
            linkedin_query = quote_plus(f"{company} engineering hiring")
            linkedin_url = f"https://www.linkedin.com/search/results/people/?keywords={linkedin_query}"
            rec_html = (
                f'<a href="mailto:{_esc(rec_email)}" style="color:#3b82f6;text-decoration:none">{_esc(rec_email)}</a>'
                if rec_email else '<span style="color:#94a3b8">(none found)</span>'
            )
            cards.append(f"""
  <div style="border:1px solid #e2e8f0;border-radius:6px;background:white;padding:14px 16px;margin-bottom:12px">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap">
      <div style="flex:1;min-width:240px">
        <div style="font-size:14px;font-weight:700;color:#0f172a">{_esc(company)} &mdash; {_esc(title)}</div>
        <div style="font-size:11px;color:#64748b;margin-top:2px">Applied {_esc(applied_at)}</div>
      </div>
      <span style="background:#22c55e;color:white;padding:3px 9px;border-radius:3px;font-size:11px;font-weight:700">FIT {_esc(fit)}</span>
    </div>
    <table style="width:100%;font-size:12px;margin-top:10px;border-collapse:collapse">
      <tr><td style="padding:2px 0;color:#64748b;width:110px">Job URL</td>
          <td style="padding:2px 0"><a href="{_esc(url)}" style="color:#3b82f6;text-decoration:none">{_esc(url[:70])}</a></td></tr>
      <tr><td style="padding:2px 0;color:#64748b">Recruiter</td>
          <td style="padding:2px 0">{rec_html}</td></tr>
      <tr><td style="padding:2px 0;color:#64748b">Find contacts</td>
          <td style="padding:2px 0"><a href="{linkedin_url}" style="color:#3b82f6;text-decoration:none">LinkedIn search: {_esc(company)} engineering</a></td></tr>
    </table>
  </div>
""")
        html = f"""
<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#1e293b">
<div style="max-width:720px;margin:0 auto">
  <div style="background:#0f172a;color:white;padding:16px 20px;border-radius:6px 6px 0 0">
    <div style="font-size:11px;color:#94a3b8;letter-spacing:2px;text-transform:uppercase">WEEKLY HIGH-VALUE TARGETS</div>
    <div style="font-size:16px;font-weight:700;margin-top:4px">Top {len(rows)} applications from last 7 days (fit &ge; 80)</div>
  </div>
  <div style="background:white;padding:14px 16px;border:1px solid #e2e8f0;border-top:none;border-bottom:none;font-size:13px;color:#475569">
    Action list for Monday morning: pick 2-3 of these, send a short personalised LinkedIn message or email to the company's engineering leadership. Higher response rate than the portal submission.
  </div>
  <div style="background:#f1f5f9;padding:16px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 6px 6px">
    {''.join(cards)}
  </div>
</div>
</body></html>"""
        plain_lines = [f"WEEKLY HIGH-VALUE TARGETS -- Top {len(rows)} applications from last 7 days\n" + "=" * 60]
        for company, title, fit, applied_at, url, rec_email in rows:
            plain_lines.append(f"\n{company} -- {title}")
            plain_lines.append(f"  Fit: {fit}/100 | Applied: {applied_at}")
            plain_lines.append(f"  URL: {url}")
            plain_lines.append(f"  Recruiter: {rec_email or '(none found)'}")
        plain = "\n".join(plain_lines)
        subject = f"[HV TARGETS] Weekly summary | {len(rows)} high-fit applications"
        try:
            self._send(subject, html, plain)
            log.info(f"[EMAIL] Weekly HV targets sent: {len(rows)} rows")
        except Exception as e:
            log.error(f"[EMAIL ERR] weekly HV: {e}")

    def send_alert(self, subject: str, message: str):
        """Immediate alert for critical events."""
        self._send(
            f"[ALERT] Job Agent — {subject}",
            f"<pre style='font-family:monospace'>{message}</pre>",
            message,
        )
