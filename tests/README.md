# Tests

Two suites: one free and fast, one that spends real API credits.

## Offline checks — run this first

```bash
python3 tests/check_offline.py
```

No API calls, no cost, a few seconds. It verifies:

- every built-in template compiles to exactly one page, and `pdflatex` is on your `PATH`
- every prompt template still renders with the arguments the agent passes it
- the guards fire — spacing crush, geometry tampering, dangerous custom templates
- bullet parsing, targeting, and replacement round-trip correctly
- the run budget gates every phase, replaying measured phase timings
- `writing_quality` falls as defects accumulate and ignores unverifiable quotes
- transport failures degrade instead of aborting a run
- a failed evaluation still returns the finished resume

It also guards the **wiring**. An AST pass checks that every name and every `self.` method
the agent calls actually exists. Both halves earned their place: a refactor once deleted
`_budget_plan`, and later `_judge`, and neither was caught until a live run died — the
method check exists because `self._judge(...)` is an attribute call the name check cannot
see.

## Live runs — these cost money

```bash
set -a && . ./.env && set +a     # ANTHROPIC_API_KEY or OPENAI_API_KEY
python3 tests/run_live.py <name> <jd-file> <dump-file> [aggressiveness] [template]
```

Example:

```bash
python3 tests/run_live.py my-run tests/fixtures/jd_clay.txt \
    tests/fixtures/dump_udaya.txt 3 udaya
```

Writes a progress log plus the final `.tex`, `.pdf`, and `.json` to `results/`. A full run
takes roughly 15 minutes; read the time-budget section in the root README before
shortening it.

To target a specific provider:

```bash
RESUME_PROVIDER=openai python3 tests/run_live.py ...
```

## Judge noise

```bash
python3 tests/judge_noise.py
```

Re-scores one identical resume k times and reports the spread. The measured ±3-point band
is where `JUDGE_NOISE_PTS` comes from — the loop treats differences inside it as noise
rather than chasing them.

## Layout

```
fixtures/              Job descriptions and a sample dump (placeholder contact details)
results/               Progress logs
results/BENCHMARK.md   Every measured run, including regressions and the bugs they exposed
```

Generated `.tex` and `.json` artifacts are gitignored — they are reproducible, and
`BENCHMARK.md` is the part worth keeping.
