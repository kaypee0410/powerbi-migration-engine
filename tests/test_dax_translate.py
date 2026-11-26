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


# ---- calculated columns: no filter context, so an aggregation is a whole-table value -------------------
def _with_calc_columns(model, table, **cols):
    model["columns"] += [{"table": table, "column": n, "dax": d, "data_type": "Decimal", "hidden": False}
                         for n, d in cols.items()]
    return model


def _calc_values(model, table, name):
    """Render a calculated column like the pipeline does and evaluate it on the conftest data."""
    from sqlglot import exp
    from pbi_insights.analyze.sqlrender import Renderer, column, table_ref
    from conftest import sqlite_with_data
    tr, _ = _tr(model)
    t = tr.calc_column(table, name, inline_calc=True)
    view = {"Sales": "vw_sales", "Store": "vw_store"}

    def whole(s):
        inner = Renderer("sqlite", col_fn=lambda c: column(c.column)).r(s.expr)
        return exp.Subquery(this=exp.select(inner).from_(table_ref(view[s.table])))

    e = Renderer("sqlite", col_fn=lambda c: column(c.column, table), scalar_fn=whole).r(t.ir)
    sql = f"SELECT {e.sql(dialect='sqlite')} FROM {view[table]} AS {table}"
    return t, [r[0] for r in sqlite_with_data().execute(sql).fetchall()]


def test_calculated_column_aggregate_is_the_whole_table_total(model):
    _with_calc_columns(model, "Sales", Share="DIVIDE(Sales[Amount], SUM(Sales[Amount]))",
                       **{"Above Avg": "IF(Sales[Amount] > AVERAGE(Sales[Amount]), 1, 0)"})
    t, got = _calc_values(model, "Sales", "Share")
    assert t.status.startswith("Translated") and not t.reviews
    assert [round(v, 6) for v in got] == [round(a / 81, 6) for a in (10, 20, 5, 30, 7, 9)]
    t, got = _calc_values(model, "Sales", "Above Avg")                       # average = 13.5
    assert t.status.startswith("Translated") and got == [0, 1, 0, 1, 0, 0]


def test_calculated_column_on_a_dimension_counts_the_whole_fact_table(model):
    # no context transition without CALCULATE: every store gets the total row count of Sales (6)
    _with_calc_columns(model, "Store", **{"All Sales Rows": "COUNTROWS(Sales)"})
    t, got = _calc_values(model, "Store", "All Sales Rows")
    assert t.status.startswith("Translated") and got == [6, 6, 6, 6]


def test_calculated_column_context_transition_needs_review(model):
    _with_calc_columns(model, "Store", **{
        "Store Rows": "CALCULATE(COUNTROWS(Sales))",                                    # context transition
        "Store Rows 2": "COUNTROWS(FILTER(Sales, Sales[StoreID] = Store[StoreID]))",    # refers to the current row
    })
    tr, _ = _tr(model)
    for name in ("Store Rows", "Store Rows 2"):
        t = tr.calc_column("Store", name, inline_calc=True)
        assert t.status == "Needs review", name
        assert any("current row" in r for r in t.reviews), (name, t.reviews)


def test_view_never_puts_a_bare_aggregate_in_the_select(model):
    from sqlglot import exp, parse_one
    from pbi_insights.analyze.querygen import Naming
    from pbi_insights.analyze.sources import analyze_sources, build_view
    _with_calc_columns(model, "Sales", Share="DIVIDE(Sales[Amount], SUM(Sales[Amount]))")
    tr, _ = _tr(model)
    sales = next(t for t in model["tables"] if t["name"] == "Sales")
    view = build_view(sales, model, analyze_sources(model), tr, Naming())
    tree = parse_one(view["sql"].split("CREATE VIEW vw_sales AS", 1)[1].rstrip(";"), read=view["dialect"])
    for agg in tree.find_all(exp.AggFunc):
        assert agg.find_ancestor(exp.Subquery) is not None, view["sql"]
    assert "FROM src" in view["sql"]


def test_outer_row_value_inside_an_iteration_needs_review(model):
    # EARLIER pattern: the variable / RELATED value belongs to the outer row. Translated naively the filter
    # becomes Sales.Amount <= Sales.Amount and every row silently gets the grand total.
    _with_calc_columns(model, "Sales", **{
        "Running": "VAR d = Sales[Amount] RETURN SUMX(FILTER(Sales, Sales[Amount] <= d), Sales[Amount])",
        "Same Store": "VAR s = Sales[StoreID] RETURN COUNTROWS(FILTER(Sales, Sales[StoreID] = s))",
        "Same Region": "COUNTROWS(FILTER(Store, Store[Region] = RELATED(Store[Region])))",
    })
    _with_calc_columns(model, "Store", Rank="VAR a = Store[Area] RETURN COUNTROWS(FILTER(Store, Store[Area] > a)) + 1")
    tr, _ = _tr(model)
    for table, name in (("Sales", "Running"), ("Sales", "Same Store"), ("Sales", "Same Region"), ("Store", "Rank")):
        t = tr.calc_column(table, name, inline_calc=True)
        assert t.status == "Needs review", name
        assert any("outer row" in r for r in t.reviews), (name, t.reviews)


def test_variable_defined_inside_the_iteration_is_still_translated(model):
    _with_calc_columns(model, "Store", **{"Double": "SUMX(Sales, VAR a = Sales[Amount] RETURN a * 2)"})
    t, got = _calc_values(model, "Store", "Double")
    assert t.status.startswith("Translated") and got == [162, 162, 162, 162]


def test_calculated_column_count_over_no_rows_is_blank(model):
    _with_calc_columns(model, "Sales", **{"Huge": "COUNTROWS(FILTER(Sales, Sales[Amount] > 1000))"})
    t, got = _calc_values(model, "Sales", "Huge")
    assert t.status.startswith("Translated") and got == [None] * 6       # DAX: BLANK, not 0


def test_calculated_column_over_a_calculated_table_needs_review(model):
    model["tables"].append({"name": "Calc", "calculated": True, "calc_dax": "DISTINCT(Sales[StoreID])"})
    model["columns"].append({"table": "Calc", "column": "StoreID", "data_type": "Int64"})
    _with_calc_columns(model, "Store", **{"Calc Rows": "COUNTROWS(Calc)"})
    tr, _ = _tr(model)
    t = tr.calc_column("Store", "Calc Rows", inline_calc=True)
    assert t.status == "Needs review" and any("no generated view" in r for r in t.reviews)


def test_true_false_calculated_column_is_valid_tsql(model):
    from sqlglot import parse_one
    from pbi_insights.analyze.querygen import Naming
    from pbi_insights.analyze.sources import analyze_sources, build_view
    _with_calc_columns(model, "Sales", Big="Sales[Amount] > AVERAGE(Sales[Amount])")
    tr, _ = _tr(model)
    sales = next(t for t in model["tables"] if t["name"] == "Sales")
    view = build_view(sales, model, analyze_sources(model), tr, Naming())
    assert view["dialect"] == "tsql"
    body = view["sql"].split("CREATE VIEW vw_sales AS", 1)[1].rstrip(";")
    parse_one(body, read="tsql")
    assert "THEN 1" in body and "THEN 0" in body, body


# ---- CALCULATE filter semantics ----------------------------------------------------------------------
def _measure_values(model, names, rows=None):
    """Evaluate measures (no visual filters) on a small Sales table: (StoreID, Scenario, Amount)."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE vw_sales (StoreID INTEGER, Scenario INTEGER, Amount REAL, Day TEXT)")
    con.execute("CREATE VIEW Sales AS SELECT * FROM vw_sales")      # filter-independent values read 'Sales'
    rows = rows or [(1, 1, 10.0), (1, 1, 20.0), (1, 2, 5.0), (2, 1, 30.0), (3, 1, 7.0), (3, 2, 9.0)]
    con.executemany("INSERT INTO vw_sales VALUES (?, ?, ?, ?)",
                    [r if len(r) == 4 else (*r, "2026-01-15 00:00:00") for r in rows])
    out = {}
    for name in names:
        t, s = sql(model, name, "sqlite")
        out[name] = (t, con.execute(f"SELECT {s} FROM vw_sales AS Sales").fetchone()[0])
    return out


def test_keepfilters_intersects_with_an_outer_filter(model):
    _with_measures(model, **{
        "Keep": "CALCULATE(CALCULATE([Total], KEEPFILTERS(Sales[Scenario] = 1)), Sales[Scenario] = 2)",
        "Replace": "CALCULATE(CALCULATE([Total], Sales[Scenario] = 1), Sales[Scenario] = 2)",
        "Both In One": "CALCULATE([Total], Sales[Scenario] = 1, Sales[Scenario] = 2)",
        "Outer Over TY": "CALCULATE([TY], Sales[Scenario] = 2)",
    })
    got = _measure_values(model, ["Keep", "Replace", "Both In One", "Outer Over TY"])
    assert got["Keep"][1] is None              # Scenario 1 AND 2: no rows, BLANK
    assert got["Replace"][1] == 67.0           # the inner filter replaces the outer one: Scenario 1
    assert got["Both In One"][1] is None       # filters of the same CALCULATE intersect
    assert got["Outer Over TY"][1] == 67.0     # [TY]'s own filter wins over the outer one
    assert all(t.status.startswith("Translated") for t, _ in got.values())


def test_comparisons_treat_blank_as_zero_like_dax(model):
    _with_measures(model, **{
        "Zero Rows": "CALCULATE(COUNTROWS(Sales), Sales[Amount] = 0)",
        "Small Rows": "CALCULATE(COUNTROWS(Sales), Sales[Amount] < 6)",
        "Big Rows": "CALCULATE(COUNTROWS(Sales), Sales[Amount] > 6)",
        "Not Ten": "CALCULATE(COUNTROWS(Sales), Sales[Amount] <> 10)",
        "Strict Zero": "CALCULATE(COUNTROWS(Sales), Sales[Amount] == 0)",
        "Zero Rows 2": "COUNTROWS(FILTER(Sales, 0 = Sales[Amount]))",
    })
    rows = [(1, 1, 10.0), (1, 1, None), (2, 1, 5.0), (2, 2, 0.0), (3, 1, None), (3, 2, 30.0)]
    got = {k: v[1] for k, v in _measure_values(model, ["Zero Rows", "Small Rows", "Big Rows", "Not Ten",
                                                       "Strict Zero", "Zero Rows 2"], rows).items()}
    # BLANK = 0 and BLANK < 6 are TRUE in DAX; BLANK > 6 is FALSE; == is strict
    assert got == {"Zero Rows": 3, "Small Rows": 4, "Big Rows": 2, "Not Ten": 5, "Strict Zero": 1,
                   "Zero Rows 2": 3}


def test_text_comparison_keeps_blank_rows_like_dax(model):
    import sqlite3
    _with_measures(model, **{"Not East": "CALCULATE(COUNTROWS(Store), Store[Region] <> \"East\")"})
    model["measures"][-1]["table"] = "Store"
    t, s = sql(model, "Not East", "sqlite")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE vw_store (StoreID INTEGER, Region TEXT)")
    con.executemany("INSERT INTO vw_store VALUES (?, ?)", [(1, "East"), (2, "West"), (3, None)])
    assert con.execute(f"SELECT {s} FROM vw_store AS Store").fetchone()[0] == 2   # West + the blank region


def test_filter_and_keepfilters_intersect_with_outer_filters(model):
    _with_measures(model, **{
        # FILTER(T, ...) is evaluated inside the outer filters, so it intersects (Scenario 1 AND 2: BLANK)
        "Filter In Outer": "CALCULATE(CALCULATE([Total], FILTER(Sales, Sales[Scenario] = 1)), Sales[Scenario] = 2)",
        # KEEPFILTERS(FILTER(ALL(T), ...)) still respects the outer filter (store 1): 20, not 50
        "Keep All": "CALCULATE(CALCULATE([Total], KEEPFILTERS(FILTER(ALL(Sales), Sales[Amount] > 10))), "
                    "Sales[StoreID] = 1)",
        # FILTER(ALL(T), ...) and a sibling filter in one CALCULATE intersect: 67, not 76
        "All And Sibling": "CALCULATE([Total], FILTER(ALL(Sales), Sales[Amount] > 6), Sales[Scenario] = 1)",
    })
    got = _measure_values(model, ["Filter In Outer", "Keep All", "All And Sibling"])
    assert got["Filter In Outer"][1] is None
    assert got["Keep All"][1] == 20.0
    assert got["All And Sibling"][1] == 67.0
    assert all(t.status.startswith("Translated") for t, _ in got.values()), {k: v[0].reviews for k, v in got.items()}


def test_comparisons_with_blank_function_follow_dax(model):
    rows = [(1, 1, 10.0), (1, 1, None), (2, 1, 5.0), (2, 2, 0.0), (3, 1, None), (3, 2, 30.0)]
    _with_measures(model, **{
        "Not Blank": "COUNTROWS(FILTER(Sales, Sales[Amount] <> BLANK()))",     # neither blank nor 0
        "Is Blank": "COUNTROWS(FILTER(Sales, Sales[Amount] = BLANK()))",       # blank or 0
        "Strict Blank": "COUNTROWS(FILTER(Sales, Sales[Amount] == BLANK()))",  # blank only
        "Total Is Blank": "IF([Total] = BLANK(), 1, 0)",
        "Not Big": "CALCULATE(COUNTROWS(Sales), NOT(Sales[Amount] > 6))",      # BLANK > 6 is FALSE, NOT gives TRUE
        "Switch Zero": "SUMX(Sales, SWITCH(Sales[Amount], 0, 1, 0))",          # SWITCH compares with =
    })
    got = {k: v[1] for k, v in _measure_values(model, ["Not Blank", "Is Blank", "Strict Blank", "Total Is Blank",
                                                       "Not Big", "Switch Zero"], rows).items()}
    assert got == {"Not Blank": 3, "Is Blank": 3, "Strict Blank": 2, "Total Is Blank": 0, "Not Big": 4,
                   "Switch Zero": 3}


def test_blank_date_is_before_every_date(model):
    rows = [(1, 1, 1.0, "2025-06-01 00:00:00"), (1, 1, 1.0, None), (1, 1, 1.0, "2026-06-01 00:00:00")]
    _with_measures(model, **{"Before 2026": "COUNTROWS(FILTER(Sales, Sales[Day] < DATE(2026, 1, 1)))",
                             "From 2026": "COUNTROWS(FILTER(Sales, Sales[Day] >= DATE(2026, 1, 1)))"})
    got = {k: v[1] for k, v in _measure_values(model, ["Before 2026", "From 2026"], rows).items()}
    assert got == {"Before 2026": 2, "From 2026": 1}
