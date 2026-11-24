"""Insight planner: one build plan per visual (approach, grain, joins, measures, filters, sort, SQL,
target-tool notes, business question) plus measure and column usage across the report."""
from __future__ import annotations

from .dax_parser import DaxSyntaxError, parse_dax, walk as ast_walk
from .dax_translate import ModelIndex, Translator, classify_measure
from .graph import RelGraph
from .ir import Agg, Bin, Col, Fn, InList, Lit, Null, Un, cols_in
from .querygen import Dim, FilterSpec, MeasureSpec, Naming, QueryBuilder, SortSpec
from .sqlrender import Renderer, column, snake

IMPLICIT_AGG = {"Sum": ("SUM", False), "Average": ("AVG", False), "Min": ("MIN", False), "Max": ("MAX", False),
                "Count": ("COUNT", False), "CountDistinct": ("COUNT", True), "Median": ("MEDIAN", False)}

TOOL_VISUALS = {
    "card": ("KPI", "KPI", "Text (BAN)"), "cardVisual": ("KPI", "KPI", "Text (BAN)"),
    "multiRowCard": ("KPI (several measures)", "KPI", "Text table"),
    "map": ("Map (bubble)", "Points on map", "Symbol map"), "filledMap": ("Map (filled)", "Filled map", "Filled map"),
    "azureMap": ("Map", "Points on map", "Symbol map"),
    "clusteredColumnChart": ("Column (grouped)", "Vertical bar, clustered", "Side-by-side bars"),
    "columnChart": ("Stacked column", "Vertical stacked bar", "Stacked bars"),
    "hundredPercentStackedColumnChart": ("Percent column", "Vertical 100% stacked bar", "Stacked bars, % of total"),
    "clusteredBarChart": ("Bar (grouped)", "Horizontal bar, clustered", "Side-by-side bars, horizontal"),
    "barChart": ("Stacked bar", "Horizontal stacked bar", "Stacked bars, horizontal"),
    "hundredPercentStackedBarChart": ("Percent bar", "Horizontal 100% stacked bar", "Stacked bars, % of total"),
    "pieChart": ("Pie", "Pie", "Pie"), "donutChart": ("Donut", "Donut", "Pie (donut)"),
    "scatterChart": ("Bubble", "Scatter plot", "Scatter (circles)"),
    "areaChart": ("Area", "Area line", "Area, not stacked"), "stackedAreaChart": ("Stacked area", "Stacked area", "Area, stacked"),
    "lineChart": ("Line", "Line", "Line"),
    "lineClusteredColumnComboChart": ("Combo (column + line)", "Bar and line combo", "Dual axis (bars + line)"),
    "lineStackedColumnComboChart": ("Combo (stacked column + line)", "Bar and line combo", "Dual axis (stacked bars + line)"),
    "tableEx": ("Aggregated table", "Table", "Text table"), "pivotTable": ("Pivot table", "Pivot table", "Crosstab"),
    "slicer": ("Dashboard filter / prompt", "Filter control", "Quick filter"),
    "listSlicer": ("Dashboard filter / prompt", "Filter control", "Quick filter"),
    "advancedSlicerVisual": ("Dashboard filter / prompt", "Filter control", "Quick filter"),
    "textSlicer": ("Dashboard filter (search)", "Filter control (text)", "Quick filter (wildcard)"),
    "gauge": ("Gauge", "Gauge", "Bullet chart"), "kpi": ("KPI", "KPI with comparison", "BAN with trend line"),
    "treemap": ("Treemap", "Tree map", "Treemap"), "waterfallChart": ("Waterfall", "Waterfall", "Waterfall (Gantt bars)"),
    "funnel": ("Funnel", "Funnel", "Funnel (bars)"), "ribbonChart": ("Stacked column", "Vertical stacked bar", "Stacked bars"),
}
ROLE_WORDS = {  # (Incorta, QuickSight, Tableau)
    "Category": ("Grouping", "X axis", "Columns"), "Axis": ("Grouping", "X axis", "Columns"),
    "Rows": ("Grouping", "Rows", "Rows"), "Columns": ("Pivot column", "Columns", "Columns"),
    "Series": ("Coloring", "Color", "Color"), "Legend": ("Coloring", "Color", "Color"),
    "Details": ("Grouping", "Group/Color", "Detail"), "Location": ("Geo", "Geospatial", "Geographic role"),
    "Y": ("Measure", "Value", "Rows"), "Y2": ("Measure (line)", "Lines", "Rows (dual axis)"),
    "X": ("Measure (X)", "X axis", "Columns"), "Size": ("Size", "Size", "Size"), "Values": ("Measure", "Value", "Rows"),
    "Tooltips": ("Tooltip", "Tooltip", "Tooltip"), "Gradient": ("Coloring (gradient)", "Color", "Color"),
}
VALUE_ROLES = {"Y", "Y2", "X", "Size", "Values", "Tooltips", "Gradient", "TargetValue", "MinValue", "MaxValue",
               "Goal", "Indicator", "TrendLine"}
GEO_CATEGORIES = {"PostalCode", "City", "Country", "StateOrProvince", "County", "Continent", "Address", "Place",
                  "Latitude", "Longitude"}


# ------------------------------------------------------------------------------------------ helpers
def measure_dependencies(model: dict, idx: ModelIndex) -> dict:
    """Direct measure -> measures it references (parsed from the DAX)."""
    deps = {}
    for m in model.get("measures", []):
        try:
            ast = parse_dax(m["dax"])
        except DaxSyntaxError:
            deps[m["name"]] = []
            continue
        refs = []
        for n in ast_walk(ast):
            if n[0] == "ref":
                name = idx.measure(n[1])
                if name and name != m["name"] and name not in refs:
                    refs.append(name)
        deps[m["name"]] = refs
    return deps


def closure(name: str, deps: dict, seen=None) -> list[str]:
    seen = seen if seen is not None else set()
    out = []
    for d in deps.get(name, []):
        if d not in seen:
            seen.add(d)
            out.append(d)
            out += closure(d, deps, seen)
    return out


def cond_tree_ir(node: dict, idx: ModelIndex):
    op = node.get("op")

    def operand(o):
        if "lit" in o:
            if o.get("type") == "datetime":
                return Fn("TIMESTAMP_LIT", (Lit(str(o["lit"]).replace("T", " ")),))
            return Null() if o["lit"] is None else Lit(o["lit"])
        if o.get("kind") == "Measure":
            return None
        t, f = (o.get("field") or [None, None])[:2]
        res = idx.column(t, f)
        return Col(*res) if res else None

    if op == "cmp":
        l, r = operand(node["left"]), operand(node["right"])
        return Bin(node["cmp"], l, r) if l is not None and r is not None else None
    if op == "in":
        l = operand(node["left"])
        vals = [operand(v) for v in node.get("values", [])]
        if l is None or any(v is None for v in vals):
            return None
        non_null = tuple(v for v in vals if not isinstance(v, Null))
        out = InList(l, non_null) if non_null else None
        if len(non_null) < len(vals):
            from .ir import IsNull
            out = IsNull(l) if out is None else Bin("OR", out, IsNull(l))
        return out
    if op == "not":
        x = cond_tree_ir(node["x"], idx)
        return Un("NOT", x) if x is not None else None
    if op in ("and", "or"):
        l, r = cond_tree_ir(node["left"], idx), cond_tree_ir(node["right"], idx)
        return Bin(op.upper(), l, r) if l is not None and r is not None else None
    if op in ("contains", "startswith", "endswith"):
        l, r = operand(node["left"]), operand(node["right"])
        name = {"contains": "LIKE_CONTAINS", "startswith": "LIKE_STARTS", "endswith": "LIKE_ENDS"}[op]
        return Fn(name, (l, r)) if l is not None and r is not None else None
    if op == "between":
        x, lo, hi = operand(node["x"]), operand(node["low"]), operand(node["high"])
        if None in (x, lo, hi):
            return None
        return Bin("AND", Bin(">=", x, lo), Bin("<=", x, hi))
    return None


def generic_measure_sql(ir, dialect: str) -> str:
    """A measure's logic as one SQL expression, columns qualified by their table name."""
    rnd = Renderer(dialect, col_fn=lambda c: column(c.column, c.table))

    def scalar(s):
        from sqlglot import exp
        from .sqlrender import table_ref
        e = s.expr
        sub = exp.select(Renderer(dialect, col_fn=lambda c: column(c.column, c.table)).r(e)).from_(table_ref(s.table))
        return exp.Subquery(this=sub)

    rnd.scalar_fn = scalar
    try:
        return rnd.r(ir).sql(dialect=dialect, pretty=False)
    except Exception as ex:  # noqa: BLE001
        return f"-- could not render: {ex}"


def business_question(v: dict, dims: list[str], measures: list[str]) -> str:
    vt = v["visual_type"]
    m = " and ".join(measures[:3]) or "the values"
    d = " and ".join(dims[:3])
    if v.get("is_slicer"):
        return f"Filter the page by {d or 'a field'}."
    if not dims:
        return f"What is the current {m}?"
    if vt in ("lineChart", "areaChart", "stackedAreaChart"):
        return f"How does {m} trend over {d}?"
    if vt in ("pieChart", "donutChart", "treemap", "hundredPercentStackedColumnChart", "hundredPercentStackedBarChart"):
        return f"How is {m} split across {d}?"
    if vt == "scatterChart":
        return f"How do {m} relate across {d}?"
    if vt in ("map", "filledMap", "azureMap", "shapeMap"):
        return f"Where is {m} concentrated by {d}?"
    if vt in ("tableEx", "pivotTable"):
        return f"Detail of {m} by {d}."
    return f"How does {m} compare across {d}?"


# ------------------------------------------------------------------------------------------ planner
class Planner:
    def __init__(self, report: dict, model: dict, dialect: str = "tsql", view_prefix: str = "vw_"):
        self.report, self.model, self.dialect = report, model, dialect
        self.idx = ModelIndex(model)
        self.graph = RelGraph(model.get("relationships", []))
        self.translator = Translator(self.idx, self.graph)
        self.naming = Naming(view_prefix)
        self.qb = QueryBuilder(self.graph, dialect, self.naming)
        self.deps = measure_dependencies(model, self.idx)

    # ---- measures ----------------------------------------------------------------------------------
    def measure_rows(self, usage: dict) -> list[dict]:
        rows = []
        for m in self.model.get("measures", []):
            tr = self.translator.measure(m["name"])
            pattern, complexity = classify_measure(tr, tr.is_alias, self.graph)
            base_cols = sorted({f"{c.table}[{c.column}]" for c in cols_in(tr.ir)}) if tr.ir is not None else []
            direct = usage.get(m["name"], [])
            all_deps = closure(m["name"], self.deps)
            rows.append({**m, "translation": tr, "pattern": pattern, "complexity": complexity,
                         "sql": generic_measure_sql(tr.ir, self.dialect) if tr.ir is not None else "",
                         "base_columns": base_cols, "depends_on": all_deps, "used_directly_in": direct})
        used = {r["name"] for r in rows if r["used_directly_in"]}
        needed = set(used)
        for name in used:
            needed |= set(closure(name, self.deps))
        for r in rows:
            r["scope"] = "Used on report" if r["name"] in used else ("Building block" if r["name"] in needed else "Not used")
            r["used_via"] = sorted(x["name"] for x in rows if x["name"] in used and r["name"] in closure(x["name"], self.deps))
        return rows

    # ---- one visual ----------------------------------------------------------------------------------
    def plan_visual(self, v: dict, page_filters: list, report_filters: list, slicer_filters: list) -> dict:
        base = {"id": v["id"], "page": v["page"], "title": v["title"], "visual_type": v["visual_type"],
                "generic_type": v["generic_type"], "notes": [], "reviews": [], "sql": "", "dims": [], "measures": [],
                "filters": [], "lineage": []}
        tools = TOOL_VISUALS.get(v["visual_type"], ("Closest native chart",) * 3)
        if v["decorative"]:
            label = "Text" if v["visual_type"] == "textbox" else ("Image" if v["visual_type"] == "image" else "Static object")
            return {**base, "approach": "Static", "complexity": "Low", "grain": "-", "dataset": "-", "joins": "",
                    "question": "Static content (no data).", "measures_text": "-", "filters_text": "-", "sort_text": "-",
                    "incorta": f"{label} object", "quicksight": f"{label} block", "tableau": f"{label} object",
                    "sql_form": "none"}
        # fields -> dims / measures, honouring drill hierarchies (only the active level is shown)
        by_role = {}
        for f in v["fields"]:
            by_role.setdefault(f["role"], []).append(f)
        dims, measures, lineage = [], [], []
        for role, fs in by_role.items():
            cols = [f for f in fs if f["kind"] == "Column" and not f.get("agg")]
            if len(cols) > 1 and any(f["active"] for f in cols) and not all(f["active"] for f in cols):
                active = [f for f in cols if f["active"]]
                base["notes"].append(f"Drill-down hierarchy in '{role}': " + " > ".join(f["field"] for f in cols)
                                     + f"; the report opens at {active[0]['field']}.")
                cols = active
            for f in fs:
                if f["kind"] == "Column" and not f.get("agg") and f not in cols:
                    continue
                if f["kind"] == "Measure":
                    name = self.idx.measure(f["field"])
                    if not name:
                        base["reviews"].append(f"measure {f['field']} not found in the model")
                        continue
                    tr = self.translator.measure(name)
                    spec = next((m for m in measures if m.name == name), None)
                    if spec is None:
                        spec = MeasureSpec(name=name, out=snake(f.get("display_name") or name), ir=tr.ir)
                        measures.append(spec)
                    lineage.append({"role": role, "kind": "Measure", "field": f"{f['table']}[{name}]",
                                    "display": f.get("display_name") or name, "measure": name})
                elif f.get("agg"):
                    res = self.idx.column(f["table"], f["field"])
                    func, distinct = IMPLICIT_AGG.get(f["agg"], (None, False))
                    if not res or not func:
                        base["reviews"].append(f"implicit {f['agg']} of {f['table']}[{f['field']}]")
                        continue
                    ir = Agg(func, Col(*res), res[0], distinct, None)
                    disp = f.get("display_name") or f"{f['agg']} of {f['field']}"
                    measures.append(MeasureSpec(name=disp, out=snake(f"{f['agg']}_{res[1]}"), ir=ir))
                    lineage.append({"role": role, "kind": "Implicit measure", "field": f"{f['agg']}({res[0]}[{res[1]}])",
                                    "display": disp, "column": res})
                elif role == "Tooltips":
                    base["notes"].append(f"Tooltip field {f['table']}[{f['field']}] is not part of the query.")
                else:
                    res = self.idx.column(f["table"], f["field"])
                    if not res:
                        base["reviews"].append(f"column {f['table']}[{f['field']}] not found in the model")
                        continue
                    if any(d.table == res[0] and d.column == res[1] for d in dims):
                        continue
                    col = self.idx.columns[res]
                    sort_by = col.get("sort_by")
                    dims.append(Dim(table=res[0], column=res[1], display=f.get("display_name") or res[1], sort_by=sort_by))
                    lineage.append({"role": role, "kind": "Dimension", "field": f"{res[0]}[{res[1]}]",
                                    "display": f.get("display_name") or res[1], "column": res})
                    if sort_by:
                        lineage.append({"role": "Sort key", "kind": "Dimension", "field": f"{res[0]}[{sort_by}]",
                                        "display": sort_by, "column": (res[0], sort_by)})
                        base["notes"].append(f"{res[1]} sorts by {sort_by}: carry {sort_by} into the dataset and sort on it.")
                    if col.get("data_category") in GEO_CATEGORIES:
                        base["notes"].append(f"{res[1]} is geocoded as {col['data_category']}: confirm the target tool "
                                             f"geocodes it (or add latitude/longitude).")
        # filters: visual + page + report + saved slicer selections
        filters, ftexts = [], []
        for f in list(v["visual_filters"]) + list(page_filters) + list(report_filters) + list(slicer_filters):
            if not f.get("applied"):
                continue
            if f.get("field_kind") == "Measure":
                base["reviews"].append(f"{f['level']} filter on a measure ({f['condition']}): apply it after aggregation")
                continue
            if f.get("filter_type") in ("TopN", "RelativeDate", "RelativeTime"):
                base["reviews"].append(f"{f['filter_type']} filter ({f['condition']}) needs manual SQL")
                continue
            irs = [cond_tree_ir(t, self.idx) for t in f.get("condition_tree", [])]
            if not irs or any(x is None for x in irs):
                base["reviews"].append(f"{f['level']} filter not translated: {f['condition']}")
                continue
            ir = irs[0]
            for x in irs[1:]:
                ir = Bin("AND", ir, x)
            res = self.idx.column(f["table"], f["field"]) or (f["table"], f["field"])
            filters.append(FilterSpec(table=res[0], column=res[1], ir=ir, text=f"{f['level']}: {f['condition']}"))
            ftexts.append(f"{f['level']}: {f['condition']}")
            lineage.append({"role": f"{f['level']} filter", "kind": "Filter", "field": f"{res[0]}[{res[1]}]",
                            "display": f["condition"], "column": res})
        # sort
        sorts = []
        for s in v.get("sort_spec", []):
            if s.get("kind") == "Measure" or s.get("agg"):
                target = next((m for m in measures if m.name == self.idx.measure(s.get("field")) or
                               m.name.lower() == str(s.get("field")).lower()), None)
                if target:
                    sorts.append(SortSpec("measure", target, s["direction"] == "DESC"))
                elif s.get("agg"):
                    base["notes"].append(f"Saved sort uses {s.get('agg')}({s.get('table')}[{s.get('field')}]), a field "
                                         f"not on the visual: check the live sort in Power BI Desktop.")
            else:
                target = next((d for d in dims if d.column.lower() == str(s.get("field")).lower()
                               and d.table.lower() == str(s.get("table")).lower()), None)
                if target:
                    sorts.append(SortSpec("dim", target, s["direction"] == "DESC"))
        if v.get("rendered_sort"):
            base["notes"].append(f"Last render was sorted by {', '.join(v['rendered_sort'])}.")
        plan = self.qb.build(dims, measures, filters, sorts)
        base["notes"] += [n for n in plan.notes if n not in base["notes"]]
        base["reviews"] += [r for r in plan.reviews if r not in base["reviews"]]
        # approach
        mtr = [self.translator.measure(m.name) for m in measures if self.idx.measure(m.name)]
        if v.get("is_slicer"):
            approach, complexity = "Filter control", "Low"
        elif base["reviews"] or any(t.reviews for t in mtr):
            approach, complexity = "Needs review", "High"
        elif plan.form == "multi-grain":
            approach, complexity = "Custom SQL dataset (mixed grain)", "High"
        elif plan.form == "constant":
            approach, complexity = "Fixed KPI (ignores filters)", "Medium"
        elif plan.agg_tables and all(self.graph.is_dimension(t) for t in plan.agg_tables):
            approach, complexity = f"Dimension dataset ({', '.join(plan.agg_tables)})", "Medium"
        else:
            approach = "Simple measures"
            complexity = "Medium" if any(classify_measure(t, t.is_alias, self.graph)[1] != "Low" and not t.is_alias
                                         for t in mtr) else "Low"
        dim_names = [d.display for d in dims]
        meas_names = [m.name for m in measures]
        ri, rq, rt = self._tool_roles(v, dims)
        suffix = {"Custom SQL dataset (mixed grain)": (" · build on an MV from the reference SQL",
                                                        " · custom SQL dataset from the reference SQL",
                                                        " · relationships (not joins) or the reference SQL"),
                  "Fixed KPI (ignores filters)": (" · keep dashboard filters off this insight",
                                                  " · exclude this visual from filter scope",
                                                  " · use a FIXED LOD calculation")}.get(approach, ("", "", ""))
        if approach.startswith("Dimension dataset"):
            t = ", ".join(plan.agg_tables)
            suffix = (f" · base table {t}", f" · separate {t}-grain dataset", f" · measure from the {t} logical table")
        return {**base, "approach": approach, "complexity": complexity,
                "grain": " × ".join(dim_names) or "Single value",
                "dataset": " + ".join(dict.fromkeys(plan.tables)) or (", ".join(plan.agg_tables) or "-"),
                "joins": "; ".join(plan.joins), "agg_tables": plan.agg_tables,
                "question": business_question(v, dim_names, meas_names),
                "measures_text": "\n".join(meas_names) or "-", "filters_text": "\n".join(ftexts) or "-",
                "sort_text": ", ".join(v.get("sort") or []) or "Default",
                "incorta": f"{tools[0]}{ri}{suffix[0]}", "quicksight": f"{tools[1]}{rq}{suffix[1]}",
                "tableau": f"{tools[2]}{rt}{suffix[2]}", "sql": plan.sql, "sql_form": plan.form,
                "dims": dims, "measures": measures, "filters": filters, "lineage": lineage}

    def _tool_roles(self, v: dict, dims: list) -> tuple[str, str, str]:
        parts = ([], [], [])
        sort_note = {(d.table, d.column): d.sort_by for d in dims}
        seen = set()
        for f in v["fields"]:
            key = (f["role"], f["field"])
            if key in seen:
                continue
            seen.add(key)
            words = ROLE_WORDS.get(f["role"], (f["role"],) * 3)
            label = f.get("display_name") or f["field"]
            sb = sort_note.get((f["table"], f["field"]))
            if sb:
                label += f" (sort by {sb})"
            for i in range(3):
                parts[i].append(f"{words[i]}: {label}")
        return tuple((" · " + " · ".join(p)) if p else "" for p in parts)

    # ---- whole report ------------------------------------------------------------------------------------
    def plan_report(self) -> list[dict]:
        pages = {p["name"]: p for p in self.report.get("pages", [])}
        slicer_by_page = {}
        for v in self.report.get("visuals", []):
            if v.get("is_slicer") and v.get("slicer_selection"):
                slicer_by_page.setdefault(v["page"], []).append(v["slicer_selection"])
        plans = []
        for v in self.report.get("visuals", []):
            plans.append(self.plan_visual(v, pages.get(v["page"], {}).get("filters", []),
                                          self.report.get("report_filters", []),
                                          [] if v.get("is_slicer") else slicer_by_page.get(v["page"], [])))
        return plans
