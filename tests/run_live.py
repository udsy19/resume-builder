"""Live end-to-end harness: runs the real agent, reports everything, saves artifacts.

Usage: python3 live_test.py <run-name> <jd-file> <dump-file> [aggressiveness] [template]
"""
import asyncio
import json
import sys
import os
import time
from pathlib import Path

# Derived from this file, not hardcoded: the absolute paths that used to live
# here pointed at one developer's machine and a scratch directory that does not
# exist anywhere else, so a fresh clone could not run a live test at all.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = Path(os.environ.get("RESUME_RUN_OUT") or (ROOT / "tests" / "results"))
OUT.mkdir(parents=True, exist_ok=True)

from src.agent import ResumeAgent          # noqa: E402
from src.ingest import Dump, ingest_file    # noqa: E402
from src.templates import get_template      # noqa: E402
from src.latex import compile_pdf           # noqa: E402


async def main():
    name = sys.argv[1]
    jd = Path(sys.argv[2]).read_text()
    dump_path = Path(sys.argv[3])
    aggr = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    tpl = sys.argv[5] if len(sys.argv) > 5 else "udaya"

    if dump_path.suffix == ".pdf":
        dump = ingest_file(dump_path.name, dump_path.read_bytes())
    else:
        dump = Dump(text=dump_path.read_text(), filename=dump_path.name)

    template_latex = get_template(tpl).read()
    agent = ResumeAgent()
    t0 = time.monotonic()
    final = None
    phase_times = {}
    last_phase = None

    print(f"\n{'='*78}\nRUN: {name} | aggressiveness={aggr} | template={tpl}\n{'='*78}", flush=True)

    async for ev in agent.run(dump=dump, job_description=jd,
                              template_latex=template_latex, aggressiveness=aggr):
        step = ev.get("step")
        el = time.monotonic() - t0
        if step == "live":
            ph = ev.get("phase")
            if ph != last_phase:
                phase_times[ph] = el
                last_phase = ph
            continue
        if step == "result":
            final = ev["result"]
            print(f"[{el:6.1f}s] RESULT", flush=True)
            break
        msg = ev.get("message", "")
        if step == "analyzed":
            d = ev["data"]
            print(f"[{el:6.1f}s] {msg}", flush=True)
            print(f"          must-have ({len(d['must_have_keywords'])}): {d['must_have_keywords']}", flush=True)
            print(f"          nice ({len(d['nice_to_have_keywords'])}): {d['nice_to_have_keywords']}", flush=True)
            print(f"          traits ({len(d['soft_signals'])}): {d['soft_signals']}", flush=True)
        elif step == "evaluated":
            d = ev["data"]
            cats = " ".join(f"{k.split('_')[0]}={v['score']}" for k, v in d["scores"].items())
            print(f"[{el:6.1f}s] PASS {ev['iteration']}: {d['total']}/100 {d['verdict']} | {cats}", flush=True)
            for i in d["issues"]:
                print(f"            - [{i['category']}] {i['fix'][:150]}", flush=True)
        else:
            print(f"[{el:6.1f}s] {step}: {msg}", flush=True)

    total = time.monotonic() - t0
    if not final:
        print("NO RESULT", flush=True)
        return

    tex_path = OUT / f"{name}.tex"
    tex_path.write_text(final["latex"])
    res = await compile_pdf(final["latex"])
    if res.ok and res.pdf:
        (OUT / f"{name}.pdf").write_bytes(res.pdf)

    lc = final["local_checks"]
    kw = lc.get("keywords", {})
    print(f"\n{'-'*78}")
    print(f"FINAL   score={final['score']}/100  verdict={final['verdict']}  time={total:.0f}s")
    print(f"PAGES   measured={res.pages}  compile_ok={res.ok}  {res.error[:80]}")
    for tier in ("must_have", "nice_to_have", "traits"):
        t = kw.get(tier, {})
        if t:
            print(f"{tier:12s} {t['coverage']}%  missing={t['missing']}")
    print(f"BULLETS {lc['total_bullets']}  repeated_verbs={lc['repeated_verbs']}  "
          f"no_metric={lc['bullets_without_metric']}  long={lc.get('long_bullets')}")
    print(f"SCORES  " + json.dumps({k: v["score"] for k, v in final["scores"].items()}))
    print(f"PASSES  {[h['total'] for h in final['iterations']]}")
    print(f"OPEN ISSUES ({len(final['issues'])}):")
    for i in final["issues"]:
        print(f"  - [{i['category']}] {i['fix'][:160]}")
    print(f"ARTIFACTS {tex_path}  {OUT / (name + '.pdf')}")
    (OUT / f"{name}.json").write_text(json.dumps(final, indent=2))

asyncio.run(main())
