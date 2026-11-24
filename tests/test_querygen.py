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
