"""The generated insight SQL must return what Power BI would show (checked on a tiny SQLite dataset)."""
import pytest

from conftest import sqlite_with_data
from pbi_insights.analyze.planner import Planner


@pytest.fixture()
def plans(report, model):
    return {p["id"]: p for p in Planner(report, model, dialect="sqlite").plan_report()}


def rows(plan):
    con = sqlite_with_data()
    return [tuple(r) for r in con.execute(plan["sql"]).fetchall()]


def test_ids_and_approaches(plans):
    assert list(plans) == ["MA-01", "MA-02", "MA-03", "MA-04", "MA-05", "MA-06"]
    assert plans["MA-01"]["approach"] == "Simple measures"
    assert plans["MA-02"]["approach"] == "Dimension dataset (Store)"
    assert plans["MA-03"]["approach"] == "Custom SQL dataset (mixed grain)"
    assert plans["MA-04"]["approach"] == "Filter control"
    assert plans["MA-05"]["approach"] == "Fixed KPI (ignores filters)"
    assert plans["MA-06"]["approach"] == "Needs review"


def test_simple_measures_by_region(plans):
    got = {r[0]: r[1:] for r in rows(plans["MA-01"])}
    assert got == {"East": (60.0, 5.0), "West": (7.0, 9.0)}


def test_dimension_count_does_not_count_fact_rows(plans):
    assert rows(plans["MA-02"]) == [(4,)]


def test_mixed_grain_is_not_double_counted(plans):
    got = {r[0]: round(r[1], 6) for r in rows(plans["MA-03"])}
    # TY / SUM(Store[Area]) per region; the area sum includes store 4, which has no sales
    assert got == {"East": round(60 / 300, 6), "West": round(7 / 120, 6)}


def test_slicer_lists_distinct_values(plans):
    assert sorted(rows(plans["MA-04"])) == [("East",), ("West",)]


def test_filter_all_is_a_fixed_number(plans):
    assert rows(plans["MA-05"]) == [(1,)]


def _unrelated_setup(model):
    """Store (dimension) plus a Calendar table that has no relationship to anything."""
    from pbi_insights.analyze.graph import RelGraph
    con = sqlite_with_data()
    con.execute("CREATE TABLE vw_calendar (Month TEXT)")
    con.executemany("INSERT INTO vw_calendar VALUES (?)", [("Jan",), ("Feb",)])
    return RelGraph(model["relationships"]), con


def test_unrelated_dimension_without_aggregates_is_cross_joined(model):
    # Power BI shows every Month for every Region/Size row (columns of different tables are cross joined);
    # Region/Size keep only the combinations that exist in Store
    from pbi_insights.analyze.ir import Lit, Review
    from pbi_insights.analyze.querygen import Dim, MeasureSpec, QueryBuilder
    graph, con = _unrelated_setup(model)
    qb = QueryBuilder(graph, dialect="sqlite")
    dims = lambda: [Dim("Calendar", "Month", "Month"), Dim("Store", "Region", "Region"),  # noqa: E731
                    Dim("Store", "Size", "Size")]
    plan = qb.build(dims(), [MeasureSpec("One", "one", Lit(1))], [], [])
    got = con.execute(plan.sql).fetchall()          # used to fail: Calendar selected but never joined
    assert sorted(got) == sorted((mo, r, s, 1) for mo in ("Jan", "Feb")
                                 for r, s in (("East", "Big"), ("East", "Small"), ("West", "Small")))
    # a blank (untranslated) measure: Power BI drops rows where every measure is blank
    plan = qb.build(dims(), [MeasureSpec("M", "m", Review("not translated"))], [], [])
    assert con.execute(plan.sql).fetchall() == []


def test_constant_measure_shows_every_combination_and_respects_filters(model):
    from pbi_insights.analyze.ir import Bin, Col, Lit
    from pbi_insights.analyze.querygen import Dim, FilterSpec, MeasureSpec, QueryBuilder, SortSpec
    graph, con = _unrelated_setup(model)
    region, scenario = Dim("Store", "Region", "Region"), Dim("Sales", "Scenario", "Scenario")
    qb = QueryBuilder(graph, dialect="sqlite")
    plan = qb.build([region, scenario], [MeasureSpec("One", "one", Lit(1))], [], [SortSpec("dim", region)])
    got = con.execute(plan.sql).fetchall()
    assert [r[0] for r in got] == ["East", "East", "West", "West"]          # sorted by Region
    assert sorted(got) == [("East", 1, 1), ("East", 2, 1), ("West", 1, 1), ("West", 2, 1)]
    east = FilterSpec("Store", "Region", Bin("=", Col("Store", "Region"), Lit("East")), "Region = East")
    region, scenario = Dim("Store", "Region", "Region"), Dim("Sales", "Scenario", "Scenario")
    plan = qb.build([region, scenario], [MeasureSpec("One", "one", Lit(1))], [east], [])
    # the Region filter limits Store, and also Sales (Sales looks up Store), never the other way round
    assert sorted(con.execute(plan.sql).fetchall()) == [("East", 1, 1), ("East", 2, 1)]
    assert not plan.reviews


def test_unrelated_dimension_in_multi_grain_query_repeats_the_measure(model):
    from pbi_insights.analyze.ir import Agg, Col, SafeDiv
    from pbi_insights.analyze.querygen import Dim, MeasureSpec, QueryBuilder, SortSpec
    graph, con = _unrelated_setup(model)
    month, region = Dim("Calendar", "Month", "Month"), Dim("Store", "Region", "Region")
    per_area = SafeDiv(Agg("SUM", Col("Sales", "Amount"), "Sales"), Agg("SUM", Col("Store", "Area"), "Store"))
    plan = QueryBuilder(graph, dialect="sqlite").build(
        [region, month], [MeasureSpec("Per Area", "per_area", per_area)], [], [SortSpec("dim", month)])
    assert plan.form == "multi-grain"
    got = sorted((r[0], r[1], round(r[2], 6)) for r in con.execute(plan.sql).fetchall())
    assert got == sorted((r, mo, round(v, 6)) for r, v in (("East", 65 / 300), ("West", 16 / 120))
                         for mo in ("Jan", "Feb"))
    assert not any("Calendar" in r for r in plan.reviews)
