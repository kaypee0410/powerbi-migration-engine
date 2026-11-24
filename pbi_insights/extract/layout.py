"""Report layout parser: pages (tabs), visuals (insights), field wells, titles, filters and sorts.

Supports the legacy single-file layout (Report/Layout) and the newer PBIR folder format
(Report/definition/pages/<page>/visuals/<visual>/visual.json). Both describe fields with the
same "semantic query" expressions ({"Column": ...}, {"Measure": ...}, {"Aggregation": ...}).
"""
from __future__ import annotations

import json
import re

from .package import Package

AGG_FN = {0: "Sum", 1: "Average", 2: "CountDistinct", 3: "Min", 4: "Max", 5: "Count",
          6: "Median", 7: "StdDev", 8: "Variance"}
CMP = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}
SORT_DIR = {1: "ASC", 2: "DESC"}

GENERIC_VISUAL = {
    "card": "KPI card (single value)", "cardVisual": "KPI card (single value)", "multiRowCard": "Multi-row card",
    "map": "Bubble map", "filledMap": "Filled (choropleth) map", "azureMap": "Map", "shapeMap": "Shape map",
    "clusteredColumnChart": "Clustered column chart", "columnChart": "Stacked column chart",
    "hundredPercentStackedColumnChart": "100% stacked column chart",
    "clusteredBarChart": "Clustered bar chart", "barChart": "Stacked bar chart",
    "hundredPercentStackedBarChart": "100% stacked bar chart",
    "pieChart": "Pie chart", "donutChart": "Donut chart", "scatterChart": "Scatter / bubble chart",
    "areaChart": "Area chart", "stackedAreaChart": "Stacked area chart", "lineChart": "Line chart",
    "lineClusteredColumnComboChart": "Line + clustered column combo",
    "lineStackedColumnComboChart": "Line + stacked column combo", "ribbonChart": "Ribbon chart",
    "tableEx": "Table", "pivotTable": "Matrix (pivot)", "slicer": "Filter control (slicer)",
    "advancedSlicerVisual": "Filter control (button slicer)", "listSlicer": "Filter control (list slicer)",
    "textSlicer": "Filter control (text slicer)",
    "gauge": "Gauge", "kpi": "KPI (with trend/target)", "treemap": "Treemap", "waterfallChart": "Waterfall",
    "funnel": "Funnel", "decompositionTreeVisual": "Decomposition tree", "keyDriversVisual": "Key influencers",
    "textbox": "Text box (static)", "image": "Image (static)", "shape": "Shape (static)",
    "basicShape": "Shape (static)", "actionButton": "Button (navigation)", "pageNavigator": "Page navigator",
    "bookmarkNavigator": "Bookmark navigator", "visualGroup": "Visual group",
}
DECORATIVE = {"textbox", "image", "shape", "basicShape", "actionButton", "pageNavigator", "bookmarkNavigator",
              "visualGroup"}
SLICERS = {"slicer", "advancedSlicerVisual", "listSlicer", "textSlicer"}


# --------------------------------------------------------------------------- semantic-query helpers
def _src_table(expr: dict | None, aliases: dict) -> str | None:
    sr = (expr or {}).get("SourceRef", {})
    if "Source" in sr:
        return aliases.get(sr["Source"], sr["Source"])
    return sr.get("Entity")


def field_ref(expr: dict, aliases: dict | None = None) -> dict:
    """Semantic-query expression -> {kind, table, field, agg?}."""
    aliases = aliases or {}
    if "Column" in expr:
        c = expr["Column"]
        return {"kind": "Column", "table": _src_table(c.get("Expression"), aliases), "field": c.get("Property")}
    if "Measure" in expr:
        m = expr["Measure"]
        return {"kind": "Measure", "table": _src_table(m.get("Expression"), aliases), "field": m.get("Property")}
    if "Aggregation" in expr:
        a = expr["Aggregation"]
        inner = dict(field_ref(a.get("Expression", {}), aliases))
        inner["agg"] = AGG_FN.get(a.get("Function"), str(a.get("Function")))
        inner["kind"] = "Column"
        inner["implicit"] = True
        return inner
    if "HierarchyLevel" in expr:
        h = expr["HierarchyLevel"]
        hier = (h.get("Expression") or {}).get("Hierarchy", {})
        base = hier.get("Expression", {})
        if "PropertyVariationSource" in base:
            pvs = base["PropertyVariationSource"]
            return {"kind": "Column", "table": _src_table(pvs.get("Expression"), aliases),
                    "field": pvs.get("Property"), "date_level": h.get("Level"), "hierarchy": hier.get("Hierarchy")}
        return {"kind": "Column", "table": _src_table(base, aliases), "field": h.get("Level"),
                "hierarchy": hier.get("Hierarchy")}
    return {"kind": "Other", "table": None, "field": json.dumps(expr)[:120]}


def ref_text(r: dict) -> str:
    s = f"{r.get('table')}[{r.get('field')}]"
    return f"{r['agg']}({s})" if r.get("agg") else s


def _literal(v: str | None):
    """Semantic-query literal -> (display text, python value, type)."""
    if v is None:
        return "NULL", None, "null"
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v, v[1:-1].replace("''", "'"), "str"
    m = re.match(r"^datetime'(.+)'$", v)
    if m:
        return f"TIMESTAMP '{m.group(1).replace('T', ' ')}'", m.group(1), "datetime"
    m = re.match(r"^(-?\d+)L$", v)
    if m:
        return m.group(1), int(m.group(1)), "int"
    m = re.match(r"^(-?\d+(?:\.\d+)?(?:E[+-]?\d+)?)[DM]$", v, re.I)
    if m:
        return m.group(1), float(m.group(1)), "float"
    if v in ("true", "false"):
        return v.upper(), v == "true", "bool"
    if v == "null":
        return "NULL", None, "null"
    return v, v, "raw"


def _operand(e: dict, aliases: dict) -> tuple[str, dict]:
    """Operand of a condition -> (text, normalized node)."""
    if "Literal" in e:
        text, value, typ = _literal(e["Literal"].get("Value"))
        return text, {"lit": value, "type": typ}
    r = field_ref(e, aliases)
    return ref_text(r), {"field": [r.get("table"), r.get("field")], "kind": r.get("kind"), "agg": r.get("agg")}


def condition(c: dict, aliases: dict) -> tuple[str, dict]:
    """Filter condition -> (readable text, normalized tree used later to build SQL)."""
    if "Comparison" in c:
        cc = c["Comparison"]
        lt, ln = _operand(cc["Left"], aliases)
        rt, rn = _operand(cc["Right"], aliases)
        op = CMP.get(cc.get("ComparisonKind"), "=")
        return f"{lt} {op} {rt}", {"op": "cmp", "cmp": op, "left": ln, "right": rn}
    if "In" in c:
        i = c["In"]
        cols = [_operand(x, aliases) for x in i.get("Expressions", [])]
        rows = [[_operand(x, aliases) for x in row] for row in i.get("Values", [])]
        if len(cols) == 1:
            text = f"{cols[0][0]} IN ({', '.join(r[0][0] for r in rows)})"
            return text, {"op": "in", "left": cols[0][1], "values": [r[0][1] for r in rows]}
        text = f"({', '.join(x[0] for x in cols)}) IN ({', '.join('(' + ', '.join(v[0] for v in r) + ')' for r in rows)})"
        return text, {"op": "unparsed", "raw": text}
    if "Not" in c:
        t, n = condition(c["Not"]["Expression"], aliases)
        return f"NOT ({t})", {"op": "not", "x": n}
    for key, op in (("And", "and"), ("Or", "or")):
        if key in c:
            lt, ln = condition(c[key]["Left"], aliases)
            rt, rn = condition(c[key]["Right"], aliases)
            return f"({lt} {op.upper()} {rt})", {"op": op, "left": ln, "right": rn}
    for key, op in (("Contains", "contains"), ("StartsWith", "startswith"), ("EndsWith", "endswith")):
        if key in c:
            lt, ln = _operand(c[key]["Left"], aliases)
            rt, rn = _operand(c[key]["Right"], aliases)
            return f"{lt} {op.upper()} {rt}", {"op": op, "left": ln, "right": rn}
    if "Between" in c:
        b = c["Between"]
        xt, xn = _operand(b["Expression"], aliases)
        lt, ln = _operand(b["LowerBound"], aliases)
        ut, un = _operand(b["UpperBound"], aliases)
        return f"{xt} BETWEEN {lt} AND {ut}", {"op": "between", "x": xn, "low": ln, "high": un}
    raw = json.dumps(c)[:300]
    return "UNPARSED: " + raw, {"op": "unparsed", "raw": raw}


def parse_filters(filters, level: str) -> list[dict]:
    out = []
    for f in filters or []:
        expr = f.get("expression") or f.get("field")
        fld = field_ref(expr) if expr else {"table": None, "field": None, "kind": None}
        flt = f.get("filter")
        texts, trees = [], []
        if flt:
            aliases = {x["Name"]: x.get("Entity") for x in flt.get("From", []) if "Name" in x}
            for w in flt.get("Where", []):
                t, n = condition(w.get("Condition", {}), aliases)
                texts.append(t)
                trees.append(n)
        out.append({
            "level": level, "table": fld.get("table"), "field": fld.get("field"), "field_kind": fld.get("kind"),
            "filter_type": f.get("type"), "condition": " AND ".join(texts) if texts else None,
            "condition_tree": trees, "applied": bool(texts),
            "hidden_in_view": bool(f.get("isHiddenInViewMode")), "locked": bool(f.get("isLockedInViewMode")),
        })
    return out


def _jload(s, default=None):
    if s is None:
        return default
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _reduction_text(binding) -> str | None:
    dr = (binding or {}).get("DataReduction") or {}
    parts = []
    for key in ("Primary", "Secondary"):
        v = dr.get(key)
        if v:
            kind = next(iter(v))
            cnt = (v.get(kind) or {}).get("Count") if isinstance(v.get(kind), dict) else None
            parts.append(f"{key}: {kind}{' ' + str(cnt) if cnt else ''}")
    return "; ".join(parts) or None


def _textbox_text(objects: dict) -> str:
    paras = (((objects or {}).get("general") or [{}])[0].get("properties") or {}).get("paragraphs", [])
    return " / ".join("".join(r.get("value", "") for r in p.get("textRuns", [])) for p in paras).strip()


def _literal_prop(holder) -> str | None:
    if not holder:
        return None
    v = ((((holder[0].get("properties") or {}).get("text") or {}).get("expr") or {})
         .get("Literal", {}).get("Value"))
    return v.strip("'") if isinstance(v, str) else None


def _explicit_title(sv: dict) -> str | None:
    t = sv.get("title")
    if isinstance(t, dict) and t.get("text"):
        return t["text"]
    return (_literal_prop((sv.get("vcObjects") or {}).get("title"))
            or _literal_prop((sv.get("visualContainerObjects") or {}).get("title"))
            or _literal_prop((sv.get("objects") or {}).get("title")))


def _auto_title(vtype: str, fields: list[dict]) -> str:
    vals, cats = [], []
    for f in fields:
        name = f["display_name"]
        if f["role"] in ("Y", "X", "Size", "Values", "Y2", "Tooltips") and f["kind"] == "Measure":
            if name not in vals and f["role"] != "Tooltips":
                vals.append(name)
        elif f["role"] in ("Category", "Series", "Details", "Rows", "Columns", "Location", "Values", "Legend", "Axis"):
            if f.get("active") is False and any(x["role"] == f["role"] and x.get("active") for x in fields):
                continue
            if name not in cats:
                cats.append(name)
    if vtype in SLICERS:
        return ", ".join(cats)

    def join(xs):
        return ", ".join(xs[:-1]) + (" and " if len(xs) > 1 else "") + xs[-1] if xs else ""

    if not cats:
        return join(vals)
    return f"{join(vals)} by {join(cats)}" if vals else join(cats)


def _slicer_mode(objects: dict) -> str | None:
    v = ((((objects or {}).get("data") or [{}])[0].get("properties") or {}).get("mode", {})
         .get("expr", {}).get("Literal", {}).get("Value"))
    return v.strip("'") if isinstance(v, str) else None


def _slicer_selection(objects: dict, fields: list[dict]) -> dict | None:
    """A slicer saved with a selection filters its page; return that selection as a filter."""
    props = (((objects or {}).get("general") or [{}])[0].get("properties") or {})
    flt = (props.get("filter") or {}).get("filter") if isinstance(props.get("filter"), dict) else None
    if not flt or not flt.get("Where"):
        return None
    aliases = {x["Name"]: x.get("Entity") for x in flt.get("From", []) if "Name" in x}
    texts, trees = [], []
    for w in flt.get("Where", []):
        t, n = condition(w.get("Condition", {}), aliases)
        texts.append(t)
        trees.append(n)
    f0 = fields[0] if fields else {"table": None, "field": None}
    return {"level": "Slicer", "table": f0.get("table"), "field": f0.get("field"), "field_kind": "Column",
            "filter_type": "Categorical", "condition": " AND ".join(texts), "condition_tree": trees, "applied": True,
            "hidden_in_view": False, "locked": False}


def _image_name(objects: dict) -> str | None:
    return (((objects or {}).get("general") or [{}])[0].get("properties", {}).get("imageUrl", {})
            .get("expr", {}).get("ResourcePackageItem", {}).get("ItemName"))


def _finish_visual(v: dict, objects: dict | None = None) -> dict:
    v["slicer_selection"] = _slicer_selection(objects, v["fields"]) if v["visual_type"] in SLICERS else None
    v["generic_type"] = GENERIC_VISUAL.get(v["visual_type"], f"Custom visual ({v['visual_type']})")
    v["decorative"] = v["visual_type"] in DECORATIVE
    v["is_slicer"] = v["visual_type"] in SLICERS
    v["is_custom"] = v["visual_type"] not in GENERIC_VISUAL
    if not v["title"] and v["visual_type"] == "image":
        v["title"] = "Image: " + (v.get("image") or "")
        v["title_source"] = "image"
    if not v["title"] and not v["decorative"]:
        v["title"] = _auto_title(v["visual_type"], v["fields"])
        v["title_source"] = "auto"
    return v


# --------------------------------------------------------------------------- legacy Report/Layout
def parse_legacy(lay: dict) -> dict:
    report_filters = parse_filters(_jload(lay.get("filters"), []), "Report")
    pages, visuals = [], []
    for s in sorted(lay.get("sections", []), key=lambda x: x.get("ordinal", 0)):
        cfg = _jload(s.get("config"), {}) or {}
        page = {"ordinal": s.get("ordinal", 0), "id": s.get("name"), "name": s.get("displayName") or s.get("name"),
                "width": s.get("width"), "height": s.get("height"),
                "filters": parse_filters(_jload(s.get("filters"), []), "Page"),
                "hidden": cfg.get("visibility") == 1}
        containers = []
        for vc in s.get("visualContainers", []):
            c = _jload(vc.get("config"), {}) or {}
            pos = (c.get("layouts") or [{}])[0].get("position", {}) or {}
            containers.append((round(pos.get("y", vc.get("y", 0)) or 0), round(pos.get("x", vc.get("x", 0)) or 0), vc, c, pos))
        containers.sort(key=lambda t: (t[0], t[1]))
        page_visuals = []
        for vc_tuple in containers:
            _, _, vc, c, pos = vc_tuple
            if c.get("singleVisualGroup"):
                continue                       # grouping container, its children are listed separately
            sv = c.get("singleVisual") or {}
            vtype = sv.get("visualType") or "unknown"
            proto = sv.get("prototypeQuery") or {}
            aliases = {f["Name"]: f.get("Entity") for f in proto.get("From", []) if "Name" in f}
            selects = {x.get("Name"): x for x in proto.get("Select", [])}
            dt = _jload(vc.get("dataTransforms"), {}) or {}
            disp = {x.get("queryName"): x for x in dt.get("selects", [])}
            q = _jload(vc.get("query"), {}) or {}
            cmd = ((q.get("Commands") or [{}])[0].get("SemanticQueryDataShapeCommand") or {})
            fields = []
            for role, items in (sv.get("projections") or {}).items():
                for it in items:
                    qref = it.get("queryRef")
                    sel = selects.get(qref)
                    if not sel:
                        continue
                    ref = field_ref({k: v for k, v in sel.items() if k != "Name"}, aliases)
                    d = disp.get(qref, {})
                    fields.append({"role": role, "kind": ref["kind"], "table": ref["table"], "field": ref["field"],
                                   "agg": ref.get("agg"), "implicit": bool(ref.get("implicit")),
                                   "display_name": d.get("displayName") or ref["field"], "format": d.get("format"),
                                   "active": bool(it.get("active"))})
            saved, saved_spec = [], []
            for o in proto.get("OrderBy") or []:
                ref = field_ref(o["Expression"], aliases)
                direction = SORT_DIR.get(o.get("Direction"), "ASC")
                saved.append(f"{ref_text(ref)} {direction}")
                saved_spec.append(dict(ref, direction=direction))
            rendered, rendered_spec = [], []
            q_aliases = {f["Name"]: f.get("Entity") for f in (cmd.get("Query") or {}).get("From", []) if "Name" in f}
            for o in (cmd.get("Query") or {}).get("OrderBy") or []:
                ref = field_ref(o["Expression"], q_aliases)
                direction = SORT_DIR.get(o.get("Direction"), "ASC")
                rendered.append(f"{ref_text(ref)} {direction}")
                rendered_spec.append(dict(ref, direction=direction))
            objects = sv.get("objects") or {}
            title = _explicit_title(sv)
            text = _textbox_text(objects) if vtype == "textbox" else None
            v = {"page": page["name"], "page_ordinal": page["ordinal"], "container": c.get("name"),
                 "visual_type": vtype, "x": round(pos.get("x", 0) or 0), "y": round(pos.get("y", 0) or 0),
                 "w": round(pos.get("width", 0) or 0), "h": round(pos.get("height", 0) or 0),
                 "title": title or text, "title_source": "explicit" if title else ("text box" if text else "auto"),
                 "text": text, "fields": fields, "sort": saved or rendered,
                 "sort_spec": saved_spec or rendered_spec,
                 "rendered_sort": rendered if saved and rendered and saved != rendered else [],
                 "visual_filters": parse_filters(_jload(vc.get("filters"), []), "Visual"),
                 "data_reduction": _reduction_text(cmd.get("Binding")), "slicer_mode": _slicer_mode(objects),
                 "image": _image_name(objects)}
            page_visuals.append(_finish_visual(v, objects))
        for i, v in enumerate(page_visuals, start=1):
            v["seq"] = i
        page["visual_count"] = len(page_visuals)
        page["data_visual_count"] = sum(1 for v in page_visuals if not v["decorative"])
        pages.append(page)
        visuals.extend(page_visuals)
    return {"format": "legacy (Report/Layout)", "pages": pages, "visuals": visuals,
            "report_filters": report_filters, "warnings": []}


# --------------------------------------------------------------------------- PBIR (Report/definition)
def parse_pbir(pkg: Package) -> dict:
    base = "Report/definition/"
    warnings = ["Report uses the PBIR folder format; parsed on a best-effort basis."]
    report_filters = []
    if pkg.exists(base + "report.json"):
        rj = pkg.read_json(base + "report.json")
        report_filters = parse_filters((rj.get("filterConfig") or {}).get("filters"), "Report")
    order = []
    if pkg.exists(base + "pages/pages.json"):
        order = pkg.read_json(base + "pages/pages.json").get("pageOrder", [])
    page_dirs = []
    for n in pkg.files_under(base + "pages/"):
        parts = n.split("/")
        if len(parts) >= 5 and parts[4] == "page.json" and parts[3] not in page_dirs:
            page_dirs.append(parts[3])
    ordered = [p for p in order if p in page_dirs] + [p for p in page_dirs if p not in order]
    pages, visuals = [], []
    for ordinal, pname in enumerate(ordered):
        pj = pkg.read_json(f"{base}pages/{pname}/page.json")
        page = {"ordinal": ordinal, "id": pj.get("name", pname), "name": pj.get("displayName", pname),
                "width": pj.get("width"), "height": pj.get("height"),
                "filters": parse_filters((pj.get("filterConfig") or {}).get("filters"), "Page"),
                "hidden": pj.get("visibility") == "HiddenInViewMode"}
        rows = []
        for n in pkg.files_under(f"{base}pages/{pname}/visuals/"):
            if not n.endswith("/visual.json"):
                continue
            vj = pkg.read_json(n)
            if vj.get("visualGroup"):
                continue
            sv = vj.get("visual") or {}
            pos = vj.get("position") or {}
            vtype = sv.get("visualType") or "unknown"
            query = sv.get("query") or {}
            fields = []
            for role, rdata in (query.get("queryState") or {}).items():
                for pr in (rdata or {}).get("projections", []):
                    ref = field_ref(pr.get("field", {}))
                    fields.append({"role": role, "kind": ref["kind"], "table": ref["table"], "field": ref["field"],
                                   "agg": ref.get("agg"), "implicit": bool(ref.get("implicit")),
                                   "display_name": pr.get("displayName") or ref["field"], "format": None,
                                   "active": bool(pr.get("active"))})
            sorts, sort_spec = [], []
            for srt in ((query.get("sortDefinition") or {}).get("sort") or []):
                direction = "DESC" if str(srt.get("direction", "Ascending")).lower().startswith("desc") else "ASC"
                ref = field_ref(srt.get("field", {}))
                sorts.append(f"{ref_text(ref)} {direction}")
                sort_spec.append(dict(ref, direction=direction))
            objects = sv.get("objects") or {}
            title = _explicit_title(sv)
            text = _textbox_text(objects) if vtype == "textbox" else None
            rows.append({"page": page["name"], "page_ordinal": ordinal, "container": vj.get("name"),
                         "visual_type": vtype, "x": round(pos.get("x", 0) or 0), "y": round(pos.get("y", 0) or 0),
                         "w": round(pos.get("width", 0) or 0), "h": round(pos.get("height", 0) or 0),
                         "title": title or text, "title_source": "explicit" if title else ("text box" if text else "auto"),
                         "text": text, "fields": fields, "sort": sorts, "sort_spec": sort_spec, "rendered_sort": [],
                         "visual_filters": parse_filters((vj.get("filterConfig") or {}).get("filters"), "Visual"),
                         "data_reduction": None, "slicer_mode": _slicer_mode(objects), "image": _image_name(objects),
                         "_objects": objects})
        rows.sort(key=lambda v: (v["y"], v["x"]))
        for i, v in enumerate(rows, start=1):
            v["seq"] = i
            visuals.append(_finish_visual(v, v.pop("_objects", None)))
        page["visual_count"] = len(rows)
        page["data_visual_count"] = sum(1 for v in rows if not v["decorative"])
        pages.append(page)
    return {"format": "PBIR (Report/definition)", "pages": pages, "visuals": visuals,
            "report_filters": report_filters, "warnings": warnings}


def page_prefixes(pages: list[dict]) -> dict:
    """Short, unique insight-ID prefixes per page: 'District Monthly Sales' -> DMS, 'Overview' -> OV."""
    out, used = {}, set()
    for p in pages:
        words = re.findall(r"[A-Za-z0-9]+", p["name"] or "") or ["P"]
        base = "".join(w[0] for w in words[:4]).upper() if len(words) > 1 else words[0][:2].upper()
        cand, n = base, 2
        while cand in used:
            cand = f"{base}{n}"
            n += 1
        used.add(cand)
        out[p["name"]] = cand
    return out


def parse_report(pkg: Package) -> dict:
    if pkg.has_legacy_layout:
        rep = parse_legacy(pkg.read_json("Report/Layout"))
    elif pkg.has_pbir:
        rep = parse_pbir(pkg)
    else:
        return {"format": None, "pages": [], "visuals": [], "report_filters": [],
                "warnings": ["No report pages found: this file holds a model only."]}
    prefixes = page_prefixes(rep["pages"])
    for p in rep["pages"]:
        p["prefix"] = prefixes[p["name"]]
    for v in rep["visuals"]:
        v["id"] = f"{prefixes[v['page']]}-{v['seq']:02d}"
    return rep
