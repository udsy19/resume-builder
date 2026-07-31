# Research notes

Working notes behind the design decisions in this repo. Each exists because something
measurable went wrong and the fix needed justifying.

| Note | Question it answers |
|---|---|
| [`one-page-fit.md`](one-page-fit.md) | How do you make LaTeX land on exactly one *full* page, when `pages == 1` is a 1-bit signal and ink coverage saturates at ~99% either way? |
| [`loop-convergence.md`](loop-convergence.md) | Why did the refine loop keep repeating its mistakes, and sometimes ship a worse draft than it started with? |

The empirical record these draw on is [`../tests/results/BENCHMARK.md`](../tests/results/BENCHMARK.md),
which logs every live run — including the regressions, and the three failures that only
surfaced once the loop was run against a second provider.

## How to read these

They are lab notes, not documentation. They record what was tried, what the numbers said,
and what was concluded — including conclusions later overturned. Where a note and the code
disagree, **the code is current and the note is history.**

The clearest example: `loop-convergence.md` records the discovery that the
`writing_quality` judge had stopped discriminating, returning 15/20 in nine of fourteen
runs regardless of the draft. That finding is why the category is now computed from
enumerated defects instead of asked for as a number.
