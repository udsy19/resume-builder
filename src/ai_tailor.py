"""
AI-powered resume tailoring using LLM with multi-pass validation.
"""

import os
import re
import json
import asyncio
from typing import Optional, Dict, List, AsyncGenerator, Callable
from dataclasses import dataclass, field
from collections import Counter


@dataclass
class BulletAnalysis:
    """Analysis of a single bullet point."""
    text: str
    word_count: int
    action_verb: str
    has_quantification: bool
    quantifications: List[str]
    is_valid_length: bool  # 24-28 words
    keywords_found: List[str]


@dataclass
class ValidationResult:
    """Result of resume validation."""
    is_valid: bool
    bullet_analyses: List[BulletAnalysis]
    verb_counts: Dict[str, int]
    repeated_verbs: List[str]  # Verbs used more than twice
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
    bullet_suggestions: List[Dict]  # Suggestions for adding missing keywords
    diff_data: Dict  # For diff view
    passes_completed: int


class ResumeValidator:
    """Validates resume against strict formatting rules."""

    ACTION_VERBS = {
        'designed', 'deployed', 'implemented', 'architected', 'developed', 'built',
        'created', 'led', 'managed', 'collaborated', 'optimized', 'automated',
        'configured', 'analyzed', 'researched', 'investigated', 'detected',
        'prevented', 'secured', 'integrated', 'contributed', 'established',
        'executed', 'facilitated', 'generated', 'improved', 'initiated',
        'launched', 'maintained', 'organized', 'performed', 'produced',
        'reduced', 'resolved', 'streamlined', 'supervised', 'transformed',
        'authored', 'conducted', 'coordinated', 'delivered', 'drove',
        'engineered', 'enhanced', 'expanded', 'identified', 'increased',
        'mentored', 'modernized', 'orchestrated', 'pioneered', 'spearheaded'
    }

    def __init__(self, all_keywords: List[str]):
        self.all_keywords = [k.lower() for k in all_keywords]

    def validate(self, latex_content: str) -> ValidationResult:
        """Validate the resume against all rules."""
        bullets = self._extract_bullets(latex_content)
        bullet_analyses = [self._analyze_bullet(b) for b in bullets]

        # Count verb usage
        verb_counts = Counter()
        for analysis in bullet_analyses:
            if analysis.action_verb:
                verb_counts[analysis.action_verb.lower()] += 1

        repeated_verbs = [v for v, c in verb_counts.items() if c > 2]

        # Count violations
        bullets_wrong_length = sum(1 for a in bullet_analyses if not a.is_valid_length)
        bullets_no_quant = sum(1 for a in bullet_analyses if not a.has_quantification)

        # Check keyword coverage
        latex_lower = latex_content.lower()
        matched = [k for k in self.all_keywords if k in latex_lower]
        missing = [k for k in self.all_keywords if k not in latex_lower]
        coverage = len(matched) / len(self.all_keywords) if self.all_keywords else 1.0

        # Generate suggestions
        suggestions = []
        if bullets_wrong_length > 0:
            suggestions.append(f"{bullets_wrong_length} bullets are not 24-28 words")
        if bullets_no_quant > 0:
            suggestions.append(f"{bullets_no_quant} bullets lack quantification")
        if repeated_verbs:
            suggestions.append(f"Verbs used >2 times: {', '.join(repeated_verbs)}")
        if missing:
            suggestions.append(f"Missing keywords: {', '.join(missing[:5])}")

        is_valid = (
            bullets_wrong_length == 0 and
            bullets_no_quant == 0 and
            len(repeated_verbs) == 0 and
            coverage >= 0.9
        )

        return ValidationResult(
            is_valid=is_valid,
            bullet_analyses=bullet_analyses,
            verb_counts=dict(verb_counts),
            repeated_verbs=repeated_verbs,
            bullets_wrong_length=bullets_wrong_length,
            bullets_no_quantification=bullets_no_quant,
            total_bullets=len(bullet_analyses),
            keyword_coverage=round(coverage * 100, 1),
            matched_keywords=matched,
            missing_keywords=missing,
            suggestions=suggestions
        )

    def _extract_bullets(self, latex: str) -> List[str]:
        """Extract bullet point text from LaTeX."""
        # Match \resumeItem{...} pattern
        pattern = r'\\resumeItem\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}'
        matches = re.findall(pattern, latex)
        return matches

    def _analyze_bullet(self, bullet_text: str) -> BulletAnalysis:
        """Analyze a single bullet point."""
        # Clean LaTeX formatting for word count
        clean_text = re.sub(r'\\textbf\{([^}]*)\}', r'\1', bullet_text)
        clean_text = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', clean_text)
        clean_text = re.sub(r'[\\$%&]', '', clean_text)

        words = clean_text.split()
        word_count = len(words)

        # Extract action verb (first word)
        action_verb = words[0].lower().rstrip('ed').rstrip('ing') if words else ""

        # Check for quantification (numbers, percentages, dollar amounts)
        quant_pattern = r'\\textbf\{([^}]*)\}|(\d+[\d,]*[+%]?|\$[\d,]+[KMB]?)'
        quantifications = re.findall(quant_pattern, bullet_text)
        quants = [q[0] or q[1] for q in quantifications if q[0] or q[1]]

        # Find keywords in bullet
        bullet_lower = bullet_text.lower()
        keywords_found = [k for k in self.all_keywords if k in bullet_lower]

        return BulletAnalysis(
            text=bullet_text,
            word_count=word_count,
            action_verb=words[0] if words else "",
            has_quantification=len(quants) > 0,
            quantifications=quants,
            is_valid_length=24 <= word_count <= 28,
            keywords_found=keywords_found
        )


class AIResumeTailorAsync:
    """Async version with streaming progress updates."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable required")

        try:
            import anthropic
            self.client = anthropic.AsyncAnthropic(api_key=self.api_key)
        except ImportError:
            raise ImportError("Please install anthropic: pip install anthropic")

    async def tailor_with_progress(
        self,
        resume_latex: str,
        job_description: str,
        max_experiences: int = 4,
        max_bullets: int = 4,
        max_passes: int = 3
    ) -> AsyncGenerator[Dict, None]:
        """Async multi-pass tailoring with progress updates."""

        # Step 1: Analyze job
        yield {"step": "analyzing", "message": "Analyzing job description...", "progress": 10}
        job_analysis = await self._analyze_job(job_description)
        all_keywords = self._get_all_keywords(job_analysis)
        validator = ResumeValidator(all_keywords)

        yield {
            "step": "analyzed",
            "message": f"Found {len(all_keywords)} keywords to optimize for",
            "progress": 20,
            "data": {"keywords_count": len(all_keywords), "job_title": job_analysis.get("role_title", "")}
        }

        # Step 2: Multi-pass generation
        tailored = ""
        passes = 0
        base_progress = 20
        progress_per_pass = 20

        for pass_num in range(max_passes):
            passes += 1
            current_progress = base_progress + (pass_num * progress_per_pass)

            yield {
                "step": "generating",
                "message": f"Pass {pass_num + 1}/{max_passes}: Generating tailored resume...",
                "progress": current_progress
            }

            tailored = await self._generate_tailored_resume(
                resume_latex if pass_num == 0 else tailored,
                job_description,
                job_analysis,
                max_experiences,
                max_bullets,
                pass_num > 0
            )

            yield {
                "step": "validating",
                "message": f"Pass {pass_num + 1}/{max_passes}: Validating against rules...",
                "progress": current_progress + 10
            }

            validation = validator.validate(tailored)

            if validation.is_valid:
                yield {
                    "step": "validated",
                    "message": "All rules passed!",
                    "progress": 80,
                    "data": {"is_valid": True, "coverage": validation.keyword_coverage}
                }
                break
            else:
                issues = validation.bullets_wrong_length + validation.bullets_no_quantification + len(validation.repeated_verbs)
                yield {
                    "step": "fixing",
                    "message": f"Pass {pass_num + 1}: Found {issues} issues, {'fixing...' if pass_num < max_passes - 1 else 'finalizing...'}",
                    "progress": current_progress + 15,
                    "data": {"issues": issues, "coverage": validation.keyword_coverage}
                }

            if pass_num < max_passes - 1:
                tailored = await self._fix_violations(tailored, validation, job_analysis)

        # Step 3: Final validation
        validation = validator.validate(tailored)

        # Step 4: Generate suggestions
        yield {"step": "suggestions", "message": "Generating improvement suggestions...", "progress": 85}
        bullet_suggestions = await self._generate_bullet_suggestions(
            tailored, validation.missing_keywords, job_analysis
        )

        # Step 5: Create diff
        yield {"step": "diff", "message": "Creating diff view...", "progress": 90}
        diff_data = self._create_diff(resume_latex, tailored)

        # Final result
        yield {"step": "complete", "message": "Done!", "progress": 100}

        result = TailoringResult(
            tailored_latex=tailored,
            original_latex=resume_latex,
            match_analysis=job_analysis,
            keyword_coverage=validation.keyword_coverage,
            matched_keywords=validation.matched_keywords,
            missing_keywords=validation.missing_keywords,
            suggestions=validation.suggestions,
            validation=validation,
            bullet_suggestions=bullet_suggestions,
            diff_data=diff_data,
            passes_completed=passes
        )

        yield {"step": "result", "result": result}

    async def tailor(
        self,
        resume_latex: str,
        job_description: str,
        max_experiences: int = 4,
        max_bullets: int = 4,
        max_passes: int = 3
    ) -> TailoringResult:
        """Non-streaming version for backwards compatibility."""
        result = None
        async for update in self.tailor_with_progress(
            resume_latex, job_description, max_experiences, max_bullets, max_passes
        ):
            if update.get("step") == "result":
                result = update["result"]
        return result

    def _get_all_keywords(self, job_analysis: Dict) -> List[str]:
        keywords = []
        keywords.extend(job_analysis.get('required_skills', []))
        keywords.extend(job_analysis.get('preferred_skills', []))
        keywords.extend(job_analysis.get('key_technologies', []))
        keywords.extend(job_analysis.get('industry_terms', []))
        keywords.extend(job_analysis.get('must_include_phrases', []))
        return keywords

    async def _analyze_job(self, job_description: str) -> Dict:
        prompt = f"""Analyze this job description and extract ALL keywords for ATS optimization.

JOB DESCRIPTION:
{job_description}

Return JSON:
{{
    "role_title": "exact job title",
    "company": "company name",
    "required_skills": ["ALL required technical skills"],
    "preferred_skills": ["ALL preferred/nice-to-have skills"],
    "key_technologies": ["ALL tools, languages, platforms, frameworks"],
    "soft_skills": ["ALL soft/interpersonal skills"],
    "action_verbs": ["ALL action verbs from responsibilities"],
    "industry_terms": ["ALL domain-specific terms"],
    "experience_level": "years required",
    "key_responsibilities": ["each main responsibility"],
    "culture_keywords": ["company culture/values words"],
    "must_include_phrases": ["exact phrases that MUST appear"]
}}

Be EXHAUSTIVE. Return ONLY JSON."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )

        try:
            text = response.content[0].text
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                return json.loads(json_match.group())
            return json.loads(text)
        except json.JSONDecodeError:
            return {"required_skills": [], "preferred_skills": [], "key_technologies": []}

    async def _generate_tailored_resume(
        self,
        resume_latex: str,
        job_description: str,
        job_analysis: Dict,
        max_experiences: int,
        max_bullets: int,
        is_refinement: bool = False
    ) -> str:
        all_keywords = self._get_all_keywords(job_analysis)

        refinement_note = """
IMPORTANT: REFINEMENT pass - previous had violations. Fix:
- Bullets must be 24-28 words
- Numbers must be in \\textbf{}
- No verb >2x
""" if is_refinement else ""

        prompt = f"""Expert ATS optimizer. Create perfectly formatted resume.

ORIGINAL:
{resume_latex}

JOB:
{job_description}

KEYWORDS:
{', '.join(all_keywords)}

VERBS:
{', '.join(job_analysis.get('action_verbs', []))}
{refinement_note}

RULES:
1. Bullets: 24-28 words exactly
2. Verbs: No repeat >2x. Use: designed, deployed, implemented, built, led, managed, collaborated, optimized, automated, analyzed, researched, secured, integrated, developed, created, established, executed, configured, architected, enhanced
3. Metrics: ALL numbers in \\textbf{{}}: \\textbf{{40\\%}}, \\textbf{{25+}}
4. Keywords: ALL must appear naturally
5. Max {max_experiences} exp, {max_bullets} bullets each, ONE page

EXAMPLE (26 words):
\\resumeItem{{Designed and deployed \\textbf{{5}} endpoint detection controls across \\textbf{{3}} cloud-native Linux environments, reducing security incidents by \\textbf{{40\\%}} through automated threat detection.}}

Return ONLY LaTeX."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}]
        )

        output = response.content[0].text
        output = re.sub(r'```latex\n?|```\n?', '', output)
        return output.strip()

    async def _fix_violations(self, latex: str, validation: ValidationResult, job_analysis: Dict) -> str:
        issues = []

        if validation.bullets_wrong_length > 0:
            for b in [a for a in validation.bullet_analyses if not a.is_valid_length][:3]:
                issues.append(f"Bullet ({b.word_count} words): '{b.text[:50]}...'")

        if validation.bullets_no_quantification > 0:
            for b in [a for a in validation.bullet_analyses if not a.has_quantification][:3]:
                issues.append(f"Needs \\textbf{{num}}: '{b.text[:50]}...'")

        if validation.repeated_verbs:
            issues.append(f"Overused verbs: {', '.join(validation.repeated_verbs)}")

        if validation.missing_keywords:
            issues.append(f"Missing: {', '.join(validation.missing_keywords[:5])}")

        prompt = f"""Fix these issues:

{chr(10).join(issues)}

RESUME:
{latex}

RULES: 24-28 words, \\textbf{{numbers}}, no verb >2x

Return ONLY fixed LaTeX."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}]
        )

        output = response.content[0].text
        output = re.sub(r'```latex\n?|```\n?', '', output)
        return output.strip()

    async def _generate_bullet_suggestions(
        self,
        latex: str,
        missing_keywords: List[str],
        job_analysis: Dict
    ) -> List[Dict]:
        if not missing_keywords:
            return []

        prompt = f"""Suggest how to add missing keywords to resume.

RESUME:
{latex}

MISSING:
{', '.join(missing_keywords)}

Return JSON array:
[{{"keyword": "...", "suggestion": "how to incorporate this keyword"}}]

Return ONLY JSON."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )

        try:
            text = response.content[0].text
            json_match = re.search(r'\[[\s\S]*\]', text)
            if json_match:
                return json.loads(json_match.group())
            return []
        except json.JSONDecodeError:
            return []

    def _create_diff(self, original: str, tailored: str) -> Dict:
        orig_bullets = re.findall(r'\\resumeItem\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', original)
        new_bullets = re.findall(r'\\resumeItem\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', tailored)

        return {
            "original_bullets": orig_bullets,
            "tailored_bullets": new_bullets,
            "added": [b for b in new_bullets if b not in orig_bullets],
            "removed": [b for b in orig_bullets if b not in new_bullets],
            "modified_count": len([b for b in new_bullets if b not in orig_bullets])
        }
