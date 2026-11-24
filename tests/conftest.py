"""Shared fixtures: a tiny synthetic Power BI model and report (no client files needed).

Model: Sales (fact) -> Store (dimension), measures that cover the main DAX patterns.
Report: one page "Main" with a column chart, a card, a mixed-grain bar chart, a slicer and a YTD card.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MEASURES = {
    "Total": "SUM(Sales[Amount])",
    "TY": "CALCULATE([Total], Sales[Scenario] = 1)",
    "LY": "CALCULATE([Total], Sales[Scenario] = 2)",
    "Var %": "DIVIDE([TY] - [LY], [LY])",
    "Stores": "COUNTROWS(Store)",
    "Per Area": "DIVIDE([TY], SUM(Store[Area]))",
    "Big Stores": "CALCULATE(COUNTROWS(Store), FILTER(ALL(Store), Store[Size] = \"Big\"))",
    "YTD": "TOTALYTD([Total], Sales[Day])",
    "Unused": "MAX(Sales[Amount])",
}
STORE_ROWS = [(1, 100, "East"), (2, 200, "East"), (3, 50, "West"), (4, 70, "West")]       # store 4 has no sales
SALES_ROWS = [(1, 1, 10.0), (1, 1, 20.0), (1, 2, 5.0), (2, 1, 30.0), (3, 1, 7.0), (3, 2, 9.0)]


def _m(sql: str) -> list[str]:
    return ["let", f'    Source = Sql.Database("srv01", "SalesDW", [Query="{sql}"])', "in", "    Source"]


def tmsl() -> dict:
    sales_cols = [{"name": n, "dataType": t, "sourceColumn": n} for n, t in
                  (("StoreID", "int64"), ("Scenario", "int64"), ("Amount", "double"), ("Day", "dateTime"))]
    store_cols = [{"name": n, "dataType": t, "sourceColumn": n} for n, t in
                  (("StoreID", "int64"), ("Area", "int64"), ("Region", "string"))]
    store_cols.append({"name": "Size", "type": "calculated", "dataType": "string",
                       "expression": 'IF([Area] > 100, "Big", "Small")'})
    return {"model": {
        "tables": [
            {"name": "Sales", "columns": sales_cols,
             "partitions": [{"name": "p", "source": {"type": "m", "expression": _m(
                 "select s.StoreID, s.Scenario, s.Amount, s.Day from dbo.FactSales s")}}],
             "measures": [{"name": k, "expression": v} for k, v in MEASURES.items() if k not in ("Stores", "Big Stores")]},
            {"name": "Store", "columns": store_cols,
             "partitions": [{"name": "p", "source": {"type": "m", "expression": _m("select * from dbo.DimStore")}}],
             "measures": [{"name": "Stores", "expression": MEASURES["Stores"]},
                          {"name": "Big Stores", "expression": MEASURES["Big Stores"]}]},
        ],
        "relationships": [{"name": "r1", "fromTable": "Sales", "fromColumn": "StoreID", "toTable": "Store",
                           "toColumn": "StoreID"}],
    }}


def _select(alias, entity, prop, kind="Column"):
    return {kind: {"Expression": {"SourceRef": {"Source": alias}}, "Property": prop}, "Name": f"{entity}.{prop}"}


def _container(i, vtype, projections, selects, y):
    froms = [{"Name": "st", "Entity": "Store", "Type": 0}, {"Name": "sa", "Entity": "Sales", "Type": 0}]
    cfg = {"name": f"v{i}", "layouts": [{"position": {"x": 10, "y": y, "width": 300, "height": 200}}],
           "singleVisual": {"visualType": vtype, "projections": projections,
                            "prototypeQuery": {"Version": 2, "From": froms, "Select": selects}}}
    return {"x": 10, "y": y, "z": i, "width": 300, "height": 200, "config": json.dumps(cfg), "filters": "[]"}


def layout() -> dict:
    region = _select("st", "Store", "Region")
    m = lambda name, alias="sa", ent="Sales": _select(alias, ent, name, "Measure")  # noqa: E731
    containers = [
        _container(1, "clusteredColumnChart", {"Category": [{"queryRef": "Store.Region", "active": True}],
                                               "Y": [{"queryRef": "Sales.TY"}, {"queryRef": "Sales.LY"}]},
                   [region, m("TY"), m("LY")], 0),
        _container(2, "card", {"Values": [{"queryRef": "Store.Stores"}]}, [m("Stores", "st", "Store")], 220),
        _container(3, "barChart", {"Category": [{"queryRef": "Store.Region", "active": True}],
                                   "Y": [{"queryRef": "Sales.Per Area"}]}, [region, m("Per Area")], 440),
        _container(4, "slicer", {"Values": [{"queryRef": "Store.Region", "active": True}]}, [region], 660),
        _container(5, "card", {"Values": [{"queryRef": "Store.Big Stores"}]}, [m("Big Stores", "st", "Store")], 880),
        _container(6, "card", {"Values": [{"queryRef": "Sales.YTD"}]}, [m("YTD")], 1100),
    ]
    return {"sections": [{"name": "s1", "displayName": "Main", "ordinal": 0, "width": 1280, "height": 720,
                          "filters": "[]", "config": "{}", "visualContainers": containers}],
            "config": "{}", "filters": "[]"}


def write_pbit(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("DataModelSchema", json.dumps(tmsl()).encode("utf-16-le"))
        z.writestr("Report/Layout", json.dumps(layout()).encode("utf-16-le"))
        z.writestr("Version", "1.28".encode("utf-16-le"))
    return path


def sqlite_with_data() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE vw_store (StoreID INTEGER, Area INTEGER, Region TEXT, Size TEXT)")
    con.executemany("INSERT INTO vw_store VALUES (?, ?, ?, ?)",
                    [(i, a, r, "Big" if a > 100 else "Small") for i, a, r in STORE_ROWS])
    con.execute("CREATE TABLE vw_sales (StoreID INTEGER, Scenario INTEGER, Amount REAL, Day TEXT)")
    con.executemany("INSERT INTO vw_sales VALUES (?, ?, ?, '2026-01-15 00:00:00')", SALES_ROWS)
    return con


@pytest.fixture()
def pbit(tmp_path) -> Path:
    return write_pbit(tmp_path / "synthetic.pbit")


@pytest.fixture()
def model():
    from pbi_insights.extract.model import from_tmsl
    return from_tmsl(tmsl())


@pytest.fixture()
def report(pbit):
    from pbi_insights.extract.layout import parse_report
    from pbi_insights.extract.package import open_package
    with open_package(pbit) as pkg:
        return parse_report(pkg)
