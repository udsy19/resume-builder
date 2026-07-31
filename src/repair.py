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
