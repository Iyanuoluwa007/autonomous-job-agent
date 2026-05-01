"""
Geographic-suitability filter for job applications.

=== PATCH 36: location filter ===============================================
Goal: prevent the agent from applying to roles in locations the candidate
cannot accept (per profile_answers.yaml policy on relocation).

Approach:
  Layer A - Title-level explicit non-UK marker (highest signal)
  Layer B - JD text "based in / located in / authorized to work in" patterns
  Layer C - Multi-location awareness: if jd lists London AND Madrid, KEEP
            (let the form's work-auth question abort if needed)

Conservative default: when uncertain, return acceptable=True. Better to
waste cover-letter tokens than miss UK-suitable roles.

Public API:
    is_location_acceptable(title, jd_text, policy) -> (bool, str)
        Returns (True, "ok") if role appears UK-suitable or location-ambiguous.
        Returns (False, "<matched non-UK marker>") if role is clearly non-UK
        AND policy says no international relocation.
=============================================================================
"""

import re
from typing import Tuple


# UK-friendly locations - cities, regions, country names. Lowercase comparison.
# Wide net intentionally - avoid false-rejecting UK roles.
_UK_FRIENDLY = {
    # Country-level
    "uk", "u.k.", "united kingdom", "britain", "great britain", "gb",
    "england", "scotland", "wales", "northern ireland", "ireland (uk)",
    # Major UK cities (top 30 + tech hubs)
    "london", "manchester", "cambridge", "oxford", "edinburgh", "glasgow",
    "bristol", "leeds", "birmingham", "liverpool", "sheffield", "newcastle",
    "belfast", "cardiff", "reading", "nottingham", "southampton", "brighton",
    "york", "exeter", "bath", "milton keynes", "coventry", "leicester",
    "aberdeen", "dundee", "swansea", "derby", "norwich", "portsmouth",
    "stoke-on-trent", "stoke", "warrington", "blackpool", "preston",
}

# Non-UK location markers - countries + major non-UK cities.
# These are STRICT triggers when matched in title or "based in"/"located in" context.
_NON_UK_MARKERS = {
    # United States
    "united states", "usa", "u.s.a.", "u.s.", "america",
    "new york", "nyc", "ny ", "manhattan", "brooklyn",
    "san francisco", "sf bay", "bay area", "silicon valley",
    "los angeles", "la (ca)", "boston", "seattle", "austin", "chicago",
    "washington dc", "denver", "atlanta", "miami", "dallas", "houston",
    # Continental Europe
    "spain", "madrid", "barcelona", "valencia", "sevilla",
    "germany", "berlin", "munich", "munchen", "hamburg", "frankfurt", "cologne",
    "france", "paris", "lyon", "marseille", "toulouse",
    "italy", "milan", "rome", "torino", "naples",
    "netherlands", "amsterdam", "rotterdam", "the hague", "utrecht",
    "belgium", "brussels", "antwerp",
    "switzerland", "zurich", "geneva", "bern", "lausanne",
    "austria", "vienna", "salzburg",
    "denmark", "copenhagen",
    "sweden", "stockholm", "gothenburg",
    "norway", "oslo", "bergen",
    "finland", "helsinki",
    "poland", "warsaw", "krakow", "wroclaw",
    "portugal", "lisbon", "porto",
    "czech republic", "prague",
    "greece", "athens",
    # Asia-Pacific
    "india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "chennai", "pune",
    "singapore",
    "china", "shanghai", "beijing", "shenzhen", "hong kong",
    "japan", "tokyo", "osaka", "kyoto",
    "south korea", "seoul",
    "australia", "sydney", "melbourne", "brisbane", "perth",
    "new zealand", "auckland", "wellington",
    "thailand", "bangkok",
    "indonesia", "jakarta",
    "vietnam", "hanoi", "ho chi minh", "saigon",
    "philippines", "manila",
    # Middle East / Africa
    "uae", "dubai", "abu dhabi",
    "israel", "tel aviv", "jerusalem",
    "saudi arabia", "riyadh",
    "south africa", "cape town", "johannesburg",
    "egypt", "cairo",
    "nigeria", "lagos", "abuja",
    "kenya", "nairobi",
    # Americas (non-US)
    "canada", "toronto", "vancouver", "montreal", "ottawa",
    "mexico", "mexico city",
    "brazil", "sao paulo", "rio de janeiro",
    "argentina", "buenos aires",
    "chile", "santiago",
}

# Phrases that indicate the role is location-flexible (KEEP)
_LOCATION_FLEXIBLE_HINTS = {
    "remote", "fully remote", "work from anywhere", "wfh",
    "global", "worldwide", "any location", "location flexible",
    "remote-first", "hybrid", "anywhere",
    "emea", "europe-wide", "across europe",
}

# Patterns that strongly bind a location to the role (Layer B)
_BASED_IN_PATTERNS = [
    re.compile(r"\bbased in[:\s]+([a-z][a-z\s,\-]{2,40})", re.IGNORECASE),
    re.compile(r"\blocated in[:\s]+([a-z][a-z\s,\-]{2,40})", re.IGNORECASE),
    re.compile(r"\bauthori[sz]ed to work in[:\s]+([a-z][a-z\s,\-]{2,40})", re.IGNORECASE),
    re.compile(r"\bmust be (?:based|located|residing) in[:\s]+([a-z][a-z\s,\-]{2,40})", re.IGNORECASE),
    re.compile(r"\bonly applicants? (?:based|located|from)[:\s]+([a-z][a-z\s,\-]{2,40})", re.IGNORECASE),
    re.compile(r"\bposition is based in[:\s]+([a-z][a-z\s,\-]{2,40})", re.IGNORECASE),
]


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for matching."""
    return " ".join((text or "").lower().split())


def _find_uk_markers(text: str) -> set:
    """Return set of UK-friendly markers found in text (word-boundary aware)."""
    if not text:
        return set()
    norm = _normalize(text)
    found = set()
    for marker in _UK_FRIENDLY:
        # Word-boundary match - "york" must not match "new york"
        pattern = r"\b" + re.escape(marker) + r"\b"
        if re.search(pattern, norm):
            # Special-case "york" - reject if "new york" appears in the same text
            if marker == "york" and "new york" in norm:
                continue
            found.add(marker)
    return found


def _find_non_uk_markers(text: str) -> set:
    """Return set of non-UK markers found in text (word-boundary aware)."""
    if not text:
        return set()
    norm = _normalize(text)
    found = set()
    for marker in _NON_UK_MARKERS:
        pattern = r"\b" + re.escape(marker) + r"\b"
        if re.search(pattern, norm):
            found.add(marker)
    return found


def _has_location_flexible_hint(text: str) -> bool:
    norm = _normalize(text)
    for hint in _LOCATION_FLEXIBLE_HINTS:
        if re.search(r"\b" + re.escape(hint) + r"\b", norm):
            return True
    return False


# === PATCH 36-FIX: catch "Remote, <city>" / "Remote / <city>" patterns ====
# A common phrasing on US/EU job boards: "Remote, San Francisco" means
# remote-but-based-in-SF, which requires US work auth. Original P36 hit
# Rule 4 (location-flexible) on "Remote" before Rule 5 (JD lock) had a
# chance to check the non-UK city, so the role passed. New pattern catches
# this directly with comma/slash/dash separators.
# ============================================================================
_REMOTE_CITY_PATTERN = re.compile(
    r"\bremote\s*[,/\-\u2014\u2013|()]+\s*([a-z][a-z\s,/\-]{2,40})",
    re.IGNORECASE,
)


def is_location_acceptable(title: str,
                           jd_text: str,
                           policy: dict) -> Tuple[bool, str]:
    """
    Decide whether a role is geographically acceptable per the policy.

    Returns (True, reason) if acceptable.
    Returns (False, matched_marker) if rejected.

    Rule order (PATCH 36-FIX: Rule 5 moved before Rule 4):
        1. If policy.open_to_relocation_international == "yes" -> always accept.
        2. If title contains a UK-friendly marker -> accept (UK-tagged role).
        3. If title contains a non-UK marker AND no UK marker in title or JD -> reject.
        4. If JD "Remote, <non-UK city>" pattern -> reject (P36-FIX).
        5. If JD "based in" pattern matches non-UK -> reject.
        6. If JD has location-flexible hint (Remote/Global/EMEA) -> accept.
        7. Default: accept (ambiguous - let form-side work-auth abort if needed).
    """
    intl_ok = str(policy.get("open_to_relocation_international", "no")).strip().lower()
    if intl_ok in ("yes", "true", "1", "y"):
        return (True, "policy: international relocation allowed")

    title_norm = _normalize(title or "")
    jd_norm    = _normalize(jd_text or "")

    title_uk     = _find_uk_markers(title or "")
    title_non_uk = _find_non_uk_markers(title or "")
    jd_uk        = _find_uk_markers(jd_text or "")
    jd_non_uk    = _find_non_uk_markers(jd_text or "")

    combined_uk     = title_uk | jd_uk
    combined_non_uk = title_non_uk | jd_non_uk

    # Rule 2: title UK-tagged -> accept
    if title_uk:
        return (True, f"title contains UK marker: {sorted(title_uk)}")

    # Rule 3: title non-UK and zero UK signal anywhere -> reject
    if title_non_uk and not combined_uk:
        return (False, f"title-only non-UK location: {sorted(title_non_uk)}")

    # === PATCH 36-FIX Rule 4: Remote, <non-UK city> pattern ===============
    # Catches: "Remote, San Francisco" / "Remote / Berlin" / "Remote - Tokyo"
    # Runs BEFORE the location-flexible hint check so "Remote" alone is not
    # mistakenly trusted when paired with a specific non-UK city.
    # ====================================================================
    for m in _REMOTE_CITY_PATTERN.finditer(jd_norm):
        captured = (m.group(1) or "").strip()
        captured_uk     = _find_uk_markers(captured)
        captured_non_uk = _find_non_uk_markers(captured)
        if captured_non_uk and not captured_uk:
            return (False, f"Remote+non-UK: {sorted(captured_non_uk)} (matched: {captured[:50]!r})")
    # === END PATCH 36-FIX Rule 4 =========================================

    # Rule 5: JD "based in" pattern locks to non-UK location
    for pattern in _BASED_IN_PATTERNS:
        for m in pattern.finditer(jd_norm):
            captured = (m.group(1) or "").strip()
            captured_uk     = _find_uk_markers(captured)
            captured_non_uk = _find_non_uk_markers(captured)
            if captured_non_uk and not captured_uk:
                if not combined_uk:
                    return (False, f"JD locks to non-UK: {sorted(captured_non_uk)} (matched: {captured!r})")

    # Rule 6: location-flexible hint -> accept (NOW runs after Rule 4 + 5)
    if _has_location_flexible_hint(title or "") or _has_location_flexible_hint(jd_text or ""):
        return (True, "location-flexible hint (remote/global/EMEA)")

    # Rule 7: default accept (conservative)
    if combined_non_uk and combined_uk:
        return (True, f"multi-location (UK={sorted(combined_uk)}, non-UK={sorted(combined_non_uk)}) - let form decide")
    if combined_non_uk and not combined_uk:
        return (True, f"non-UK mentioned ({sorted(combined_non_uk)}) but no hard lock - let form decide")
    return (True, "no location markers found")


# Self-test when run directly
if __name__ == "__main__":
    import json
    policy = {"open_to_relocation_international": "no"}

    cases = [
        # Rule 2: title UK
        ("Senior AI Engineer London", "We hire across Europe", True),
        # Rule 3: title non-UK, no UK signal
        ("Forward Deployed Engineer Spain", "ElevenLabs Madrid office", False),
        ("Solutions Engineer India", "Bangalore-based role", False),
        # Rule 4 (P36-FIX): Remote + non-UK city
        ("Forward Deployed Engineer", "Remote, San Francisco", False),
        ("Senior Engineer", "Remote / Berlin office", False),
        ("Engineer", "Remote - Tokyo, Japan", False),
        # Rule 4 should ACCEPT remote-only / remote-UK
        ("AI Engineer", "Remote, UK", True),
        ("AI Engineer", "Fully remote work from anywhere", True),
        ("AI Engineer", "Remote across Europe", True),
        # Rule 5: JD locks to non-UK
        ("Engineer", "Position is based in Madrid, Spain", False),
        # Rule 6: location-flexible
        ("Senior Engineer", "Global EMEA team", True),
        # Multi-location
        ("Senior Engineer", "Offices in London, Berlin and NYC", True),
        # No markers
        ("Software Engineer", "We build great products", True),
        # Title with UK city, but JD also says Spain
        ("Engineer London", "Position is based in Madrid", True),  # Rule 2 wins early
        # Edge: "york" alone vs "new york"
        ("Engineer York", "We're in York, North Yorkshire", True),
        ("Engineer New York", "Manhattan office", False),
    ]
    for title, jd, expected in cases:
        ok, reason = is_location_acceptable(title, jd, policy)
        status = "PASS" if ok == expected else "FAIL"
        print(f"  [{status}] expected={expected} got={ok}  title={title!r}")
        if ok != expected:
            print(f"          jd={jd!r}")
            print(f"          reason={reason}")
