/* Resume Builder — multi-role tailoring desk.
   Sessions persist in localStorage; each role runs its own live agent stream. */

"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ══ State & persistence ═══════════════════════════════════════════ */

const LS = { sessions: "rb.sessions", profile: "rb.profile", settings: "rb.settings", active: "rb.active" };

const state = {
  sessions: [],           // persisted role sessions
  profile: null,          // { kind: 'file'|'text', filename, text?, fileB64?, fileType? }
  settings: { apiKey: "" },
  activeId: null,
  templates: [],          // fetched once
  runs: new Map(),        // sessionId -> { controller }
  unloading: false,       // page is being torn down — don't mark dying fetches as errors
};

window.addEventListener("pagehide", () => { state.unloading = true; });

function loadState() {
  try { state.sessions = JSON.parse(localStorage.getItem(LS.sessions)) || []; } catch { state.sessions = []; }
  try { state.profile = JSON.parse(localStorage.getItem(LS.profile)); } catch { state.profile = null; }
  try { state.settings = JSON.parse(localStorage.getItem(LS.settings)) || { apiKey: "" }; } catch { state.settings = { apiKey: "" }; }
  state.activeId = localStorage.getItem(LS.active);
  // A run can't survive a page reload — mark any as interrupted.
  state.sessions.forEach((s) => { if (s.status === "running") { s.status = "interrupted"; } });
  if (!state.sessions.length) state.sessions.push(newSession());
  if (!state.sessions.some((s) => s.id === state.activeId)) state.activeId = state.sessions[0].id;
}

let saveTimer = null;
function save(immediate = false) {
  clearTimeout(saveTimer);
  const doSave = () => {
    try {
      localStorage.setItem(LS.sessions, JSON.stringify(state.sessions));
      localStorage.setItem(LS.active, state.activeId ?? "");
    } catch { /* quota — drop oldest results */
      trimForQuota();
    }
  };
  if (immediate) doSave(); else saveTimer = setTimeout(doSave, 600);
}
function saveProfile() {
  try { localStorage.setItem(LS.profile, JSON.stringify(state.profile)); }
  catch { alert("Your dossier is too large to persist in this browser — it will still work for this visit."); }
}
function saveSettings() { localStorage.setItem(LS.settings, JSON.stringify(state.settings)); }

function trimForQuota() {
  state.sessions.forEach((s) => {
    s.log = s.log.slice(-40); s.thinking = "";
    if (s.revisions && s.revisions.length > 4) s.revisions = [s.revisions[0], ...s.revisions.slice(-3)];
  });
  try { localStorage.setItem(LS.sessions, JSON.stringify(state.sessions)); } catch { /* give up quietly */ }
}

function newSession() {
  return {
    id: "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    name: "Untitled role",
    jd: "", templateId: "udaya", aggressiveness: 2, customTemplate: null,
    status: "draft",          // draft | running | done | error | interrupted
    progress: 0, log: [], thinking: "", phase: "", writingSection: "",
    jdAnalysis: null, coveragePlan: null, liveChecks: null, pages: null, passes: [],
    result: null, chat: [], revisions: [], viewRev: null, viewTab: "pdf",
    updatedAt: Date.now(),
  };
}
const activeSession = () => state.sessions.find((s) => s.id === state.activeId);

/* ══ Rail (side panel) ═════════════════════════════════════════════ */

const STATUS_DOT = { draft: "", running: "run", done: "good", error: "bad", interrupted: "warn" };
const STATUS_LABEL = { draft: "DRAFT", running: "WORKING", done: "READY", error: "ERROR", interrupted: "INTERRUPTED" };

function renderRail() {
  const list = $("session-list");
  list.innerHTML = "";
  state.sessions.forEach((s) => {
    const el = document.createElement("button");
    el.className = "session-item" + (s.id === state.activeId ? " active" : "");
    const score = s.result ? `${s.result.score}/100` : STATUS_LABEL[s.status];
    el.innerHTML = `
      <span class="dot ${STATUS_DOT[s.status]}"></span>
      <span class="session-name">${esc(s.name)}</span>
      <span class="session-score meta">${esc(score)}</span>
      <span class="session-del" title="Delete role">×</span>`;
    el.addEventListener("click", (e) => {
      if (e.target.classList.contains("session-del")) return deleteSession(s.id);
      state.activeId = s.id; save(true); renderAll();
    });
    list.appendChild(el);
  });

  const p = $("profile-status");
  if (state.profile) {
    p.innerHTML = `<span class="dot good"></span> ${esc(state.profile.filename || "Pasted text")}`;
  } else {
    p.innerHTML = `<span class="dot warn"></span> No information yet`;
  }
}

function deleteSession(id) {
  const s = state.sessions.find((x) => x.id === id);
  if (s.status === "running" && !confirm(`"${s.name}" is still working — cancel and delete it?`)) return;
  stopRun(id);
  state.sessions = state.sessions.filter((x) => x.id !== id);
  if (!state.sessions.length) state.sessions.push(newSession());
  if (state.activeId === id) state.activeId = state.sessions[0].id;
  save(true); renderAll();
}

$("new-session-btn").addEventListener("click", () => {
  const s = newSession();
  state.sessions.unshift(s);
  state.activeId = s.id;
  save(true); renderAll();
});

/* ══ Desk (main views) ═════════════════════════════════════════════ */

function renderAll() { renderRail(); renderDesk(); }

// The heat field is torn down and remounted with the plate it lives on, so its
// ResizeObserver and pointer listeners never outlive the canvas they were bound to.
let stopHeat = null;

function renderDesk() {
  const s = activeSession();
  const desk = $("desk");
  if (stopHeat) { stopHeat(); stopHeat = null; }
  if (!s) { desk.innerHTML = ""; return; }
  if (s.status === "draft") { renderDraft(desk, s); return mountHeat(); }
  if (s.status === "running") return renderRunning(desk, s);
  if (s.status === "done") return renderDone(desk, s);
  return renderStopped(desk, s); // error | interrupted
}

function mountHeat() {
  const canvas = $("heat");
  if (!canvas || !window.__mountHeatField) return;
  stopHeat = window.__mountHeatField(canvas);
}

/* ── Repo star count ──
   Public, unauthenticated call; the count is decoration, so any failure (rate limit,
   private repo, offline) just leaves the link without a number rather than erroring. */
async function loadStars() {
  try {
    const r = await fetch("https://api.github.com/repos/udsy19/resume-builder");
    if (!r.ok) return;
    const { stargazers_count } = await r.json();
    if (typeof stargazers_count !== "number") return;
    const el = $("repo-stars");
    el.textContent = stargazers_count >= 1000
      ? (stargazers_count / 1000).toFixed(1) + "k"
      : String(stargazers_count);
    el.hidden = false;
  } catch { /* no count, no problem */ }
}

/* ── Draft view ── */
function renderDraft(desk, s) {
  desk.innerHTML = `
  <div class="sheet">
    <div class="sheet-top">
      <div class="caption">Tailoring brief. One dossier in, one role-specific plate out.
        The agent writes, scores itself, and rewrites until it passes.</div>
      <div class="meta sheet-meta">RESUME BUILDER<br>ROLE SETUP</div>
    </div>

    <div class="field-band"><canvas id="heat"></canvas></div>

    <div class="display-line">Point it at <em>the job.</em></div>

    <div class="form-grid">
      <div>
        <label class="field-label">Role name <span class="opt">(optional — filled in from the job description)</span></label>
        <input type="text" id="f-name" value="${s.nameSetByUser ? esc(s.name) : ""}" placeholder="Left blank? We'll name it from the posting.">
      </div>
    </div>

    <label class="field-label">Job description</label>
    <textarea id="f-jd" rows="10" placeholder="Paste the full job description here">${esc(s.jd)}</textarea>

    <details class="advanced" ${s.customTemplate || s.templateId !== "udaya" || s.aggressiveness !== 2 ? "open" : ""}>
      <summary><span class="field-label">Template &amp; aggressiveness</span></summary>
      <div id="f-templates" class="template-grid"></div>
      <div class="custom-template-row">
        <input type="file" id="f-custom-template" accept=".tex" hidden>
        <button type="button" class="linklike" id="f-custom-btn">Upload a custom LaTeX template (.tex)</button>
        <span id="f-custom-name" class="mono small">${s.customTemplate ? esc("using " + s.customTemplate.name) : ""}</span>
      </div>
      <div class="agg-grid" id="f-agg">
        ${[["1", "Polish", "Your resume, professionally edited with the JD's keywords woven in"],
           ["2", "Tailor", "Every fact kept, but reordered and reweighted around this role"],
           ["3", "Transform", "Designs the ideal resume for this role first, then fills it with your real evidence — including material you'd never have highlighted"]].map(([n, t, d]) => `
          <button type="button" class="agg-option ${s.aggressiveness === +n ? "selected" : ""}" data-level="${n}">
            <strong>${n} · ${t}</strong><span>${d}</span>
          </button>`).join("")}
      </div>
    </details>

    <button class="btn primary big" id="f-run">Build this resume</button>
    <p id="f-error" class="error" hidden></p>

    <div class="stub-table">
      <div><span class="meta">DOSSIER</span><span class="mono">${state.profile ? esc(state.profile.filename || "pasted text") : "MISSING — add it in the rail"}</span></div>
      <div><span class="meta">ENGINE</span><span class="mono">GENERATE · EVALUATE · REFINE</span></div>
      <div><span class="meta">OUTPUT</span><span class="mono">ONE PAGE, LATEX + PDF</span></div>
    </div>
  </div>`;

  // wire
  $("f-name").addEventListener("input", (e) => {
    const v = e.target.value.trim();
    s.nameSetByUser = !!v;
    s.name = v || "Untitled role";
    save(); renderRail();
  });
  $("f-jd").addEventListener("input", (e) => { s.jd = e.target.value; save(); });
  renderTemplateGrid(s);
  $("f-custom-btn").addEventListener("click", () => $("f-custom-template").click());
  $("f-custom-template").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    s.customTemplate = { name: f.name, text: await f.text() };
    save(); renderDesk();
  });
  $("f-agg").addEventListener("click", (e) => {
    const opt = e.target.closest(".agg-option");
    if (!opt) return;
    s.aggressiveness = +opt.dataset.level; save();
    desk.querySelectorAll(".agg-option").forEach((o) => o.classList.remove("selected"));
    opt.classList.add("selected");
  });
  $("f-run").addEventListener("click", () => startRun(s));
}

function renderTemplateGrid(s) {
  const grid = $("f-templates");
  grid.innerHTML = "";
  state.templates.forEach((t) => {
    const selected = !s.customTemplate && s.templateId === t.id;
    const card = document.createElement("button");
    card.type = "button";
    card.className = "template-card" + (selected ? " selected" : "");
    card.innerHTML = `
      <strong>${esc(t.name)}</strong>
      <span>${esc(t.description)}</span>
      <span class="tpl-peek meta">Preview ▸</span>`;
    card.addEventListener("click", () => {
      s.templateId = t.id; s.customTemplate = null; save();
      $("f-custom-name").textContent = "";
      grid.querySelectorAll(".template-card").forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");
    });
    // Preview on hover/focus, and on clicking the peek label (touch).
    let timer = null;
    const open = () => { timer = setTimeout(() => showTemplatePreview(t, card), 220); };
    const close = () => { clearTimeout(timer); hideTemplatePreview(); };
    card.addEventListener("mouseenter", open);
    card.addEventListener("mouseleave", close);
    card.addEventListener("focus", () => showTemplatePreview(t, card));
    card.addEventListener("blur", close);
    card.querySelector(".tpl-peek").addEventListener("click", (e) => {
      e.stopPropagation(); showTemplatePreview(t, card, true);
    });
    grid.appendChild(card);
  });
}

/* ── Template preview popover (compiled PDF of the blank template) ── */

let previewEl = null;

function hideTemplatePreview() {
  if (previewEl) { previewEl.remove(); previewEl = null; }
}

function showTemplatePreview(t, anchor, pinned = false) {
  hideTemplatePreview();
  previewEl = document.createElement("div");
  previewEl.className = "tpl-preview" + (pinned ? " pinned" : "");
  previewEl.innerHTML = `
    <div class="tpl-preview-head">
      <span class="meta">${esc(t.name)} — TEMPLATE PREVIEW</span>
      ${pinned ? `<button class="linklike" data-close="1">close</button>` : ""}
    </div>
    <iframe class="tpl-frame" title="${esc(t.name)} preview"
      src="/api/templates/${encodeURIComponent(t.id)}/preview.pdf#toolbar=0&navpanes=0&view=FitH"></iframe>`;
  document.body.appendChild(previewEl);
  const r = anchor.getBoundingClientRect();
  const w = previewEl.offsetWidth || 420;
  previewEl.style.left = `${Math.max(12, Math.min(window.innerWidth - w - 12, r.left))}px`;
  const below = window.innerHeight - r.bottom;
  if (below > 380) previewEl.style.top = `${r.bottom + 8}px`;
  else previewEl.style.bottom = `${window.innerHeight - r.top + 8}px`;
  if (pinned) {
    previewEl.addEventListener("click", (e) => { if (e.target.dataset.close) hideTemplatePreview(); });
  } else {
    previewEl.addEventListener("mouseenter", () => previewEl.classList.add("pinned"));
    previewEl.addEventListener("mouseleave", hideTemplatePreview);
  }
}

function formError(msg) {
  const el = $("f-error");
  if (!el) return alert(msg);
  el.textContent = msg; el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 8000);
}

/* ── Running view ── */
const PHASES = [["analyze", "Analyze"], ["generate", "Write"], ["evaluate", "Evaluate"], ["refine", "Refine"]];

function renderRunning(desk, s) {
  const pages = s.pages;
  const lc = s.liveChecks || {};
  const cov = (lc.keywords || {}).must_have;
  const latest = (s.passes || [])[s.passes.length - 1];

  desk.innerHTML = `
  <div class="sheet cobalt dash">
    <div class="dash-head">
      <div>
        <div class="display-line tight">Building <em>${esc(s.name)}.</em></div>
        <div class="caption">Live trace. Every number below is measured, not estimated.</div>
      </div>
      <div class="meta sheet-meta">RESUME BUILDER<br>AGENT RUNNING</div>
    </div>

    <div class="stat-row" id="r-stats">${statRow(s, latest, cov, pages)}</div>

    <div class="phase-chips" id="r-phases">
      ${PHASES.map(([k, label]) => `<span class="phase-chip ${s.phase === k ? "on" : ""}" data-phase="${k}">${label}</span>`).join("<span class='phase-sep'>→</span>")}
    </div>
    <div class="progress-track"><div id="r-bar" class="progress-bar" style="width:${s.progress}%"></div></div>
    <div class="running-meta">
      <span class="meta" id="r-writing">${s.writingSection ? "WRITING: " + esc(s.writingSection.toUpperCase()) : ""}</span>
      <button class="linklike light" id="r-cancel">cancel run</button>
    </div>

    <div class="dash-grid">
      <section class="dash-panel span2">
        <div class="panel-head"><span class="meta">MODEL REASONING · LIVE</span></div>
        <pre id="r-think" class="think-stream">${s.thinking ? esc(s.thinking)
          : "Waiting for the model's reasoning…\n\nThe model only narrates when it actually deliberates, so this stays empty on\nstraightforward steps. Progress is still visible in the bar and the activity log."}</pre>
      </section>

      <section class="dash-panel">
        <div class="panel-head"><span class="meta">ATS KEYWORDS</span></div>
        <div class="panel-body kw-live" id="r-keywords">${keywordPanel(s)}</div>
      </section>

      <section class="dash-panel span2">
        <div class="panel-head"><span class="meta">ACTIVITY</span></div>
        <ul id="r-log" class="progress-log">
          ${s.log.map((l) => `<li class="${esc(l.kind || "")}">${esc(l.text)}</li>`).join("")}
        </ul>
      </section>

      <section class="dash-panel">
        <div class="panel-head"><span class="meta">EVALUATION PASSES</span></div>
        <div class="panel-body pass-panel" id="r-passes">${passPanel(s)}</div>
      </section>
    </div>
  </div>`;

  $("r-cancel").addEventListener("click", () => {
    stopRun(s.id);
    s.status = "interrupted"; pushLog(s, "Run cancelled.", "error");
    save(true); renderAll();
  });
  const log = $("r-log"); log.scrollTop = log.scrollHeight;
  const think = $("r-think"); think.scrollTop = think.scrollHeight;
}

/* The four numbers that actually tell you how the run is going. */
function statRow(s, latest, cov, pages) {
  const cells = [
    ["SCORE", latest ? `${latest.total}` : "—", latest ? "/100" : "", latest && latest.total >= 95 ? "good" : ""],
    ["PAGES", pages ? `${pages}` : "—", pages ? (pages > 1 ? "must be 1" : "one page") : "not compiled yet",
      pages && pages > 1 ? "bad" : (pages === 1 ? "good" : "")],
    ["KEYWORDS", cov ? `${cov.matched.length}` : "—", cov ? `/${cov.matched.length + cov.missing.length} placed` : "pending", ""],
    ["PASS", `${(s.passes || []).length}`, "of 4", ""],
  ];
  return cells.map(([label, big, sub, tone]) => `
    <div class="stat">
      <span class="meta">${label}</span>
      <span class="stat-big ${tone}">${esc(big)}<small>${esc(sub)}</small></span>
    </div>`).join("");
}

/* ── Revision strip: every version is kept, viewable and downloadable ── */

function revStrip(s) {
  const revs = s.revisions || [];
  if (revs.length <= 1) return `<span class="meta">REV 1 · ORIGINAL</span>`;
  const cur = revIndex(s);
  return `<span class="meta">REVISIONS</span>` + revs.map((r, i) => `
    <button class="rev-chip ${i === cur ? "on" : ""}" data-rev="${i}"
      title="${esc(r.label)}">R${i + 1}</button>`).join("")
    + (cur !== revs.length - 1 ? `<span class="meta rev-viewing">VIEWING R${cur + 1} — ${esc(revs[cur].label)} · EDITS APPLY TO R${revs.length}</span>`
                               : `<span class="meta rev-viewing">${esc(revs[cur].label)}</span>`);
}

/* ── Pass-by-pass timeline: what each iteration scored and what it's fixing ── */

const CAT_LABEL = {
  keyword_match: "Keywords", ats_compliance: "ATS", writing_quality: "Writing",
  truthfulness: "Truthful", page_fit: "Page fit", latex: "LaTeX",
};
const CAT_CAP = { keyword_match: 30, ats_compliance: 20, writing_quality: 20, truthfulness: 20, page_fit: 10 };

function passPanel(s) {
  if (!s.passes || !s.passes.length) {
    return `<div class="meta">EVALUATION PASSES</div>
      <div class="caption kw-waiting">The first score lands after the draft is written.</div>`;
  }
  const rows = s.passes.map((p, idx) => {
    const prev = idx > 0 ? s.passes[idx - 1].total : null;
    const delta = prev === null ? "" : (p.total - prev >= 0 ? `+${p.total - prev}` : `${p.total - prev}`);
    return `
      <div class="pass-row">
        <div class="pass-head">
          <span class="meta">PASS ${p.iteration}</span>
          <span class="pass-score">${p.total}<small>/100</small>${delta ? `<span class="pass-delta">${esc(delta)}</span>` : ""}</span>
          <span class="meta pass-verdict ${p.verdict === "pass" ? "good-text" : ""}">${esc((p.verdict || "").toUpperCase())}</span>
        </div>
        ${p.pages ? `<div class="meta pass-pages ${p.pages > 1 ? "warn-text" : ""}">COMPILED ${p.pages} PAGE${p.pages > 1 ? "S — MUST CUT" : ""}</div>` : ""}
        <div class="pass-cats">
          ${Object.keys(CAT_CAP).map((c) => p.scores?.[c]
            ? `<span class="pass-cat" title="${esc(p.scores[c].evidence || "")}">${CAT_LABEL[c]} <b>${p.scores[c].score}/${CAT_CAP[c]}</b></span>`
            : "").join("")}
        </div>
        ${(p.issues || []).length ? `
          <details class="pass-issues" ${idx === s.passes.length - 1 ? "open" : ""}>
            <summary class="meta">${p.issues.length} ISSUE(S) SENT BACK TO THE WRITER</summary>
            <ul class="issues">${p.issues.map((i) => `<li><em>${esc(CAT_LABEL[i.category] || i.category)}</em> ${esc(i.fix)}</li>`).join("")}</ul>
          </details>` : `<div class="meta good-text">NO ISSUES FOUND</div>`}
      </div>`;
  }).join("");
  return `<div class="meta">EVALUATION PASSES · SCORE BEFORE → AFTER</div>${rows}`;
}

/* ── The ATS keyword panel: every extracted keyword, and whether it landed ── */

const TIERS = [
  ["must_have", "must_have_keywords", "MUST-HAVE KEYWORDS", "Required by the JD — these drive the ATS score"],
  ["nice_to_have", "nice_to_have_keywords", "NICE-TO-HAVE", "Preferred/bonus items"],
];

function keywordPanel(s) {
  const jd = s.jdAnalysis || s.result?.jd_analysis;
  if (!jd) {
    return `<div class="meta">ATS KEYWORDS</div><div class="caption kw-waiting">Extracting from the job description…</div>`;
  }
  const checks = s.liveChecks || s.result?.local_checks || {};
  const cov = checks.keywords || {};
  const plan = s.coveragePlan || s.result?.coverage_plan;
  const absent = new Set(plan?.absent || checks.unsupported || []);
  const html = TIERS.map(([tierKey, jdKey, label, blurb]) => {
    let all = jd[jdKey] || [];
    if (tierKey !== "traits") all = all.filter((k) => !absent.has(k));
    if (!all.length) return "";
    const tier = cov[tierKey];
    const matched = new Set(tier ? tier.matched : []);
    const scored = !!tier;
    const placed = all.filter((k) => matched.has(k));
    const missing = all.filter((k) => !matched.has(k));
    return `
      <div class="kw-tier">
        <div class="kw-tier-head">
          <span class="meta">${label}</span>
          <span class="meta kw-count">${scored ? `${placed.length}/${all.length} IN RESUME` : `${all.length} FOUND`}</span>
        </div>
        <div class="caption kw-blurb">${blurb}</div>
        ${!scored ? `<div class="chips">${all.map((k) => `<span class="chip">${esc(k)}</span>`).join("")}</div>` : ""}
        ${scored && placed.length ? `
          <div class="kw-group"><span class="meta kw-group-label">✓ IN THE RESUME</span>
            <div class="chips">${placed.map((k) => `<span class="chip placed">${esc(k)}</span>`).join("")}</div></div>` : ""}
        ${scored && missing.length ? `
          <div class="kw-group"><span class="meta kw-group-label missing-label">✗ NOT YET INCLUDED</span>
            <div class="chips missing">${missing.map((k) => `<span class="chip">${esc(k)}</span>`).join("")}</div></div>` : ""}
      </div>`;
  }).join("");
  const gapBlock = absent.size ? `
    <div class="kw-tier kw-gap">
      <div class="kw-tier-head">
        <span class="meta">NOT IN YOUR BACKGROUND</span>
        <span class="meta kw-count">${absent.size} REQUIREMENT${absent.size > 1 ? "S" : ""}</span>
      </div>
      <div class="caption kw-blurb">The job asks for these and your dossier has no evidence for them.
        They are deliberately left out — never invented — and they don't count against your score.
        This is the honest gap between you and the posting.</div>
      <div class="chips gap">${[...absent].map((k) => `<span class="chip">${esc(k)}</span>`).join("")}</div>
    </div>` : "";
  const traitList = jd.soft_signals || [];
  const traitBlock = traitList.length ? `
    <div class="kw-tier">
      <div class="kw-tier-head">
        <span class="meta">FRAMING SIGNALS</span>
        <span class="meta kw-count">${traitList.length} CUES</span>
      </div>
      <div class="caption kw-blurb">Narrative qualities the posting wants. These shape how your work is
        described rather than appearing as literal keywords.</div>
      <div class="chips">${traitList.map((k) => `<span class="chip soft">${esc(k)}</span>`).join("")}</div>
    </div>` : "";
  return `<div class="kw-head"><span class="meta">ATS KEYWORDS EXTRACTED FROM THE JOB DESCRIPTION</span>
    <span class="meta kw-note">${cov.must_have ? "Checked against the current draft" : "Coverage is checked after the first pass"}</span></div>${html}${traitBlock}${gapBlock}`;
}

/* ── Done view ── */
const CAPS = { keyword_match: 30, ats_compliance: 20, writing_quality: 20, truthfulness: 20, page_fit: 10 };
const LABELS = {
  keyword_match: "Keyword match", ats_compliance: "ATS compliance",
  writing_quality: "Writing quality", truthfulness: "Truthfulness", page_fit: "Page fit",
};

function renderDone(desk, s) {
  const r = s.result;
  const lc = r.local_checks || {};
  desk.innerHTML = `
  <div class="sheet">
    <div class="sheet-top">
      <div class="caption">Final plate. Scored by an ATS pass, a recruiter read, and a
        fact-check against your dossier. Chat below to adjust anything.</div>
      <div class="meta sheet-meta">RESUME BUILDER<br>SCORE ${r.score}/100 · ${esc(r.verdict.toUpperCase())}</div>
    </div>

    <div class="display-line">${esc(r.jd_analysis.role_title || s.name)}<em>${r.jd_analysis.company ? " @ " + esc(r.jd_analysis.company) : ""}</em></div>

    <div class="result-grid">
      <div class="score-block">
        <div class="score-num ${r.score >= 88 ? "good" : r.score >= 70 ? "ok" : "bad"}">${r.score}<small>/100</small></div>
        <div class="score-bars">
          ${Object.entries(CAPS).map(([cat, cap]) => `
            <div class="score-bar-row" title="${esc(r.scores[cat].evidence)}">
              <span>${LABELS[cat]}</span>
              <div class="bar"><div style="width:${Math.min(100, (r.scores[cat].score / cap) * 100)}%"></div></div>
              <span class="val mono">${r.scores[cat].score}/${cap}</span>
            </div>`).join("")}
        </div>
      </div>
      <div class="kw-block kw-live">${keywordPanel(s)}</div>
    </div>

    ${(r.issues || []).length ? `
    <details class="issues-details">
      <summary class="meta">EVALUATOR NOTES (${r.issues.length})</summary>
      <ul class="issues">${r.issues.map((i) => `<li><em>${esc(i.category)}</em> ${esc(i.fix)}</li>`).join("")}</ul>
    </details>` : ""}

    <div class="work-grid">
      <div class="latex-pane">
        <div class="pane-head">
          <span class="tab-row">
            <button class="tab ${s.viewTab !== "tex" ? "on" : ""}" id="d-tab-pdf">PDF</button>
            <button class="tab ${s.viewTab === "tex" ? "on" : ""}" id="d-tab-tex">TEX</button>
            <span class="meta page-badge ${s.pages && s.pages > 1 ? "over" : ""}" id="d-pages">${
              s.pages ? `${s.pages} PAGE${s.pages > 1 ? "S ⚠" : ""}` : ""}</span>
          </span>
          <span class="btn-row">
            <button class="btn ghost sm" id="d-copy">Copy</button>
            <button class="btn ghost sm" id="d-tex">.tex</button>
            <button class="btn primary sm" id="d-pdf" title="Compiled via latex.ytotech.com — the resume content is sent there to build the PDF">Save PDF</button>
          </span>
        </div>
        <div class="rev-strip" id="d-revs">${revStrip(s)}</div>
        <div id="d-preview" class="preview-box">
          <pre id="d-latex" class="latex-block" ${s.viewTab === "tex" ? "" : "hidden"}>${esc(currentLatex(s))}</pre>
          <div id="d-pdf-view" class="pdf-view" ${s.viewTab === "tex" ? "hidden" : ""}></div>
        </div>
      </div>

      <div class="chat-pane">
        <div class="pane-head"><span class="meta">EDIT BY CHAT</span></div>
        <div id="d-chat" class="chat-log">
          ${s.chat.length ? s.chat.map((m) => chatBubble(m.role, m.content)).join("")
            : `<div class="chat-empty caption">Tell it what to change — "make the dates bolder",
               "remove the paintball project", "the second KPMG bullet isn't using its line well".</div>`}
        </div>
        <div id="d-chat-live" class="chat-live" hidden>
          <div class="meta">EDITING…</div><pre class="think-stream sm" id="d-chat-think"></pre>
        </div>
        <div class="chat-input-row">
          <textarea id="d-chat-input" rows="2" placeholder="Describe an edit..."></textarea>
          <button class="btn primary" id="d-chat-send">Send</button>
        </div>
      </div>
    </div>

    <div class="stub-table">
      <div><span class="meta">PASSES</span><span class="mono">${(r.iterations || []).map((h) => h.total).join(" → ") || "1"}</span></div>
      <div><span class="meta">AGGRESSIVENESS</span><span class="mono">LEVEL ${r.aggressiveness}</span></div>
      <div><span class="meta">REBUILD</span><span class="mono"><button class="linklike" id="d-rerun">RUN AGAIN</button></span></div>
    </div>
  </div>`;

  $("d-copy").addEventListener("click", async () => {
    await navigator.clipboard.writeText(currentLatex(s));
    $("d-copy").textContent = "Copied!"; setTimeout(() => { const b = $("d-copy"); if (b) b.textContent = "Copy"; }, 1400);
  });
    $("d-tex").addEventListener("click", () => download(s, "/api/export/tex", `resume-r${revIndex(s) + 1}.tex`, $("d-tex")));
  $("d-pdf").addEventListener("click", () => savePdf(s, $("d-pdf")));
  $("d-tab-pdf").addEventListener("click", () => { s.viewTab = "pdf"; save(); toggleTab(s); });
  $("d-tab-tex").addEventListener("click", () => { s.viewTab = "tex"; save(); toggleTab(s); });
  if (s.viewTab !== "tex") showPdfPreview(s);
  $("d-rerun").addEventListener("click", () => { s.status = "draft"; save(true); renderAll(); });
  const revBar = $("d-revs");
  if (revBar) revBar.addEventListener("click", (e) => {
    const chip = e.target.closest(".rev-chip");
    if (!chip) return;
    s.viewRev = +chip.dataset.rev;
    save(); renderDesk();
  });
  $("d-chat-send").addEventListener("click", () => sendEdit(s));
  $("d-chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendEdit(s); }
  });
  const chat = $("d-chat"); chat.scrollTop = chat.scrollHeight;
}

const chatBubble = (role, text) =>
  `<div class="chat-msg ${role === "user" ? "user" : "editor"}"><span class="meta">${role === "user" ? "YOU" : "EDITOR"}</span>${esc(text)}</div>`;

function revIndex(s) {
  const n = (s.revisions || []).length;
  if (!n) return -1;
  return s.viewRev == null || s.viewRev < 0 || s.viewRev >= n ? n - 1 : s.viewRev;
}
const latestLatex = (s) => (s.revisions?.length ? s.revisions[s.revisions.length - 1].latex : s.result.latex);
function currentLatex(s) {
  const i = revIndex(s);
  return i >= 0 ? s.revisions[i].latex : s.result.latex;
}
const viewingLatest = (s) => revIndex(s) === (s.revisions?.length || 1) - 1;

/* ── Stopped view (error / interrupted) ── */
function renderStopped(desk, s) {
  const isErr = s.status === "error";
  desk.innerHTML = `
  <div class="sheet ${isErr ? "" : "ink"}">
    <div class="sheet-top">
      <div class="caption">${isErr ? "The run failed. The trace below shows where." :
        "This run was interrupted — a reload or cancel stopped the stream. Your inputs are intact."}</div>
      <div class="meta sheet-meta">RESUME BUILDER<br>${isErr ? "RUN FAILED" : "RUN INTERRUPTED"}</div>
    </div>
    <div class="display-line">${isErr ? "Something <em>broke.</em>" : "Paused, <em>not lost.</em>"}</div>
    <ul class="progress-log">${s.log.slice(-14).map((l) => `<li class="${esc(l.kind || "")}">${esc(l.text)}</li>`).join("")}</ul>
    <div class="btn-row">
      <button class="btn primary" id="x-retry">Run again</button>
      <button class="btn ghost" id="x-edit">Edit brief</button>
      ${s.result ? `<button class="btn ghost" id="x-view">View last result</button>` : ""}
    </div>
  </div>`;
  $("x-retry").addEventListener("click", () => startRun(s));
  $("x-edit").addEventListener("click", () => { s.status = "draft"; save(true); renderAll(); });
  const v = $("x-view");
  if (v) v.addEventListener("click", () => { s.status = "done"; save(true); renderAll(); });
}

/* ══ Run engine ════════════════════════════════════════════════════ */

function pushLog(s, text, kind = "") {
  s.log.push({ text, kind });

}

async function startRun(s) {
  if (!state.profile) return formError("Add your dossier first (left rail → Edit dossier).");
  const jd = (s.jd || "").trim();
  if (!jd) return formError("Paste the job description first.");
  if (s.status === "running") return;

  const form = new FormData();
  form.append("job_description", jd);
  form.append("aggressiveness", String(s.aggressiveness));
  form.append("template_id", s.templateId);
  if (state.settings.apiKey) form.append("api_key", state.settings.apiKey);
  if (state.profile.kind === "file") {
    form.append("dump", b64ToFile(state.profile.fileB64, state.profile.filename, state.profile.fileType));
  } else {
    form.append("dump_text", state.profile.text);
  }
  if (s.customTemplate) {
    form.append("custom_template", new File([s.customTemplate.text], s.customTemplate.name, { type: "text/x-tex" }));
  }

  s.status = "running"; s.progress = 1; s.log = []; s.thinking = ""; s.phase = "analyze"; s.writingSection = "";
  s.updatedAt = Date.now();
  pushLog(s, "Starting agent run...");
  save(true); renderAll();

  const controller = new AbortController();
  state.runs.set(s.id, { controller });

  try {
    const res = await fetch("/api/tailor/stream", { method: "POST", body: form, signal: controller.signal });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed (${res.status})`);
    }
    await readSSE(res.body, (u) => handleRunUpdate(s, u));
    if (s.status === "running") { // stream ended without a result
      s.status = "error"; pushLog(s, "The stream ended unexpectedly — try again.", "error");
    }
  } catch (err) {
    if (err.name !== "AbortError" && !state.unloading) {
      s.status = "error"; pushLog(s, `Error: ${err.message}`, "error");
    }
  } finally {
    state.runs.delete(s.id);
    save(true);
    renderRail();
    if (s.id === state.activeId) renderDesk();
  }
}

function stopRun(id) {
  const run = state.runs.get(id);
  if (run) { run.controller.abort(); state.runs.delete(id); }
}

function handleRunUpdate(s, u) {
  const isActive = s.id === state.activeId && s.status === "running";

  if (u.step === "live") {
    if (u.event === "thinking") {
      s.thinking += u.text;
      s.phase = u.phase;
      if (isActive) {
        const el = $("r-think");
        if (el) {
          const stick = atBottom(el);
          el.textContent = s.thinking;
          el.classList.remove("placeholder");
          if (stick) el.scrollTop = el.scrollHeight;
        }
        setPhaseChip(u.phase);
      }
    } else if (u.event === "writing") {
      s.phase = u.phase;
      if (u.section) s.writingSection = u.section;
      if (isActive) {
        const w = $("r-writing");
        if (w) w.textContent = s.writingSection ? `WRITING: ${s.writingSection.toUpperCase()} · ${u.chars} CHARS` : `${u.chars} CHARS`;
        setPhaseChip(u.phase);
      }
    }
    save();
    return;
  }

  if (u.progress != null) {
    s.progress = u.progress;
    if (isActive) { const b = $("r-bar"); if (b) b.style.width = `${u.progress}%`; }
  }
  if (u.phase) { s.phase = u.phase; if (isActive) setPhaseChip(u.phase); }

  switch (u.step) {
    case "error":
      s.status = "error"; pushLog(s, u.message, "error");
      save(true); renderRail(); if (s.id === state.activeId) renderDesk();
      return;
    case "analyzed":
      s.jdAnalysis = u.data;
      if (!s.nameSetByUser) {
        s.name = roleNameFrom(u.data) || s.name;
        renderRail();
        const title = document.querySelector(".display-line");
        if (isActive && title) title.innerHTML = `Building <em>${esc(s.name)}.</em>`;
      }
      pushLog(s, u.message);
      save();
      if (isActive) { const p = $("r-keywords"); if (p) p.innerHTML = keywordPanel(s); }
      return;
    case "planned":
      s.coveragePlan = u.data;
      pushLog(s, u.message);
      save();
      if (isActive) { const p = $("r-keywords"); if (p) p.innerHTML = keywordPanel(s); }
      return;
    case "compiled":
      if (u.data?.pages) s.pages = u.data.pages;
      if (isActive) refreshStats(s);
      s.lastCompiledPages = u.data?.pages || null;
      pushLog(s, u.message, u.data?.compile_ok === false || (u.data?.pages > 1) ? "error" : "");
      break;
    case "lens_done":
      pushLog(s, u.message);
      break;
    case "degraded":
      pushLog(s, u.message, "error");
      break;
    case "evaluated": {
      const d = u.data;
      if (d.local_checks) s.liveChecks = d.local_checks;
      s.passes = s.passes || [];
      s.passes.push({
        iteration: u.iteration, total: d.total, verdict: d.verdict,
        scores: d.scores, issues: d.issues || [],
        pages: d.local_checks?.pages ?? s.lastCompiledPages ?? null,
      });
      pushLog(s, `Pass ${u.iteration}: ${d.total}/100 — ${d.verdict === "pass" ? "passed" : `${d.issues.length} issue(s) to fix`}`,
        d.verdict === "pass" ? "good" : "");
      if (isActive) {
        const kp = $("r-keywords"); if (kp) kp.innerHTML = keywordPanel(s);
        const pp = $("r-passes"); if (pp) pp.innerHTML = passPanel(s);
        refreshStats(s);
      }
      break;
    }
    case "result":
      s.result = u.result; s.status = "done"; s.chat = [];
      s.revisions = [{ latex: u.result.latex, label: "Generated", at: Date.now() }];
      s.viewRev = 0;
      if (!s.nameSetByUser) s.name = roleNameFrom(u.result.jd_analysis) || s.name;
      pushLog(s, u.message, "good");
      save(true); renderRail(); if (s.id === state.activeId) renderDesk();
      return;
    default:
      if (u.message) pushLog(s, u.message);
  }
  save();
  if (isActive) {
    const log = $("r-log");
    if (log) {
      const stick = atBottom(log);
      log.innerHTML = s.log.map((l) => `<li class="${esc(l.kind || "")}">${esc(l.text)}</li>`).join("");
      if (stick) log.scrollTop = log.scrollHeight;
    }
  }
}

function refreshStats(s) {
  const el = $("r-stats");
  if (!el) return;
  const lc = s.liveChecks || {};
  el.innerHTML = statRow(s, (s.passes || [])[s.passes.length - 1], (lc.keywords || {}).must_have, s.pages);
}

function setPhaseChip(phase) {
  document.querySelectorAll(".phase-chip").forEach((c) => c.classList.toggle("on", c.dataset.phase === phase));
}

async function readSSE(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop();
    for (const part of parts) {
      const line = part.trim();
      if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
    }
  }
}

const tail = (str, n) => (str && str.length > n ? str.slice(str.length - n) : str || "");
const atBottom = (el) => !el || el.scrollHeight - el.scrollTop - el.clientHeight < 40;

function roleNameFrom(jd) {
  if (!jd) return "";
  const role = (jd.role_title || "").trim();
  const company = (jd.company || "").trim();
  if (role && company) return `${role} — ${company}`;
  return role || company || "";
}

/* ══ Chat editing ══════════════════════════════════════════════════ */

async function sendEdit(s) {
  const input = $("d-chat-input");
  const instruction = input.value.trim();
  if (!instruction || s.editing) return;
  s.editing = true;
  s.chat.push({ role: "user", content: instruction });
  input.value = "";
  $("d-chat").insertAdjacentHTML("beforeend", chatBubble("user", instruction));
  const live = $("d-chat-live"); live.hidden = false;
  const think = $("d-chat-think"); think.textContent = "";
  $("d-chat-send").disabled = true;
  $("d-chat").scrollTop = $("d-chat").scrollHeight;

  try {
    const res = await fetch("/api/edit/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        latex: latestLatex(s),
        instruction,
        history: s.chat.slice(0, -1).slice(-10),
        job_description: (s.jd || "").slice(0, 6000),
        api_key: state.settings.apiKey || "",
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Edit failed (${res.status})`);
    }
    let result = null;
    await readSSE(res.body, (u) => {
      if (u.step === "live" && u.event === "thinking") {
        think.textContent = tail(think.textContent + u.text, 900);
        think.scrollTop = think.scrollHeight;
      } else if (u.step === "result") result = u.result;
      else if (u.step === "error") throw new Error(u.message);
    });
    if (!result) throw new Error("The edit stream ended unexpectedly.");
    if (result.changed) {
      s.revisions = s.revisions || [{ latex: s.result.latex, label: "Generated", at: Date.now() }];
      s.revisions.push({ latex: result.latex, label: instruction.slice(0, 80), at: Date.now() });
      s.viewRev = s.revisions.length - 1;
      s.result.latex = result.latex;
    }
    s.chat.push({ role: "assistant", content: result.reply });
  } catch (err) {
    s.chat.push({ role: "assistant", content: `That edit failed: ${err.message}` });
  } finally {
    s.editing = false;
    save(true);
    if (s.id === state.activeId && s.status === "done") renderDesk();
  }
}

/* ══ PDF preview (compile once per version, cache the bytes) ═══════ */

const pdfCache = new Map(); // sessionId -> { key, blobUrl, blob }

const latexKey = (latex) => {
  let h = 0;
  for (let i = 0; i < latex.length; i++) h = (h * 31 + latex.charCodeAt(i)) | 0;
  return `${latex.length}:${h}`;
};

function toggleTab(s) {
  const isTex = s.viewTab === "tex";
  $("d-latex").hidden = !isTex;
  $("d-pdf-view").hidden = isTex;
  $("d-tab-pdf").classList.toggle("on", !isTex);
  $("d-tab-tex").classList.toggle("on", isTex);
  if (!isTex) showPdfPreview(s);
}

async function compilePdf(s) {
  const key = latexKey(currentLatex(s));
  const cached = pdfCache.get(s.id);
  if (cached && cached.key === key) return cached;
  const form = new FormData();
  form.append("latex_content", currentLatex(s));
  const res = await fetch("/api/export/pdf", { method: "POST", body: form });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "PDF compilation failed");
  }
  const blob = await res.blob();
  if (cached?.blobUrl) URL.revokeObjectURL(cached.blobUrl);
  const entry = { key, blob, blobUrl: URL.createObjectURL(blob) };
  pdfCache.set(s.id, entry);
  const pages = parseInt(res.headers.get("X-Page-Count") || "", 10);
  if (pages) {
    s.pages = pages;
    save();
    const badge = $("d-pages");
    if (badge) {
      badge.textContent = `${pages} PAGE${pages > 1 ? "S ⚠" : ""}`;
      badge.classList.toggle("over", pages > 1);
    }
  }
  return entry;
}

async function showPdfPreview(s) {
  const view = $("d-pdf-view");
  if (!view) return;
  const key = latexKey(currentLatex(s));
  const cached = pdfCache.get(s.id);
  if (cached && cached.key === key && view.dataset.key === key) return; // already showing
  view.dataset.key = "";
  view.innerHTML = `<div class="compiling"><span class="meta">COMPILING PLATE…</span></div>`;
  try {
    const entry = await compilePdf(s);
    if (s.id !== state.activeId || s.status !== "done") return; // user moved on
    view.innerHTML = `<iframe class="pdf-frame" title="Resume preview" src="${entry.blobUrl}#toolbar=0&navpanes=0&view=FitH"></iframe>`;
    view.dataset.key = entry.key;
  } catch (err) {
    view.innerHTML = `<div class="compiling"><span class="meta warn-text">PREVIEW FAILED</span>
      <span class="caption">${esc(err.message)} — showing the source instead.</span></div>`;
    setTimeout(() => { if (s.id === state.activeId && s.status === "done") { s.viewTab = "tex"; toggleTab(s); } }, 1600);
  }
}

async function savePdf(s, btn) {
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = "…";
  try {
    const entry = await compilePdf(s); // cached — no recompile if already previewed
    const n = revIndex(s) + 1;
    saveBlob(entry.blob, `resume-r${n}.pdf`);
  } catch (err) { alert(err.message); }
  btn.disabled = false; btn.textContent = original;
}

/* ══ Exports ═══════════════════════════════════════════════════════ */

/* Downloads need the anchor attached to the document, and the object URL must
   outlive the click — revoking immediately cancels the download in some browsers. */
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { a.remove(); URL.revokeObjectURL(url); }, 20000);
}

async function download(s, endpoint, filename, btn) {
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = "…";
  try {
    const form = new FormData();
    form.append("latex_content", currentLatex(s));
    const res = await fetch(endpoint, { method: "POST", body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Export failed");
    }
    const blob = await res.blob();
    saveBlob(blob, filename);
  } catch (err) { alert(err.message); }
  btn.disabled = false; btn.textContent = original;
}

/* ══ Dossier modal ═════════════════════════════════════════════════ */

let pendingFile = null;

function openProfile() {
  const m = $("profile-modal");
  pendingFile = null;
  const dzIdle = m.querySelector(".dz-idle"), dzFile = m.querySelector(".dz-file");
  if (state.profile?.kind === "file") {
    dzIdle.hidden = true; dzFile.hidden = false;
    $("dump-filename").textContent = state.profile.filename;
  } else {
    dzIdle.hidden = false; dzFile.hidden = true;
  }
  if (state.profile?.kind === "text") {
    $("dump-text").hidden = false;
    $("dump-text").value = state.profile.text;
  }
  m.showModal();
}

$("profile-btn").addEventListener("click", openProfile);
$("profile-close").addEventListener("click", () => $("profile-modal").close());
$("browse-btn").addEventListener("click", () => $("dump-file").click());
$("dump-file").addEventListener("change", (e) => e.target.files[0] && stageFile(e.target.files[0]));
$("clear-file").addEventListener("click", () => {
  pendingFile = null; state.profile = null; saveProfile();
  $("profile-modal").querySelector(".dz-idle").hidden = false;
  $("profile-modal").querySelector(".dz-file").hidden = true;
  renderRail();
});
$("toggle-paste").addEventListener("click", () => {
  const ta = $("dump-text"); ta.hidden = !ta.hidden;
});

const dz = $("dropzone");
["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragging"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("dragging"); }));
dz.addEventListener("drop", (e) => e.dataTransfer.files[0] && stageFile(e.dataTransfer.files[0]));

function stageFile(file) {
  if (file.size > 4 * 1024 * 1024) return alert("Keep the file under 4MB — export a lighter PDF or paste the text.");
  pendingFile = file;
  $("profile-modal").querySelector(".dz-idle").hidden = true;
  $("profile-modal").querySelector(".dz-file").hidden = false;
  $("dump-filename").textContent = file.name;
}

$("profile-save").addEventListener("click", async () => {
  if (pendingFile) {
    const b64 = await fileToB64(pendingFile);
    state.profile = { kind: "file", filename: pendingFile.name, fileB64: b64, fileType: pendingFile.type || "application/octet-stream" };
  } else if (!$("dump-text").hidden && $("dump-text").value.trim()) {
    state.profile = { kind: "text", filename: "", text: $("dump-text").value };
  } else if (!state.profile) {
    return alert("Add a file or paste text first.");
  }
  saveProfile(); renderRail();
  $("profile-modal").close();
});

const fileToB64 = (file) => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(r.result.split(",")[1]);
  r.onerror = reject;
  r.readAsDataURL(file);
});

function b64ToFile(b64, filename, type) {
  const bytes = atob(b64);
  const arr = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
  return new File([arr], filename, { type });
}

/* ══ Settings modal ════════════════════════════════════════════════ */

$("settings-btn").addEventListener("click", () => {
  $("api-key-input").value = state.settings.apiKey || "";
  $("settings-modal").showModal();
});
$("settings-close").addEventListener("click", () => $("settings-modal").close());
$("settings-save").addEventListener("click", () => {
  state.settings.apiKey = $("api-key-input").value.trim();
  saveSettings();
  $("settings-modal").close();
});

/* ══ Boot ══════════════════════════════════════════════════════════ */

async function boot() {
  loadState();
  try {
    const res = await fetch("/api/templates");
    state.templates = (await res.json()).templates;
  } catch { state.templates = [{ id: "udaya", name: "Udaya's Template", description: "Default", default: true }]; }
  renderAll();
  loadStars();
  if (!state.profile) setTimeout(openProfile, 400);
}

boot();
