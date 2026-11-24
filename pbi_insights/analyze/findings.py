"""Findings: things in the source report that change how it must be rebuilt, with a recommendation each."""
from __future__ import annotations

import re

SEV_ORDER = {"High": 0, "Medium": 1, "Low": 2, "Info": 3}
LOCAL_SERVERS = {".", "localhost", "(local)", "127.0.0.1"}


def _f(sev, area, title, detail, rec):
    return {"severity": sev, "area": area, "finding": title, "detail": detail, "recommendation": rec}


def build_findings(report: dict, model: dict, sources: dict, measures: list[dict], plans: list[dict], graph,
                   profile: dict | None = None) -> list[dict]:
    out = []
    cols = {(c["table"], c["column"]): c for c in model.get("columns", [])}

    # ---- sources ------------------------------------------------------------------------------------------
    sampled = [t for t, s in sources.items() if s.get("sql") and "TABLESAMPLE (random sample)" in s["sql"]["flags"]]
    if sampled:
        out.append(_f("High", "Source", "Source data is a random sample",
                      f"{', '.join(sampled)} read with TABLESAMPLE: every Power BI refresh returns a different random "
                      f"subset, so its numbers cannot be reproduced from the database.",
                      "Reconcile the new build against the data saved in the PBIX, then load full data."))
    capped = [t for t, s in sources.items() if s.get("sql") and "TOP / LIMIT (row cap)" in s["sql"]["flags"]]
    if capped:
        out.append(_f("Medium", "Source", "Row cap in a source query", f"TOP/LIMIT in: {', '.join(capped)}.",
                      "Confirm whether the cap is intentional before migrating."))
    shifted = [t for t, s in sources.items() if s.get("sql") and "DATEADD (date shifting)" in s["sql"]["flags"]]
    key_math = []
    for t, s in sources.items():
        for p in (s.get("sql") or {}).get("projections", []):
            if re.search(r"(id|key)$", str(p["name"]), re.I) and re.search(r"[+-]\s*\d", p["expression"]):
                key_math.append(f"{t}.{p['name']} = {p['expression']}")
    if shifted or key_math:
        out.append(_f("Medium", "Source", "Dates or keys shifted in source SQL",
                      "; ".join(([f"DATEADD in {', '.join(shifted)}"] if shifted else []) + key_math),
                      "Decide whether this is demo-data aging (drop it) or business logic (keep it in the views)."))
    xdb = [t for t, s in sources.items() if s.get("sql") and "cross-database names" in s["sql"]["flags"]]
    if xdb:
        out.append(_f("Low", "Source", "Cross-database references",
                      f"Three-part names in: {', '.join(xdb)}.", "The target connection needs access to every database named."))
    local = sorted({s["m"]["server"] for s in sources.values() if s.get("m") and str(s["m"].get("server")).lower() in LOCAL_SERVERS})
    if local:
        out.append(_f("Low", "Source", "Local server name",
                      "Connections point to " + ", ".join(f'"{s}"' for s in local) + " (the author's own machine).",
                      "Re-point the connection to the real server in the target tool."))
    files = [f"{t} ({s['m']['connector']})" for t, s in sources.items()
             if s.get("m") and s["m"].get("source_kind") in ("file", "service", "embedded")]
    if files:
        out.append(_f("Medium", "Source", "Non-database sources", ", ".join(files),
                      "Land files and services in database tables first so every BI tool reads the same data."))
    steps = [f"{t}: {', '.join(s['m']['untranslated_steps'])}" for t, s in sources.items()
             if s.get("m") and s["m"].get("untranslated_steps")]
    if steps:
        out.append(_f("Medium", "Source", "Power Query transformations to rebuild", "; ".join(steps),
                      "Rewrite these steps in the source views (SQL); the Target Views tab marks where."))

    # ---- model ----------------------------------------------------------------------------------------------
    auto = [t["name"] for t in model.get("tables", []) if t.get("auto_date")]
    if auto:
        out.append(_f("Low", "Model", "Auto date/time tables", f"{len(auto)} hidden auto date tables.",
                      "Not needed in the target; switch off Auto date/time in the PBIX."))
    calc_tables = [t["name"] for t in model.get("tables", []) if t.get("calculated") and not t.get("auto_date")]
    if calc_tables:
        out.append(_f("Medium", "Model", "DAX calculated tables", ", ".join(calc_tables),
                      "Write each as a SQL view or table."))
    calc_cols = [f"{c['table']}[{c['column']}]" for c in model.get("columns", []) if c.get("dax") and not c.get("auto_date")]
    if calc_cols:
        out.append(_f("Info", "Model", "Calculated columns moved to SQL", ", ".join(calc_cols),
                      "They are computed in the proposed views (Target Views tab)."))
    for r in model.get("relationships", []):
        if r.get("auto_date"):
            continue
        a, b = cols.get((r["from_table"], r["from_column"])), cols.get((r["to_table"], r["to_column"]))
        if a and b and a.get("data_type") != b.get("data_type"):
            calc = " (a DAX calculated column)" if a.get("dax") or b.get("dax") else ""
            out.append(_f("Medium", "Model", "Relationship keys of different types",
                          f"{r['from_table']}[{r['from_column']}] ({a['data_type']}){calc} joins "
                          f"{r['to_table']}[{r['to_column']}] ({b['data_type']}).",
                          "Give both keys the same type in the views (or join on a cleaner key)."))
    if graph.both_direction:
        out.append(_f("High", "Model", "Bidirectional relationships",
                      "; ".join(f"{r['from_table']} <-> {r['to_table']}" for r in graph.both_direction),
                      "Filters flow both ways in Power BI: replicate with EXISTS/semi-joins or tool-specific settings."))
    if graph.inactive:
        out.append(_f("Medium", "Model", "Inactive relationships",
                      "; ".join(f"{r['from_table']}[{r['from_column']}] -> {r['to_table']}[{r['to_column']}]" for r in graph.inactive),
                      "Measures may switch to them with USERELATIONSHIP: build those joins explicitly."))
    if graph.many_to_many:
        out.append(_f("High", "Model", "Many-to-many relationships",
                      "; ".join(f"{r['from_table']} <-> {r['to_table']}" for r in graph.many_to_many),
                      "Joins multiply rows: aggregate before joining, or use a bridge table."))
    years = []
    for m in measures:
        if re.search(r"\b(19|20)\d{2}\b", m.get("dax") or "") and m["scope"] != "Not used":
            years.append(f"[{m['name']}]")
    for c in model.get("columns", []):
        if c.get("dax") and re.search(r"\b(19|20)\d{2}\b", c["dax"]) and not c.get("auto_date"):
            years.append(f"{c['table']}[{c['column']}]")
    if years:
        out.append(_f("Medium", "DAX", "Hard-coded years", ", ".join(years),
                      "Replace with a parameter (e.g. the reporting year) or keep for parity."))
    if model.get("roles"):
        out.append(_f("High", "Security", "Row-level security roles",
                      "; ".join(f"{r['role']}: {r['table']} {r['filter']}" for r in model["roles"]),
                      "Re-implement security in the target tool (Incorta session variables, QuickSight RLS datasets, Tableau user filters)."))
    unused = [m["name"] for m in measures if m["scope"] == "Not used"]
    if unused:
        out.append(_f("Info", "DAX", "Measures not used by any visual", f"{len(unused)} of {len(measures)}: " + ", ".join(unused),
                      "Leave them out of the build; they stay listed in the Measures tab."))
    kpis = [k["measure"] for k in model.get("kpis", [])]
    if kpis:
        out.append(_f("Info", "DAX", "Model KPIs", ", ".join(kpis) + " have KPI goal/status definitions.",
                      "Rebuild as conditional formatting only if a visual shows them."))
    money_counts = [m["name"] for m in measures if "$" in (m.get("format_string") or "")
                    and re.search(r"\b(units|count|qty|quantity)\b", m["name"], re.I)
                    and not re.search(r"price|avg|average|per\b|/|\$|%", m["name"], re.I)]
    if money_counts:
        out.append(_f("Low", "DAX", "Currency format on count measures", ", ".join(money_counts),
                      "Use a number format in the target."))

    # ---- DAX patterns ---------------------------------------------------------------------------------------
    used = [m for m in measures if m["scope"] != "Not used"]
    by_pattern = {}
    for m in used:
        by_pattern.setdefault(m["pattern"], []).append(m["name"])
    review = [m for m in used if m["translation"].reviews]
    if review:
        out.append(_f("High", "DAX", "Measures that need manual translation",
                      "; ".join(f"[{m['name']}]: {m['translation'].reviews[0]}" for m in review),
                      "Write these by hand; the Measures tab shows the partial SQL."))
    if by_pattern.get("Mixed grain (fact + dimension)"):
        out.append(_f("High", "DAX", "Measures mixing fact and dimension grains",
                      ", ".join(by_pattern["Mixed grain (fact + dimension)"]),
                      "On a flat joined dataset the dimension part is repeated per fact row. Use the two-step SQL in "
                      "the Insight SQL tab (or Tableau relationships)."))
    if by_pattern.get("Dimension-grain aggregate"):
        out.append(_f("Medium", "DAX", "Measures computed on a dimension table",
                      ", ".join(by_pattern["Dimension-grain aggregate"]),
                      "Build them on a dataset of that dimension only, never on the fact join."))
    if by_pattern.get("Filter-independent (ALL)"):
        out.append(_f("Medium", "DAX", "Measures that ignore filters", ", ".join(by_pattern["Filter-independent (ALL)"]),
                      "Exclude them from dashboard filters (Tableau: FIXED LOD)."))

    # ---- report ---------------------------------------------------------------------------------------------
    data_plans = [p for p in plans if p["approach"] != "Static"]
    rev_plans = [p for p in data_plans if p["approach"] == "Needs review"]
    if rev_plans:
        out.append(_f("High", "Report", "Insights that need manual work",
                      "; ".join(f"{p['id']}: {p['reviews'][0] if p['reviews'] else 'measure needs review'}" for p in rev_plans),
                      "See the Build Plan tab for each."))
    oneway = [p["id"] for p in data_plans if any("does not reach" in n or "is not related to" in n for n in p["notes"])]
    if oneway:
        out.append(_f("Medium", "Report", "Filters that do not reach every measure", ", ".join(oneway),
                      "Power BI filters flow one way; the reference SQL reproduces this. Check the target tool's filter scope."))
    drills = [p["id"] for p in data_plans if any(n.startswith("Drill-down") for n in p["notes"])]
    if drills:
        out.append(_f("Low", "Report", "Drill-down hierarchies", ", ".join(drills),
                      "Build the top level and add drill-down to the next level."))
    sorted_by = sorted({c["column"] + " by " + c["sort_by"] for c in model.get("columns", [])
                        if c.get("sort_by") and not c.get("auto_date")})
    if sorted_by:
        out.append(_f("Low", "Report", "Sort-by columns", ", ".join(sorted_by),
                      "Carry the sort column into each dataset and sort on it, not alphabetically."))
    geo = sorted({p["id"] for p in data_plans if any("geocoded" in n for n in p["notes"])})
    if geo:
        out.append(_f("Low", "Report", "Map visuals geocode from text", ", ".join(geo),
                      "Confirm the target geocodes these fields, or add latitude/longitude."))
    custom = sorted({f"{v['id']} ({v['visual_type']})" for v in report.get("visuals", []) if v.get("is_custom")})
    if custom:
        out.append(_f("Medium", "Report", "Custom visuals", ", ".join(custom), "Pick the closest native chart in the target."))
    saved_sort = [p["id"] for p in data_plans if any("Saved sort uses" in n for n in p["notes"])]
    if saved_sort:
        out.append(_f("Low", "Report", "Saved sort on a field not in the visual", ", ".join(saved_sort),
                      "Check the live sort order in Power BI Desktop."))
    overrides = [p["id"] for p in data_plans if any("overrides the visual filter" in n for n in p["notes"])]
    if overrides:
        out.append(_f("Medium", "Report", "CALCULATE overrides a visual filter", ", ".join(overrides),
                      "Power BI replaces the filter, SQL intersects it: verify those numbers."))
    slicers = [v["id"] for v in report.get("visuals", []) if v.get("slicer_selection")]
    if slicers:
        out.append(_f("Low", "Report", "Slicers saved with a selection", ", ".join(slicers),
                      "The saved selection filters its page; the reference SQL applies it."))

    # ---- data profile -----------------------------------------------------------------------------------------
    dim_aggs = {t for p in plans for t in (p.get("agg_tables") or []) if graph.is_dimension(t)}
    for f in (profile or {}).get("findings", []):
        if f.get("table") and f["table"] not in dim_aggs:
            continue          # only matters when a measure aggregates that dimension (e.g. a store count)
        out.append({k: v for k, v in f.items() if k != "table"})
    return sorted(out, key=lambda x: SEV_ORDER.get(x["severity"], 9))


def build_decisions(findings: list[dict]) -> list[tuple]:
    titles = {f["finding"] for f in findings}
    out = []
    if "Source data is a random sample" in titles:
        out.append(("Data volume", "Keep the sample for parity, or load full data",
                    "Reconcile on the PBIX data, then load full data"))
    if "Dates or keys shifted in source SQL" in titles:
        out.append(("Date / key shifting", "Keep the offsets or remove them", "Keep only if it is business logic"))
    if "Hard-coded years" in titles:
        out.append(("Hard-coded years", "Keep as is, or parameterise", "Parameterise (reporting year)"))
    if "Row-level security roles" in titles:
        out.append(("Security model", "Rebuild RLS per tool", "Design once, implement per tool"))
    out.append(("Where the logic lives", "SQL views at the source, or in-tool (Incorta MVs / formulas, QuickSight "
                "datasets, Tableau calculations)", "Views at the source, thin tool layer"))
    out.append(("Power BI quirks", "Replicate them for sign-off, or fix them", "Replicate first, then fix"))
    out.append(("First target tool", "Incorta, QuickSight or Tableau", "Your call"))
    return [(f"D{i}", *d) for i, d in enumerate(out, start=1)]
