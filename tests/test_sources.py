from pbi_insights.analyze.formulas import tool_formulas
from pbi_insights.analyze.ir import Agg, Col
from pbi_insights.extract.mquery import output_to_source_name, parse_m
from pbi_insights.extract.sqllineage import analyze_sql

M = """let
    Source = Sql.Database(".", "RetailBIDW", [Query="select s.a, s.b + 1 as bb from dbo.t s tablesample (10 percent)"]),
    #"Renamed Columns" = Table.RenameColumns(Source,{{"bb", "B Name"}}),
    #"Changed Type" = Table.TransformColumnTypes(#"Renamed Columns",{{"a", Int64.Type}})
in
    #"Changed Type\""""


def test_power_query_source_is_decoded():
    m = parse_m(M)
    assert m["connector"] == "Sql.Database"
    assert (m["server"], m["database"]) == (".", "RetailBIDW")
    assert "tablesample" in m["native_query"].lower()
    assert output_to_source_name(m)["B Name"] == "bb"


def test_native_sql_lineage_and_flags():
    a = analyze_sql("select s.a, s.b + 1 as bb from dbo.t s tablesample (10 percent)", "tsql")
    assert "TABLESAMPLE (random sample)" in a["flags"]
    assert a["objects"] == ["dbo.t"]
    assert [p["qualified"] for p in a["projections"]] == ["dbo.t.a", "dbo.t.b + 1"]


def test_single_table_columns_are_qualified_and_comments_dropped():
    a = analyze_sql("SELECT [ItemID] -- ,[SupplierID]\n , left([Seg], 4) Seg FROM [db].[dbo].[Item]", "tsql")
    assert a["projections"][0]["qualified"] == "db.dbo.Item.ItemID"
    assert a["projections"][1]["qualified"] == "LEFT(db.dbo.Item.Seg, 4)"


def test_tool_formulas():
    f = tool_formulas(Agg("SUM", Col("Sales", "Amount"), "Sales"))
    assert f == {"tableau": "SUM([Amount])", "quicksight": "sum({Amount})", "incorta": "sum(Sales.Amount)"}
