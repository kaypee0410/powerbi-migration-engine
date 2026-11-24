"""Power Query (M) parser: what the Advanced Editor shows, decoded into connector, server, database,
native SQL, navigation, renames and the list of applied steps."""
from __future__ import annotations

import re

DB_CONNECTORS = {
    "Sql.Database": "tsql", "Sql.Databases": "tsql", "AzureSql.Database": "tsql",
    "Oracle.Database": "oracle", "PostgreSQL.Database": "postgres", "MySQL.Database": "mysql",
    "Snowflake.Databases": "snowflake", "AmazonRedshift.Database": "redshift",
    "GoogleBigQuery.Database": "bigquery", "Databricks.Catalogs": "databricks", "Databricks.Query": "databricks",
    "Teradata.Database": "teradata", "Odbc.Query": None, "Odbc.DataSource": None, "OleDb.DataSource": None,
    "Sybase.Database": None, "DB2.Database": None, "SapHana.Database": None,
}
FILE_CONNECTORS = {"Excel.Workbook", "Csv.Document", "Json.Document", "Xml.Tables", "File.Contents",
                   "Folder.Files", "Folder.Contents", "SharePoint.Files", "SharePoint.Contents",
                   "SharePoint.Tables", "Web.Contents", "Web.Page", "AzureStorage.Blobs", "AzureStorage.DataLake"}
OTHER_SOURCES = {"OData.Feed", "Salesforce.Data", "Salesforce.Reports", "AnalysisServices.Database",
                 "PowerBI.Dataflows", "PowerPlatform.Dataflows", "CommonDataService.Database", "Dynamics365.Tables"}
# steps that only reshape metadata: safe to express as SELECT ... AS ...
SIMPLE_STEPS = {"Table.RenameColumns", "Table.TransformColumnTypes", "Table.SelectColumns", "Table.RemoveColumns",
                "Table.ReorderColumns", "Navigation", "Source"}


def split_top_level(text: str, sep: str = ",") -> list[str]:
    parts, buf, depth, in_str, i = [], [], 0, False, 0
    while i < len(text):
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == '"':
                if i + 1 < len(text) and text[i + 1] == '"':
                    buf.append('"')
                    i += 1
                else:
                    in_str = False
        elif ch == '"':
            in_str = True
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


def m_unescape(s: str) -> str:
    s = s.replace('""', '"')
    for k, v in (("#(cr,lf)", "\r\n"), ("#(lf)", "\n"), ("#(cr)", "\r"), ("#(tab)", "\t")):
        s = s.replace(k, v)
    return s


def m_string(arg: str, params: dict | None = None) -> str | None:
    arg = (arg or "").strip()
    if arg.startswith('"') and arg.endswith('"'):
        return m_unescape(arg[1:-1])
    name = arg[2:-1] if arg.startswith('#"') else arg
    if params and name in params:
        return params[name]
    return arg or None


def parameter_values(expressions: list[dict]) -> dict:
    """Shared M parameters ('"server01" meta [IsParameterQuery=true, ...]') -> {name: value}."""
    out = {}
    for e in expressions or []:
        text = (e.get("expression") or "").strip()
        m = re.match(r'^"((?:[^"]|"")*)"\s*meta\s*\[', text)
        if m:
            out[e.get("name")] = m_unescape(m.group(1))
    return out


def _call_args(val: str) -> list[str]:
    try:
        inner = val[val.index("(") + 1: val.rindex(")")]
    except ValueError:
        return []
    return split_top_level(inner)


def parse_m(expr: str, params: dict | None = None) -> dict:
    res = {"expression": expr, "steps": [], "connector": None, "source_kind": None, "server": None,
           "database": None, "native_query": None, "navigation": [], "source_objects": [], "renames": [],
           "types": [], "final_step": None, "untranslated_steps": [], "dialect": None}
    if not expr:
        return res
    m = re.match(r"\s*let\s+(.*)\s+in\s+(.*?)\s*$", expr, re.S)
    if not m:
        res["steps"].append({"name": "(expression)", "function": "Expression", "detail": expr[:300]})
        return res
    body, res["final_step"] = m.group(1), m.group(2).strip()
    for part in split_top_level(body):
        name, _, val = part.partition("=")
        name = name.strip()
        name = name[2:-1] if name.startswith('#"') else name
        val = val.strip()
        fnm = re.match(r"([A-Za-z_][\w\.]*)\s*\(", val)
        fn = fnm.group(1) if fnm else ("Navigation" if re.match(r"^[#\w\"]+\s*\{", val) else "Expression")
        step = {"name": name, "function": fn, "detail": ""}
        if fn in DB_CONNECTORS:
            args = _call_args(val)
            res["connector"], res["source_kind"] = fn, "database"
            res["dialect"] = DB_CONNECTORS[fn]
            res["server"] = m_string(args[0], params) if args else None
            if len(args) > 1 and not args[1].strip().startswith("["):
                res["database"] = m_string(args[1], params)
            opt = next((a for a in args if a.strip().startswith("[")), None)
            if opt:
                q = re.search(r'Query\s*=\s*"((?:[^"]|"")*)"', opt, re.S)
                if q:
                    res["native_query"] = m_unescape(q.group(1))
            step["detail"] = (f"Connect {fn}(server='{res['server']}', database='{res['database']}')"
                              + (" with a native SQL query" if res["native_query"] else ""))
        elif fn == "Value.NativeQuery":
            args = _call_args(val)
            if len(args) > 1:
                res["native_query"] = m_string(args[1], params)
            step["detail"] = "Native SQL query (Value.NativeQuery)"
        elif fn in FILE_CONNECTORS or fn in OTHER_SOURCES:
            res["connector"] = res["connector"] or fn
            res["source_kind"] = res["source_kind"] or ("file" if fn in FILE_CONNECTORS else "service")
            p = re.search(r'"((?:[^"]|"")*)"', val)
            if p and not res["server"]:
                res["server"] = m_unescape(p.group(1))
            step["detail"] = f"{fn}({res['server'] or ''})"
        elif fn == "Table.FromRows" or val.startswith("#table("):
            res["connector"] = res["connector"] or "Enter data (embedded table)"
            res["source_kind"] = res["source_kind"] or "embedded"
            step["detail"] = "Data typed into Power BI ('Enter data')"
        elif fn == "Navigation":
            nav = dict(re.findall(r'(\w+)\s*=\s*"((?:[^"]|"")*)"', val))
            res["navigation"].append(nav)
            parts = [nav.get(k) for k in ("Catalog", "Database", "Schema", "Item", "Name") if nav.get(k)]
            if parts:
                res["source_objects"].append(".".join(parts))
            step["detail"] = "Navigate to " + ", ".join(f"{k}={v}" for k, v in nav.items())
        elif fn == "Table.RenameColumns":
            pairs = re.findall(r'\{\s*"((?:[^"]|"")*)"\s*,\s*"((?:[^"]|"")*)"\s*\}', val)
            res["renames"].extend((m_unescape(a), m_unescape(b)) for a, b in pairs)
            changed = [f"{a} -> {b}" for a, b in pairs if a != b]
            step["detail"] = "Rename: " + (", ".join(changed) if changed else "(no effective change)")
        elif fn == "Table.TransformColumnTypes":
            pairs = re.findall(r'\{\s*"((?:[^"]|"")*)"\s*,\s*((?:type\s+[\w.]+)|(?:[\w.]+\.Type))\s*\}', val)
            res["types"].extend(pairs)
            step["detail"] = f"Set data types on {len(pairs)} columns"
        elif fn in ("Table.SelectColumns", "Table.RemoveColumns", "Table.ReorderColumns"):
            cols = re.findall(r'"((?:[^"]|"")*)"', val)
            step["detail"] = f"{fn.split('.')[-1]}: {', '.join(cols[:12])}{' ...' if len(cols) > 12 else ''}"
        else:
            step["detail"] = re.sub(r"\s+", " ", val)[:300]
        if fn not in SIMPLE_STEPS and fn not in DB_CONNECTORS and fn not in FILE_CONNECTORS \
                and fn not in OTHER_SOURCES and fn not in ("Value.NativeQuery", "Expression"):
            res["untranslated_steps"].append(f"{name} ({fn})")
        res["steps"].append(step)
    return res


def output_to_source_name(m: dict) -> dict:
    """Map each final Power Query column name back to the name the source returns (undo renames)."""
    mapping = {}
    for old, new in m.get("renames", []):
        mapping[new] = mapping.get(old, old)
    return mapping
