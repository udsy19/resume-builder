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
            assert r and r.ok and r.pages == 1, f"{t.id} -> ok={r and r.ok} pages={r and r.pages}"
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

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        sys.exit(1)
    print("all offline checks passed")


main()
