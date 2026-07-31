# Resume Builder

**ATS-perfect resumes, recursively evaluated.** One input — a dump of everything about you — plus a job description in; a tailored, truthful, exactly-one-page LaTeX resume out.

This is not a template filler. It is an agentic loop built directly on the Anthropic SDK that writes a resume, judges it against a recruiter-grade rubric, and keeps refining until it scores **95+/100** or the time budget runs out. Page fit is *measured* by actually compiling the LaTeX, never guessed.

## How it works

Each run is a generate → evaluate → refine loop with deterministic polish phases:

```
 dump + JD + template + aggressiveness
        │
        ▼
 1. Analyze the JD ──── role framing + ATS keyword plan (structured output)
        │
        ▼
 2. Feasibility plan ── which requirements the dump can honestly support,
        │               and which are genuinely outside the background
        ▼
 3. Generate ────────── tailored LaTeX written into the chosen template,
        │               against a measured line budget for the page
        ▼
 4. Evaluate ────────── fast local checks (validator.py) feed ground-truth
        │               counts into three parallel LLM judges — recruiter,
        │               ATS, and fact-checker lenses
        ▼
 5. Refine ──────────── if the verdict says revise, the critique goes back
        │               to the writer; repeat until pass, score plateau,
        │               or the wall-clock budget is spent
        ▼
 6. Polish ──────────── deterministic repair passes: tighten overlong
        │               bullets, expand missing keywords, fix measured
        │               craft defects, kill widowed lines
        ▼
 7. One-page fit ────── compile the PDF, measure the real page count and
        │               fill, and add/cut lines by exact arithmetic until
        │               the page is ~95% full — exactly one page
        ▼
 8. Final audit ─────── ship the champion draft (best-scoring, always kept)
```

Key design decisions, each backed by measurement (see [`research/`](research/)):

- **The unit of fit is rendered lines.** The writer is given a line budget computed from the template skeleton, and the fit solver does line arithmetic against the compiled PDF rather than eyeballing whitespace.
- **Only measured defects get repaired.** Across live runs, critique items backed by a deterministic check reliably improved the draft; model-judged opinions were a coin flip. So the repair phases (`src/repair.py`) act only on mechanically detected defects — a missing metric, emphasis spam, a widowed line — and accept a rewrite only if it verifiably fixes the target.
- **Judge noise is accounted for.** Re-scoring the same resume k=5 times showed a ±3-point spread, so the loop treats score differences inside that band as noise instead of chasing them.
- **The champion draft always ships.** Every run keeps its best-scoring draft; a refinement that regresses, times out, or fails to compile never loses work.
- **Everything streams.** Every model call forwards summarized thinking and writing progress over SSE, so the UI is never a dead loading screen.
- **A wall-clock budget governs the run.** The loop reserves time for the mandatory closing phases (fit solver, widow repair, audit) and only starts a refine cycle it can finish — sized for serverless timeouts, overridable for long local runs.

## Truthfulness

The candidate dump is the **sole source of facts**. The writer may select, condense, reorder, reframe, and align terminology with the JD — it may never invent employers, roles, dates, degrees, certifications, tools, or numbers, extend a date range, upgrade a title, or move a real metric onto work it didn't come from. A dedicated fact-checker judge audits every draft against the dump, and the JD/dump are explicitly treated as *data, not instructions* (prompt-injection text inside either is ignored).

## Aggressiveness levels

| Level | Name | What it does |
|---|---|---|
| 1 | **Polish** | Your resume, professionally edited. Rephrasing, keyword weaving, metric surfacing — no reordering, dropping, or retitling. |
| 2 | **Tailor** | Every fact stays, but the page is re-weighted for this JD: most relevant work leads and gets the most bullets, skills are rebuilt around the JD's taxonomy, off-target bullets are reframed around their technical substance. |
| 3 | **Transform** | Works backwards from the ideal candidate: designs the perfect one-page resume for the role from the JD alone, then fills each slot with the strongest *true* evidence from the dump. Slots the dump can't support stay empty — the transformation lives in selection, framing, and language, never in the facts. |

## Templates

Four built-in LaTeX templates in [`templates/`](templates/), with compiled PDF previews served by the API:

| ID | Name | Best for |
|---|---|---|
| `udaya` | Udaya's Template *(default)* | Dense single-page layout with bold dates/locations, linked certifications, categorized skills rows |
| `jakes` | Jake's Resume | The classic Jake Gutierrez single-column layout |
| `mst` | Big Tech New Grad | Students/new grads — coursework grid, Activities & Honors |
| `sb2nov` | Software Engineer | Sourabh Bajaj's template — titled bullet items for experienced engineers |

You can also upload a **custom LaTeX template**; it is validated and sandboxed (dangerous LaTeX primitives are rejected) and rides in the user turn so it never carries system-prompt authority.

## Input formats

The dump can be pasted text or an uploaded file: **LaTeX, Markdown, plain text, Word (.docx), or PDF**. Text-like formats are decoded locally; PDFs are passed to Claude natively as document blocks — no lossy local extraction.

## Model providers

`src/providers.py` normalizes Anthropic and OpenAI behind one streaming interface (PDF inputs, JSON-schema outputs, reasoning-effort control), so the whole agent is provider-agnostic.

- **Anthropic** (default): Claude Opus 5, with per-phase reasoning effort tuning.
- **OpenAI**: GPT-5.x, with effort names mapped and clamped for older models.

Provider selection: `RESUME_PROVIDER` env var, or auto-detected from whichever API key is present (`sk-ant-…` → Anthropic, `sk-proj-…` → OpenAI). Override the model with `RESUME_MODEL`. Users can also bring their own API key per request from the web UI's settings.

## Repository layout

```
src/
  agent.py       The agent: run loop, three-judge evaluation, refine/polish
                 phases, wall-clock budgeting, and the chat editor
  prompts.py     Writer system prompt, aggressiveness levels, judge rubrics
  providers.py   Anthropic + OpenAI behind one streaming interface
  validator.py   Fast local checks (no API): keywords, banned verbs, craft
                 scoring, spacing-crush detection — ground truth for judges
  repair.py      Deterministic defect repair prompts/schemas
  latex.py       Compilation (local pdflatex or hosted fallback), page-fit
                 measurement, line arithmetic, bullet surgery
  templates.py   Template registry + custom template validation
  ingest.py      Dump ingestion for .tex/.md/.txt/.docx/.pdf
  fitprobe.tex   Probe document for calibrating line-height arithmetic
web/
  app.py         FastAPI app: SSE endpoints, rate limiting, input caps
  static/        Single-page frontend (vanilla JS, SSE client, chat editor)
api/index.py     Vercel serverless entry point
templates/       Built-in LaTeX templates
tests/           Offline checks + live end-to-end runs and benchmarks
research/        Design notes: one-page fit math, loop convergence findings
```

## API

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/` | The web UI |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/templates` | List built-in templates |
| `GET` | `/api/templates/{id}/preview.pdf` | Compiled template preview |
| `POST` | `/api/tailor/stream` | Run the full agent loop (SSE progress stream) |
| `POST` | `/api/edit/stream` | Conversational resume edits via the chat editor (SSE) |
| `POST` | `/api/export/pdf` | Compile final LaTeX to PDF |
| `POST` | `/api/export/tex` | Download the LaTeX source |

`POST /api/tailor/stream` takes multipart form data: `job_description` (required), `dump_text` or a `dump` file, `aggressiveness` (1–3), `template_id` or a `custom_template` file, and an optional `api_key`. The response is a Server-Sent Events stream of progress steps (`analyzing`, `planned`, `generating`, `evaluated`, `refining`, `fitted`, `audited`, …) ending with a `result` event containing the final LaTeX, scores, and stats.

The app enforces input caps (4 MB dump, 50 K-char JD) and per-IP rate limits, and scrubs submitted values (which can contain API keys) from validation errors.

## Getting started

### Prerequisites

- Python 3.11+
- An Anthropic API key (or OpenAI key)
- Optional but recommended: a local TeX installation (`pdflatex`). Without one, compilation falls back to a hosted LaTeX compiler — slower, and your resume transits a third-party service. Common TeX install paths are auto-detected even when not on `PATH`; you can also point at a binary with `PDFLATEX=/path/to/pdflatex`.

### Run locally

```bash
pip install -r requirements.txt

# .env: ANTHROPIC_API_KEY=sk-ant-...   (or OPENAI_API_KEY)
set -a && . ./.env && set +a

uvicorn web.app:app --reload
# open http://127.0.0.1:8000
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Anthropic provider key |
| `OPENAI_API_KEY` | — | OpenAI provider key |
| `RESUME_PROVIDER` | auto-detect | Force `anthropic` or `openai` |
| `RESUME_MODEL` | provider default | Override the model id |
| `RUN_BUDGET_SECONDS` | `250` | Wall-clock budget per run (raise for long local runs) |
| `TAIL_RESERVE_SECONDS` | `80` | Time reserved for the closing fit/repair/audit phases |
| `PDFLATEX` | auto-detect | Explicit path to a `pdflatex` binary |

### Deploy to Vercel

The repo deploys as-is: [`vercel.json`](vercel.json) rewrites everything to the serverless function [`api/index.py`](api/index.py), bundles `templates/`, `web/`, and `src/`, and sets a 300 s function timeout (the run budget's soft deadline exists precisely so a run ships its best draft before that timeout). Set the API key env vars in the Vercel project settings, or let users bring their own key in the UI.

## Testing

```bash
python3 tests/check_offline.py     # seconds, no API cost: templates compile,
                                   # prompts format, guards fire

python3 tests/run_live.py <name> <jd-file> <dump-file> [aggressiveness] [template]
                                   # full live run; writes .tex/.pdf/.json and a
                                   # summary line into tests/results/
```

`tests/results/BENCHMARK.md` accumulates scores across live runs; `tests/judge_noise.py` measures the evaluator's score variance (the source of the ±3-point noise band the loop uses).

## Research notes

The reasoning behind the architecture is written down in [`research/`](research/) so it isn't lost between sessions:

- [`one-page-fit.md`](research/one-page-fit.md) — how to make a LaTeX resume land on exactly one full page (line arithmetic, fill measurement, the fit probe).
- [`loop-convergence.md`](research/loop-convergence.md) — why the generate→evaluate→refine loop initially regressed, and the fixes: judge-noise banding, champion drafts, and repairing only measured defects.
