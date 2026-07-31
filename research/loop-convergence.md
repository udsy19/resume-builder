# Why the refine loop regressed, and what fixes it

Observed: pass 1 scored 74/100 → fed back ~16 fixes → pass 2 scored **58/100**. The loop
then stopped ("score didn't improve") and shipped the 74 draft, which was still 2 pages.

## 1. Critique overload is the dominant cause

*Prompt Design at Scale* (rules scaled 10→160, 5 models incl. Claude Sonnet 5) —
perfect-response rate by instruction count:

| instructions | perfect-response rate |
|---|---|
| 10 | 58.8–93.8% |
| 20 | 35.0–82.5% |
| **40** | **9.4–31.2%** |
| 80 | ~0% for every model, format and placement |

Our 20–35 item critiques sat exactly on the cliff. *When Instructions Multiply* finds
instruction **count** predicts all-instruction satisfaction within ~10% error — the count,
not the wording, is the dominant variable.

Nuance from **ComplexBench**: conjunctive ("and") composition is cheap; *chained* and
*conditional* instructions degrade sharply. So prefer independent, non-interacting fixes
over ones like "shorten X but keep the metric, which means moving Y".

**Applied:** `MAX_ISSUES_PER_REFINE = 6`, priority-ordered (page fit → truthfulness → LaTeX
→ keywords → ATS → writing).

## 2. Self-correction is not free

Huang et al. (GSM8K): the model keeps its initial answer 74.7% of the time, and **among the
cases where it changes, correct→incorrect is more likely than incorrect→correct.** A
mechanistic match to our 74→58.

> Corollary we now design around: **any critique item not backed by a deterministic check
> is a coin flip with negative expected value.**

**Applied:** deterministic checks (compiled page count, headroom, verb reuse, metric
presence, keyword coverage) drive the worklist; subjective judgements rank last.

## 3. Judge noise is larger than we assumed

*Rating Roulette* (EMNLP 2025 Findings): identical judges, identical generations, three
runs → Krippendorff's α = 0.265 / 0.507 / 0.563. The **best** judge gave the same verdict
across all three runs only **61.3%** of the time. A 2026 study found a **25-point
inter-judge spread** on identical responses, shrinking to 12 with rubric-level +
reasoning-augmented evaluation.

So part of any 16-point swing may not exist. Thresholds should be set from measured
variance, not intuition. **Open item:** re-score a saved draft k=5 times to measure ours.

## 4. Gate hard constraints; never weight them

*Certifiable Safe RLHF*: Lagrangian/soft-weight formulations "do not guarantee simultaneous
safety satisfaction and optimality"; a rectified exact penalty does. Formal argument that
page-fit-as-a-10-point-rubric-category **will** be traded away for keyword points, and
page-fit-as-a-gate won't.

**FALCON** is the cleanest published instance of the architecture: constrained decoding →
**deterministic feasibility repair** → adaptive best-of-N. 100% feasibility at both N=1 and
N=8, optimality gap 0.84–6.98%, 40% fewer samples. Enforcing feasibility outside the model
does not cost quality.

Also: constrained decoding cannot help here — it only enforces properties decidable from the
prefix with one token of lookahead. "Fits on one rendered page" is a whole-document property
plus a typesetting engine.

**Applied:** one page is a gate that forces continued iteration and can never be "passed";
`fit_to_one_page` is the deterministic repair layer.

## 5. Whole-document rewrite beats diffs at this size

Aider's leaderboard, same model both formats (133 tasks): gemini-exp-1206 **80.5% whole vs
69.2% diff**; o1-mini **70.7% vs 61.1%**. Diffs are recommended for *token efficiency*, not
accuracy — and Diff-XYZ shows diff *generation* is much harder than application
(Claude 4 Sonnet 85% generate vs 95% apply). For a one-page document, whole-document return
is the reliable choice; the patch path stays for interactive chat edits, where blast-radius
control matters more than generation accuracy.

Counter-evidence against localizing per se: span-guided detoxification **lost to unguided
global rewrite 15 vs 39** across 1,860 human judgements.

**Applied:** refinement returns the whole document, with an explicit
"reproduce everything else verbatim" instruction — measured to reduce drift across all models
tested (Claude Opus 4.6: Levenshtein 0.060, added-complexity 0.200).

## 6. Consolidate, don't accumulate

The multi-turn decomposition: **aptitude −16%, unreliability +112%** — and a
recap/consolidation turn recovers **15–20%**.

**Applied:** each refinement is a fresh consolidated respecification (spec + measured state +
short worklist + current draft) rather than an ever-growing chat history. Also cheaper.

## 7. Numeric measured deltas work; exhortation does not

GPT-4.1 strict length compliance: **<30% naive → >95%** with explicit counting scaffolds.
An external length function injecting measured usage cut MAE **52.04 → 22.81 (−56%)** and
raised precise match **15.20% → 58.24%**. Use **word/line** budgets, not characters or tokens
— word counting is markedly easier for models.

*Models Recall What They Violate* reports a recall–adherence gap: models can correctly state
a constraint they just violated. Constraint drift is an execution-fidelity failure, not a
comprehension failure — only an external check plus deterministic repair fixes it. Saying it
louder does nothing.

**Applied:** the writer receives a measured line budget before writing, and measured headroom
("you are 15.3 lines over") during refinement.

## Sources

Prompt Design at Scale (2607.19257) · When Instructions Multiply (2509.21051) ·
ComplexBench (2407.03978) · FALCON (2602.01090) · Certifiable Safe RLHF (2510.03520) ·
Rating Roulette (2510.27106) · Diff-XYZ (2510.12487) · Aider edit leaderboard ·
LIFEBench (2505.16234) · Exact Length-Controlled Generation (2508.13805) · LLMRefine (2311.09336)

## The writing_quality judge is anchored, not measuring

Across **13 full runs** this session — four templates, three aggressiveness levels, two
job descriptions, two providers, with and without deterministic craft repair, before and
after a full rewrite of the writing prompts — `writing_quality` returned:

| score | runs |
|---|---|
| 14 | 2 |
| **15** | **9** |
| 16 | 1 |
| 17 | 1 |

Nine of thirteen returned exactly 15/20. The drafts differed enormously; the score did
not. Measured judge noise on a *fixed* draft is sd 1.17, so this is not random scatter
around a true value — the lens is parking on a default regardless of content.

**Consequences.**

1. The apparent "gap to 95" is substantially an artifact. `writing_quality` contributing a
   near-constant 15/20 caps the achievable total around 85-90 no matter how good the
   resume is. Every other category responded to intervention (keyword_match 16 → 28,
   page_fit 0 → 9, truthfulness 11 → 19); this one never did.
2. Interventions aimed at writing quality cannot be evaluated by this number. The craft
   repairs demonstrably fire and demonstrably fix real defects — the defects are gone from
   the artifact — and the score does not move. Judge that work by the deterministic
   detectors, not by the lens.
3. Fixing the lens is a separate problem from fixing the resumes. Anchoring a rubric with
   worked examples at each score band, or switching to comparative judging (rank two
   drafts rather than score one), are the standard remedies; neither is implemented.

Corroborating evidence that the resumes themselves are fine: our output has **zero**
violations of VMock's mechanical Impact rules, and Boston University reports its own
professionally-written sample resumes score in the 80s on VMock.

**Do not tune further against this number until the lens is rebuilt.**
