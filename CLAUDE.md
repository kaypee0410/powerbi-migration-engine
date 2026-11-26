# PBI Extraction Insights

Local web app + CLI that decodes a Power BI file (.pbix / .pbit / .bim) and writes a migration workbook
(Excel) for rebuilding the report in Incorta, Amazon QuickSight or Tableau. This file is a starter: grow it
as the project grows (decisions, conventions, gotchas).

## Commands (Windows, from the project folder with .venv active)
- Set up: `python -m venv .venv`, `.venv\Scripts\activate`, `pip install -r requirements.txt -r requirements-dev.txt`
- Web app: `python app.py` then open http://127.0.0.1:5000 (or double-click `run.bat`)
- CLI: `python -m pbi_insights samples\MyReport.pbix --out outputs [--data] [--dialect spark]`
- Tests: `python -m pytest -q` (put real files in `samples/` to also run the sample test)

## How it works
`pbi_insights/pipeline.py` runs these steps; each lives in its own module:
1. `extract/package.py` opens the zip. `extract/layout.py` reads pages, visuals, fields, filters, sorts
   (legacy `Report/Layout` and PBIR). Visual IDs are `<page prefix>-<nn>`, e.g. `OV-02`.
2. `extract/model.py` reads the semantic model: VertiPaq via pbixray for .pbix, TMSL JSON for .pbit/.bim.
3. `extract/mquery.py` decodes Power Query M (connector, server, database, native SQL, renames, types);
   `extract/sqllineage.py` parses the native SQL with sqlglot (objects, column lineage, risky patterns).
4. `analyze/dax_parser.py` -> `analyze/dax_translate.py` -> IR (`analyze/ir.py`) -> `analyze/sqlrender.py`.
5. `analyze/planner.py` + `analyze/querygen.py`: one build plan and one reference SQL query per visual.
6. `analyze/sources.py` (target views), `analyze/findings.py`, `analyze/formulas.py` (tool drafts),
   `analyze/profile.py` (optional: profile saved data, test-run every query on SQLite).
7. `report/workbook.py` (the Excel file).

## Conventions
- Correct or flagged, never guessed: anything the translator cannot reproduce exactly becomes a
  `Review(reason)`, and the insight's approach becomes "Needs review".
- Generated SQL targets one view per Power BI table (`vw_<table in snake_case>`), LEFT JOIN from the many side.
- Keep DAX semantics (filter context, BLANK, one-way filters, grain). See the dax-to-sql skill.
- Workbook text is data: write cells through `_set`/`_sheet` (strips illegal characters, blocks formulas).
- Python 3.10+, type hints, no new heavy dependencies without a reason.

## Data handling
- `uploads/` and `samples/` hold client files: never commit them, never paste their data into chats or docs.
- Nothing leaves the machine: there is no AI/Claude API call in the pipeline.

## Claude Code helpers in .claude/
- Skills: `extract-pbix` (run and summarise a file), `dax-to-sql` (extend the translator),
  `add-workbook-sheet` (change the Excel output), `ui-from-mockup` (build the front end from the HTML
  mockups in `design/mockups/`; plan lives in `design/ui-plan.md`).

## Front end
- One page, no build step: `templates/index.html` (sections in `templates/partials/`) +
  `static/css/tokens.css` (all colours, fonts, spacing) + `static/css/style.css` (components, no hex values) +
  `static/js/app.js` (selects elements by ID; keep the IDs in sync when changing the markup).
- The page reads `/api/jobs/<id>` (`_public()` + `_preview()` in `app.py`). Measures to review are grouped by
  `analyze/review_groups.py` (reads translator output only); "Mark as done" is stored in `outputs/<id>/review.json`.
- Design rules (design/ui-plan.md): green = translated / main action, amber = needs review only, red = High only;
  monospace only for real code and object names; sentence case.
- Runs offline: no CDN, web fonts or remote images; vendor assets into `static/`.
- Design mockups go in `design/mockups/` (placeholder data only; `design/mockups/private/` is ignored).
- Agent: `dax-sql-reviewer` (checks generated SQL against DAX semantics).

## Roadmap / open items
- Fine-grain the workbook layout with the team (it follows the Retail Analysis Sample Phase 1 plan).
- Time intelligence (TOTALYTD, SAMEPERIODLASTYEAR, DATEADD) needs a date-table SQL pattern; today it is flagged.
- Thin reports (live connection) have no model inside: export a .pbit or use the model's own .pbix.
- Compare test-run results with Power BI's rendered visuals for sign-off.
