"""
Autonomous Form Filler — Playwright
Detects ATS platform (Greenhouse, Lever, Workday, generic) and fills accordingly.
Zero human interaction. Fully autonomous submit.
"""

import logging, time
log = logging.getLogger("form_filler")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PW_AVAILABLE = True
except ImportError:
    PW_AVAILABLE = False
    log.warning("[FORM] Playwright not installed — run: pip install playwright && playwright install chromium")


# ── ATS-specific selectors ───────────────────────────────────────────────────
ATS_PROFILES = {
    "greenhouse": {
        "name"       : "#first_name, #last_name, input[name='first_name'], input[name='last_name']",
        "email"      : "#email, input[name='email']",
        "phone"      : "#phone, input[name='phone']",
        "resume"     : "input[type='file']",
        "cover"      : "#cover_letter_text, textarea[name='cover_letter']",
        "submit"     : "input[type='submit'], #submit_app",
    },
    "lever": {
        "name"       : "input[name='name'], input[placeholder*='name' i]",
        "email"      : "input[name='email']",
        "phone"      : "input[name='phone']",
        "resume"     : "input[type='file']",
        "cover"      : "textarea[name='comments'], textarea[placeholder*='cover' i]",
        "submit"     : ".submit-app-btn, button[type='submit']",
    },
    "workday": {
        "name"       : "input[data-automation-id='legalNameSection_firstName']",
        "email"      : "input[data-automation-id='email']",
        "phone"      : "input[data-automation-id='phone-number']",
        "resume"     : "input[type='file']",
        "cover"      : "textarea[data-automation-id='coverLetter']",
        "submit"     : "button[data-automation-id='bottomNavigationApplyButton']",
    },
    "generic": {
        "name"       : "input[name*='name' i], input[placeholder*='name' i]",
        "email"      : "input[type='email'], input[name*='email' i]",
        "phone"      : "input[type='tel'], input[name*='phone' i]",
        "resume"     : "input[type='file']",
        "cover"      : "textarea[name*='cover' i], textarea[placeholder*='cover' i], textarea[name*='letter' i], div[contenteditable='true']",
        "submit"     : "button[type='submit'], input[type='submit'], button:text-matches('Apply|Submit', 'i')",
    },
}

def _detect_ats(url: str) -> str:
    url = url.lower()
    if "greenhouse.io"   in url or "boards.greenhouse" in url: return "greenhouse"
    if "jobs.lever.co"   in url or "lever.co"          in url: return "lever"
    if "myworkdayjobs"   in url or "workday.com"        in url: return "workday"
    return "generic"

def _try_fill(page, selector: str, value: str, method="fill") -> bool:
    """Try each selector variant. Returns True only if the intended action ran.

    === PATCH 31A: added click_submit branch; action-gated return ====
    Previous version returned True whenever loc.count() > 0, even for
    unknown methods (silent no-op). This meant method="click_submit"
    was logged as success without any click occurring, producing
    phantom APPLIED entries in the DB.
    Now: the action must execute (fill / upload / click) before True.
    === END PATCH 31A ================================================
    """
    for sel in selector.split(","):
        sel = sel.strip()
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if method == "fill":
                loc.fill(value)
                return True
            elif method == "upload":
                loc.set_input_files(value)
                return True
            elif method == "click_submit":
                loc.scroll_into_view_if_needed(timeout=3000)
                loc.click(timeout=5000)
                return True
            else:
                log.warning(f"[FORM] _try_fill unknown method={method!r} selector={sel!r}")
                return False
        except Exception as e:
            log.debug(f"[FORM] _try_fill miss sel={sel!r} method={method} err={e}")
            continue
    return False


# === PATCH 32C: consent checkbox handler ====================================
# Scans visible checkboxes on the page. For each, tries to extract a label,
# then runs DENY-list first (absolute-never-tick). If not denied, runs
# ALLOW-list (GDPR/privacy/terms). Ticks only on allow-match. Skips on
# anything ambiguous. Every checkbox is logged for post-hoc allow-list
# refinement. Uses .check() (idempotent). Per-checkbox try/except so
# one broken selector cannot break the submission.
# ============================================================================
_CONSENT_DENY_SUBSTRINGS = (
    "sponsorship", "sponsor", "visa", "right to work", "work permit",
    "salary", "compensation", "remunerat", "pay expect", "wage",
    "relocat", "background check", "background verif",
    "criminal", "conviction", "offence", "offense",
    "ethnic", "race", "gender", "sexual", "orientation",
    "disabilit", "veteran", "military service",
    "religion", "religious", "politic",
    "married", "marital", "spouse", "dependent", "children",
    "age range", "date of birth", "born",
    "not a bot", "automated", "robot",
)
_CONSENT_ALLOW_SUBSTRINGS = (
    "i agree to the terms", "i accept the terms",
    "i agree to the privacy", "i accept the privacy",
    "agree to the privacy policy", "accept the privacy policy",
    "agree to the terms and conditions", "accept the terms and conditions",
    "consent to the processing", "consent to processing of",
    "consent to the storage", "consent to storage of",
    "i have read and accept", "i have read and agree",
    "i confirm i have read", "confirm i have read",
    "i acknowledge the privacy", "acknowledge the privacy notice",
    "data processing consent", "gdpr consent",
)

def _checkbox_label_text(page, handle) -> str:
    """Best-effort extraction of a label string for an <input type=checkbox>.
    Order: for-attribute -> wrapping <label> -> aria-labelledby -> aria-label
    -> immediate-following sibling text. Returns empty string on all failure.
    """
    try:
        text = page.evaluate("""(el) => {
          function clean(s) { return (s || '').replace(/\\s+/g, ' ').trim(); }
          // 1. for=id
          if (el.id) {
            const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (l) return clean(l.innerText);
          }
          // 2. wrapping label
          let p = el.closest('label');
          if (p) return clean(p.innerText);
          // 3. aria-labelledby
          const ref = el.getAttribute('aria-labelledby');
          if (ref) {
            const t = ref.split(' ').map(id => {
              const x = document.getElementById(id);
              return x ? x.innerText : '';
            }).join(' ');
            if (t.trim()) return clean(t);
          }
          // 4. aria-label
          const al = el.getAttribute('aria-label');
          if (al) return clean(al);
          // 5. following sibling text
          let sib = el.nextSibling;
          while (sib && sib.nodeType === 3 && !sib.textContent.trim()) sib = sib.nextSibling;
          if (sib && sib.textContent) return clean(sib.textContent);
          return '';
        }""", handle)
        return (text or "").strip()
    except Exception:
        return ""

def _consent_classify(label: str) -> str:
    """Return action: 'tick' | 'skip_deny' | 'skip_unknown'."""
    import re
    if not label:
        return "skip_unknown"
    lower = label.lower()
    # deny-list first
    for phrase in _CONSENT_DENY_SUBSTRINGS:
        if phrase in lower:
            return "skip_deny"
    # deny-list structural: digits, currency, question marks, yes/no answers
    if re.search(r"\d", lower) or any(c in lower for c in ("\u00a3", "$", "\u20ac")):
        return "skip_deny"
    if "?" in lower:
        return "skip_deny"
    if re.search(r"\byes\b|\bno\b", lower):
        return "skip_deny"
    # allow-list
    for phrase in _CONSENT_ALLOW_SUBSTRINGS:
        if phrase in lower:
            return "tick"
    return "skip_unknown"

def _handle_consent_checkboxes(page):
    """Scan visible checkboxes and tick the ones whose label matches the
    narrow consent allow-list. Logs every checkbox action for audit."""
    try:
        locs = page.locator("input[type='checkbox']:not([disabled])")
        count = locs.count()
    except Exception as e:
        log.warning(f"[CONSENT] scan error: {e}")
        return
    if count == 0:
        log.info("[CONSENT] no checkboxes on page")
        return
    ticked = 0
    for i in range(count):
        try:
            box = locs.nth(i)
            if not box.is_visible():
                continue
            handle = box.element_handle()
            if handle is None:
                log.info(f"[CONSENT] idx={i} no-handle skip_unknown")
                continue
            label = _checkbox_label_text(page, handle)
            action = _consent_classify(label)
            label_trunc = (label[:120] + "...") if len(label) > 120 else label
            log.info(f"[CONSENT] idx={i} action={action} label={label_trunc!r}")
            if action == "tick":
                try:
                    box.check(timeout=3000)
                    ticked += 1
                except Exception as e:
                    log.warning(f"[CONSENT] idx={i} tick error: {e}")
        except Exception as e:
            log.warning(f"[CONSENT] idx={i} skipped due to error: {e}")
            continue
    log.info(f"[CONSENT] scan complete — scanned={count} ticked={ticked}")
# === END PATCH 32C ==========================================================


# === PATCH 33B: policy-based dropdown + radio-group handler ================
# Uses core.policy_answers.PolicyAnswers (loaded lazily as a module-level
# singleton) to auto-answer <select> dropdowns and radio groups based on
# profile_answers.yaml. Returns a list of unresolved required questions so
# the caller can abort form submission when policy-sensitive required
# questions have no answer.
# ============================================================================

_POLICY_CACHE = None
_POLICY_YAML_PATH = "/app/profile_answers.yaml"


def _get_policy():
    """Lazy-load PolicyAnswers singleton. On first call, also wires up the
    LLM clients + resume_text so the LLM-fallback path (P35) can fire when
    the policy YAML has llm_fallback_enabled=true.

    === PATCH 35D: wire LLM clients into PolicyAnswers ====================
    Without this wiring, set_llm_clients is never called and even with
    the YAML flag ON, the LLM fallback would silently no-op. Adding the
    wiring here (rather than in __init__) keeps PolicyAnswers itself
    LLM-agnostic and lets us swap clients out for tests.
    =======================================================================
    """
    global _POLICY_CACHE
    if _POLICY_CACHE is not None:
        return _POLICY_CACHE
    try:
        from core.policy_answers import PolicyAnswers
        pa = PolicyAnswers(_POLICY_YAML_PATH)

        # Best-effort LLM client wiring. Failures here log + continue with
        # rule-based-only mode; we never crash the form pipeline because of
        # an LLM-config issue.
        try:
            import yaml as _yaml
            with open("/app/config.yaml") as _cf:
                _cfg = _yaml.safe_load(_cf)
            from core.llm import NIMClient, AnthropicClient

            _nim = None
            _ant = None
            try:
                _nim_key = _cfg.get("nvidia_nim_api_key")
                if _nim_key:
                    _nim = NIMClient(_nim_key, _cfg.get("models", {}))
            except Exception as _e:
                log.warning(f"[POLICY] NIM client init failed: {_e}")
            try:
                _ant_key = _cfg.get("anthropic_api_key")
                if _ant_key and not str(_ant_key).startswith("PLACEHOLDER"):
                    _ant = AnthropicClient(_ant_key)
            except Exception as _e:
                log.warning(f"[POLICY] Anthropic client init failed: {_e}")

            # Load resume text - same source cover_letter.py uses
            _resume_text = ""
            try:
                with open("/app/resume.txt") as _rf:
                    _resume_text = _rf.read()
            except Exception as _e:
                log.warning(f"[POLICY] resume.txt load failed: {_e}")

            pa.set_llm_clients(nim_client=_nim,
                               anthropic_client=_ant,
                               resume_text=_resume_text)
        except Exception as _e:
            log.warning(f"[POLICY] LLM wiring failed (continuing rule-only): {_e}")

        _POLICY_CACHE = pa
        return _POLICY_CACHE
    except Exception as e:
        log.warning(f"[POLICY] failed to load PolicyAnswers: {e}")
        return None
# === END PATCH 35D =====================================================


def _extract_label_for_element(page, handle) -> str:
    """Best-effort label extraction for a form element. Returns '' on failure.

    === PATCH 33B-FIX: radio-group label extraction ===============
    For radio inputs, the wrapping <label> contains the per-option
    text ('Yes'/'No'), NOT the group question. Prefer fieldset
    legend / aria-labelledby / preceding-sibling-text for radios.
    ===============================================================
    """
    try:
        text = page.evaluate("""(el) => {
          function clean(s) { return (s || '').replace(/\\s+/g, ' ').trim(); }
          const is_radio = (el.tagName === 'INPUT' && el.type === 'radio');

          // For radios: prefer fieldset legend BEFORE wrapping label
          if (is_radio) {
            const fs_r = el.closest('fieldset');
            if (fs_r) {
              const leg = fs_r.querySelector('legend');
              if (leg && leg.innerText) return clean(leg.innerText);
            }
            const ref_r = el.getAttribute('aria-labelledby');
            if (ref_r) {
              const t = ref_r.split(' ').map(id => {
                const x = document.getElementById(id);
                return x ? x.innerText : '';
              }).join(' ');
              if (t.trim()) return clean(t);
            }
            // Walk up searching for preceding-sibling text that looks like
            // a group question (ends with ? or *, has some length)
            let walker = el.parentElement;
            while (walker && walker !== document.body) {
              let prev = walker.previousElementSibling;
              while (prev) {
                const txt = clean(prev.innerText || '');
                if (txt.length > 10 && (txt.includes('?') || txt.includes('*'))) {
                  return txt;
                }
                prev = prev.previousElementSibling;
              }
              walker = walker.parentElement;
            }
            return '';
          }

          // Non-radio: for-id -> wrapping label -> aria-labelledby -> aria-label
          // -> fieldset legend -> preceding sibling
          if (el.id) {
            const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (l) return clean(l.innerText);
          }
          let p_wrap = el.closest('label');
          if (p_wrap) return clean(p_wrap.innerText);
          const ref = el.getAttribute('aria-labelledby');
          if (ref) {
            const t = ref.split(' ').map(id => {
              const x = document.getElementById(id);
              return x ? x.innerText : '';
            }).join(' ');
            if (t.trim()) return clean(t);
          }
          const al = el.getAttribute('aria-label');
          if (al) return clean(al);
          let fs = el.closest('fieldset');
          if (fs) {
            const leg = fs.querySelector('legend');
            if (leg) return clean(leg.innerText);
          }
          let sib = el.previousElementSibling;
          while (sib) {
            const t = clean(sib.innerText || '');
            if (t) return t;
            sib = sib.previousElementSibling;
          }
          return '';
        }""", handle)
        return (text or "").strip()
    except Exception:
        return ""


def _is_required(page, handle) -> bool:
    try:
        req = page.evaluate("""(el) => {
          if (el.required) return true;
          if (el.hasAttribute('aria-required') && el.getAttribute('aria-required') === 'true') return true;
          if (el.id) {
            const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (l && l.innerText && l.innerText.includes('*')) return true;
          }
          let p = el.closest('label');
          if (p && p.innerText && p.innerText.includes('*')) return true;
          let fs = el.closest('fieldset');
          if (fs) {
            const leg = fs.querySelector('legend');
            if (leg && leg.innerText && leg.innerText.includes('*')) return true;
          }
          return false;
        }""", handle)
        return bool(req)
    except Exception:
        return False


def _handle_policy_dropdowns(page, role_ctx=None):
    unresolved_required = []
    pa = _get_policy()
    if pa is None:
        log.warning("[POLICY] PolicyAnswers not available - skipping")
        return unresolved_required

    try:
        selects = page.locator("select:not([disabled])")
        n_selects = selects.count()
    except Exception as e:
        log.warning(f"[POLICY] select scan error: {e}")
        n_selects = 0

    log.info(f"[POLICY] scanning {n_selects} <select> dropdowns")
    for i in range(n_selects):
        try:
            el = selects.nth(i)
            if not el.is_visible():
                continue
            handle = el.element_handle()
            if handle is None:
                continue
            label = _extract_label_for_element(page, handle)
            required = _is_required(page, handle)
            options_text = []
            try:
                options_text = page.evaluate("""(el) => {
                  return Array.from(el.options)
                    .filter(o => o.value !== '' && !/^select/i.test(o.text.trim()))
                    .map(o => o.text.trim());
                }""", handle)
            except Exception:
                options_text = []

            if not label:
                log.info(f"[POLICY] select idx={i} skip_no_label required={required} opts={options_text[:5]}")
                if required:
                    unresolved_required.append({"kind":"select","label":"(no label)","reason":"no label found","options":options_text})
                continue

            action = pa.answer_question(label, options_text, role_ctx or {})
            a_type = action.get("action")
            label_trunc = label[:100] + ("..." if len(label) > 100 else "")
            log.info(f"[POLICY] select idx={i} required={required} action={a_type} rule={action.get('rule','')} label={label_trunc!r}")

            if a_type == "pick":
                try:
                    el.select_option(label=action["option"])
                except Exception as e:
                    log.warning(f"[POLICY] select_option failed: {e}")
                    if required:
                        unresolved_required.append({"kind":"select","label":label,"reason":f"select_option failed: {e}","options":options_text})
            elif a_type in ("skip_unknown","skip_no_option_match","skip_deny","error"):
                if required:
                    unresolved_required.append({"kind":"select","label":label,"reason":f"{a_type}: {action.get('reason','')}","options":options_text})
        except Exception as e:
            log.warning(f"[POLICY] select idx={i} unexpected error: {e}")
            continue

    try:
        groups_map = page.evaluate("""() => {
          const radios = Array.from(document.querySelectorAll("input[type='radio']:not([disabled])"));
          const groups = {};
          for (const r of radios) {
            if (r.offsetParent === null) continue;
            const nm = r.name || r.id || '';
            if (!nm) continue;
            if (!groups[nm]) groups[nm] = [];
            groups[nm].push({
              value: r.value,
              labelText: (function(){
                if (r.id) {
                  const l = document.querySelector('label[for="' + CSS.escape(r.id) + '"]');
                  if (l) return l.innerText.trim();
                }
                const p = r.closest('label');
                if (p) return p.innerText.trim();
                const sib = r.nextSibling;
                if (sib && sib.textContent) return sib.textContent.trim();
                return r.value;
              })(),
            });
          }
          return groups;
        }""")
    except Exception as e:
        log.warning(f"[POLICY] radio group scan error: {e}")
        groups_map = {}

    log.info(f"[POLICY] scanning {len(groups_map or {})} radio groups")
    for group_name, group_entries in (groups_map or {}).items():
        try:
            if not group_entries:
                continue
            first_radio_sel = f"input[type='radio'][name='{group_name}']"
            try:
                first_loc = page.locator(first_radio_sel).first
                first_handle = first_loc.element_handle()
                group_label = _extract_label_for_element(page, first_handle) if first_handle else ""
                required = _is_required(page, first_handle) if first_handle else False
            except Exception:
                group_label = ""
                required = False

            options_text = [e["labelText"] for e in group_entries if e.get("labelText")]

            if not group_label:
                log.info(f"[POLICY] radio group={group_name!r} skip_no_label required={required} opts={options_text}")
                if required:
                    unresolved_required.append({"kind":"radio","label":f"(no label, name={group_name})","reason":"no group label found","options":options_text})
                continue

            action = pa.answer_question(group_label, options_text, role_ctx or {})
            a_type = action.get("action")
            label_trunc = group_label[:100] + ("..." if len(group_label) > 100 else "")
            log.info(f"[POLICY] radio group={group_name!r} required={required} action={a_type} rule={action.get('rule','')} label={label_trunc!r}")

            if a_type == "pick":
                chosen_text = action["option"]
                target_value = None
                for entry in group_entries:
                    if entry["labelText"].strip().lower() == chosen_text.strip().lower():
                        target_value = entry["value"]
                        break
                if target_value is None:
                    log.warning(f"[POLICY] radio group={group_name} no value matched chosen_text={chosen_text!r}")
                    if required:
                        unresolved_required.append({"kind":"radio","label":group_label,"reason":f"no value matched chosen option {chosen_text!r}","options":options_text})
                    continue
                try:
                    sel = f"input[type='radio'][name='{group_name}'][value='{target_value}']"
                    page.locator(sel).first.check(timeout=3000)
                except Exception as e:
                    log.warning(f"[POLICY] radio check failed: {e}")
                    if required:
                        unresolved_required.append({"kind":"radio","label":group_label,"reason":f"check failed: {e}","options":options_text})
            elif a_type in ("skip_unknown","skip_no_option_match","skip_deny","error"):
                if required:
                    unresolved_required.append({"kind":"radio","label":group_label,"reason":f"{a_type}: {action.get('reason','')}","options":options_text})
        except Exception as e:
            log.warning(f"[POLICY] radio group={group_name} unexpected error: {e}")
            continue

    log.info(f"[POLICY] scan complete selects={n_selects} radio_groups={len(groups_map or {})} unresolved_required={len(unresolved_required)}")
    return unresolved_required
# === END PATCH 33B ==========================================================



class FormFiller:
    def __init__(self, profile: dict):
        self.profile = profile

    def apply(self, url: str, cover_letter: str, resume_path: str, role_ctx: dict = None) -> bool:
        if not PW_AVAILABLE:
            log.error("[FORM] Playwright unavailable.")
            return False

        ats  = _detect_ats(url)
        sels = ATS_PROFILES[ats]
        log.info(f"[FORM] ATS={ats}  url={url}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx     = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = ctx.new_page()

            try:
                page.goto(url, timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=15_000)

                p_name  = self.profile
                first   = p_name.get("first_name") or p_name.get("full_name","").split()[0]
                last    = p_name.get("last_name")  or " ".join(p_name.get("full_name","").split()[1:])

                # === PATCH 31D: name-fill priority fix =====================
                # Original logic tried combined full_name first. _try_fill
                # stops at the first matching selector - so on forms with
                # #first_name and #last_name, "Firstname Middlename Lastname" was
                # stuffed into #first_name alone, #last_name stayed empty,
                # and HTML5 validation blocked submit.
                # New order: try SPLIT fields first with correct values.
                # Only if no split field exists, fall back to combined.
                # ===========================================================
                split_first = _try_fill(
                    page,
                    "#first_name, input[name='first_name'], input[id*='first' i]",
                    first,
                )
                split_last = _try_fill(
                    page,
                    "#last_name, input[name='last_name'], input[id*='last' i]",
                    last,
                )
                if not (split_first or split_last):
                    _try_fill(
                        page,
                        "input[name='name'], input[placeholder*='name' i], input[name*='full' i]",
                        p_name.get("full_name",""),
                    )
                # === END PATCH 31D ==========================================

                _try_fill(page, sels["email"], p_name.get("email",""))
                _try_fill(page, sels["phone"], p_name.get("phone",""))

                # === PATCH 32B: LinkedIn / website label-based fallback ======
                # Original selectors only matched attribute-based fields. Many
                # Greenhouse/Lever forms render LinkedIn as "Question 42" style
                # fields with label-based identification (id='question_4000_abc',
                # aria-label='LinkedIn Profile'). This block tries attribute
                # matches first, then aria-label/id fuzzy, then Playwright's
                # get_by_label. Values come from the profile config only -
                # no invention.
                # =============================================================
                _linkedin_value = p_name.get("linkedin", "")
                _website_value  = p_name.get("website", "")
                _linkedin_selectors = [
                    "input[name*='linkedin' i]",
                    "input[placeholder*='linkedin' i]",
                    "input[aria-label*='linkedin' i]",
                    "input[id*='linkedin' i]",
                ]
                _website_selectors = [
                    "input[name*='website' i]",
                    "input[placeholder*='website' i]",
                    "input[aria-label*='website' i]",
                    "input[id*='website' i]",
                    "input[name*='portfolio' i]",
                    "input[aria-label*='portfolio' i]",
                ]
                # LinkedIn
                _linkedin_filled = False
                if _linkedin_value:
                    for _sel in _linkedin_selectors:
                        if _try_fill(page, _sel, _linkedin_value):
                            _linkedin_filled = True
                            break
                    if not _linkedin_filled:
                        for _lbl in ("LinkedIn Profile", "LinkedIn URL", "LinkedIn"):
                            try:
                                _loc = page.get_by_label(_lbl, exact=False).first
                                if _loc.count() > 0:
                                    _loc.fill(_linkedin_value)
                                    _linkedin_filled = True
                                    break
                            except Exception:
                                continue
                # Website / portfolio
                _website_filled = False
                if _website_value:
                    for _sel in _website_selectors:
                        if _try_fill(page, _sel, _website_value):
                            _website_filled = True
                            break
                    if not _website_filled:
                        for _lbl in ("Website", "Portfolio", "Personal Website", "Personal site"):
                            try:
                                _loc = page.get_by_label(_lbl, exact=False).first
                                if _loc.count() > 0:
                                    _loc.fill(_website_value)
                                    _website_filled = True
                                    break
                            except Exception:
                                continue
                log.info(f"[FORM] linkedin_filled={_linkedin_filled} website_filled={_website_filled}")
                # === END PATCH 32B ===========================================

                # Resume upload
                if resume_path:
                    _try_fill(page, sels["resume"], resume_path, method="upload")

                # Cover letter
                if cover_letter:
                    filled_cover = _try_fill(page, sels["cover"], cover_letter)
                    # === PATCH 32A: cover letter fallback ========================
                    # Many modern ATS forms (Greenhouse especially) don't use
                    # name='cover_letter'; they expose 'Additional Information'
                    # or 'Why [company]' textareas. Without this fallback, the
                    # devstral-generated cover letter gets silently dropped.
                    # Fallback selectors below are tried in order, covering:
                    #  - name/id/aria-label attribute fuzzy matches
                    #  - Playwright label-based lookup for common phrases
                    # Content is ALWAYS the already-generated cover letter.
                    # We never modify or generate new text here.
                    # =============================================================
                    if not filled_cover:
                        _cover_fallback_selectors = [
                            "textarea[name*='additional' i]",
                            "textarea[id*='additional' i]",
                            "textarea[aria-label*='additional' i]",
                            "textarea[name*='comment' i]",
                            "textarea[id*='comment' i]",
                            "textarea[aria-label*='comment' i]",
                            "textarea[aria-label*='cover' i]",
                            "textarea[name*='note' i]",
                            "textarea[name*='message' i]",
                            "textarea[aria-label*='tell us' i]",
                            "textarea[aria-label*='why' i]",
                        ]
                        fb_hit = None
                        for fb_sel in _cover_fallback_selectors:
                            if _try_fill(page, fb_sel, cover_letter):
                                fb_hit = fb_sel
                                filled_cover = True
                                break
                        # Label-based lookup (Playwright get_by_label)
                        if not filled_cover:
                            _cover_labels = [
                                "Additional Information",
                                "Additional information",
                                "Cover Letter",
                                "Cover letter",
                                "Tell us about yourself",
                                "Why are you interested",
                                "Why this role",
                            ]
                            for lbl in _cover_labels:
                                try:
                                    loc = page.get_by_label(lbl, exact=False).first
                                    if loc.count() > 0:
                                        loc.fill(cover_letter)
                                        fb_hit = f"label:{lbl}"
                                        filled_cover = True
                                        break
                                except Exception:
                                    continue
                        if filled_cover:
                            log.info(f"[FORM] cover letter filled via fallback: {fb_hit}")
                        else:
                            log.info("[FORM] no cover letter target found (primary + fallbacks missed)")
                    # === END PATCH 32A ===========================================

                # Small pause for JS validation to settle
                time.sleep(1.5)

                # === PATCH 32C: handle consent checkboxes before submit =======
                # Scans all visible checkboxes, ticks only those matching the
                # narrow consent allow-list. Deny-list of sponsorship/visa/
                # salary/etc. checked first. All actions logged.
                # =============================================================
                try:
                    _handle_consent_checkboxes(page)
                except Exception as _e:
                    log.warning(f"[CONSENT] handler exception: {_e}")
                # === END PATCH 32C ============================================

                # === PATCH 33B: policy-based dropdown + radio-group handling ==
                # Auto-answer <select> dropdowns and radio groups from
                # profile_answers.yaml via the PolicyAnswers dispatcher.
                # If any REQUIRED question is unresolved (no rule match or
                # no option match), abort submission rather than submit
                # a partial form - a partial submit would generate a
                # validation error and waste the cover-letter token spend.
                # ================================================================
                try:
                    unresolved = _handle_policy_dropdowns(page, role_ctx=role_ctx)
                except Exception as _e:
                    log.warning(f"[POLICY] handler exception: {_e}")
                    unresolved = []
                if unresolved:
                    for u in unresolved:
                        log.info(
                            f"[POLICY] UNRESOLVED REQUIRED: kind={u['kind']} "
                            f"reason={u['reason']} label={u['label'][:100]!r} "
                            f"options={u['options']}"
                        )
                    log.info(
                        f"[FORM] FORM_SKIP_POLICY - {len(unresolved)} required "
                        f"questions unresolved; aborting submission for {url}"
                    )
                    return False
                # === END PATCH 33B ============================================

                # === PATCH 31A: real submit + verification ====================
                # Previous logic treated button detection as success and
                # returned True on (submitted OR confirmed). Combined with
                # the _try_fill bug this produced 428/439 phantom APPLIED
                # entries. Now: capture pre-submit URL, click, wait 5s,
                # require POSITIVE evidence (URL change OR confirmation
                # text) before returning True.
                # =============================================================
                pre_submit_url = page.url
                submitted = _try_fill(page, sels["submit"], "", method="click_submit")
                if not submitted:
                    log.info(f"[FORM] Submit click failed - no matching button ATS={ats}")
                    return False

                # Forms need time: client-side validation, XHR round-trip,
                # redirect to confirmation page. 5s is conservative.
                time.sleep(5)

                try:
                    post_submit_url = page.url
                except Exception:
                    post_submit_url = pre_submit_url
                url_changed = (post_submit_url != pre_submit_url)

                # Confirmation-text heuristic - expanded to cover
                # Greenhouse / Lever / Workday / Ashby / generic replies.
                try:
                    body = page.inner_text("body").lower()
                except Exception:
                    body = ""
                confirmation_phrases = [
                    "application submitted",
                    "thank you for applying",
                    "thanks for applying",
                    "we received your",
                    "we've received your",
                    "successfully applied",
                    "your application has been",
                    "application complete",
                    "application received",
                    "application sent",
                    "you've applied",
                    "we'll be in touch",
                    "we will be in touch",
                    "next steps",
                    "thank you for your interest",
                    "thank you for your submission",
                ]
                confirmed = any(w in body for w in confirmation_phrases)
                success = submitted and (url_changed or confirmed)

                log.info(
                    f"[FORM] Submitted={submitted}  URLChanged={url_changed}  "
                    f"Confirmed={confirmed}  Success={success}  "
                    f"pre={pre_submit_url[:80]}  post={post_submit_url[:80]}"
                )
                return success
                # === END PATCH 31A ============================================

            except PWTimeout:
                log.warning(f"[FORM] Timeout on {url}")
                return False
            except Exception as e:
                log.error(f"[FORM] Error: {e}")
                return False
            finally:
                browser.close()
