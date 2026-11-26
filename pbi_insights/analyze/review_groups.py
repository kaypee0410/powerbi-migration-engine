"""Group the measures that need review by what blocked their translation, for the web page.

The translator records free-text review reasons (see `dax_translate.py`). This module derives a stable
group key from each reason, attaches a short "why" and "fix", and finds the parts of the DAX and the draft
SQL to highlight. It only reads translator output: nothing here changes a translation.
"""
from __future__ import annotations

import re

# key -> (why it blocks translation, what to do instead)
GROUP_TEXT: dict[str, tuple[str, str]] = {
    "VALUES": ("Used as a filter or iterator, which has no direct SQL equivalent.",
               "Usually becomes a join to a distinct list or an IN subquery."),
    "DISTINCT": ("Used as a filter or iterator, which has no direct SQL equivalent.",
                 "Usually becomes a join to a distinct list or an IN subquery."),
    "CALCULATETABLE": ("Builds a filtered table inside the measure.",
                       "Build the table as its own view, then count or join to it."),
    "SUMMARIZE": ("Builds a grouped table inside the measure.",
                  "Group in a subquery or view, then aggregate the result."),
    "ADDCOLUMNS": ("Builds a table with extra columns inside the measure.",
                   "Compute the columns in a subquery or view first."),
    "TREATAS": ("Changes how two tables relate for this one calculation.",
                "Write the join explicitly in the view."),
    "USERELATIONSHIP": ("Switches to an inactive relationship for this one calculation.",
                        "Write the join on the inactive key explicitly in the view."),
    "CROSSFILTER": ("Changes the filter direction of a relationship for this one calculation.",
                    "Write the join explicitly in the view."),
    "RANKX": ("Ranks each row against a whole table, which needs the full result set.",
              "Use RANK() or DENSE_RANK() OVER (ORDER BY ...) in the view or the insight query."),
    "FILTER": ("Returns a filtered table where SQL needs a condition or a value.",
               "Move the condition into a WHERE clause, or filter in a subquery first."),
    "TOPN": ("Keeps the top rows of a table inside the measure.",
             "Use ORDER BY with LIMIT, or ROW_NUMBER() OVER (...) in a subquery."),
    "LOOKUPVALUE": ("Looks up a value in another table without a relationship.",
                    "Join to that table on the lookup keys."),
    "CONCATENATEX": ("Joins text from many rows into one value.",
                     "Use the dialect's string aggregation (STRING_AGG, LISTAGG, GROUP_CONCAT)."),
    "UNION": ("Combines tables inside the measure.", "Build the combined table as its own view with UNION ALL."),
    "EXCEPT": ("Subtracts one table from another inside the measure.", "Use an anti-join (NOT EXISTS) in a view."),
    "INTERSECT": ("Keeps rows found in both tables inside the measure.", "Use an inner join or EXISTS in a view."),
    "CROSSJOIN": ("Builds every combination of two tables inside the measure.", "Use CROSS JOIN in a view."),
    "GENERATE": ("Builds a table row by row inside the measure.", "Use a lateral join or a grouped subquery."),
    "Removes filters (ALL / REMOVEFILTERS)": ("Needs a total at a coarser grain than the row.",
                                              "Use a window function with the right partition, or a separate view "
                                              "for the total."),
    "FILTER(ALL(...)) across tables": ("Ignores filters on one table while aggregating another.",
                                       "Use a separate dataset or a window function."),
    "FILTER over a computed table": ("Filters a table that only exists inside the measure.",
                                     "Materialise the table as a view first, then filter it."),
    "COUNTROWS over a computed table": ("Counts rows of a table that only exists inside the measure.",
                                        "Materialise the computed table as a view first."),
    "Iterator over a grouped table": ("Iterates a table grouped inside the measure.",
                                      "Group in a subquery or view, then aggregate the result."),
    "Nested aggregation": ("Aggregates the result of another aggregation.",
                           "Compute the inner aggregation in a grouped subquery."),
    "Context transition": ("A measure is evaluated once per row, which changes its filter context.",
                           "Group the inner measure in a subquery at the row's grain, then join."),
    "Time intelligence": ("Shifts or accumulates dates through a date table.",
                          "Join to a date table with the period offset in SQL."),
    "Row context": ("A column or RELATED is used where there is no current row.",
                    "Aggregate the column, or move the logic into a calculated column in the view."),
    "IN with a table": ("Compares against a table expression or several columns.",
                        "Use an IN subquery or a join."),
    "Table used as a filter": ("A whole table is passed as a filter.",
                               "Join to that table, or filter on its key columns."),
    "Aggregation over an expression": ("A plain aggregation is given an expression instead of a column.",
                                       "Use the X version (SUMX, AVERAGEX ...) or a calculated column."),
    "Unknown name": ("Refers to a table, column or measure the model doesn't have.",
                     "Check the name in the model; the report may use a field that was removed."),
    "Circular reference": ("The measure refers back to itself.", "Untangle the definitions by hand."),
    "Could not parse": ("The DAX could not be read.", "Check the expression; it may use syntax the parser lacks."),
    "Other": ("No SQL rule for this construct yet.", "Translate by hand; the Measures sheet has the details."),
}

_FN = r"([A-Z][A-Z0-9_.]*)"
# (reason regex, group key or a function of the match) in priority order
_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(r"^time intelligence \((\w+)\)"), "Time intelligence"),
    (re.compile(r"evaluated per row \(context transition\)"), "Context transition"),
    (re.compile(r"^COUNTROWS over a computed table"), "COUNTROWS over a computed table"),
    (re.compile(r"^FILTER over a computed table"), "FILTER over a computed table"),
    (re.compile(r"^FILTER\(ALL\("), "FILTER(ALL(...)) across tables"),
    (re.compile(rf"^{_FN}\(\.\.\.\) removes filters"), "Removes filters (ALL / REMOVEFILTERS)"),
    (re.compile(rf"^{_FN} changes how tables relate"), lambda m: m.group(1)),
    (re.compile(rf"^{_FN} over a grouped or computed table"), "Iterator over a grouped table"),
    (re.compile(rf"^{_FN} with a nested aggregation"), "Nested aggregation"),
    (re.compile(r"^(?:column .* used outside an aggregation|RELATED outside row context|RELATED over an expression)"),
     "Row context"),
    (re.compile(r"^(?:IN against a table expression|multi-column IN)"), "IN with a table"),
    (re.compile(r"^a whole table used as a filter"), "Table used as a filter"),
    (re.compile(rf"^{_FN}\(\.\.\.\) as a filter table"), lambda m: m.group(1)),
    (re.compile(rf"^{_FN} is not translated automatically"), lambda m: m.group(1)),
    (re.compile(rf"^{_FN} used as a value"), lambda m: m.group(1)),
    (re.compile(rf"^{_FN} needs a table and an expression"), lambda m: m.group(1)),
    (re.compile(rf"^{_FN} over an expression"), "Aggregation over an expression"),
    (re.compile(r"^COUNTBLANK over an expression"), "Aggregation over an expression"),
    (re.compile(r"^(?:unknown (?:name|column)|table '?.+'? used where a value is expected)"), "Unknown name"),
    (re.compile(r"^circular"), "Circular reference"),
    (re.compile(r"could not be parsed"), "Could not parse"),
]

# DAX text worth highlighting for a reason: function names, measure refs and Table[Column] refs it mentions
_RE_FN_IN_REASON = re.compile(r"\b([A-Z][A-Z0-9_.]{2,})\b")
_RE_MEASURE_IN_REASON = re.compile(r"measure \[([^\]]+)\]")
_RE_COLUMN_IN_REASON = re.compile(r"column ([^\[\s]+)\[([^\]]+)\]")
_NOT_FUNCTIONS = {"REVIEW", "SQL", "DAX", "GROUP", "BY", "IN", "AND", "OR", "NOT"}
_RE_SQL_REVIEW = re.compile(r"NULL\s*/\*\s*REVIEW:.*?\*/", re.S)


def group_key(reason: str) -> str:
    for rx, key in _RULES:
        m = rx.search(reason)
        if m:
            return key(m) if callable(key) else key
    return "Other"


def _merge(spans: list[tuple[int, int]]) -> list[list[int]]:
    out: list[list[int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def dax_marks(dax: str, reasons: list[str]) -> list[list[int]]:
    """Character spans in the DAX that the reasons point at. Approximate: the translator keeps no source
    positions, so this finds the function names, measure references and columns the reasons name."""
    pats: list[str] = []
    for r in reasons:
        pats += [rf"\b{re.escape(f)}\s*\(" for f in _RE_FN_IN_REASON.findall(r) if f not in _NOT_FUNCTIONS]
        pats += [re.escape(f"[{m}]") for m in _RE_MEASURE_IN_REASON.findall(r)]
        pats += [rf"'?{re.escape(t.strip(chr(39)))}'?\s*\[{re.escape(c)}\]" for t, c in _RE_COLUMN_IN_REASON.findall(r)]
    spans = []
    for p in set(pats):
        for m in re.finditer(p, dax or "", re.I):
            end = m.end() - 1 if m.group().endswith("(") else m.end()   # leave the bracket unmarked
            spans.append((m.start(), end))
    return _merge(spans)


def sql_marks(sql: str) -> list[list[int]]:
    """Spans of the `NULL /* REVIEW: ... */` placeholders the renderer writes for untranslated parts."""
    return [[m.start(), m.end()] for m in _RE_SQL_REVIEW.finditer(sql or "")]


def group_reviews(measures: list[dict]) -> list[dict]:
    """One entry per blocking pattern, most measures first. Each review measure sits in exactly one group:
    the group of its first review reason."""
    groups: dict[str, dict] = {}
    for m in measures:
        reasons = list(m["translation"].reviews)
        if not reasons:
            continue
        key = group_key(reasons[0])
        why, fix = GROUP_TEXT.get(key, GROUP_TEXT["Other"])
        g = groups.setdefault(key, {"pattern": key, "why": why, "fix": fix, "measures": []})
        sql = m.get("sql") or ""
        g["measures"].append({
            "name": m["name"], "table": m.get("table"), "scope": m.get("scope"), "reasons": reasons,
            "dax": m.get("dax") or "", "dax_marks": dax_marks(m.get("dax") or "", reasons),
            "sql": sql, "sql_marks": sql_marks(sql),
        })
    return sorted(groups.values(), key=lambda g: (g["pattern"] == "Other", -len(g["measures"]), g["pattern"]))
