"""
Role Title Filter
Hard-rejects jobs whose title is outside the candidate's target universe,
BEFORE any fit-scoring or cover-letter generation. Saves NIM quota and
prevents wrong-job submissions even when the fit scorer misbehaves.

Rule: a title must contain at least one ACCEPT keyword AND zero REJECT
keywords. Both lists match on whole words, case-insensitive.
"""

import re

# Must contain at least ONE of these (whole-word match, case-insensitive).
# Broad enough to catch genuine variants, narrow enough to reject retail.
ACCEPT_KEYWORDS = {
    # Engineer-family titles
    "engineer", "engineering",
    "developer", "programmer",
    "scientist", "researcher", "research",
    "architect",
    "specialist", "technologist",
    "lead", "principal", "staff",
    # Core target domains
    "robotics", "robot",
    "ml", "ai", "cv",
    "vision", "perception", "sensor", "sensors",
    "autonomy", "autonomous",
    "embedded", "firmware",
    "slam", "lidar", "radar",
    "motion", "control", "controls",
    "ros", "ros2",
    "learning",  # machine learning, deep learning
    "simulation", "simulator",
    "mlops", "devops",
    "backend", "systems",
    "algorithm", "algorithms",
    "mathematician",
    # Seniority tokens that imply engineering context
    "intern",  # intentional: allow engineering internships
    # === PATCH 12: quant/research expansion ===
    "quant", "quantitative",
    "trader", "trading",
    "hft", "systematic",
    "mts",          # "member of technical staff"
    "applied",      # "applied scientist"
    "analyst",      # "quantitative analyst" etc -- engineering adjacent
    "execution",    # "execution trader"
    "risk",         # "risk analyst/engineer"
    "portfolio",    # "portfolio engineer"
    "derivatives",
    "fixed-income", "fixed income",
    "options",
}

# If ANY of these appear (whole-word), reject immediately. Overrides ACCEPT.
# Covers retail, admin, sales, hospitality, care, teaching assistants etc.
# Note: uses whole-word matching, so "care" won't match "career", "sales"
# won't match "salesforce-engineer" etc.
REJECT_KEYWORDS = {
    # Retail / optical / in-store
    "optical", "optician", "dispenser",
    "cashier", "barista", "waiter", "waitress", "bartender",
    "shop", "store", "retail",
    "salesperson", "seller", "merchandiser",
    "stylist", "beautician", "therapist",
    # Care / hospitality / manual
    "carer", "nurse", "nursing", "care", "caregiver", "caretaker",
    "cleaner", "cleaning", "housekeeper", "janitor",
    "driver", "porter", "picker", "packer", "warehouse",
    "labourer", "labor", "handyman", "fitter",
    "chef", "cook", "kitchen", "restaurant",
    # Admin / reception / education (non-engineering)
    "receptionist", "secretary", "pa",
    "teacher", "teaching", "tutor", "lecturer",
    "assistant",  # most assistants (optical, teaching, personal, care) are not eng
    "administrator", "clerk",
    # Finance / HR / legal / marketing (non-engineering variants)
    "accountant", "bookkeeper",
    "recruiter", "recruitment", "hr",
    "paralegal", "solicitor", "barrister", "lawyer",
    "copywriter", "journalist", "editor",
    "nanny", "babysitter",
    # Medical (non-research)
    "doctor", "physician", "dentist", "pharmacist", "radiographer",
    "paramedic", "physiotherapist",
}

# Exceptions: engineering-qualified versions of otherwise-rejected words.
# If the title contains BOTH a REJECT keyword AND one of these phrases
# (case-insensitive substring match), allow it through.
REJECT_OVERRIDES = (
    "software engineer",
    "data engineer",
    "research engineer",
    "ml engineer",
    "ai engineer",
    "machine learning engineer",
    "computer vision engineer",
    "robotics engineer",
    "systems engineer",
    "embedded engineer",
    "perception engineer",
    "research scientist",
    "research assistant",          # academic/research assistants are ok
    "teaching assistant phd",      # PhD TA positions are engineering-adjacent
    # === PATCH 12: quant override phrases ===
    "quantitative analyst",        # real job, not admin
    "quant analyst",
    "research analyst",
    "applied scientist",
    "research engineer",           # already above but harmless dup
    "member of technical staff",
    "members of technical staff",
)


# === LEAD_GEN_PATTERNS (patch 11) ==========================================
# Patterns that indicate a listing is a paid training programme, bootcamp,
# or recruitment-agency lead-gen rather than a real engineering job.
# Checked BEFORE the normal ACCEPT/REJECT lists.
LEAD_GEN_TITLE_PATTERNS = (
    "placement programme",
    "placement program",
    "expiring soon",
    "apprenticeship",
    "bootcamp",
    "boot camp",
)

# If URL is from cv-library and title contains any of these softer tokens,
# treat as lead-gen. CV-Library hosts a lot of paid training placements.
CVLIB_SOFT_TOKENS = (
    "trainee",
    "training",
    "course",
    "programme",
    "program",
)


def _is_leadgen(title_low: str, url_low: str) -> tuple[bool, str]:
    """Return (is_leadgen, matched_pattern). Called before ACCEPT/REJECT."""
    for pat in LEAD_GEN_TITLE_PATTERNS:
        if pat in title_low:
            return True, f"leadgen:{pat}"
    if "cv-library.co.uk" in url_low:
        for tok in CVLIB_SOFT_TOKENS:
            if tok in title_low:
                return True, f"cvlib_softtoken:{tok}"
    return False, ""


def _has_whole_word(text_lower: str, word: str) -> bool:
    return re.search(r"\b" + re.escape(word) + r"\b", text_lower) is not None


def is_relevant_role(title: str, url: str = "") -> tuple[bool, str]:
    """
    Returns (accepted, reason).
    True  -> proceed to fit-scoring
    False -> skip entirely, log reason
    """
    if not title or not title.strip():
        return False, "empty_title"

    low = title.lower().strip()
    url_low = (url or "").lower()

    # leadgen check (patch 11) -- runs before ACCEPT/REJECT
    is_lg, lg_reason = _is_leadgen(low, url_low)
    if is_lg:
        return False, lg_reason

    # 1. REJECT-list check with override pass.
    hit_reject = None
    for kw in REJECT_KEYWORDS:
        if _has_whole_word(low, kw):
            hit_reject = kw
            break

    if hit_reject:
        # Override: does the title also contain an engineering phrase?
        for override in REJECT_OVERRIDES:
            if override in low:
                return True, f"override:{override}"
        return False, f"reject_keyword:{hit_reject}"

    # 2. ACCEPT-list check.
    for kw in ACCEPT_KEYWORDS:
        if _has_whole_word(low, kw):
            return True, f"accept_keyword:{kw}"

    # 3. Nothing matched: reject as off-topic.
    return False, "no_accept_keyword"


if __name__ == "__main__":
    # Smoke test when run directly.
    tests = [
        # Should ACCEPT
        ("Computer Vision Engineer",           True),
        ("Senior ML Engineer",                 True),
        ("Robotics Engineer",                  True),
        ("Embedded Systems Engineer",          True),
        ("ROS2 Software Engineer",             True),
        ("Research Scientist - Perception",    True),
        ("Principal AI Engineer",              True),
        ("Machine Learning Developer",         True),
        ("Autonomous Vehicle Engineer",        True),
        ("Software Engineer Intern",           True),
        # Should REJECT (these are what broke production)
        ("Optical Assistant",                  False),
        ("Experienced Optical Assistant",      False),
        ("Sales Assistant",                    False),
        ("Teaching Assistant",                 False),
        ("Care Assistant",                     False),
        ("Cleaner",                            False),
        ("Warehouse Picker",                   False),
        ("Kitchen Porter",                     False),
        ("Receptionist",                       False),
        ("HR Administrator",                   False),
        ("Chef de Partie",                     False),
        ("",                                   False),
    ]

    print("Role filter smoke test:")
    failed = 0
    for title, expected in tests:
        got, reason = is_relevant_role(title)
        ok = "[OK] " if got == expected else "[FAIL]"
        if got != expected:
            failed += 1
        print(f"  {ok} {title!r:45} expected={expected} got={got} ({reason})")

    print()
    print(f"{len(tests) - failed}/{len(tests)} passed" if not failed else f"{failed} FAILED")
