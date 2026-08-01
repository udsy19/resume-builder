#!/usr/bin/env python3
"""Compare two run configurations from their logs.

Written for the paired design used to test deterministic writing repair: each batch
runs both arms concurrently, so contention on the host hits both equally. Pairing also
removes between-batch variation, which matters at n=3 where it would otherwise swamp
the effect.

Reports the per-pair difference rather than two independent means, and says plainly
when the sample cannot support a conclusion — which at three pairs it usually cannot.

    python3 tests/ab_analyze.py /tmp/ab-on-*.log -- /tmp/ab-off-*.log
"""
import json
import re
import statistics as st
import sys
from pathlib import Path

CATS = ["keyword_match", "ats_compliance", "writing_quality", "truthfulness", "page_fit"]


def parse(path):
    txt = Path(path).read_text(errors="replace")
    out = {}
    m = re.search(r"^FINAL\s+score=(\d+)/100.*?time=(\d+)s", txt, re.M)
    if not m:
        return None
    out["total"], out["seconds"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"^SCORES\s+(\{.*)$", txt, re.M)
    if m:
        try:
            out.update(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass
    m = re.search(r"^must_have\s+([\d.]+)%", txt, re.M)
    if m:
        out["coverage"] = float(m.group(1))
    return out


def main():
    argv = sys.argv[1:]
    if "--" not in argv:
        print(__doc__)
        sys.exit(2)
    cut = argv.index("--")
    a_runs = [parse(p) for p in sorted(argv[:cut])]
    b_runs = [parse(p) for p in sorted(argv[cut + 1:])]
    a_runs = [r for r in a_runs if r]
    b_runs = [r for r in b_runs if r]

    if not a_runs or not b_runs:
        print("No completed runs to compare.")
        sys.exit(1)

    n = min(len(a_runs), len(b_runs))
    print(f"\n  repair ON: {len(a_runs)} run(s)   repair OFF: {len(b_runs)} run(s)   pairs: {n}\n")
    print(f"  {'metric':<18}{'ON':>8}{'OFF':>8}{'diff':>8}{'pairs':>10}")
    print("  " + "-" * 52)

    verdicts = []
    for key in ["total"] + CATS + ["coverage", "seconds"]:
        av = [r[key] for r in a_runs if key in r]
        bv = [r[key] for r in b_runs if key in r]
        if not av or not bv:
            continue
        diffs = [a[key] - b[key] for a, b in zip(a_runs[:n], b_runs[:n]) if key in a and key in b]
        pair_str = ", ".join(f"{d:+g}" for d in diffs)
        print(f"  {key:<18}{st.mean(av):>8.1f}{st.mean(bv):>8.1f}{st.mean(av) - st.mean(bv):>+8.1f}{pair_str:>10}")
        # A result is only worth calling when every pair agrees on direction.
        if diffs and len(diffs) >= 2:
            if all(d > 0 for d in diffs):
                verdicts.append((key, "ON higher in every pair"))
            elif all(d < 0 for d in diffs):
                verdicts.append((key, "OFF higher in every pair"))

    print("\n  Consistent across every pair:")
    if verdicts:
        for k, v in verdicts:
            print(f"    {k}: {v}")
    else:
        print("    nothing — no metric moved the same way in all pairs")

    print(f"\n  With {n} pairs this can show a direction, not a magnitude. Treat anything")
    print("  that does not agree across every pair as noise.\n")


main()
