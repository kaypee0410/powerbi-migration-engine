# Power BI Extraction Insights

A local web page (and CLI) that takes a Power BI file and produces the migration workbook: every report tab
and insight, the semantic model, Power Query sources, relationships, and each DAX measure decoded into
generic SQL, with a build plan per insight for Incorta, Amazon QuickSight or Tableau.

It runs on your own computer. Files never leave it.

## Quick start (Windows, PowerShell, Python 3.11+)

```powershell
cd C:\path\to\powerbi-migration-engine
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000, drop a `.pbix` on the page, choose **Create workbook**, then
**Download workbook (.xlsx)**. Next time, double-click `run.bat` (it does all of the above).

If PowerShell blocks `activate`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

## Command line

```powershell
python -m pbi_insights samples\Retail.pbix --out outputs
python -m pbi_insights samples\Retail.pbix --out outputs --data --dialect spark
```

| Option | What it does |
|---|---|
| `--dialect` | SQL dialect for generated SQL: `auto` (same as the source), `tsql`, `spark`, `snowflake`, `postgres`, `redshift`, `bigquery`, `mysql`, `oracle` |
| `--data` | Reads the data saved in the .pbix: profiles columns and test-runs every insight query (SQLite) |
| `--view-prefix` | Prefix for target view names (default `vw_`) |

Each run writes `<name>_migration_inventory.xlsx` and `<name>_inventory.json` (everything, machine-readable).

## What the workbook contains

| Sheet | Contents |
|---|---|
| Plan | File summary, how each insight gets built, migration steps, findings that change the build, open decisions |
| Tabs | Each report tab: purpose, insight counts, slicers, filter-pane fields, applied page filters |
| Insights | Each visual with an ID (`OV-02`): chart type, dimensions, measures, filters, sort, business question, approach |
| Insight Lineage | For every field on every insight: DAX or column, model base columns, database source, target column / SQL |
| Sources | Each table: role, rows, connector, server, database, source objects, business rules in the native SQL, Power Query steps, M code |
| Columns | Every column: where it is used, type, database source, target column, DAX and SQL for calculated columns |
| Relationships | Cardinality, filter direction, the target `LEFT JOIN`, notes (type mismatches, bidirectional, inactive) |
| Measures | DAX, plain-English logic, generic SQL, pattern, complexity, dependencies, draft formulas for Incorta / QuickSight / Tableau |
| Build Plan | Per insight: approach, grain, dataset, measure logic, filters, sort, how to build it in each tool |
| Target Views | One `CREATE VIEW` per table: native SQL + Power Query renames/types + calculated columns as SQL |
| Insight SQL | One reference query per insight (in your dialect) and, with `--data`, whether it ran and how many rows it returned |
| Data Profile | With `--data`: distinct values, nulls, min and max per column |

## How the SQL stays faithful to Power BI

The generated SQL follows DAX semantics rather than a literal rewrite:

- CALCULATE filters are pushed into the aggregate (`SUM(CASE WHEN ... THEN ... END)`) and replace visual
  filters on the same column; `FILTER(ALL(...))` becomes a fixed subquery that ignores the visual's filters.
- Filters flow one way (from dimension to fact), joins are `LEFT JOIN`s from the many side.
- Measures that mix grains (for example sales per square foot) are computed per table in CTEs and joined on
  the shared dimensions, so nothing is double counted.
- BLANK arithmetic, safe division, drill-down levels, sort-by columns and implicit measures are handled.
- Anything it cannot reproduce exactly (time intelligence, context transition, TopN filters) is marked
  **Needs review** with the reason. It never guesses.

On the Retail Analysis Sample all 16 data insights match an independent re-implementation of the DAX logic.

## Limits

- Thin reports (live connection to a published model) carry no model: export a `.pbit`, or use the model's own `.pbix`.
- DirectQuery files have no saved data, so `--data` profiles nothing.
- Time-intelligence measures are flagged for review, not translated.
- Custom visuals are listed; you pick the closest native chart.

## Project layout

```
app.py                  Flask server (upload, background jobs, downloads)
templates/index.html    The page
static/css, static/js   Page style and behaviour
pbi_insights/
  extract/              PBIX package, report layout, semantic model, Power Query, native SQL lineage
  analyze/              DAX parser and translator, SQL rendering, query builder, planner, findings, views
  report/workbook.py    The Excel writer
  pipeline.py, cli.py   Orchestration and command line
tests/                  Unit and end-to-end tests (synthetic .pbit, no client files needed)
samples/, uploads/, outputs/   Your files (ignored by git)
.claude/                Claude Code settings, skills and a reviewer agent
CLAUDE.md               Project notes for Claude Code
```

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

Drop a real `.pbix` into `samples/` and the sample test also runs it end to end with `--data`.

## Working with Claude Code

Open the folder in VS Code and start `claude` in the terminal. It reads `CLAUDE.md`, and you can use:

- `/extract-pbix samples\MyReport.pbix --data`: run a file and get a summary of what needs attention
- `/dax-to-sql <function or measure>`: extend the translator for a new DAX pattern
- `/add-workbook-sheet <sheet> <contents>`: change the Excel output
- the `dax-sql-reviewer` agent: checks generated SQL against DAX semantics

## Put it in your own GitHub repo

```powershell
git init
git add .
git commit -m "Initial commit: Power BI Extraction Insights"
git branch -M main
git remote add origin https://github.com/kaypee0410/powerbi-migration-engine.git
git push -u origin main
```

Create the empty repository on GitHub first (no README, so the first push is clean). `.gitignore` already
keeps `.env`, `.venv`, `uploads/`, `outputs/` and `samples/` out of the repository.
