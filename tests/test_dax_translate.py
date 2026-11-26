from pbi_insights.analyze.dax_translate import ModelIndex, Translator, classify_measure
from pbi_insights.analyze.graph import RelGraph
from pbi_insights.analyze.planner import generic_measure_sql


def _tr(model):
    graph = RelGraph(model["relationships"])
    return Translator(ModelIndex(model), graph), graph


def sql(model, name, dialect="tsql"):
    tr, _ = _tr(model)
    t = tr.measure(name)
    return t, generic_measure_sql(t.ir, dialect)


def test_simple_sum(model):
    t, s = sql(model, "Total")
    assert t.status.startswith("Translated")
    assert s == "SUM(Sales.Amount)"


def test_calculate_filter_is_pushed_into_the_aggregate(model):
    _, s = sql(model, "TY")
    assert "CASE WHEN Sales.Scenario = 1 THEN Sales.Amount END" in s


def test_divide_is_safe(model):
    t, s = sql(model, "Var %")
    assert t.status.startswith("Translated")
    assert "NULLIF" in s or "CASE WHEN" in s


def test_time_intelligence_needs_review(model):
    t, s = sql(model, "YTD")
    assert t.reviews, "TOTALYTD must be flagged, never silently translated"
    tr, graph = _tr(model)
    assert classify_measure(t, t.is_alias, graph)[0].startswith(("Time intelligence", "Needs review"))


def test_patterns(model):
    tr, graph = _tr(model)
    pattern = {n: classify_measure(tr.measure(n), tr.measure(n).is_alias, graph)[0]
               for n in ("Stores", "Per Area", "Big Stores")}
    assert pattern["Stores"] == "Dimension-grain aggregate"
    assert pattern["Per Area"] == "Mixed grain (fact + dimension)"
    assert pattern["Big Stores"] == "Filter-independent (ALL)"


def test_calculated_column(model):
    tr, _ = _tr(model)
    t = tr.calc_column("Store", "Size")
    s = generic_measure_sql(t.ir, "tsql")
    assert s == "CASE WHEN Store.Area > 100 THEN 'Big' ELSE 'Small' END"


def _with_measures(model, **measures):
    model["measures"] += [{"table": "Sales", "name": n, "dax": d} for n, d in measures.items()]
    return model


def test_aggregate_inside_a_filter_condition_needs_review(model):
    # DAX evaluates SELECTEDVALUE / AVERAGE in a FILTER condition once per visual cell, not per row.
    # Inlining it would give SUM(CASE WHEN x >= MAX(y) ...), which is invalid SQL: flag it instead.
    _with_measures(model,
                   **{"From Selected Day": "CALCULATE([Total], FILTER(Sales, Sales[Day] >= SELECTEDVALUE(Sales[Day])))",
                      "Above Avg Rows": "COUNTROWS(FILTER(Sales, Sales[Amount] > AVERAGE(Sales[Amount])))",
                      "Above Avg Sum": "SUMX(FILTER(Sales, Sales[Amount] > AVERAGE(Sales[Amount])), Sales[Amount])",
                      "Boolean Max": "CALCULATE([Total], Sales[Amount] >= MAX(Sales[Amount]))"})
    for name in ("From Selected Day", "Above Avg Rows", "Above Avg Sum", "Boolean Max"):
        t, s = sql(model, name)
        assert t.status == "Needs review", name
        assert any("filter condition" in r for r in t.reviews), name
        assert "MAX(" not in s and "AVG(" not in s, f"{name}: aggregate leaked into a row filter: {s}"


def test_plain_value_filters_still_translate_and_run(model):
    import sqlite3
    _with_measures(model, **{"Since 2026": "CALCULATE([Total], FILTER(Sales, Sales[Day] >= DATE(2026, 1, 1)))",
                             "Before 2026": "CALCULATE([Total], FILTER(Sales, Sales[Day] < DATE(2026, 1, 1)))"})
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE vw_sales (Amount REAL, Day TEXT)")
    con.executemany("INSERT INTO vw_sales VALUES (?, ?)", [(10.0, "2025-12-31 00:00:00"), (5.0, "2026-01-01 00:00:00"),
                                                           (7.0, "2026-03-15 00:00:00")])
    got = {}
    for name in ("Since 2026", "Before 2026"):
        t, s = sql(model, name, "sqlite")
        assert t.status.startswith("Translated"), (name, t.reviews)
        got[name] = con.execute(f"SELECT {s} FROM vw_sales AS Sales").fetchone()[0]
    assert got == {"Since 2026": 12.0, "Before 2026": 10.0}
