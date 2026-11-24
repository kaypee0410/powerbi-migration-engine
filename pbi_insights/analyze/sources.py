"""Per-table source analysis: decoded Power Query, native SQL lineage, column lineage back to the
database, and a proposed target view that replaces Power Query + DAX calculated columns with SQL."""
from __future__ import annotations

import textwrap

from sqlglot import exp

from ..extract.mquery import output_to_source_name, parameter_values, parse_m
from ..extract.sqllineage import analyze_sql
from .dax_translate import Translator
from .ir import Col
from .querygen import Naming
from .sqlrender import Renderer, column, ident


# Power Query "Changed Type" -> SQL type. Text is left alone (a bare VARCHAR cast truncates in T-SQL).
M_TYPE_CAST = {"int64.type": "BIGINT", "int32.type": "INT", "int16.type": "SMALLINT", "int8.type": "SMALLINT",
               "number.type": "DOUBLE", "type number": "DOUBLE", "double.type": "DOUBLE", "single.type": "DOUBLE",
               "decimal.type": "DOUBLE", "percentage.type": "DOUBLE", "currency.type": "DECIMAL(19, 4)",
               "type datetime": "DATETIME", "datetime.type": "DATETIME", "type date": "DATE", "date.type": "DATE",
               "type logical": "BOOLEAN", "logical.type": "BOOLEAN"}


def analyze_sources(model: dict) -> dict:
    """table name -> {m, sql, back (output->source names), dialect}."""
    params = parameter_values(model.get("expressions", []))
    out = {}
    for t in model.get("tables", []):
        if t.get("auto_date"):
            continue
        m = parse_m(t.get("m_expression") or "", params) if t.get("m_expression") else None
        sql = analyze_sql(m["native_query"], m.get("dialect")) if m and m.get("native_query") else None
        out[t["name"]] = {"m": m, "sql": sql, "back": output_to_source_name(m) if m else {},
                          "dialect": (m or {}).get("dialect") or "tsql"}
    return out


def source_dialect(sources: dict) -> str:
    dialects = [s["dialect"] for s in sources.values() if s.get("m") and s["m"].get("connector")]
    return max(set(dialects), key=dialects.count) if dialects else "tsql"


def column_source(table: str, column_name: str, model_cols: dict, sources: dict, translator: Translator,
                  _seen=None) -> list[str]:
    """Database expression(s) a model column comes from."""
    _seen = _seen or set()
    if (table, column_name) in _seen:
        return []
    _seen.add((table, column_name))
    col = model_cols.get((table, column_name))
    if not col:
        return []
    if col.get("dax"):
        tr = translator.calc_column(table, column_name)
        out = []
        for ref in tr.column_refs:
            t, _, c = ref.partition("[")
            for s in column_source(t, c.rstrip("]"), model_cols, sources, translator, _seen):
                if s not in out:
                    out.append(s)
        return out
    src = sources.get(table) or {}
    m, sql = src.get("m"), src.get("sql")
    out_name = col.get("source_column") or column_name
    sql_name = (src.get("back") or {}).get(out_name, out_name)
    if sql and sql.get("projections"):
        p = next((p for p in sql["projections"] if str(p["name"]).lower() == sql_name.lower()), None)
        if p:
            return [p["qualified"]]
    if sql and sql.get("star_sources"):
        return [f"{sql['star_sources'][0]}.{sql_name}"]
    if m and m.get("source_objects"):
        return [f"{m['source_objects'][-1]}.{sql_name}"]
    if m and m.get("connector"):
        return [f"{m['connector']} ({m.get('server') or ''}) column '{sql_name}'"]
    return [sql_name]


def build_view(table: dict, model: dict, sources: dict, translator: Translator, naming: Naming) -> dict:
    """Proposed CREATE VIEW for one table, in the source system's dialect."""
    name = table["name"]
    src = sources.get(name) or {}
    m, sql = src.get("m") or {}, src.get("sql") or {}
    dialect = src.get("dialect") or "tsql"
    view = naming.view(name)
    notes, comments = [], []
    if table.get("calculated"):
        return {"view": view, "replaces": name, "sql": f"-- {name} is a DAX calculated table: write its SQL by hand.\n"
                f"-- DAX:\n" + textwrap.indent(table.get("calc_dax") or "", "--   "),
                "notes": ["DAX calculated table"], "dialect": dialect}
    cols = [c for c in model.get("columns", []) if c["table"] == name]
    back = src.get("back") or {}
    mtypes = {str(k).lower(): v for k, v in (m.get("types") or [])}

    def src_col(c: Col) -> exp.Expression:
        info = next((x for x in cols if x["column"] == c.column), None)
        if c.table != name or not info:
            e = exp.Null()
            e.add_comments([f" REVIEW: needs a join to {c.table} "])
            return e
        out_name = info.get("source_column") or info["column"]
        return column(back.get(out_name, out_name), "src")

    rnd = Renderer(dialect, col_fn=src_col)
    select = []
    for c in cols:
        if c.get("dax"):
            tr = translator.calc_column(name, c["column"], inline_calc=True)
            e = rnd.r(tr.ir) if tr.ir is not None else exp.Null()
            dax_one_line = " ".join(c["dax"].split())
            e.add_comments([f" DAX: {dax_one_line} "])
            select.append(exp.alias_(e, ident(c["column"])))
            if tr.reviews:
                notes.append(f"{c['column']}: " + "; ".join(tr.reviews))
        else:
            out_name = c.get("source_column") or c["column"]
            source_name = back.get(out_name, out_name)
            e = column(source_name, "src")
            proj = next((p for p in sql.get("projections", []) if str(p["name"]).lower() == source_name.lower()), None)
            m_type = next((mtypes[k.lower()] for k in (c["column"], out_name, source_name) if k and k.lower() in mtypes), None)
            sql_type = M_TYPE_CAST.get(str(m_type).lower()) if m_type else None
            if proj and not proj["is_plain_column"] and sql_type:
                e = exp.Cast(this=e, to=exp.DataType.build(sql_type, dialect=dialect))   # Power Query type step
                select.append(exp.alias_(e, ident(c["column"])))
            else:
                select.append(e if source_name == c["column"] else exp.alias_(e, ident(c["column"])))
    if not select:
        select = [exp.Star()]
    outer = exp.select(*select).from_(exp.Table(this=exp.to_identifier("src")))
    body = outer.sql(dialect=dialect, pretty=True)
    native = m.get("native_query")
    if native:
        if "TABLESAMPLE (random sample)" in sql.get("flags", []):
            comments.append("Power BI reads a TABLESAMPLE: a random sample that changes every refresh. "
                            "SQL Server does not allow TABLESAMPLE inside a view; remove it for full data.")
        cte = textwrap.indent(native.strip().replace("\r\n", "\n"), "    ")
        text = f"CREATE VIEW {view} AS\nWITH src AS (\n{cte}\n)\n{body};"
    elif m.get("source_objects"):
        obj = m["source_objects"][-1]
        text = f"CREATE VIEW {view} AS\n{body.replace('FROM src', f'FROM {obj} AS src')};"
    elif m.get("source_kind") in ("file", "service", "embedded"):
        comments.append(f"Source is {m.get('connector')} ({m.get('server') or 'embedded data'}): land it in a "
                        f"database table first, then select from that table.")
        text = f"CREATE VIEW {view} AS\n{body.replace('FROM src', 'FROM stg_' + view[len(naming.view_prefix):] + ' AS src')};"
    else:
        comments.append("No Power Query source found for this table.")
        text = f"-- CREATE VIEW {view} AS\n{body};"
    if m.get("untranslated_steps"):
        comments.append("Power Query steps not translated (add them to the SQL): " + ", ".join(m["untranslated_steps"]))
    header = "".join(f"-- {c}\n" for c in comments)
    return {"view": view, "replaces": name, "sql": header + text, "notes": comments + notes, "dialect": dialect}
