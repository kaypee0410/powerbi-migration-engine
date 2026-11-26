# UI plan: Power BI Extraction Insights

How to turn the approved mockup into the app's real interface.

- **Design reference:** `design/mockups/private/pbi-extraction-insights-final.html` (self-contained mockup with sample data; git-ignored)
- **Scope:** the single page the app serves today. It covers upload, options, the run, results and history.
- **Not in scope:** changing how .pbix files are decoded, translated or exported. The backend stays as it is. Only the page and the data it receives change.

---

## 1. What the redesign fixes

| Today | After |
|---|---|
| The 9-step checklist stays on screen after every run | One line, "Decoded in 6.8 s", with a "Show steps" link |
| The stats strip sits under a scrolling table | Coverage ("8 of 43 translated") and the key counts open the results |
| The "High" finding is one long block of text | Measures grouped by the DAX pattern that blocked them, each with a DAX-to-SQL view |
| The SQL dialect is picked separately from the target (BigQuery with Incorta) | The target is picked first and sets the dialect. A note shows if they don't match |
| The workbook is always the main download | The main download follows the target: the package when it's built, otherwise the workbook |
| The Claude's notes option reads as disabled setup text | A "Set up Claude's notes" link opens three setup steps |
| History lists every run on its own | Runs are grouped by file, with what changed between them |
| A plain header | A header diagram of .pbix parts, then Translate, then the targets. The selected target is highlighted |

---

## 2. Design tokens

Put these in one CSS file (for example `static/css/tokens.css`) and use them everywhere. Don't write hex values into components.

### Colour

| Token | Hex | Use |
|---|---|---|
| `--paper` | `#F2F3F1` | Page background |
| `--surface` | `#FFFFFF` | Panels, tables, cards |
| `--ink` | `#1C2420` | Body text, headings |
| `--muted` | `#5B6660` | Supporting text |
| `--rule` / `--rule-strong` | `#D9DDD8` / `#B9BFB9` | Borders and dividers |
| `--green` / `--green-dark` / `--green-tint` | `#1F6B4F` / `#17533D` / `#E3EFE8` | Translated, primary action, selected target |
| `--amber` / `--amber-text` / `--amber-tint` | `#B7791F` / `#7E520F` / `#F8EBD3` | Needs review, and the parts that couldn't be translated |
| `--red` / `--red-tint` | `#A33A2E` / `#F6E3E0` | High-severity findings only |

Colour rules:
- Green means "translated" or "the main action". Don't use it for decoration.
- Amber means "needs review". Nothing else uses it.
- Red is only for the High severity dot.

### Type

| Role | Face | Sizes |
|---|---|---|
| Display: headings, big numbers | Barlow Semi Condensed 600 | 48 (h1), 26 (h2), 19 (h3), 64 (coverage), 26 (stats) |
| Body: UI text | Source Sans 3, 400 and 600 | 17 (lede), 16 (body), 15 (controls), 14 (help), 13 (meta) |
| Code: DAX, SQL, measure names, object names | JetBrains Mono 400 | 12.5 to 13.5 |

Type rules:
- Monospace is only for real code and object names. Never use it for labels.
- Use sentence case everywhere. Don't use all-caps labels.
- Numbers use `font-variant-numeric: tabular-nums`.
- Bundle the fonts locally (in `static/fonts/`) because the app runs offline. Keep the system fallbacks already in the mockup.

### Shape and spacing
- Corner radius is `4px` on panels, inputs and buttons. Pills are fully rounded.
- Borders are 1px. Panels have no shadows.
- Page max width is 1120px, with 32px side padding (18px on phones).
- Vertical rhythm is 12 / 24 / 36 / 56px between blocks.

---

## 3. Page structure

```
┌───────────────────────────────────────────────────────────────┐
│ HERO (grid-paper band, full width)                            │
│  Title, lede, version pill   │  .pbix parts ─► Translate ─► targets │
├───────────────────────────────────────────────────────────────┤
│ UPLOAD PANEL                                                  │
│  [ drop zone: file name, size ]                               │
│  Migrate to [Incorta|QuickSight|Tableau]  ☑ Read data  ☑ Package  ☐ Claude's notes │
│  [dialect select]  (mismatch note)                            │
│  [Create workbook]                                            │
├───────────────────────────────────────────────────────────────┤
│ RUN STATUS: steps while running, then "Decoded in 6.8 s" + Show steps │
├───────────────────────────────────────────────────────────────┤
│ RESULTS                                                       │
│  Workbook for <file>                [Package] [Workbook] [JSON] │
│  ┌ coverage: 8 of 43, green/amber bar, legend, 6 stats ┐      │
│  Tabs: To review | Insights | Measures | Sources              │
│  To review: pattern groups ▸ measures ▸ DAX | SQL split       │
│             Other findings table                              │
├───────────────────────────────────────────────────────────────┤
│ WORKBOOKS ON THIS COMPUTER (grouped by file, runs listed)     │
└───────────────────────────────────────────────────────────────┘
```

Everything is left-aligned. Text never runs past 62 characters per line.

---

## 4. Components

Build each as its own partial or template block so they can be tested on their own.

| Component | Contents | Behaviour |
|---|---|---|
| `Hero` | Title, lede, version pill, flow SVG | The target box and its line turn green when the target changes |
| `DropZone` | File icon, name, size, hint | Click to browse. Highlights on drag over. Accepts `.pbix` and `.pbit` |
| `TargetPicker` | 3-button segmented control, dialect select, mismatch note | Picking a target sets its default dialect. A mismatch shows the note |
| `OptionCheck` | Checkbox, title, help text | "Build the package" relabels to match the target |
| `AiSetup` | Checkbox, setup link, 3-step list | The link toggles the list (`aria-expanded`) |
| `RunStatus` | Step list, summary line | Steps tick while running, then fold into one line |
| `ResultHeader` | File name, target and dialect line, download buttons | The primary button follows the target and the package option |
| `Coverage` | Big number, bar, legend, 6 stats | Bar segments sized by translated vs review |
| `Tabs` | To review, Insights, Measures, Sources | ARIA tabs. Arrow keys move between them |
| `PatternGroup` | Pattern code chip, why, fix, count, measure rows | `<details>`. The first group is open by default |
| `MeasureRow` | Name, status, DAX and SQL split, Copy SQL, Mark as done | Untranslated parts highlighted in amber |
| `FindingsTable` | Severity dot, area, finding, what to do | Sorted High, then Medium, then Low |
| `History` | File name, run count, runs with diff and links | Grouped by file name |

---

## 5. Data contract

The page draws everything from one object. The backend returns it as JSON when a run finishes (or embeds it in the page). Field names match the mockup's `RESULT` object.

```jsonc
{
  "file": "Sample Report.pbix",
  "sizeMB": 4.5,
  "seconds": 6.8,
  "target": "incorta",            // incorta | quicksight | tableau
  "dialect": "spark",             // spark | postgres | athena | redshift | bigquery | snowflake
  "counts": {
    "tabs": 14, "insights": 67, "staticItems": 15, "tables": 27,
    "relationships": 36, "measures": 43, "translated": 8,
    "findings": 19, "high": 2, "queries": 67, "mvs": 26, "joins": 7
  },
  "steps": ["Open file", "Read report layout", "..."],
  "groups": [                     // measures needing review, grouped by blocking pattern
    {
      "pattern": "VALUES",
      "why": "Used as a filter or iterator, which has no direct SQL equivalent.",
      "fix": "Usually becomes a join to a distinct list or an IN subquery.",
      "measures": [
        { "name": "Measure A",
          "dax": "... «VALUES(Dim[Key])» ...",
          "sql": "... «-- grain: one row per key?»",
          "done": false }
      ]
    }
  ],
  "otherReviewCount": 17,         // review measures not in a named group
  "findings": [ { "severity": "high", "area": "Model", "title": "...", "detail": "...", "rec": "..." } ],
  "insights": [ { "tab": "Overview", "name": "...", "type": "Line", "rows": 24, "ok": true } ],
  "sources":  [ { "name": "Fact", "kind": "SQL Server", "detail": "dbo.fact_table", "steps": 6 } ],
  "downloads": { "package": "/download/<run>/package.zip", "workbook": "/download/<run>/workbook.xlsx", "json": "/download/<run>/result.json" },
  "history": [ { "file": "...", "runs": [ { "id": "...", "created": "26/11/2026, 00:06", "summary": "...", "diff": "No changes from the earlier run" } ] }
}
```

### Backend changes needed

1. **Group review measures by pattern.** The translator already knows why each measure failed (for example "VALUES is not translated automatically"). Return that reason as a structured `pattern` key instead of joining the reasons into one finding string. Map the patterns to `why` and `fix` text in one lookup table.
2. **Mark untranslated spans.** Wrap the part of the DAX that blocked translation, and the placeholder in the draft SQL, in `«…»`. If exact spans are hard to get at first, mark only the function name (for example `«VALUES»`). The page escapes HTML before it highlights anything.
3. **Draft SQL for review measures.** The partial SQL the Measures sheet already has goes in `sql`.
4. **Default dialect per target.** Incorta uses Spark SQL. Confirm the defaults for QuickSight and Tableau (see open questions).
5. **History diff.** When a file name has more than one run, compare the `counts` of the latest run with the one before. Return "No changes from the earlier run" or a short list such as "2 more measures translated".
6. **Done state.** Store each measure's `done` flag per run, in the run folder next to the workbook, so it survives a refresh.
7. **Download URLs** for each run.

---

## 6. Interactions

| Action | Result |
|---|---|
| Drop or browse a file | Drop zone shows the name and size |
| Pick a target | Hero highlight, default dialect, package label and primary download all update |
| Change the dialect so it doesn't match the target | Amber note under the select. The run is still allowed |
| Click "Create workbook" | Button shows "Creating workbook…" and is disabled. Steps tick as the backend reports progress. At the end they fold into one line and the page scrolls to the results |
| The run fails | The step that failed shows an error and what to try next. The button is enabled again |
| Open a pattern group or measure | `<details>` expands. No page reload |
| Click "Copy SQL" | SQL goes to the clipboard without the `«»` marks. The button reads "Copied" for 1.5 s |
| Click "Mark as done" | Status turns green "Done". Saved to the backend |
| Pick a history run | Loads that run's results into the results section |

**Progress:** replace the mockup's timer with real progress. Send step events from the backend, using server-sent events or polling a `/runs/<id>/status` endpoint. Each event carries `{ step, state: "active" | "done" | "error", message }`.

**Motion:** the step sequence is the page's only animation. Everything else changes instantly or with a 150 to 200 ms colour fade. Respect `prefers-reduced-motion`.

---

## 7. Accessibility and responsive

- All controls work by keyboard, with a visible 2px green focus ring.
- Tabs follow the ARIA tabs pattern. Groups use native `<details>`.
- The hero SVG has a `<title>`. The coverage bar has an `aria-label` ("8 of 43 measures translated").
- Run status is in an `aria-live="polite"` region.
- Colour is never the only signal: statuses carry text ("Draft SQL", "Done", "High").
- Text contrast is at least 4.5:1. Check amber text on amber tint, and muted text on paper.
- Breakpoints:
  - **980px:** the hero diagram moves below the text.
  - **900px:** options go to 2 columns and stats to 3.
  - **600px:** options go to 1 column, stats to 2, and the DAX/SQL split stacks.

---

## 8. Build order

| Phase | Work | Done when |
|---|---|---|
| 1. Foundations | Tokens file, local fonts, base styles, page shell | The empty page matches the mockup's colours, type and spacing |
| 2. Static page | Port the mockup into the app's templates, split into the components above, still on sample data | The app's page looks the same as the mockup at 1280, 900 and 400 px |
| 3. Upload and options | Wire the drop zone, target picker, dialect and options to the real upload endpoint | A real .pbix uploads with the chosen target and options |
| 4. Data contract | Backend returns the JSON in section 5, including pattern groups and `«»` spans | A real report renders with real data and no sample data left |
| 5. Live progress | Step events from the backend drive the run status | Steps tick in real time, and an error state shows on a failed run |
| 6. Downloads and history | Real download URLs, grouped history, diff text, load a past run | Every download works. Past runs reopen |
| 7. Review workflow | Copy SQL, Mark as done saved per run | Done state survives a refresh |
| 8. Polish and checks | Keyboard pass, contrast check, reduced motion, phone layout | Section 9 passes |

---

## 9. Acceptance checks

Run these against the sample .pbix files in `samples/` (local only):

- [ ] Counts in the coverage block match the workbook's sheets for each file.
- [ ] Translated plus review equals total measures. The bar proportions match.
- [ ] Every review measure appears in exactly one pattern group or in the "other patterns" count.
- [ ] Highlighted spans show for each listed measure. No raw `«` or `»` is visible anywhere.
- [ ] Copied SQL pastes without the `«»` marks.
- [ ] Picking each target updates the hero, dialect, package label and primary download.
- [ ] A dialect mismatch shows the note. A run still completes.
- [ ] A corrupt or non-.pbix file shows a clear error and recovers.
- [ ] Two runs of the same file are grouped, with a diff line.
- [ ] The page works fully by keyboard, and at 400px wide.
- [ ] The page loads with no network access (fonts bundled).

---

## 10. Open questions

1. **App stack:** which framework serves the page (Flask, FastAPI, Streamlit or other)? This decides the template engine and how progress events are sent.
2. **Default dialects:** confirm the defaults for QuickSight (Athena, or the source database's dialect?) and Tableau.
3. **Pattern list:** are there more blocking patterns than the six in the mockup (VALUES, CALCULATETABLE, REMOVEFILTERS/ALL, COUNTROWS over a computed table, TREATAS, SUMX over a grouped table)? Should the "other patterns" bucket get its own groups?
4. **Done state:** should it live with the run only, or carry over to the next run of the same file?
5. **Claude's notes on:** where should Claude's drafts appear? The suggestion is a third column, or a "Claude's draft" toggle, in the DAX/SQL split.

---

## 11. Check against the app (added by `/ui-from-mockup`, status: Planned)

The mockup file is at `design/mockups/assets/pbi-extraction-insights-final.html` (the path at the top of
this plan says `design/`). Stack answer for open question 1: **Flask + Jinja, vanilla JS, no build step**.
Progress already works by polling `GET /api/jobs/<id>` every few hundred ms (`step`, `message`, `error`),
so phase 5 is restyling, not new plumbing.

### 11.1 Where each mockup field comes from

| Mockup field | Real source today | Status |
|---|---|---|
| `file`, `sizeMB`, `seconds` | `summary.file`, `summary.file_size_mb`, `summary.seconds` | Exists |
| `counts.tabs / insights / staticItems / tables / relationships / measures` | `summary.tabs / data_insights / static_items / tables / relationships / measures` | Exists |
| `counts.translated` | `summary.measures_translated` (includes "Translated with notes") | Exists |
| review count | `summary.measures_review` (not `measures - translated`: use the real field) | Exists |
| `counts.findings`, `counts.high` | `summary.findings`, `summary.findings_high` | Exists |
| `counts.queries` ("ran on the saved data") | `summary.sql_test_runs`: only when "Read the data" is on, else hide the sentence | Exists, conditional |
| `counts.mvs` ("Incorta MVs") | Nothing. Nearest is the number of target views (`len(views)`) | **Gap A** |
| `counts.joins` | Not shown in the mockup UI | Drop |
| `steps` | `STEPS` in `pipeline.py` (8 steps, mockup shows 9 incl. "Plan the Incorta build") | Exists; 9th step only with Gap B |
| `groups[].pattern / why / fix` | Translator review reasons are free text (`"VALUES is not translated automatically"`, `"COUNTROWS over a computed table"`, `"time intelligence (...)"`, `"... context transition ..."`, ~25 forms). Derive a key with one lookup in the backend | **New (small)** |
| `groups[].measures[].dax` / `.sql` | Measure `dax` and draft `sql` already exist; untranslated parts render as `NULL /* REVIEW: reason */` | Exists |
| `«…»` marks in SQL | Wrap each `NULL /* REVIEW: … */` | **New (small)**, exact |
| `«…»` marks in DAX | Translator keeps no source positions. Highlight the blocking function name where it appears (e.g. every `VALUES(`) | **New (small)**, approximate: the plan's fallback option |
| `otherReviewCount` | Review measures whose reason has no group | New (derived) |
| `findings[]` | `findings`: `severity` ("High"/"Medium"/"Low"/"Info"), `area`, `finding`, `detail`, `recommendation` | Exists (rename only) |
| `insights[]` tab, name, type, ok | `plans`: `page`, `title`, `generic_type`, `approach != "Needs review"` | Exists |
| `insights[].rows` | Test-run rows, only with "Read the data" on; blank otherwise | Exists, conditional |
| `sources[]` name, kind, detail | Already in `_preview().sources` (`table`, `connector`, `objects`) | Exists |
| `sources[].steps` | `len(sources[t].m.steps)` | New (one line) |
| `target` | No target option in the pipeline: the workbook already covers all three tools | **Gap B** |
| `downloads.workbook / json` | `/api/jobs/<id>/download/xlsx` and `/json` | Exists |
| `downloads.package` | No package is built today | **Gap B** |
| `history[]` grouped by file | `GET /api/jobs` returns `file`, `created`, `summary`: group in the browser | Exists |
| `history[].diff` | Compare the two latest runs' `summary` counts | New (browser only) |
| `done` per measure | Nothing stored | **New**: `outputs/<id>/review.json` + one POST endpoint |
| Claude's notes option | Nothing, and against the project rule that nothing leaves the machine | **Gap C** |

### 11.2 Gaps and decisions

| # | Gap | Options | Recommendation |
|---|---|---|---|
| A | "Incorta MVs" stat has no data | (1) Show "target views" from `views` instead; (2) wait for the package (B) | (1): label it "target views" |
| B | Target picker + "Build the package" + package download + 9th step: the pipeline has no target and builds no .zip/PDF/lineage diagram | (1) Picker only sets the default dialect and labels; hide package option and download until a package builder exists; (2) build the package now (a new backend feature, outside "backend stays as it is") | (1) now, package as its own later piece |
| C | "Add Claude's notes" sends client DAX to an API. `CLAUDE.md` says no AI/API call in the pipeline | (1) Leave it out; (2) show it but disabled ("coming later"); (3) build it as opt-in, off by default, and change the project rule | (1) until the team decides the data rule |
| D | Mockup dialects (Spark, PostgreSQL, Athena, Redshift, BigQuery, Snowflake) differ from the app's (adds Same as source, T-SQL, MySQL, Oracle; no Athena) | Keep the app's list rendered from `DIALECTS`; add Athena (installed sqlglot 30.33 supports it) | Keep server list + add Athena; QuickSight default Athena, Tableau default "Same as source" |
| E | Mockup drops today's "Insight SQL" preview tab | (1) Keep it as a 5th tab; (2) drop it (SQL still in the workbook) | (1) |
| F | Web fonts (Barlow Semi Condensed, Source Sans 3, JetBrains Mono) | (1) Download the woff2 files once (all SIL OFL) into `static/fonts/` with the licence; (2) system fonts only (Bahnschrift / Segoe UI / Cascadia Mono, the current stack) | (1), Latin subset, about 250 KB |
| G | Old runs' `meta.json` lack the new fields | Fall back to what's there and hide empty blocks | Fallback |
| H | `.bim` files: mockup accepts `.pbix,.pbit` only | Keep the server's list (`.pbix, .pbit, .bim`) | Keep |

### 11.3 Answers to section 10
1. Flask + Jinja; progress by polling (already in place).
2. See Gap D.
3. More than six: also time intelligence, context transition (measure evaluated per row), row-context columns, `RELATED` outside row context, `IN` against a table, `FILTER(ALL(...))` across tables, parse errors, unknown names. Proposal: one group per derived key, sorted by count; keys with one measure fall into "other patterns".
4. Your call (still open). Simplest: per run only.
5. Depends on Gap C.

### 11.4 Data safety
Done: the mockup now lives in `design/mockups/private/` (git-ignored) and the client names in sections 5
and 9 were replaced with placeholders. The page is filled only from the API, never from mockup data.

### 11.5 Decisions (26 Nov 2026)
| Gap | Decision |
|---|---|
| A | "Incorta MVs" stat becomes "target views" (`len(views)`) |
| B | **No target picker.** Keep the dialect select (server list from `DIALECTS`). The hero diagram stays, showing all three targets with no selected state. No package option or package download, no 9th step |
| C | **No Claude's notes** option |
| D | No new dialects: with no target picker there is no Athena default to serve |
| E | Keep "Insight SQL" as a 5th tab |
| F | Bundle the three fonts (woff2, Latin, SIL OFL) in `static/fonts/` |
| G | Old runs: fall back, hide blocks that have no data |
| H | Keep `.pbix, .pbit, .bim` |
| Q4 | Done state per run only (`outputs/<id>/review.json`) |

Status: **Approved** with the decisions above.

### 11.6 Build status (26 Nov 2026): **Verified**
Built and checked in headless Chrome against a synthetic file (no client data): empty page, file chosen,
progress, failed run, wrong file type, results on every tab, Mark as done (survives reopening the run),
history grouped by file with a diff line. No horizontal page scroll at 375 px or 1440 px (tables scroll
inside their own box). All colour pairs measured at 4.5:1 or better. Fonts load from `static/fonts/`.

Differences from the mockup, on purpose:
- No target picker, package option or package download (decision B), no Claude's notes (decision C).
  The hero diagram shows the three targets without a selected state.
- 8 steps, not 9 (no "Plan the Incorta build").
- "Incorta MVs" stat is "target views".
- A fifth tab, Insight SQL, keeps today's reference SQL preview.
- Highlights travel as character offsets (`dax_marks`, `sql_marks`), not `«…»` inside the text, so DAX that
  contains those characters can't break the page. DAX highlights are approximate (the function names and
  references a review reason names); SQL highlights are exact (`NULL /* REVIEW: … */`).
- Group names that are DAX functions (`VALUES`) show in code font; descriptive ones ("Time intelligence")
  don't, per the type rules.
- A measure with several blockers, or no draft SQL, lists its reasons above the DAX/SQL split.
- Old runs (made before this build) have no review groups: the To review tab says so and points to the workbook.

Still open: §10 Q4 (done state carried to the next run of the same file?) is per run for now.
