// Power BI Extraction Insights: upload, follow the steps, review the results, download the workbook.
// All text from the API comes from client files: it goes into the page through esc() or textContent only.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const CONFIG = JSON.parse($("#pbi-config").textContent);
  const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

  const form = $("#upload-form"), input = $("#file"), drop = $("#drop"), submit = $("#submit");
  const formError = $("#form-error"), progress = $("#progress"), live = $("#live");
  const steps = $$("#steps li"), stepsList = $("#steps"), runSummary = $("#run-summary");
  const stepsToggle = $("#steps-toggle"), result = $("#result");
  let job = null, done = new Set(), timer = null, tab = "review";

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const has = (n) => n !== null && n !== undefined && n !== "";
  const num = (n) => (has(n) ? Number(n).toLocaleString() : "");
  const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : many}`;
  const dialectName = (key) => CONFIG.dialects[key] || key || "";
  const icon = '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M7 1v8M3.5 5.5 7 9l3.5-3.5M1.5 12.5h11" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>';

  // Text with highlighted spans. Spans are code-point offsets from the server; each slice is escaped.
  function marked(text, spans) {
    const cp = Array.from(String(text ?? ""));
    let out = "", at = 0;
    for (const [s, e] of spans || []) {
      if (s < at || e <= s) continue;
      out += esc(cp.slice(at, s).join("")) + "<mark>" + esc(cp.slice(s, e).join("")) + "</mark>";
      at = e;
    }
    return out + esc(cp.slice(at).join(""));
  }

  // ---- choosing a file ---------------------------------------------------------------------------
  function showFile(f) {
    formError.hidden = true;
    if (!f) return;
    $("#drop-title").textContent = f.name;
    $("#drop-hint").textContent = `${(f.size / 1048576).toFixed(1)} MB. Drop another file here or browse to replace it.`;
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
    submit.textContent = "Creating workbook…";
    formError.hidden = true;
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
      ready();
    }
  });

  function fail(msg) { formError.textContent = msg; formError.hidden = false; }
  function ready() { submit.disabled = !input.files[0]; submit.textContent = "Create workbook"; }

  function resetSteps(name) {
    $("#progress-verb").textContent = "Decoding";
    $("#progress-file").textContent = name;
    steps.forEach((li) => { li.dataset.state = ""; $(".step-note", li).textContent = ""; });
    runSummary.hidden = true;
    stepsList.hidden = false;
    progress.hidden = false;
  }

  function poll(id) {
    clearTimeout(timer);
    fetch(`/api/jobs/${encodeURIComponent(id)}`).then((r) => r.json()).then((j) => {
      renderSteps(j);
      if (j.status === "done") { foldSteps(j); renderResult(j, true); ready(); loadRecent(); return; }
      if (j.status === "error") {
        $("#progress-verb").textContent = "Stopped decoding";
        fail(`${String(j.error || "The run failed").replace(/\.\s*$/, "")}. Check that the file opens in Power BI Desktop. A live-connection (thin) report has no ` +
          "model inside: export a .pbit or use the model's own .pbix.");
        ready();
        return;
      }
      timer = setTimeout(() => poll(id), 700);
    }).catch(() => { timer = setTimeout(() => poll(id), 1500); });
  }

  function renderSteps(j) {
    const at = j.status === "done" ? steps.length : j.step;
    steps.forEach((li, i) => {
      const note = $(".step-note", li);
      if (j.status === "error" && i === at) { li.dataset.state = "error"; note.textContent = `Stopped: ${j.error}`; }
      else if (i < at) { li.dataset.state = "done"; note.textContent = ""; }
      else if (i === at) { li.dataset.state = "active"; note.textContent = j.message || ""; }
      else { li.dataset.state = ""; note.textContent = ""; }
    });
    const name = $(".step-name", steps[Math.min(at, steps.length - 1)]).textContent;
    live.textContent = j.status === "done" ? "Workbook ready." :
      j.status === "error" ? `Stopped: ${j.error}` : `Step ${Math.min(at + 1, steps.length)} of ${steps.length}: ${name}`;
  }

  // The one orchestrated moment: when the run ends the steps fold into a single line.
  function foldSteps(j) {
    const s = j.summary || {};
    $("#progress-verb").textContent = "Decoded";
    $("#run-summary-text").innerHTML = `<strong>Decoded in ${esc(s.seconds)} s</strong>, all ${steps.length} steps done.`;
    setTimeout(() => {
      stepsList.hidden = true;
      runSummary.hidden = false;
      stepsToggle.setAttribute("aria-expanded", "false");
      stepsToggle.textContent = "Show steps";
    }, reduceMotion() ? 0 : 350);
  }
  stepsToggle.addEventListener("click", () => {
    const open = stepsToggle.getAttribute("aria-expanded") === "true";
    stepsToggle.setAttribute("aria-expanded", String(!open));
    stepsToggle.textContent = open ? "Show steps" : "Hide steps";
    stepsList.hidden = open;
  });

  // ---- results -----------------------------------------------------------------------------------------
  function renderResult(j, scroll) {
    job = j;
    done = new Set(j.done || []);
    const s = j.summary || {}, p = j.preview || {};
    $("#result-title").textContent = `Workbook for ${j.file}`;
    const ran = has(s.sql_test_runs) ? ` ${plural(s.sql_test_runs, "insight query", "insight queries")} ran on the saved data.` : "";
    $("#result-meta").textContent = `SQL dialect: ${dialectName(s.sql_dialect)}. Made ${s.generated_at || ""}.${ran}`;
    $("#dl-xlsx").href = j.downloads.xlsx;
    $("#dl-json").href = j.downloads.json;
    const warn = $("#warnings");
    warn.hidden = !(s.warnings && s.warnings.length);
    warn.textContent = (s.warnings || []).join(" ");

    renderCoverage(s, p);
    const counts = { review: s.measures_review, insights: (p.insights || []).length, measures: s.measures,
      sources: (p.sources || []).length, sql: (p.sql || []).length };
    $$(".tab").forEach((t) => { $(".count", t).textContent = num(counts[t.dataset.tab]); });
    $("#panel-review").innerHTML = renderReview(s, p);
    $("#panel-insights").innerHTML = renderInsights(p);
    $("#panel-measures").innerHTML = renderMeasures(p);
    $("#panel-sources").innerHTML = renderSources(p);
    $("#panel-sql").innerHTML = renderSql(p);
    selectTab(tab, false);
    result.hidden = false;
    if (scroll) result.scrollIntoView({ behavior: reduceMotion() ? "auto" : "smooth", block: "start" });
  }

  function renderCoverage(s, p) {
    const total = s.measures || 0, ok = s.measures_translated || 0, todo = s.measures_review || 0;
    const bar = $("#bar");
    if (!total) {
      $("#cov-num").textContent = "0";
      $("#cov-text").textContent = "measures in this model";
      bar.hidden = true; $("#legend").hidden = true;
    } else {
      $("#cov-num").innerHTML = `${num(ok)}<span class="of"> of ${num(total)}</span>`;
      $("#cov-text").textContent = "measures translated to SQL automatically";
      bar.hidden = false; $("#legend").hidden = false;
      bar.innerHTML = (ok ? `<div class="ok-fill" style="flex:${ok}"></div>` : "") +
        (todo ? `<div class="todo-fill" style="flex:${todo}"></div>` : "");
      bar.setAttribute("aria-label", `${ok} of ${total} measures translated`);
      $("#legend").innerHTML = `<span><i class="key ok-fill"></i>${num(ok)} translated</span>` +
        (todo ? `<span><i class="key todo-fill"></i>${num(todo)} to review, grouped by what blocked them</span>` : "");
    }
    const stats = [[s.tabs, "report tabs"], [s.data_insights, "data insights"], [s.tables, "tables"],
      [s.relationships, "relationships"], [p.target_views, "target views"],
      [s.findings, `findings, ${num(s.findings_high || 0)} high`]].filter(([n]) => has(n));
    $("#stats").innerHTML = stats.map(([n, l]) => `<div class="stat"><dt>${esc(l)}</dt><dd>${num(n)}</dd></div>`).join("");
  }

  // ---- tabs (ARIA tabs pattern, arrow keys move) -----------------------------------------------------------
  function selectTab(name, focus) {
    tab = name;
    $$(".tab").forEach((t) => {
      const on = t.dataset.tab === name;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      $(`#${t.getAttribute("aria-controls")}`).hidden = !on;
      if (on && focus) t.focus();
    });
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab, false)));
  $("#tabs").addEventListener("keydown", (e) => {
    const tabs = $$(".tab"), i = tabs.findIndex((t) => t.dataset.tab === tab);
    const to = { ArrowRight: i + 1, ArrowLeft: i - 1 + tabs.length, Home: 0, End: tabs.length - 1 }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    selectTab(tabs[to % tabs.length].dataset.tab, true);
  });

  const table = (head, rows, empty) => `<div class="table-wrap"><table><thead><tr>${head.map(([h, cls]) =>
    `<th scope="col"${cls ? ` class="${cls}"` : ""}>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.length ? rows.join("") :
    `<tr><td colspan="${head.length}" class="muted">${esc(empty)}</td></tr>`}</tbody></table></div>`;

  function renderReview(s, p) {
    let out = "";
    if (!p.review) {
      out = `<p class="empty-note">This run was made before measures were grouped on the page. The Measures sheet of the
        workbook lists the ${num(s.measures_review)} measures to review. Run the file again to see them here.</p>`;
    } else if (!p.review.length) {
      out = `<p class="empty-note">Every measure translated automatically. Nothing to write by hand.</p>`;
    } else {
      out = `<div class="review-intro"><h3>Measures to write by hand</h3>
        <p class="help">Highlighted parts are what the rules couldn't translate.</p></div>` +
        p.review.map((g, gi) => `
        <details class="group"${gi === 0 ? " open" : ""}>
          <summary>
            <div><div class="group-title">${/^[A-Z][A-Z0-9_.]*$/.test(g.pattern) ? `<code>${esc(g.pattern)}</code>`
              : `<span class="chip">${esc(g.pattern)}</span>`}</div><div class="group-why">${esc(g.why)}</div></div>
            <span class="group-n" aria-label="${g.measures.length} measures">${g.measures.length}</span>
            <span class="chev" aria-hidden="true"></span>
          </summary>
          <p class="group-fix">${esc(g.fix)}</p>
          ${g.measures.map(measureRow).join("")}
        </details>`).join("");
    }
    const sevClass = (v) => `sev sev-${esc(String(v).toLowerCase())}`;
    const findings = (p.findings || []).map((f) => `<tr><td><span class="${sevClass(f.severity)}">${esc(f.severity)}</span></td>
      <td>${esc(f.area)}</td><td><strong>${esc(f.finding)}</strong><br><span class="muted">${esc(f.detail)}</span></td>
      <td>${esc(f.recommendation)}</td></tr>`);
    return out + `<h3 style="margin-top:28px">Other findings</h3>` +
      table([["Severity"], ["Area"], ["Finding"], ["What to do"]], findings, "No findings.");
  }

  function measureRow(m) {
    const isDone = done.has(m.name);
    const reasons = m.reasons.length > 1 || !m.sql ? `<ul class="reasons">${m.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "";
    return `
      <details class="measure" data-measure="${esc(m.name)}">
        <summary><span class="measure-name">[${esc(m.name)}]</span>
          <span class="status${isDone ? " is-done" : ""}">${isDone ? "Done" : "Draft SQL"}</span></summary>
        ${reasons}
        <div class="split">
          <div><p class="split-label">DAX in Power BI</p><pre>${marked(m.dax, m.dax_marks)}</pre></div>
          <div><p class="split-label">Draft ${esc(dialectName(job.summary.sql_dialect))}</p>
            <pre>${m.sql ? marked(m.sql, m.sql_marks) : '<span class="muted">No draft SQL for this measure.</span>'}</pre></div>
        </div>
        <div class="measure-actions">
          ${m.sql ? '<button type="button" class="btn btn-small" data-copy>Copy SQL</button>' : ""}
          <button type="button" class="btn btn-small" data-done aria-pressed="${isDone}">${isDone ? "Mark as to do" : "Mark as done"}</button>
        </div>
      </details>`;
  }

  function findMeasure(name) {
    for (const g of (job.preview.review || [])) for (const m of g.measures) if (m.name === name) return m;
    return null;
  }

  $("#panel-review").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy], [data-done]");
    if (!btn) return;
    const row = btn.closest(".measure"), m = findMeasure(row.dataset.measure);
    if (!m) return;
    if (btn.hasAttribute("data-copy")) {
      await copy(m.sql);
      btn.textContent = "Copied";
      setTimeout(() => { btn.textContent = "Copy SQL"; }, 1500);
      return;
    }
    const want = !done.has(m.name);
    btn.disabled = true;
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(job.id)}/done`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ measure: m.name, done: want }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `The server answered ${res.status}.`);
      done = new Set(data.done);
      const st = $(".status", row);
      st.textContent = want ? "Done" : "Draft SQL";
      st.classList.toggle("is-done", want);
      btn.textContent = want ? "Mark as to do" : "Mark as done";
      btn.setAttribute("aria-pressed", String(want));
      live.textContent = `${m.name} marked as ${want ? "done" : "to do"}.`;
    } catch (ex) {
      live.textContent = `Could not save: ${ex.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* fall back below */ }
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } finally { ta.remove(); }
  }

  function approachPill(a) {
    if (a === "Needs review") return '<span class="pill pill-todo">Needs review</span>';
    if (a === "Static") return '<span class="pill pill-plain">Static</span>';
    return `<span class="pill pill-ok">${esc(a)}</span>`;
  }

  function renderInsights(p) {
    const rows = (p.insights || []).map((i) => `<tr><td class="code">${esc(i.id)}</td><td>${esc(i.tab)}</td>
      <td>${esc(i.title || i.question)}</td><td>${esc(i.chart)}</td><td class="num">${num(i.rows)}</td>
      <td>${approachPill(i.approach)}</td></tr>`);
    return table([["ID"], ["Tab"], ["Insight"], ["Visual"], ["Rows returned", "num"], ["Build approach"]], rows,
      "No report pages in this file.");
  }

  function renderMeasures(p) {
    const rows = (p.measures || []).map((m) => `<tr><td class="code">[${esc(m.name)}]</td><td>${esc(m.scope)}</td>
      <td>${esc(m.pattern)}</td><td>${m.status.startsWith("Translated") ? `<span class="pill pill-ok">${esc(m.status)}</span>`
      : `<span class="pill pill-todo">${esc(m.status)}</span>`}</td><td>${esc(m.used || "-")}</td></tr>`);
    return table([["Measure"], ["Scope"], ["Pattern"], ["SQL"], ["Used by insights"]], rows, "No measures in this model.");
  }

  function renderSources(p) {
    const rows = (p.sources || []).map((t) => `<tr><td><strong>${esc(t.table)}</strong></td><td>${esc(t.role)}</td>
      <td>${esc(t.connector)}</td><td class="code">${esc(t.objects)}</td><td class="num">${has(t.steps) ? num(t.steps) : ""}</td>
      <td class="num">${num(t.rows)}</td><td class="code">${esc(t.view)}</td></tr>`);
    return table([["Table"], ["Role"], ["Source"], ["Object"], ["Applied steps", "num"], ["Rows", "num"], ["Target view"]],
      rows, "No tables in this model.");
  }

  function renderSql(p) {
    const run = (r) => r === "Runs" ? '<span class="pill pill-ok">Runs</span>' :
      r === "Error" ? '<span class="pill pill-bad">Error</span>' : `<span class="pill pill-plain">${esc(r)}</span>`;
    const rows = (p.sql || []).map((q) => `<tr><td class="code">${esc(q.id)}</td><td>${esc(q.title)}</td>
      <td>${approachPill(q.approach)}</td><td>${run(q.run)}</td><td class="num">${num(q.rows)}</td>
      <td><pre>${esc(q.sql)}</pre></td></tr>`);
    return table([["ID"], ["Insight"], ["Approach"], ["Test run"], ["Rows", "num"], ["Reference SQL"]], rows,
      "No insight queries.");
  }

  // ---- workbooks on this computer, grouped by file --------------------------------------------------------
  const DIFF_KEYS = [["measures_translated", "measure translated", "measures translated"],
    ["measures_review", "measure to review", "measures to review"], ["measures", "measure", "measures"],
    ["data_insights", "insight", "insights"], ["tabs", "tab", "tabs"], ["findings_high", "high finding", "high findings"],
    ["findings", "finding", "findings"]];

  function diff(now, before) {
    const parts = DIFF_KEYS.map(([k, one, many]) => {
      const d = (now[k] || 0) - (before[k] || 0);
      return d ? `${Math.abs(d)} ${d > 0 ? "more" : "fewer"} ${Math.abs(d) === 1 ? one : many}` : null;
    }).filter(Boolean);
    return parts.length ? `${parts.join(", ")} than the earlier run` : "No changes from the earlier run";
  }

  async function loadRecent() {
    const res = await fetch("/api/jobs").catch(() => null);
    if (!res || !res.ok) return;
    const runs = await res.json();
    if (!runs.length) return;
    const files = new Map();
    runs.forEach((r) => { if (!files.has(r.file)) files.set(r.file, []); files.get(r.file).push(r); });
    $("#recent").innerHTML = [...files].map(([file, list]) => `
      <div class="hist-file">
        <div class="hist-top"><span class="hist-name">${esc(file)}</span>
          <span class="help">${plural(list.length, "run", "runs")}</span></div>
        <ul class="hist-runs">${list.map((r, i) => {
          const s = r.summary || {};
          const change = list[i + 1] ? `. <span class="diff">${esc(diff(s, list[i + 1].summary || {}))}</span>` : "";
          return `<li><button type="button" class="linkbtn" data-open="${esc(r.id)}">${esc(new Date(r.created * 1000).toLocaleString())}</button>
            <span>${plural(s.tabs, "tab", "tabs")}, ${plural(s.data_insights, "insight", "insights")},
              ${plural(s.measures, "measure", "measures")}${change}</span>
            <span class="hist-links"><a href="${esc(r.downloads.xlsx)}" download>Workbook</a><a href="${esc(r.downloads.json)}" download>JSON</a></span></li>`;
        }).join("")}</ul>
      </div>`).join("");
  }

  $("#recent").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-open]");
    if (!b) return;
    const res = await fetch(`/api/jobs/${encodeURIComponent(b.dataset.open)}`).catch(() => null);
    if (!res || !res.ok) { live.textContent = "That workbook could not be opened."; return; }
    progress.hidden = true;
    renderResult(await res.json(), true);
  });

  loadRecent();
})();
