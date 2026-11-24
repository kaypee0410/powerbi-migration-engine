"""Optional data checks on the data saved inside the PBIX (import mode only):

* profile: rows, distinct values, nulls, min/max per column of the tables the report uses;
* relationship checks: fact keys missing from a dimension, dimension rows without facts;
* test run: every insight's reference SQL is regenerated for SQLite and executed on the saved data, so the
  workbook can show that each query runs and how many rows it returns.
"""
from __future__ import annotations

import sqlite3
import time

MAX_ROWS = 5_000_000          # tables above this are not loaded
QUERY_SECONDS = 20            # per insight query


def used_tables(model: dict, plans: list[dict]) -> set[str]:
    out = set()
    for p in plans:
        out |= set(p.get("agg_tables") or [])
        for d in p.get("dims", []):
            out.add(d.table)
        for f in p.get("filters", []):
            out.add(f.table)
    for r in model.get("relationships", []):
        if not r.get("auto_date"):
            out |= {r["from_table"], r["to_table"]}
    return out


def load_frames(model: dict, tables: set[str], progress=None) -> tuple[dict, list[dict]]:
    handle = model.get("_handle")
    frames, info = {}, []
    if handle is None:
        return frames, info
    for t in model.get("tables", []):
        if t.get("auto_date") or t["name"] not in tables:
            continue
        if (t.get("row_count") or 0) > MAX_ROWS:
            info.append({"table": t["name"], "rows": t.get("row_count"), "note": f"not loaded (over {MAX_ROWS:,} rows)"})
            continue
        if progress:
            progress(f"Reading {t['name']} data")
        try:
            frames[t["name"]] = handle.get_table(t["name"])
            info.append({"table": t["name"], "rows": len(frames[t["name"]]), "note": ""})
        except Exception as ex:  # noqa: BLE001
            info.append({"table": t["name"], "rows": t.get("row_count"), "note": f"could not read: {ex}"})
    return frames, info


def _is_binary(s) -> bool:
    if s.dtype != object:
        return False
    first = s.dropna().head(1)
    return len(first) > 0 and isinstance(first.iloc[0], (bytes, bytearray, memoryview))


def profile_frames(frames: dict, model: dict) -> dict:
    columns, findings = [], []
    for name, df in frames.items():
        for c in df.columns:
            s = df[c]
            if _is_binary(s):
                columns.append({"table": name, "column": c, "distinct": None, "nulls": int(s.isna().sum()),
                                "min": "(binary)", "max": ""})
                continue
            row = {"table": name, "column": c, "distinct": int(s.nunique(dropna=True)), "nulls": int(s.isna().sum()),
                   "min": None, "max": None}
            try:
                nn = s.dropna()
                if len(nn):
                    row["min"], row["max"] = str(nn.min()), str(nn.max())
            except Exception:  # noqa: BLE001 - mixed types
                pass
            columns.append(row)
    for r in model.get("relationships", []):
        if r.get("auto_date") or r["from_table"] not in frames or r["to_table"] not in frames:
            continue
        many, one = frames[r["from_table"]], frames[r["to_table"]]
        if r["from_column"] not in many or r["to_column"] not in one:
            continue
        fk = set(many[r["from_column"]].dropna().astype(str))
        pk = set(one[r["to_column"]].dropna().astype(str))
        orphans, unused = fk - pk, pk - fk
        if orphans:
            findings.append({"severity": "Medium", "area": "Data", "finding": "Fact keys missing from a dimension",
                             "detail": f"{len(orphans):,} {r['from_table']}[{r['from_column']}] values have no "
                                       f"{r['to_table']} row (Power BI shows them as (Blank)).",
                             "recommendation": "Use LEFT JOINs (the reference SQL does) and decide how to label blanks."})
        if unused and len(unused) < len(pk):
            findings.append({"severity": "Info", "area": "Data", "finding": "Dimension rows without facts",
                             "table": r["to_table"],
                             "detail": f"{len(unused):,} of {len(pk):,} {r['to_table']} rows have no "
                                       f"{r['from_table']} rows; they still count in {r['to_table']}-based measures.",
                             "recommendation": "Keep this in mind when reconciling counts and per-unit measures."})
    return {"columns": columns, "findings": findings}


def _sqlite_frame(df):
    out = df.copy()
    for c in out.columns:
        s = out[c]
        if str(s.dtype).startswith("datetime"):
            out[c] = s.dt.strftime("%Y-%m-%d %H:%M:%S")
        elif _is_binary(s):
            out[c] = None            # images etc. are not needed to test the queries
    return out


def test_run(frames: dict, report: dict, model: dict, view_prefix: str = "vw_", progress=None) -> dict:
    """Regenerate every insight query for SQLite and run it on the saved data."""
    from .planner import Planner

    planner = Planner(report, model, dialect="sqlite", view_prefix=view_prefix)
    con = sqlite3.connect(":memory:")
    for name, df in frames.items():
        _sqlite_frame(df).to_sql(planner.naming.view(name), con, index=False)
    results = {}
    for p in planner.plan_report():
        if p["approach"] == "Static" or not p.get("sql"):
            continue
        if progress:
            progress(f"Test-running {p['id']}")
        missing = [t for t in set(p.get("agg_tables") or []) | {d.table for d in p["dims"]} if t not in frames]
        if missing:
            results[p["id"]] = {"status": "Not run", "rows": None, "detail": f"table(s) not loaded: {', '.join(missing)}"}
            continue
        deadline = time.time() + QUERY_SECONDS
        con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10_000)
        try:
            cur = con.execute(p["sql"])
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchall()
            preview = "; ".join(", ".join(f"{c}={_fmt(v)}" for c, v in zip(cols, r)) for r in rows[:3])
            results[p["id"]] = {"status": "Runs", "rows": len(rows), "columns": cols, "detail": preview}
        except Exception as ex:  # noqa: BLE001
            results[p["id"]] = {"status": "Error", "rows": None, "detail": str(ex)[:300]}
        finally:
            con.set_progress_handler(None, 0)
    con.close()
    return results


def _fmt(v):
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def data_checks(model: dict, report: dict, plans: list[dict], view_prefix: str = "vw_", progress=None) -> dict:
    if model.get("_handle") is None:
        return {"tables": [], "columns": [], "findings": [], "test_run": {},
                "note": "No data in this file (template or model-only), so nothing was profiled or test-run."}
    frames, info = load_frames(model, used_tables(model, plans), progress)
    prof = profile_frames(frames, model)
    runs = test_run(frames, report, model, view_prefix, progress)
    return {"tables": info, "columns": prof["columns"], "findings": prof["findings"], "test_run": runs, "note": ""}
