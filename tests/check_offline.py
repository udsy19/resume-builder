#!/usr/bin/env python3
"""Fast offline checks — no API calls. Run before any live test."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures = []


def check(name, fn):
    try:
        fn()
        print(f"  ok    {name}")
    except Exception as e:
        print(f"  FAIL  {name}: {e}")
        failures.append(name)


def main():
    from src import prompts
    from src.templates import BUILTIN_TEMPLATES, get_template, validate_custom_template
    from src.latex import compile_local, find_pdflatex, measure_fit, skeleton_line_budget
    from src.validator import run_checks, spacing_crush_reason
    from src.agent import apply_patches, AgentError, _prioritize, _budget_plan as agent_budget

    print("prompts render:")
    check("generation", lambda: prompts.GENERATION_PROMPT.format(job_description="x", jd_analysis="y"))
    check("respec", lambda: prompts.RESPEC_PROMPT.format(
        budget_line="b", job_description="j", coverage="c", local_checks="l", n=1, issues="i", latex="x"))
    check("judge", lambda: prompts.JUDGE_PROMPT.format(
        focus="f", job_description="j", jd_analysis="a", local_checks="c", latex="l", categories="x"))
    check("edit", lambda: prompts.EDIT_PROMPT.format(latex="l", history="h", instruction="i", job_description="j"))
    check("coverage plan", lambda: prompts.COVERAGE_PLAN_PROMPT.format(keywords="k"))
    check("budget brief", lambda: prompts.BUDGET_BRIEF.format(
        lines=40, max_bullets=24, two_line_allowance=15, exp=11, proj=6, skills=4, edu=2))
    check("judge lenses cover all 5 categories", lambda: (
        sorted(c for l in prompts.JUDGE_LENSES.values() for c in l["categories"])
        == ["ats_compliance", "keyword_match", "page_fit", "truthfulness", "writing_quality"]
    ) or (_ for _ in ()).throw(AssertionError("lens coverage")))

    print("latex toolchain:")
    check("pdflatex found", lambda: find_pdflatex() or (_ for _ in ()).throw(AssertionError("not found")))
    for t in BUILTIN_TEMPLATES:
        def one(t=t):
            r = compile_local(t.read())
            # Surface the actual TeX error. "ok=False pages=None" sent a CI failure
            # through two blind guess-and-push cycles before anyone could see that
            # the real cause was a missing font package.
            detail = (r.error or "").strip().replace("\n", " ")[:300] if r else "no result"
            assert r and r.ok and r.pages == 1, \
                f"{t.id} -> ok={r and r.ok} pages={r and r.pages}: {detail}"
        check(f"{t.id} compiles to 1 page", one)
    check("headroom probe", lambda: (
        measure_fit(get_template("udaya").read()).headroom_pt is not None
    ) or (_ for _ in ()).throw(AssertionError("no headroom")))
    check("line budget", lambda: (skeleton_line_budget(get_template("udaya").read()) or 0) > 20
          or (_ for _ in ()).throw(AssertionError("budget too small")))

    print("wiring (catches helpers deleted by a refactor):")

    def wiring():
        import ast, builtins, inspect
        import src.agent as agent_mod
        tree = ast.parse(inspect.getsource(agent_mod))
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        defined |= set(dir(agent_mod))
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        missing = sorted(c for c in called if c not in defined and not hasattr(builtins, c))
        assert not missing, f"agent.py calls undefined name(s): {missing}"

        # Methods too: self._judge(...) is an attribute call, so the name check above
        # cannot see it — this is exactly how a deleted method reached production.
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            have = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            used = {n.func.attr for n in ast.walk(cls)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"}
            gone = sorted(used - have)
            assert not gone, f"{cls.name} calls missing method(s): {gone}"
    check("agent calls no undefined names", wiring)

    def budget_math():
        pl = agent_budget(43.1)
        assert pl["max_bullets"] + pl["two_line_allowance"] <= pl["lines"], pl
        prompts.BUDGET_BRIEF.format(**pl)
    check("budget plan renders + arithmetic holds", budget_math)

    print("guards:")
    tex = get_template("udaya").read()
    check("spacing crush caught", lambda: spacing_crush_reason(
        tex, tex.replace(r"\vspace{-2pt}", r"\vspace{-22pt}")) or (_ for _ in ()).throw(AssertionError("missed")))
    check("geometry tamper caught", lambda: spacing_crush_reason(
        tex, tex.replace(r"\addtolength{\textheight}{1.0in}", r"\addtolength{\textheight}{1.9in}"))
        or (_ for _ in ()).throw(AssertionError("missed")))
    check("content trim allowed", lambda: spacing_crush_reason(tex, tex.replace(r"\resumeItem{", "% ", 1)) is None
          or (_ for _ in ()).throw(AssertionError("false positive")))
    check("dangerous template rejected", lambda: validate_custom_template(
        r"\documentclass{a}\input{/etc/passwd}\begin{document}x\end{document}")
        or (_ for _ in ()).throw(AssertionError("missed")))

    def patches():
        doc = r"\documentclass{a}\begin{document}May 2026\end{document}"
        assert r"\textbf{May 2026}" in apply_patches(doc, [{"find": "May 2026", "replace": r"\textbf{May 2026}"}])
        try:
            apply_patches(doc, [{"find": "nope", "replace": "x"}]); raise AssertionError("should fail")
        except AgentError:
            pass
    check("patch apply + missing anchor", patches)
    def tighten_targeting():
        from src.latex import parse_bullets, line_arithmetic, bullets_to_tighten, replace_bullets
        doc = r"\documentclass{a}\begin{document}" + "".join(
            r"\resumeItem{" + ("x" * n) + "}" for n in (100, 200, 300)) + r"\end{document}"
        b = parse_bullets(doc)
        assert [x["chars"] for x in b] == [100, 200, 300], b
        assert line_arithmetic(doc) == (3, 2, 5)
        todo = bullets_to_tighten(doc, 2)
        assert todo and todo[0]["index"] == 1, todo
        out = replace_bullets(doc, {1: "short"})
        assert "short" in out and parse_bullets(out)[1]["chars"] == 5
    check("bullet parse / target / replace", tighten_targeting)

    check("worklist capped at 6", lambda: len(_prioritize([{"category": "writing_quality", "fix": str(i)} for i in range(30)])) == 6
          or (_ for _ in ()).throw(AssertionError("not capped")))

    def scoring():
        from src.latex import CompileResult
        c = run_checks(tex, must_have=["Python"], unsupported=["Terraform"],
                       compiled=CompileResult(ok=True, pages=2, page_texts=["a", "b" * 500]))
        s = c.summary()
        assert "HARD FAILURE" in s and "Terraform" in s and "must NOT be penalized" in s
    check("checks surface hard failure + absent list", scoring)

    def budget_gating():
        """Replay the measured v5-udaya-t3 timeline against the gates.

        Phase costs are the real ones from tests/results/v5-udaya-t3.log. The point is
        that the run stops inside its budget instead of spending 194s on pre-loop work,
        never checking, and then running the tail unbilled on top.
        """
        from src.agent import (_Clock, TAIL_RESERVE_SECONDS, POLISH_ROUND_SECONDS,
                               REFINE_CYCLE_SECONDS)

        class FakeClock(_Clock):
            """Same gates, simulated time — no sleeping in a unit test."""
            def __init__(self, budget):
                self.budget, self.now = budget, 0.0
            def elapsed(self): return self.now
            def spend(self, secs): self.now += secs

        # measured: analyze+plan 22, generate 116, then polish rounds of ~18 each
        c = FakeClock(250)
        c.spend(22 + 116)                                  # 138s: analyze, plan, generate
        polish_gate = TAIL_RESERVE_SECONDS + POLISH_ROUND_SECONDS
        ran_polish = 0
        for cost in (14, 22, 18):                          # tighten, repair, expand
            if c.expired(polish_gate):
                break
            c.spend(cost)
            ran_polish += 1
        c.spend(36)                                        # first evaluation (3 judges)
        # The refine gate must refuse a cycle it cannot finish.
        assert c.expired(TAIL_RESERVE_SECONDS + REFINE_CYCLE_SECONDS), \
            "refine gate would start a cycle it cannot pay for"
        c.spend(6 + 71)                                    # widow repair + audit
        assert c.elapsed() <= 250 * 1.15, \
            f"run overshoots budget by more than 15%: {c.elapsed():.0f}s of 250s"
        # And the polish phases must actually be curtailed, not all run regardless.
        assert ran_polish < 3, "no polish round was skipped despite a tight budget"

        # A budget with real room must NOT curtail anything.
        roomy = FakeClock(900)
        roomy.spend(138)
        assert not roomy.expired(polish_gate), "roomy budget wrongly skips polish"
        roomy.spend(14 + 22 + 18 + 36)
        assert not roomy.expired(TAIL_RESERVE_SECONDS + REFINE_CYCLE_SECONDS), \
            "roomy budget wrongly refuses to refine"
    check("run budget gates every phase, not just the loop", budget_gating)

    def craft_discriminates():
        """The property the old holistic lens lacked: different drafts, different scores."""
        from src.validator import score_craft, CRAFT_PENALTIES
        doc = r"""
        \resumeItem{Built a Python pipeline that cut nightly ETL runtime from 6h to 40m}
        \resumeItem{Taught weekly labs for 75 students}
        \resumeItem{Was responsible for maintaining the deployment scripts}
        \resumeItem{Wrote \textbf{Terraform} and \textbf{Ansible} and \textbf{Helm} charts}
        """
        q = lambda s: {"quote": s, "fix": "close on a result"}
        clean = score_craft({}, doc)["score"]
        one = score_craft({"dead_tail_bullets": [q("Taught weekly labs for 75 students")]}, doc)["score"]
        two = score_craft({
            "dead_tail_bullets": [q("Taught weekly labs for 75 students")],
            "weak_openings": [q("Was responsible for maintaining the deployment scripts")],
        }, doc)["score"]
        assert clean == 20, f"a defect-free draft should score 20, got {clean}"
        assert clean > one > two, f"score must fall as defects accumulate: {clean}, {one}, {two}"

        # Unverifiable quotes are discarded rather than charged.
        bogus = score_craft({"dead_tail_bullets": [q("text that appears nowhere in the document")]}, doc)
        assert bogus["score"] == 20 and bogus["rejected"] == 1, \
            f"hallucinated defect was charged: {bogus}"

        # Caps hold: one class alone cannot drive the score to zero.
        flood = score_craft({"overbolded_bullets": [q("Wrote \\textbf{Terraform}")] * 40}, doc)
        assert flood["score"] >= 20 - CRAFT_PENALTIES["overbolded_bullets"][1], "cap not applied"

        # And the judge is no longer *asked* for the number it used to park on.
        from src.prompts import judge_schema
        assert "writing_quality" not in judge_schema(["writing_quality", "page_fit"])["properties"]["scores"]["properties"], \
            "craft judge is still being asked to score writing_quality directly"
    check("writing_quality is computed and discriminates", craft_discriminates)

    def transport_errors_degrade():
        """A stalled read must not abort a healthy run.

        A live OpenAI run died on httpx.ReadTimeout during tightening: transport
        exceptions are not ProviderError, so they escaped every degradation path.
        """
        import httpx
        from src.providers import _as_provider_error, ProviderError

        for exc in (httpx.ReadTimeout("stalled"), httpx.ConnectError("no route"),
                    httpx.RemoteProtocolError("truncated")):
            out = _as_provider_error(exc)
            assert isinstance(out, ProviderError), f"{type(exc).__name__} not normalised"
            assert str(out), "normalised error carries no message"

        # The polish phases must catch ProviderError so a failed round is survivable.
        src = (ROOT / "src" / "agent.py").read_text()
        for phase in ("_tighten", "_expand"):
            body = src.split(f"async def {phase}(")[1].split("\n    async def ")[0]
            assert "except (ProviderError, AgentError)" in body, \
                f"{phase} does not degrade on a provider failure"
        # And _stream_call must normalise anything the provider lets through.
        call = src.split("async def _stream_call(")[1].split("\n    async def ")[0]
        assert "_as_provider_error" in call, "_stream_call does not normalise transport errors"
    check("transport failures degrade instead of aborting the run", transport_errors_degrade)

    def evaluation_failure_still_ships():
        """A failed first evaluation must still hand back the finished resume."""
        src = (ROOT / "src" / "agent.py").read_text()
        loop = src.split("Never throw away a good draft")[1].split("evaluation[\"local_checks\"]")[0]
        assert "unscored" in loop, "first-pass evaluation failure does not ship the draft"
        assert loop.count("raise") == 1, "should only re-raise when the draft does not compile"
        # Each judge lens retries once before being called lost.
        judge = src.split("async def _judge(")[1].split("\n    async def ")[0]
        assert "for attempt in (1, 2)" in judge, "judge lens has no retry"
        assert "Evaluation lens failed \u2014" in judge, "lens failure discards the reason"
        # A null score must not render as "null/100".
        app = (ROOT / "web" / "static" / "app.js").read_text()
        assert 'r.score == null ? "UNSCORED"' in app, "UI does not handle an unscored result"
    check("a failed evaluation still returns the resume", evaluation_failure_still_ships)

    def cover_letter_template():
        """The letter is judged on the page before it is read, so check the page."""
        from src.latex import compile_local
        tpl = (ROOT / "templates" / "cover-letter.tex").read_text()

        # Placeholders must not overlap: NAME being a substring of SIGNATURE NAME
        # corrupted the sign-off under any ordered replacement.
        import re as _re
        tokens = _re.findall(r"<<[A-Z_]+>>", tpl)
        assert tokens, "template has no placeholders"
        for a in tokens:
            for b in tokens:
                assert a == b or a not in b, f"placeholder {a} is a substring of {b}"

        body = ("Rebuilt a detection pipeline around change events rather than a nightly "
                "clock, cutting runtime from six hours to forty minutes.\n\n"
                "Most detection problems are plumbing problems wearing a modelling costume.\n\n"
                "I would like to bring that to your platform team.")
        vals = {"<<FULL_NAME>>": "TEST CANDIDATE",
                "<<CONTACT_LINE>>": r"Somewhere $\cdot$ you@example.com",
                "<<DATE>>": "July 31, 2026",
                "<<RECIPIENT_BLOCK>>": "Hiring Team",
                "<<SALUTATION>>": "Dear Hiring Team,",
                "<<BODY>>": body,
                "<<CLOSING>>": "Sincerely,",
                "<<SIGNATURE_NAME>>": "Test Candidate"}
        out = tpl
        for k, v in vals.items():
            out = out.replace(k, v)
        assert "<<" not in out, "a placeholder survived substitution"

        r = compile_local(out)
        assert r and r.ok, f"cover letter does not compile: {(r.error if r else 'no result')[:200]}"
        assert r.pages == 1, f"cover letter is {r.pages} pages"
        assert r.overfull_vboxes == 0, f"{r.overfull_vboxes} overfull box(es) — visible as bad spacing"

        # The agent must refuse a letter with an unfilled placeholder.
        agent_src = (ROOT / "src" / "agent.py").read_text()
        assert '<<[A-Z_]+>>' in agent_src, "no guard against unfilled placeholders reaching the user"
    check("cover letter compiles to one clean page", cover_letter_template)

    def no_machine_specific_paths():
        """A committed absolute path to one machine breaks every other checkout.

        tests/run_live.py carried a hardcoded home directory and a scratch path that
        existed on exactly one computer, so a fresh clone could not run a live test.
        """
        import re as _re
        bad = []
        for f in list(ROOT.glob("*.py")) + list(ROOT.glob("*/*.py")) + list(ROOT.glob("*/*.sh")):
            if ".venv" in str(f):
                continue
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if _re.search(r"[\"']/(Users|home)/[a-zA-Z]", line) or "/private/tmp/claude" in line:
                    bad.append(f"{f.relative_to(ROOT)}:{n}")
        assert not bad, f"machine-specific absolute path(s): {bad[:4]}"
    check("no machine-specific absolute paths", no_machine_specific_paths)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        sys.exit(1)
    print("all offline checks passed")


main()
