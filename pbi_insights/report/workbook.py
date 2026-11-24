"""Excel workbook writer: the migration inventory and build plan, one sheet per topic.

Sheets: Plan, Tabs, Insights, Insight Lineage, Sources, Columns, Relationships, Measures, Build Plan,
Target Views, Insight SQL (+ Data Profile when the data was read).
To add a sheet, see .claude/skills/add-workbook-sheet/SKILL.md.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..analyze.sqlrender import column as sql_column

HEAD_FILL = PatternFill("solid", fgColor="F3F3F3")
TITLE_FONT = Font(bold=True, size=12)
CODE_FONT = Font(name="Consolas", size=9)
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
MAX_CELL = 32_000            # Excel's limit is 32,767 characters per cell

ROLE_BY_TYPE = {
    "map": {"Category": "Location", "Series": "Legend", "Size": "Bubble size"},
    "filledMap": {"Category": "Location", "Series": "Legend"},
    "pieChart": {"Category": "Slices", "Y": "Values"}, "donutChart": {"Category": "Slices", "Y": "Values"},
    "scatterChart": {"Category": "Details", "Series": "Legend", "X": "X axis", "Y": "Y axis", "Size": "Bubble size"},
    "card": {"Values": "Value"}, "slicer": {"Values": "Field"},
    "tableEx": {"Values": "Column"}, "pivotTable": {"Rows": "Rows", "Columns": "Columns", "Values": "Values"},
}
DEFAULT_ROLE = {"Category": "X axis", "Series": "Legend", "Y": "Y axis", "Y2": "Line values", "X": "X axis",
                "Size": "Size", "Values": "Values", "Tooltips": "Tooltip"}

APPROACH_KEY = [
    ("Simple measures", "Aggregates on one fact table joined to its dimensions: one dataset plus tool formulas."),
    ("Dimension dataset", "Measures that aggregate a dimension table (for example store counts): build them on "
                          "that table alone, never on the fact join."),
    ("Custom SQL dataset (mixed grain)", "Measures that mix fact and dimension grains: use the two-step SQL in the "
                                         "Insight SQL tab (an MV in Incorta, custom SQL in QuickSight)."),
    ("Fixed KPI (ignores filters)", "Measures built with FILTER(ALL(...)): a fixed number that dashboard filters "
                                    "must not change."),
    ("Filter control", "Slicers: a dashboard filter or prompt over the distinct values of one field."),
    ("Needs review", "Something could not be translated automatically: see the Build Plan notes."),
    ("Static", "Text boxes, images and shapes: recreate them by hand."),
]
PLAN_STEPS = [
    ("1", "Agree scope", "Confirm which tabs and insights move; leave out measures nothing uses.", "Tabs, Insights,"
     " Measures", "Inventory ready"),
    ("2", "Rebuild the sources", "Create one view per table: the Power Query logic and DAX calculated columns "
     "become SQL.", "Sources, Target Views, Columns", "SQL drafted"),
    ("3", "Model the joins", "Recreate relationships as LEFT JOINs from fact to dimension (filters flow one way, "
     "as in Power BI).", "Relationships", "Joins listed"),
    ("4", "Build the measures", "Rebuild each in-scope measure from its generic SQL or tool formula.", "Measures",
     "SQL and drafts ready"),
    ("5", "Build the insights", "Create each chart from its build plan; use the reference SQL where the approach "
     "says so.", "Build Plan, Insight SQL", "Plans ready"),
    ("6", "Reconcile and sign off", "Compare every insight with Power BI under the same filters and settle the "
     "findings and decisions above.", "Insight SQL, Plan", "To do"),
]


# ------------------------------------------------------------------------------------------ cell helpers
def _clean(x):
    if x is None:
        return ""
    if isinstance(x, (list, tuple, set)):
        x = "\n".join(str(i) for i in x)
    if isinstance(x, bool):
        return "Yes" if x else ""
    if isinstance(x, str):
        x = ILLEGAL_CHARACTERS_RE.sub("", x)
        if len(x) > MAX_CELL:
            x = x[:MAX_CELL] + "\n... (truncated: see the JSON export)"
    return x


def _set(ws, row, col, value, align=TOP, font=None):
    cell = ws.cell(row=row, column=col, value=_clean(value))
    if isinstance(cell.value, str) and cell.value.startswith("="):
        cell.data_type = "s"          # file content is data: never let it become an Excel formula
    cell.alignment = align
    if font:
        cell.font = font
    return cell


def _sheet(wb, name, headers, rows, widths, wrap=(), code=(), nums=None):
    ws = wb.create_sheet(name)
    for c, h in enumerate(headers, start=1):
        cell = _set(ws, 1, c, h)
        cell.fill = HEAD_FILL
        cell.font = Font(bold=True)
    for r, row in enumerate(rows, start=2):
        for c, x in enumerate(row, start=1):
            cell = _set(ws, r, c, x, WRAP if c in wrap or c in code else TOP, CODE_FONT if c in code else None)
            if nums and c in nums and isinstance(cell.value, (int, float)):
                cell.number_format = nums[c]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = round(w / 7, 1)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    return ws


# ------------------------------------------------------------------------------------------ text helpers
class _Ctx:
    def __init__(self, data: dict):
        self.d = data
        self.model, self.report = data["model"], data["report"]
        self.naming, self.graph = data["naming"], data["graph"]
        self.dialect = data["summary"]["sql_dialect"]
        self.cols = {(c["table"], c["column"]): c for c in self.model.get("columns", [])}
        self.meas = {m["name"]: m for m in data["measures"]}
        self.plans = {p["id"]: p for p in data["plans"]}
        self.pages = {p["name"]: p for p in self.report.get("pages", [])}
        self.order = {v["id"]: i for i, v in enumerate(self.report.get("visuals", []))}
        self.runs = (data.get("checks") or {}).get("test_run") or {}
        self.slicers = {}
        for v in self.report.get("visuals", []):
            if v.get("is_slicer"):
                self.slicers.setdefault(v["page"], []).append(v)
        # where each column is used: insight IDs (+ role) and filter panes
        self.col_use: dict[tuple, list] = {}
        for p in data["plans"]:
            for ln in p.get("lineage", []):
                if "column" in ln:
                    tag = p["id"] + (" (filter)" if ln["kind"] == "Filter" else " (sort)" if ln["role"] == "Sort key" else "")
                    lst = self.col_use.setdefault(tuple(ln["column"]), [])
                    if tag not in lst and p["id"] not in lst:
                        lst.append(tag)
        for pg in self.report.get("pages", []):
            for f in pg.get("filters", []):
                if not f.get("condition") and f.get("table"):
                    self.col_use.setdefault((f["table"], f["field"]), []).append(f"filter pane: {pg['name']}")

    def role_label(self, vtype, role):
        return ROLE_BY_TYPE.get(vtype, {}).get(role, DEFAULT_ROLE.get(role, role))

    def sort_by(self, f):
        c = self.cols.get((f.get("table"), f.get("field")))
        return c.get("sort_by") if c else None

    def view_col(self, table, col):
        return sql_column(col, self.naming.view(table)).sql(dialect=self.dialect)

    # ---- insight texts ----
    def dims_text(self, v):
        by_role = {}
        for f in v["fields"]:
            if f["kind"] == "Measure" or f.get("agg"):
                continue
            by_role.setdefault(f["role"], []).append(f)
        parts = []
        for role, fs in by_role.items():
            names = []
            for f in fs:
                n = f"{f['table']}[{f['field']}]"
                if self.sort_by(f):
                    n += f" (sorted by {self.sort_by(f)})"
                names.append(n)
            label = self.role_label(v["visual_type"], role)
            if len(fs) > 1 and any(f.get("active") for f in fs) and not all(f.get("active") for f in fs):
                active = next(f"{f['table']}[{f['field']}]" for f in fs if f.get("active"))
                parts.append(f"{label}: " + " > ".join(names) + f" (drill-down; opens at {active})")
            else:
                parts.append(f"{label}: " + ", ".join(names))
        return "\n".join(parts)

    def measures_text(self, v):
        out = []
        for f in v["fields"]:
            if f["kind"] == "Measure":
                out.append(f"{self.role_label(v['visual_type'], f['role'])}: {f['table']}[{f['field']}]")
            elif f.get("agg"):
                out.append(f"{self.role_label(v['visual_type'], f['role'])}: {f['agg']} of {f['table']}[{f['field']}]")
        return "\n".join(out)

    def filters_text(self, v):
        out = [f"Visual: {f['condition']}" for f in v.get("visual_filters", []) if f.get("condition")]
        out += [f"Page: {f['condition']}" for f in self.pages.get(v["page"], {}).get("filters", []) if f.get("condition")]
        out += [f"Report: {f['condition']}" for f in self.report.get("report_filters", []) if f.get("condition")]
        if not v.get("is_slicer"):
            for s in self.slicers.get(v["page"], []):
                fld = next((f"{f['table']}[{f['field']}]" for f in s["fields"]), "?")
                sel = s.get("slicer_selection")
                out.append(f"Page slicer: {fld}" + (f" = {sel['condition']}" if sel else " (no saved selection)"))
        return "\n".join(out) or "-"

    def sort_text(self, v):
        out = []
        for s in v.get("sort") or []:
            txt = s
            for f in v["fields"]:
                if s.startswith(f"{f['table']}[{f['field']}]") and self.sort_by(f):
                    txt += f" (by {self.sort_by(f)})"
            out.append(txt)
        return "\n".join(out) or "Default"

    def ids(self, tags):
        return ", ".join(sorted(dict.fromkeys(tags), key=lambda t: (t.startswith("filter pane"),
                                                                     self.order.get(t.split(" ")[0], 999))))

    # ---- lineage ----
    def col_lineage(self, table, col):
        c = self.cols.get((table, col))
        if not c:
            return "Column (not in model)", f"{table}[{col}]", "", ""
        src = "\n".join(c.get("db_source") or [])
        if c.get("dax"):
            return (f"DAX column: {c['dax']}", ", ".join(c.get("ref_columns") or []),
                    src + "\n(via DAX calculated column)", f"{c.get('target')}\n= {c.get('sql') or '(see Columns tab)'}")
        return "Column", f"{table}[{col}]", src, c.get("target") or self.view_col(table, col)

    def measure_sources(self, m):
        out = []
        for bc in m.get("base_columns", []):
            t, _, n = bc.partition("[")
            c = self.cols.get((t, n.rstrip("]")))
            for s in (c or {}).get("db_source") or []:
                if s not in out:
                    out.append(s)
        return "\n".join(out)

    def table_role(self, t):
        if t.get("auto_date"):
            return "Auto date/time (hidden)"
        if t.get("calculated"):
            return "DAX calculated table"
        n = t["name"]
        if self.graph.is_fact(n):
            return "Fact"
        if n in self.graph.one_side and n in self.graph.parents:
            return "Dimension (snowflaked)"
        if n in self.graph.one_side:
            return "Dimension"
        if any(n in (r["from_table"], r["to_table"]) for r in self.model.get("relationships", [])):
            return "Related (inactive / many-to-many)"
        return "Disconnected (no relationships)"


# ------------------------------------------------------------------------------------------ sheets
def _plan_sheet(wb, x: _Ctx):
    d, s = x.d, x.d["summary"]
    ws = wb.create_sheet("Plan")
    ws.sheet_view.showGridLines = False
    c0, r = 2, 2
    stem = Path(s["file"]).stem
    _set(ws, r, c0, f"{stem}: Power BI migration inventory and build plan", font=TITLE_FONT)
    tables = [t for t in x.model.get("tables", []) if not t.get("auto_date")]
    roles = {}
    for t in sorted(tables, key=lambda t: ("Fact", "Dimension").index(x.table_role(t).split(" (")[0])
                    if x.table_role(t).split(" (")[0] in ("Fact", "Dimension") else 9):
        roles.setdefault(x.table_role(t).split(" (")[0], []).append(t["name"])
    rels = [r_ for r_ in x.model.get("relationships", []) if not r_.get("auto_date")]
    slicers = sum(1 for v in x.report.get("visuals", []) if v.get("is_slicer"))
    panes = [p["name"] for p in x.report.get("pages", []) if p.get("filters")]
    conns = {}
    for src in d["sources"].values():
        m = src.get("m") or {}
        if m.get("connector"):
            key = f"{m['connector']}({m.get('server') or ''}{', ' + m['database'] if m.get('database') else ''})"
            conns[key] = conns.get(key, 0) + 1
    snapshot = " · ".join(f"{t['name']} {t['row_count']:,}" for t in tables if t.get("row_count") is not None)
    kpis, roles_rls = x.model.get("kpis", []), x.model.get("roles", [])
    summary = [
        ("Source file", f"{s['file']} · {s['file_size_mb']} MB · report format: {s['report_format'] or 'none'} · "
                        f"model read from: {s['model_source'] or 'none'}"),
        ("Report", f"{s['tabs']} tabs · {s['visuals']} visuals: {s['data_insights']} data insights"
                   f"{f' (incl. {slicers} slicers)' if slicers else ''}, {s['static_items']} static text/image items"
                   + (f" · filter panes on {len(panes)} tabs" if panes else "")),
        ("Model", f"{s['tables']} tables (" + ", ".join(f"{len(v)} {k.lower()}" for k, v in roles.items()) + ")"
                  + (f" + {s['auto_date_tables']} auto date tables" if s["auto_date_tables"] else "")
                  + f" · {s['relationships']} relationships · {s['measures']} measures ({s['measures_in_scope']} "
                    f"needed by the report) · {s['calculated_columns']} calculated columns · {len(kpis)} KPIs · "
                  + (f"{len({r_['role'] for r_ in roles_rls})} security roles" if roles_rls else "no row-level security")),
        ("Sources", " · ".join(f"{k}{f' ×{n}' if n > 1 else ''}" for k, n in conns.items()) or "No Power Query sources found"),
        ("Data snapshot", (snapshot + " rows (as last refreshed inside the PBIX)") if snapshot else "No row counts in this file"),
        ("DAX to SQL", f"{s['measures_translated']} of {s['measures']} measures translated automatically"
                       + (f"; {s['measures_review']} need review" if s["measures_review"] else "")
                       + f" · {s['data_insights'] - s['insights_review']} of {s['data_insights']} insights have "
                         f"generated reference SQL ({s['sql_dialect']})"),
    ]
    if s.get("sql_test_runs") is not None:
        summary.append(("SQL test run", f"{s['sql_test_runs']} of {len(x.runs)} insight queries ran on the data saved in "
                                        f"the PBIX ({s['sql_test_errors']} errors). Results are not yet compared with "
                                        f"Power BI's rendered visuals."))
    summary.append(("Generated", f"{s['generated_at']} · PBI Extraction Insights v{s['version']} in {s['seconds']} s"))
    for k, v in summary:
        r += 1
        _set(ws, r, c0, k)
        _set(ws, r, c0 + 1, v, WRAP)
        ws.merge_cells(start_row=r, start_column=c0 + 1, end_row=r, end_column=c0 + 4)
        ws.row_dimensions[r].height = 15 * max(1, len(str(v)) // 120 + 1)

    def block(title, headers, rows, wrap_from=2):
        nonlocal r
        r += 2
        _set(ws, r, c0, title, font=TITLE_FONT)
        r += 1
        for c, h in enumerate(headers, start=c0):
            cell = _set(ws, r, c, h)
            cell.fill = HEAD_FILL
            cell.font = Font(bold=True)
        for row in rows:
            r += 1
            for c, v in enumerate(row, start=c0):
                _set(ws, r, c, v, WRAP if c - c0 + 1 >= wrap_from else TOP)

    by_app = {}
    for p in d["plans"]:
        key = "Dimension dataset" if p["approach"].startswith("Dimension dataset") else p["approach"]
        by_app.setdefault(key, []).append(p["id"])
    block("How each insight gets built", ["Approach", "When it applies", "Insights"],
          [(a, why, ", ".join(by_app.get(a, [])) or "-") for a, why in APPROACH_KEY if by_app.get(a) or a != "Needs review"])
    block("Migration steps", ["Step", "What", "How", "Inventory tab", "Status"], PLAN_STEPS)
    block("Findings that change the build", ["#", "Severity", "Area", "Finding", "Detail", "Recommendation"],
          [(str(i), f["severity"], f["area"], f["finding"], f["detail"], f["recommendation"])
           for i, f in enumerate(d["findings"], start=1)], wrap_from=4)
    block("Open decisions", ["ID", "Decision", "Options", "Recommendation"], d["decisions"])
    if s.get("warnings"):
        block("Warnings", ["#", "Warning"], [(str(i), w) for i, w in enumerate(s["warnings"], start=1)])
    for col, w in zip("ABCDEFG", (24, 190, 120, 170, 300, 420, 360)):
        ws.column_dimensions[col].width = round(w / 7, 1)


def _tabs_sheet(wb, x: _Ctx):
    rows = []
    for p in x.report.get("pages", []):
        vis = [v for v in x.report.get("visuals", []) if v["page"] == p["name"]]
        data_v = [v for v in vis if not v.get("decorative")]
        charts = [v["title"] or v["generic_type"] for v in data_v if not v.get("is_slicer")]
        purpose = "Shows: " + "; ".join(charts[:8]) if charts else "Text and images only"
        pane = [f"{f['table']}[{f['field']}]" for f in p.get("filters", []) if not f.get("condition")]
        applied = [f["condition"] for f in p.get("filters", []) if f.get("condition")]
        slic = [next((f"{f['table']}[{f['field']}]" for f in v["fields"]), "?") for v in data_v if v.get("is_slicer")]
        rows.append([p["ordinal"] + 1, p["name"], p.get("prefix"), purpose, len(data_v), len(vis) - len(data_v),
                     ", ".join(slic) or "-", ", ".join(pane) or "-", "; ".join(applied) or "-",
                     "Yes" if p.get("hidden") else "", f"{p.get('width')} × {p.get('height')}"])
    _sheet(wb, "Tabs", ["Tab #", "Tab", "ID prefix", "Purpose", "Data insights", "Static items", "Slicers",
                        "Filter pane fields (no default selection)", "Applied page filters", "Hidden", "Canvas (px)"],
           rows, [60, 170, 70, 420, 95, 90, 200, 330, 260, 60, 95], wrap={4, 7, 8, 9})


def _insights_sheet(wb, x: _Ctx):
    rows = []
    for v in x.report.get("visuals", []):
        p = x.plans[v["id"]]
        title = v["title"] or (f"Image: {v.get('image') or ''}" if v["visual_type"] == "image" else "")
        static = p["approach"] == "Static"
        rows.append([v["id"], v["page"], title, v["generic_type"], x.dims_text(v) or "-", x.measures_text(v) or "-",
                     "-" if static else x.filters_text(v), "-" if static else x.sort_text(v),
                     p["question"], p["approach"], v["visual_type"], v.get("title_source"),
                     f"{v['x']}, {v['y']} · {v['w']}×{v['h']}", v.get("data_reduction") or "-"])
    _sheet(wb, "Insights", ["Insight ID", "Tab", "Insight (title)", "Chart type", "Dimensions", "Measures", "Filters",
                            "Sort", "Business question", "Build approach", "Power BI visual type", "Title source",
                            "Position (x, y · w×h)", "Power BI row limit"],
           rows, [80, 150, 300, 170, 320, 280, 250, 210, 300, 210, 160, 110, 150, 170], wrap={3, 5, 6, 7, 8, 9})


def _lineage_sheet(wb, x: _Ctx):
    rows = []
    for p in x.d["plans"]:
        if p["approach"] == "Static":
            continue
        for ln in p.get("lineage", []):
            head = [p["id"], p["title"], ln["role"] if ln["role"] in ("Sort key",) or "filter" in ln["role"]
                    else x.role_label(p["visual_type"], ln["role"]), ln["kind"], ln["field"], ln["display"]]
            if ln["kind"] == "Measure":
                m = x.meas.get(ln["measure"], {})
                rows.append(head + [m.get("dax"), ", ".join(m.get("base_columns", [])), x.measure_sources(m), m.get("sql")])
            elif ln["kind"] == "Implicit measure":
                t, c = ln["column"]
                agg = ln["field"].split("(")[0]
                _, base, src, _ = x.col_lineage(t, c)
                rows.append(head + [f"Implicit {agg} (no DAX)", base, src, f"{agg.upper()}({x.view_col(t, c)})"])
            else:
                t, c = ln["column"]
                rows.append(head + list(x.col_lineage(t, c)))
    _sheet(wb, "Insight Lineage", ["Insight ID", "Insight", "Role", "Kind", "Power BI field", "Display name / condition",
                                   "Definition (DAX or column)", "Model base columns", "Database source",
                                   "Target column / SQL"],
           rows, [80, 260, 110, 110, 230, 190, 340, 300, 320, 360], wrap={2, 6, 8, 9, 10}, code={7})


def _sources_sheet(wb, x: _Ctx):
    rows = []
    views = {v["replaces"]: v for v in x.d["views"]}
    for t in x.model.get("tables", []):
        src = x.d["sources"].get(t["name"]) or {}
        m, sq = src.get("m") or {}, src.get("sql") or {}
        keys = sorted({r["to_column"] for r in x.model.get("relationships", []) if r["to_table"] == t["name"]} |
                      {r["from_column"] for r in x.model.get("relationships", []) if r["from_table"] == t["name"]})
        rules = list(sq.get("flags", []))
        if sq.get("where"):
            rules.append(f"WHERE {sq['where']}")
        if sq.get("error"):
            rules.append(f"(SQL not fully parsed: {sq['error']})")
        steps = "\n".join(f"{i}. {s_['name']}: {s_['detail'] or s_['function']}"
                          for i, s_ in enumerate(m.get("steps", []), start=1))
        view = views.get(t["name"], {})
        if t.get("auto_date"):
            rows.append([t["name"], "Auto date/time (hidden)", "", t.get("row_count"), "DAX calculated table", "-", "-",
                         "-", t.get("calc_dax"), "-", "Not migrated",
                         "Created by Power BI's Auto date/time option; use a real date table instead.", "", ""])
            continue
        rows.append([t["name"], x.table_role(t), ", ".join(keys), t.get("row_count"),
                     m.get("connector") or ("DAX calculated table" if t.get("calculated") else ""),
                     m.get("server"), m.get("database"), "\n".join(m.get("source_objects") or sq.get("objects") or []),
                     "\n".join(rules), steps, view.get("view"), "\n".join(view.get("notes") or []),
                     m.get("native_query") or "", t.get("calc_dax") or t.get("m_expression") or ""])
    _sheet(wb, "Sources", ["Table", "Role", "Keys used in relationships", "Rows (snapshot)", "Connector", "Server",
                           "Database", "Source object(s)", "Business rules in the source query",
                           "Power Query applied steps", "Target view", "Notes", "Native SQL (decoded)",
                           "Advanced Editor (M code) / DAX"],
           rows, [180, 170, 200, 100, 140, 90, 110, 230, 400, 330, 120, 300, 460, 520],
           wrap={2, 3, 8, 9, 10, 12}, code={13, 14}, nums={4: "#,##0"})


def _columns_sheet(wb, x: _Ctx):
    in_scope = [m for m in x.d["measures"] if m["scope"] != "Not used"]
    rows = []
    for c in x.model.get("columns", []):
        if c.get("auto_date"):
            continue
        key = f"{c['table']}[{c['column']}]"
        used_by = [m["name"] for m in in_scope if key in m.get("base_columns", [])]
        rows.append([c["table"], c["column"], x.ids(x.col_use.get((c["table"], c["column"]), [])) or "-",
                     c.get("data_type"), c.get("column_type"), "\n".join(c.get("db_source") or []), c.get("target"),
                     c.get("dax") or "", c.get("sql") or "", "Yes" if c.get("is_key") else "",
                     "Yes" if c.get("hidden") else "", c.get("data_category") or "", c.get("sort_by") or "",
                     c.get("summarize_by"), ", ".join(used_by)])
    _sheet(wb, "Columns", ["Table", "Column", "Used in report", "Data type", "Column type", "Database source",
                           "Target column", "DAX (calculated column)", "SQL equivalent", "Key", "Hidden",
                           "Data category", "Sort by", "Default summarization", "Used by measures (in scope)"],
           rows, [110, 180, 260, 120, 120, 320, 260, 280, 330, 50, 60, 120, 110, 130, 300],
           wrap={3, 6, 7, 15}, code={8, 9})


def _relationships_sheet(wb, x: _Ctx):
    rows = []
    card = {("many", "one"): "Many to one (*:1)", ("one", "one"): "One to one (1:1)",
            ("one", "many"): "One to many (1:*)", ("many", "many"): "Many to many (*:*)"}
    for i, r in enumerate(x.model.get("relationships", []), start=1):
        notes = []
        a, b = x.cols.get((r["from_table"], r["from_column"])), x.cols.get((r["to_table"], r["to_column"]))
        if r.get("auto_date"):
            join, notes = "-", ["Auto date/time relationship: not migrated."]
        else:
            many, mcol, one, ocol = (r["to_table"], r["to_column"], r["from_table"], r["from_column"]) \
                if (r.get("from_cardinality"), r.get("to_cardinality")) == ("one", "many") \
                else (r["from_table"], r["from_column"], r["to_table"], r["to_column"])
            join = (f"FROM {x.naming.view(many)}\nLEFT JOIN {x.naming.view(one)}\n  ON {x.view_col(many, mcol)} = "
                    f"{x.view_col(one, ocol)}")
            if a and b and a.get("data_type") != b.get("data_type"):
                notes.append(f"Key types differ ({a['data_type']} vs {b['data_type']}): cast one side in the view.")
            if a and a.get("dax") or b and b.get("dax"):
                notes.append("Joins on a DAX calculated column: compute it in the view first.")
            if r.get("cross_filter") == "Both":
                notes.append("Filters both ways in Power BI: a plain join filters one way only.")
            if not r.get("active", True):
                notes.append("Inactive: only used by measures that call USERELATIONSHIP.")
            if (r.get("from_cardinality"), r.get("to_cardinality")) == ("many", "many"):
                notes.append("Many-to-many: aggregate before joining.")
        rows.append([i, f"{r['from_table']}[{r['from_column']}]",
                     "Auto date table" if r.get("auto_date") else f"{r['to_table']}[{r['to_column']}]",
                     card.get((r.get("from_cardinality"), r.get("to_cardinality")), "?"), r.get("cross_filter"),
                     "Yes" if r.get("active", True) else "No", join, "\n".join(notes)])
    _sheet(wb, "Relationships", ["#", "From", "To", "Cardinality", "Filter direction", "Active", "Target SQL join",
                                 "Notes"],
           rows, [40, 220, 220, 150, 110, 70, 420, 460], wrap={8}, code={7})


def _measures_sheet(wb, x: _Ctx):
    order = {"Used on report": 0, "Building block": 1, "Not used": 2}
    rows = []
    for m in sorted(x.d["measures"], key=lambda m: (order[m["scope"]], -len(m["used_directly_in"]), m["id"])):
        tr = m["translation"]
        used = x.ids(m["used_directly_in"]) or ("via " + ", ".join(m["used_via"]) if m.get("used_via") else "-")
        logic = tr.explanation
        if tr.is_alias and m["depends_on"]:
            dep = x.meas.get(m["depends_on"][0])
            logic = f"Same as [{m['depends_on'][0]}]" + (f": {dep['translation'].explanation}" if dep else "")
        rows.append([m["name"], m["table"], m["scope"], used, m["dax"], logic, m["sql"], m["pattern"], m["complexity"],
                     tr.status, ", ".join(m["depends_on"]) or "-", ", ".join(m["base_columns"]) or "-",
                     m.get("format_string") or "", m["formulas"]["incorta"], m["formulas"]["quicksight"],
                     m["formulas"]["tableau"], "\n".join(m.get("notes") or [])])
    _sheet(wb, "Measures", ["Measure", "Table", "Scope", "Used by insights", "DAX", "Plain-English logic",
                            "Generic SQL (on target views)", "Pattern", "Complexity", "Translation status",
                            "Depends on measures", "Base columns", "Format string", "Incorta (draft)",
                            "QuickSight (draft)", "Tableau (draft)", "Notes"],
           rows, [200, 90, 120, 230, 340, 340, 400, 210, 90, 130, 240, 280, 160, 320, 340, 340, 360],
           wrap={4, 6, 8, 11, 12, 14, 15, 16, 17}, code={5, 7})


def _build_plan_sheet(wb, x: _Ctx):
    rows = []
    for p in x.d["plans"]:
        mlines = []
        for spec in p.get("measures", []):
            m = x.meas.get(spec.name)
            if m:
                mlines.append(f"{spec.name} = {m['sql']}")
            else:
                from ..analyze.planner import generic_measure_sql
                mlines.append(f"{spec.name} = {generic_measure_sql(spec.ir, x.dialect)}")
        notes = list(p.get("notes", [])) + [f"REVIEW: {r}" for r in p.get("reviews", [])]
        if p["approach"] == "Static":
            v = next((v for v in x.report.get("visuals", []) if v["id"] == p["id"]), {})
            notes = [f"Text: {v.get('text')}" if v.get("text") else f"{v.get('visual_type', 'Object')}: "
                     f"{v.get('image') or v.get('title') or ''}"]
        rows.append([p["id"], p["page"], p["title"] or "-", p["approach"], p["grain"], p["dataset"],
                     "\n".join(mlines) or "-", p["filters_text"], p["sort_text"], p["complexity"], p["incorta"],
                     p["quicksight"], p["tableau"], "\n".join(notes)])
    _sheet(wb, "Build Plan", ["Insight ID", "Tab", "Insight", "Approach", "Grain", "Dataset (tables)",
                              "Measures (target logic)", "Filters to apply", "Sort", "Complexity", "Incorta",
                              "QuickSight", "Tableau", "Notes"],
           rows, [80, 150, 280, 210, 190, 220, 420, 260, 220, 90, 330, 330, 330, 380],
           wrap={3, 4, 5, 6, 8, 9, 11, 12, 13, 14}, code={7})


def _views_sheet(wb, x: _Ctx):
    rows = [[v["view"], v["replaces"], v["dialect"], "\n".join(v.get("notes") or []), v["sql"]] for v in x.d["views"]]
    _sheet(wb, "Target Views", ["View", "Replaces (Power BI table)", "SQL dialect", "Notes",
                                "SQL (runs on the source database)"],
           rows, [140, 160, 90, 380, 820], wrap={4}, code={5})


def _insight_sql_sheet(wb, x: _Ctx):
    rows = []
    for p in x.d["plans"]:
        if p["approach"] == "Static":
            continue
        run = x.runs.get(p["id"]) or {}
        rows.append([p["id"], p["title"], p["approach"], p.get("sql_form"), run.get("status") or "Not run",
                     run.get("rows"), run.get("detail") or "", p["sql"]])
    _sheet(wb, "Insight SQL", ["Insight ID", "Insight", "Approach", "Query shape", "Test run on PBIX data",
                               "Result rows", "Sample output / error", f"Reference SQL ({x.dialect}, on the target views)"],
           rows, [80, 280, 210, 100, 120, 90, 360, 820], wrap={2, 3, 7}, code={8}, nums={6: "#,##0"})


def _profile_sheet(wb, x: _Ctx):
    checks = x.d.get("checks") or {}
    rows = [[c["table"], c["column"], c["distinct"], c["nulls"], c["min"], c["max"]] for c in checks.get("columns", [])]
    if not rows:
        rows = [["-", checks.get("note") or "No data profiled", None, None, None, None]]
    _sheet(wb, "Data Profile", ["Table", "Column", "Distinct values", "Nulls", "Min", "Max"],
           rows, [140, 220, 110, 90, 260, 260], nums={3: "#,##0", 4: "#,##0"})


def write_workbook(data: dict, path: str | Path) -> Path:
    x = _Ctx(data)
    wb = Workbook()
    wb.remove(wb.active)
    _plan_sheet(wb, x)
    _tabs_sheet(wb, x)
    _insights_sheet(wb, x)
    _lineage_sheet(wb, x)
    _sources_sheet(wb, x)
    _columns_sheet(wb, x)
    _relationships_sheet(wb, x)
    _measures_sheet(wb, x)
    _build_plan_sheet(wb, x)
    _views_sheet(wb, x)
    _insight_sql_sheet(wb, x)
    if data.get("checks"):
        _profile_sheet(wb, x)
    wb.properties.title = f"{data['summary']['file']} migration inventory"
    wb.properties.creator = "PBI Extraction Insights"
    wb.save(path)
    return Path(path)
