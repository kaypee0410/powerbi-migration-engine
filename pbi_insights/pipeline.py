"""End-to-end pipeline: PBIX -> extraction -> analysis -> (optional data checks) -> Excel workbook + JSON."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .analyze.findings import build_decisions, build_findings
from .analyze.formulas import tool_formulas
from .analyze.ir import as_value
from .analyze.planner import Planner
from .analyze.profile import data_checks
from .analyze.sources import analyze_sources, build_view, column_source, source_dialect
from .analyze.sqlrender import Renderer, column
from .extract.layout import parse_report
from .extract.model import extract_model
from .extract.package import open_package

DIALECTS = {
    "auto": "Same as source", "tsql": "SQL Server (T-SQL)", "spark": "Spark SQL (Incorta MVs, Databricks)",
    "snowflake": "Snowflake", "postgres": "PostgreSQL", "redshift": "Amazon Redshift", "bigquery": "Google BigQuery",
    "mysql": "MySQL", "oracle": "Oracle",
}
STEPS = ["Open file", "Read report layout", "Decode semantic model", "Decode Power Query sources",
         "Translate DAX to SQL", "Plan insights", "Check data", "Write workbook"]

PATTERN_NOTES = {
    "Mixed grain (fact + dimension)": "Mixes a fact-table aggregate with a dimension-table aggregate. On one flat "
                                      "joined dataset the dimension part repeats per fact row: compute each part at "
                                      "its own grain (Insight SQL tab) or use Tableau relationships.",
    "Dimension-grain aggregate": "Aggregates a dimension table: build it on a dataset of that table only, not on "
                                 "the fact join (which would count fact rows).",
    "Filter-independent (ALL)": "Ignores report filters (FILTER(ALL(...))): keep it out of dashboard filter scope "
                                "(Tableau: FIXED LOD).",
    "Time intelligence": "Uses time-intelligence functions: rebuild with a date table (period offsets in SQL).",
}


@dataclass
class Options:
    dialect: str = "auto"
    read_data: bool = False
    view_prefix: str = "vw_"


@dataclass
class Result:
    workbook: Path | None = None
    json_path: Path | None = None
    summary: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    data: dict | None = None


def _jsonable(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        if type(o).__module__.endswith(".ir"):
            return repr(o)
        return {k: _jsonable(v) for k, v in dataclasses.asdict(o).items() if k != "ir"}
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(x) for x in o]
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return repr(o)


def _measure_extras(m: dict) -> None:
    tr = m["translation"]
    notes = list(tr.notes)
    note = next((v for k, v in PATTERN_NOTES.items() if m["pattern"].startswith(k)), None)
    if note:
        notes.append(note)
    if tr.is_alias and m["depends_on"]:
        notes.append(f"Same value as [{m['depends_on'][0]}]: reuse that measure under a new name.")
    notes += [f"REVIEW: {r}" for r in tr.reviews]
    m["notes"] = notes
    if tr.reviews:
        m["formulas"] = {k: "(needs review: see the SQL)" for k in ("tableau", "quicksight", "incorta")}
        return
    f = tool_formulas(tr.ir)
    if m["pattern"] == "Mixed grain (fact + dimension)":
        f["tableau"] += "\n// needs relationships (logical tables), not a flat join"
        f["quicksight"] = "(mixed grain: use a custom SQL dataset from the Insight SQL tab)"
        f["incorta"] = "(mixed grain: build a materialized view from the Insight SQL tab)"
    m["formulas"] = f


def _whole_table_sql(s, dialect: str, naming):
    """Calculated-column aggregate: a subquery over the whole table's view (no filters)."""
    from sqlglot import exp
    from .analyze.sqlrender import table_ref
    inner = Renderer(dialect, col_fn=lambda x: column(x.column)).r(s.expr)
    return exp.Subquery(this=exp.select(inner).from_(table_ref(naming.view(s.table))))


def analyze(path: str | Path, options: Options | None = None, progress=None) -> dict:
    """Run extraction + analysis and return one dictionary with everything the workbook needs."""
    options = options or Options()
    say = progress or (lambda i, msg: None)
    t0 = time.time()
    say(0, f"Opening {Path(path).name}")
    pkg = open_package(path)
    try:
        say(1, "Reading report pages and visuals")
        report = parse_report(pkg)
        say(2, "Decoding the semantic model")
        model = extract_model(pkg)
    finally:
        pkg.close()
    say(3, "Decoding Power Query and native SQL")
    sources = analyze_sources(model)
    src_dialect = source_dialect(sources)
    dialect = src_dialect if options.dialect in ("auto", "", None) else options.dialect
    say(4, "Translating DAX measures and calculated columns to SQL")
    planner = Planner(report, model, dialect=dialect, view_prefix=options.view_prefix)
    say(5, "Planning each insight and generating reference SQL")
    plans = planner.plan_report()
    usage: dict[str, list] = {}
    for p in plans:
        for ln in p.get("lineage", []):
            if ln["kind"] == "Measure" and p["id"] not in usage.setdefault(ln["measure"], []):
                usage[ln["measure"]].append(p["id"])
    measures = planner.measure_rows(usage)
    for m in measures:
        _measure_extras(m)
    model_cols = {(c["table"], c["column"]): c for c in model.get("columns", [])}
    for c in model.get("columns", []):
        if c.get("auto_date"):
            continue
        c["db_source"] = column_source(c["table"], c["column"], model_cols, sources, planner.translator)
        c["target"] = f"{planner.naming.view(c['table'])}.{c['column']}"
        if c.get("dax"):
            tr = planner.translator.calc_column(c["table"], c["column"], inline_calc=True)
            home = c["table"]
            rnd = Renderer(src_dialect, col_fn=lambda x, home=home: column(
                x.column, None if x.table == home else planner.naming.view(x.table)),
                scalar_fn=lambda s: _whole_table_sql(s, src_dialect, planner.naming))
            try:
                c["sql"] = rnd.r(as_value(tr.ir, src_dialect)).sql(dialect=src_dialect) if tr.ir is not None else ""
            except Exception:  # noqa: BLE001
                c["sql"] = ""
            c["sql_status"], c["sql_reviews"], c["explanation"] = tr.status, tr.reviews, tr.explanation
            c["ref_columns"] = planner.translator.calc_column(c["table"], c["column"]).column_refs
    views = [build_view(t, model, sources, planner.translator, planner.naming)
             for t in model.get("tables", []) if not t.get("auto_date")]
    checks = None
    if options.read_data:
        say(6, "Profiling the saved data and test-running the SQL")
        checks = data_checks(model, report, plans, options.view_prefix, progress=lambda msg: say(6, msg))
    findings = build_findings(report, model, sources, measures, plans, planner.graph, checks)
    decisions = build_decisions(findings)
    data_plans = [p for p in plans if p["approach"] != "Static"]
    runs = (checks or {}).get("test_run", {})
    summary = {
        "file": Path(path).name, "file_size_mb": round(Path(path).stat().st_size / 1_048_576, 2),
        "report_format": report.get("format"), "model_source": model.get("source"), "sql_dialect": dialect,
        "source_dialect": src_dialect, "tabs": len(report.get("pages", [])), "visuals": len(report.get("visuals", [])),
        "data_insights": len(data_plans), "static_items": len(plans) - len(data_plans),
        "tables": len([t for t in model.get("tables", []) if not t.get("auto_date")]),
        "auto_date_tables": len([t for t in model.get("tables", []) if t.get("auto_date")]),
        "relationships": len([r for r in model.get("relationships", []) if not r.get("auto_date")]),
        "measures": len(measures), "measures_in_scope": len([m for m in measures if m["scope"] != "Not used"]),
        "measures_translated": len([m for m in measures if m["translation"].status.startswith("Translated")]),
        "measures_review": len([m for m in measures if m["translation"].reviews]),
        "calculated_columns": len([c for c in model.get("columns", []) if c.get("dax") and not c.get("auto_date")]),
        "insights_review": len([p for p in data_plans if p["approach"] == "Needs review"]),
        "findings_high": len([f for f in findings if f["severity"] == "High"]), "findings": len(findings),
        "sql_test_runs": len([r for r in runs.values() if r["status"] == "Runs"]) if runs else None,
        "sql_test_errors": len([r for r in runs.values() if r["status"] == "Error"]) if runs else None,
        "seconds": round(time.time() - t0, 1), "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "version": __version__, "warnings": report.get("warnings", []) + model.get("warnings", []),
    }
    return {"summary": summary, "options": dataclasses.asdict(options), "report": report, "model": model,
            "sources": sources, "measures": measures, "plans": plans, "views": views, "findings": findings,
            "decisions": decisions, "checks": checks, "graph": planner.graph, "naming": planner.naming}


def run(path: str | Path, out_dir: str | Path, options: Options | None = None, progress=None) -> Result:
    from .report.workbook import write_workbook

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda i, msg: None)
    data = analyze(path, options, progress)
    say(7, "Writing the Excel workbook")
    stem = Path(path).stem
    wb_path = out / f"{stem}_migration_inventory.xlsx"
    write_workbook(data, wb_path)
    json_path = out / f"{stem}_inventory.json"
    payload = {k: v for k, v in data.items() if k not in ("graph", "naming")}
    json_path.write_text(json.dumps(_jsonable(payload), indent=1, default=str), encoding="utf-8")
    handle = data["model"].get("_handle")
    data["model"].pop("_handle", None)
    del handle
    return Result(workbook=wb_path, json_path=json_path, summary=data["summary"],
                  warnings=data["summary"].get("warnings", []), data=data)
