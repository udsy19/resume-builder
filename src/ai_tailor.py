"""
AI-powered resume tailoring - FAST single-pass version.
One API call does everything: analyze job, generate resume, self-verify.
"""

import os
import re
import json
from typing import Optional, Dict, List, AsyncGenerator, Tuple
from dataclasses import dataclass
from collections import Counter


@dataclass
class BulletAnalysis:
    """Analysis of a single bullet point."""
    text: str
    word_count: int
    action_verb: str
    has_quantification: bool
    is_valid_length: bool


@dataclass
class ValidationResult:
    """Result of resume validation."""
    is_valid: bool
    bullet_analyses: List[BulletAnalysis]
    verb_counts: Dict[str, int]
    repeated_verbs: List[str]
    bullets_wrong_length: int
    bullets_no_quantification: int
    total_bullets: int
    keyword_coverage: float
    matched_keywords: List[str]
    missing_keywords: List[str]
    suggestions: List[str]


@dataclass
class TailoringResult:
    """Result from AI tailoring."""
    tailored_latex: str
    original_latex: str
    match_analysis: Dict
    keyword_coverage: float
    matched_keywords: List[str]
    missing_keywords: List[str]
    suggestions: List[str]
    validation: ValidationResult
    bullet_suggestions: List[Dict]
    diff_data: Dict
    passes_completed: int


class FastValidator:
    """Fast local validation - no API calls."""

    @staticmethod
    def extract_bullets(latex: str) -> List[str]:
        pattern = r'\\resumeItem\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}'
        return re.findall(pattern, latex)

    @staticmethod
    def count_words(bullet: str) -> int:
        clean = re.sub(r'\\textbf\{([^}]*)\}', r'\1', bullet)
        clean = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', clean)
        clean = re.sub(r'[\\$%&]', '', clean)
        return len(clean.split())

    @staticmethod
    def has_metric(bullet: str) -> bool:
        return bool(re.search(r'\\textbf\{[^}]+\}', bullet))

    @staticmethod
    def get_verb(bullet: str) -> str:
        clean = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', bullet)
        words = clean.split()
        return words[0].lower() if words else ""

    @classmethod
    def validate(cls, latex: str, keywords: List[str]) -> ValidationResult:
        bullets = cls.extract_bullets(latex)
        analyses = []
        verb_counts = Counter()

        for b in bullets:
            wc = cls.count_words(b)
            verb = cls.get_verb(b)
            verb_counts[verb] += 1
            analyses.append(BulletAnalysis(
                text=b,
                word_count=wc,
                action_verb=verb,
                has_quantification=cls.has_metric(b),
                is_valid_length=24 <= wc <= 28
            ))

        repeated = [v for v, c in verb_counts.items() if c > 2]
        wrong_len = sum(1 for a in analyses if not a.is_valid_length)
        no_quant = sum(1 for a in analyses if not a.has_quantification)

        # Keyword matching
        latex_lower = latex.lower()
        matched = [k for k in keywords if k.lower() in latex_lower]
        missing = [k for k in keywords if k.lower() not in latex_lower]
        coverage = len(matched) / len(keywords) * 100 if keywords else 100

        suggestions = []
        if wrong_len: suggestions.append(f"{wrong_len} bullets not 24-28 words")
        if no_quant: suggestions.append(f"{no_quant} bullets lack metrics")
        if repeated: suggestions.append(f"Verbs used >2x: {', '.join(repeated)}")

        return ValidationResult(
            is_valid=(wrong_len == 0 and no_quant == 0 and not repeated),
            bullet_analyses=analyses,
            verb_counts=dict(verb_counts),
            repeated_verbs=repeated,
            bullets_wrong_length=wrong_len,
            bullets_no_quantification=no_quant,
            total_bullets=len(analyses),
            keyword_coverage=round(coverage, 1),
            matched_keywords=matched,
            missing_keywords=missing,
            suggestions=suggestions
        )

    @staticmethod
    def estimate_pages(latex: str) -> int:
        """Fast page estimate without compilation."""
        bullets = len(re.findall(r'\\resumeItem\{', latex))
        experiences = len(re.findall(r'\\resumeSubheading\{', latex))

        # Rough heuristic: 15 bullets + 4 experiences = 1 page
        score = bullets * 6 + experiences * 10
        if score <= 100:
            return 1
        elif score <= 180:
            return 2
        return 3


class AIResumeTailorAsync:
    """Fast async resume tailor - ONE main API call."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY required")

        import anthropic
        self.client = anthropic.AsyncAnthropic(api_key=self.api_key)

    async def tailor_with_progress(
        self,
        resume_latex: str,
        job_description: str,
        max_experiences: int = 4,
        max_bullets: int = 4,
        max_passes: int = 3
    ) -> AsyncGenerator[Dict, None]:
        """Fast single-pass tailoring with progress updates."""

        yield {"step": "starting", "message": "Starting fast tailoring...", "progress": 5}

        # Validate inputs
        if not resume_latex or not job_description:
            yield {"step": "error", "message": "Missing resume or job description", "progress": 0}
            return

        # SINGLE API CALL - does everything
        yield {"step": "generating", "message": "Generating optimized resume (single pass)...", "progress": 15}

        try:
            result_json = await self._generate_complete(
                resume_latex, job_description, max_experiences, max_bullets
            )
        except Exception as e:
            yield {"step": "error", "message": f"API error: {str(e)}", "progress": 0}
            return

        tailored = result_json.get("latex", "")
        keywords = result_json.get("keywords", [])
        job_analysis = result_json.get("analysis", {})

        if not tailored:
            yield {"step": "error", "message": "No output generated", "progress": 0}
            return

        yield {"step": "validating", "message": "Validating output...", "progress": 70}

        # Fast local validation
        validation = FastValidator.validate(tailored, keywords)

        # Quick fix pass if needed (uses haiku for speed)
        if not validation.is_valid and validation.bullets_wrong_length > 2:
            yield {"step": "fixing", "message": "Quick fix pass...", "progress": 80}
            tailored = await self._quick_fix(tailored, validation)
            validation = FastValidator.validate(tailored, keywords)

        # Page check (heuristic only - no compilation)
        pages = FastValidator.estimate_pages(tailored)
        if pages > 1:
            yield {"step": "reducing", "message": "Reducing to 1 page...", "progress": 85}
            tailored = await self._reduce_to_one_page(tailored)
            pages = FastValidator.estimate_pages(tailored)

        yield {"step": "complete", "message": "Done!", "progress": 100}

        # Build result
        diff_data = self._create_diff(resume_latex, tailored)

        result = TailoringResult(
            tailored_latex=tailored,
            original_latex=resume_latex,
            match_analysis=job_analysis,
            keyword_coverage=validation.keyword_coverage,
            matched_keywords=validation.matched_keywords,
            missing_keywords=validation.missing_keywords,
            suggestions=validation.suggestions,
            validation=validation,
            bullet_suggestions=[],  # Skip this to save time
            diff_data=diff_data,
            passes_completed=1
        )

        yield {"step": "result", "result": result}

    async def _generate_complete(
        self,
        resume: str,
        job: str,
        max_exp: int,
        max_bullets: int
    ) -> Dict:
        """ONE API call that does everything."""

        prompt = f"""You are an ATS resume optimizer. Do everything in ONE response:
1. Extract keywords from job description
2. Generate tailored resume
3. Self-verify all rules

=== ORIGINAL RESUME ===
{resume}

=== JOB DESCRIPTION ===
{job}

=== STRICT RULES ===
• Each \\resumeItem: EXACTLY 24-28 words (count them!)
• Every bullet starts with action verb (past tense)
• NO verb used more than 2x total
• Every bullet has \\textbf{{metric}} (numbers wrapped in \\textbf{{}})
• Max {max_exp} experiences, {max_bullets} bullets each
• MUST fit 1 page - keep skills section minimal
• DO NOT modify Education or Certifications sections

=== KEYWORD PRIORITY ===
1. Put keywords in Experience/Projects bullets (primary)
2. Only overflow keywords go to Skills section
3. Don't bloat Skills - keep it compact

=== BEFORE OUTPUTTING ===
For EACH bullet, verify:
☐ Count = 24-28 words
☐ Has \\textbf{{number}}
☐ Verb not overused

=== OUTPUT FORMAT ===
Return ONLY this JSON:
{{
  "analysis": {{
    "role_title": "job title",
    "company": "company name"
  }},
  "keywords": ["keyword1", "keyword2", ...],
  "latex": "COMPLETE LaTeX document here"
}}

CRITICAL: The "latex" field must contain the COMPLETE resume from \\documentclass to \\end{{document}}.
Return ONLY the JSON, no markdown or explanation."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}]
        )

        text = response.content[0].text

        # Try to parse JSON
        try:
            # Remove markdown if present
            text = re.sub(r'```json\n?|```\n?', '', text)
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    return json.loads(match.group())
                except:
                    pass
            # Fallback: treat entire response as LaTeX
            return {"latex": text, "keywords": [], "analysis": {}}

    async def _quick_fix(self, latex: str, validation: ValidationResult) -> str:
        """Quick fix using haiku (fast model)."""

        issues = []
        for b in validation.bullet_analyses[:5]:
            if not b.is_valid_length:
                issues.append(f"• {b.word_count} words: \"{b.text[:50]}...\"")

        if not issues:
            return latex

        prompt = f"""Fix these bullets to be 24-28 words each. Keep everything else unchanged.

ISSUES:
{chr(10).join(issues)}

RESUME:
{latex}

Return ONLY the fixed LaTeX, no explanation."""

        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-20250514",  # Fast model
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}]
            )
            output = response.content[0].text
            output = re.sub(r'```latex\n?|```\n?', '', output)
            return output.strip() if output.strip() else latex
        except:
            return latex

    async def _reduce_to_one_page(self, latex: str) -> str:
        """Quick reduction using haiku."""

        prompt = f"""This resume is too long. Make it fit 1 page by:
1. Remove least important skills (keep top 10)
2. Reduce each bullet to exactly 24 words
3. Keep max 3 experiences with 3 bullets each

DO NOT modify Education or Certifications.
Keep all \\textbf{{}} formatting.

{latex}

Return ONLY the reduced LaTeX."""

        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-20250514",
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}]
            )
            output = response.content[0].text
            output = re.sub(r'```latex\n?|```\n?', '', output)
            return output.strip() if output.strip() else latex
        except:
            return latex

    def _create_diff(self, original: str, tailored: str) -> Dict:
        orig = re.findall(r'\\resumeItem\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', original)
        new = re.findall(r'\\resumeItem\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', tailored)
        return {
            "original_bullets": orig,
            "tailored_bullets": new,
            "added": [b for b in new if b not in orig],
            "removed": [b for b in orig if b not in new],
            "modified_count": len([b for b in new if b not in orig])
        }

    async def tailor(
        self,
        resume_latex: str,
        job_description: str,
        max_experiences: int = 4,
        max_bullets: int = 4,
        max_passes: int = 3
    ) -> TailoringResult:
        """Non-streaming version."""
        result = None
        async for update in self.tailor_with_progress(
            resume_latex, job_description, max_experiences, max_bullets, max_passes
        ):
            if update.get("step") == "result":
                result = update["result"]
        return result
