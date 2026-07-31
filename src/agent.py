"""
The resume agent: a recursive generate -> evaluate -> refine loop built
entirely on the Anthropic SDK.

Flow per run:
  1. Analyze the JD (role framing + ATS keyword plan, structured output)
  2. Generate a tailored LaTeX resume into the chosen template
  3. Evaluate: fast local checks + LLM recruiter/ATS/fact-checker rubric
  4. If the evaluator says revise, feed the critique back and regenerate;
     repeat until pass, score plateau, or max_iterations.

Every model call streams: the model's summarized thinking and generation
progress are forwarded live so the UI is never a dead loading screen.
Also hosts the chat editor used for conversational resume edits.
"""

import asyncio
import base64
import json
import os
import re
import time
from typing import AsyncGenerator, Dict, List, Optional

from .ingest import Dump
from . import prompts
from .latex import (LINE_COST_PT, bullets_to_expand, bullets_with_widows, craft_defects, bullets_to_tighten, compile_pdf, fit_to_one_page,
                    line_arithmetic, measure_fit, replace_bullets, skeleton_line_budget)
from . import repair
from .providers import ProviderError, make_provider
from .validator import keyword_in_text, run_checks, spacing_crush_reason

MODEL = "claude-opus-5"
MAX_TOKENS = 32000
JUDGE_NOISE_PTS = 3  # measured: sd 1.17, spread 3 over k=5 identical re-scores
PASS_THRESHOLD = 95  # out of 100 — the ResumeWorded/VMock bar, not "good enough"
# Soft deadline: ship the best draft before a serverless timeout kills the run.
# Override with RUN_BUDGET_SECONDS for long local runs that should keep refining.
RUN_BUDGET_SECONDS = int(os.environ.get("RUN_BUDGET_SECONDS", "250"))
SCORE_CAPS = {"keyword_match": 30, "ats_compliance": 20, "writing_quality": 20, "truthfulness": 20, "page_fit": 10}

# Reasoning depth per phase — Opus 5 is strong at low/medium; only writing needs high.
EFFORT = {"analyze": "low", "plan": "medium", "fit": "low", "generate": "medium", "evaluate": "medium", "refine": "high", "edit": "medium", "audit": "medium", "tighten": "low", "expand": "medium", "repair": "medium"}

# Cyber-adjacent resumes can trip Opus 5's safety classifiers on benign content;
# the server-side fallback re-runs a declined request on the recommended model.
_FALLBACK_HEADERS = {"anthropic-beta": "server-side-fallback-2026-07-01"}
_FALLBACK_BODY = {"fallbacks": "default"}

# Emit thinking deltas in chunks so the SSE stream stays light.
_THINKING_FLUSH_AT = 90


class AgentError(Exception):
    pass


def _text_of(message) -> str:
    text = getattr(message, "text", "") or ""
    if not text.strip():
        raise AgentError("The model returned an empty response. Try again.")
    return text


def _parse_json(text: str) -> Dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AgentError("The model returned malformed structured output. Try again.")


def _extract_latex(text: str) -> str:
    text = re.sub(r"```(?:latex|tex)?\n?|```", "", text)
    matches = re.findall(r"\\documentclass[\s\S]*?\\end\{document\}", text)
    if not matches:
        raise AgentError("The writer did not return a complete LaTeX document.")
    return matches[-1]  # if the model emitted a draft + a revision, take the revision


def apply_patches(latex: str, edits: List[Dict]) -> str:
    """Apply find/replace edits. All-or-nothing: a missing anchor aborts the edit."""
    if not edits:
        raise AgentError("The editor returned no changes to apply.")
    out = latex
    for edit in edits:
        find = edit.get("find", "")
        if not find:
            raise AgentError("The editor sent an empty search string.")
        if find not in out:
            snippet = find.strip().splitlines()[0][:80]
            raise AgentError(
                f"Couldn't locate the text to change (\"{snippet}…\"). Nothing was modified — try rephrasing the request."
            )
        out = out.replace(find, edit.get("replace", ""))
    if "\\documentclass" not in out or "\\end{document}" not in out:
        raise AgentError("That edit would have broken the document structure, so it was not applied.")
    return out


def _budget_plan(budget_lines: float) -> Dict:
    """Turn a measured line budget into an arithmetically exact, countable plan.

    Rendered lines fill a page, and a bullet is 1 line up to ~127 characters or 2 up to
    ~254. So for N bullets of which K are two-liners: total_lines = N + K. Choosing N and
    K with N + K <= budget makes the fit provable by counting, which the model can do —
    it cannot count rendered lines.
    """
    usable = max(8.0, budget_lines - 1.0)
    n = int(usable * 0.62)
    k = max(0, int(usable) - n)
    edu = 2 if n >= 18 else 1
    skills = max(3, min(5, round(n * 0.18)))
    remaining = max(4, n - edu - skills)
    exp = max(3, round(remaining * 0.62))
    proj = max(2, remaining - exp)
    return {"lines": round(budget_lines, 1), "max_bullets": n, "two_line_allowance": k,
            "exp": exp, "proj": proj, "skills": skills, "edu": edu}


# Refinement gets a short, focused worklist — long critique lists cause regressions.
# Ordering by a fixed category rank starved whole categories: in one run writing_quality
# raised 35 issues (the most of any category) and not one reached the writer, because
# keyword issues filled all six slots every pass. So the worklist is now driven by where
# the points are actually being lost, round-robin so no category can monopolize it.
MAX_ISSUES_PER_REFINE = 6
_HARD_FIRST = ("page_fit", "latex")


def _prioritize(issues: List[Dict], scores: Optional[Dict] = None) -> List[Dict]:
    """Pick the highest-leverage issues: worst-scoring categories first, interleaved."""
    if not issues:
        return []
    by_cat: Dict[str, List[Dict]] = {}
    for i in issues:
        by_cat.setdefault(i.get("category", "other"), []).append(i)

    def deficit(cat: str) -> float:
        cap = SCORE_CAPS.get(cat)
        if not cap or not scores or cat not in scores:
            return 0.5           # unscored (e.g. "latex"): mid priority
        return (cap - scores[cat]["score"]) / cap

    # A broken page or uncompilable document is not a scoring matter — fix it first.
    order = sorted(by_cat, key=lambda c: (c not in _HARD_FIRST, -deficit(c)))
    picked: List[Dict] = []
    round_no = 0
    while len(picked) < MAX_ISSUES_PER_REFINE:
        added = False
        for cat in order:
            if round_no < len(by_cat[cat]) and len(picked) < MAX_ISSUES_PER_REFINE:
                picked.append(by_cat[cat][round_no])
                added = True
        if not added:
            break
        round_no += 1
    return picked


_SECTION_RE = re.compile(r"\\section\{([^}]+)\}")


class _Result:
    """What the loop needs from a completed call: its text (and usage, for telemetry)."""

    def __init__(self, text: str, usage=None):
        self.text = text
        self.usage = usage


class ResumeAgent:
    def __init__(self, user_api_key: Optional[str] = None,
                 provider: Optional[str] = None, model: Optional[str] = None):
        self.provider = make_provider(provider, model, user_api_key)
        self.model_label = f"{self.provider.name}:{self.provider.model}"

    async def _stream_call(self, *, phase: str, system, messages, output_schema: Optional[dict] = None):
        """One streamed model call, provider-agnostic.

        Yields {"event": "thinking"|"writing"} live, then {"event": "message"} carrying a
        lightweight result object with `.text` — the rest of the loop only ever reads text.
        """
        blocks = messages[0]["content"] if messages else []
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]

        sections_seen: List[str] = []
        text_so_far = ""
        result = None
        async for ev in self.provider.stream(
            phase=phase, system=system, blocks=blocks, schema=output_schema,
            effort=EFFORT.get(phase, "high"), max_tokens=MAX_TOKENS,
        ):
            if ev["event"] == "done":
                result = ev
            elif ev["event"] == "writing":
                yield {"event": "writing", "phase": phase, "section": None, "chars": ev["chars"]}
            else:
                yield ev

        if result is None:
            raise AgentError("The model returned nothing.")
        text = result["text"]
        if not text.strip():
            raise AgentError("The model returned an empty response. Try again.")
        yield {"event": "message", "message": _Result(text, result.get("usage"))}

    # ── Evaluation: three focused judges, concurrently ───────────────

    async def _judge(self, *, latex, dump, job_description, jd_analysis, checks, pdf=None):
        """Run the judge lenses in parallel, streaming their live events, then merge.

        Three narrow lenses beat one broad pass: each is more thorough, and they run
        concurrently so the wall-clock cost is the slowest lens rather than their sum.
        Yields progress events plus a final {"step": "verdict", "evaluation": {...}}.
        """
        base = dict(
            job_description=job_description,
            jd_analysis=json.dumps(jd_analysis, indent=2),
            local_checks=checks.summary(),
            latex=latex,
        )

        async def one(name: str, lens: Dict, queue: asyncio.Queue):
            prompt = prompts.JUDGE_PROMPT.format(
                focus=lens["focus"], categories=", ".join(lens["categories"]), **base
            )
            content: List[Dict] = []
            # The fact-checker needs the dump; the page judge needs to see the PDF.
            if name == "truth":
                content.append({"type": "text", "text": prompts.EVALUATION_DUMP_PREFIX})
                content += dump.as_content_blocks(prompt)
            else:
                if name == "craft" and pdf:
                    content.append({
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf",
                                   "data": base64.standard_b64encode(pdf).decode()},
                    })
                content.append({"type": "text", "text": prompt})

            result = None
            try:
                async for ev in self._stream_call(
                    phase="evaluate",
                    system=prompts.EVALUATOR_SYSTEM,
                    messages=[{"role": "user", "content": content}],
                    output_schema=prompts.judge_schema(lens["categories"]),
                ):
                    if ev["event"] == "message":
                        result = _parse_json(_text_of(ev["message"]))
                    else:
                        await queue.put({"step": "live", **ev, "lens": name})
            except Exception as e:                      # one lens failing must not hang the rest
                await queue.put({"__done__": name, "result": None, "error": repr(e)})
                return
            await queue.put({"__done__": name, "result": result})

        queue: asyncio.Queue = asyncio.Queue()
        tasks = [asyncio.create_task(one(n, l, queue)) for n, l in prompts.JUDGE_LENSES.items()]
        results: Dict[str, Dict] = {}
        errors: Dict[str, str] = {}
        try:
            while len(results) + len(errors) < len(tasks):
                item = await queue.get()
                if "__done__" in item:
                    name = item["__done__"]
                    if item.get("error") or item.get("result") is None:
                        errors[name] = item.get("error", "no result")
                    else:
                        results[name] = item["result"]
                    yield {"step": "lens_done", "lens": name,
                           "message": f"{name} judge finished ({len(results)}/{len(tasks)})"}
                else:
                    yield item
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

        if errors:
            raise AgentError(f"Evaluation lens failed: {', '.join(errors)}")

        scores: Dict[str, Dict] = {}
        issues: List[Dict] = []
        for name, res in results.items():
            scores.update(res["scores"])
            issues += res.get("issues") or []

        missing = [c for c in SCORE_CAPS if c not in scores]
        if missing:
            raise AgentError(f"Evaluation incomplete — missing {', '.join(missing)}.")

        total = sum(min(max(0, scores[cat]["score"]), cap) for cat, cap in SCORE_CAPS.items())
        yield {
            "step": "verdict",
            "evaluation": {
                "scores": scores,
                "issues": issues,
                "verdict": "pass" if total >= PASS_THRESHOLD and not issues else "revise",
                "total": total,
            },
        }

    # ── Deterministic length control ─────────────────────────────────

    async def _tighten(self, latex: str, rounds: int = 4):
        """Compile, measure headroom, tighten exactly enough bullets, repeat.

        Every generation we measured overflowed (four attempts, four two-page drafts)
        because the model cannot self-limit bullet length even given exact character
        allowances. So length is enforced from the outside: TeX reports how many points
        over the page is, that converts to lines, and precisely that many over-length
        bullets get an exact character target.
        """
        total_fixed = 0
        self._tightened, self._tightened_latex = 0, latex
        for _ in range(rounds):
            fit = measure_fit(latex)
            if not fit.ok or fit.headroom_pt is None:
                break
            if fit.pages == 1 and fit.headroom_pt >= 0:
                break
            lines_over = (-fit.headroom_pt / LINE_COST_PT) + 1  # +1 line of slack
            todo = bullets_to_tighten(latex, lines_over)
            if not todo:
                break
            yield {"step": "tightening", "phase": "tighten",
                   "message": f"{round(-fit.headroom_pt)}pt over one page (~{round(lines_over)} lines) — "
                              f"tightening {len(todo)} bullet(s) to single lines",
                   "data": {"headroom_pt": round(fit.headroom_pt, 1), "count": len(todo)}}

            listing = "\n\n".join(
                f"[{t['index']}] currently {t['chars']} characters, target {t['target']}:\n{t['text']}"
                for t in todo
            )
            result = None
            async for ev in self._stream_call(
                phase="tighten", system=prompts.TIGHTEN_SYSTEM,
                messages=[{"role": "user", "content": prompts.TIGHTEN_PROMPT.format(
                    n=len(todo), bullets=listing)}],
                output_schema=prompts.TIGHTEN_SCHEMA,
            ):
                if ev["event"] == "message":
                    result = _parse_json(_text_of(ev["message"]))
                else:
                    yield {"step": "live", **ev}

            targets = {t["index"]: t["target"] for t in todo}
            accepted = {
                b["index"]: b["text"] for b in (result or {}).get("bullets", [])
                if b["index"] in targets and b["text"].strip()
                and len(b["text"]) <= targets[b["index"]]
            }
            if not accepted:
                break
            latex = replace_bullets(latex, accepted)
            total_fixed += len(accepted)
            self._tightened, self._tightened_latex = total_fixed, latex


    async def _expand(self, latex: str, dump, missing_keywords=None, rounds: int = 2):
        """Fill an under-used page with genuine detail, mirroring _tighten.

        An under-filled page wastes the candidate's only page as surely as an overflowing
        one. This also happens to be the cheapest place to land still-missing keywords.
        """
        added = 0
        self._expanded, self._expanded_latex = 0, latex
        for _ in range(rounds):
            fit = measure_fit(latex)
            if not fit.ok or fit.headroom_pt is None or fit.pages != 1:
                break
            lines_free = fit.headroom_pt / LINE_COST_PT
            if lines_free < 2:                       # already full enough
                break
            todo = bullets_to_expand(latex, lines_free - 0.5)
            if not todo:
                break
            yield {"step": "expanding", "phase": "expand",
                   "message": f"Page is {round(lines_free, 1)} lines short of full — "
                              f"expanding {len(todo)} bullet(s) with real detail",
                   "data": {"lines_free": round(lines_free, 1), "count": len(todo)}}

            kw_note = ""
            if missing_keywords:
                kw_note = ("Still-missing keywords worth landing IF a bullet genuinely involved them: "
                           + ", ".join(missing_keywords[:12]))
            listing = "\n\n".join(
                f"[{t['index']}] currently {t['chars']} characters, target ~{t['target']}:\n{t['text']}"
                for t in todo
            )
            prompt = prompts.EXPAND_PROMPT.format(
                lines=round(lines_free, 1), n=len(todo), keyword_note=kw_note, bullets=listing)
            result = None
            async for ev in self._stream_call(
                phase="expand", system=prompts.EXPAND_SYSTEM,
                messages=[{"role": "user", "content": dump.as_content_blocks(prompt)}],
                output_schema=prompts.EXPAND_SCHEMA,
            ):
                if ev["event"] == "message":
                    result = _parse_json(_text_of(ev["message"]))
                else:
                    yield {"step": "live", **ev}

            index = {t["index"]: t for t in todo}
            accepted = {
                b["index"]: b["text"] for b in (result or {}).get("bullets", [])
                if b["index"] in index and not b.get("unchanged")
                and b["text"].strip() and len(b["text"]) > index[b["index"]]["chars"]
                and len(b["text"]) <= index[b["index"]]["target"] + 20
            }
            if not accepted:
                break
            candidate = replace_bullets(latex, accepted)
            check = measure_fit(candidate)
            if not check.ok or check.pages != 1:
                break                                # never trade a full page for a second one
            latex = candidate
            added += len(accepted)
            self._expanded, self._expanded_latex = added, latex


    async def _repair_craft(self, latex: str, dump, max_bullets: int = 8):
        """Fix mechanically-detected craft defects: activity-only bullets, bold overload.

        Runs before the judges so they score a draft that has already had its measurable
        writing defects removed — those were the evaluator's most frequent complaint and
        the category that stayed pinned at 15/20.
        """
        self._repaired, self._repaired_latex = 0, latex
        defects = craft_defects(latex)[:max_bullets]
        if not defects:
            return
        yield {"step": "repairing", "phase": "repair",
               "message": f"Repairing {len(defects)} measured writing defect(s) — "
                          "bullets with no outcome, or too much bold",
               "data": {"count": len(defects)}}

        result = None
        prompt = repair.CRAFT_PROMPT.format(n=len(defects), bullets=repair.format_defects(defects))
        try:
            async for ev in self._stream_call(
                phase="repair", system=repair.CRAFT_SYSTEM,
                messages=[{"role": "user", "content": dump.as_content_blocks(prompt)}],
                output_schema=repair.CRAFT_SCHEMA,
            ):
                if ev["event"] == "message":
                    result = _parse_json(_text_of(ev["message"]))
                else:
                    yield {"step": "live", **ev}
        except (ProviderError, AgentError) as e:
            yield {"step": "repaired", "message": f"Craft repair skipped ({e})", "data": {"count": 0}}
            return

        allowed = {d["index"]: max(240, d["chars"]) for d in defects}
        accepted = {
            b["index"]: b["text"] for b in (result or {}).get("bullets", [])
            if b["index"] in allowed and not b.get("unchanged")
            and b["text"].strip() and len(b["text"]) <= allowed[b["index"]]
        }
        if not accepted:
            return
        candidate = replace_bullets(latex, accepted)
        verify = await compile_pdf(candidate)
        if not (verify.ok and verify.pages == 1):
            yield {"step": "repaired",
                   "message": "Craft repairs rejected — they would have pushed past one page",
                   "data": {"count": 0}}
            return
        self._repaired, self._repaired_latex = len(accepted), candidate
        yield {"step": "repaired",
               "message": f"Repaired {len(accepted)} bullet(s): added missing outcomes, trimmed bold",
               "data": {"count": len(accepted)}}

    # ── The main loop ────────────────────────────────────────────────

    async def run(
        self,
        dump: Dump,
        job_description: str,
        template_latex: str,
        aggressiveness: int = 2,
        max_iterations: int = 4,
    ) -> AsyncGenerator[Dict, None]:
        """Run the full agentic loop, yielding progress events; the last event is step=result."""
        aggressiveness = max(1, min(3, int(aggressiveness)))
        run_start = time.monotonic()

        # ── 1. JD analysis ──
        yield {"step": "analyzing", "phase": "analyze",
               "message": "Reading the job description & extracting ATS keywords...", "progress": 4}
        jd_analysis = None
        async for ev in self._stream_call(
            phase="analyze",
            system="You are an expert ATS and technical recruiting analyst.",
            messages=[{"role": "user", "content": prompts.JD_ANALYSIS_PROMPT.format(job_description=job_description)}],
            output_schema=prompts.JD_ANALYSIS_SCHEMA,
        ):
            if ev["event"] == "message":
                jd_analysis = _parse_json(_text_of(ev["message"]))
            else:
                yield {"step": "live", **ev}
        yield {
            "step": "analyzed",
            "message": f"Role: {jd_analysis['role_title'] or 'unknown'} — "
                       f"{len(jd_analysis['must_have_keywords'])} must-have keywords, "
                       f"framing: {jd_analysis['role_type']}",
            "progress": 14,
            "data": jd_analysis,
        }

        # ── 1b. Feasibility: which requirements can this candidate honestly claim? ──
        all_required = (jd_analysis.get("must_have_keywords") or []) + (jd_analysis.get("nice_to_have_keywords") or [])
        plan = {"supported": [], "absent": []}
        if all_required:
            yield {"step": "planning", "phase": "plan",
                   "message": f"Checking which of {len(all_required)} requirements your background actually supports...",
                   "progress": 16}
            plan_content = dump.as_content_blocks(
                prompts.COVERAGE_PLAN_PROMPT.format(keywords="\n".join(f"- {k}" for k in all_required))
            )
            async for ev in self._stream_call(
                phase="plan",
                system=prompts.COVERAGE_PLAN_SYSTEM,
                messages=[{"role": "user", "content": plan_content}],
                output_schema=prompts.COVERAGE_PLAN_SCHEMA,
            ):
                if ev["event"] == "message":
                    plan = _parse_json(_text_of(ev["message"]))
                else:
                    yield {"step": "live", **ev}

        supported = [s["keyword"] for s in plan.get("supported", [])]
        absent = plan.get("absent", [])
        yield {
            "step": "planned",
            "message": f"{len(supported)} requirement(s) you can claim, {len(absent)} genuinely outside your background",
            "progress": 20,
            "data": {"supported": plan.get("supported", []), "absent": absent},
        }

        # ── 2. Generation ──
        # Only trusted rules live in the system prompt; the template (user-uploadable)
        # rides in the user turn so it never carries operator authority.
        writer_system = [{
            "type": "text",
            "text": prompts.WRITER_SYSTEM + "\n\n" + prompts.AGGRESSIVENESS[aggressiveness],
            "cache_control": {"type": "ephemeral"},
        }]
        gen_prompt = prompts.GENERATION_PROMPT.format(
            job_description=job_description, jd_analysis=json.dumps(jd_analysis, indent=2)
        )
        if all_required:
            gen_prompt += prompts.COVERAGE_BRIEF.format(
                supported="\n".join(
                    f"- {s['keyword']} — evidence: {s['evidence'][:160]} (place in: {s['where']})"
                    for s in plan.get("supported", [])
                ) or "(none)",
                absent=", ".join(absent) or "(none)",
            )
        first_content = [{"type": "text", "text": prompts.TEMPLATE_INSTRUCTION.format(template=template_latex)}]
        first_content += dump.as_content_blocks(gen_prompt)
        first_content[-1]["cache_control"] = {"type": "ephemeral"}
        messages: List[Dict] = [{"role": "user", "content": first_content}]

        budget = skeleton_line_budget(template_latex)
        if budget:
            gen_prompt_budget = prompts.BUDGET_BRIEF.format(**_budget_plan(budget))
            first_content.insert(1, {"type": "text", "text": gen_prompt_budget})
            yield {"step": "budgeted", "phase": "generate",
                   "message": f"Template fits ~{budget} lines of bullet text — writing to that budget",
                   "data": {"budget_lines": budget}}

        yield {"step": "generating", "phase": "generate",
               "message": f"Writing the resume (aggressiveness {aggressiveness})...", "progress": 18}
        msg = None
        async for ev in self._stream_call(phase="generate", system=writer_system, messages=messages):
            if ev["event"] == "message":
                msg = ev["message"]
            else:
                yield {"step": "live", **ev}
        latex = _extract_latex(_text_of(msg))
        yield {"step": "generated", "message": "Draft written — enforcing the line budget...", "progress": 34}

        # Deterministic length control before any scoring: the page constraint is
        # mechanical, so satisfy it mechanically rather than spending judge passes on it.
        if budget:
            self._tightened, self._tightened_latex = 0, latex
            async for ev in self._tighten(latex):
                yield ev
            latex = self._tightened_latex
            if self._tightened:
                yield {"step": "tightened",
                       "message": f"Tightened {self._tightened} bullet(s) to fit one page",
                       "progress": 37, "data": {"count": self._tightened}}

            # Craft repair goes BEFORE expansion, not after: adding a missing outcome makes
            # a bullet longer, and on an already-full page those repairs get rejected for
            # overflowing. Repair first, then let expansion spend whatever room is left.
            async for ev in self._repair_craft(latex, dump):
                yield ev
            latex = self._repaired_latex

            # Symmetric fill: tightening overshoots, so give the reclaimed lines back as
            # substance — and use them to land keywords that are still missing.
            still_missing = [k for k in supported
                             if not keyword_in_text(k, latex.lower())]
            async for ev in self._expand(latex, dump, still_missing):
                yield ev
            latex = self._expanded_latex
            if self._expanded:
                yield {"step": "expanded",
                       "message": f"Expanded {self._expanded} bullet(s) to fill the page",
                       "progress": 39, "data": {"count": self._expanded}}
        yield {"step": "generated", "message": "Starting evaluation loop...", "progress": 40}

        # ── 3. Evaluate ↔ refine loop ──
        best = {"latex": latex, "evaluation": None, "total": -1}
        stalls = 0
        history = []

        for iteration in range(1, max_iterations + 1):
            yield {"step": "evaluating", "phase": "evaluate", "iteration": iteration,
                   "message": f"Evaluation pass {iteration}: ATS scan + recruiter read + fact-check...",
                   "progress": min(40 + iteration * 14, 92)}

            # Measure the real page count instead of guessing — this drives page_fit,
            # catches documents that don't compile, and gives the judges something to look at.
            compiled = await compile_pdf(latex)
            checks = run_checks(
                latex,
                must_have=[k for k in jd_analysis.get("must_have_keywords", []) if k in supported],
                nice_to_have=[k for k in jd_analysis.get("nice_to_have_keywords", []) if k in supported],
                unsupported=absent,
                compiled=compiled,
            )
            if checks.pages is not None:
                yield {"step": "compiled", "iteration": iteration,
                       "message": f"Compiled: {checks.pages} page(s)"
                                  + ("" if checks.pages == 1 else " — must be cut to one"),
                       "data": {"pages": checks.pages, "compile_ok": checks.compile_ok}}
            elif checks.compile_ok is False:
                yield {"step": "compiled", "iteration": iteration,
                       "message": f"LaTeX did not compile: {checks.compile_error}",
                       "data": {"pages": None, "compile_ok": False}}

            try:
                evaluation = None
                async for ev in self._judge(
                    latex=latex, dump=dump, job_description=job_description,
                    jd_analysis=jd_analysis, checks=checks, pdf=compiled.pdf,
                ):
                    if ev.get("step") == "verdict":
                        evaluation = ev["evaluation"]
                    else:
                        yield ev
            except (ProviderError, AgentError) as e:
                # Never throw away a good draft over a flaky later pass.
                if best["evaluation"] is not None:
                    yield {"step": "degraded",
                           "message": f"Evaluation pass {iteration} failed ({e}); shipping the best draft so far."}
                    break
                raise

            evaluation["local_checks"] = checks.to_dict()

            # A resume that doesn't fit one page can never pass, whatever the judges said.
            if checks.pages is not None and checks.pages > 1:
                evaluation["verdict"] = "revise"
                evaluation["scores"]["page_fit"]["score"] = min(evaluation["scores"]["page_fit"]["score"], 2)
                evaluation["scores"]["page_fit"]["evidence"] = (
                    f"Compiled PDF is {checks.pages} pages — must be exactly one."
                )
                evaluation["issues"].insert(0, {
                    "category": "page_fit",
                    "fix": f"The compiled PDF is {checks.pages} pages. Cut or merge the least JD-relevant "
                           "content until it fits ONE page. Do not touch margins, font size, \\textheight, "
                           "\\topmargin, or vertical spacing.",
                })
                evaluation["total"] = sum(
                    min(max(0, evaluation["scores"][cat]["score"]), cap) for cat, cap in SCORE_CAPS.items()
                )
            history.append({"iteration": iteration, "total": evaluation["total"], "verdict": evaluation["verdict"]})
            yield {
                "step": "evaluated", "iteration": iteration,
                "message": f"Score: {evaluation['total']}/100 ({evaluation['verdict']}) — "
                           f"{len(evaluation['issues'])} issue(s)",
                "progress": min(46 + iteration * 14, 94),
                "data": {
                    "total": evaluation["total"],
                    "verdict": evaluation["verdict"],
                    "scores": evaluation["scores"],
                    "issues": evaluation["issues"],
                    "local_checks": evaluation["local_checks"],
                },
            }

            # Hard constraints dominate the score: a one-page draft always beats a
            # higher-scoring two-page draft, because two pages is not a resume.
            hard_ok = (checks.pages == 1) if checks.pages is not None else (checks.compile_ok is not False)
            candidate = {"latex": latex, "evaluation": evaluation, "total": evaluation["total"], "hard_ok": hard_ok}
            if best["evaluation"] is None:
                improved, best = True, candidate
            elif hard_ok and not best.get("hard_ok"):
                improved, best = True, candidate          # first draft that actually fits
            elif hard_ok == best.get("hard_ok") and evaluation["total"] > best["total"]:
                improved, best = True, candidate
            else:
                improved = False

            if hard_ok and (evaluation["verdict"] == "pass" or evaluation["total"] >= PASS_THRESHOLD):
                break
            # Measured judge noise is sd 1.17 / spread 3, so a change under that is not
            # real. A large drop is unambiguous degradation and ends the loop at once;
            # smaller ones need two in a row before we give up on further refinement.
            drop = best["total"] - evaluation["total"]
            if hard_ok and drop >= JUDGE_NOISE_PTS * 3:
                yield {"step": "degraded",
                       "message": f"That pass scored {drop} points below the best draft — "
                                  "refinement is going backwards, stopping here."}
                break
            if hard_ok and drop >= JUDGE_NOISE_PTS:
                stalls += 1
                if stalls >= 2:
                    yield {"step": "degraded",
                           "message": "Two passes in a row scored below the best draft — stopping there."}
                    break
            else:
                stalls = 0
            if iteration == max_iterations:
                break
            out_of_time = time.monotonic() - run_start > RUN_BUDGET_SECONDS
            # Only a satisfied hard constraint earns the right to stop early. While the
            # resume still doesn't fit or doesn't compile, keep working even if the score
            # stalled or dipped — giving up here is what shipped two-page resumes.
            if hard_ok and (not improved or out_of_time):
                yield {"step": "degraded",
                       "message": "Score stopped improving — shipping the best draft."
                                  if not improved else "Time budget reached — shipping the best draft."}
                break
            if out_of_time and not hard_ok:
                yield {"step": "degraded",
                       "message": "Out of time with the page constraint unmet — trimming to one page directly."}
                break

            worklist = _prioritize(evaluation["issues"], evaluation["scores"])
            yield {"step": "refining", "phase": "refine", "iteration": iteration,
                   "message": f"Fixing the {len(worklist)} highest-leverage issue(s) "
                              f"(of {len(evaluation['issues'])} found)...",
                   "progress": min(50 + iteration * 14, 95)}

            # A consolidated respecification, not an ever-growing chat history: restate the
            # spec, the measured state, and a SHORT worklist. Long histories degrade
            # instruction-following, and large simultaneous critique lists cause regressions.
            budget_line = prompts.BUDGET_BRIEF.format(**_budget_plan(budget)) if budget else ""
            respec = prompts.RESPEC_PROMPT.format(
                budget_line=budget_line,
                job_description=job_description,
                coverage=(", ".join(supported) or "(none)") + (
                    f"\nNEVER ADD: {', '.join(absent)}" if absent else ""),
                local_checks=checks.summary(),
                n=len(worklist),
                issues="\n".join(f"{n}. [{i['category']}] {i['fix']}" for n, i in enumerate(worklist, 1)),
                latex=latex,
            )
            respec_content = [
                {"type": "text", "text": prompts.TEMPLATE_INSTRUCTION.format(template=template_latex),
                 "cache_control": {"type": "ephemeral"}},
            ] + dump.as_content_blocks(respec)
            msg = None
            try:
                async for ev in self._stream_call(
                    phase="refine", system=writer_system,
                    messages=[{"role": "user", "content": respec_content}],
                ):
                    if ev["event"] == "message":
                        msg = ev["message"]
                    else:
                        yield {"step": "live", **ev}
                latex = _extract_latex(_text_of(msg))
            except (ProviderError, AgentError) as e:
                yield {"step": "degraded",
                       "message": f"Refinement failed ({e}); shipping the best draft so far."}
                break

        if best["evaluation"] is None:  # e.g. every pass scored 0 — still ship what we have
            best = {"latex": latex, "evaluation": evaluation, "total": evaluation["total"], "hard_ok": False}
        evaluation = best["evaluation"]

        # Last-resort guarantee: never hand back more than one page. If the model
        # couldn't get there, trim bullets deterministically until it compiles to one.
        if not best.get("hard_ok"):
            yield {"step": "fitting", "phase": "fit",
                   "message": "Enforcing one page by trimming the lowest-value content...", "progress": 96}
            fitted, removed, final_compile = await fit_to_one_page(best["latex"], compile_pdf)
            if removed:
                best["latex"] = fitted
                pages_now = final_compile.pages if final_compile else None
                yield {"step": "fitted",
                       "message": f"Removed {removed} lowest-value bullet(s) to fit one page"
                                  + (f" (now {pages_now} page)" if pages_now else ""),
                       "data": {"removed": removed, "pages": pages_now}}
                if pages_now == 1:
                    evaluation["scores"]["page_fit"]["score"] = 7
                    evaluation["scores"]["page_fit"]["evidence"] = (
                        f"Trimmed to exactly one page ({removed} bullet(s) removed automatically)."
                    )
                    evaluation["issues"] = [i for i in evaluation["issues"] if i["category"] != "page_fit"]
                    evaluation["total"] = sum(
                        min(max(0, evaluation["scores"][cat]["score"]), cap) for cat, cap in SCORE_CAPS.items()
                    )


        # ── Widow repair: the judges' most frequent page_fit complaint is a bullet whose
        # last line holds one or two words. It is detectable from character counts, so it
        # is fixed mechanically rather than argued about in another judge pass.
        widows = bullets_with_widows(best["latex"])
        if widows:
            yield {"step": "widows", "phase": "tighten",
                   "message": f"Removing {len(widows)} widow line(s) — bullets whose last line holds a word or two",
                   "data": {"count": len(widows)}}
            listing = "\n\n".join(
                f"[{w['index']}] currently {w['chars']} characters, target {w['target']} "
                f"(its last line holds only {w['tail']} characters):\n{w['text']}"
                for w in widows[:8]
            )
            try:
                res = None
                async for ev in self._stream_call(
                    phase="tighten", system=prompts.TIGHTEN_SYSTEM,
                    messages=[{"role": "user", "content": prompts.TIGHTEN_PROMPT.format(
                        n=len(widows[:8]), bullets=listing)}],
                    output_schema=prompts.TIGHTEN_SCHEMA,
                ):
                    if ev["event"] == "message":
                        res = _parse_json(_text_of(ev["message"]))
                    else:
                        yield {"step": "live", **ev}
                tgt = {w["index"]: w["target"] for w in widows[:8]}
                ok = {b["index"]: b["text"] for b in (res or {}).get("bullets", [])
                      if b["index"] in tgt and b["text"].strip() and len(b["text"]) <= tgt[b["index"]]}
                if ok:
                    candidate = replace_bullets(best["latex"], ok)
                    verify = await compile_pdf(candidate)
                    if verify.ok and verify.pages == 1:
                        best["latex"] = candidate
                        yield {"step": "widows_fixed", "message": f"Removed {len(ok)} widow line(s)",
                               "data": {"count": len(ok)}}
            except (ProviderError, AgentError) as e:
                yield {"step": "widows_fixed", "message": f"Widow repair skipped ({e})", "data": {"count": 0}}

        # ── Final editorial audit: the craft layer. Content is frozen; this fixes only
        # consistency, typography and widow lines. Accepted only if the result still fits
        # one page and keeps every keyword — otherwise discarded.
        yield {"step": "auditing", "phase": "audit",
               "message": "Final audit: date formats, parallel grammar, bold discipline, widow lines...",
               "progress": 97}
        try:
            audit_prompt = prompts.AUDIT_PROMPT.format(
                local_checks=checks.summary(),
                keywords=", ".join(checks.must_have.matched) or "(none recorded)",
                latex=best["latex"],
            )
            audited = None
            async for ev in self._stream_call(
                phase="audit", system=prompts.AUDIT_SYSTEM,
                messages=[{"role": "user", "content": audit_prompt}],
                output_schema=prompts.AUDIT_SCHEMA,
            ):
                if ev["event"] == "message":
                    audited = _parse_json(_text_of(ev["message"]))
                else:
                    yield {"step": "live", **ev}

            candidate_latex = _extract_latex(audited["latex"])
            verify = await compile_pdf(candidate_latex)
            recheck = run_checks(
                candidate_latex,
                must_have=[k for k in jd_analysis.get("must_have_keywords", []) if k in supported],
                nice_to_have=[k for k in jd_analysis.get("nice_to_have_keywords", []) if k in supported],
                unsupported=absent, compiled=verify,
            )
            safe = (
                verify.ok and verify.pages == 1
                and recheck.must_have.coverage >= checks.must_have.coverage
                and not spacing_crush_reason(best["latex"], candidate_latex)
            )
            if safe:
                best["latex"] = candidate_latex
                evaluation["local_checks"] = recheck.to_dict()
                yield {"step": "audited",
                       "message": f"Applied {len(audited['corrections'])} editorial correction(s)",
                       "data": {"corrections": audited["corrections"]}}
            else:
                yield {"step": "audited",
                       "message": "Audit changes rejected — they would have hurt fit or keyword coverage",
                       "data": {"corrections": []}}
        except (ProviderError, AgentError) as e:
            yield {"step": "audited", "message": f"Audit skipped ({e})", "data": {"corrections": []}}

        yield {
            "step": "result",
            "message": f"Done — final score {best['total']}/100 after {len(history)} evaluation pass(es)",
            "progress": 100,
            "result": {
                "latex": best["latex"],
                "score": best["total"],
                "verdict": evaluation["verdict"],
                "scores": evaluation["scores"],
                "issues": evaluation["issues"],
                "local_checks": evaluation["local_checks"],
                "jd_analysis": jd_analysis,
                "coverage_plan": {"supported": plan.get("supported", []), "absent": absent},
                "aggressiveness": aggressiveness,
                "iterations": history,
            },
        }

    # ── Chat editing ─────────────────────────────────────────────────

    async def edit(
        self,
        latex: str,
        instruction: str,
        chat_history: Optional[List[Dict]] = None,
        job_description: str = "",
    ) -> AsyncGenerator[Dict, None]:
        """Apply one conversational edit to the resume. Yields live events, then step=result.

        chat_history is a list of {"role": "user"|"assistant", "content": str}
        prior turns (instructions and editor replies only — not old LaTeX).
        """
        history_text = ""
        if chat_history:
            history_text = "\n".join(
                f"{'USER' if t['role'] == 'user' else 'EDITOR'}: {t['content']}" for t in chat_history[-12:]
            )

        # Split so the document + JD (stable across edits in a session) can be cached,
        # and only the short instruction varies per request.
        stable = prompts.EDIT_PROMPT.format(
            latex=latex,
            history=history_text or "(none yet)",
            instruction="",
            job_description=job_description[:6000] or "(not provided)",
        )
        content = [
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": instruction},
        ]

        result = None
        async for ev in self._stream_call(
            phase="edit",
            system=prompts.EDITOR_SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_schema=prompts.EDIT_SCHEMA,
        ):
            if ev["event"] == "message":
                result = _parse_json(_text_of(ev["message"]))
            else:
                yield {"step": "live", **ev}

        changed = bool(result.get("changed", True))
        if not changed:
            yield {"step": "result", "result": {"latex": latex, "reply": result["reply"], "changed": False}}
            return

        if result.get("mode") == "rewrite" and result.get("latex", "").strip():
            new_latex = _extract_latex(result["latex"])
        else:
            new_latex = apply_patches(latex, result.get("edits") or [])

        crushed = spacing_crush_reason(latex, new_latex)
        if crushed:
            raise AgentError(crushed)

        yield {
            "step": "result",
            "result": {"latex": new_latex, "reply": result["reply"], "changed": True},
        }
