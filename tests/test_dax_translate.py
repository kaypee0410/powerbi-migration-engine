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
