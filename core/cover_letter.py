"""
Cover Letter Generator
Uses devstral (primary) for best writing quality.
Falls back through the NIM client's own tier cascade if primary rate-limits.

Target style: formal, measured UK professional cover letter.
  - Opens with "Dear Hiring Manager,"
  - First body line: "I am applying for the {title} role at {company}."
  - Five body paragraphs: intro, capability 1, capability 2, why-this-company, summary.
  - Closing line: "Thank you for your time and consideration..."
  - Signs off "Yours sincerely, {full_name}" with no contact block.
  - British English spelling throughout.

Pipeline per call:
  1. Sanity-check JD and resume (short-circuit on empty).
  2. Format prompt and call NIM with retry-on-validation-fail across tiers.
  3. Post-scrub: normalise punctuation/quotes, strip markdown and any contact
     block after the name, de-duplicate repeated sentences, remove corporate
     cliches that slipped past the prompt.
  4. Validate: word count, greeting present, sign-off present, company+title
     referenced, no placeholder leakage.
  5. Return (text, model_used) or raise CoverLetterError.
"""

import logging
import re

log = logging.getLogger("cover_letter")


# ------------------------------------------------------------------ errors
class CoverLetterError(Exception):
    """Raised when no attempt produced a usable letter. Caller decides fallback."""
    pass


# ------------------------------------------------------------------ scrub helpers
# Corporate cliches the user dislikes. Removed with word-boundary regex.
_FORBIDDEN_WORDS = (
    "leverage", "leveraging", "leveraged",
    "synergy", "synergies", "synergistic",
    "passionate", "passionately",
    "stakeholder", "stakeholders",
)

# Clunky openers and filler phrases to strip. Case-insensitive substring removals.
_FORBIDDEN_PHRASES = (
    "i am writing to apply",
    "i am writing to express",
    "i am excited to apply",
    "i am thrilled to apply",
    "not just",
    "more than just",
    "the same kind of",
)


def _normalise_unicode(text: str) -> str:
    """Replace AI-giveaway punctuation and smart quotes with plain ASCII."""
    t = text
    t = t.replace("\u2014", ", ")   # em dash  -> comma + space
    t = t.replace("\u2013", "-")    # en dash  -> hyphen
    t = t.replace("\u2026", "...")  # ellipsis
    t = t.replace("\u2018", "'").replace("\u2019", "'")   # curly singles
    t = t.replace("\u201c", '"').replace("\u201d", '"')   # curly doubles
    t = t.replace("\u00a0", " ")    # non-breaking space
    return t


def _strip_markdown(text: str) -> str:
    """Remove bold/italic markers, inline code, headers, bullets, numbered lists."""
    t = text
    t = t.replace("***", "").replace("**", "").replace("*", "")
    t = t.replace("__", "").replace("`", "")
    cleaned = []
    for line in t.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            line = stripped.lstrip("#").strip()
        elif stripped.startswith(("- ", "* ", "+ ")):
            line = stripped[2:]
        elif re.match(r"^\d+\.\s", stripped):
            line = re.sub(r"^\d+\.\s", "", stripped)
        cleaned.append(line)
    return "\n".join(cleaned)


def _strip_signoff_contact_block(text: str, full_name: str) -> str:
    """
    Models sometimes append contact details after the name:
        Yours sincerely,
        Firstname Lastname
        candidate@example.com
        +44 ...
        linkedin.com/in/...
    The PDF CV and the application form already carry contact info, so anything
    after the last line containing the candidate's name is discarded.
    """
    if not full_name:
        return text.rstrip()

    lines = text.split("\n")
    name_lower = full_name.lower().strip()
    last_name_idx = -1
    for i, line in enumerate(lines):
        if name_lower and name_lower in line.lower():
            last_name_idx = i

    if last_name_idx >= 0:
        lines = lines[: last_name_idx + 1]

    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines)


def _scrub_forbidden(text: str) -> str:
    """Remove banned words/phrases that slipped past the prompt."""
    out = text
    for phrase in _FORBIDDEN_PHRASES:
        out = re.compile(re.escape(phrase), re.IGNORECASE).sub("", out)
    for word in _FORBIDDEN_WORDS:
        out = re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE).sub("", out)
    # Tidy whitespace/punctuation holes just created.
    out = re.sub(r" +", " ", out)
    out = re.sub(r" +([,.;:!?])", r"\1", out)
    out = re.sub(r"([,.;:])\1+", r"\1", out)
    return out


def _dedupe_sentences(text: str) -> str:
    """Drop duplicate sentences within a paragraph (adjacent or not)."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    seen = set()
    out = []
    for p in parts:
        norm = re.sub(r"\s+", " ", p.strip().lower())
        if len(norm) > 20 and norm in seen:
            continue
        if norm:
            seen.add(norm)
        out.append(p)
    return " ".join(out)


def _has_placeholder_leak(text: str) -> bool:
    """Detect unresolved template placeholders that must never reach the employer."""
    patterns = [
        r"\{[a-z_]+\}",              # {full_name}, {company}
        r"\[your [^\]]+\]",          # [Your Name]
        r"\[insert [^\]]+\]",        # [insert role]
        r"\[company\]", r"\[role\]", r"\[name\]", r"\[title\]",
    ]
    low = text.lower()
    return any(re.search(p, low) for p in patterns)


def _clean_cover_letter(raw: str, full_name: str = "") -> str:
    """Full scrub pipeline. Order matters."""
    t = raw or ""

    t = _normalise_unicode(t)
    t = _strip_markdown(t)
    t = _strip_signoff_contact_block(t, full_name)
    t = _scrub_forbidden(t)

    # Per-paragraph dedupe.
    paragraphs = [_dedupe_sentences(p) for p in t.split("\n\n")]
    t = "\n\n".join(paragraphs)

    # Collapse excess whitespace.
    t = re.sub(r"[ \t]+\n", "\n", t)
    while "\n\n\n" in t:
        t = t.replace("\n\n\n", "\n\n")

    return t.strip()


def _word_count(text: str) -> int:
    return len(text.split())


# ------------------------------------------------------------------ prompts
SYSTEM = (
    "You are a professional careers writer producing formal UK-style cover letters. "
    "Your tone is measured, confident, and professional, never casual, never dense with jargon. "
    "You write in British English: 'optimised', 'centred', 'behaviour', 'specialised'. "
    "You write clear, natural prose that flows when read aloud. "
    "Do not cram every technology name from the resume into the letter. Pick the two or three most "
    "relevant and describe the capability clearly in plain language. "
    "CRITICAL: The candidate is APPLYING to the company. They do NOT work there. "
    "All projects on the resume are the candidate's own independent or previous work. "
    "Never write as if the candidate already works at the target company. "
    "Output ONLY the cover letter text, starting with 'Dear Hiring Manager,' and ending with the "
    "candidate's name on its own line. No subject line, no preamble, no contact block, no email, "
    "no phone, no LinkedIn after the name."
)

TEMPLATE = """Write a tailored cover letter for this job application.

CANDIDATE RESUME:
{resume}

TARGET ROLE: {title} at {company}

JOB DESCRIPTION:
{jd}

CANDIDATE NAME (use ONLY for the sign-off line): {full_name}

REQUIRED STRUCTURE (follow exactly):

Dear Hiring Manager,

[Paragraph 1 - Intro, 2-3 sentences]
First sentence must be exactly: "I am applying for the {title} role at {company}."
Then one or two sentences summarising the candidate's background at a high level and how it matches the role focus. Do not list technologies yet.

[Paragraph 2 - Primary capability, 3-4 sentences]
Describe the candidate's most relevant hands-on experience for THIS job. Reference one or two specific projects from the resume that directly match the job description. Name the core technologies in flowing prose, not as a list. Include one or two concrete numbers or outcomes if they are in the resume.

[Paragraph 3 - Secondary capability, 3-4 sentences]
Cover a complementary strength: deployment, integration, engineering discipline, or system design. Tie it back to what the job description asks for. Keep it specific but readable, not a technology dump.

[Paragraph 4 - Why this company, 2-3 sentences]
Start with "What draws me to {company} is..." or "I am particularly drawn to {company}'s...". Reference something concrete from the job description, such as the product, the mission, or the problem space. Explain what makes this specific company a good fit for the candidate.

[Paragraph 5 - Summary, 2-3 sentences]
A closing statement of value. What the candidate brings and how they would contribute. Confident but not boastful.

[Closing line, one sentence on its own paragraph]
"Thank you for your time and consideration. I look forward to the opportunity to discuss how I can contribute to {company}'s work."

Yours sincerely,
{full_name}

STRICT RULES:
1. Only reference resume projects and skills that DIRECTLY match the job description. If a project is not relevant, do not mention it.
2. Target length: 260 to 360 words total (excluding the greeting and sign-off lines).
3. Be specific. Use exact technology names, numbers, and outcomes from the resume where they matter.
4. Never open with "I am writing to apply" or "I am excited to apply". Use the required opening sentence exactly.
5. Never use the words leverage, synergy, passionate, or stakeholder.
6. Never use phrases like "not just", "more than just", or "the same kind of".
7. Never use em dashes or en dashes. Use commas, full stops, or colons.
8. British English spelling throughout. No American spellings.
9. Do not add email, phone, LinkedIn, address, or any contact detail after the name.
10. Do not use any placeholders in square brackets or curly braces. Every detail must be real text.
11. Do not include a subject line or role title header above the greeting.
"""


# === PATCH 18A: aggregator-as-employer check =============================
# If the cover letter references one of these as the employer, the scraper
# mis-identified the company (usually falling back to the URL host for CV-
# Library / Reed / Indeed / LinkedIn aggregators). Reject such letters
# regardless of how well they read -- shipping "at Cv-Library" as the
# company is always wrong.
AGGREGATOR_TOKENS = (
    "cv-library", "cv library", "cvlibrary",
    "reed.co.uk", "reed plc",
    "indeed.com", "indeed jobs", "indeed uk",
    "linkedin.com", "linkedin jobs",
    "totaljobs", "jobserve", "jobsite", "monster.co.uk", "glassdoor",
    " at uk ", " at uk.", " at uk,",
    " at ca ", " at ca.", " at ca,",
    " at us ", " at us.", " at us,",
)
# === END PATCH 18A ========================================================


# ------------------------------------------------------------------ main class
class CoverLetterGenerator:
    """
    Usage:
        gen = CoverLetterGenerator(nim_client, resume_text, profile)
        try:
            letter, model = gen.generate(title, company, jd_text)
        except CoverLetterError as e:
            # caller logs + marks application as skipped/manual
            ...
    """

    MIN_WORDS = 240    # user's reference letters run 250-260 words
    MAX_WORDS = 450
    MIN_JD_CHARS = 200   # below this, scrape almost certainly failed

    def __init__(self, nim_client, resume_text, profile):
        self.nim = nim_client
        self.resume = resume_text or ""
        self.profile = profile or {}

    # ------------------------------------------------------------ public
    def generate(self, title, company, jd_text):
        """
        Returns (cover_letter_text, model_used).
        Raises CoverLetterError if nothing usable came back after all retries.
        """
        title = (title or "").strip()
        company = (company or "").strip()
        jd_text = (jd_text or "").strip()
        full_name = (self.profile.get("full_name") or "").strip()

        # Sanity guards — never burn a quota call on junk input.
        if not title or not company:
            raise CoverLetterError("Missing title or company.")
        if len(jd_text) < self.MIN_JD_CHARS:
            raise CoverLetterError(
                f"JD too short ({len(jd_text)} chars, need {self.MIN_JD_CHARS}). "
                f"Likely scrape failure."
            )
        if len(self.resume) < 200:
            raise CoverLetterError("Resume text missing or too short.")
        if not full_name:
            raise CoverLetterError("profile.full_name is required for sign-off.")

        prompt = TEMPLATE.format(
            resume    = self.resume[:4000],
            title     = title,
            company   = company,
            jd        = jd_text[:3000],
            full_name = full_name,
        )

        last_cleaned = ""
        last_model = None
        last_reason = "no attempts made"

        # Attempt schedule:
        #   1. primary @ temp 0.70  (devstral, quality run)
        #   2. primary @ temp 0.80  (rephrase if length/validation failed)
        #   3. fast    @ temp 0.65  (cheaper fallback if primary keeps failing)
        attempts = [
            ("primary", 0.70),
            ("primary", 0.80),
            ("fast",    0.65),
        ]

        for i, (tier, temp) in enumerate(attempts, start=1):
            try:
                raw, model = self.nim.complete_system(
                    SYSTEM, prompt,
                    tier=tier,
                    max_tokens=1100,
                    temperature=temp,
                )
            except Exception as e:
                log.warning(f"[CL] Attempt {i} ({tier}) call failed: {e}")
                last_reason = f"api_error:{e}"
                continue

            cleaned = _clean_cover_letter(raw, full_name=full_name)
            ok, reason = self._validate(cleaned, title, company, full_name)
            words = _word_count(cleaned)

            if ok:
                log.info(
                    f"[CL] Generated for {title} @ {company} | "
                    f"{words} words | {model} | attempt {i}"
                )
                return cleaned, model

            log.warning(
                f"[CL] Attempt {i} ({tier}, temp={temp}) rejected: "
                f"{reason} | {words} words | {model}"
            )
            last_cleaned, last_model, last_reason = cleaned, model, reason

        # === C2: soft-pass removed ===========================================
        # Previous soft-pass shipped letters that failed validation if they
        # merely had acceptable length + no placeholder/contact leaks. This
        # allowed letters to ship without mentioning the role title, which
        # looks bot-like to recruiters. Now: always hard-fail on validation
        # failure. The caller will log ERROR:cover_letter and skip.
        raise CoverLetterError(
            f"Failed to produce usable cover letter for {title} @ {company} "
            f"after {len(attempts)} attempts. Last reason: {last_reason}"
        )

    # ------------------------------------------------------------ validation
    def _validate(self, text, title, company, full_name):
        """Returns (ok: bool, reason: str)."""
        if not text:
            return False, "empty output"

        words = _word_count(text)
        if words < self.MIN_WORDS:
            return False, f"too short ({words} words)"
        if words > self.MAX_WORDS:
            return False, f"too long ({words} words)"

        if _has_placeholder_leak(text):
            return False, "unresolved placeholder in output"

        lower = text.lower()

        # === PATCH 18A: aggregator-in-letter check ============================
        # If any aggregator token appears in the letter body, the scraper
        # mis-identified the employer. Hard-fail.
        for tok in AGGREGATOR_TOKENS:
            if tok in lower:
                return False, f"aggregator name as employer: {tok!r}"
        # === END PATCH 18A ====================================================

        first_line = text.strip().split("\n")[0].strip().lower()

        # Greeting must be present at the top.
        if not first_line.startswith("dear"):
            return False, f"missing greeting (first line: {first_line!r})"

        # Must contain the required opening sentence shape.
        if "i am applying for the" not in lower:
            return False, "missing required opening sentence"

        # Must mention the company and role title.
        if company.lower() not in lower:
            return False, f"company name '{company}' not mentioned"
        # === PATCH 18B: soft title match ==================================
        # Previous logic required the first two significant title words to
        # appear as a consecutive phrase. For "Senior AI/ML Engineer" the
        # filter yielded ['senior', 'engineer'] and required the literal
        # substring "senior engineer" which never appears verbatim (the
        # real title has "AI/ML" between them), rejecting correct letters.
        #
        # New logic: check that N-1 of N significant title words appear
        # as separate words anywhere in the letter. For 2-word titles,
        # both required. For 3+ word titles, N-1 required (tolerates one
        # word being rephrased, dropped, or reformulated).
        title_words = [w for w in re.findall(r"[a-z]+", title.lower()) if len(w) > 2]
        if title_words:
            # Pre-filter: ignore common adjectives in titles that the LLM
            # may legitimately re-phrase (senior -> experienced, lead -> principal etc.)
            # Note: we still require the core-noun words to match.
            required_min = max(1, len(title_words) - 1) if len(title_words) >= 3 else len(title_words)
            # Match as standalone words, not substrings
            matched = sum(
                1 for w in title_words
                if re.search(r"\b" + re.escape(w) + r"\b", lower)
            )
            if matched < required_min:
                missing = [w for w in title_words if not re.search(r"\b" + re.escape(w) + r"\b", lower)]
                return False, (
                    f"role title match insufficient: need {required_min} of "
                    f"{len(title_words)} words, got {matched} "
                    f"(missing: {missing})"
                )
        # === END PATCH 18B ==================================================

        # Sign-off must contain the candidate's name somewhere in the last 4 lines,
        # and a recognised closing phrase must be present.
        tail_lines = text.strip().split("\n")[-4:]
        tail = "\n".join(tail_lines).lower()
        if full_name.lower() not in tail:
            return False, "missing sign-off name"
        if "yours sincerely" not in lower \
                and "kind regards" not in lower \
                and "best regards" not in lower:
            return False, "missing sign-off phrase"

        # Contact block must not have leaked into the sign-off tail.
        if "@" in tail or "linkedin.com" in tail:
            return False, "contact block leaked into sign-off"

        # Tenure-implying phrases (protect against the 'CRITICAL' rule failing).
        for marker in ("during my time here", "in my role here", "my current role at "):
            if marker in lower and marker + company.lower() in lower:
                return False, f"tenure-implying phrase present: {marker!r}"

        # Em/en dashes must have been scrubbed by now.
        if "\u2014" in text or "\u2013" in text:
            return False, "em/en dash present after scrub"

        return True, "ok"
