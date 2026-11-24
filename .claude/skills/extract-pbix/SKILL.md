---
name: extract-pbix
description: Run the Power BI extraction pipeline on a .pbix, .pbit or .bim file and summarise the migration inventory (tabs, insights, measures to review, high-severity findings). Use when asked to analyse, inventory or plan the migration of a Power BI file.
argument-hint: <path to .pbix> [--dialect spark] [--data]
allowed-tools: Bash(python -m pbi_insights *), Read, Glob
---

# Extract a Power BI file

Arguments: `$ARGUMENTS` (a file path, then any CLI options).

1. Check the file exists (Glob). If no path was given, look in `samples/` and ask which file to use.
2. Run the pipeline:
   ```
   python -m pbi_insights $ARGUMENTS --out outputs
   ```
   Add `--data` when the user wants the saved data profiled and every insight query test-run (slower).
   Add `--dialect <tsql|spark|snowflake|postgres|redshift|bigquery|mysql|oracle>` when they name a target.
3. Read `outputs/<file stem>_inventory.json` (not the .xlsx). Useful keys:
   - `summary`: counts, `measures_review`, `insights_review`, `findings_high`, `sql_test_errors`
   - `findings`: severity, area, finding, detail, recommendation
   - `plans`: one per visual: `id`, `title`, `approach`, `notes`, `reviews`, `sql`
   - `measures`: `name`, `scope`, `pattern`, `translation.status`, `translation.reviews`, `sql`
4. Reply with:
   - one line of scope (tabs, data insights, tables, measures in scope);
   - every High finding, one line each;
   - insights and measures that need review, with the reason;
   - the workbook path, so the user can open it.

Rules:
- Files in `uploads/` and `samples/` are client data. Never paste data rows, and never commit them.
- Do not edit the generated workbook by hand: fix the code and re-run instead.
