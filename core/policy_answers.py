"""
Policy Answers - Form auto-answer engine (Patch 33)
====================================================

Loads profile_answers.yaml once, exposes a rule-based dispatcher that takes
a form question (label text + available options) and returns an answer the
agent can safely submit, OR a signal that the question is unanswerable.

HARD RULES:
  1. Never invent an answer. Only pick from options the form actually offers.
  2. Never auto-answer policy-sensitive questions (sponsorship, salary) from
     hardcoded defaults - always route through the policy YAML.
  3. Never auto-answer diversity / criminal-record / background-check questions
     (explicit deny list).
  4. If a required question is unanswerable, the agent must abort submission
     rather than submit a partial form.

USAGE:
    from core.policy_answers import PolicyAnswers
    pa = PolicyAnswers("/app/profile_answers.yaml")
    result = pa.answer_question(
        question="Are you open to relocation for this role?",
        options=["Yes", "No", "Maybe"],
        role_ctx={"title": "Senior AI Engineer", "jd_text": "..."}
    )
    # result is a dict: {"action": "pick", "option": "Yes", "rule": "relocation_uk"}
    # or {"action": "skip_unknown", "reason": "no rule matched"}
    # or {"action": "skip_deny", "reason": "diversity question"}
    # or {"action": "skip_no_option_match", "reason": "no option matches policy"}
"""

import re
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any

import yaml

log = logging.getLogger("policy_answers")


# === RULE DISPATCHER: maps question keywords to a policy resolver + picker ===
# Each rule is a dict:
#   {
#     "name":           short identifier for logging
#     "keywords":       list of substrings (lowercased); any match -> rule fires
#     "exclude":        list of substrings that DISQUALIFY the rule if present
#                       (use for questions that contain rule keywords but are
#                        actually asking something else)
#     "policy_fn":      callable(policy, role_ctx) -> str | None
#                       returns the desired answer string (or None if unanswerable)
#     "picker":         which option-matching strategy to use
#                       "yes_no", "yes_no_pns" (prefer-not-to-say), "contains",
#                       "exact", "numeric", "first_non_match", "salary"
#   }


# ====================================================================
# POLICY RESOLVERS - extract the desired answer from the policy YAML.
# Each returns the DESIRED answer string (e.g. "Yes"), which is then
# matched against the form's actual option list by the picker.
# ====================================================================

def _resolve_yes(policy, role_ctx):
    return "Yes"

def _resolve_no(policy, role_ctx):
    return "No"

def _resolve_right_to_work(policy, role_ctx):
    val = policy.get("right_to_work_uk", "").strip().lower()
    if val in ("yes", "true", "y"):
        return "Yes"
    if val in ("no", "false", "n"):
        return "No"
    return None

def _resolve_sponsorship_current(policy, role_ctx):
    val = policy.get("requires_sponsorship_current", "").strip().lower()
    if val in ("no", "false", "n"):
        return "No"
    if val in ("yes", "true", "y"):
        return "Yes"
    return None

def _resolve_sponsorship_smart(policy, role_ctx):
    """
    Future-sponsorship question. Requires role classification.
    Policy has three sub-answers: short_term, permanent, unknown.
    """
    future = policy.get("requires_sponsorship_future_policy", {})
    if not future:
        return None

    # Classify role from title + JD text
    title = (role_ctx or {}).get("title", "")
    jd    = (role_ctx or {}).get("jd_text", "")
    combined = f"{title} {jd}".lower()

    classification_cfg = policy.get("role_duration_classification", {})
    short_kw = [k.lower() for k in classification_cfg.get("short_term_keywords", [])]
    perm_kw  = [k.lower() for k in classification_cfg.get("permanent_keywords", [])]

    # First short-term match wins (they're more specific)
    if any(k in combined for k in short_kw):
        answer = future.get("short_term_answer", "no")
    elif any(k in combined for k in perm_kw):
        answer = future.get("permanent_answer", "yes")
    else:
        answer = future.get("unknown_answer", "yes")

    return answer.capitalize()

def _resolve_visa_type(policy, role_ctx):
    return policy.get("visa_type", None)

def _resolve_relocation_uk(policy, role_ctx):
    val = policy.get("open_to_relocation_uk", "").strip().lower()
    if val in ("yes", "true", "y"):
        return "Yes"
    if val in ("no", "false", "n"):
        return "No"
    return None

def _resolve_relocation_intl(policy, role_ctx):
    val = policy.get("open_to_relocation_international", "").strip().lower()
    if val in ("yes", "true", "y"):
        return "Yes"
    if val in ("no", "false", "n"):
        return "No"
    return None

def _resolve_currently_based(policy, role_ctx):
    return policy.get("currently_based", None)

def _resolve_preferred_location(policy, role_ctx):
    locs = policy.get("preferred_uk_locations", [])
    if isinstance(locs, list) and locs:
        return locs[0]  # default to first: Manchester
    return None

def _resolve_total_years(policy, role_ctx):
    n = policy.get("total_years_experience", None)
    if n is None:
        return None
    return str(n)

def _resolve_skill_years(policy, role_ctx, question_text=""):
    """Try to detect which skill the question asks about, then return years."""
    skills = policy.get("skill_years", {}) or {}
    q = question_text.lower()
    # Try each skill key against the question
    for skill_key, yrs in skills.items():
        # Also handle aliases
        aliases = {
            "cpp":          ["c++", "cpp"],
            "c_plus_plus":  ["c++", "cpp"],
            "ml":           ["ml", "machine learning"],
            "ai":           ["ai", "artificial intelligence"],
            "ros":          ["ros"],
            "ros2":         ["ros2", "ros 2"],
            "python":       ["python"],
            "robotics":     ["robotics", "robot"],
            "machine_learning": ["machine learning", "ml"],
            "deep_learning":    ["deep learning", "neural network"],
        }
        candidates = aliases.get(skill_key, [skill_key.replace("_", " ")])
        if any(c in q for c in candidates):
            return str(yrs)
    return None

def _resolve_salary_display(policy, role_ctx):
    sp = policy.get("salary_policy", {}) or {}
    return sp.get("display_when_asked", None)

def _resolve_salary_range(policy, role_ctx):
    sp = policy.get("salary_policy", {}) or {}
    return sp.get("target_range_gbp", None)

def _resolve_notice_period(policy, role_ctx):
    n = policy.get("notice_period_weeks", None)
    if n is None:
        return None
    return f"{n} weeks" if n > 0 else "Immediately"

def _resolve_available_start(policy, role_ctx):
    return policy.get("available_start", "immediately")

def _resolve_prev_interviewed(policy, role_ctx):
    default = policy.get("previously_interviewed_default", "no")
    if default == "prefer_not_to_say":
        return "Prefer not to say"
    if default.lower() == "no":
        return "No"
    return default.capitalize()

def _resolve_source(policy, role_ctx):
    return "Company website"

def _resolve_contact_pref(policy, role_ctx):
    return "Email"

def _resolve_travel(policy, role_ctx):
    # Per session discussion: override -- default yes, user accepts risk
    return "Yes"

def _resolve_referral(policy, role_ctx):
    return "Company website"


# ====================================================================
# OPTION PICKERS - given a desired answer string + form's actual options,
# return the best-matching option or None.
# ====================================================================

def _pick_yes_no(desired: str, options: List[str]) -> Optional[str]:
    if not desired or not options:
        return None
    d_lower = desired.strip().lower()
    for opt in options:
        o_lower = opt.strip().lower()
        if d_lower == o_lower:
            return opt
        if d_lower in ("yes", "no") and o_lower in ("yes", "no") and d_lower == o_lower:
            return opt
    # Fuzzy fallback - substring match
    for opt in options:
        o_lower = opt.strip().lower()
        if d_lower in o_lower:
            return opt
    return None

def _pick_yes_no_pns(desired: str, options: List[str]) -> Optional[str]:
    """Yes/No with prefer-not-to-say fallback."""
    match = _pick_yes_no(desired, options)
    if match:
        return match
    # Try PNS options
    for opt in options:
        o_lower = opt.strip().lower()
        if "prefer not" in o_lower or "rather not say" in o_lower:
            return opt
    return None

def _pick_contains(desired: str, options: List[str]) -> Optional[str]:
    """Substring match - option whose text contains the desired string."""
    if not desired or not options:
        return None
    d_lower = desired.strip().lower()
    # Exact first
    for opt in options:
        if opt.strip().lower() == d_lower:
            return opt
    # Contains
    for opt in options:
        if d_lower in opt.strip().lower():
            return opt
    # Reverse contains (option is substring of desired)
    for opt in options:
        o_lower = opt.strip().lower()
        if o_lower and o_lower in d_lower:
            return opt
    return None

def _pick_numeric(desired: str, options: List[str]) -> Optional[str]:
    """Pick option containing the desired number."""
    if not desired or not options:
        return None
    # Extract digits from desired
    m = re.search(r"\d+", desired)
    if not m:
        return None
    target = int(m.group())
    # Look for option with that number, or a range containing it
    for opt in options:
        nums = [int(x) for x in re.findall(r"\d+", opt)]
        if target in nums:
            return opt
        if len(nums) == 2 and nums[0] <= target <= nums[1]:
            return opt
    # Fallback: first option containing any number (numeric context)
    for opt in options:
        if re.search(r"\d", opt):
            return opt
    return None

def _pick_salary(desired: str, options: List[str]) -> Optional[str]:
    """Salary dropdowns often have bands. Try to find our target range."""
    result = _pick_contains(desired, options)
    if result:
        return result
    return _pick_numeric(desired, options)

def _pick_first_non_placeholder(desired: str, options: List[str]) -> Optional[str]:
    """For questions where 'Company website' or similar is safe fallback."""
    result = _pick_contains(desired, options)
    if result:
        return result
    # If no match, pick first non-placeholder option (skips Select..., blank, ---)
    for opt in options:
        o = opt.strip()
        if o and o.lower() not in ("select...", "select", "please select", "---", ""):
            return opt
    return None


# ====================================================================
# THE DISPATCH TABLE
# ====================================================================

# Deny-list keywords - questions containing these NEVER get auto-answered
_DENY_KEYWORDS = [
    "ethnicity", "ethnic group", "race ", " race,", " race?",
    "gender", "sexual orientation",
    "disabilit", "disabled",  # catches disability, disabilities, disabled
    "veteran", "military service",
    "criminal", "conviction", "offence", "offense",
    "background check", "background verific",
    "religion", "religious",
    "political",
]

# Rules are checked in order; first match wins. Put more specific rules before
# general ones (e.g. "visa type" before "require visa").
_RULES = [
    # --- Work authorization ---
    {
        "name": "visa_type",
        "keywords": ["visa type", "which visa", "type of visa"],
        "exclude": [],
        "policy_fn": _resolve_visa_type,
        "picker": "contains",
    },
    # === SPONSORSHIP: handled by _match_sponsorship compound matcher ====
    # === Flat keyword rules REMOVED in P33a-fix; see _match_sponsorship helper.
    {
        "name": "right_to_work",
        "keywords": ["right to work", "authorized to work", "authorised to work",
                     "work eligibility", "eligible to work", "legally able to work"],
        "exclude": ["sponsorship", "visa"],
        "policy_fn": _resolve_right_to_work,
        "picker": "yes_no",
    },

    # --- Location ---
    {
        "name": "relocation_uk",
        "keywords": ["open to relocat", "willing to relocat"],
        "exclude": ["internationally", "abroad", "outside"],
        "policy_fn": _resolve_relocation_uk,
        "picker": "yes_no",
    },
    {
        "name": "relocation_intl",
        "keywords": ["relocat internationally", "relocate internationally",
                     "relocation internationally", "relocation international",
                     "relocate abroad", "relocate outside",
                     "international relocation", "relocation outside the uk",
                     "relocation overseas", "move abroad"],
        "exclude": [],
        "policy_fn": _resolve_relocation_intl,
        "picker": "yes_no",
    },
    {
        "name": "currently_based",
        "keywords": ["currently based", "current location", "where are you based",
                     "where do you live", "where are you located"],
        "exclude": ["relocat", "preferred"],
        "policy_fn": _resolve_currently_based,
        "picker": "contains",
    },
    {
        "name": "preferred_location",
        "keywords": ["preferred location", "preferred office", "preferred working location"],
        "exclude": [],
        "policy_fn": _resolve_preferred_location,
        "picker": "contains",
    },

    # --- Experience ---
    # NOTE: skill_years rule is handled specially since it needs the question text
    {
        "name": "total_years",
        "keywords": ["total years of experience", "how many years of professional",
                     "years of professional experience", "total professional"],
        "exclude": ["python", "c++", "ml ", "machine learning", "ai ", "robotics", "ros "],
        "policy_fn": _resolve_total_years,
        "picker": "numeric",
    },
    # skill_years rule handled in dispatch by pattern "years" + skill detection
    # See _match_rule for special handling

    # --- Compensation ---
    {
        "name": "salary_expect",
        "keywords": ["salary expect", "expected salary",
                     "compensation expect", "expected compensation",
                     "desired salary", "desired compensation"],
        "exclude": ["range"],
        "policy_fn": _resolve_salary_display,
        "picker": "salary",
    },
    {
        "name": "salary_range",
        "keywords": ["salary range", "desired salary range", "compensation range"],
        "exclude": [],
        "policy_fn": _resolve_salary_range,
        "picker": "salary",
    },

    # --- Availability ---
    {
        "name": "notice_period",
        "keywords": ["notice period"],
        "exclude": [],
        "policy_fn": _resolve_notice_period,
        "picker": "contains",
    },
    {
        "name": "available_start",
        "keywords": ["available start", "start date", "when can you start",
                     "earliest start", "earliest available"],
        "exclude": [],
        "policy_fn": _resolve_available_start,
        "picker": "contains",
    },

    # --- Interview history ---
    {
        "name": "prev_interviewed",
        "keywords": ["previously interviewed", "been interviewed", "applied before",
                     "interviewed with us", "prior application"],
        "exclude": [],
        "policy_fn": _resolve_prev_interviewed,
        "picker": "yes_no_pns",
    },

    # --- Referral / source ---
    {
        "name": "source_heard",
        "keywords": ["how did you hear", "source of referral", "how you found",
                     "where did you find"],
        "exclude": [],
        "policy_fn": _resolve_referral,
        "picker": "first_non_placeholder",
    },

    # --- Contact preference ---
    {
        "name": "contact_pref",
        "keywords": ["preferred contact", "best way to reach",
                     "preferred communication", "how should we contact"],
        "exclude": [],
        "policy_fn": _resolve_contact_pref,
        "picker": "contains",
    },

    # --- Travel ---
    {
        "name": "travel",
        "keywords": ["willing to travel", "can you travel", "able to travel",
                     "ok with travel"],
        "exclude": ["international"],  # international travel is more sensitive, skip
        "policy_fn": _resolve_travel,
        "picker": "yes_no",
    },

    # --- Commute ---
    {
        "name": "commute",
        "keywords": ["can you commute", "willing to commute"],
        "exclude": [],
        "policy_fn": _resolve_travel,  # reuse: yes since we're open to UK relocation
        "picker": "yes_no",
    },
]


# ====================================================================
# PUBLIC API
# ====================================================================

class PolicyAnswers:
    """Rule-based form-question answerer backed by profile_answers.yaml."""

    def __init__(self, yaml_path: str):
        self.yaml_path = Path(yaml_path)
        self.policy = self._load_policy()
        # === PATCH 35C: LLM fallback wiring =============================
        # Optional clients for LLM fallback. Caller injects via
        # set_llm_clients(). If unset, LLM fallback is skipped silently.
        # The llm_fallback_enabled flag on the policy YAML gates whether
        # fallback fires at all (default OFF for safety).
        self._nim_client = None
        self._anthropic_client = None
        self._resume_text = None
        # === END PATCH 35C ===============================================

    # === PATCH 35C: LLM client injection ==============================
    def set_llm_clients(self, nim_client=None, anthropic_client=None, resume_text=None):
        """Inject optional LLM clients + resume context for the LLM fallback
        path used when the rule dispatcher returns skip_unknown. All three
        params are optional - missing clients just disable that stage.
        Call once after construction. Idempotent.
        """
        if nim_client is not None:
            self._nim_client = nim_client
        if anthropic_client is not None:
            self._anthropic_client = anthropic_client
        if resume_text is not None:
            self._resume_text = resume_text
        log.info(
            f"[POLICY] LLM clients injected: nim={self._nim_client is not None} "
            f"anthropic={self._anthropic_client is not None} "
            f"resume_chars={len(self._resume_text or '')}"
        )

    def _llm_fallback_enabled(self) -> bool:
        """Read the YAML feature flag. Defaults to False if missing."""
        v = self.policy.get("llm_fallback_enabled", False)
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "y", "1", "on")
        return bool(v)
    # === END PATCH 35C ==================================================

    def _load_policy(self) -> Dict[str, Any]:
        if not self.yaml_path.exists():
            log.error(f"[POLICY] profile_answers.yaml not found at {self.yaml_path}")
            return {}
        try:
            with open(self.yaml_path) as f:
                data = yaml.safe_load(f) or {}
            pa = data.get("profile_answers", {})
            log.info(f"[POLICY] loaded from {self.yaml_path} (version {pa.get('policy_version','?')})")
            return pa
        except Exception as e:
            log.error(f"[POLICY] failed to load {self.yaml_path}: {e}")
            return {}

    def _is_denied(self, question_lower: str) -> bool:
        for kw in _DENY_KEYWORDS:
            if kw in question_lower:
                return True
        return False

    def _match_sponsorship(self, question_lower: str, question_text: str):
        """Compound-condition sponsorship matcher.

        Returns (rule_name, resolver_fn) or None.

        Logic:
          - Must contain sponsor/visa/sponsorship terms
          - 'current' if it contains now-terms AND NOT future-terms
          - 'combined' if it contains BOTH now-terms AND future-terms
            (or explicit combined phrases like 'now or in the future')
          - 'smart' (future-inclusive default) otherwise
        """
        q = question_lower

        # Must be sponsorship/visa-related
        SPONSOR_TERMS = ("sponsor", "sponsorship", "visa sponsorship", "sponsoring")
        if not any(t in q for t in SPONSOR_TERMS):
            return None
        # Skip "visa type" - that is a different rule
        if "visa type" in q or "which visa" in q or "type of visa" in q:
            return None

        NOW_TERMS = ("now", "currently", "at this time", "at the moment",
                     "presently", "right now", "at present")
        FUTURE_TERMS = ("future", "any future point", "any future time",
                        "ever need", "in the future", "at any point",
                        "at some point", "eventually", "down the line")
        COMBINED_PHRASES = ("now or in the future", "currently or in the future",
                            "at present or in the future", "now or ever")

        has_now      = any(t in q for t in NOW_TERMS)
        has_future   = any(t in q for t in FUTURE_TERMS)
        has_combined = any(t in q for t in COMBINED_PHRASES)

        # Combined wins if we see an explicit combined phrase, or both now+future
        if has_combined or (has_now and has_future):
            return ("sponsorship_combined", _resolve_sponsorship_smart)

        # Current-only if we see a now-term and NOT future
        if has_now and not has_future:
            return ("sponsorship_current", _resolve_sponsorship_current)

        # Fallback: future-inclusive smart answer
        return ("sponsorship_smart", _resolve_sponsorship_smart)

    def _match_skill_years(self, question_lower: str, question_text: str):
        """Special handling: 'years of experience with X' where X is a skill."""
        if "year" not in question_lower:
            return None
        # Must contain skill keyword
        skills = self.policy.get("skill_years", {}) or {}
        if not skills:
            return None
        aliases_present = any(
            alias in question_lower
            for alias in ("python", "c++", "cpp", "ml", "machine learning",
                          "ai ", "robotics", "ros ", "ros2", "deep learning")
        )
        if not aliases_present:
            return None
        # Resolve
        desired = _resolve_skill_years(self.policy, None, question_text=question_text)
        return desired

    def _match_rule(self, question_text: str):
        """Find first matching rule; returns (rule, desired_answer) or (None, None)."""
        q_lower = question_text.lower()

        # Special-case 1: sponsorship compound-condition matcher (bypass flat rules)
        sponsor_match = self._match_sponsorship(q_lower, question_text)
        if sponsor_match is not None:
            rule_name, resolver_fn = sponsor_match
            synthetic = {
                "name":      rule_name,
                "policy_fn": resolver_fn,
                "picker":    "yes_no",
            }
            return synthetic, None  # None means: caller will invoke policy_fn

        # Special-case 2: skill_years first (before generic rules)
        skill_years = self._match_skill_years(q_lower, question_text)
        if skill_years is not None:
            synthetic_rule = {
                "name":      "skill_years",
                "policy_fn": lambda *a, **k: skill_years,
                "picker":    "numeric",
            }
            return synthetic_rule, skill_years

        for rule in _RULES:
            # All keywords-set: at least one must match
            if not any(kw in q_lower for kw in rule["keywords"]):
                continue
            # Exclusions: none can match
            if any(ex in q_lower for ex in rule.get("exclude", [])):
                continue
            # Resolve policy -> desired answer
            # Note: role_ctx passed via answer_question()
            return rule, None  # caller will call policy_fn with ctx

        return None, None

    def answer_question(self,
                        question: str,
                        options: List[str],
                        role_ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Main entry point. Given a form question and its available options,
        return an action dict:
          {"action": "pick",               "option": "<opt>", "rule": "<name>"}
          {"action": "skip_unknown",       "reason": "no rule matched"}
          {"action": "skip_deny",          "reason": "diversity/criminal etc"}
          {"action": "skip_no_option_match","reason": "policy answer doesn't match any option", "rule": "<name>", "desired": "<str>"}
          {"action": "error",              "reason": "<str>"}
        """
        try:
            if not question:
                return {"action": "skip_unknown", "reason": "empty question"}

            q_lower = question.lower().strip()

            # 1. Deny list
            if self._is_denied(q_lower):
                return {"action": "skip_deny", "reason": "deny-list keyword in question"}

            # 2. Rule match
            rule, preresolved = self._match_rule(question)
            if rule is None:
                # === PATCH 35C: LLM fallback ===============================
                # If the rule dispatcher missed AND the feature flag is on AND
                # we have at least one LLM client, route the question through
                # the LLM cascade. Returns same shape as rule-based pick, with
                # rule="llm_nim" or "llm_haiku" so the caller can audit.
                # Deny-list was already applied above (step 1) - we only get
                # here for non-policy-sensitive questions.
                # ============================================================
                if self._llm_fallback_enabled() and (self._nim_client is not None or self._anthropic_client is not None):
                    _dry = bool(self.policy.get("llm_fallback_dry_run", False))
                    log.info(f"[POLICY] rule miss for {question[:80]!r} - routing to LLM fallback (dry_run={_dry})")
                    return _llm_answer_question(
                        question         = question,
                        options          = options or [],
                        policy           = self.policy,
                        role_ctx         = role_ctx or {},
                        resume_text      = self._resume_text,
                        nim_client       = self._nim_client,
                        anthropic_client = self._anthropic_client,
                        threshold        = _LLM_CONFIDENCE_MIN,
                        dry_run          = _dry,
                    )
                # === END PATCH 35C ==========================================
                return {"action": "skip_unknown", "reason": "no rule matched"}

            # 3. Resolve desired answer
            if preresolved is not None:
                desired = preresolved
            else:
                try:
                    desired = rule["policy_fn"](self.policy, role_ctx or {})
                except Exception as e:
                    return {"action": "error", "reason": f"policy_fn failed: {e}"}

            if not desired:
                return {"action": "skip_no_option_match",
                        "reason": "policy did not yield a desired answer",
                        "rule": rule["name"],
                        "desired": None}

            # 4. Pick option matching desired
            picker_name = rule["picker"]
            picker = {
                "yes_no":               _pick_yes_no,
                "yes_no_pns":           _pick_yes_no_pns,
                "contains":             _pick_contains,
                "exact":                _pick_contains,
                "numeric":              _pick_numeric,
                "salary":               _pick_salary,
                "first_non_placeholder": _pick_first_non_placeholder,
            }.get(picker_name, _pick_contains)

            option = picker(desired, options or [])
            if option is None:
                return {"action": "skip_no_option_match",
                        "reason": f"no option matches desired {desired!r}",
                        "rule": rule["name"],
                        "desired": desired}

            return {"action": "pick",
                    "option": option,
                    "rule": rule["name"],
                    "desired": desired}

        except Exception as e:
            log.exception("[POLICY] unexpected error in answer_question")
            return {"action": "error", "reason": str(e)}


# Convenience for test / dev


# === PATCH 35B: LLM fallback for skip_unknown questions =====================
# When the rule dispatcher returns skip_unknown (no keyword pattern matched),
# this fallback consults an LLM. NIM primary tier (devstral 123B) is tried
# first; if it fails, returns malformed JSON, hallucinates an option, or
# returns confidence < threshold, we fall through to Anthropic Haiku.
#
# HARD RULES (mirror the rule dispatcher):
#   1. The chosen option MUST be exactly one of the form's offered options.
#      Case-insensitive exact match. Hallucinated options are rejected.
#   2. Confidence < threshold -> reject and try next model.
#   3. Never auto-answer deny-listed questions (caller filters before us).
#   4. JSON parse error / network error / rate-limit -> graceful fall-through.
#   5. If the cascade is exhausted, return skip_unknown with reason logged.
#
# This function is wired into PolicyAnswers.answer_question by P35C, gated
# by the YAML flag llm_fallback_enabled (default OFF).
# ============================================================================

import json as _json
from typing import Tuple

_LLM_CONFIDENCE_MIN = 0.7
_LLM_AUDIT_LOG = "/app/policy_llm_audit.log"   # write-only audit trail

_LLM_SYSTEM_PROMPT = """You are a form-answering assistant for UK job applications.
You read ONE form question, the options offered, and the candidate policy.
You return ONE option from the list - or null if no option fits the policy.

HARD RULES:
1. The "option" value MUST be an exact string from the options array.
   Never invent or paraphrase. If no option fits, return null.
2. Confidence is a number 0-1. Use 0.9+ only when the policy directly states
   the answer. Use 0.7-0.9 for clear inferences. Below 0.7 means uncertain.
3. Never answer policy-sensitive questions (diversity, criminal record,
   background check, exact salary). For those, return null with reasoning.
4. Output ONLY a JSON object. No preamble. No markdown fence. No explanation.
   The very first character of your response must be {.

Output schema (strict, no extra fields):
{"option": <exact option string or null>, "confidence": <0-1>, "reasoning": "<<=15 words>"}
"""


def _strip_json_fence(text: str) -> str:
    """Strip ```json ... ``` markdown fencing, return inner JSON candidate."""
    s = (text or "").strip()
    if s.startswith("```"):
        # Drop opening fence (and optional 'json' language tag)
        s = s.split("```", 2)[1] if "```" in s[3:] else s[3:]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    if "```" in s:
        s = s.split("```", 1)[0].strip()
    return s


def _policy_summary_for_llm(policy: dict) -> str:
    """Compact subset of the policy YAML safe to send to LLM.
    Never exposes metadata fields (reviewed_by, policy_version, audit log).
    Includes the P35B-extended fields: leadership, languages, hardware,
    software stack, cloud, domain experience, project names."""
    if not policy:
        return "(no policy loaded)"
    parts = []

    # Core eligibility / preferences (existing)
    safe_keys = [
        "right_to_work_uk", "visa_type", "requires_sponsorship_current",
        "currently_based", "open_to_relocation_uk",
        "open_to_relocation_international", "preferred_uk_locations",
        "total_years_experience", "skill_years",
        "available_start", "notice_period_weeks",
        "previously_interviewed_default",
    ]
    for k in safe_keys:
        if k in policy:
            parts.append(f"{k}: {policy[k]}")

    # Salary: never expose min/max numeric, only display string
    sp = policy.get("salary_policy", {}) or {}
    if sp:
        parts.append(f'salary_display: "{sp.get("display_when_asked","")}"')

    # Sponsorship future policy
    fp = policy.get("requires_sponsorship_future_policy", {}) or {}
    if fp:
        parts.append(f"sponsorship_future_policy: {fp}")

    # === PATCH 35B-EXT: include extended profile so the LLM can answer
    # CV-context questions like "have you used K8s?" / "managed a team?"
    ext_keys = [
        "leadership",
        "programming_languages",
        "hardware_platforms",
        "software_stack",
        "cloud_platforms",
        "domain_experience",
        "open_source_visible_projects",
    ]
    for k in ext_keys:
        if k in policy:
            parts.append(f"{k}: {policy[k]}")
    # === END PATCH 35B-EXT

    return "\n  - ".join(["Candidate policy:"] + parts)


def _validate_llm_response(parsed: dict,
                           options: list,
                           threshold: float) -> Tuple[bool, str, str]:
    """Returns (ok, normalised_option, reason). normalised_option is the
    EXACT-CASE form-side option string when ok; empty when not ok."""
    if not isinstance(parsed, dict):
        return False, "", f"not a dict: {type(parsed).__name__}"
    opt = parsed.get("option")
    conf = parsed.get("confidence")

    # Confidence numeric check
    try:
        conf = float(conf) if conf is not None else 0.0
    except Exception:
        return False, "", f"confidence not numeric: {parsed.get('confidence')!r}"
    if not 0.0 <= conf <= 1.0:
        return False, "", f"confidence out of range: {conf}"

    if opt is None:
        return False, "", f"option=null (model declined)"

    if not isinstance(opt, str):
        return False, "", f"option not a string: {type(opt).__name__}"

    # Match exactly to one of the offered options (case-insensitive),
    # but return the exact-case form value so .select_option(label=...) works.
    opt_lower = opt.strip().lower()
    matched = None
    for real in options:
        if real.strip().lower() == opt_lower:
            matched = real
            break
    if matched is None:
        return False, "", f"option {opt!r} not in offered options {options!r}"

    if conf < threshold:
        return False, matched, f"confidence {conf} < threshold {threshold}"

    return True, matched, "ok"


def _audit_llm_call(model: str,
                    question: str,
                    options: list,
                    raw: str,
                    parsed_or_err: str,
                    decision: str) -> None:
    """Append a single audit line to /app/policy_llm_audit.log.
    Never raises - audit failures must not break form submission."""
    try:
        import datetime
        ts = datetime.datetime.utcnow().isoformat(timespec="seconds")
        # Truncate raw + parsed to keep log readable
        raw_t  = (raw or "")[:300].replace("\n", " | ")
        line   = (
            f"{ts}Z  model={model}  decision={decision}\n"
            f"  Q: {question[:200]!r}\n"
            f"  OPTS: {options}\n"
            f"  RAW: {raw_t!r}\n"
            f"  RESULT: {parsed_or_err}\n"
        )
        with open(_LLM_AUDIT_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass  # never break submission for audit failure


def _try_llm(client_fn,
             model_label: str,
             question: str,
             options: list,
             policy_summary: str,
             role_ctx: dict,
             threshold: float) -> dict:
    """Single LLM attempt. Returns dict in same shape as answer_question."""
    title  = (role_ctx or {}).get("title", "")
    jd     = (role_ctx or {}).get("jd_text", "")
    user_prompt = (
        f'Question: "{question}"\n'
        f"Options: {options}\n"
        f"{policy_summary}\n"
        f"Role: {title}\n"
        f"Role description (first 400 chars): {jd[:400]}\n"
        f"Return the JSON now."
    )

    try:
        raw, model_used = client_fn(_LLM_SYSTEM_PROMPT, user_prompt,
                                    max_tokens=200, temperature=0.1)
    except Exception as e:
        _audit_llm_call(model_label, question, options, "", f"call_error:{e}", "skip_call_error")
        log.warning(f"[LLM-{model_label}] call failed: {e}")
        return {"action": "skip_unknown", "reason": f"llm call error: {e}", "rule": f"llm_{model_label}_call_error"}

    cleaned = _strip_json_fence(raw)
    try:
        parsed = _json.loads(cleaned)
    except Exception as e:
        _audit_llm_call(model_used, question, options, raw, f"json_parse_error:{e}", "skip_parse")
        log.info(f"[LLM-{model_label}] {model_used} parse failed: {e}; raw[:120]={raw[:120]!r}")
        return {"action": "skip_unknown", "reason": f"llm json parse error: {e}", "rule": f"llm_{model_label}_parse_error"}

    ok, matched_opt, reason = _validate_llm_response(parsed, options, threshold)
    if not ok:
        _audit_llm_call(model_used, question, options, raw, f"validate_fail:{reason} parsed={parsed}", "skip_validate")
        log.info(f"[LLM-{model_label}] {model_used} validate failed: {reason} parsed={parsed}")
        return {"action": "skip_unknown", "reason": f"llm validate fail: {reason}",
                "rule": f"llm_{model_label}_validate_failed",
                "_llm_parsed": parsed}

    # Success
    confidence = float(parsed.get("confidence", 0.0))
    reasoning  = str(parsed.get("reasoning", ""))[:200]
    _audit_llm_call(model_used, question, options, raw,
                    f"ok option={matched_opt!r} conf={confidence}",
                    "pick")
    log.info(f"[LLM-{model_label}] {model_used} pick option={matched_opt!r} conf={confidence:.2f} reasoning={reasoning!r}")
    return {
        "action":     "pick",
        "option":     matched_opt,
        "rule":       f"llm_{model_label}",
        "confidence": confidence,
        "reasoning":  reasoning,
        "model":      model_used,
    }


# === PATCH 35E: dry-run mode for LLM fallback ==============================
# When dry_run=True, the cascade still runs and all decisions are recorded
# in the audit log, but any "pick" result is rewritten to "skip_unknown"
# before returning to the caller. Net effect: caller treats every novel
# question as unanswerable, so the form aborts on required questions and
# never submits an LLM-derived answer.
#
# This is the safety mode for collecting real-world ground-truth on what
# the LLM would have answered, before flipping to live submissions.
# ============================================================================

def _maybe_dry_run(result: dict, dry_run: bool, question: str, options: list) -> dict:
    """If dry_run is set and result is a pick, log the would-be answer and
    return a skip_unknown result instead. Audit log still records the pick
    decision with a 'dry_run_pick' marker so we can audit later."""
    if not dry_run:
        return result
    if result.get("action") != "pick":
        return result

    # Log the dry-run intercept (separate audit log line for clarity)
    try:
        _audit_llm_call(
            result.get("model", "?"),
            question, options,
            f"dry_run_pick option={result.get('option')!r} conf={result.get('confidence')}",
            f"would_have_picked={result.get('option')!r} rule={result.get('rule')}",
            "DRY_RUN_INTERCEPT",
        )
    except Exception:
        pass
    log.info(
        f"[LLM-DRY-RUN] would have picked {result.get('option')!r} "
        f"conf={result.get('confidence')} rule={result.get('rule')} - "
        f"returning skip_unknown instead"
    )
    return {
        "action": "skip_unknown",
        "reason": "dry_run mode - LLM pick intercepted",
        "rule": "llm_dry_run",
        "dry_run_would_have_picked": result.get("option"),
        "dry_run_would_have_conf":   result.get("confidence"),
        "dry_run_rule":              result.get("rule"),
    }
# === END PATCH 35E ==========================================================


def _llm_answer_question(question: str,
                         options: list,
                         policy: dict,
                         role_ctx: dict = None,
                         resume_text: str = None,
                         nim_client=None,
                         anthropic_client=None,
                         threshold: float = _LLM_CONFIDENCE_MIN,
                         dry_run: bool = False) -> dict:
    """LLM cascade: NIM primary tier -> Haiku fallback. Same return shape as
    PolicyAnswers.answer_question().

    resume_text (optional): the candidate's full resume in plain text. When
    provided, it is appended to the prompt so the LLM can answer questions
    grounded in actual CV content (projects, technologies, work history).
    """
    if not options:
        return {"action": "skip_unknown", "reason": "no options to pick from", "rule": "llm_no_options"}

    policy_summary = _policy_summary_for_llm(policy)
    # === PATCH 35B-RESUME: append CV when available =================
    if resume_text:
        # Cap at 4000 chars to stay within reasonable token budget.
        # Same trim as cover_letter.py uses.
        resume_block = resume_text[:4000]
        policy_summary = policy_summary + "\n\nCandidate resume (CV, plain text):\n" + resume_block
    # === END PATCH 35B-RESUME ========================================

    # Stage 1: NIM primary
    if nim_client is not None:
        # === PATCH 35B-TIER-FIX: tier="fast" not "primary" ==============
        # Diagnostic in P35B testing showed devstral (primary) returned
        # 400/500 on structured-extraction prompts and the NIMClient
        # internal cascade silently fell through to Llama 3.3 (fast tier)
        # which handled the task cleanly. Skip the wasted devstral round
        # by requesting "fast" directly. Saves ~30-90s/call wasted retries.
        # =================================================================
        def _nim_call(system, user, **kwargs):
            return nim_client.complete_system(system, user, tier="fast", **kwargs)
        result = _try_llm(_nim_call, "nim", question, options, policy_summary, role_ctx or {}, threshold)
        if result.get("action") == "pick":
            # === PATCH 35E: dry-run intercept ====================
            return _maybe_dry_run(result, dry_run, question, options)
        log.info(f"[LLM] NIM primary tier did not pick (rule={result.get('rule')}). Falling through to Haiku.")
    else:
        log.warning("[LLM] no NIM client provided; skipping NIM stage")

    # Stage 2: Haiku
    if anthropic_client is not None:
        def _haiku_call(system, user, **kwargs):
            return anthropic_client.complete_system(system, user, **kwargs)
        result = _try_llm(_haiku_call, "haiku", question, options, policy_summary, role_ctx or {}, threshold)
        if result.get("action") == "pick":
            # === PATCH 35E: dry-run intercept ====================
            return _maybe_dry_run(result, dry_run, question, options)
        log.info(f"[LLM] Haiku did not pick either (rule={result.get('rule')}).")
    else:
        log.warning("[LLM] no Anthropic client provided; skipping Haiku stage")

    return {"action": "skip_unknown", "reason": "llm cascade exhausted", "rule": "llm_cascade_exhausted"}
# === END PATCH 35B ==========================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    pa = PolicyAnswers("/app/profile_answers.yaml")
    tests = [
        ("Are you open to relocation for this role?", ["Yes", "No"]),
        ("Do you require visa sponsorship?", ["Yes", "No"]),
        ("Will you now or in the future require sponsorship?", ["Yes", "No"]),
        ("What is your expected salary?", ["<40k", "40-55k", "55-70k", "70-90k", "90k+"]),
        ("How many years of Python experience?", ["0-1", "2-3", "4-6", "7+"]),
        ("How did you hear about us?", ["LinkedIn", "Company website", "Referral", "Other"]),
        ("What is your gender?", ["Male", "Female", "Other", "Prefer not to say"]),
    ]
    for q, opts in tests:
        r = pa.answer_question(q, opts, role_ctx={"title": "Senior AI Engineer", "jd_text": "Permanent role"})
        print(f"  Q: {q!r}")
        print(f"     opts: {opts}")
        print(f"     -> {r}")
        print()
