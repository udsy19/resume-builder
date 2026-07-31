"""
Deterministic defect repair.

Everything here fixes a defect that was *measured*, never one that was argued about.
That distinction is load-bearing: across live runs, critique items backed by a
deterministic check reliably improved the draft, while model-judged items were roughly a
coin flip and sometimes made it worse. So each repair below takes a machine-detected
defect, names the exact bullet and the exact target, and accepts the rewrite only if it
verifiably satisfies the target.
"""

from typing import Dict, List

CRAFT_SYSTEM = r"""You repair specific defects in resume bullets. Each bullet you receive has a defect that was detected mechanically, so it is definitely present — your job is to fix that defect without disturbing anything else.

**"no metric or scale signal"** — the bullet describes activity and never reports a result. Add the outcome from the candidate's dump: what changed, by how much, at what scale, for whom. Prefer a real number; where the dump has none, a truthful scale signal ("across 11 systems", "for two cohorts") is acceptable. If the dump genuinely records no outcome for this work, sharpen the bullet to state the hardest technical thing about it instead — never invent a number.

**"N bold spans"** — emphasis has stopped meaning anything. Keep at most three \textbf spans: the strongest metric and the strongest job-description keyword. Unbold the rest, changing no words.

RULES
- Change only what the defect requires. Everything else in the bullet stays as written.
- Never invent an employer, date, technology, or number. Facts come from the dump only.
- Keep the opening action verb, and keep the bullet's length within the stated maximum — these resumes fit one page by exact measurement, and a longer bullet pushes the page over.
- Escape LaTeX specials in anything you add: \% \& \$ \# \_

Return every bullet you were given, same indices."""

CRAFT_PROMPT = """Repair these {n} bullets. Each one's detected defect and its maximum length are given.

{bullets}

Return one entry per bullet, same indices, with the repaired text and its character count."""

CRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "text": {"type": "string", "description": "Repaired bullet, LaTeX markup preserved"},
                    "chars": {"type": "integer"},
                    "unchanged": {"type": "boolean",
                                  "description": "True if the dump offered nothing truthful to add"},
                },
                "required": ["index", "text", "chars", "unchanged"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["bullets"],
    "additionalProperties": False,
}


def format_defects(defects: List[Dict], max_chars: int = 240) -> str:
    """Render detected defects as a numbered worklist with explicit targets."""
    out = []
    for d in defects:
        problems = "; ".join(d.get("problems", [d.get("problem", "")]))
        out.append(
            f"[{d['index']}] {problems}\n"
            f"currently {d['chars']} characters, maximum {max(max_chars, d['chars'])}:\n{d['text']}"
        )
    return "\n\n".join(out)


# ── Judge-found writing defects ──────────────────────────────────────
#
# The refine loop sends at most six issues per cycle, chosen from twenty to forty found.
# That cap exists because large simultaneous critique lists caused regressions — but it
# was set when critique was *argued*. Writing defects now arrive as enumerated offenders
# with quotes verified against the document, which is the same footing craft repair
# already works on: exact target in, corrected bullet out, accepted only if it verifies.
#
# So they are repaired here rather than competing for worklist slots, which is what left
# writing_quality the largest remaining loss on the scorecard.

WRITING_SYSTEM = r"""You repair one specific, already-verified defect in each resume bullet you are given. The defect was located by quoting the bullet out of the document, so it is definitely present. Fix exactly that and disturb nothing else.

DEFECT TYPES
- **dead tail** — the bullet ends on activity, a headcount, a tool list, or an empty purpose clause instead of a result. Rewrite the closing clause so it states what the work produced, found, prevented, saved, or enabled.

  THE OUTCOME MUST ALREADY BE IN THE DUMP. Before you write it, find the sentence in the dump that states it and satisfy yourself it is about THIS work. If the dump does not record an outcome for this bullet, do NOT supply one: instead cut the dead tail so the bullet ends on the hardest concrete technical fact it already contains, which is shorter and always true. A bullet that merely stops early is a small loss; a bullet with an invented result is a lie on someone's resume, and fixing this defect is not worth causing that one.
- **weak opening** — replace the opening verb with a strong, specific one. It must not duplicate any verb already used elsewhere in the resume.
- **off audience** — the bullet is written for a different reader than this role. Reframe it onto the technical substance this job cares about, keeping the same underlying facts.
- **overbolded** — reduce to at most two \textbf spans: the strongest metric and the strongest job-description keyword. Change no words, only emphasis.
- **repeated verb** — replace the opening verb with one used nowhere else in the document.
- **multi claim** — the bullet carries two unrelated claims. Keep the one more relevant to the job description and cut the other, or split if both are strong and the budget allows.

RULES
- Preserve every fact. You are rewriting how something is said, never what happened.
- Stay within the stated character maximum: these resumes fit one page by exact measurement, and a longer bullet pushes the page over.
- Keep the LaTeX markup valid and escape specials in anything you add: \% \& \$ \# \_
- If a bullet genuinely cannot be improved without inventing something, return it unchanged and say so.

Return every bullet you were given, same indices."""

WRITING_PROMPT = """Repair these {n} bullets. Each carries the defect a judge verified against the document, and its maximum length.

{bullets}

Return one entry per bullet, same indices, with the repaired text and its character count."""


def format_writing_defects(defects, max_chars: int = 240) -> str:
    """Render judge-found writing defects as a worklist with explicit targets."""
    out = []
    for d in defects:
        out.append(
            f"[{d['index']}] {d['kind']}: {d['fix']}\n"
            f"currently {d['chars']} characters, maximum {max(max_chars, d['chars'])}:\n{d['text']}"
        )
    return "\n\n".join(out)
