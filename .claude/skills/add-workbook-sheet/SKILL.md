---
name: add-workbook-sheet
description: Add or change a sheet in the migration workbook (Excel). Use when the user wants new columns, a new tab, or a different layout in the generated .xlsx.
argument-hint: <sheet name> <what it should list>
---

# Add a workbook sheet

Request: `$ARGUMENTS`

1. Find the data. Everything the writer sees comes from `pipeline.analyze()` (keys: `summary`, `report`,
   `model`, `sources`, `measures`, `plans`, `views`, `findings`, `decisions`, `checks`). If the
   new sheet needs something else, compute it in `analyze()` and keep it JSON-safe (it is also exported).
2. In `pbi_insights/report/workbook.py`, add `_<name>_sheet(wb, x)`:
   ```python
   def _example_sheet(wb, x: _Ctx):
       rows = [[...], ...]
       _sheet(wb, "Example", ["Header 1", "Header 2"], rows, widths_in_px,
              wrap={2}, code={3}, nums={4: "#,##0"})
   ```
   `_sheet` handles the header fill, wrapping, code font, frozen header and filters. Always go through
   `_set`/`_sheet`: they strip illegal characters, cap cell length, and stop text from becoming formulas.
3. Register it in `write_workbook()` in the order users should read it.
4. Add the sheet name to `CORE_SHEETS` in `tests/test_pipeline.py` (or test it separately if optional).
5. If the page should preview it, add a slice in `_preview()` in `app.py` and a sheet in `SHEETS` in
   `static/js/app.js`.
6. Update the sheet table in `README.md`. Run `python -m pytest -q`.

House style: column headers in sentence case, one fact per column, IDs like `OV-02` first, long SQL and DAX in
code-font columns, no merged cells outside the Plan sheet.
