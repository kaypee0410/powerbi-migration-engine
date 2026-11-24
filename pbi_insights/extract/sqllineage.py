"""Native SQL lineage: which database objects a Power Query native query reads, what each output
column is computed from, and risky patterns (sampling, date shifting, cross-database names)."""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

_IDENT = r"(?:\[[^\]]+\]|[A-Za-z_][\w$#@]*)"
_NAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,3}}"


def _table_name(t: exp.Table) -> str:
    return ".".join(p for p in (t.catalog, t.db, t.name) if p)


_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _unquote(node: exp.Expression) -> exp.Expression:
    """[MonthID] -> MonthID when the name needs no quoting (easier to read in the workbook)."""
    if isinstance(node, exp.Identifier) and node.args.get("quoted") and _PLAIN.match(node.name or ""):
        return exp.to_identifier(node.name, quoted=False)
    return node


def _qualify(col: exp.Column, alias_map: dict, default: str | None = None) -> exp.Column:
    """Replace a table alias (s.MonthID) with the full object name (dbo.Monthly_sales.MonthID); an unqualified
    column in a single-table query gets that table's name."""
    full = alias_map.get(col.table) if col.table else default
    if not full:
        return col
    parts = full.split(".")
    table, db, catalog = parts[-1], (parts[-2] if len(parts) > 1 else None), (parts[-3] if len(parts) > 2 else None)
    return exp.Column(this=col.this.copy(), table=exp.to_identifier(table),
                      db=exp.to_identifier(db) if db else None, catalog=exp.to_identifier(catalog) if catalog else None)


def _regex_objects(sql: str) -> list[str]:
    s = re.sub(r"--[^\n]*", "", sql or "")
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    found = [m.group(1) for m in re.finditer(rf"\b(?:FROM|JOIN)\s+({_NAME})", s, re.I)]
    out = []
    for f in found:
        f = re.sub(r"\s+", "", f).replace("[", "").replace("]", "")
        if f.lower() not in [o.lower() for o in out]:
            out.append(f)
    return out


def analyze_sql(sql: str | None, dialect: str | None = "tsql") -> dict:
    out = {"parse_ok": False, "objects": [], "projections": [], "star_sources": [], "flags": [], "where": None,
           "error": None}
    if not sql:
        return out
    dialect = dialect or "tsql"
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception as ex:  # noqa: BLE001 - any parser failure falls back to regex
        out["error"] = str(ex).splitlines()[0][:200]
        out["objects"] = _regex_objects(sql)
        if re.search(r"\btablesample\b", sql, re.I):
            out["flags"].append("TABLESAMPLE (random sample)")
        return out
    out["parse_ok"] = True
    tables = [t for t in tree.find_all(exp.Table) if t.name]
    for t in tables:
        name = _table_name(t)
        if name.lower() not in [o.lower() for o in out["objects"]]:
            out["objects"].append(name)
    alias_map = {}
    for t in tables:
        alias_map[t.alias_or_name] = _table_name(t)
        alias_map[t.name] = _table_name(t)
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is not None:
        for p in select.selects:
            star = isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
            if star:
                q = p.table if isinstance(p, exp.Column) else ""
                src = alias_map.get(q) if q else (out["objects"][0] if len(out["objects"]) == 1 else "*")
                out["star_sources"].append(src or q or "*")
                continue
            inner = p.this if isinstance(p, exp.Alias) else p
            single = out["objects"][0] if len(out["objects"]) == 1 else None
            qualified = inner.copy().transform(
                lambda n: _qualify(n, alias_map, single) if isinstance(n, exp.Column) else n).transform(_unquote)
            out["projections"].append({
                "name": p.alias_or_name,
                "expression": inner.sql(dialect=dialect, comments=False),
                "qualified": qualified.sql(dialect=dialect, comments=False),
                "is_plain_column": isinstance(inner, exp.Column),
                "source_columns": [(alias_map.get(c.table, c.table), c.name) for c in inner.find_all(exp.Column)],
            })
    flags = out["flags"]
    if tree.find(exp.TableSample):
        flags.append("TABLESAMPLE (random sample)")
    if select is not None and select.args.get("distinct"):
        flags.append("SELECT DISTINCT")
    if tree.find(exp.Limit) or (select is not None and select.args.get("top")) or re.search(r"\btop\s+\d", sql, re.I):
        flags.append("TOP / LIMIT (row cap)")
    if tree.find(exp.Union):
        flags.append("UNION")
    if tree.find(exp.Group):
        flags.append("GROUP BY (pre-aggregated)")
    joins = list(tree.find_all(exp.Join))
    if joins or len(tables) > 1:
        flags.append(f"joins {len(tables)} objects")
    if re.search(r"\bdateadd\s*\(", sql, re.I):
        flags.append("DATEADD (date shifting)")
    if any(t.catalog for t in tables):
        flags.append("cross-database names")
    where = tree.find(exp.Where)
    if where is not None:
        out["where"] = where.this.sql(dialect=dialect)
    return out
