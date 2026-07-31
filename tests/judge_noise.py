#!/usr/bin/env python3
"""Score one fixed draft k times with the same judges to measure evaluator variance.

Research reports identical judges agreeing on only ~61% of repeat runs, so a score
difference smaller than our own measured spread is noise, not improvement. Every
threshold in the loop (PASS_THRESHOLD, "did it improve?") depends on knowing this.
"""
import asyncio
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agent import ResumeAgent, SCORE_CAPS          # noqa: E402
from src.ingest import Dump                            # noqa: E402
from src.latex import compile_pdf                      # noqa: E402
from src.validator import run_checks                   # noqa: E402


async def main():
    latex = Path(sys.argv[1]).read_text()
    jd = Path(sys.argv[2]).read_text()
    dump = Dump(text=Path(sys.argv[3]).read_text())
    k = int(sys.argv[4]) if len(sys.argv) > 4 else 5

    jd_analysis = {"must_have_keywords": ["Python", "Kubernetes", "IAM", "container security",
                                          "cloud security", "SIEM", "incident response"],
                   "nice_to_have_keywords": [], "soft_signals": []}
    compiled = await compile_pdf(latex)
    checks = run_checks(latex, must_have=jd_analysis["must_have_keywords"], compiled=compiled)
    agent = ResumeAgent()

    runs = []
    for i in range(k):
        ev = None
        async for e in agent._judge(latex=latex, dump=dump, job_description=jd,
                                    jd_analysis=jd_analysis, checks=checks, pdf=compiled.pdf):
            if e.get("step") == "verdict":
                ev = e["evaluation"]
        runs.append(ev)
        cats = " ".join(f"{c.split('_')[0]}={ev['scores'][c]['score']}" for c in SCORE_CAPS)
        print(f"  run {i+1}: total={ev['total']:3d}  {cats}  issues={len(ev['issues'])}", flush=True)

    totals = [r["total"] for r in runs]
    print(f"\nTOTAL      mean={statistics.mean(totals):.1f}  sd={statistics.pstdev(totals):.2f}  "
          f"range={min(totals)}-{max(totals)}  spread={max(totals)-min(totals)}")
    for cat, cap in SCORE_CAPS.items():
        vals = [r["scores"][cat]["score"] for r in runs]
        print(f"  {cat:16s} mean={statistics.mean(vals):5.1f}/{cap}  sd={statistics.pstdev(vals):.2f}  "
              f"range={min(vals)}-{max(vals)}")
    counts = [len(r["issues"]) for r in runs]
    print(f"  issues raised    mean={statistics.mean(counts):.1f}  range={min(counts)}-{max(counts)}")
    spread = max(totals) - min(totals)
    print(f"\nA score change must exceed ~{spread} points to be real rather than judge noise.")
    Path("tests/results/judge_noise.json").write_text(json.dumps(
        {"totals": totals, "spread": spread,
         "per_category": {c: [r["scores"][c]["score"] for r in runs] for c in SCORE_CAPS}}, indent=2))

asyncio.run(main())
