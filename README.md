# Autonomous Job Agent

A policy-driven autonomous job-application agent with multi-layer safety filtering, designed to apply to engineering roles on a candidate's behalf without misrepresentation.

Built as a portfolio project to learn what production-grade automation actually requires: rule-based dispatch backed by LLM fallback, dry-run audit mode, geographic + role + dedup filters, and verified-submit semantics that prove a form was actually accepted before recording success.

---

## What It Does

Given a YAML policy file and a CV, the agent:

1. **Discovers** roles from Reed (UK) and a configurable list of direct-employer career pages (Greenhouse, Lever, Ashby, custom).
2. **Filters** out roles that don't match the candidate's target profile: irrelevant titles, aggregator URLs, geographic mismatches, low fit-score, or already-processed URLs.
3. **Generates** tailored cover letters via a local LLM (NVIDIA NIM Llama 3.3 / devstral).
4. **Fills** application forms with Playwright, including dropdowns and radio groups answered from policy.
5. **Submits** only after verifying the form actually navigated to a confirmation page (no phantom-success).
6. **Logs** everything for audit, with a separate dry-run mode that intercepts LLM-derived answers before they reach a real form.

The differentiator is the **safety architecture**, not the discovery rate. The agent will refuse to submit a form rather than guess an answer to a question that isn't in its policy or doesn't match a fixture.

---

## Architecture

```
                    +-----------------+
   discovery        |  Reed search    |  +- blacklist filter
                    |  Career-page    |--+  role filter (~100 keywords)
                    |   crawler       |  +- aggregator pre-skip
                    +--------+--------+
                             |
                    +--------v--------+
   gating           | process_job(url)|
                    +--------+--------+
                             |
        +--------------------+--------------------+
        v                    v                    v
  geographic-filter    fit-score (LLM)      dedup (canonical URL)
  (location_filter)    (~3-class scorer)    (status-aware)
        |                    |                    |
        +--------------------+--------------------+
                             v
                    +-----------------+
   form-fill        | Playwright      |
                    | + Cover letter  |
                    | + Policy answers|  <- rule dispatcher (22 unit tests)
                    | + LLM cascade   |  <- NIM primary, Haiku fallback
                    |   (dry-run gate)|  <- intercepts in audit mode
                    +--------+--------+
                             |
                    +--------v--------+
   verification     | submit + verify |
                    | URL-change OR   |
                    | confirmation-   |
                    | text match      |
                    +--------+--------+
                             |
                    +--------v--------+
   record           | applications.db |
                    | + audit log     |
                    +-----------------+
```

### Key Components

| Module | Role |
|--------|------|
| `agent.py` | Top-level orchestration, scheduler, `process_job()` pipeline |
| `core/policy_answers.py` | Rule dispatcher: keyword patterns answer common form questions from `profile_answers.yaml`. Falls back to LLM cascade for novel questions. |
| `core/llm.py` | NIM (Llama 3.3) and Anthropic (Haiku) clients with retry + backoff |
| `core/form_filler.py` | Playwright wrapper, verified-submit logic, name-split priority, consent checkbox handling |
| `core/role_filter.py` | Title-keyword classifier (accept/reject/override lists) |
| `core/location_filter.py` | Geographic filter: title-level non-UK detection, "Remote, <city>" pattern, multi-location awareness |
| `core/fit_scorer.py` | Fit-score classifier (LLM-backed) |
| `core/cover_letter.py` | Cover letter generation, signoff stripping, validation |
| `scrapers/scraper.py` | JD extraction (HTML, JSON-LD), employer override |
| `scrapers/finder.py` | Reed search + career-page crawler |
| `watchdog.py` | Out-of-band monitor, runs every 2h, checks for silent failures (stale log, NIM auth, cover-letter fail rate, etc.) |

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker (recommended for the agent itself)
- An NVIDIA NIM API key (free tier works) and/or Anthropic API key
- A Reed.co.uk API key (free, 24h delay)

### Setup

```bash
git clone https://github.com/Iyanuoluwa007/autonomous-job-agent.git
cd autonomous-job-agent
pip install -r requirements.txt
playwright install chromium

# Configure
cp config.yaml.example config.yaml
# Edit config.yaml with your API keys, profile email, paths

cp profile_answers.yaml.example profile_answers.yaml
# Edit profile_answers.yaml with YOUR actual policy. Read every line.

cp discovered_companies.json.example discovered_companies.json
# Edit discovered_companies.json to point at the career pages you want crawled.

# Place your CV at the path config.yaml points to
# Default: ./resume.pdf
```

### First Run (Dry-Run Strongly Recommended)

```bash
# Dry-run mode: no real form submissions, only audit logging
python agent.py --dry-run

# Watchdog: read silent-failure checks (run separately, e.g. via cron)
python watchdog.py
```

### Production

Set `llm_fallback_dry_run: false` in `profile_answers.yaml` only after auditing the LLM cascade output for at least 50-100 decisions in dry-run mode. The agent will still skip questions where the LLM confidence is below threshold (default 0.7).

---

## Configuration

All runtime behavior is controlled by two files:

- **`config.yaml`** - infrastructure settings: API keys, paths, scheduler intervals, watchdog thresholds.
- **`profile_answers.yaml`** - the candidate's policy: work authorization, salary, skills, location preferences, what the agent is allowed to claim.

Both files have `.example` versions in the repo. Neither real version should be committed - both are in `.gitignore`.

---

## Engineering Notes

This section documents real bugs and decisions encountered during operation. They're included because the lessons are more interesting than the architecture diagram.

### The phantom-submission bug

**What happened:** During production use, the agent's database showed 352 rows with status `APPLIED` and timestamps. The candidate noticed they were not receiving confirmation emails for these applications - the count of confirmation emails in their inbox was much lower than the database claimed.

**Investigation:** The form-submit code was clicking the submit button and recording success based on a 200 HTTP response. But many forms either:
- Required a final consent checkbox that was sometimes missed
- Returned 200 on a validation-error page (form re-displayed with error highlighting)
- Used JavaScript-driven submission that the click-handler triggered before the JS was ready

In all three cases the agent recorded `APPLIED` but no application was actually submitted.

**Fix (P31a-e patch series):** Submit logic was rewritten to require positive evidence of acceptance:
1. URL change to a confirmation page, OR
2. Confirmation-text match in the post-submit DOM (~30 phrases like "thank you for applying", "application received", language-aware)

If neither signal appears within 10 seconds, the row is recorded as `FORM_FAILED` instead of `APPLIED`. The 352 affected rows were migrated to `APPLIED_UNVERIFIED` so that dedup wouldn't block legitimate retry attempts under the new logic.

**Lesson:** "200 OK" is not "form accepted." Verified-submit semantics are mandatory for any form-submission automation. The client-server contract for form completion is application-specific - you have to look for the actual confirmation, not just the HTTP layer.

### Geographic filter false positive ("Remote, San Francisco")

**What happened:** Shipped a layered geographic filter (`location_filter.py`) that detects non-UK roles and skips them when policy says relocation is not allowed. Self-tests passed, deployed.

**Same day:** Manual diagnostic on an ElevenLabs role titled "Forward Deployed Engineer - Software Engineer" showed the filter returned `ACCEPT` with reason `"location-flexible hint (remote/global/EMEA)"`. But the JD said "Remote, San Francisco" - a US-based remote role requiring US work authorization.

**Root cause:** Rule order. The location-flexible hint check (Rule 4) fired on the word "Remote" before the JD-lock check (Rule 5) had a chance to evaluate the non-UK city. First-match-wins semantics meant the role passed despite the SF anchor.

**Fix (P36-FIX patch):** Two changes:
1. New `_REMOTE_CITY_PATTERN` regex matches `Remote[,/-|] <city>` constructs and reads the captured city as a non-UK candidate.
2. Re-ordered rules so JD-lock and Remote+city checks run before the location-flexible-hint check.

Self-tests extended from 11 to 16 cases; all pass. Verified live against the original ElevenLabs URL: now correctly REJECTS with reason `"Remote+non-UK: ['san francisco']"`.

**Lesson:** Multi-rule evaluators with first-match-wins semantics are subtly ordering-sensitive. When you add a new rule that overlaps with existing rules, you have to think about ordering, not just correctness in isolation. Adversarial real-world inputs find the edge cases that synthetic test fixtures don't.

### LLM cascade dry-run audit mode

**What happened:** When the rule dispatcher couldn't answer a form question (no keyword match, no fixture), the original behavior was to skip the question and abort the form. This was safe but blocked the agent on any form with a novel question.

**Design decision:** Add an LLM fallback - send the question + options + the candidate's policy as context, ask the model to pick the most policy-consistent option, validate the response, fill if confidence > threshold. NIM Llama 3.3 70B as primary (free, low latency), Anthropic Haiku as fallback for ambiguous picks.

**Risk:** The LLM might pick an answer that's plausible but wrong - hallucinating skills the candidate doesn't have, claiming work authorization that doesn't apply, etc. Misrepresenting the candidate to employers is a much worse failure mode than skipping a form.

**Mitigation - dry-run mode:** A flag `llm_fallback_dry_run: true` intercepts every LLM-derived answer before it reaches the form, logs what it would have answered, and aborts the form. The agent collects audit data without submitting anything based on LLM output. Operator reviews the audit log, confirms the picks are sensible, then flips the flag.

**Lesson:** When introducing a non-deterministic component into a pipeline that affects real-world artifacts (applications under your name), audit-only mode is the responsible default. Ship the audit before shipping the action.

---

## Project Status

**Active:** code in this repo reflects the current production state of the agent.

**Limitations and unresolved work:**

- Discovery surface is narrower than ideal. Many career pages render job listings via JavaScript that the plain HTTP crawler doesn't see. A Playwright-based crawler for JS-rendered pages would help but isn't implemented.
- The Reed integration uses Reed's free-tier API which has a 24h freshness delay.
- LinkedIn integration is intentionally throttled (max 10 applications per 24h) because LinkedIn aggressively detects automation. The cap may be too cautious; tuning is empirical.
- The LLM cascade has not yet been promoted to live mode (`dry_run: false`) - audit data is still being collected.
- Cover letter validation catches obvious failures (truncation, signoff stripping issues) but does not check for substance quality. A second LLM pass for quality scoring is on the roadmap.

---

## Repository Structure

```
autonomous-job-agent/
├── agent.py                       # Main orchestration
├── watchdog.py                    # Out-of-band failure monitor
├── core/
│   ├── config.py                  # Config loader, hot-reload
│   ├── llm.py                     # NIM + Anthropic clients
│   ├── role_filter.py             # Title classifier
│   ├── location_filter.py         # Geographic filter
│   ├── fit_scorer.py              # LLM-backed fit scorer
│   ├── policy_answers.py          # Rule dispatcher + LLM cascade
│   ├── cover_letter.py            # Cover letter generation
│   ├── form_filler.py             # Playwright form automation
│   └── notifier.py                # Email + Telegram alerts
├── scrapers/
│   ├── scraper.py                 # JD extraction
│   ├── finder.py                  # Reed + career-page discovery
│   └── company_discovery.py       # Adds new companies to crawl list
├── tools/
│   └── discovery/
│       └── weekly_sweep.py        # Scheduled batch discovery
├── config.yaml.example
├── profile_answers.yaml.example
├── discovered_companies.json      # Seed list of career pages
├── requirements.txt
├── LICENSE
└── README.md
```

---

## License

MIT - see [LICENSE](LICENSE).

---

## Author

Built by [@Iyanuoluwa007](https://github.com/Iyanuoluwa007). Independent Robotics & AI Systems Engineer.

Issues and pull requests welcome. This is an honest portfolio piece - I'm interested in feedback on the architecture, the safety design, and the engineering trade-offs documented above.
