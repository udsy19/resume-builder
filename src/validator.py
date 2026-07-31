"""
Fast local LaTeX resume checks — no API calls. Results are fed into the LLM
evaluator as ground-truth counts and drive the refinement loop.
"""

import re
from dataclasses import dataclass, field
from collections import Counter
from statistics import median
from typing import Dict, List, Optional, Tuple

BANNED_VERBS = {"utilized", "leveraged", "helped", "assisted", "worked", "responsible"}

SYNONYMS = [
    {"js", "javascript", "ecmascript"},
    {"ts", "typescript"},
    {"k8s", "kubernetes", "kube"},
    {"aws", "amazon web services"},
    {"gcp", "google cloud", "google cloud platform"},
    {"azure", "microsoft azure"},
    {"ci/cd", "cicd", "ci cd", "continuous integration", "continuous deployment"},
    {"ml", "machine learning"},
    {"ai", "artificial intelligence"},
    {"api", "apis", "rest api", "restful api"},
    {"iac", "infrastructure-as-code", "infrastructure as code"},
    {"iam", "identity and access management"},
    {"sast", "static analysis", "static application security testing"},
    {"dast", "dynamic analysis", "dynamic application security testing"},
]

TIERS = ("must_have", "nice_to_have")


def keyword_in_text(keyword: str, text_lower: str) -> bool:
    kw = keyword.lower().strip()
    if not kw:
        return False
    if kw in text_lower:
        return True
    for group in SYNONYMS:
        if kw in group and any(syn in text_lower for syn in group):
            return True
    # Multi-word phrases: count as present when every significant word appears.
    words = [w for w in re.split(r"[\s/&,-]+", kw) if len(w) > 3]
    if len(words) >= 3 and all(w in text_lower for w in words):
        return True
    return False


@dataclass
class TierCoverage:
    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.matched) + len(self.missing)

    @property
    def coverage(self) -> float:
        return round(len(self.matched) / self.total * 100, 1) if self.total else 100.0


@dataclass
class LocalChecks:
    total_bullets: int = 0
    repeated_verbs: List[str] = field(default_factory=list)
    banned_verbs_used: List[str] = field(default_factory=list)
    bullets_without_metric: int = 0
    long_bullets: List[str] = field(default_factory=list)
    keywords: Dict[str, TierCoverage] = field(default_factory=dict)
    unsupported: List[str] = field(default_factory=list)  # requirements the candidate genuinely lacks
    pages: Optional[int] = None            # real page count when compiled
    overflow_chars: int = 0                # body text sitting past page 1
    overflow_preview: str = ""
    page1_fill: Optional[float] = None     # 0-1, how full page 1 is
    pages_estimated: int = 1               # heuristic fallback
    compile_ok: Optional[bool] = None
    compile_error: str = ""
    overfull_vboxes: int = 0
    latex_balanced: bool = True
    skills_row_count: int = 0
    skills_row_overflows: List[Dict] = field(default_factory=list)  # rows wrapping to an orphan line
    imbalanced_groups: List[Dict] = field(default_factory=list)     # thin blocks vs. their siblings

    @property
    def must_have(self) -> TierCoverage:
        return self.keywords.get("must_have", TierCoverage())

    def summary(self) -> str:
        lines = [f"- {self.total_bullets} bullets found"]

        if self.compile_ok is False:
            lines.append(f"- ⚠ THE DOCUMENT DOES NOT COMPILE: {self.compile_error}")
        elif self.pages is not None and self.pages > 1:
            lines.append(
                f"- ⚠ MEASURED PAGE COUNT: {self.pages} pages. This is a HARD FAILURE.\n"
                f"  EXACTLY {self.overflow_chars} characters of body text sit past page 1. To fit one page you must "
                f"remove AT LEAST {self.overflow_chars + 200} characters of body text (roughly "
                f"{max(1, round((self.overflow_chars + 200) / 190))} full bullet(s), or the equivalent in tightened wording "
                f"and merged bullets). This is a measurement, not an estimate — cutting less will not fit.\n"
                f"  The text currently spilling onto page {self.pages} is: {self.overflow_preview}"
            )
        elif self.pages == 1:
            fill = f"{round(self.page1_fill * 100)}%" if self.page1_fill is not None else "unknown"
            note = "well filled" if (self.page1_fill or 0) >= 0.9 else (
                "UNDER-FILLED — add substance to reach ~95%" if self.page1_fill is not None else "")
            lines.append(f"- Compiled page count (measured, authoritative): 1 page — fill {fill} {note}")
            if self.overfull_vboxes:
                lines.append(f"- {self.overfull_vboxes} overfull/underfull vbox warning(s) — layout is strained")
        else:
            lines.append(f"- Page count could not be measured; rough estimate: {self.pages_estimated}")

        for tier in TIERS:
            cov = self.keywords.get(tier)
            if not cov or not cov.total:
                continue
            label = {"must_have": "Must-have", "nice_to_have": "Nice-to-have"}[tier]
            lines.append(
                f"- {label} keyword coverage (dump-supported only): {cov.coverage}% ({len(cov.matched)}/{cov.total})"
                + (f" — STILL MISSING: {', '.join(cov.missing[:20])}" if cov.missing else "")
            )
        if self.unsupported:
            lines.append(
                "- NOT IN THE CANDIDATE'S BACKGROUND (verified against the dump — their absence is CORRECT "
                f"and must NOT be penalized, and they must NOT be added): {', '.join(self.unsupported[:25])}"
            )

        lines += [
            f"- Action verbs used more than once: {', '.join(self.repeated_verbs) or 'none'}",
            f"- Banned weak verbs used: {', '.join(self.banned_verbs_used) or 'none'}",
            f"- Bullets without a \\textbf{{}} metric/emphasis: {self.bullets_without_metric}",
        ]
        if self.long_bullets:
            lines.append(f"- Bullets likely spilling past 2 lines (>210 chars): {len(self.long_bullets)}")

        # Measured layout defects. Phrased as instructions with the exact number of
        # characters/bullets involved, because that is the only form of page_fit
        # feedback that survives a rewrite — "tighten this row" does not.
        if self.skills_row_overflows:
            lines.append(
                f"- ⚠ {len(self.skills_row_overflows)} of {self.skills_row_count} skills rows WRAP TO AN ORPHAN "
                "LINE (rendered line width measured for this template). Each wastes a full line of "
                "page height on a couple of words. Fix each by DELETING trailing entries — do not reword:"
            )
            for row in self.skills_row_overflows[:6]:
                lines.append(
                    f"  · \"{row['label']}\" row is {row['chars']} chars and renders as {row['lines']} lines; "
                    f"the last one holds only {row['orphan_words']} word(s): \"{row['orphan_text']}\". "
                    f"CUT AT LEAST {row['cut_chars']} characters from this row (target {row['target_chars']} "
                    "chars or fewer) by dropping its least JD-relevant trailing entries."
                )
        if self.imbalanced_groups:
            lines.append(
                f"- ⚠ {len(self.imbalanced_groups)} entr(ies) are VISUALLY THIN — far fewer bullets than the "
                "other entries in the same section, which leaves a hole in the page:"
            )
            for g in self.imbalanced_groups[:6]:
                lines.append(
                    f"  · \"{g['heading']}\" under {g['section']} has {g['bullets']} bullet(s) while its siblings "
                    f"average {g['sibling_median']:g}. ADD {g['add_bullets']} more bullet(s) of the same length "
                    "to it, or merge the entry into a sibling and remove the heading."
                )
        lines.append(f"- LaTeX environments balanced: {self.latex_balanced}")
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "total_bullets": self.total_bullets,
            "repeated_verbs": self.repeated_verbs,
            "banned_verbs_used": self.banned_verbs_used,
            "bullets_without_metric": self.bullets_without_metric,
            "long_bullets": len(self.long_bullets),
            "keywords": {t: {"matched": c.matched, "missing": c.missing, "coverage": c.coverage}
                         for t, c in self.keywords.items()},
            "unsupported": self.unsupported,
            # kept flat for the UI's keyword chips
            "matched_keywords": self.must_have.matched,
            "missing_keywords": self.must_have.missing,
            "keyword_coverage": self.must_have.coverage,
            "pages": self.pages,
            "overflow_chars": self.overflow_chars,
            "page1_fill": self.page1_fill,
            "pages_estimated": self.pages_estimated,
            "compile_ok": self.compile_ok,
            "compile_error": self.compile_error,
            "overfull_vboxes": self.overfull_vboxes,
            "latex_balanced": self.latex_balanced,
            "skills_row_count": self.skills_row_count,
            "skills_row_overflows": self.skills_row_overflows,
            "imbalanced_groups": self.imbalanced_groups,
        }


_BULLET_RE = re.compile(r"\\resumeItem\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}")


def strip_comments(latex: str) -> str:
    """Drop LaTeX comments (unescaped % to end of line) so they don't skew counts."""
    return re.sub(r"(?<!\\)(?<!\\\\)%.*", "", latex)


def extract_bullets(latex: str) -> List[str]:
    return [b for b in _BULLET_RE.findall(strip_comments(latex)) if b.strip()]


_ESCAPES = ((r"\&", "&"), (r"\%", "%"), (r"\$", "$"), (r"\_", "_"), (r"\#", "#"))


def _rendered_text(fragment: str) -> str:
    """The visible text a LaTeX fragment renders to.

    Everything downstream measures *length*, so the two things that skewed it most
    are handled explicitly: an \\href URL is markup rather than ink (counting a GitHub
    URL added ~50 phantom characters to a project bullet), and an escaped character
    like \\& is one glyph, not two. Naive brace-stripping got both wrong.
    """
    s = re.sub(r"\\href\s*\{[^{}]*\}", "", fragment)   # the URL argument renders nothing
    s = re.sub(r"\$\s*\|\s*\$", "|", s)                # the $|$ separator is a single glyph
    s = re.sub(r"\\[a-zA-Z]+\*?", "", s)               # control words: \textbf, \small, \hfill...
    for esc, char in _ESCAPES:
        s = s.replace(esc, char)
    # Braces are dropped, not spaced out: the jakes form \textbf{Languages}{: Git, ...}
    # renders "Languages: Git", and a stray space there both miscounts the row and
    # moves where it wraps.
    s = re.sub(r"[{}$\\]", "", s).replace("~", " ")
    return re.sub(r"\s+", " ", s).strip()


# ── Rendered-layout checks ───────────────────────────────────────────
# These exist because page_fit is the score that stays stuck, and the two defects
# the judges name run after run are both invisible in the source and computable from
# it: a skills row that wraps to a two-word orphan line ("Jupyter alone on its own
# line"), and a role whose bullet count leaves a visually thin block between its
# neighbours ("Teaching Assistant renders as a heading plus a single bullet").
# Critique the model can be *shown a number for* changes behaviour; critique it has
# to judge by eye does not — so both are measured here rather than asked of it.

# The rest of the codebase approximates a rendered line as 127 characters
# (latex.py's CHARS_PER_LINE), which is good enough for prose bullets. It is not here:
# skills rows are dense with acronyms and product names, and in a proportional font
# "AWS, IAM/RBAC, VPC, S3/Blob" is far wider than the same count of lowercase letters.
# Raw character counts cannot even separate the two cases — measured off compiled PDFs,
# a 113-character row fits on one line where a 107-character row breaks. So a row is
# measured in width units instead: uppercase and digits wide, spaces and thin
# punctuation narrow, everything else 1, plus a surcharge for the bold category label.
# The weights and both capacities are fitted to 25 fit/break lines read back from the
# glyph positions of compiled resumes; they classify 24 of the 25 correctly.
WIDE_UNITS = 1.25
NARROW_UNITS = 0.65
NARROW_CHARS = frozenset(" ,.:;()/'|-!itlIj[]")
BOLD_EXTRA = 0.55             # the \textbf category label renders wider than plain text
SMALL_ROW_UNITS = 104.3       # \small{\item{ ... }} skills blocks (jakes, mst)
FULL_ROW_UNITS = 117.5        # full-size \resumeSubItem / \item rows (udaya, sb2nov)
ORPHAN_WORDS = 4              # "1-4 word trailing line" is the judges' own wording
ORPHAN_MARGIN_UNITS = 3.0     # don't claim a defect the width model only just predicts
LINE_SAFETY_CHARS = 6         # break points drift a little; ask for slightly more

_SECTION_RE = re.compile(r"\\section\s*\*?\s*\{([^{}]*)\}")
# Skills live under a different heading in every template, and the writer renames it
# to the JD's own taxonomy, so match on meaning rather than on an exact title.
_SKILLS_SECTION_RE = re.compile(r"skill|technical|programming|competenc|technolog|tooling", re.I)
# Every template spells a skills row differently: \resumeSubItem (udaya), a bare
# \item (sb2nov), and \small{\item{ ... \\ ... }} holding several rows in one group
# (jakes, mst). Matching the container and splitting on \\ covers all four.
_ROW_CMD_RE = re.compile(r"\\(?:resumeSubItem|resumeItem|item)(?![A-Za-z])\s*(?=\{)")
_HEADING_RE = re.compile(
    r"\\(?:resumeSubheading|resumeSubHeading|resumeProjectHeading|resumeItem)(?![A-Za-z])\s*(?=\{)")
_BULLET_CMD_RE = re.compile(r"\\(?:resumeItem|resumeSubItem)(?![A-Za-z])\s*(?=\{)")
_DATE_LIKE_RE = re.compile(
    r"\b(19|20)\d\d\b|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|present", re.I)


def _balanced_arg(text: str, index: int) -> Tuple[str, int]:
    """Contents of the brace group starting at `index`, plus the index just past it."""
    depth = 0
    for i in range(index, len(text)):
        char = text[i]
        if char == "{" and text[i - 1: i] != "\\":
            depth += 1
        elif char == "}" and text[i - 1: i] != "\\":
            depth -= 1
            if depth == 0:
                return text[index + 1:i], i + 1
    return text[index + 1:], len(text)


def _sections(latex: str):
    """(name, body_start, body_end) for every \\section, in document order."""
    marks = list(_SECTION_RE.finditer(latex))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(latex)
        yield _rendered_text(m.group(1)), m.end(), end


def _text_units(text: str) -> float:
    """Rendered width of a string, in the fitted units described above."""
    total = 0.0
    for char in text:
        if char in NARROW_CHARS:
            total += NARROW_UNITS
        elif char.isupper() or char.isdigit():
            total += WIDE_UNITS
        else:
            total += 1.0
    return total


def _wrap_lines(text: str, capacity: float, first_line_penalty: float = 0.0) -> List[str]:
    """Greedy word wrap — the lines TeX will actually break this text into.

    Wrapping properly rather than doing `chars % width` (as the bullet widow check in
    latex.py must, working from lengths alone) matters here: the defect is defined by
    what lands on the *last* line, and modulo arithmetic only approximates that.
    Wrapping names the orphan words outright, so the fix can be stated exactly.
    """
    lines: List[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        room = capacity - (first_line_penalty if not lines else 0.0)
        if current and _text_units(candidate) > room:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def skills_rows(latex: str) -> List[Dict]:
    """Every skills row in the document, with the rendered length it occupies."""
    latex = strip_comments(latex)          # udaya ships a whole commented-out skills block
    rows: List[Dict] = []
    for name, start, end in _sections(latex):
        if not _SKILLS_SECTION_RE.search(name):
            continue
        pos = start
        while True:
            m = _ROW_CMD_RE.search(latex, pos, end)
            if not m:
                break
            # The jakes/mst form wraps its rows in \small inside a narrower list, and
            # that block measurably breaks earlier than the full-size templates do.
            small = "\\small" in latex[max(start, m.start() - 60):m.start()]
            capacity = SMALL_ROW_UNITS if small else FULL_ROW_UNITS
            content, pos = _balanced_arg(latex, m.end())   # skip past it: never recurse in
            for piece in re.split(r"\\\\", content):
                text = _rendered_text(piece)
                if not text:
                    continue
                label = re.search(r"\\textbf\s*\{([^{}]*)\}", piece)
                label_text = _rendered_text(label.group(1)) if label else ""
                penalty = round(BOLD_EXTRA * _text_units(label_text), 1) if label_text else 0.0
                rows.append({
                    "section": name,
                    "label": label_text.rstrip(":") or text.split(",")[0][:32],
                    "text": text,
                    "chars": len(text),
                    "capacity": capacity,
                    "bold_penalty": penalty,
                    "lines": len(_wrap_lines(text, capacity, penalty)),
                })
    return rows


def skills_row_overflows(latex: str, max_orphan_words: int = ORPHAN_WORDS) -> List[Dict]:
    """Skills rows whose last rendered line holds only a word or two.

    An orphan line costs a full line of page height to display two words, which is
    why the judges flag it as a page_fit defect rather than a cosmetic one. The fix is
    always the same and always computable: drop exactly the entries that spilled.
    """
    out: List[Dict] = []
    for row in skills_rows(latex):
        lines = _wrap_lines(row["text"], row["capacity"], row["bold_penalty"])
        if len(lines) < 2:
            continue
        tail = lines[-1]
        words = tail.split()
        if len(words) > max_orphan_words:
            continue                        # a full trailing line is fine, not a widow
        # Rows whose tail only just failed to fit are the ones the width model is least
        # sure about, and a wrong "measured fact" costs more than a missed one.
        room = row["capacity"] - (row["bold_penalty"] if len(lines) == 2 else 0.0)
        if _text_units(f"{lines[-2]} {tail}") - room < ORPHAN_MARGIN_UNITS:
            continue
        # Removing the orphan words leaves the preceding lines untouched, so this cut
        # is exact; the safety margin covers rows whose bold label renders wider.
        cut = len(tail) + 1 + LINE_SAFETY_CHARS
        out.append({**row, "lines": len(lines), "orphan_text": tail,
                    "orphan_words": len(words), "cut_chars": cut,
                    "target_chars": max(40, row["chars"] - cut)})
    out.sort(key=lambda r: r["orphan_words"])
    return out


def _heading_label(latex: str, index: int) -> str:
    """Readable name for the heading command at `index`.

    Argument order is not portable — jakes puts the role first and the employer third,
    udaya the reverse — so take both content arguments and drop the date/location ones.
    """
    args: List[str] = []
    pos = index
    while len(args) < 4 and pos < len(latex):
        nxt = re.match(r"\s*\{", latex[pos:])
        if not nxt:
            break
        content, pos = _balanced_arg(latex, pos + nxt.end() - 1)
        args.append(_rendered_text(content))
    parts = [a for i, a in enumerate(args) if i in (0, 2) and a and not _DATE_LIKE_RE.search(a)]
    return " — ".join(parts)[:70] or (args[0][:70] if args else "(untitled)")


def subheading_groups(latex: str) -> List[Dict]:
    """Every heading and the number of bullets hanging off it, grouped by section."""
    latex = strip_comments(latex)
    groups: List[Dict] = []
    for name, start, end in _sections(latex):
        cursor = start
        while True:
            open_at = latex.find(r"\resumeItemListStart", cursor, end)
            if open_at == -1:
                break
            close_at = latex.find(r"\resumeItemListEnd", open_at, end)
            if close_at == -1:
                break
            block = latex[open_at:close_at]
            bullets = 0
            for m in _BULLET_CMD_RE.finditer(block):
                content, _ = _balanced_arg(block, m.end())
                if _rendered_text(content):
                    bullets += 1          # \resumeItem{} placeholders are not bullets
            headings = [h for h in _HEADING_RE.finditer(latex, cursor, open_at)]
            groups.append({
                "section": name,
                "heading": _heading_label(latex, headings[-1].end()) if headings else "(untitled)",
                "bullets": bullets,
            })
            cursor = close_at + len(r"\resumeItemListEnd")
    return groups


def imbalanced_groups(latex: str) -> List[Dict]:
    """Headings carrying visibly fewer bullets than their siblings.

    A block that is a heading plus one bullet reads as a hole in the page next to
    three- and four-bullet neighbours, and it is the second page_fit complaint the
    judges keep filing. Compared against the median of the *other* groups in the same
    section so a single thin block stands out instead of dragging the bar down with it.
    An evenly thin section (Education, one bullet each) is correct and stays unflagged.
    """
    out: List[Dict] = []
    by_section: Dict[str, List[Dict]] = {}
    for g in subheading_groups(latex):
        by_section.setdefault(g["section"], []).append(g)

    for name, groups in by_section.items():
        if len(groups) < 2:
            continue                       # nothing to be imbalanced against
        for i, g in enumerate(groups):
            siblings = [s["bullets"] for j, s in enumerate(groups) if j != i]
            norm = median(siblings)
            # Two bullets short of the norm, or a lone bullet among multi-bullet peers.
            if not (g["bullets"] <= norm - 2 or (g["bullets"] <= 1 <= norm - 1)):
                continue
            out.append({**g, "sibling_median": norm,
                        "add_bullets": max(1, int(round(norm)) - g["bullets"])})
    out.sort(key=lambda g: g["bullets"])
    return out


def run_checks(
    latex: str,
    must_have: Optional[List[str]] = None,
    nice_to_have: Optional[List[str]] = None,
    unsupported: Optional[List[str]] = None,
    compiled=None,
) -> LocalChecks:
    """Local structural checks.

    `must_have`/`nice_to_have` should already be filtered to requirements the
    candidate's dump can support — scoring a resume against keywords the person
    genuinely lacks would punish it for being truthful. `unsupported` carries the
    rest so the evaluator knows their absence is expected.
    Pass a CompileResult as `compiled` for a true page count.
    """
    latex = strip_comments(latex)
    checks = LocalChecks()
    bullets = extract_bullets(latex)
    checks.total_bullets = len(bullets)

    verb_counts: Counter = Counter()
    for b in bullets:
        plain = _rendered_text(b)
        words = plain.split()
        if words:
            verb_counts[words[0].lower().rstrip(",.")] += 1
        if "\\textbf{" not in b:
            checks.bullets_without_metric += 1
        if len(plain) > 210:
            checks.long_bullets.append(plain[:70])

    checks.skills_row_count = len(skills_rows(latex))
    checks.skills_row_overflows = skills_row_overflows(latex)
    checks.imbalanced_groups = imbalanced_groups(latex)

    checks.repeated_verbs = sorted(v for v, c in verb_counts.items() if c > 1)
    checks.banned_verbs_used = sorted(v for v in verb_counts if v in BANNED_VERBS)

    text_lower = latex.lower()
    checks.unsupported = list(unsupported or [])
    for tier, words in (("must_have", must_have), ("nice_to_have", nice_to_have)):
        cov = TierCoverage()
        for kw in words or []:
            (cov.matched if keyword_in_text(kw, text_lower) else cov.missing).append(kw)
        checks.keywords[tier] = cov

    checks.pages_estimated = estimate_pages(latex)
    if compiled is not None:
        checks.compile_ok = compiled.ok
        checks.compile_error = compiled.error
        checks.pages = compiled.pages
        checks.overfull_vboxes = compiled.overfull_vboxes
        checks.overflow_chars = compiled.overflow_chars
        checks.overflow_preview = compiled.overflow_preview
        checks.page1_fill = compiled.page1_fill

    checks.latex_balanced = (
        latex.count("\\begin{") == latex.count("\\end{")
        and "\\begin{document}" in latex
        and "\\end{document}" in latex
    )
    return checks


_VSPACE_RE = re.compile(r"\\vspace\*?\{\s*(-?\d*\.?\d+)\s*(pt|mm|cm|in|ex|em)\s*\}")
_LISTSPACE_RE = re.compile(r"(?:itemsep|topsep|parsep|partopsep|labelsep)\s*=\s*(-?\d*\.?\d+)\s*(pt|mm|cm|in|ex|em)")
_UNIT_PT = {"pt": 1.0, "mm": 2.845, "cm": 28.45, "in": 72.27, "ex": 4.3, "em": 10.0}

# Tightening beyond this (in points, summed across the document) is what makes
# lines collide. Small legitimate tweaks stay under it.
SPACING_CRUSH_TOLERANCE_PT = 6.0


def _total_vertical_space(latex: str) -> float:
    total = 0.0
    for value, unit in _VSPACE_RE.findall(latex) + _LISTSPACE_RE.findall(latex):
        total += float(value) * _UNIT_PT.get(unit, 1.0)
    return total


_GEOMETRY_RE = re.compile(
    r"\\addtolength\s*\{\s*\\(topmargin|textheight|textwidth|oddsidemargin|evensidemargin|footskip)\s*\}"
    r"\s*\{\s*(-?\d*\.?\d+)\s*(pt|mm|cm|in|ex|em)\s*\}"
)


def _geometry_signature(latex: str) -> Dict[str, float]:
    """Page-geometry adjustments, in points, keyed by dimension."""
    out: Dict[str, float] = {}
    for dim, value, unit in _GEOMETRY_RE.findall(latex):
        out[dim] = out.get(dim, 0.0) + float(value) * _UNIT_PT.get(unit, 1.0)
    return out


def spacing_crush_reason(before: str, after: str) -> Optional[str]:
    """Reject edits that fake a page fit by shrinking the layout instead of the content.

    Two failure modes, both seen in practice:
      1. Vertical space crushed until lines physically overlap.
      2. Page geometry stretched (bigger \\textheight, smaller \\topmargin) to cram
         more on — which is how a "one page" resume ends up unreadably dense.
    Content edits move neither number, so any movement here is layout tampering.
    """
    delta = _total_vertical_space(after) - _total_vertical_space(before)
    if delta < -SPACING_CRUSH_TOLERANCE_PT:
        return (
            f"That edit compressed vertical spacing by {abs(delta):.0f}pt, which makes lines overlap. "
            "Ask again and say to shorten or cut content instead of tightening spacing."
        )

    geo_before, geo_after = _geometry_signature(before), _geometry_signature(after)
    for dim in set(geo_before) | set(geo_after):
        b, a = geo_before.get(dim, 0.0), geo_after.get(dim, 0.0)
        # More text height, or a smaller top margin, means the page was stretched.
        stretched = (dim in ("textheight", "textwidth") and a > b + 2) or (
            dim in ("topmargin", "footskip") and a < b - 2
        )
        if stretched:
            return (
                f"That edit changed the page geometry (\\{dim}) to force a fit. "
                "Ask again and say to cut or shorten content instead of changing margins or text height."
            )
    return None


def estimate_pages(latex: str) -> int:
    """Rough page estimate, used only when compilation is unavailable."""
    latex = strip_comments(latex)
    bullets = len(re.findall(r"\\resumeItem\{", latex))
    headings = len(re.findall(r"\\resumeSubheading\{|\\resumeProjectHeading\{", latex))
    body = latex.split("\\begin{document}")[-1]
    score = bullets * 6 + headings * 8 + len(body) / 500
    if score <= 125:
        return 1
    elif score <= 230:
        return 2
    return 3
