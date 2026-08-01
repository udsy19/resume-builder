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
from .latex import (LINE_COST_PT, bullets_to_expand, bullets_with_widows, craft_defects,
                    bullets_to_tighten, compile_pdf, date_format_defects, fit_to_one_page,
                    line_arithmetic, measure_fit, replace_bullets, skeleton_line_budget,
                    vmock_defects)
from . import repair
from .providers import ProviderError, _as_provider_error, make_provider
from .validator import (keyword_in_text, locate_defects, run_checks, score_craft,
                        spacing_crush_reason)

MODEL = "claude-opus-5"
MAX_TOKENS = 32000
JUDGE_NOISE_PTS = 3  # measured: sd 1.17, spread 3 over k=5 identical re-scores
PASS_THRESHOLD = 95  # out of 100 — the ResumeWorded/VMock bar, not "good enough"
# Wall-clock ceiling for a whole run. The default is what a COMPLETE run costs, measured:
# ~130s to analyse and generate, ~60s of deterministic polish, then four evaluate/refine
# cycles. Keyword coverage is what those later cycles buy — at two cycles it measured
# 82%, at four, 100% — so a smaller budget does not buy a faster resume, it buys a worse
# one. Serverless deployments that cannot run this long must set RUN_BUDGET_SECONDS to
# their platform ceiling and accept a reduced loop; run() says so explicitly when they do.
RUN_BUDGET_SECONDS = int(os.environ.get("RUN_BUDGET_SECONDS", "900"))
# Below this there is not enough room for the refine cycles that land the last keywords.
FULL_LOOP_SECONDS = 900
# The budget covers the WHOLE run, so the closing phases have to be paid for out of it.
# Measured on live runs: the one-page fit solver, widow repair and audit together cost
# ~80s. The loop stops that far short of the deadline so it can still afford them —
# without this reserve the run overshoots by the entire length of the tail.
TAIL_RESERVE_SECONDS = int(os.environ.get("TAIL_RESERVE_SECONDS", "80"))
# Measured cost of one tighten / repair / expand round. These phases are polish: when
# time is short they are skipped so the evaluation loop, which is not optional, still runs.
POLISH_ROUND_SECONDS = 20
# Measured cost of one refine + re-evaluate cycle. Since refinement returns patches
# instead of a re-emitted document this is ~25-45s of refining plus ~40s across the three
# parallel judges, against ~190s before. The occasional structural rewrite still costs the
# old amount, so the reserve keeps some headroom over the common case.
# A cycle is only started if it can finish: a half-finished refinement is worth nothing,
# because the loop ships the champion draft either way.
REFINE_CYCLE_SECONDS = int(os.environ.get("REFINE_CYCLE_SECONDS", "110"))
# Hard ceiling on any one judge lens, including its retry. A lens normally takes 25-45s.
# This exists because the SDK's per-request timeout only catches a stalled read: a stream
# that trickles keeps every read alive, and one run sat in the fan-out for 72 minutes.
LENS_TIMEOUT_SECONDS = int(os.environ.get("LENS_TIMEOUT_SECONDS", "150"))
# Stop filling once fewer than this many rendered lines remain free. A resume is
# meant to use its one page; leftover space is wasted, and it is the second
# largest scoring loss after writing quality.
FILL_TARGET_LINES = float(os.environ.get("FILL_TARGET_LINES", "1.0"))
# Deterministic writing repair, OFF by default.
#
# It was built to close the largest gap on the scorecard, and a paired A/B over three
# batches did not support it: per-pair totals came out +8, +2, -7, a mean of +1.0 that
# sits inside the measured +/-3 judge-noise band, and no metric moved the same way in
# every pair. The run that lost 7 points also lost 12.5 points of keyword coverage.
# Unbounded, an earlier version cost 5 points of truthfulness.
#
# So: a model call and a truthfulness risk buying no demonstrated benefit. The code and
# the switch stay, because three pairs is a small sample and this is "not shown to help"
# rather than "shown not to help" — set WRITING_REPAIR=1 to run it, and add pairs before
# concluding either way.
WRITING_REPAIR = os.environ.get("WRITING_REPAIR", "0").strip().lower() not in ("0", "false", "no")
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


class _Clock:
    """Wall-clock budget for one run.

    Held on the agent so every phase can consult it, not just the refine loop. Checking
    it only between refine iterations was not enough: generation plus the polish phases
    alone cost ~194s of a 250s budget on a measured run, so the loop reached its first
    deadline check having already spent the budget, and the closing phases then ran
    unbilled on top.
    """

    def __init__(self, budget: float):
        self.start = time.monotonic()
        self.budget = budget

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def left(self) -> float:
        return self.budget - self.elapsed()

    def expired(self, reserve: float = 0.0) -> bool:
        """True when less than `reserve` seconds remain — i.e. too late to start work
        that costs about `reserve` and still finish inside the budget."""
        return self.left() <= reserve


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
        # Replaced per-run by run(); this default keeps edit() and any direct phase call
        # (tests, the chat editor) unbudgeted rather than instantly "expired".
        self._clock = _Clock(float("inf"))
        self._last_craft_obs: Dict = {}
        self._wrepaired, self._wrepaired_latex = 0, ""
        self._wrepair_done = False
        # Token usage accumulates across every call in a run so the cost of a
        # run is reportable instead of invisible.
        self.usage = {"input": 0, "output": 0, "cached": 0, "calls": 0}

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
        # Transport failures arrive as raw httpx exceptions, not ProviderError, so they
        # used to sail past every degradation path and abort an otherwise healthy run —
        # a single stalled read during tightening killed a full live run. Normalising them
        # here, at the one chokepoint every phase goes through, lets callers degrade.
        stream = self.provider.stream(
            phase=phase, system=system, blocks=blocks, schema=output_schema,
            effort=EFFORT.get(phase, "high"), max_tokens=MAX_TOKENS,
        )
        try:
            async for ev in stream:
                if ev["event"] == "done":
                    result = ev
                elif ev["event"] == "writing":
                    yield {"event": "writing", "phase": phase, "section": None, "chars": ev["chars"]}
                else:
                    yield ev
        except (ProviderError, AgentError):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise _as_provider_error(e) from e

        if result is None:
            raise AgentError("The model returned nothing.")
        text = result["text"]
        if not text.strip():
            raise AgentError("The model returned an empty response. Try again.")
        u = result.get("usage")
        if u is not None:
            self.usage["input"] += getattr(u, "input_tokens", 0) or 0
            self.usage["output"] += getattr(u, "output_tokens", 0) or 0
            self.usage["cached"] += getattr(u, "cached_tokens", 0) or 0
            self.usage["calls"] += 1
        yield {"event": "message", "message": _Result(text, u)}

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

            # One transient failure used to cost the entire run: a lens that errored took
            # the whole evaluation with it, and on the first pass there was no earlier
            # draft to fall back to, so a finished one-page resume was thrown away. Each
            # lens gets one retry before it is called lost.
            last_error = None
            for attempt in (1, 2):
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
                except asyncio.CancelledError:
                    raise
                except Exception as e:                  # one lens failing must not hang the rest
                    last_error = e
                    result = None
                if result is not None:
                    await queue.put({"__done__": name, "result": result})
                    return
                if attempt == 1:
                    await queue.put({"step": "retry", "lens": name,
                                     "message": f"{name} judge failed ({last_error}) — retrying once."})
            await queue.put({"__done__": name, "result": None,
                             "error": str(last_error) if last_error else "returned no result"})

        async def guarded(name: str, lens: Dict, queue: asyncio.Queue):
            """Bound each lens in wall-clock time.

            The per-request SDK timeout only fires when a read stalls; a stream that
            trickles slowly keeps every individual read alive and can run indefinitely.
            One observed run sat in this fan-out for 72 minutes because a lens never
            finished and never errored. The fan-out therefore enforces its own deadline.
            """
            try:
                await asyncio.wait_for(one(name, lens, queue), timeout=LENS_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                await queue.put({"__done__": name, "result": None,
                                 "error": f"no verdict within {LENS_TIMEOUT_SECONDS}s"})

        queue: asyncio.Queue = asyncio.Queue()
        tasks = [asyncio.create_task(guarded(n, l, queue)) for n, l in prompts.JUDGE_LENSES.items()]
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
            # Name the reason, not just the lens. "Evaluation lens failed: ats" cost real
            # debugging time because the actual cause was thrown away here.
            detail = "; ".join(f"{n}: {why}" for n, why in errors.items())
            raise AgentError(f"Evaluation lens failed — {detail}")

        scores: Dict[str, Dict] = {}
        issues: List[Dict] = []
        for name, res in results.items():
            scores.update(res["scores"])
            issues += res.get("issues") or []
            # The craft judge enumerates writing defects rather than scoring them; the
            # number is arithmetic over quotes verified against the document, so a draft
            # with fewer defects cannot score the same as one with more.
            obs = res.get("craft_observations")
            if obs is not None:
                self._last_craft_obs = obs
                craft = score_craft(obs, latex)
                scores["writing_quality"] = {"score": craft["score"], "evidence": craft["evidence"]}
                yield {"step": "craft_scored", "lens": name,
                       "message": f"Writing quality {craft['score']}/20 from "
                                  f"{sum(craft['counts'].values())} verified defect(s)",
                       "data": {"counts": craft["counts"], "rejected": craft["rejected"]}}
                # Every verified defect becomes a concrete worklist item, so the refine
                # pass fixes the exact bullets the score was computed from.
                for field, entries in obs.items():
                    for e in entries or []:
                        issues.append({
                            "category": "writing_quality",
                            "fix": f"[{field.replace('_', ' ')}] \"{e.get('quote', '')[:120]}\" — "
                                   f"{e.get('fix', 'rewrite it')}",
                        })

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
        prev_headroom = None
        for _ in range(rounds):
            if self._clock.expired(TAIL_RESERVE_SECONDS + POLISH_ROUND_SECONDS):
                yield {"step": "skipped", "phase": "tighten",
                       "message": "Skipping further tightening — not enough time left in the run budget."}
                break
            fit = await asyncio.to_thread(measure_fit, latex)
            if not fit.ok or fit.headroom_pt is None:
                break
            if fit.pages == 1 and fit.headroom_pt >= 0:
                break
            # A round that does not claw back any height is not going to succeed on the
            # next attempt either: an observed run spent 146s tightening twice against an
            # unchanged 186pt overflow. The deterministic fit solver handles this case
            # properly at the end, so hand off rather than spend more rounds on it.
            if prev_headroom is not None and fit.headroom_pt <= prev_headroom + 1.0:
                yield {"step": "skipped", "phase": "tighten",
                       "message": f"Tightening stopped making progress ({round(-fit.headroom_pt)}pt "
                                  "still over) — the one-page solver will finish the job.",
                       "data": {"headroom_pt": round(fit.headroom_pt, 1)}}
                break
            prev_headroom = fit.headroom_pt
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
            try:
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
            except (ProviderError, AgentError) as e:
                # Tightening is deterministic polish; the one-page guarantee is enforced
                # again by the fit solver at the end, so a failed round is survivable.
                yield {"step": "skipped", "phase": "tighten",
                       "message": f"Tightening round failed ({e}) — continuing."}
                break

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
            if self._clock.expired(TAIL_RESERVE_SECONDS + POLISH_ROUND_SECONDS):
                yield {"step": "skipped", "phase": "expand",
                       "message": "Skipping page fill — not enough time left in the run budget."}
                break
            fit = await asyncio.to_thread(measure_fit, latex)
            if not fit.ok or fit.headroom_pt is None or fit.pages != 1:
                break
            lines_free = fit.headroom_pt / LINE_COST_PT
            # Was 2. A page with a line and a half spare ships visibly short, and page_fit
            # measured 7.2/10 across runs for exactly that reason — the judges see the
            # whitespace even when the score sheet says one page. One line of slack is the
            # smallest gap worth leaving, and it is measured rather than argued.
            if lines_free < FILL_TARGET_LINES:
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
            try:
                async for ev in self._stream_call(
                    phase="expand", system=prompts.EXPAND_SYSTEM,
                    messages=[{"role": "user", "content": dump.as_content_blocks(prompt)}],
                    output_schema=prompts.EXPAND_SCHEMA,
                ):
                    if ev["event"] == "message":
                        result = _parse_json(_text_of(ev["message"]))
                    else:
                        yield {"step": "live", **ev}
            except (ProviderError, AgentError) as e:
                # Page fill is optional; an under-filled page still ships.
                yield {"step": "skipped", "phase": "expand",
                       "message": f"Page fill failed ({e}) — continuing."}
                break

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
            check = await asyncio.to_thread(measure_fit, candidate)
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
        if self._clock.expired(TAIL_RESERVE_SECONDS + POLISH_ROUND_SECONDS):
            yield {"step": "skipped", "phase": "repair",
                   "message": f"Skipping repair of {len(defects)} writing defect(s) — "
                              "not enough time left in the run budget."}
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
        self._clock = _Clock(RUN_BUDGET_SECONDS)
        clock = self._clock
        self._wrepair_done = False
        if RUN_BUDGET_SECONDS < FULL_LOOP_SECONDS:
            # Silent truncation would read as a complete run that simply scored lower.
            yield {"step": "reduced_loop", "phase": "analyze",
                   "message": f"Time budget is {RUN_BUDGET_SECONDS}s; a full loop needs about "
                              f"{FULL_LOOP_SECONDS}s. This run will stop early, and the last "
                              "few job-description keywords are the part most likely to be missing.",
                   "data": {"budget_seconds": RUN_BUDGET_SECONDS, "full_loop_seconds": FULL_LOOP_SECONDS}}

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
                # Nothing scored yet. The draft is still written, tightened and compiled to
                # one page, so shipping it unscored beats raising and handing back nothing —
                # which is what an evaluation failure on the first pass used to do.
                if checks.compile_ok is not False:
                    yield {"step": "degraded",
                           "message": f"Scoring failed ({e}). The resume itself is finished and fits "
                                      "one page, so it is below — it just has no score attached. "
                                      "Re-run to have it evaluated."}
                    evaluation = {
                        "total": None,
                        "verdict": "unscored",
                        "scores": {cat: {"score": 0, "evidence": "Not scored — evaluation failed."}
                                   for cat in SCORE_CAPS},
                        "issues": [],
                        "local_checks": checks.to_dict(),
                    }
                    best = {"latex": latex, "evaluation": evaluation,
                            "total": None, "hard_ok": checks.pages == 1}
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
            # Enough time for another refine AND the re-evaluation that judges it AND the
            # closing phases — not merely "the deadline has not passed yet". Asking the
            # weaker question is what let a run spend its whole budget before the first
            # check and then run the tail on top of it.
            out_of_time = clock.expired(TAIL_RESERVE_SECONDS + REFINE_CYCLE_SECONDS)
            # Only a satisfied hard constraint earns the right to stop early. While the
            # resume still doesn't fit or doesn't compile, keep working even if the score
            # stalled or dipped — giving up here is what shipped two-page resumes.
            if hard_ok and (not improved or out_of_time):
                yield {"step": "degraded",
                       "message": "Score stopped improving — shipping the best draft."
                                  if not improved else
                                  f"Not enough of the {int(clock.budget)}s budget left for another "
                                  f"refine-and-re-evaluate cycle ({int(clock.left())}s remaining) — "
                                  "shipping the best draft."}
                break
            if out_of_time and not hard_ok:
                yield {"step": "degraded",
                       "message": "Out of time with the page constraint unmet — trimming to one page directly."}
                break

            # Writing defects are repaired directly from their verified quotes rather
            # than competing for the six worklist slots, so the worklist can spend all
            # six on things only a rewrite can address.
            # Once per run, not once per cycle. Running it every cycle meant up to
            # thirty bullet rewrites, which cost truthfulness five points on a measured
            # run and burned enough time to lose a refine cycle. The first pass fixes
            # the bulk; the judges still catch what is left through the worklist.
            if WRITING_REPAIR and self._last_craft_obs and not self._wrepair_done:
                self._wrepair_done = True
                async for ev in self._repair_writing(latex, dump, self._last_craft_obs):
                    yield ev
                if self._wrepaired:
                    latex = self._wrepaired_latex

            worklist = _prioritize(
                [i for i in evaluation["issues"] if i.get("category") != "writing_quality"]
                if self._wrepaired else evaluation["issues"],
                evaluation["scores"],
            )
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
            # Patches, not a re-emitted document. Refinement was ~58% of a full run's wall
            # clock purely because fixing six bullets meant writing the whole resume out
            # again — several thousand output tokens to change a few hundred. The writer
            # can still ask for a rewrite when the change is genuinely structural.
            msg = None
            try:
                async for ev in self._stream_call(
                    phase="refine", system=writer_system,
                    messages=[{"role": "user", "content": respec_content}],
                    output_schema=prompts.EDIT_SCHEMA,
                ):
                    if ev["event"] == "message":
                        msg = ev["message"]
                    else:
                        yield {"step": "live", **ev}
                revision = _parse_json(_text_of(msg))
            except (ProviderError, AgentError) as e:
                yield {"step": "degraded",
                       "message": f"Refinement failed ({e}); shipping the best draft so far."}
                break

            previous = latex
            if revision.get("mode") == "patch" and revision.get("edits"):
                try:
                    latex = apply_patches(latex, revision["edits"])
                    yield {"step": "patched", "iteration": iteration,
                           "message": f"Applied {len(revision['edits'])} targeted edit(s)"
                                      + (f" — {revision['reply']}" if revision.get("reply") else ""),
                           "data": {"edits": len(revision["edits"])}}
                except AgentError as e:
                    # An anchor that no longer matches means the patch set is unusable.
                    # Keep the draft rather than applying it half-way.
                    latex = previous
                    yield {"step": "degraded",
                           "message": f"Targeted edits did not apply ({e}); keeping the current draft."}
            elif revision.get("latex", "").strip():
                latex = _extract_latex(revision["latex"])
            else:
                yield {"step": "degraded",
                       "message": "Refinement returned no usable change; keeping the current draft."}

            # A patch set can still produce a document that will not build. Verify before
            # the next evaluation spends three judges on it, and roll back if it broke.
            if latex is not previous:
                verify = await compile_pdf(latex)
                if not verify.ok:
                    latex = previous
                    yield {"step": "degraded",
                           "message": "That revision no longer compiled, so it was rolled back."}

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

        # ── Final fill: the refine cycles cut content, and nothing put it back.
        # Expansion runs once, before the loop. Every refine pass after that trims — a
        # bullet split, a claim cut, a widow removed — so the shipped page was routinely
        # emptier than the one that was measured, which is what held page_fit at 7.2/10.
        # Measure what is actually left and spend it.
        if not clock.expired(TAIL_RESERVE_SECONDS):
            final_fit = await asyncio.to_thread(measure_fit, best["latex"])
            if final_fit.ok and final_fit.pages == 1 and final_fit.headroom_pt is not None:
                free = final_fit.headroom_pt / LINE_COST_PT
                if free >= FILL_TARGET_LINES + 0.5:
                    yield {"step": "filling", "phase": "expand",
                           "message": f"Page has ~{free:.1f} lines spare after refinement — filling it",
                           "data": {"lines_free": round(free, 1)}}
                    async for ev in self._expand(best["latex"], dump, rounds=1):
                        yield ev
                    if self._expanded:
                        verify = await compile_pdf(self._expanded_latex)
                        if verify.ok and verify.pages == 1:
                            best["latex"] = self._expanded_latex
                            yield {"step": "filled",
                                   "message": f"Filled the page with {self._expanded} expanded bullet(s)",
                                   "data": {"count": self._expanded}}

        # ── Final editorial audit: the craft layer. Content is frozen; this fixes only
        # consistency, typography and widow lines. Accepted only if the result still fits
        # one page and keeps every keyword — otherwise discarded.
        audit_worth_it = bool(
            craft_defects(best["latex"]) or bullets_with_widows(best["latex"])
            or date_format_defects(best["latex"]) or vmock_defects(best["latex"])
        )
        if not audit_worth_it:
            # The audit costs 70-80s and only ever fixes typography and consistency.
            # When every mechanical checker reports the document clean there is nothing
            # for it to find, and the time is better left unspent.
            yield {"step": "skipped", "phase": "audit",
                   "message": "Skipping the final audit — no typography or consistency "
                              "defects were detected.", "progress": 97}
            audited = None
        elif clock.expired():
            yield {"step": "skipped", "phase": "audit",
                   "message": f"Skipping the final audit — the {int(clock.budget)}s run budget is spent. "
                              "The draft below is the champion; run again for the editorial pass.",
                   "progress": 97}
            audited = None
        else:
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
            "message": (f"Done — final score {best['total']}/100 after {len(history)} evaluation pass(es)"
                        if best["total"] is not None else
                        "Done — resume finished, but scoring failed so it is unscored"),
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
                "usage": {**self.usage, "provider": self.provider.name,
                          "model": self.provider.model,
                          "seconds": round(clock.elapsed(), 1)},
            },
        }



    async def _repair_writing(self, latex: str, dump, observations: Dict, max_bullets: int = 10):
        """Repair judge-found writing defects directly, instead of rationing them.

        The refine worklist sends six issues per cycle out of twenty to forty found, so
        most writing defects were reported and never fixed — which is why writing_quality
        was the largest remaining loss. These arrive as quotes verified against the
        document, so they can be targeted the same way craft defects are: exact bullet,
        exact target, accepted only if the rewrite verifies.
        """
        self._wrepaired, self._wrepaired_latex = 0, latex
        defects = locate_defects(observations, latex)[:max_bullets]
        if not defects:
            return
        if self._clock.expired(TAIL_RESERVE_SECONDS + POLISH_ROUND_SECONDS):
            yield {"step": "skipped", "phase": "repair",
                   "message": f"Skipping {len(defects)} writing repair(s) — out of time."}
            return

        yield {"step": "wrepairing", "phase": "repair",
               "message": f"Repairing {len(defects)} bullet(s) the judges flagged for writing",
               "data": {"count": len(defects)}}

        result = None
        try:
            async for ev in self._stream_call(
                phase="repair", system=repair.WRITING_SYSTEM,
                messages=[{"role": "user", "content": dump.as_content_blocks(
                    repair.WRITING_PROMPT.format(
                        n=len(defects), bullets=repair.format_writing_defects(defects)))}],
                output_schema=repair.CRAFT_SCHEMA,
            ):
                if ev["event"] == "message":
                    result = _parse_json(_text_of(ev["message"]))
                else:
                    yield {"step": "live", **ev}
        except (ProviderError, AgentError) as e:
            yield {"step": "wrepaired", "message": f"Writing repair skipped ({e})", "data": {"count": 0}}
            return

        budget = {d["index"]: max(240, d["chars"]) for d in defects}
        accepted = {
            b["index"]: b["text"] for b in (result or {}).get("bullets", [])
            if b["index"] in budget and b["text"].strip() and not b.get("unchanged")
            and len(b["text"]) <= budget[b["index"]]
        }
        if not accepted:
            yield {"step": "wrepaired", "message": "No writing repairs were usable", "data": {"count": 0}}
            return

        candidate = replace_bullets(latex, accepted)
        # A repair that breaks the build or spills the page is worse than the defect.
        verify = await compile_pdf(candidate)
        if not verify.ok or verify.pages != 1:
            yield {"step": "wrepaired",
                   "message": "Writing repairs rejected — they broke the one-page fit",
                   "data": {"count": 0}}
            return

        self._wrepaired, self._wrepaired_latex = len(accepted), candidate
        yield {"step": "wrepaired",
               "message": f"Repaired {len(accepted)} bullet(s) for writing quality",
               "data": {"count": len(accepted)}}

    # ── Cover letter ─────────────────────────────────────────────────

    async def cover_letter(self, *, dump, job_description, jd_analysis, resume_latex,
                           template_latex: str, today: str):
        """Write a cover letter for the same application, guaranteed to fit one page.

        Generated after the resume is final so it can reinforce the same framing instead
        of restating bullets. One page is enforced the same way it is for the resume —
        by compiling and measuring, not by trusting a word count — because a letter that
        spills three lines onto a second page is the one formatting error a reader
        cannot miss.
        """
        yield {"step": "letter_writing", "phase": "generate",
               "message": "Writing the cover letter...", "progress": 97}

        prompt = prompts.COVER_LETTER_PROMPT.format(
            template=template_latex,
            job_description=job_description,
            jd_analysis=json.dumps(jd_analysis, indent=2),
            resume=resume_latex,
            today=today,
        )
        msg = None
        try:
            async for ev in self._stream_call(
                phase="generate", system=prompts.COVER_LETTER_SYSTEM,
                messages=[{"role": "user", "content": dump.as_content_blocks(prompt)}],
            ):
                if ev["event"] == "message":
                    msg = ev["message"]
                else:
                    yield {"step": "live", **ev}
        except (ProviderError, AgentError) as e:
            yield {"step": "letter_failed", "message": f"Cover letter skipped ({e})."}
            return

        letter = _extract_latex(_text_of(msg))

        # An unfilled placeholder reaching a user would be the single most embarrassing
        # possible output, so it is checked rather than hoped for.
        leftover = re.findall(r"<<[A-Z_]+>>", letter)
        if leftover:
            yield {"step": "letter_failed",
                   "message": f"Cover letter left {len(leftover)} placeholder(s) unfilled "
                              f"({', '.join(sorted(set(leftover))[:3])}); not attaching it."}
            return

        compiled = await compile_pdf(letter)

        # Overlong letters are common and mechanically fixable: measure the spill and ask
        # for exactly that many words back, then verify rather than assume.
        for _ in range(2):
            if compiled.ok and compiled.pages == 1:
                break
            if not compiled.ok:
                yield {"step": "letter_failed",
                       "message": f"Cover letter did not compile ({compiled.error})."}
                return
            words = max(40, int(compiled.overflow_chars / 6) + 25)
            yield {"step": "letter_tightening", "phase": "tighten",
                   "message": f"Cover letter runs to {compiled.pages} pages — cutting about {words} words"}
            trimmed = None
            try:
                async for ev in self._stream_call(
                    phase="tighten", system=prompts.COVER_LETTER_SYSTEM,
                    messages=[{"role": "user", "content": prompts.COVER_LETTER_TIGHTEN.format(
                        words=words, latex=letter)}],
                ):
                    if ev["event"] == "message":
                        trimmed = _extract_latex(_text_of(ev["message"]))
                    else:
                        yield {"step": "live", **ev}
            except (ProviderError, AgentError):
                break
            if not trimmed:
                break
            verify = await compile_pdf(trimmed)
            # Only accept a shorter letter that still builds.
            if verify.ok and (verify.pages or 9) <= (compiled.pages or 9):
                letter, compiled = trimmed, verify

        if not compiled.ok or compiled.pages != 1:
            yield {"step": "letter_failed",
                   "message": "Cover letter would not fit one page; not attaching it."}
            return

        yield {"step": "letter_done",
               "message": "Cover letter written — one page",
               "progress": 99,
               "data": {"pages": compiled.pages},
               "letter": letter}

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
