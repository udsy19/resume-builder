<div align="center">

# Resume Builder

### ATS-optimized, one-page LaTeX resumes — written, judged, and rewritten by an agent loop

One dump of everything about you, plus a job description.<br>
Out comes a tailored, truthful resume that fits on exactly one page — because the fit is **measured** by compiling the PDF, never guessed.

<br>

[![License: MIT](https://img.shields.io/badge/License-MIT-1e2edc?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-1e2edc?style=flat-square&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Anthropic](https://img.shields.io/badge/Claude-Opus%205-d97757?style=flat-square)](#-model-providers)
[![OpenAI](https://img.shields.io/badge/OpenAI-GPT--5.6-412991?style=flat-square&logo=openai&logoColor=white)](#-model-providers)
[![Checks](https://github.com/udsy19/resume-builder/actions/workflows/checks.yml/badge.svg)](https://github.com/udsy19/resume-builder/actions/workflows/checks.yml)

**[Quickstart](#-quickstart)** · **[How it works](#-how-it-works)** · **[Configuration](#-configuration)** · **[Deployment](#-deployment)** · **[API](#-api)** · **[Contributing](#-contributing)**

<br>

<table>
<tr>
<td align="center" width="25%"><b>1</b><br><sub>page, guaranteed</sub></td>
<td align="center" width="25%"><b>100%</b><br><sub>must-have keywords</sub></td>
<td align="center" width="25%"><b>77–89</b><br><sub>/100 measured</sub></td>
<td align="center" width="25%"><b>4</b><br><sub>LaTeX templates</sub></td>
</tr>
</table>

<sub>Every number here comes from <a href="tests/results/BENCHMARK.md">logged runs</a>, including the ones that regressed.</sub>

</div>

---

## What this is

Not a template filler. An agent loop that writes a resume, judges it against a recruiter-grade rubric, and keeps refining until the score stops improving or the time budget runs out.

| | |
|---|---|
| 📐 **One page is a guarantee** | The LaTeX is compiled and TeX's own page arithmetic is read back. Overflow is fixed by shortening bullets to exact character targets — never by shrinking margins or fonts. |
| 🔒 **Facts come only from your dossier** | A dedicated fact-checking judge audits every draft against what you actually wrote. The writer may reframe and re-language; it may not invent. |
| 📏 **Repairs act on measured defects** | Critique backed by a deterministic check reliably improved drafts; model-argued opinion was roughly a coin flip. So repair passes only fix what a checker found. |

### What it actually scores

> [!IMPORTANT]
> Finished resumes land in the **77–89 / 100** band, on exactly one page, with **100%** of the
> must-have keywords your background can honestly support — given a full time budget.
>
> The 95 in the code is the loop's *stopping condition*, not a claim about typical output.

<details>
<summary><b>Why this README quotes its own failures</b></summary>

<br>

[`tests/results/BENCHMARK.md`](tests/results/BENCHMARK.md) logs every measured run, including
the regressions and the bugs they exposed — a judge that returned the same score for fourteen
different drafts, a repair pass that cost five points of truthfulness, and an A/B that killed a
feature this project had already built.

Numbers here are reproducible from committed logs rather than asserted. Where something is
unproven, it says so.

</details>

---

## 🚀 Quickstart

**Prerequisites:** Python 3.11+, a TeX distribution with `pdflatex` on your `PATH`, and an API key from Anthropic or OpenAI.

The templates use font packages that a *base* TeX install does not ship — `CormorantGaramond`, `FiraSans`, `roboto`, `sourcesanspro`, `noto-sans`. A full distribution ([MacTeX](https://tug.org/mactex/), [TeX Live](https://tug.org/texlive/), MiKTeX) has them. On a minimal Debian/Ubuntu install:

```bash
sudo apt-get install texlive-latex-base texlive-latex-recommended \
                     texlive-latex-extra texlive-fonts-recommended \
                     texlive-fonts-extra cm-super
```

If a template fails to compile, this is almost always why — `tests/check_offline.py` will tell you which one.

```bash
git clone https://github.com/udsy19/resume-builder.git
cd resume-builder
pip install -r requirements.txt

echo "ANTHROPIC_API_KEY=sk-ant-..." > .env      # or OPENAI_API_KEY=sk-proj-...

python3 -m uvicorn web.app:app --reload
# open http://127.0.0.1:8000
```

Verify your toolchain before the first run — this compiles every template and checks the agent's wiring, with no API calls and no cost:

```bash
python3 tests/check_offline.py
```

> [!IMPORTANT]
> A complete run takes **~15 minutes** and the default budget reflects that. See [Time budget](#time-budget) before you shorten it — a smaller budget does not produce a faster resume, it produces a worse one.

---

## ⚙️ How it works

Each run is a generate → evaluate → refine loop wrapped in deterministic polish phases.

```mermaid
flowchart TD
    A["📄 Dossier + job description<br/>+ template + aggressiveness"] --> B

    B["<b>1 · Analyze</b><br/><sub>role framing and ATS keywords, extracted as<br/>atomic placeable terms, not narrative phrases</sub>"] --> C
    C["<b>2 · Feasibility plan</b><br/><sub>what your dossier can honestly support —<br/>scoring only counts these</sub>"] --> D
    D["<b>3 · Generate</b><br/><sub>tailored LaTeX, written to a line budget<br/>measured from the template skeleton</sub>"] --> E

    E["<b>4 · Deterministic polish</b><br/><sub>tighten overlong bullets to exact character<br/>targets, then fill the page</sub>"] --> F

    F["<b>5 · Evaluate</b><br/><sub>local checks feed ground truth into three<br/>parallel judges</sub>"] --> G{"Improving?"}

    G -->|yes| H["<b>6 · Refine</b><br/><sub>restated spec + short worklist,<br/>returned as patches</sub>"]
    H --> F
    G -->|"plateau, pass,<br/>or out of budget"| I

    I["<b>7 · One-page solver</b><br/><sub>compile, measure, cut lowest-value<br/>content by exact arithmetic</sub>"] --> J
    J["<b>8 · Audit</b><br/><sub>typography and consistency —<br/>skipped when checkers find nothing</sub>"] --> K

    K(["✅ Champion draft<br/><sub>best-scoring, always kept</sub>"])

    style A fill:#efedea,stroke:#0a0a0a,stroke-width:2px,color:#0a0a0a
    style K fill:#1e2edc,stroke:#1e2edc,color:#ffffff
    style G fill:#e0492a,stroke:#e0492a,color:#ffffff
    style E fill:#7fb2e8,stroke:#1e2edc,color:#0a0a0a
    style I fill:#7fb2e8,stroke:#1e2edc,color:#0a0a0a
```

> [!NOTE]
> Steps 4 and 7 involve **no model judgement at all**. Page fit is arithmetic over a
> compiled PDF, which is why one page is a guarantee rather than a hope.

### Design decisions, each backed by measurement

Full write-ups in [`research/`](research/).

| Decision | Why |
|---|---|
| **The unit of fit is rendered lines** | A probe injected at the `pdflatex` command line reports TeX's page arithmetic (`textheight + pageshrink − pagetotal`). It is linear and unsaturated, where PDF ink coverage pins at ~99% whether the page has four bullets of slack or none. |
| **Only measured defects get repaired** | Deterministic-backed critique reliably improved drafts; model-argued critique did not. Repairs name the exact bullet and target, and are accepted only if they verifiably hit it. |
| **Writing quality is computed, not scored** | Asked for a number, the judge returned 15/20 in nine of fourteen runs regardless of the draft — its own rubric bands funnelled every real resume into one range. It now *enumerates* defects with verbatim quotes, and the score is arithmetic over the ones that can be found in the document. |
| **Judge noise is measured** | Re-scoring an identical resume five times gave a ±3-point spread, so differences inside that band are treated as noise instead of chased. |
| **The champion always ships** | A refinement that regresses, times out, or fails to compile never costs you work. A one-page draft beats a higher-scoring two-page one, because two pages is not a resume. |
| **A wall-clock budget governs every phase** | Optional polish is skipped when time is short so evaluation still runs, and a refine cycle is only started if it can finish. Skips are announced — a truncated run never reads as a finished one. |
| **Everything streams** | Every model call forwards summarized reasoning over SSE, so the UI is never a dead loading screen. |

---

## ✉️ Cover letters

Tick the box on the setup screen and the run writes a matching cover letter after the
resume is final, so it reinforces the same framing instead of restating bullets.

It is a proper business letter, not a resume with paragraphs: ragged-right setting,
paragraph spacing instead of indentation, and widow and orphan penalties maxed out. One
page is enforced the way it is for the resume — by compiling and measuring, then asking
for a specific number of words back if it spills. The same source-of-truth rule applies,
and a letter that leaves a placeholder unfilled or will not fit is dropped rather than
attached.

---

## 🔒 Truthfulness

Your dump is the **sole source of facts.**

The writer may select, condense, reorder, reframe, and align terminology with the job description. It may **not** invent employers, roles, dates, degrees, certifications, tools, or numbers; extend a date range; upgrade a title; or move a real metric onto work it didn't come from.

A dedicated fact-checker judge audits every draft against the dump. Both the job description and the dump are treated as **data, not instructions** — prompt-injection text inside either is ignored.

---

## 🎚️ Aggressiveness levels

| Level | Name | What it does |
|:---:|---|---|
| 1 | **Polish** | Your resume, professionally edited. Rephrasing, keyword weaving, metric surfacing — no reordering, dropping, or retitling. |
| 2 | **Tailor** | Every fact stays, but the page is re-weighted for this role: the most relevant work leads and gets the most bullets, skills are rebuilt around the JD's taxonomy, off-target bullets are reframed around their technical substance. |
| 3 | **Transform** | Works backwards from the ideal candidate — designs the perfect one-page resume for the role from the JD alone, then fills each slot with the strongest *true* evidence from your dump. Slots your dump can't support stay empty. The transformation is in selection, framing, and language; never in the facts. |

---

## 🎨 Templates

Four built-in LaTeX templates in [`templates/`](templates/), each with a compiled PDF preview served by the API.

| ID | Name | Best for |
|---|---|---|
| `udaya` | Udaya's Template *(default)* | Dense single-page layout — bold dates and locations, linked certifications, categorized skills rows |
| `jakes` | Jake's Resume | The classic Jake Gutierrez single-column layout |
| `mst` | Big Tech New Grad | Students and new grads — coursework grid, Activities & Honors |
| `sb2nov` | Software Engineer | Sourabh Bajaj's template — titled bullet items, for experienced engineers |

You can also upload a **custom LaTeX template.** It is validated and sandboxed (dangerous LaTeX primitives are rejected) and rides in the user turn so it never carries system-prompt authority.

**Input formats:** the dump can be pasted text or an uploaded file — LaTeX, Markdown, plain text, Word (`.docx`), or PDF. Text formats are decoded locally; PDFs are passed to the model natively as document blocks, with no lossy local extraction.

---

## 🧠 Model providers

[`src/providers.py`](src/providers.py) normalizes Anthropic and OpenAI behind one streaming interface — PDF inputs, JSON-schema outputs, reasoning-effort control — so the agent itself is provider-agnostic.

| Provider | Default model | Status |
|---|---|---|
| **Anthropic** *(default)* | `claude-opus-5` | Fully validated end to end |
| **OpenAI** | `gpt-5.6` | Runs end to end; **see the caveat below** |

> [!WARNING]
> **OpenAI is not at parity on truthfulness.** In the most recent measured run, `gpt-5.6` at aggressiveness 3 scored **truthfulness 4/20** against **17/20** for the comparable Claude run on the same fixture — the fact-checker found claims the dump did not support. Keyword coverage and page fit reach parity; truthfulness does not, and it is the one category where a bad score makes a resume actively harmful to send. **Cap OpenAI at aggressiveness 2** until a run scores truthfulness in the high teens. Details in [`tests/results/BENCHMARK.md`](tests/results/BENCHMARK.md).

Provider selection uses `RESUME_PROVIDER`, or is auto-detected from whichever key is present (`sk-ant-…` → Anthropic, `sk-proj-…` → OpenAI). Override the model with `RESUME_MODEL`. Users can also bring their own key per request from the web UI's settings — it is kept in their browser's local storage and never persisted server-side.

---

## 🔧 Configuration

| Variable | Default | Purpose |
|---|:---:|---|
| `ANTHROPIC_API_KEY` | — | Anthropic key (or let users bring their own in the UI) |
| `OPENAI_API_KEY` | — | OpenAI key |
| `RESUME_PROVIDER` | auto | `anthropic` or `openai` |
| `RESUME_MODEL` | provider default | Override the model id |
| `RUN_BUDGET_SECONDS` | `900` | Wall-clock budget per run — see below |
| `TAIL_RESERVE_SECONDS` | `80` | Reserved for the closing phases so they are actually paid for |
| `PROVIDER_TIMEOUT_SECONDS` | `180` | Per-request timeout for the model provider |
| `PROVIDER_MAX_RETRIES` | `3` | SDK-level retries on connection failures |
| `LENS_TIMEOUT_SECONDS` | `150` | Hard ceiling on any one judge lens |
| `ACCESS_PIN` | *(unset)* | Gate the server's API key behind a PIN — see below |
| `PIN_PROVIDER` | `openai` | Provider used by PIN-unlocked runs, which spend the operator's credits. A visitor's own key always routes to that key's own provider. |
| `SESSION_SECRET` | derived | HMAC secret for session tokens; set explicitly in production |

### Time budget

`RUN_BUDGET_SECONDS` defaults to **900** because that is what a *complete* run costs. Lowering it makes the loop stop early, and the refine cycles are what land the last keywords:

| Budget | Refine cycles | Measured keyword coverage |
|---|:---:|---|
| ~900 s | 4 | **100%** |
| 600 s | 2 | 82% |
| 250 s | 0 | one evaluation, no refinement |

**Shorter does not mean faster. It means worse.** When the budget cannot fit a full loop, the run says so up front and the result page repeats it, rather than letting a truncated run read as a resume that merely scored badly.

---

### Protecting your API key on a public deployment

By default the server falls back to its own `ANTHROPIC_API_KEY` whenever a request
carries no key. **On a public URL that means anyone who finds it can spend your credits.**

Set `ACCESS_PIN` in your hosting environment to close that. Visitors then see a small PIN
field at the bottom of the sidebar; entering the PIN exchanges it for a signed, expiring
session token, and only requests carrying a valid token may use the server's key.

- **The API key is never sent to the browser.** The client proves it knows the PIN; the
  server spends its own key on that token's behalf.
- The PIN is compared in constant time, and attempts are rate limited to 8 per 15 minutes
  per IP — six digits is only a million guesses.
- Beyond that, each wrong PIN from an address costs more than the last: after four free
  attempts the lockout escalates 1 min → 5 min → 30 min → 2 hours. A flat rate limit only
  slows a guessing run down; this stalls it. A correct PIN is refused while locked, and a
  success clears the record, so a typo costs nothing. Failures are logged (never the PIN)
  so an attack on a live host is visible in `journalctl`.
- Users can always bring their own key in Settings instead, gate or no gate.
- With `ACCESS_PIN` unset the gate disappears entirely, which is what you want locally.
- Set `SESSION_SECRET` explicitly in production. Without it the signing secret is derived
  from the PIN, so changing the PIN invalidates every existing session.

> [!CAUTION]
> Put `ACCESS_PIN` in your hosting provider's environment variables — never in the repo,
> and never in a committed test. A PIN in a public repository is not a gate.


---

## 🌐 Deployment

### A VPS or container host (recommended)

This app needs two things a serverless platform cannot give it: a real `pdflatex`, and
requests that can run for ~15 minutes. On a host that provides both, the full loop runs
and you get the numbers at the top of this README.

```bash
git clone https://github.com/udsy19/resume-builder.git
sudo bash resume-builder/deploy/setup-vps.sh
```

The script installs the TeX packages the templates need, creates a service user, builds a
virtualenv, runs the offline checks as a gate, installs a hardened systemd unit, and
configures nginx with TLS. Two nginx settings in [`deploy/nginx-resume.conf`](deploy/nginx-resume.conf)
are load-bearing: buffering is off so Server-Sent Events stream in real time, and the read
timeout outlasts a full run so nginx does not kill a generation mid-flight.

Secrets go in `/etc/resume-builder.env` (root-owned, `0600`) — never in the repo. Point
your domain's A record at the host **before** running the script, or certbot cannot issue
a certificate.

### Vercel


The repo deploys to Vercel as-is: [`vercel.json`](vercel.json) rewrites everything to the serverless function [`api/index.py`](api/index.py), bundles `templates/`, `web/`, and `src/`, sets a 300 s function timeout, and pins `RUN_BUDGET_SECONDS` to 250 so a run ships its best draft before that timeout.

> [!WARNING]
> **Vercel cannot run this app properly, for two independent reasons.** There is no
> `pdflatex`, so the one-page guarantee — the core of the product — cannot work unless you
> set `ALLOW_ONLINE_COMPILER=1`, which sends every document to a third party. And 300 s
> fits roughly one evaluation pass and no refinement. To get the full loop, run it somewhere without a per-request ceiling — a background worker, a job queue, or any host that allows ~15-minute requests. The app is explicit in the UI when it is running a reduced loop.

Set your API key in the Vercel project's environment variables, or let users supply their own in the UI.

---

## 🔌 API

<details>
<summary><b>Endpoints and the streaming contract</b></summary>

<br>

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/` | The web UI |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/auth/status` | Whether this deployment gates its key, and whether you are in |
| `POST` | `/api/auth` | Exchange the access PIN for a session token |
| `GET` | `/api/templates` | List built-in templates |
| `GET` | `/api/templates/{id}/preview.pdf` | Compiled template preview |
| `POST` | `/api/tailor/stream` | Run the full agent loop (SSE) |
| `POST` | `/api/edit/stream` | Conversational resume edits (SSE) |
| `POST` | `/api/export/pdf` | Compile final LaTeX to PDF |
| `POST` | `/api/export/tex` | Download the LaTeX source |

`POST /api/tailor/stream` takes multipart form data: `job_description` (required), `dump_text` or a `dump` file, `aggressiveness` (1–3), `template_id` or a `custom_template` file, and an optional `api_key`. It responds with a Server-Sent Events stream of progress steps — `analyzing`, `planned`, `generating`, `evaluated`, `refining`, `fitted`, `audited` — ending with a `result` event carrying the final LaTeX, scores, and stats.

The app enforces input caps (4 MB dump, 50 K-character JD) and per-IP rate limits, and scrubs submitted values from validation errors, since those values can contain API keys.

---

</details>

## 📁 Repository layout

<details>
<summary><b>Where everything lives</b></summary>

<br>

```
src/
  agent.py       Run loop, three-judge evaluation, refine and polish phases,
                 wall-clock budgeting, and the chat editor
  prompts.py     Writer system prompt, aggressiveness levels, judge rubrics
  providers.py   Anthropic + OpenAI behind one streaming interface
  validator.py   Fast local checks (no API): keyword coverage, layout defects,
                 craft scoring, spacing-crush detection — ground truth for judges
  repair.py      Deterministic defect repair prompts and schemas
  latex.py       Compilation, page-fit measurement, line arithmetic, bullet surgery
  templates.py   Template registry and custom-template validation
  ingest.py      Dump ingestion for .tex / .md / .txt / .docx / .pdf
  fitprobe.tex   Non-invasive probe for TeX's page arithmetic
web/
  app.py         FastAPI app: SSE endpoints, rate limiting, input caps
  static/        Single-page frontend — vanilla JS, SSE client, chat editor
api/index.py     Vercel serverless entry point
templates/       Built-in LaTeX templates
tests/           Offline checks, live end-to-end runs, benchmarks
research/        Design notes: one-page fit math, loop convergence findings
```

---

</details>

## 🧪 Testing

```bash
python3 tests/check_offline.py          # no API calls, no cost — run this first
python3 tests/run_live.py <name> <jd-file> <dump-file> [aggressiveness] [template]
python3 tests/judge_noise.py            # measure evaluator score variance
```

`check_offline.py` compiles every template, renders every prompt, replays measured phase timings against the budget gates, and asserts the craft score discriminates. It also guards the wiring: an AST pass catches functions *and methods* deleted by a refactor, which is how a `NameError` once reached production.

Live results accumulate in [`tests/results/BENCHMARK.md`](tests/results/BENCHMARK.md) — including the regressions, and the three failures that only appeared when the loop was run against OpenAI.

---

## 📊 Research notes

- [`research/one-page-fit.md`](research/one-page-fit.md) — why headroom measurement beats ink coverage, and the line arithmetic behind the fit solver
- [`research/loop-convergence.md`](research/loop-convergence.md) — why the loop used to repeat mistakes, and why the writing-quality judge had to be rebuilt

---

## 🤝 Contributing

Issues and pull requests are welcome.

1. Run `python3 tests/check_offline.py` before opening a PR — it is fast and free.
2. If you change agent behaviour, include a live run in `tests/results/` showing the effect. Claims about quality in this repo are backed by measurements, and new ones should be too.
3. Match the surrounding style: comments explain *why*, especially where a decision looks odd but is load-bearing.

---

## License

[MIT](LICENSE) © Udaya Vijay Anand

## Credits

Built by **Udaya Vijay Anand** ([udsy.in](https://udsy.in) · [@udsy19](https://github.com/udsy19)).

Template credits: [Jake Gutierrez](https://github.com/jakegut/resume) and [Sourabh Bajaj](https://github.com/sb2nov/resume) for the layouts bundled here under their original terms.
