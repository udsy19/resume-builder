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
    is_valid_length: bool  # 28-32 words
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
            suggestions.append(f"{bullets_wrong_length} bullets are not 28-32 words")
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
            is_valid_length=28 <= word_count <= 32,
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
        """Extract ALL keywords from job analysis for ATS matching."""
        keywords = []
        # Core technical skills
        keywords.extend(job_analysis.get('required_skills', []))
        keywords.extend(job_analysis.get('preferred_skills', []))
        keywords.extend(job_analysis.get('key_technologies', []))
        # Programming and frameworks
        keywords.extend(job_analysis.get('programming_languages', []))
        keywords.extend(job_analysis.get('frameworks_libraries', []))
        # Industry and domain
        keywords.extend(job_analysis.get('industry_terms', []))
        keywords.extend(job_analysis.get('certifications', []))
        # Must-have phrases
        keywords.extend(job_analysis.get('must_include_phrases', []))
        # Deduplicate while preserving order
        seen = set()
        unique_keywords = []
        for k in keywords:
            k_lower = k.lower()
            if k_lower not in seen:
                seen.add(k_lower)
                unique_keywords.append(k)
        return unique_keywords

    async def _analyze_job(self, job_description: str) -> Dict:
        prompt = f"""You are an expert ATS (Applicant Tracking System) keyword extraction specialist. Your task is to perform an EXHAUSTIVE analysis of this job description to extract EVERY possible keyword, phrase, and term that an ATS system would scan for.

=== JOB DESCRIPTION ===
{job_description}

=== EXTRACTION INSTRUCTIONS ===

You MUST extract EVERY term in each category. ATS systems are literal - they scan for exact keyword matches. Missing even ONE important keyword can cause a resume to be filtered out.

EXTRACTION RULES:
1. Extract EXACT phrases as they appear (e.g., "machine learning" not just "ML")
2. Include BOTH the acronym AND full form (e.g., "AWS" AND "Amazon Web Services")
3. Include ALL variations (e.g., "Python", "Python 3", "Python programming")
4. Extract skills from REQUIREMENTS, RESPONSIBILITIES, QUALIFICATIONS, and NICE-TO-HAVES
5. Include implied skills (if they mention "deploy to cloud", include "cloud deployment", "CI/CD", etc.)
6. Extract action verbs EXACTLY as used in the job posting
7. Include industry-specific terminology and jargon
8. Extract certifications mentioned or implied
9. Include soft skills and leadership qualities mentioned
10. Extract company values and culture keywords

=== REQUIRED OUTPUT FORMAT ===

Return a JSON object with these EXACT keys:
{{
    "role_title": "The exact job title as stated",
    "company": "Company name if mentioned",
    "seniority_level": "junior/mid/senior/lead/principal/staff",
    "years_experience": "X+ years or range",

    "required_skills": [
        "EVERY technical skill marked as required",
        "Include programming languages, frameworks, tools",
        "Include methodologies (Agile, Scrum, etc.)",
        "Include both specific versions and general terms"
    ],

    "preferred_skills": [
        "ALL nice-to-have or preferred skills",
        "Skills mentioned as 'bonus' or 'plus'",
        "Skills mentioned in 'ideal candidate' section"
    ],

    "key_technologies": [
        "ALL tools, platforms, software mentioned",
        "Cloud platforms (AWS, GCP, Azure) with specific services",
        "Databases (SQL, PostgreSQL, MongoDB, etc.)",
        "DevOps tools (Docker, Kubernetes, Jenkins, etc.)",
        "Monitoring tools (Splunk, Datadog, etc.)",
        "Security tools if applicable",
        "Include BOTH abbreviations AND full names"
    ],

    "programming_languages": [
        "ALL languages mentioned",
        "Include scripting languages",
        "Include query languages (SQL, GraphQL)"
    ],

    "frameworks_libraries": [
        "ALL frameworks mentioned",
        "ALL libraries mentioned",
        "Frontend frameworks (React, Vue, Angular)",
        "Backend frameworks (Django, Flask, Spring)",
        "Testing frameworks"
    ],

    "soft_skills": [
        "Communication skills mentioned",
        "Leadership qualities",
        "Collaboration/teamwork terms",
        "Problem-solving abilities",
        "Analytical skills"
    ],

    "action_verbs": [
        "EVERY action verb used in responsibilities",
        "Verbs from 'you will...' sections",
        "Verbs from 'responsibilities include...' sections",
        "Examples: design, develop, implement, lead, manage, collaborate, deploy, architect, optimize"
    ],

    "industry_terms": [
        "Domain-specific terminology",
        "Industry jargon",
        "Compliance terms (SOC2, HIPAA, PCI-DSS, etc.)",
        "Methodology terms",
        "Architecture patterns mentioned"
    ],

    "certifications": [
        "ANY certifications mentioned",
        "Implied certifications (if security role, include common security certs)",
        "Cloud certifications",
        "Industry certifications"
    ],

    "key_responsibilities": [
        "Each main responsibility area",
        "Core job functions",
        "Primary deliverables"
    ],

    "culture_keywords": [
        "Company values mentioned",
        "Work environment descriptors",
        "Team culture terms"
    ],

    "must_include_phrases": [
        "Exact multi-word phrases that MUST appear in resume",
        "Technical phrases as written",
        "Methodology names",
        "Specific tool combinations"
    ],

    "education_requirements": [
        "Degree requirements",
        "Field of study preferences",
        "Equivalent experience clauses"
    ]
}}

=== CRITICAL REMINDERS ===
• Be EXHAUSTIVE - ATS systems are literal matchers
• Include SYNONYMS and VARIATIONS of each term
• Extract from EVERY section of the job posting
• Include implied/related skills that would logically be required
• When in doubt, INCLUDE the term - more keywords = better matching

Return ONLY the JSON object, no additional text."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
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
        action_verbs = job_analysis.get('action_verbs', [])

        refinement_note = """
══════════════════════════════════════════════════════════════════════════════
⚠️  CRITICAL: THIS IS A REFINEMENT PASS - PREVIOUS VERSION HAD VIOLATIONS ⚠️
══════════════════════════════════════════════════════════════════════════════

The previous version violated one or more rules. You MUST fix these issues:
• Count EVERY word in EVERY bullet - must be EXACTLY 28-32 words
• Wrap ALL numbers, percentages, dollar amounts in \\textbf{{}}
• Check verb usage - NO action verb can appear more than TWICE total
• Naturally incorporate any missing keywords

Before submitting, VERIFY each bullet by counting words one by one.
""" if is_refinement else ""

        prompt = f"""You are an elite ATS (Applicant Tracking System) resume optimization expert. Your task is to transform this resume to MAXIMIZE ATS keyword matching while following STRICT formatting rules.

══════════════════════════════════════════════════════════════════════════════
                              ORIGINAL RESUME
══════════════════════════════════════════════════════════════════════════════
{resume_latex}

══════════════════════════════════════════════════════════════════════════════
                            TARGET JOB DESCRIPTION
══════════════════════════════════════════════════════════════════════════════
{job_description}

══════════════════════════════════════════════════════════════════════════════
                    KEYWORDS TO INCORPORATE (MUST INCLUDE ALL)
══════════════════════════════════════════════════════════════════════════════
{', '.join(all_keywords)}

══════════════════════════════════════════════════════════════════════════════
                         ACTION VERBS FROM JOB POSTING
══════════════════════════════════════════════════════════════════════════════
{', '.join(action_verbs)}
{refinement_note}
══════════════════════════════════════════════════════════════════════════════
                    ⚡ ABSOLUTE RULES - ZERO TOLERANCE ⚡
══════════════════════════════════════════════════════════════════════════════

RULE 1: BULLET POINT LENGTH (CRITICAL - COUNT EVERY WORD)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Each \\resumeItem MUST contain EXACTLY 28-32 words
• Count contractions as ONE word (don't = 1 word)
• Count hyphenated terms as ONE word (cloud-native = 1 word)
• Numbers count as words (\\textbf{{40\\%}} = 1 word)
• Articles (a, an, the) count as words
• VERIFY: Count each bullet's words before including it

RULE 2: ACTION VERB DIVERSITY (STRICTLY ENFORCED)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Every bullet MUST start with a strong action verb in past tense
• NO action verb can be used MORE than TWICE in the ENTIRE resume
• Track your verb usage as you write:

  VERB BANK (use max 2x each):
  Tier 1 (Strong): Architected, Spearheaded, Pioneered, Orchestrated, Engineered
  Tier 2 (Impact): Transformed, Revolutionized, Accelerated, Maximized, Elevated
  Tier 3 (Technical): Implemented, Deployed, Configured, Integrated, Automated
  Tier 4 (Analysis): Analyzed, Investigated, Researched, Assessed, Evaluated
  Tier 5 (Leadership): Led, Managed, Directed, Supervised, Mentored, Coordinated
  Tier 6 (Creation): Designed, Developed, Built, Created, Established, Launched
  Tier 7 (Improvement): Optimized, Enhanced, Streamlined, Improved, Refined
  Tier 8 (Security): Secured, Protected, Hardened, Fortified, Safeguarded
  Tier 9 (Collaboration): Collaborated, Partnered, Facilitated, Liaised, Advised
  Tier 10 (Execution): Executed, Delivered, Achieved, Accomplished, Completed

RULE 3: QUANTIFICATION (EVERY BULLET MUST HAVE METRICS)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• EVERY bullet MUST contain at least ONE quantified metric
• ALL numbers MUST be wrapped in \\textbf{{}}
• Formats:
  - Percentages: \\textbf{{40\\%}}, \\textbf{{95\\%}}
  - Numbers: \\textbf{{5}}, \\textbf{{100+}}, \\textbf{{25+}}
  - Money: \\textbf{{\\$2M}}, \\textbf{{\\$500K}}
  - Time: \\textbf{{6}} months, \\textbf{{24/7}}
  - Scale: \\textbf{{10,000+}} users, \\textbf{{50+}} endpoints
• If original bullet lacks metrics, ADD realistic ones based on context

RULE 4: KEYWORD INTEGRATION (ATS OPTIMIZATION)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Include ALL keywords from the KEYWORDS section above
• Use EXACT phrases from job description where possible
• Integrate naturally - don't force awkward keyword stuffing
• Technical terms should match job posting exactly
• Include BOTH acronyms AND full forms when space allows

RULE 5: STRUCTURE CONSTRAINTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Maximum {max_experiences} work experiences
• Maximum {max_bullets} bullets per experience
• MUST fit on ONE page
• Preserve the original LaTeX structure and formatting
• Keep all \\resumeSubheading formatting intact

══════════════════════════════════════════════════════════════════════════════
                              EXAMPLE BULLETS
══════════════════════════════════════════════════════════════════════════════

✅ CORRECT (30 words, starts with action verb, has \\textbf{{}} metrics):
\\resumeItem{{Architected and deployed \\textbf{{5}} cloud-native endpoint detection and response solutions across \\textbf{{3}} AWS environments, reducing mean time to detection by \\textbf{{40\\%}} through automated threat correlation and real-time security monitoring dashboards.}}

✅ CORRECT (29 words):
\\resumeItem{{Spearheaded implementation of zero-trust security framework protecting \\textbf{{10,000+}} endpoints across global infrastructure, achieving \\textbf{{99.9\\%}} uptime while reducing unauthorized access attempts by \\textbf{{75\\%}} through continuous multi-factor authentication protocols.}}

✅ CORRECT (31 words):
\\resumeItem{{Led cross-functional team of \\textbf{{8}} engineers to successfully migrate legacy infrastructure to Kubernetes orchestration platform, decreasing deployment time from \\textbf{{4}} hours to \\textbf{{15}} minutes while improving overall system reliability by \\textbf{{60\\%}}.}}

❌ WRONG (only 18 words - TOO SHORT):
\\resumeItem{{Implemented security controls across cloud environments, reducing incidents by 40\\% through automated detection.}}

❌ WRONG (40 words - TOO LONG):
\\resumeItem{{Designed and implemented a comprehensive security monitoring solution that integrated with multiple cloud platforms including AWS and GCP to provide real-time threat detection and automated incident response capabilities for the entire organization while ensuring compliance with industry regulations.}}

❌ WRONG (no \\textbf{{}} around numbers):
\\resumeItem{{Deployed 5 endpoint detection controls across 3 environments, reducing security incidents by 40 percent.}}

❌ WRONG (doesn't start with action verb):
\\resumeItem{{The security team worked on implementing 5 new controls that reduced incidents significantly.}}

══════════════════════════════════════════════════════════════════════════════
                              YOUR TASK
══════════════════════════════════════════════════════════════════════════════

Generate the complete tailored LaTeX resume following ALL rules above.
• Preserve the exact LaTeX document structure
• Transform each bullet to match the target job
• Verify EVERY bullet is 28-32 words by counting
• Ensure EVERY number is wrapped in \\textbf{{}}
• Track verb usage - no verb more than twice

Return ONLY the complete LaTeX document, no explanations or markdown."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}]
        )

        output = response.content[0].text
        output = re.sub(r'```latex\n?|```\n?', '', output)
        return output.strip()

    async def _fix_violations(self, latex: str, validation: ValidationResult, job_analysis: Dict) -> str:
        """Fix specific rule violations in the resume with targeted corrections."""

        # Build detailed issue report
        length_issues = []
        quant_issues = []
        for b in validation.bullet_analyses:
            if not b.is_valid_length:
                direction = "ADD" if b.word_count < 28 else "REMOVE"
                diff = abs(30 - b.word_count)  # Target middle of range
                length_issues.append(
                    f"  • [{b.word_count} words, need 28-32, {direction} ~{diff} words]\n"
                    f"    Current: \"{b.text[:80]}{'...' if len(b.text) > 80 else ''}\""
                )
            if not b.has_quantification:
                quant_issues.append(
                    f"  • Missing \\textbf{{metric}}: \"{b.text[:60]}...\""
                )

        issues_text = ""

        if length_issues:
            issues_text += f"""
══════════════════════════════════════════════════════════════════════════════
              ❌ WORD COUNT VIOLATIONS ({len(length_issues)} bullets)
══════════════════════════════════════════════════════════════════════════════
{chr(10).join(length_issues[:5])}

FIX STRATEGY:
• If too SHORT: Add context, specifics, or additional impact metrics
• If too LONG: Remove filler words, combine phrases, use concise terms
• TARGET: Aim for 30 words (middle of 28-32 range)
"""

        if quant_issues:
            issues_text += f"""
══════════════════════════════════════════════════════════════════════════════
              ❌ MISSING QUANTIFICATION ({len(quant_issues)} bullets)
══════════════════════════════════════════════════════════════════════════════
{chr(10).join(quant_issues[:5])}

FIX STRATEGY:
• Add realistic metrics: \\textbf{{X\\%}}, \\textbf{{Y+}}, \\textbf{{\\$ZM}}
• Every bullet NEEDS at least one \\textbf{{}} wrapped number
• Common metrics: reduction %, team size, users impacted, cost savings, time saved
"""

        if validation.repeated_verbs:
            issues_text += f"""
══════════════════════════════════════════════════════════════════════════════
              ❌ OVERUSED VERBS (max 2x each allowed)
══════════════════════════════════════════════════════════════════════════════
Verbs used too many times: {', '.join(validation.repeated_verbs)}
Current verb counts: {validation.verb_counts}

FIX STRATEGY: Replace with alternatives:
• "Implemented" → Deployed, Configured, Integrated, Established
• "Developed" → Built, Created, Engineered, Designed
• "Managed" → Directed, Supervised, Coordinated, Oversaw
• "Led" → Spearheaded, Headed, Championed, Drove
• "Designed" → Architected, Crafted, Devised, Formulated
"""

        if validation.missing_keywords:
            issues_text += f"""
══════════════════════════════════════════════════════════════════════════════
              ⚠️ MISSING KEYWORDS (ATS optimization)
══════════════════════════════════════════════════════════════════════════════
Must incorporate: {', '.join(validation.missing_keywords[:10])}

FIX STRATEGY:
• Weave keywords naturally into existing bullets
• Replace generic terms with job-specific keywords
• Add technical specifics that match job requirements
"""

        prompt = f"""You are an expert resume editor. Fix the SPECIFIC violations listed below.

{issues_text}

══════════════════════════════════════════════════════════════════════════════
                           CURRENT RESUME TO FIX
══════════════════════════════════════════════════════════════════════════════
{latex}

══════════════════════════════════════════════════════════════════════════════
                              REPAIR INSTRUCTIONS
══════════════════════════════════════════════════════════════════════════════

1. For EACH bullet with wrong word count:
   • Count the current words
   • Adjust to be EXACTLY 28-32 words
   • Verify by recounting before including

2. For EACH bullet missing quantification:
   • Add a realistic metric wrapped in \\textbf{{}}
   • Examples: \\textbf{{40\\%}}, \\textbf{{5}}, \\textbf{{\\$2M}}

3. For EACH overused verb:
   • Replace with a synonym from a different tier
   • Ensure no verb appears more than 2x total

4. For missing keywords:
   • Integrate naturally into relevant bullets
   • Don't force awkward phrasing

Return ONLY the complete fixed LaTeX document. No explanations."""

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
        """Generate actionable suggestions for incorporating missing keywords."""
        if not missing_keywords:
            return []

        prompt = f"""You are an expert resume optimization consultant. Your task is to provide SPECIFIC, ACTIONABLE suggestions for incorporating missing ATS keywords into the resume.

══════════════════════════════════════════════════════════════════════════════
                              CURRENT RESUME
══════════════════════════════════════════════════════════════════════════════
{latex}

══════════════════════════════════════════════════════════════════════════════
                          MISSING KEYWORDS TO ADD
══════════════════════════════════════════════════════════════════════════════
{', '.join(missing_keywords)}

══════════════════════════════════════════════════════════════════════════════
                         JOB CONTEXT & REQUIREMENTS
══════════════════════════════════════════════════════════════════════════════
Role: {job_analysis.get('role_title', 'Not specified')}
Key Responsibilities: {', '.join(job_analysis.get('key_responsibilities', [])[:5])}

══════════════════════════════════════════════════════════════════════════════
                              YOUR TASK
══════════════════════════════════════════════════════════════════════════════

For EACH missing keyword, provide a specific suggestion on how to incorporate it.

REQUIREMENTS FOR EACH SUGGESTION:
1. Identify which EXISTING bullet point could be modified to include the keyword
2. Show the CURRENT bullet text
3. Provide a REWRITTEN bullet that:
   • Includes the missing keyword naturally
   • Is EXACTLY 28-32 words (count carefully!)
   • Maintains \\textbf{{}} around all metrics
   • Starts with a strong action verb
   • Preserves the original meaning/achievement

══════════════════════════════════════════════════════════════════════════════
                           REQUIRED OUTPUT FORMAT
══════════════════════════════════════════════════════════════════════════════

Return a JSON array with this EXACT structure:
[
    {{
        "keyword": "the missing keyword",
        "current_bullet": "the existing bullet text that can be modified",
        "suggested_bullet": "the rewritten bullet WITH the keyword (28-32 words, with \\\\textbf{{}} metrics)",
        "location": "which experience/section contains this bullet",
        "word_count": 30,
        "explanation": "brief note on how the keyword was integrated"
    }}
]

IMPORTANT:
• Provide suggestions for up to 10 keywords
• Each suggested_bullet MUST be 28-32 words - COUNT THEM
• Keep \\textbf{{}} formatting for all numbers
• Make the keyword integration sound natural, not forced
• If a keyword genuinely cannot fit, note that in explanation

Return ONLY the JSON array, no additional text or markdown."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
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
