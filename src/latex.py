"""
LaTeX compilation: local pdflatex when available, hosted compiler otherwise.

The agent loop uses this to learn the *true* page count instead of guessing,
and to catch documents that don't compile at all.
"""

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ONLINE_COMPILER = "https://latex.ytotech.com/builds/sync"

# TeX installs frequently aren't on a GUI app's PATH. Look where they actually live,
# so we compile locally (fast, private) instead of silently shipping the resume to a
# third-party compiler.
_TEX_SEARCH = (
    "/Library/TeX/texbin", "/usr/local/texlive/2026/bin/universal-darwin",
    "/usr/local/texlive/2025/bin/universal-darwin", "/usr/local/texlive/2024/bin/universal-darwin",
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
)


def find_pdflatex() -> Optional[str]:
    """Locate pdflatex on PATH, via $PDFLATEX, or in a standard TeX install."""
    explicit = os.environ.get("PDFLATEX")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("pdflatex")
    if found:
        return found
    for d in _TEX_SEARCH:
        candidate = Path(d) / "pdflatex"
        if candidate.exists():
            return str(candidate)
    return None

# Commands that read or write outside the build directory.
DANGEROUS = re.compile(
    r"\\write18|\\openin|\\openout|\\immediate\s*\\write"
    r"|\\(?:input|include)\s*\{\s*(?:/|\.\.|[a-zA-Z]:)"
)

_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page[^s]")
_LOG_PAGES_RE = re.compile(r"Output written on .*?\((\d+) pages?")
_OVERFULL_RE = re.compile(r"^(Overfull|Underfull) \\vbox", re.MULTILINE)


@dataclass
class CompileResult:
    ok: bool
    pdf: Optional[bytes] = None
    pages: Optional[int] = None
    error: str = ""
    overfull_vboxes: int = 0
    page_texts: Optional[list] = None   # extracted text per page

    @property
    def fits_one_page(self) -> Optional[bool]:
        return None if self.pages is None else self.pages == 1

    @property
    def overflow_chars(self) -> int:
        """Characters of body text sitting past page 1 — how much must go."""
        if not self.page_texts or len(self.page_texts) < 2:
            return 0
        return sum(len(t.strip()) for t in self.page_texts[1:])

    @property
    def overflow_preview(self) -> str:
        if not self.page_texts or len(self.page_texts) < 2:
            return ""
        spill = " ".join(t.strip() for t in self.page_texts[1:])
        return " ".join(spill.split())[:400]

    @property
    def page1_fill(self) -> Optional[float]:
        """Rough fill ratio of page 1, from how far down the text block reaches."""
        if not self.page_texts:
            return None
        return _fill_ratio(self.pdf) if self.pdf else None


def _page_texts(pdf: bytes) -> Optional[list]:
    try:
        import io
        import pypdf

        return [(p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(pdf)).pages]
    except Exception:
        return None


def _fill_ratio(pdf: bytes) -> Optional[float]:
    """How much of page 1's usable height carries content (0-1), via text bounding boxes."""
    try:
        import io
        import pypdf

        page = pypdf.PdfReader(io.BytesIO(pdf)).pages[0]
        height = float(page.mediabox.height)
        lowest = [height]

        def visit(text, cm, tm, font, size):
            if text and text.strip():
                lowest[0] = min(lowest[0], tm[5])

        page.extract_text(visitor_text=visit)
        # Resume templates start ~0.5in from the top; treat the block as the rest of the page.
        top = height - 36
        used = top - lowest[0]
        usable = top - 36
        return max(0.0, min(1.0, used / usable)) if usable > 0 else None
    except Exception:
        return None


def page_count_from_pdf(pdf: bytes) -> Optional[int]:
    """Count pages in a PDF. Modern pdfTeX compresses the page tree into object
    streams, so a plain byte scan isn't enough — try a parser, then inflate, then scan.
    """
    try:
        import io
        import pypdf

        return len(pypdf.PdfReader(io.BytesIO(pdf)).pages) or None
    except Exception:
        pass

    # No parser available: inflate every FlateDecode stream and scan the objects.
    try:
        import zlib

        count = 0
        for match in re.finditer(rb"stream\r?\n", pdf):
            start = match.end()
            end = pdf.find(b"endstream", start)
            if end == -1:
                continue
            try:
                inflated = zlib.decompress(pdf[start:end])
            except zlib.error:
                continue
            count += len(_PDF_PAGE_RE.findall(inflated))
            for c in re.findall(rb"/Type\s*/Pages[^>]*?/Count\s+(\d+)", inflated):
                return int(c)
        if count:
            return count
    except Exception:
        pass

    return len(_PDF_PAGE_RE.findall(pdf)) or None


def _first_latex_error(log: str) -> str:
    """Pull the first real TeX error out of a compile log."""
    for line in log.splitlines():
        if line.startswith("!"):
            return line.strip()[:300]
    return "LaTeX compilation failed."


def compile_local(latex: str, timeout: int = 40) -> Optional[CompileResult]:
    """Compile with pdflatex if it exists anywhere; None when it doesn't."""
    binary = find_pdflatex()
    if not binary:
        return None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tex_path = Path(tmpdir) / "resume.tex"
            tex_path.write_text(latex, encoding="utf-8")
            env = {**os.environ, "openin_any": "p", "openout_any": "p", "TEXMFOUTPUT": tmpdir}
            subprocess.run(
                [binary, "-no-shell-escape", "-interaction=nonstopmode",
                 "-output-directory", tmpdir, str(tex_path)],
                capture_output=True, text=True, timeout=timeout, env=env,
            )
            pdf_path = Path(tmpdir) / "resume.pdf"
            log_path = Path(tmpdir) / "resume.log"
            log = log_path.read_text(errors="replace") if log_path.exists() else ""
            if not pdf_path.exists():
                return CompileResult(ok=False, error=_first_latex_error(log))
            pdf = pdf_path.read_bytes()
            match = _LOG_PAGES_RE.search(log)
            pages = int(match.group(1)) if match else page_count_from_pdf(pdf)
            return CompileResult(ok=True, pdf=pdf, pages=pages,
                                 overfull_vboxes=len(_OVERFULL_RE.findall(log)),
                                 page_texts=_page_texts(pdf))
    except subprocess.TimeoutExpired:
        return CompileResult(ok=False, error="LaTeX compilation timed out.")


async def compile_online(latex: str, timeout: float = 45.0) -> CompileResult:
    """Compile via the hosted compiler (content leaves this server)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                ONLINE_COMPILER,
                json={"compiler": "pdflatex", "resources": [{"main": True, "content": latex}]},
            )
    except httpx.HTTPError as e:
        return CompileResult(ok=False, error=f"Could not reach the LaTeX compiler ({e.__class__.__name__}).")

    if response.status_code in (200, 201) and response.content[:5] == b"%PDF-":
        return CompileResult(ok=True, pdf=response.content,
                             pages=page_count_from_pdf(response.content),
                             page_texts=_page_texts(response.content))

    detail = ""
    try:
        body = response.json()
        logs = body.get("logs") or body.get("log") or ""
        detail = _first_latex_error(logs if isinstance(logs, str) else str(logs))
    except Exception:
        detail = "LaTeX compilation failed."
    return CompileResult(ok=False, error=detail)


async def compile_pdf(latex: str) -> CompileResult:
    """Compile with whatever is available. Never raises."""
    if DANGEROUS.search(latex):
        return CompileResult(ok=False, error="Document contains file-access commands that aren't allowed.")
    local = compile_local(latex)
    if local is not None:
        return local
    return await compile_online(latex)


# ── Guaranteed one-page fit ──────────────────────────────────────────
# When the model can't land one page, trim deterministically: drop the lowest-value
# bullet (last bullet of the last section that has more than one) and recompile.
# Never leaves a heading with zero bullets.

_BULLET_SPAN = re.compile(
    r"[ \t]*\\(?:resumeItem|resumeSubItem)\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}"
    r"(?:\s*\{(?:[^{}]|\{[^{}]*\})*\})?[ \t]*\n?"
)
_SECTION_SPLIT = re.compile(r"\\section\s*\{")


def _trimmable_bullets(latex: str):
    """Removable bullet spans, ordered worst-to-keep first.

    A section is never emptied, and Skills/Certifications are trimmed only as a last
    resort — a skills row carries many ATS keywords, while a trailing project bullet
    carries the least unique value.
    """
    bounds = [(m.start(), (m.group(1) or "").strip().lower()) for m in
              re.finditer(r"\\section\s*\{([^}]*)\}", latex)]
    if not bounds:
        return []
    edges = [b[0] for b in bounds] + [len(latex)]

    def rank(name: str) -> int:
        # Cut in ascending order of value to the application.
        if "project" in name or "activit" in name or "honor" in name or "leadership" in name:
            return 1          # a trailing project bullet is the cheapest loss
        if "experience" in name or "employment" in name or "work" in name:
            return 2          # real employment evidence
        if "skill" in name or "certification" in name or "award" in name:
            return 3          # dense ATS keyword payload
        return 4              # education and anything unrecognized: leave alone

    candidates = []
    for i, (start, name) in enumerate(bounds):
        end = edges[i + 1]
        found = [(m.start(), m.end()) for m in _BULLET_SPAN.finditer(latex, start, end)]
        if len(found) <= 1:
            continue          # keep at least one bullet per section
        for order, (a, b) in enumerate(reversed(found[1:])):
            # later bullets in a section are cut before earlier ones
            candidates.append((rank(name), order, a, b))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return [(a, b) for _, _, a, b in candidates]


async def fit_to_one_page(latex: str, compile_fn, max_removals: int = 14):
    """Remove lowest-value bullets until the document compiles to one page.

    Returns (latex, removals_made, final_compile_result).
    """
    current = latex
    removed = 0
    result = await compile_fn(current)
    if result.ok and result.pages == 1:
        return current, 0, result

    for _ in range(max_removals):
        spans = _trimmable_bullets(current)
        if not spans:
            break
        start, end = spans[0]         # lowest-value removable bullet
        candidate = current[:start] + current[end:]
        trial = await compile_fn(candidate)
        if not trial.ok:
            break                     # never ship something that stopped compiling
        current, result, removed = candidate, trial, removed + 1
        if trial.pages == 1:
            break
    return current, removed, result


# ── Headroom measurement (TeX's own page arithmetic) ─────────────────
# PDF ink geometry saturates: a document with 4 bullets of slack and one with none
# both read "99% full". TeX's page arithmetic does not. headroom_pt is linear in
# points and tells us exactly how much room is left, or how much overflows.

PROBE = Path(__file__).parent / "fitprobe.tex"

_FITSUM_RE = re.compile(r"FITSUM (\w+)=([-\d.]+)pt")
_FITPOS_RE = re.compile(r"FITPOS (\d+) page=(\S+) posy=(\S+)")
LINE_COST_PT = 12.0          # one rendered line of a resume bullet
BULLET_BASE_PT = 2.0         # per-bullet overhead on top of its lines
CHARS_PER_LINE = 127         # measured for these templates at 10-11pt


@dataclass
class FitReport:
    ok: bool
    pages: Optional[int] = None
    headroom_pt: Optional[float] = None      # >0 room left, <0 overflow
    bullet_pages: Optional[dict] = None      # bullet index -> page it landed on
    error: str = ""

    @property
    def fits(self) -> bool:
        return bool(self.ok and self.pages == 1)

    @property
    def spare_lines(self) -> Optional[float]:
        return None if self.headroom_pt is None else round(self.headroom_pt / LINE_COST_PT, 1)


def bullet_cost_pt(text_chars: int) -> float:
    """What one bullet of this length costs vertically."""
    import math
    return BULLET_BASE_PT + LINE_COST_PT * max(1, math.ceil(text_chars / CHARS_PER_LINE))


def measure_fit(latex: str, timeout: int = 45) -> FitReport:
    """Compile with the probe injected and read TeX's page arithmetic.

    Requires a local pdflatex; returns ok=False when unavailable so callers can
    fall back to page counting.
    """
    binary = find_pdflatex()
    if not binary or not PROBE.exists():
        return FitReport(ok=False, error="local pdflatex unavailable")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tex = Path(tmpdir) / "doc.tex"
            tex.write_text(latex, encoding="utf-8")
            shutil.copy(PROBE, Path(tmpdir) / "fitprobe.tex")
            env = {**os.environ, "openin_any": "p", "openout_any": "p", "TEXMFOUTPUT": tmpdir}
            # Two passes: zref positions are only correct once the .aux is written.
            for _ in range(2):
                subprocess.run(
                    [binary, "-no-shell-escape", "-interaction=nonstopmode",
                     "-output-directory", tmpdir,
                     r"\input{fitprobe.tex}\input{doc.tex}"],
                    capture_output=True, text=True, timeout=timeout, env=env, cwd=tmpdir,
                )
            log_path = Path(tmpdir) / "doc.log"
            if not log_path.exists():
                log_path = next(Path(tmpdir).glob("*.log"), None)
            if not log_path:
                return FitReport(ok=False, error="probe produced no log")
            log = log_path.read_text(errors="replace")

            vals = {k: float(v) for k, v in _FITSUM_RE.findall(log)}
            pages = None
            m = _LOG_PAGES_RE.search(log)
            if m:
                pages = int(m.group(1))
            else:
                # The probe run writes its PDF under the first \input's name, so the log
                # line can be missing; read the page count off the artifact instead.
                produced = sorted(Path(tmpdir).glob("*.pdf"))
                if produced:
                    pages = page_count_from_pdf(produced[0].read_bytes())
            bullet_pages = {}

            headroom = None
            if "textheight" in vals and "lastpagetotal" in vals:
                headroom = vals["textheight"] + vals.get("lastpageshrink", 0.0) - vals["lastpagetotal"]
                if pages and pages > 1:
                    # Overflow: express what spilled past page 1 as negative headroom.
                    headroom = -(vals["lastpagetotal"] + (pages - 1) * vals["textheight"] * 0.12)
            return FitReport(ok=True, pages=pages, headroom_pt=headroom, bullet_pages=bullet_pages)
    except subprocess.TimeoutExpired:
        return FitReport(ok=False, error="probe timed out")
    except Exception as e:
        return FitReport(ok=False, error=f"probe failed: {e.__class__.__name__}")


def skeleton_line_budget(template_latex: str) -> Optional[float]:
    """How many bullet lines this template's fixed furniture leaves room for.

    Strips every bullet from the template, measures the remaining overhead, and
    converts the leftover height into lines. Gives the writer a concrete budget
    before it writes a word — numeric budgets work where exhortation does not.
    """
    skeleton = _BULLET_SPAN.sub("", template_latex)
    report = measure_fit(skeleton)
    if not report.ok or report.headroom_pt is None:
        return None
    return round(report.headroom_pt / LINE_COST_PT, 1)


# ── Bullet-level parsing for deterministic length control ────────────

_ARG = r"\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}"
_BULLET_CONTENT = re.compile(r"\\(?:resumeItem|resumeSubItem)\s*" + _ARG + r"(\s*" + _ARG + r")?")


def parse_bullets(latex: str):
    """Every real bullet with its editable span and length, in document order.

    Three things this has to get right, each learned from a template that broke it:
      - only the body counts (preamble \newcommand bodies contain literal `#1`)
      - commented-out blocks are not bullets (udaya ships many; they inflated the count
        from 24 to 56 and would have been "tightened" pointlessly)
      - \resumeItem takes one argument in most templates but two in sb2nov, where the
        first is a bold title and the *second* holds the prose worth tightening
    """
    body_start = latex.find(r"\begin{document}")
    offset = body_start if body_start != -1 else 0
    out = []
    for m in _BULLET_CONTENT.finditer(latex, offset):
        # Skip anything inside a LaTeX comment.
        line_start = latex.rfind("\n", 0, m.start()) + 1
        prefix = latex[line_start:m.start()]
        if re.search(r"(?<!\\)%", prefix):
            continue
        # Two-argument form: edit the description, not the title.
        if m.group(2) is not None:
            start, end, text = m.start(3), m.end(3), m.group(3)
        else:
            start, end, text = m.start(1), m.end(1), m.group(1)
        if text and text.strip() and "#" not in text[:3]:
            out.append({"start": start, "end": end, "text": text, "chars": len(text)})
    return out


def replace_bullets(latex: str, replacements: dict) -> str:
    """Replace bullet bodies by index (from parse_bullets), right-to-left so spans hold."""
    bullets = parse_bullets(latex)
    for idx in sorted(replacements, reverse=True):
        if 0 <= idx < len(bullets):
            b = bullets[idx]
            latex = latex[: b["start"]] + replacements[idx] + latex[b["end"]:]
    return latex


def line_arithmetic(latex: str, one_line_chars: int = 127):
    """(bullet count, two-liner count, total rendered lines) — the page arithmetic."""
    bullets = parse_bullets(latex)
    two = sum(1 for b in bullets if b["chars"] > one_line_chars)
    return len(bullets), two, len(bullets) + two


def bullets_to_tighten(latex: str, lines_over: float, one_line_chars: int = 127):
    """Pick enough two-line bullets to reclaim `lines_over` rendered lines.

    Driven by MEASURED headroom, not by a modelled line count: the writer changes the
    document's furniture (roles, project titles, section count), so any budget derived
    from the empty template drifts. Compiled headroom is ground truth.

    Converting a two-line bullet into a one-line bullet reclaims exactly one line, so we
    need ceil(lines_over) candidates, preferring those closest to the boundary — they
    need the fewest characters cut and so survive tightening with the least damage.
    """
    import math
    if lines_over <= 0:
        return []
    need = math.ceil(lines_over)
    bullets = parse_bullets(latex)
    over = [(i, b) for i, b in enumerate(bullets) if b["chars"] > one_line_chars]
    over.sort(key=lambda p: p[1]["chars"])
    return [{"index": i, "chars": b["chars"], "text": b["text"], "target": one_line_chars}
            for i, b in over[:need]]


def bullets_to_expand(latex: str, lines_free: float, one_line_chars: int = 127):
    """Pick one-line bullets that can absorb the free lines, shortest first.

    A one-line bullet grown past 127 characters becomes a two-line bullet, consuming
    exactly one free line. The shortest bullets have the most room to say something
    substantive, so they are expanded first.
    """
    import math
    if lines_free < 1:
        return []
    need = int(math.floor(lines_free))
    bullets = parse_bullets(latex)
    room = [(i, b) for i, b in enumerate(bullets) if b["chars"] <= one_line_chars]
    room.sort(key=lambda p: p[1]["chars"])
    return [{"index": i, "chars": b["chars"], "text": b["text"],
             "target": min(240, one_line_chars * 2 - 14)} for i, b in room[:need]]


def bullets_with_widows(latex: str, one_line_chars: int = 127, min_tail: int = 45):
    """Bullets whose final rendered line holds only a few words.

    A bullet of 132 characters wraps to two lines with ~5 characters on the second —
    the "widow" the judges flag on nearly every run ("NumPy alone on its own line").
    Detected from character counts rather than rendered geometry, so it needs no probe:
    the tail is chars beyond the last full line, and a short tail is a widow.
    """
    out = []
    for i, b in enumerate(parse_bullets(latex)):
        if b["chars"] <= one_line_chars:
            continue
        tail = b["chars"] % one_line_chars
        if 0 < tail < min_tail:
            out.append({"index": i, "chars": b["chars"], "text": b["text"],
                        "tail": tail,
                        # Pull it back under the line boundary: cheapest, always removes the widow.
                        "target": one_line_chars * (b["chars"] // one_line_chars)})
    out.sort(key=lambda w: w["tail"])
    return out


# ── Deterministic craft defects ──────────────────────────────────────
# The evaluator's two most common writing_quality complaints are mechanically
# detectable, which matters: research found that critique items backed by a
# deterministic check are the only ones worth feeding back — model-judged items
# are a coin flip with negative expected value.

_BOLD_RE = re.compile(r"\\textbf\s*\{")
_DIGIT_RE = re.compile(r"\d")
_SCALE_WORDS = re.compile(
    r"\b(?:daily|weekly|monthly|quarterly|annually|per\s+\w+|across|company-wide|"
    r"organization-wide|enterprise-wide|end-to-end)\b", re.I)


# Not every parsed item is an achievement bullet. Skills rows, project title lines and
# education metadata are legitimately metric-free — demanding an outcome from
# "Detection & Response: SIEM, EDR, Sysmon" is nonsense, and early versions of this
# check spent most of its budget doing exactly that.
_NOT_AN_ACHIEVEMENT = re.compile(
    # A skills row: "\textbf{Security Engineering:} A, B, C" or "\textbf{Languages}: A, B"
    r"^\s*\\textbf\s*\{[^}]*:\s*\}"
    r"|^\s*\\textbf\s*\{[^}]*\}\s*(?::|\{\s*:)"
    # A project title line: "\textbf{Name} | \textit{Subtitle}"
    r"|^\s*\\textbf\s*\{[^}]*\}\s*\$?\|"
    # Education / credential metadata rather than an achievement
    r"|^\s*(?:Focus|Minor|GPA|Courses?|Relevant\s+Coursework|Certifications?|Honors?|Awards?)\b\s*:"
    r"|\b(?:CompTIA|EC-Council|Dean's List)\b",
    re.I)


def is_achievement_bullet(text: str) -> bool:
    """True when this item is a claim about work done, rather than a list or a label."""
    return not _NOT_AN_ACHIEVEMENT.search(text.strip())


def bullets_missing_outcome(latex: str):
    """Achievement bullets that describe activity but report no result.

    "Facilitated hands-on labs on traffic analysis" states what was done and never what
    changed. A bullet with no digit anywhere — and no scale word standing in for one —
    cannot be reporting an outcome.
    """
    out = []
    for i, b in enumerate(parse_bullets(latex)):
        text = b["text"]
        if not is_achievement_bullet(text):
            continue
        if _DIGIT_RE.search(text) or _SCALE_WORDS.search(text):
            continue
        out.append({"index": i, "chars": b["chars"], "text": text,
                    "problem": "no metric or scale signal — states activity, not outcome"})
    return out


def bullets_overbolded(latex: str, limit: int = 3):
    """Bullets with so much bold that emphasis stops meaning anything.

    One observed bullet carried five \\textbf spans; the rule is 2-3, reserved for the
    strongest metric and the strongest JD keyword.
    """
    out = []
    for i, b in enumerate(parse_bullets(latex)):
        if not is_achievement_bullet(b["text"]):
            continue
        n = len(_BOLD_RE.findall(b["text"]))
        if n > limit:
            out.append({"index": i, "chars": b["chars"], "text": b["text"], "bolds": n,
                        "problem": f"{n} bold spans — keep at most {limit}, "
                                   "the strongest metric and the strongest keyword"})
    return out


def craft_defects(latex: str):
    """All mechanically-detectable craft defects, worst first.

    Includes VMock's own Impact-module rules, so a regression that reintroduces filler
    adverbs, pronouns or weak openers is caught and repaired rather than argued about.
    """
    found = bullets_missing_outcome(latex) + bullets_overbolded(latex)
    for d in vmock_defects(latex):
        for prob in d["problems"]:
            found.append({"index": d["index"], "chars": d["chars"], "text": d["text"], "problem": prob})
    by_index = {}
    for d in found:
        cur = by_index.setdefault(d["index"], dict(d, problems=[]))
        cur["problems"].append(d["problem"])
    return sorted(by_index.values(), key=lambda d: -len(d["problems"]))


# ── VMock rule checks ────────────────────────────────────────────────
# Straight from VMock's published scoring modules (Impact / Presentation). These are the
# criteria the product is actually judged against, and nearly all of them are mechanical,
# so they are checked rather than described to the model and hoped for.

# Impact > Avoided Words: adverbs VMock flags as padding.
_FILLER_ADVERBS = re.compile(
    r"\b(?:successfully|effectively|independently|efficiently|actively|greatly|highly|"
    r"significantly|substantially|extensively|thoroughly|closely|quickly|easily|simply|"
    r"various|numerous|several|really|quite|truly|actually)\b", re.I)

# Impact > Avoided Words: pronouns.
_PRONOUNS = re.compile(r"\b(?:I|we|me|my|our|us|myself|ourselves)\b")

# Impact > Action-Oriented: VMock flags bullets opening on a noun phrase or a weak verb.
_WEAK_OPENERS = re.compile(
    r"^\s*(?:responsible\s+for|worked|helped|assisted|participated|involved|tasked|"
    r"duties|a|an|the)\b", re.I)

# Presentation > Date Formatting: the formats VMock accepts.
_DATE_OK = re.compile(
    r"(?:Expected\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}"
    r"|\d{2}/\d{2}"
    r"|(?:Fall|Spring|Summer|Winter)\s+\d{4}"
    r"|^\s*\d{4}\s*$")
_DATE_SEPARATOR_BAD = re.compile(r"\d{4}\s*(?:-|—)\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d)")


def vmock_defects(latex: str):
    """Bullet-level violations of VMock's Impact rules, worst first."""
    out = []
    for i, b in enumerate(parse_bullets(latex)):
        text = b["text"]
        if not is_achievement_bullet(text):
            continue
        plain = re.sub(r"\\[a-zA-Z]+\s*\{([^{}]*)\}", r"\1", text)
        problems = []
        fillers = sorted({m.group(0).lower() for m in _FILLER_ADVERBS.finditer(plain)})
        if fillers:
            problems.append(f"filler word(s) VMock flags as padding: {', '.join(fillers)} — delete them")
        prons = sorted({m.group(0) for m in _PRONOUNS.finditer(plain)})
        if prons:
            problems.append(f"pronoun(s) {', '.join(prons)} — resumes omit them")
        if _WEAK_OPENERS.match(plain):
            problems.append("opens on a weak verb or an article — VMock requires a strong action verb first")
        if problems:
            out.append({"index": i, "chars": b["chars"], "text": text, "problems": problems})
    return sorted(out, key=lambda d: -len(d["problems"]))


def date_format_defects(latex: str):
    """Date strings that fall outside VMock's accepted formats.

    VMock is strict here and says inconsistent dates are the most common problem it sees.
    It wants an en dash between endpoints ("Jun -- Aug 2025"), not a hyphen.
    """
    body = re.sub(r"(?<!\\\\)%.*", "", latex)
    marker = r"\begin{document}"
    body = body[body.find(marker):] if marker in body else body
    problems = []
    for m in _DATE_SEPARATOR_BAD.finditer(body):
        problems.append({"text": m.group(0),
                         "problem": "date range uses a hyphen; VMock expects an en dash (--)"})
    return problems
