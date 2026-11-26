"""Semantic model reader: tables, columns, measures, relationships, Power Query, KPIs, RLS.

Two sources give the same normalized result:
  * .pbix  -> VertiPaq DataModel decoded with pbixray (metadata SQLite inside the model backup)
  * .pbit / .bim -> TMSL JSON (DataModelSchema)
"""
from __future__ import annotations

from .package import Package

DATATYPE = {1: "Automatic", 2: "Text", 6: "Whole number", 8: "Decimal number", 9: "Date/time",
            10: "Fixed decimal", 11: "True/false", 17: "Binary", 19: "Unknown", 20: "Variant"}
TMSL_DATATYPE = {"string": "Text", "int64": "Whole number", "double": "Decimal number", "datetime": "Date/time",
                 "decimal": "Fixed decimal", "boolean": "True/false", "binary": "Binary", "variant": "Variant",
                 "automatic": "Automatic", "unknown": "Unknown"}
SUMMARIZE = {1: "Default", 2: "Don't summarize", 3: "Sum", 4: "Min", 5: "Max", 6: "Count", 7: "Average", 8: "DistinctCount"}
COL_TYPE = {1: "Data", 2: "Calculated", 3: "RowNumber", 4: "Calculated table column"}
CROSSFILTER = {1: "Single", 2: "Both", 3: "Automatic"}
CARD = {0: "none", 1: "one", 2: "many"}
SYSTEM_PREFIXES = ("H$", "R$", "U$")


def is_auto_date(name: str) -> bool:
    return str(name).startswith(("LocalDateTable_", "DateTableTemplate_"))


def _s(v):
    """Clean a value coming from pandas (NaN -> None)."""
    if v is None:
        return None
    try:
        if v != v:          # NaN
            return None
    except Exception:  # noqa: BLE001
        pass
    return v


def empty_model(warning: str | None = None) -> dict:
    return {"source": None, "tables": [], "columns": [], "measures": [], "kpis": [], "relationships": [],
            "hierarchies": [], "roles": [], "expressions": [], "data_sources": [],
            "warnings": [warning] if warning else []}


# ------------------------------------------------------------------------------------- VertiPaq / pbixray
def _metadata_db(model):
    """pbixray keeps the model's metadata SQLite behind a private attribute; isolate that access here."""
    try:
        return model._metadata._meta._db
    except AttributeError as ex:  # pragma: no cover - depends on the pinned pbixray version
        raise RuntimeError("This pbixray version does not expose the metadata database; "
                           "install the version pinned in requirements.txt") from ex


def _q(db, sql: str):
    try:
        return db.query(sql)
    except Exception:  # noqa: BLE001 - table missing in older/newer model versions
        return None


def from_vertipaq(path) -> dict:
    from pbixray import PBIXRay  # imported lazily: heavy native dependencies

    pbx = PBIXRay(str(path))
    db = _metadata_db(pbx)
    out = empty_model()
    out["source"] = "VertiPaq DataModel (pbixray)"
    out["_handle"] = pbx

    # ---- tables + row counts --------------------------------------------------------------
    rows = {}
    rc = _q(db, """SELECT t.Name AS TableName, SUM(sms.RecordCount) AS Rows
                   FROM [Partition] p JOIN [Table] t ON t.ID = p.TableID
                   JOIN PartitionStorage ps ON ps.ID = p.PartitionStorageID
                   JOIN SegmentMapStorage sms ON sms.ID = ps.SegmentMapStorageID
                   WHERE p.Type <> 3 GROUP BY t.Name""")
    if rc is not None:
        rows = {r.TableName: int(r.Rows) for r in rc.itertuples() if _s(r.Rows) is not None}
    m_by_table = {}
    try:
        pq = pbx.power_query
        if pq is not None and len(pq):
            for _, r in pq.iterrows():
                m_by_table[str(r.iloc[0])] = str(r.iloc[1])
    except Exception:  # noqa: BLE001
        pass
    try:
        mq = pbx.mashup_queries
        if mq is not None and len(mq):
            name_col = next((c for c in mq.columns if c.lower() in ("name", "queryname", "tablename")), mq.columns[0])
            expr_col = next((c for c in mq.columns if c.lower() == "expression"), mq.columns[-1])
            for _, r in mq.iterrows():
                m_by_table.setdefault(str(r[name_col]), str(r[expr_col]))
    except Exception:  # noqa: BLE001
        pass
    calc_tables = {}
    parts = _q(db, "SELECT t.Name AS TableName, p.Type, p.QueryDefinition FROM [Partition] p JOIN [Table] t ON t.ID = p.TableID")
    if parts is not None:
        for r in parts.itertuples():
            if r.Type == 2 and _s(r.QueryDefinition):
                calc_tables[r.TableName] = r.QueryDefinition
            if r.Type == 4 and _s(r.QueryDefinition):
                m_by_table.setdefault(r.TableName, r.QueryDefinition)
    tdf = _q(db, "SELECT ID, Name, IsHidden, Description FROM [Table]")
    for r in tdf.itertuples():
        if str(r.Name).startswith(SYSTEM_PREFIXES):
            continue
        out["tables"].append({
            "name": r.Name, "hidden": bool(r.IsHidden), "auto_date": is_auto_date(r.Name),
            "calculated": r.Name in calc_tables, "calc_dax": calc_tables.get(r.Name),
            "row_count": rows.get(r.Name), "description": _s(r.Description),
            "m_expression": m_by_table.get(r.Name)})

    # ---- columns ----------------------------------------------------------------------------
    cdf = _q(db, """SELECT c.ID, t.Name AS TableName, c.ExplicitName, c.InferredName, c.Type, c.ExplicitDataType,
                      c.InferredDataType, c.DataCategory, c.IsHidden, c.IsKey, c.SummarizeBy, c.SourceColumn,
                      c.Expression, c.FormatString, COALESCE(NULLIF(s.ExplicitName, ''), s.InferredName) AS SortBy,
                      c.Description, c.DisplayFolder
                    FROM [Column] c JOIN [Table] t ON t.ID = c.TableID
                    LEFT JOIN [Column] s ON s.ID = c.SortByColumnID""")
    for r in cdf.itertuples():
        if r.Type == 3 or str(r.TableName).startswith(SYSTEM_PREFIXES):
            continue
        name = _s(r.ExplicitName) or _s(r.InferredName)
        dt = DATATYPE.get(r.ExplicitDataType, str(r.ExplicitDataType))
        if dt == "Automatic":
            dt = DATATYPE.get(_s(r.InferredDataType), "Automatic")
        out["columns"].append({
            "table": r.TableName, "column": name, "column_type": COL_TYPE.get(r.Type, str(r.Type)),
            "data_type": dt, "data_category": _s(r.DataCategory), "hidden": bool(r.IsHidden),
            "is_key": bool(r.IsKey), "summarize_by": SUMMARIZE.get(r.SummarizeBy, str(r.SummarizeBy)),
            "source_column": _s(r.SourceColumn), "dax": _s(r.Expression), "format_string": _s(r.FormatString),
            "sort_by": _s(r.SortBy), "description": _s(r.Description), "display_folder": _s(r.DisplayFolder),
            "auto_date": is_auto_date(r.TableName)})

    # ---- measures + KPIs ---------------------------------------------------------------------
    mdf = _q(db, """SELECT m.ID, t.Name AS TableName, m.Name, m.Expression, m.FormatString, m.IsHidden, m.KPIID,
                      m.DisplayFolder, m.Description FROM Measure m JOIN [Table] t ON t.ID = m.TableID ORDER BY m.ID""")
    kpi_by_id = {}
    kdf = _q(db, "SELECT ID, TargetExpression, StatusExpression, StatusGraphic, TargetFormatString FROM KPI")
    if kdf is not None:
        kpi_by_id = {r.ID: r for r in kdf.itertuples()}
    for r in mdf.itertuples():
        k = kpi_by_id.get(_s(r.KPIID))
        out["measures"].append({
            "id": int(r.ID), "table": r.TableName, "name": r.Name, "dax": _s(r.Expression) or "",
            "format_string": _s(r.FormatString), "hidden": bool(r.IsHidden), "display_folder": _s(r.DisplayFolder),
            "description": _s(r.Description), "has_kpi": k is not None})
        if k is not None:
            out["kpis"].append({"measure": r.Name, "table": r.TableName, "target_expression": _s(k.TargetExpression),
                                "status_expression": _s(k.StatusExpression), "status_graphic": _s(k.StatusGraphic)})

    # ---- relationships --------------------------------------------------------------------------
    rdf = _q(db, """SELECT r.ID, r.IsActive, r.CrossFilteringBehavior, r.FromCardinality, r.ToCardinality,
                      ft.Name AS FromTable, COALESCE(NULLIF(fc.ExplicitName, ''), fc.InferredName) AS FromColumn,
                      tt.Name AS ToTable, COALESCE(NULLIF(tc.ExplicitName, ''), tc.InferredName) AS ToColumn
                    FROM Relationship r JOIN [Table] ft ON ft.ID = r.FromTableID JOIN [Column] fc ON fc.ID = r.FromColumnID
                    JOIN [Table] tt ON tt.ID = r.ToTableID LEFT JOIN [Column] tc ON tc.ID = r.ToColumnID""")
    for r in rdf.itertuples():
        out["relationships"].append({
            "from_table": r.FromTable, "from_column": _s(r.FromColumn), "to_table": r.ToTable,
            "to_column": _s(r.ToColumn) or "Date", "from_cardinality": CARD.get(r.FromCardinality, "many"),
            "to_cardinality": CARD.get(r.ToCardinality, "one"),
            "cross_filter": CROSSFILTER.get(r.CrossFilteringBehavior, str(r.CrossFilteringBehavior)),
            "active": bool(r.IsActive), "auto_date": is_auto_date(r.ToTable) or is_auto_date(r.FromTable)})

    # ---- hierarchies, RLS, shared expressions, data sources ------------------------------------
    hdf = _q(db, """SELECT t.Name AS TableName, h.Name AS Hierarchy, l.Name AS Level, l.Ordinal,
                      COALESCE(NULLIF(c.ExplicitName, ''), c.InferredName) AS ColumnName
                    FROM Level l JOIN Hierarchy h ON h.ID = l.HierarchyID JOIN [Table] t ON t.ID = h.TableID
                    JOIN [Column] c ON c.ID = l.ColumnID""")
    if hdf is not None:
        out["hierarchies"] = [{"table": r.TableName, "hierarchy": r.Hierarchy, "level": r.Level,
                               "ordinal": int(r.Ordinal), "column": _s(r.ColumnName)}
                              for r in hdf.itertuples() if not is_auto_date(r.TableName)]
    roles = _q(db, """SELECT ro.Name AS RoleName, t.Name AS TableName, tp.FilterExpression
                      FROM TablePermission tp JOIN Role ro ON ro.ID = tp.RoleID JOIN [Table] t ON t.ID = tp.TableID""")
    if roles is not None:
        out["roles"] = [{"role": r.RoleName, "table": r.TableName, "filter": _s(r.FilterExpression)}
                        for r in roles.itertuples()]
    exprs = _q(db, "SELECT Name, Kind, Expression FROM Expression")
    if exprs is not None:
        out["expressions"] = [{"name": r.Name, "kind": _s(r.Kind), "expression": _s(r.Expression)}
                              for r in exprs.itertuples()]
    ds = _q(db, "SELECT Name, ConnectionString FROM DataSource")
    if ds is not None:
        out["data_sources"] = [{"name": r.Name, "connection": _s(r.ConnectionString)} for r in ds.itertuples()]
    return out


# ------------------------------------------------------------------------------------- TMSL JSON (.pbit/.bim)
def _text(v) -> str | None:
    if v is None:
        return None
    return "\n".join(v) if isinstance(v, list) else str(v)


def from_tmsl(doc: dict) -> dict:
    model = (doc or {}).get("model", doc or {})
    out = empty_model()
    out["source"] = "TMSL JSON (DataModelSchema)"
    for t in model.get("tables", []):
        name = t.get("name")
        parts = t.get("partitions") or []
        src = (parts[0].get("source") or {}) if parts else {}
        stype = (src.get("type") or "").lower()
        out["tables"].append({
            "name": name, "hidden": bool(t.get("isHidden")),
            "auto_date": is_auto_date(name) or any(a.get("name") == "__PBI_LocalDateTable" for a in t.get("annotations", [])),
            "calculated": stype == "calculated", "calc_dax": _text(src.get("expression")) if stype == "calculated" else None,
            "row_count": None, "description": _text(t.get("description")),
            "m_expression": _text(src.get("expression")) if stype in ("m", "") and src.get("expression") else
            (_text(src.get("query")) if stype == "query" else None)})
        for c in t.get("columns", []):
            ctype = (c.get("type") or "data").lower()
            if ctype == "rownumber":
                continue
            out["columns"].append({
                "table": name, "column": c.get("name"),
                "column_type": {"calculated": "Calculated", "calculatedtablecolumn": "Calculated table column"}.get(ctype, "Data"),
                "data_type": TMSL_DATATYPE.get(str(c.get("dataType", "")).lower(), c.get("dataType")),
                "data_category": c.get("dataCategory"), "hidden": bool(c.get("isHidden")), "is_key": bool(c.get("isKey")),
                "summarize_by": {"none": "Don't summarize"}.get(str(c.get("summarizeBy", "default")).lower(),
                                                                          str(c.get("summarizeBy", "Default")).capitalize()), "source_column": c.get("sourceColumn"),
                "dax": _text(c.get("expression")), "format_string": c.get("formatString"),
                "sort_by": c.get("sortByColumn"), "description": _text(c.get("description")),
                "display_folder": c.get("displayFolder"), "auto_date": out["tables"][-1]["auto_date"]})
        for i, m in enumerate(t.get("measures", [])):
            out["measures"].append({
                "id": len(out["measures"]) + 1, "table": name, "name": m.get("name"), "dax": _text(m.get("expression")) or "",
                "format_string": m.get("formatString"), "hidden": bool(m.get("isHidden")),
                "display_folder": m.get("displayFolder"), "description": _text(m.get("description")),
                "has_kpi": bool(m.get("kpi"))})
            if m.get("kpi"):
                k = m["kpi"]
                out["kpis"].append({"measure": m.get("name"), "table": name,
                                    "target_expression": _text(k.get("targetExpression")),
                                    "status_expression": _text(k.get("statusExpression")),
                                    "status_graphic": k.get("statusGraphic")})
        for h in t.get("hierarchies", []):
            for i, lvl in enumerate(h.get("levels", [])):
                out["hierarchies"].append({"table": name, "hierarchy": h.get("name"), "level": lvl.get("name"),
                                           "ordinal": lvl.get("ordinal", i), "column": lvl.get("column")})
    for r in model.get("relationships", []):
        out["relationships"].append({
            "from_table": r.get("fromTable"), "from_column": r.get("fromColumn"), "to_table": r.get("toTable"),
            "to_column": r.get("toColumn"), "from_cardinality": r.get("fromCardinality", "many"),
            "to_cardinality": r.get("toCardinality", "one"),
            "cross_filter": "Both" if r.get("crossFilteringBehavior") == "bothDirections" else "Single",
            "active": r.get("isActive", True) is not False,
            "auto_date": is_auto_date(r.get("toTable", "")) or is_auto_date(r.get("fromTable", ""))})
    for role in model.get("roles", []):
        for tp in role.get("tablePermissions", []):
            out["roles"].append({"role": role.get("name"), "table": tp.get("name"), "filter": _text(tp.get("filterExpression"))})
    out["expressions"] = [{"name": e.get("name"), "kind": e.get("kind"), "expression": _text(e.get("expression"))}
                          for e in model.get("expressions", [])]
    out["data_sources"] = [{"name": d.get("name"), "connection": d.get("connectionString") or str(d.get("connectionDetails", ""))}
                           for d in model.get("dataSources", [])]
    return out


def extract_model(pkg: Package) -> dict:
    if pkg.has_model_schema:
        return from_tmsl(pkg.model_schema())
    if pkg.has_vertipaq_model:
        return from_vertipaq(pkg.path)
    return empty_model("No semantic model in this file: it is probably a thin report connected to a published "
                       "semantic model. Export a .pbit, or open the semantic model's own .pbix, to decode DAX and sources.")
