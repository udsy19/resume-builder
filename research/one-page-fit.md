# Making a LaTeX resume land on exactly one full page

## The core mistake we were making

We measured `pages == 1` — a 1-bit signal — and PDF ink geometry, which **saturates**.
A draft with four bullets of slack and one with none both measure "99% full". So the
loop had no idea whether it was close or far, and the writer was told "cut content"
with no quantity attached.

## The measurement that works

TeX's own page arithmetic, read from the log at `\AtEndDocument`:

```
headroom_pt = textheight + pageshrink − pagetotal
```

Linear in points, never saturates. Measured on our own files:

| variant | pages | ink fill | headroom |
|---|---|---|---|
| 2-page draft | 2 | 92.3% | −184.0pt (≈15 lines over) |
| same, 4 bullets trimmed | 1 | 99.9% | **+5.2pt** |
| `templates/udaya.tex` | 1 | 99.7% | **+26.1pt** (≈2 spare lines) |

Ink fill cannot tell 99.0 from 99.9. Headroom can, and since a bullet line costs
~12pt it tells you exactly how many lines to cut or add.

## Getting it without touching the user's template

`pdflatex '\input{fitprobe.tex}\input{doc.tex}'` — the command line is processed
before `\documentclass`, so `\RequirePackage` and `\AtBeginDocument` hooks work and the
template is never edited. See [`src/fitprobe.tex`](../src/fitprobe.tex). It also wraps
`\resumeItem` with `zref-savepos` + `zref-abspage`, so a single compile reports every
bullet's page and y-position — which bullets actually landed on page 2.

Two passes are required: zref positions are only correct once the `.aux` exists.

## The cost model

```
cost_pt   = 2 + 12 × ceil(chars / 127)
budget_lines = (usable_height − skeleton_overhead) / 12
```

Exact to 0.01pt in testing. The invariant is **total lines**, not characters:
38×1-line and 19×2-line bullets both fit. Overhead must be *measured* by compiling the
template with bullets stripped — modelling it analytically fails, because per-section
and per-subheading costs are non-linear (29.5 then 27.4pt; 14.0 then 20.0pt).

## LaTeX "make it fit" levers are mostly theatre

Measured against a 124pt overflow:

| lever | effect |
|---|---|
| `microtype` expansion/tracking | **0pt** |
| `\looseness=-1` via preamble hook | **0pt** (resets per paragraph) |
| `\linespread{0.97}` | ~0pt |
| `\linespread{0.92}` | ~24pt, and readability suffers |
| `\enlargethispage{130pt}` | "1 page", but content lands *off* the page |

Ceiling is 1–2 lines. **Content removal is the only real mechanism.** Readability floor:
`lead/font-size ≥ 1.10`, i.e. `linespread ≥ 0.93`.

## Solver

Headroom-guided: jump using measured per-bullet costs, then restore any candidate whose
cost fits the remaining headroom. Benchmarked at **3 compiles, provably minimal** vs 5
for naive one-at-a-time and 4 for binary search (which ignores geometry you already have).
Orphan headings are prevented structurally by a per-section minimum, not by heuristics.

Under-fill is the *same* metric: headroom ≥ one bullet's cost means add content.

## Prior art

- **PaperFit** (arXiv 2605.10341) — ranks repairs layout-native → spacing → forbidden; 80.5% exact-page-budget rate.
- **ResumeFlow** (arXiv 2402.06221, SIGIR) — no page guarantee at all; one pass, no retry.
- `btseytlin/hr-breaker` — the good architectural idea: enforce fit in an **output validator that raises a retry**, so a non-fitting document physically cannot be returned. Asking for it in the prompt is what fails.
- No CTAN package fits a flowing document to N pages (`textfit`/`fitbox` scale single boxes only).

## Two real bugs this research surfaced in our own code

1. `templates/udaya.tex` had an **unescaped `&`** ("VPN & IPSec"), throwing
   `! Misplaced alignment tab character &`. It only appeared to work because we compile with
   `-interaction=nonstopmode` and no `-halt-on-error`, so TeX recovered silently. **Fixed.**
2. `shutil.which("pdflatex")` returned `None` because TeX Live isn't on a GUI app's PATH —
   so **every** compile silently fell back to the third-party host `latex.ytotech.com`.
   Resume content was leaving the machine and each iteration paid a network round-trip.
   **Fixed** with a search over standard TeX install locations: local compile is 0.5s vs ~6s.

## VMock's published criteria vs. our output

Boston University's VMock guide documents the actual scoring modules. Two findings:

**Our resumes already pass VMock's mechanical Impact rules.** Checking real generated
output for the things VMock flags — filler adverbs (successfully, effectively,
independently), pronouns (I, we, our), and bullets opening on a weak verb or an article
("Responsible for", "worked", "helped") — returns **zero violations** across every saved
artifact. The writer prompt was already banning them. These are now checked mechanically
anyway, as regression guards.

**Calibration worth knowing:** BU states its own professionally-written sample resumes
score **in the 80s** on VMock, and that "if your score is in the 70s, you're well on your
way to a great resume." Our internal rubric is stricter than VMock's practical bar, so an
internal 86-89 plausibly corresponds to a strong real VMock result. Chasing 95 on our own
scale may be chasing a number that does not correspond to real-world quality.

The remaining internal gap is in the judges' subjective craft assessment (bullet rhythm,
outcome strength), not in VMock-style rule violations.

## Skills rows do not obey the 127-character rule

A flat characters-per-line constant works for prose bullets and **fails** for skills rows.
Measuring glyph positions in compiled PDFs: a 113-character skills row fits on one line
while a 107-character row breaks. Skills rows are acronym-dense, and proportional-font
width does not track character count — uppercase and digits are wide, spaces and thin
punctuation narrow, and a bold label carries a surcharge.

`validator.py` therefore measures skills rows in fitted width units with separate
capacities for `\small` blocks (jakes, mst) and full-size rows (udaya, sb2nov). Parameters
were fitted against 25 fit/break lines read off compiled resumes and classify 24 of 25
correctly. Validated end-to-end by rebuilding the exact failing case from the logs —
real generated skills rows placed in `templates/jakes.tex`, compiled, glyph positions read
back: **3 predicted orphans, 3 real orphans, no false positives and no misses.**

`127` remains the codebase-wide constant for prose bullets, where it holds.
