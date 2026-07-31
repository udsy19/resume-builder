# Live benchmark log

Fixture: `tests/fixtures/jd_clay.txt` (Clay Security Engineer) + `dump_udaya.txt`,
template `udaya`, aggressiveness 3, model `claude-opus-5`.

## The one-page problem: four failed framings, then a fix

Every attempt to make the *model* respect the page budget failed. The overflow barely moved:

| # | approach | gen effort | time | pages | headroom | note |
|---|---|---|---|---|---|---|
| 1 | "must fit one page" prose | high | 180s | 2 | −184pt | |
| 2 | measured line budget ("43.1 lines") | medium | 83s | 2 | −208pt | 2.2× faster, same fit |
| 3 | countable ("≤26 bullets, ≤190 chars") | medium | 88s | 2 | −172pt | bullet cap respected |
| 4 | exact arithmetic (N+K ≤ budget) | medium | 91s | 2 | −196pt | **26/26 bullets, but 22 over-length vs 16 allowed** |

Attempt 4 is the decisive one: the model obeys a *count* precisely and cannot obey a
*length* at all. Prompting harder does not fix an execution-fidelity failure.

Generation effort `high` vs `medium`: identical fit quality, 2.2× the time, 2.2× the
tokens (17,087 vs 7,793 output). **Generation runs at `medium`.**

## What worked: headroom-driven deterministic tightening

Measure TeX's page arithmetic, convert the overflow to lines, hand exactly that many
over-length bullets back with an exact character target, recompile, repeat.

| stage | pages | headroom | action |
|---|---|---|---|
| generated | 2 | −196pt | — |
| round 1 | 2 | −184pt | tightened 18 bullets → 1 line each |
| round 2 | **1** | **+153pt** | tightened 15 more |

**23 bullets tightened in 111s, zero content deleted.** Artifacts: `tightened.tex` / `.pdf`.

## Open: the page is now under-filled

+153pt of headroom is ~12 spare lines. Under-fill is the same metric with the opposite
sign, so the symmetric fix is an expand phase — restore substance to the tightened bullets
until headroom is under one line's cost. Not yet built.

## Earlier full-loop runs (before tightening existed)

| run | passes | final | pages | notes |
|---|---|---|---|---|
| 1 | 61 → 70 | 70 | 1 | hit 250s budget; 33 open issues |
| 2 | 74 → **58** | 74 | 2 | score regressed and it shipped 2 pages — the bug that drove the loop rewrite |

## Run 5 — full loop with tighten + expand + audit (615s)

| phase | result |
|---|---|
| JD analysis | 28 atomic must-have keywords |
| feasibility | 19 claimable, 13 genuinely absent |
| budget | 43.1 lines measured from the template |
| generation | 2 pages, −184pt |
| **tighten** | 19 bullets → **1 page** |
| **expand** | 8 bullets → page filled (was 9.3 lines short) |
| passes | 77 → 82 → 82 |
| **audit** | 12 editorial corrections applied |
| **final** | **1 page · 100% must-have coverage · 82/100** |

Category scores: keyword_match **28/30**, ats 17/20, truthfulness 17/20,
page_fit 8/10, writing_quality **12/20**.

### Where the remaining 18 points are

`writing_quality` is flat at 11-12 across all three passes and is now the binding
constraint. The likely cause is a real tension the tightening phase introduces: bullets
compressed to ≤127 characters read as terse, and the craft judge marks down rhythm
variety and STAR-lite completeness. The expand phase only partially compensates because
it restores length to the *shortest* bullets, not necessarily the ones the judge faults.

Next experiment: have the expand phase target the bullets the craft judge named, rather
than the shortest ones — i.e. feed judge findings into expansion targeting.

## Provider parity

| provider / model | structured output | PDF input | first-shot page fit | time |
|---|---|---|---|---|
| anthropic / claude-opus-5 | ✅ identical JSON | ✅ reads the PDF | 2 pages (−184pt) | 100s |
| openai / gpt-5.6 | ✅ identical JSON | ✅ reads the PDF | **1 page (+11pt)** | 105s |
| openai / gpt-5.1 | ✅ identical JSON | — | — | 1s (analysis) |

Notable: **gpt-5.6 landed one page unaided**, which Claude never did across four attempts.
Worth a head-to-head on full-loop quality before choosing a default.

## Worklist starvation — why the score plateaued at 82

Issue counts from run 5, by category:

| category | issues raised | score | ever sent to the writer? |
|---|---|---|---|
| writing_quality | **35** | 12/20 | **never** |
| truthfulness | 32 | 17/20 | some |
| ats_compliance | 24 | 17/20 | never |
| keyword_match | 13 | 28/30 | all six slots, every pass |
| page_fit | 10 | 8/10 | when failing |

The worklist was ordered by a fixed category rank with writing_quality last, and capped at
six. Keyword issues filled every slot on every pass, so the category with the most problems
and the lowest score never received a single fix — writing went 11 → 11 → 12 while keywords
went 21 → 27 → 28.

Now ordered by **score deficit**, round-robin across categories, with page-fit and
uncompilable LaTeX still jumping the queue. On run 5's final state the worklist becomes
`[writing_quality, ats_compliance, truthfulness, keyword_match, writing_quality, keyword_match]`
instead of six keyword items.

## Bug found only by end-to-end testing

`_budget_plan` was deleted by the worklist refactor (the replaced span swallowed it), giving
`NameError: name '_budget_plan' is not defined` immediately after the planning phase. It
killed the browser run and all three matrix runs. The offline suite passed throughout,
because nothing called it.

Added `agent calls no undefined names` to `tests/check_offline.py` — an AST walk over
`src/agent.py` asserting every called name resolves.

## Judge noise, measured (k=5 on one fixed draft)

| metric | value |
|---|---|
| total score | mean 72.8, **sd 1.17**, range 71–74 |
| keyword_match | mean 21.6/30, sd 0.49 |
| ats_compliance | mean 17.0/20, sd 0.63 |
| writing_quality | mean 14.4/20, sd 0.49 |
| truthfulness | mean 13.6/20, sd 0.80 |
| page_fit | mean 6.2/10, sd 0.40 |
| issues raised | mean 23.8, range 22–26 |

**A score change must exceed ~3 points to be real.** Far tighter than the literature's
warning of 25-point inter-judge spreads — the three-lens split with deterministic checks
fed in appears to stabilize scoring. Practical consequences: run 5's 77 → 82 was a real
improvement; its 82 → 82 was real stagnation, not noise.

## Template portability — the fit machinery was udaya-only

Running the matrix across all four templates exposed that tightening only worked on the
template it was developed against:

| template | `\resumeItem` arity | before | after |
|---|---|---|---|
| udaya | 1 arg | worked | works |
| jakes | 1 arg | 2 pages, page_fit 0 | works |
| mst | 1 arg | untested | works |
| sb2nov | **2 args** | 2 pages throughout the run | works |

Three defects, each surfaced by a different template:

1. **The probe wrapped `\resumeItem` with a fixed one-argument macro.** On sb2nov, whose
   `\resumeItem` takes `{title}{description}`, the wrapper silently mangled every bullet.
   Fixed by not wrapping at all — `_tighten` only ever used `headroom_pt`, and page
   arithmetic needs no wrapper.
2. **`parse_bullets` counted commented-out blocks.** udaya ships many; the count read 56
   against 24 real bullets, so tightening targeted text that never renders.
3. **Two-argument bullets were tightened on the wrong argument** — the bold title rather
   than the prose.

Post-fix bullet counts: udaya 24, jakes 20, mst 10, sb2nov 16, with sane minimum lengths
(40/44/26/80 chars) and no artifacts.

**Lesson:** every fit measurement was developed and verified against one template, and
three of four were broken. Matrix testing across templates is not optional for this code.

## v2 — after the worklist fix (all four templates, levels 1/2/3)

| template | level | final | pages | must-have coverage | passes |
|---|---|---|---|---|---|
| udaya | 3 | 81 | ✅ 1 | 89.5% | 75→78→**81**→79 |
| jakes | 2 | **89** | ✅ 1 | **100%** | 78→88→**89**→83 |
| sb2nov | 3 | 81 | ✅ 1 | **100%** | 66→68→69→**81** |
| mst | 1 | **88** | ✅ 1 | **100%** | 71→84→**88**→85 |

`writing_quality` moved from 11-12 (starved) to **15-17** once the worklist stopped being
monopolized by keyword issues. Every template reached one page; three of four hit full
keyword coverage. The champion/challenger guard earned its place: the last pass scored
*below* the peak in three of four runs.

## v3 — widow repair + noise-aware early stopping + OpenAI

| run | provider | final | pages | coverage | widows removed | time |
|---|---|---|---|---|---|---|
| udaya L3 | claude-opus-5 | **87** | ✅ 1 | 100% | 1 | 698s |
| mst L1 | claude-opus-5 | 83 | ✅ 1 | 100% | 3 | 655s |
| **udaya L3** | **openai gpt-5.6** | **86** | ✅ 1 | **100%** | 1 | **549s** |

**OpenAI runs the full loop successfully** — comparable score, full keyword coverage, and
the fastest run of the set (2 passes vs 4), with `page_fit` 9/10 against Claude's 7-8.

Widow repair fires on every run (1-5 per resume). It detects a bullet whose final line
holds only a word or two from character counts alone — `chars % 127 < 45` — so it needs no
rendered geometry, and it addresses what the judges flagged as a page_fit defect on
literally every previous run.

### Remaining gap to 95

Scores now cluster 83-89. `writing_quality` (15-17/20) and `page_fit` (7-9/10) are the
binding constraints. Judge noise is ±3, so these are real differences, not measurement.

## v4 — craft repair + tuned writing prompts

| run | provider | final | pages | coverage | repaired | widows | passes |
|---|---|---|---|---|---|---|---|
| jakes L2 | claude-opus-5 | 86 | ✅ 1 | 100% | 2 | 2 | 66→70→83→**86** |
| **udaya L3** | **openai gpt-5.6** | **89** | ✅ 1 | **100%** | 4 | 4 | 84→**89**→80 |

Deterministic craft repair (activity-only bullets, bold overload) now runs before the
judges see the draft. Detection precision mattered: the first version fired on skills
rows and project title lines — 9 "defects" per resume, mostly nonsense. After excluding
non-achievement items it reports 1-3 real ones, and those are exactly the bullets the
judges had been flagging.

## VMock rules encoded

Checking generated output against VMock's published Impact module — filler adverbs,
pronouns, weak/article openers — returns **zero violations**; the writer prompt already
prevented them. They are now checked mechanically as regression guards, folded into the
same craft-defect stream.

Calibration from BU's guide: their own professionally-written samples score **in the 80s**
on VMock. Our internal 86-89 is therefore likely a strong real-world result, and the
internal rubric is stricter than the tool it is trying to satisfy.

## v5 — full stack (craft repair reordered before expand)

| run | provider | final | pages | coverage | repaired | widows |
|---|---|---|---|---|---|---|
| jakes L2 | claude-opus-5 | 84 | ✅ 1 | 100% | — | — |
| udaya L3 | openai gpt-5.6 | 85 | ✅ 1 | 100% | 3 | 3 |
| udaya L3 | claude-opus-5 | 82 | ✅ 1 | 100% | 3 | 1 |

The reorder worked mechanically — craft repairs now land (3 bullets each) where v4 rejected
them for overflowing a full page. Scores did not rise, and `writing_quality` stayed at
14-15. See `research/loop-convergence.md` § "The writing_quality judge is anchored": that
category has returned 15/20 in 9 of 13 runs regardless of draft, so it cannot currently
measure the improvements being made.

## Session summary

| metric | start | end |
|---|---|---|
| pages | 2 | **1 on every template** |
| must-have keyword coverage | 60% | **100%** |
| total score | 70 | **84-89** |
| keyword_match | 16/30 | **27-28/30** |
| truthfulness | 11-15/20 | **17-19/20** |
| page_fit | 0-2/10 | **7-9/10** |
| writing_quality | 11-12/20 | 14-15/20 (anchored — see above) |
| templates working | 1 of 4 | **4 of 4** |
| providers | 1 | **2, both full-loop verified** |

### v5-udaya-t3 — champion/challenger confirmed under regression

`PASSES [78, 82, 69, 66]`, `FINAL 82`. The loop genuinely got worse after pass 2 —
two consecutive refinements lost 13 and 16 points — and the champion logic shipped
pass 2's draft anyway. This is the exact failure that produced the `PASSES [74, 58]`
ship-the-worse-draft bug earlier in the session, now caught. Cost: 912s against a
250s `RUN_BUDGET_SECONDS`, because the budget is checked between phases and a single
refine+evaluate cycle can overrun it. Worth a mid-phase check before shipping.

## v6 — computed craft lens + whole-run budget gating

| run | provider | final | pages | coverage | passes | time |
|---|---|---|---|---|---|---|
| udaya L3 | claude-opus-5 | 77 | ✅ 1 | 82.4% | 2 | 476s |

**Not comparable to v4/v5.** Two things changed at once and both move the number down for
reasons unrelated to resume quality:

1. `writing_quality` is now computed from enumerated defects instead of asked for as a
   number. It returned **10/20 then 12/20** as verified defects fell 6 → 5. Across the
   previous 14 runs it never left 14-17 and returned exactly 15 nine times. The category
   is roughly 3 points lower because 15 was never a measurement.
2. The run budget now gates every phase, so the loop stopped after **2 passes** where v5
   ran 4. `keyword_match` fell 27-28 → 23 and coverage 100% → 82.4%: coverage is what the
   later refine passes were buying.

So v6 is a more honest score of a less-refined draft. The lens fix is validated; the
budget fix is working as designed but exposes that **coverage needs ~4 passes, and 4
passes need roughly 900s** — a 250s or 600s budget buys a worse resume, not a faster one.
The default budget is a product decision, not a tuning knob: see below.

### What the budget actually buys

| budget | passes | observed coverage |
|---|---|---|
| 250s | 1, no refinement | not measured — one pass only |
| 600s | 2 | 82.4% |
| ~900s | 4 | 100% |

Vercel's serverless ceiling is 300s, which fits *one* pass. Shipping the full loop needs
a background worker or a streaming job that survives past a single request.

## v6b / v6c — OpenAI parity, and three failures found by running it

Running the loop against OpenAI rather than reading the code found three defects that
the Anthropic path had never exposed. All three are fixed; each one had killed a run.

| run | outcome |
|---|---|
| v6b (first attempt) | died on `httpx.ReadTimeout` while tightening — transport errors are not `ProviderError`, so they escaped every degradation path |
| v6b (second attempt) | died when one judge lens failed on the first pass, discarding a finished one-page resume and reporting only `Evaluation lens failed: ats` with the cause thrown away |
| v6c | **completed**: 69/100, exactly one page, **100% must-have coverage** |

### The computed craft lens works on both providers

OpenAI accepts the `craft_observations` schema under strict mode, and the score moves:

| provider | observed writing_quality |
|---|---|
| claude-opus-5 | 10 → 12 |
| gpt-5.6 | 14 → 12 |

Four distinct values across two providers. The old lens returned 15 in nine of fourteen
runs and never left 14-17.

### OpenAI is NOT at parity on truthfulness

`SCORES {"keyword_match": 28, "ats_compliance": 16, "page_fit": 7, "writing_quality": 14,
"truthfulness": 4}`

**truthfulness 4/20.** The fact-checker found claims the dump does not support — at
aggressiveness 3, gpt-5.6 invented material rather than reframing what was there. The
comparable Claude run scored 17/20 on the same fixture and the same aggressiveness.
Keyword coverage and page fit are at parity; truthfulness is not, and truthfulness is the
one category where a failure makes the resume actively harmful to send.

**Do not ship OpenAI at aggressiveness 3 without addressing this.** Either constrain the
OpenAI writer prompt further, or cap OpenAI runs at aggressiveness 2 until a run scores
truthfulness in the high teens.

Secondary: OpenAI tightening cost ~73s per round against ~14s on Claude, so polish
consumed enough budget to leave the page constraint unmet at the deadline ("Out of time
with the page constraint unmet — trimming to one page directly"). The deterministic fit
solver caught it and the resume still shipped on one page.

## v7 — patch-based refinement

Refinement re-emitted the entire document every cycle to change a few bullets, which made
it ~58% of a full run's wall clock. It now returns find/replace patches, with a `rewrite`
escape hatch for genuinely structural changes, verified by compiling before the next
evaluation and rolled back if it breaks.

| | before (v6) | after (v7) |
|---|---|---|
| refine phase | ~131-150 s | **43 s, 23 s, then one 188 s rewrite** |
| refine cycles inside the budget | 2 | **4** |
| must-have coverage | 82.4% | **100%** |
| final score | 77 | **82** |
| total run | 476 s (2 cycles) | 851 s (4 cycles) |

`PASSES [73, 75, 82, 81]` — the champion held at 82 when the fourth pass came in lower.

The win is not only speed: cheaper cycles mean **more of them fit in the same budget**, and
cycles are what land the last keywords. Coverage returned to 100% for the first time since
budget gating landed. One cycle chose `rewrite` and cost the old amount, which is the
escape hatch working as intended rather than a regression.

`REFINE_CYCLE_SECONDS` drops 190 → 110 to match, so the loop now starts cycles it would
previously have refused for lack of time.

## VPS deployment — end-to-end verification

First run on the production host (Ubuntu 24.04, local TeX, no request ceiling), with the
full `RUN_BUDGET_SECONDS=900` the platform can finally afford.

```
FINAL   score=80/100  verdict=revise  time=681s
must_have    100.0%  missing=[]
SCORES  {"keyword_match": 28, "ats_compliance": 17, "truthfulness": 18,
         "page_fit": 7, "writing_quality": 10}
PASSES  [72, 67, 80, 77]
```

- **Exactly one page**, confirmed by counting the pages of the emitted PDF, not by
  trusting the score.
- **100% must-have keyword coverage** — three refine cycles inside the budget, which is
  what coverage costs. The same fixture on Vercel's 250s ceiling reached 82%.
- **truthfulness 18/20** on Anthropic, against 4/20 for the OpenAI run on this fixture.
- The champion held at 80 when the fourth pass came back at 77.
- `writing_quality 10` is the computed lens reporting honestly; it is not comparable to
  the pre-v6 numbers, which were a judge parked on 15.

The deployment also survived what the run did to it: the service stayed active throughout,
and the two unrelated applications sharing the host were unaffected.
