---
name: dax-to-sql
description: Extend or fix the DAX-to-SQL translator (a new DAX function, a wrong translation, a new pattern). Use when a measure shows "Needs review" or its generated SQL disagrees with Power BI.
argument-hint: <DAX function or measure name>
---

# Extend the DAX translator

Target: `$ARGUMENTS`

## How a measure becomes SQL
1. `pbi_insights/analyze/dax_parser.py` parses DAX into tuples, e.g. `("func", "CALCULATE", [args])`,
   `("col", table, column)`, `("ref", measure)`, `("bin", op, left, right)`, `("var", defs, body)`.
2. `pbi_insights/analyze/dax_translate.py` (`Translator`) turns the AST into the IR in `analyze/ir.py`:
   `Agg(func, arg, table, distinct, pred)`, `Case`, `SafeDiv`, `BlankArith`, `Fn(name, args)`, `Scalar`,
   and `Review(reason)` for anything it cannot translate faithfully. Functions dispatch in `Translator.func`.
3. `pbi_insights/analyze/sqlrender.py` (`Renderer`) renders IR with sqlglot for any dialect. New `Fn` names
   need a case in `Renderer.fn`.
4. `pbi_insights/analyze/querygen.py` builds one query per visual: groups aggregates by table, joins along
   `RelGraph`, splits mixed grains into CTEs, applies visual/page/report/slicer filters.
5. `pbi_insights/analyze/formulas.py` renders the same IR as Tableau / QuickSight / Incorta drafts.

## Rules that keep the SQL honest
- Same answer as Power BI or a `Review(...)`: never a plausible-looking guess.
- Keep DAX semantics: BLANK arithmetic (`BlankArith`), `DIVIDE` -> `SafeDiv`, CALCULATE filters replace
  filters on the same column (`ir.override`), filters flow one way (many -> one), `FILTER(ALL(T))` ignores
  the visual's filters (`Scalar`).
- Measure references inside row context (iterators, FILTER conditions) are context transitions: `Review`.
- Use `self.review("reason")` to stop with a reason the user can act on, and append to `self.notes`
  whenever the SQL differs from DAX in an edge case (for example BLANK vs 0). Both end up in the workbook.

## Checklist
1. Add a failing test in `tests/test_dax_translate.py` (unit) and, for visual-level behaviour,
   `tests/test_querygen.py` (runs on SQLite with known numbers).
2. Implement in `Translator.func` (and `Renderer.fn` / `ToolFormula.fn` for new `Fn` names).
3. Update `classify_measure` if it is a new pattern, and `PATTERN_NOTES` in `pipeline.py` if users need advice.
4. `python -m pytest -q`, then run the CLI with `--data` on a sample and check the Insight SQL sheet.
5. Ask the `dax-sql-reviewer` agent to review the change.
