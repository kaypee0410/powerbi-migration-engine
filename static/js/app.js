// Power BI Extraction Insights: upload, follow the steps, preview the workbook, download it.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const form = $("#upload-form"), input = $("#file"), drop = $("#drop"), submit = $("#submit");
  const formError = $("#form-error"), progress = $("#progress"), live = $("#live");
  const result = $("#result"), grid = $("#grid"), statusbar = $("#statusbar");
  const steps = $$("#steps li");
  let preview = null, sheet = "findings", timer = null;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (n) => (n === null || n === undefined || n === "" ? "" : Number(n).toLocaleString());
  const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : many}`;

  // ---- choosing a file ---------------------------------------------------------------------------
  function showFile(f) {
    formError.hidden = true;
    if (!f) return;
    $("#drop-title").textContent = f.name;
    $("#drop-hint").textContent = `${(f.size / 1048576).toFixed(1)} MB. Drop another file to replace it.`;
    drop.classList.add("has-file");
    submit.disabled = false;
  }
  input.addEventListener("change", () => showFile(input.files[0]));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add("is-over");
  }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove("is-over");
  }));
  drop.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (!f) return;
    const dt = new DataTransfer();
    dt.items.add(f);
    input.files = dt.files;
    showFile(f);
  });

  // ---- running a job ------------------------------------------------------------------------------
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = input.files[0];
    if (!f) { fail("Choose a .pbix file first."); return; }
    const body = new FormData(form);
    submit.disabled = true;
    submit.textContent = "Creating workbook";
    result.hidden = true;
    resetSteps(f.name);
    try {
      const res = await fetch("/api/jobs", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `The server answered ${res.status}.`);
      poll(data.id);
    } catch (ex) {
      progress.hidden = true;
      fail(ex.message);
      done();
    }
  });

  function fail(msg) { formError.textContent = msg; formError.hidden = false; }
  function done() { submit.disabled = !input.files[0]; submit.textContent = "Create workbook"; }

  function resetSteps(name) {
    $("#progress-title").firstChild.textContent = "Decoding ";
    $("#progress-file").textContent = name;
    steps.forEach((li) => { li.dataset.state = ""; $(".step-note", li).textContent = ""; });
    progress.hidden = false;
  }

  function poll(id) {
    clearTimeout(timer);
    fetch(`/api/jobs/${id}`).then((r) => r.json()).then((job) => {
      renderSteps(job);
      if (job.status === "done") {
        $("#progress-title").firstChild.textContent = "Decoded ";
        renderResult(job); done(); loadRecent(); return;
      }
      if (job.status === "error") { $("#progress-title").firstChild.textContent = "Stopped decoding "; done(); return; }
      timer = setTimeout(() => poll(id), 700);
    }).catch(() => { timer = setTimeout(() => poll(id), 1500); });
  }

  function renderSteps(job) {
    const at = job.status === "done" ? steps.length : job.step;
    steps.forEach((li, i) => {
      const note = $(".step-note", li);
      if (job.status === "error" && i === at) {
        li.dataset.state = "error";
        note.textContent = `Stopped: ${job.error}`;
      } else if (i < at) {
        li.dataset.state = "done"; note.textContent = "";
      } else if (i === at) {
        li.dataset.state = "active"; note.textContent = job.message || "";
      } else {
        li.dataset.state = ""; note.textContent = "";
      }
    });
    const name = steps[Math.min(at, steps.length - 1)]?.querySelector(".step-name").textContent;
    live.textContent = job.status === "done" ? "Workbook ready." :
      job.status === "error" ? `Stopped: ${job.error}` : `Step ${Math.min(at + 1, steps.length)} of ${steps.length}: ${name}`;
  }

  // ---- the workbook preview -------------------------------------------------------------------------
  function renderResult(job) {
    preview = job.preview;
    const s = job.summary || {};
    $("#result-title").textContent = `Workbook for ${job.file}`;
    $("#dl-xlsx").href = job.downloads.xlsx;
    $("#dl-json").href = job.downloads.json;
    const warn = $("#warnings");
    warn.hidden = !(s.warnings && s.warnings.length);
    warn.textContent = (s.warnings || []).join(" ");
    const items = [
      [s.tabs, "tabs"], [s.data_insights, "data insights"], [s.static_items, "static items"], [s.tables, "tables"],
      [s.relationships, "relationships"], [s.measures, `measures (${num(s.measures_translated)} translated)`],
      [s.measures_review, "measures to review"], [s.findings, `findings (${num(s.findings_high)} high)`],
    ];
    if (s.sql_test_runs !== null && s.sql_test_runs !== undefined) items.push([s.sql_test_runs, "queries ran on the saved data"]);
    statusbar.innerHTML = items.map(([n, label]) => `<span><b>${num(n)}</b> ${esc(label)}</span>`).join("") +
      `<span>SQL dialect <b>${esc(s.sql_dialect)}</b></span><span><b>${esc(s.seconds)}</b> s</span>`;
    result.hidden = false;
    showSheet(sheet);
    result.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }

  const SHEETS = {
    findings: {
      head: ["Severity", "Area", "Finding", "Detail", "Recommendation"],
      rows: (p) => p.findings.map((f) => [
        `<span class="sev sev-${esc(f.severity.toLowerCase())}">${esc(f.severity)}</span>`, esc(f.area),
        `<strong>${esc(f.finding)}</strong>`, cell(f.detail, "wide"), cell(f.recommendation, "wide")]),
      empty: "No findings.",
    },
    insights: {
      head: ["Insight ID", "Tab", "Insight", "Chart type", "Build approach", "Complexity", "Business question"],
      rows: (p) => p.insights.map((i) => [cell(i.id, "id"), esc(i.tab), cell(i.title, "wide"), esc(i.chart),
        esc(i.approach), esc(i.complexity), cell(i.question, "wide")]),
      empty: "No report pages in this file.",
    },
    measures: {
      head: ["Measure", "Scope", "Pattern", "Translation", "Used by insights"],
      rows: (p) => p.measures.map((m) => [`<strong>${esc(m.name)}</strong>`, esc(m.scope), esc(m.pattern),
        m.status.startsWith("Translated") ? `<span class="tag-ok">${esc(m.status)}</span>` :
          `<span class="tag-bad">${esc(m.status)}</span>`, esc(m.used || "-")]),
      empty: "No measures in this model.",
    },
    sources: {
      head: ["Table", "Role", "Rows", "Connector", "Source object(s)", "Target view"],
      rows: (p) => p.sources.map((t) => [`<strong>${esc(t.table)}</strong>`, esc(t.role), cell(num(t.rows), "num"),
        esc(t.connector), cell(t.objects, "wide"), esc(t.view)]),
      empty: "No tables in this model.",
    },
    sql: {
      head: ["Insight ID", "Insight", "Approach", "Test run", "Rows", "Reference SQL"],
      rows: (p) => p.sql.map((q) => [cell(q.id, "id"), cell(q.title, "wide"), esc(q.approach),
        q.run === "Runs" ? `<span class="tag-ok">Runs</span>` : q.run === "Error" ? `<span class="tag-bad">Error</span>` : esc(q.run),
        cell(num(q.rows), "num"), `<pre>${esc(q.sql)}</pre>`]),
      empty: "No insight queries.",
    },
  };

  function cell(v, cls) { return { html: esc(v), cls }; }

  function showSheet(name) {
    sheet = name;
    $$(".sheet-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.sheet === name)));
    if (!preview) return;
    const def = SHEETS[name];
    const rows = def.rows(preview);
    const head = `<thead><tr><th class="rn" scope="col"></th>${def.head.map((h) => `<th scope="col">${esc(h)}</th>`).join("")}</tr></thead>`;
    const body = rows.length ? rows.map((r, i) => `<tr><td class="rn">${i + 1}</td>${r.map((c) =>
      typeof c === "object" ? `<td class="${c.cls || ""}">${c.html}</td>` : `<td>${c}</td>`).join("")}</tr>`).join("")
      : `<tr><td class="rn">1</td><td colspan="${def.head.length}">${esc(def.empty)}</td></tr>`;
    grid.innerHTML = head + `<tbody>${body}</tbody>`;
  }
  $$(".sheet-tabs button").forEach((b) => b.addEventListener("click", () => showSheet(b.dataset.sheet)));
  $(".sheet-tabs").addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    const tabs = $$(".sheet-tabs button");
    const i = tabs.findIndex((b) => b.dataset.sheet === sheet);
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    next.focus();
    showSheet(next.dataset.sheet);
  });

  // ---- workbooks already on this computer ---------------------------------------------------------------
  async function loadRecent() {
    const res = await fetch("/api/jobs").catch(() => null);
    if (!res || !res.ok) return;
    const rows = await res.json();
    const tbody = $("#recent");
    if (!rows.length) return;
    tbody.innerHTML = rows.map((r) => {
      const s = r.summary || {};
      const when = new Date(r.created * 1000).toLocaleString();
      return `<tr><td><button class="link-like plain" data-open="${esc(r.id)}">${esc(r.file)}</button></td>
        <td>${esc(when)}</td>
        <td>${plural(s.tabs, "tab", "tabs")}, ${plural(s.data_insights, "insight", "insights")}, ${plural(s.measures, "measure", "measures")}</td>
        <td><a href="${esc(r.downloads.xlsx)}">Workbook</a><a href="${esc(r.downloads.json)}">JSON</a></td></tr>`;
    }).join("");
    $$("[data-open]", tbody).forEach((b) => b.addEventListener("click", async () => {
      const job = await (await fetch(`/api/jobs/${b.dataset.open}`)).json();
      progress.hidden = true;
      renderResult(job);
    }));
  }
  loadRecent();
})();
