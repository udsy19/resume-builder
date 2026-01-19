"""
AI-powered resume tailoring using LLM with multi-pass validation.
Enhanced with robust error handling, retry logic, and smart keyword matching.
"""

import os
import re
import json
import asyncio
import random
from typing import Optional, Dict, List, AsyncGenerator, Callable, Tuple
from dataclasses import dataclass, field
from collections import Counter


# ═══════════════════════════════════════════════════════════════════════════════
#                          KEYWORD MATCHING UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

class KeywordMatcher:
    """Smart keyword matching with stemming and synonym detection."""

    # Common word endings to strip for stemming
    SUFFIXES = ['ing', 'ed', 'er', 'tion', 'ment', 'ness', 'ity', 'ies', 's']

    # Synonym groups for common tech terms
    SYNONYMS = {
        'javascript': ['js', 'javascript', 'ecmascript'],
        'typescript': ['ts', 'typescript'],
        'python': ['python', 'python3', 'py'],
        'kubernetes': ['kubernetes', 'k8s', 'kube'],
        'amazon web services': ['aws', 'amazon web services'],
        'google cloud platform': ['gcp', 'google cloud', 'google cloud platform'],
        'microsoft azure': ['azure', 'microsoft azure'],
        'ci/cd': ['ci/cd', 'cicd', 'ci cd', 'continuous integration', 'continuous deployment'],
        'machine learning': ['ml', 'machine learning'],
        'artificial intelligence': ['ai', 'artificial intelligence'],
        'devops': ['devops', 'dev ops', 'dev-ops'],
        'api': ['api', 'apis', 'rest api', 'restful api'],
        'sql': ['sql', 'mysql', 'postgresql', 'postgres'],
        'nosql': ['nosql', 'mongodb', 'dynamodb', 'cassandra'],
        'docker': ['docker', 'containerization', 'containers'],
    }

    @classmethod
    def stem(cls, word: str) -> str:
        """Simple stemming - remove common suffixes."""
        word = word.lower().strip()
        for suffix in cls.SUFFIXES:
            if word.endswith(suffix) and len(word) > len(suffix) + 2:
                return word[:-len(suffix)]
        return word

    @classmethod
    def get_synonyms(cls, keyword: str) -> List[str]:
        """Get all synonyms for a keyword."""
        keyword_lower = keyword.lower()
        for group in cls.SYNONYMS.values():
            if keyword_lower in group:
                return group
        return [keyword_lower]

    @classmethod
    def matches(cls, keyword: str, text: str) -> bool:
        """Check if keyword matches in text (with stemming and synonyms)."""
        text_lower = text.lower()

        # Direct match
        if keyword.lower() in text_lower:
            return True

        # Synonym match
        for synonym in cls.get_synonyms(keyword):
            if synonym in text_lower:
                return True

        # Stemmed match (for single words)
        if ' ' not in keyword:
            keyword_stem = cls.stem(keyword)
            words_in_text = text_lower.split()
            for word in words_in_text:
                if cls.stem(word) == keyword_stem:
                    return True

        return False

    @classmethod
    def count_matches(cls, keywords: List[str], text: str) -> Tuple[List[str], List[str]]:
        """Return matched and unmatched keywords."""
        matched = []
        unmatched = []
        for keyword in keywords:
            if cls.matches(keyword, text):
                matched.append(keyword)
            else:
                unmatched.append(keyword)
        return matched, unmatched


# ═══════════════════════════════════════════════════════════════════════════════
#                              RETRY UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

async def retry_with_backoff(
    func,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = (Exception,)
):
    """Retry a function with exponential backoff."""
    last_exception = None

    for attempt in range(max_retries):
        try:
            return await func()
        except exceptions as e:
            last_exception = e
            if attempt < max_retries - 1:
                delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                await asyncio.sleep(delay)

    raise last_exception


# ═══════════════════════════════════════════════════════════════════════════════
#                            LATEX VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

class LaTeXValidator:
    """Validate LaTeX content for common issues."""

    @staticmethod
    def validate(content: str) -> Tuple[bool, List[str]]:
        """Validate LaTeX and return (is_valid, list of issues)."""
        issues = []

        if not content or not content.strip():
            return False, ["Empty content"]

        # Check for document structure
        if r'\begin{document}' not in content:
            issues.append("Missing \\begin{document}")
        if r'\end{document}' not in content:
            issues.append("Missing \\end{document}")

        # Check for balanced braces
        open_braces = content.count('{')
        close_braces = content.count('}')
        if open_braces != close_braces:
            issues.append(f"Unbalanced braces: {open_braces} open, {close_braces} close")

        # Check for common escape issues
        if '\\%' not in content and '%' in content.replace('%%', ''):
            # Might have unescaped percent signs
            pass  # This is often fine in LaTeX comments

        # Check for truncated content
        if content.rstrip().endswith('...'):
            issues.append("Content appears truncated")

        # Check for minimum structure
        if r'\resumeItem' not in content and r'\section' not in content:
            issues.append("Missing resume structure elements")

        return len(issues) == 0, issues

    @staticmethod
    def sanitize_input(content: str) -> str:
        """Sanitize input LaTeX content."""
        if not content:
            return ""

        # Remove null bytes
        content = content.replace('\x00', '')

        # Normalize line endings
        content = content.replace('\r\n', '\n').replace('\r', '\n')

        # Remove excessive whitespace while preserving structure
        lines = content.split('\n')
        cleaned_lines = []
        for line in lines:
            # Don't strip lines that are intentionally empty (for paragraph breaks)
            if line.strip() or (cleaned_lines and cleaned_lines[-1].strip()):
                cleaned_lines.append(line.rstrip())

        return '\n'.join(cleaned_lines)

    @staticmethod
    def extract_sections(content: str) -> Dict[str, str]:
        """Extract major sections from LaTeX resume."""
        sections = {}

        # Common section patterns
        section_patterns = [
            (r'education', r'\\section\{[Ee]ducation\}(.*?)(?=\\section|\\end\{document\})'),
            (r'experience', r'\\section\{[Ee]xperience\}(.*?)(?=\\section|\\end\{document\})'),
            (r'projects', r'\\section\{[Pp]rojects\}(.*?)(?=\\section|\\end\{document\})'),
            (r'skills', r'\\section\{[Ss]kills\}(.*?)(?=\\section|\\end\{document\})'),
            (r'certifications', r'\\section\{[Cc]ertifications?\}(.*?)(?=\\section|\\end\{document\})'),
        ]

        for name, pattern in section_patterns:
            match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
            if match:
                sections[name] = match.group(1).strip()

        return sections


# ═══════════════════════════════════════════════════════════════════════════════
#                          PAGE COUNT VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

class PageCounter:
    """Check if LaTeX compiles to exactly one page."""

    @staticmethod
    async def check_page_count(latex_content: str) -> Tuple[int, bool, str]:
        """
        Compile LaTeX and check page count.
        Returns: (page_count, success, error_message)
        """
        import tempfile
        import subprocess
        import shutil
        from pathlib import Path

        # Check if pdflatex is available locally
        pdflatex_available = shutil.which("pdflatex") is not None

        if pdflatex_available:
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    tex_path = Path(tmpdir) / "resume.tex"
                    tex_path.write_text(latex_content)

                    result = subprocess.run(
                        ["pdflatex", "-interaction=nonstopmode", "-output-directory", tmpdir, str(tex_path)],
                        capture_output=True,
                        text=True,
                        timeout=30
                    )

                    log_path = Path(tmpdir) / "resume.log"
                    pdf_path = Path(tmpdir) / "resume.pdf"

                    if pdf_path.exists() and log_path.exists():
                        log_content = log_path.read_text()
                        # Look for page count in log
                        page_match = re.search(r'Output written on .+ \((\d+) page', log_content)
                        if page_match:
                            pages = int(page_match.group(1))
                            return pages, True, ""
                        return 1, True, ""  # Assume 1 if can't parse
                    else:
                        return 0, False, result.stderr[:200]

            except subprocess.TimeoutExpired:
                return 0, False, "Compilation timeout"
            except Exception as e:
                return 0, False, str(e)
        else:
            # Use online compiler for page check
            try:
                import httpx

                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        "https://latex.ytotech.com/builds/sync",
                        json={
                            "compiler": "pdflatex",
                            "resources": [{"main": True, "content": latex_content}]
                        }
                    )

                    if response.status_code == 200:
                        # PDF compiled successfully - estimate pages from size
                        # Rough heuristic: single page resume PDF is typically 30-80KB
                        pdf_size = len(response.content)
                        estimated_pages = max(1, pdf_size // 60000)  # ~60KB per page estimate
                        return estimated_pages, True, ""
                    else:
                        return 0, False, response.text[:200]

            except Exception as e:
                return 0, False, str(e)

    @staticmethod
    def estimate_page_count_heuristic(latex_content: str) -> int:
        """
        Estimate page count without compilation (fast heuristic).
        Based on content analysis.
        """
        # Count resume items
        bullet_count = len(re.findall(r'\\resumeItem\{', latex_content))

        # Count experiences
        experience_count = len(re.findall(r'\\resumeSubheading\{', latex_content))

        # Count sections
        section_count = len(re.findall(r'\\section\{', latex_content))

        # Estimate based on typical resume layouts
        # Typical 1-page resume: 3-4 experiences, 12-16 bullets, 4-5 sections
        content_score = (
            bullet_count * 2.5 +  # Each bullet ~2.5% of page
            experience_count * 5 +  # Each experience header ~5%
            section_count * 3  # Each section header ~3%
        )

        # Also consider raw content length
        # Typical 1-page resume LaTeX is ~3000-5000 chars of content
        doc_start = latex_content.find(r'\begin{document}')
        doc_end = latex_content.find(r'\end{document}')
        if doc_start != -1 and doc_end != -1:
            content_length = doc_end - doc_start
            length_score = content_length / 4000 * 100  # 4000 chars = 100%
        else:
            length_score = 0

        # Combine scores
        total_score = max(content_score, length_score)

        if total_score <= 100:
            return 1
        elif total_score <= 200:
            return 2
        else:
            return 3


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

    def __init__(self, all_keywords: List[str], prioritized_keywords: Dict[str, List[str]] = None):
        self.all_keywords = [k.lower() for k in all_keywords]
        self.prioritized_keywords = prioritized_keywords or {}

    def validate(self, latex_content: str) -> ValidationResult:
        """Validate the resume against all rules."""
        bullets = self._extract_bullets(latex_content)
        bullet_analyses = [self._analyze_bullet(b) for b in bullets]

        # Count verb usage
        verb_counts = Counter()
        for analysis in bullet_analyses:
            if analysis.action_verb:
                # Normalize verb (handle past tense)
                verb = analysis.action_verb.lower().rstrip('ed').rstrip('d')
                verb_counts[verb] += 1

        repeated_verbs = [v for v, c in verb_counts.items() if c > 2]

        # Count violations
        bullets_wrong_length = sum(1 for a in bullet_analyses if not a.is_valid_length)
        bullets_no_quant = sum(1 for a in bullet_analyses if not a.has_quantification)

        # Check keyword coverage using smart matching
        matched, missing = KeywordMatcher.count_matches(self.all_keywords, latex_content)
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

        # Find keywords in bullet using smart matching
        keywords_found, _ = KeywordMatcher.count_matches(self.all_keywords, bullet_text)

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

        # Step 0: Validate and sanitize input
        yield {"step": "validating_input", "message": "Validating input...", "progress": 5}

        # Sanitize inputs
        resume_latex = LaTeXValidator.sanitize_input(resume_latex)
        job_description = job_description.strip() if job_description else ""

        # Validate resume LaTeX
        is_valid, issues = LaTeXValidator.validate(resume_latex)
        if not is_valid:
            yield {
                "step": "error",
                "message": f"Invalid LaTeX input: {', '.join(issues)}",
                "progress": 0
            }
            return

        if not job_description:
            yield {
                "step": "error",
                "message": "Job description is empty",
                "progress": 0
            }
            return

        # Step 1: Analyze job
        yield {"step": "analyzing", "message": "Analyzing job description...", "progress": 10}

        try:
            job_analysis = await self._analyze_job(job_description)
        except Exception as e:
            yield {
                "step": "error",
                "message": f"Failed to analyze job description: {str(e)}",
                "progress": 0
            }
            return

        all_keywords = self._get_all_keywords(job_analysis)
        prioritized = self._get_prioritized_keywords(job_analysis)
        validator = ResumeValidator(all_keywords, prioritized)

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

        # Step 3: Post-generation verification pass
        yield {"step": "verifying", "message": "Running verification pass...", "progress": 75}
        tailored = await self._verify_and_fix_output(tailored, validator)

        # Step 4: Enforce one-page limit (recursive)
        yield {"step": "checking_pages", "message": "Checking page count...", "progress": 78}

        # Quick heuristic check first
        estimated_pages = PageCounter.estimate_page_count_heuristic(tailored)

        if estimated_pages > 1:
            yield {
                "step": "reducing",
                "message": f"Resume exceeds 1 page (estimated {estimated_pages}). Reducing content...",
                "progress": 80
            }

            tailored, final_pages, success = await self._enforce_one_page(tailored, max_attempts=3)

            if success:
                yield {
                    "step": "page_fixed",
                    "message": "Resume reduced to 1 page!",
                    "progress": 83,
                    "data": {"pages": final_pages}
                }
            else:
                yield {
                    "step": "page_warning",
                    "message": f"Warning: Resume may still be {final_pages} pages. Manual reduction may be needed.",
                    "progress": 83,
                    "data": {"pages": final_pages}
                }
        else:
            yield {
                "step": "page_ok",
                "message": "Resume fits on 1 page!",
                "progress": 83,
                "data": {"pages": 1}
            }

        # Step 5: Final validation
        validation = validator.validate(tailored)

        # Step 6: Generate suggestions
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
        # Core technical skills (highest priority)
        keywords.extend(job_analysis.get('required_skills', []))
        keywords.extend(job_analysis.get('must_include_phrases', []))
        # Key technologies
        keywords.extend(job_analysis.get('key_technologies', []))
        keywords.extend(job_analysis.get('programming_languages', []))
        keywords.extend(job_analysis.get('frameworks_libraries', []))
        # Preferred skills (medium priority)
        keywords.extend(job_analysis.get('preferred_skills', []))
        # Industry and domain
        keywords.extend(job_analysis.get('industry_terms', []))
        keywords.extend(job_analysis.get('certifications', []))
        # Deduplicate while preserving order
        seen = set()
        unique_keywords = []
        for k in keywords:
            if not k or not isinstance(k, str):
                continue
            k_lower = k.lower().strip()
            if k_lower and k_lower not in seen:
                seen.add(k_lower)
                unique_keywords.append(k)
        return unique_keywords

    def _get_prioritized_keywords(self, job_analysis: Dict) -> Dict[str, List[str]]:
        """Get keywords organized by priority for smarter placement."""
        return {
            'critical': (
                job_analysis.get('required_skills', []) +
                job_analysis.get('must_include_phrases', [])
            ),
            'high': (
                job_analysis.get('key_technologies', []) +
                job_analysis.get('programming_languages', [])
            ),
            'medium': (
                job_analysis.get('preferred_skills', []) +
                job_analysis.get('frameworks_libraries', [])
            ),
            'low': (
                job_analysis.get('industry_terms', []) +
                job_analysis.get('certifications', [])
            )
        }

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
• Count EVERY word in EVERY bullet - must be EXACTLY 24-28 words
• Wrap ALL numbers, percentages, dollar amounts in \\textbf{{}}
• Check verb usage - NO action verb can appear more than TWICE total
• Naturally incorporate any missing keywords into EXPERIENCE and PROJECTS only
• DO NOT modify EDUCATION or CERTIFICATIONS sections
• DO NOT bloat SKILLS section - only add overflow keywords
• MUST fit on ONE page - remove skills if needed to fit

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
• Each \\resumeItem MUST contain EXACTLY 24-28 words
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

RULE 4: KEYWORD INTEGRATION (ATS OPTIMIZATION - STRICT PRIORITY ORDER)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRIORITY 1: Integrate keywords into EXPERIENCE section bullets (primary)
PRIORITY 2: Integrate keywords into PROJECTS section bullets (secondary)
PRIORITY 3: ONLY add remaining keywords to SKILLS section if they could NOT fit naturally in experience/projects

• Use EXACT phrases from job description where possible
• Integrate naturally - don't force awkward keyword stuffing
• Technical terms should match job posting exactly
• DO NOT bloat the skills section with every keyword - most should be in bullets

RULE 5: PROTECTED SECTIONS (DO NOT MODIFY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• ⛔ EDUCATION section: Copy EXACTLY as-is, NO changes whatsoever
• ⛔ CERTIFICATIONS section: Copy EXACTLY as-is, NO changes whatsoever
• ⛔ Contact/Header info: Copy EXACTLY as-is, NO changes
• ✅ EXPERIENCE section: MODIFY bullets to incorporate keywords
• ✅ PROJECTS section: MODIFY bullets to incorporate keywords
• ✅ SKILLS section: May add ONLY overflow keywords that couldn't fit elsewhere

RULE 6: STRUCTURE CONSTRAINTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Maximum {max_experiences} work experiences
• Maximum {max_bullets} bullets per experience
• ⚠️ MUST fit on ONE page - this is NON-NEGOTIABLE
• DO NOT add excessive skills that would push content to page 2
• Preserve the original LaTeX structure and formatting
• Keep all \\resumeSubheading formatting intact
• If adding a skill would cause page overflow, DO NOT add it

══════════════════════════════════════════════════════════════════════════════
                              EXAMPLE BULLETS
══════════════════════════════════════════════════════════════════════════════

✅ CORRECT (26 words, starts with action verb, has \\textbf{{}} metrics):
\\resumeItem{{Architected and deployed \\textbf{{5}} cloud-native endpoint detection solutions across \\textbf{{3}} AWS environments, reducing mean time to detection by \\textbf{{40\\%}} through automated threat correlation.}}

✅ CORRECT (25 words):
\\resumeItem{{Spearheaded implementation of zero-trust security framework protecting \\textbf{{10,000+}} endpoints, achieving \\textbf{{99.9\\%}} uptime while reducing unauthorized access attempts by \\textbf{{75\\%}} through continuous authentication.}}

✅ CORRECT (27 words):
\\resumeItem{{Led cross-functional team of \\textbf{{8}} engineers to migrate legacy infrastructure to Kubernetes, decreasing deployment time from \\textbf{{4}} hours to \\textbf{{15}} minutes while improving reliability by \\textbf{{60\\%}}.}}

❌ WRONG (only 18 words - TOO SHORT):
\\resumeItem{{Implemented security controls across cloud environments, reducing incidents by 40\\% through automated detection.}}

❌ WRONG (35 words - TOO LONG):
\\resumeItem{{Designed and implemented a comprehensive security monitoring solution that integrated with multiple cloud platforms including AWS and GCP to provide real-time threat detection and automated incident response capabilities for the entire organization.}}

❌ WRONG (no \\textbf{{}} around numbers):
\\resumeItem{{Deployed 5 endpoint detection controls across 3 environments, reducing security incidents by 40 percent.}}

❌ WRONG (doesn't start with action verb):
\\resumeItem{{The security team worked on implementing 5 new controls that reduced incidents significantly.}}

══════════════════════════════════════════════════════════════════════════════
                              YOUR TASK
══════════════════════════════════════════════════════════════════════════════

STEP-BY-STEP PROCESS (follow this exactly):

STEP 1: ANALYZE THE ORIGINAL RESUME
• Identify all sections (Education, Experience, Projects, Skills, Certifications)
• Note which sections are PROTECTED (Education, Certifications - copy exactly)
• Count current bullets per experience

STEP 2: PLAN KEYWORD PLACEMENT
• List the top 20 most important keywords from the job
• For each keyword, identify which EXPERIENCE or PROJECT bullet can incorporate it
• Plan verb usage - assign different verbs to each bullet (no repeats >2x)

STEP 3: WRITE EACH BULLET WITH VERIFICATION
For each bullet, do this:
a) Write the bullet
b) Count the words: "Word 1, Word 2, Word 3..." until you reach the end
c) Verify count is 24-28. If not, adjust and recount.
d) Verify it has \\textbf{{number}}. If not, add one.
e) Check verb hasn't been used >2x. If so, replace.

STEP 4: FINAL VERIFICATION
Before outputting, verify:
☐ EDUCATION section is UNCHANGED from original
☐ CERTIFICATIONS section is UNCHANGED from original
☐ Header/contact info is UNCHANGED
☐ Every bullet is 24-28 words (you counted each one)
☐ Every number is wrapped in \\textbf{{}}
☐ No action verb appears more than 2 times total
☐ Keywords are in EXPERIENCE/PROJECTS, not bloating skills
☐ Resume will fit on ONE PAGE

══════════════════════════════════════════════════════════════════════════════
                           SELF-VERIFICATION EXAMPLE
══════════════════════════════════════════════════════════════════════════════

Before including this bullet, I verify:
"Architected and deployed \\textbf{{5}} cloud-native endpoint detection solutions..."

Word count: Architected(1) and(2) deployed(3) \\textbf{{5}}(4) cloud-native(5) endpoint(6)
detection(7) solutions(8)... = 30 words ✓

Has \\textbf{{}}: Yes, \\textbf{{5}} ✓
Verb "Architected": Used 1 time total ✓

INCLUDE THIS BULLET.

══════════════════════════════════════════════════════════════════════════════

Return ONLY the complete LaTeX document, no explanations or markdown."""

        async def make_request():
            return await self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}]
            )

        # Retry with exponential backoff on API errors
        response = await retry_with_backoff(
            make_request,
            max_retries=3,
            base_delay=2.0,
            exceptions=(Exception,)
        )

        output = response.content[0].text
        output = re.sub(r'```latex\n?|```\n?', '', output)
        return output.strip()

    async def _enforce_one_page(
        self,
        latex: str,
        max_attempts: int = 3
    ) -> Tuple[str, int, bool]:
        """
        Recursively reduce content until resume fits on one page.
        Returns: (latex, page_count, success)
        """
        current_latex = latex

        for attempt in range(max_attempts):
            # First try fast heuristic estimate
            estimated_pages = PageCounter.estimate_page_count_heuristic(current_latex)

            if estimated_pages == 1:
                # Heuristic says 1 page - verify with actual compilation if possible
                page_count, success, _ = await PageCounter.check_page_count(current_latex)
                if success and page_count == 1:
                    return current_latex, 1, True
                elif success and page_count > 1:
                    # Need to reduce more
                    pass
                else:
                    # Compilation failed, trust heuristic
                    return current_latex, 1, True

            # Need to reduce content
            current_latex = await self._reduce_content_for_one_page(current_latex, attempt + 1)

        # Final check
        page_count, success, _ = await PageCounter.check_page_count(current_latex)
        if not success:
            page_count = PageCounter.estimate_page_count_heuristic(current_latex)

        return current_latex, page_count, page_count == 1

    async def _reduce_content_for_one_page(self, latex: str, attempt: int) -> str:
        """
        Reduce content to fit on one page using AI.
        Strategy depends on attempt number.
        """
        strategies = {
            1: "Remove the least important skills from the skills section. Keep only the top 10-12 most relevant skills.",
            2: "Reduce each bullet point to exactly 24 words (minimum of range). Remove any project if there are more than 2.",
            3: "Remove the oldest/least relevant experience entirely. Keep only 3 experiences with 3 bullets each."
        }

        strategy = strategies.get(attempt, strategies[3])

        prompt = f"""The resume below is TOO LONG and exceeds 1 page. You MUST reduce its content.

CURRENT RESUME:
{latex}

REDUCTION STRATEGY (Attempt {attempt}):
{strategy}

STRICT RULES:
• DO NOT modify EDUCATION section
• DO NOT modify CERTIFICATIONS section
• Keep all \\textbf{{}} formatting
• Each bullet must still be 24-28 words
• Resume MUST fit on ONE page after this reduction

Apply the reduction strategy and return the complete LaTeX document.
Return ONLY the LaTeX, no explanations."""

        async def make_request():
            return await self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}]
            )

        try:
            response = await retry_with_backoff(make_request, max_retries=2)
            output = response.content[0].text
            output = re.sub(r'```latex\n?|```\n?', '', output)
            return output.strip()
        except Exception:
            return latex  # Return original if reduction fails

    async def _verify_and_fix_output(self, latex: str, validator: ResumeValidator) -> str:
        """Post-generation verification pass to catch any remaining issues."""
        validation = validator.validate(latex)

        # If mostly valid, return as-is
        if validation.is_valid or (
            validation.bullets_wrong_length <= 1 and
            validation.bullets_no_quantification == 0 and
            len(validation.repeated_verbs) == 0
        ):
            return latex

        # Otherwise, do a quick targeted fix
        issues = []
        for b in validation.bullet_analyses:
            if not b.is_valid_length:
                issues.append(f"Bullet has {b.word_count} words (need 24-28): \"{b.text[:50]}...\"")

        if not issues:
            return latex

        prompt = f"""Fix ONLY these word count issues. Keep everything else the same.

ISSUES:
{chr(10).join(issues[:5])}

RESUME:
{latex}

Rules:
• Adjust each flagged bullet to EXACTLY 24-28 words
• Keep all other bullets unchanged
• Keep all \\textbf{{}} formatting
• Keep Education/Certifications unchanged

Return ONLY the fixed LaTeX."""

        async def make_request():
            return await self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}]
            )

        try:
            response = await retry_with_backoff(make_request, max_retries=2)
            output = response.content[0].text
            output = re.sub(r'```latex\n?|```\n?', '', output)
            return output.strip()
        except Exception:
            # If verification fix fails, return original
            return latex

    async def _fix_violations(self, latex: str, validation: ValidationResult, job_analysis: Dict) -> str:
        """Fix specific rule violations in the resume with targeted corrections."""

        # Build detailed issue report
        length_issues = []
        quant_issues = []
        for b in validation.bullet_analyses:
            if not b.is_valid_length:
                direction = "ADD" if b.word_count < 24 else "REMOVE"
                diff = abs(26 - b.word_count)  # Target middle of range
                length_issues.append(
                    f"  • [{b.word_count} words, need 24-28, {direction} ~{diff} words]\n"
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
• TARGET: Aim for 26 words (middle of 24-28 range)
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
   • Adjust to be EXACTLY 24-28 words
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
   • Is EXACTLY 24-28 words (count carefully!)
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
        "suggested_bullet": "the rewritten bullet WITH the keyword (24-28 words, with \\\\textbf{{}} metrics)",
        "location": "which experience/section contains this bullet",
        "word_count": 26,
        "explanation": "brief note on how the keyword was integrated"
    }}
]

IMPORTANT:
• Provide suggestions for up to 10 keywords
• Each suggested_bullet MUST be 24-28 words - COUNT THEM
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
