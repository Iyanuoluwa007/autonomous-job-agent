"""
Fit Scorer — uses fast model (step-3.5-flash) to score job relevance 0-100.
Saves expensive Kimi K2 calls for cover letter generation only.
"""

import re, logging
log = logging.getLogger("fit_scorer")

SYSTEM = """You are a strict job-fit scoring assistant. Score how well the candidate's
resume matches the job description using this rubric:

  85-100: Resume directly matches >70% of the JD's required skills AND role type
  60-84 : Resume matches most core skills, some gaps, role type aligned
  40-59 : Partial match, adjacent field, meaningful skill gaps
  20-39 : Wrong role family but some transferable skills
   0-19 : Completely wrong field or no overlap

Be strict. If the role family is wrong (e.g. retail, admin, care, hospitality)
versus an engineering resume, score below 30 even if a few keywords coincide.

Output ONLY a JSON object: {"score": <int 0-100>, "reason": "<one sentence>"}
No markdown, no preamble, no code fences. Just the raw JSON."""


class FitScorer:
    def __init__(self, nim_client, resume_text: str):
        self.nim    = nim_client
        self.resume = resume_text

    def score(self, job_title: str, company: str, jd_text: str) -> int:
        user = f"""RESUME:\n{self.resume[:3000]}\n\nJOB: {job_title} at {company}\n\nJD:\n{jd_text[:3000]}"""
        try:
            raw, _ = self.nim.complete_system(SYSTEM, user, tier="fast", max_tokens=150, temperature=0.2)
            # Strip markdown fences if model ignores instructions
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            import json
            data = json.loads(raw)
            score = int(data.get("score", 50))
            log.info(f"[FIT] {job_title} @ {company} => {score} | {data.get('reason','')}")
            return max(0, min(100, score))
        except Exception as e:
            log.warning(f"[FIT ERR] {e} — raw: {raw if 'raw' in dir() else 'N/A'}")
            # Fallback: keyword match
            return self._keyword_fallback(jd_text)

    def _keyword_fallback(self, jd: str) -> int:
        # Flat safe score — well below any reasonable min_fit_score threshold.
        # Rationale: keyword overlap on long resumes can inflate to 60-90+ for
        # totally irrelevant JDs (e.g. "experience" and "service" match retail).
        # Role filter in agent.py already rejects wrong titles upstream, so this
        # fallback only fires on real engineering roles where the fast model died,
        # and on those we want a conservative skip, not a confident auto-apply.
        log.warning("[FIT] fallback engaged -> flat 20")
        return 20
