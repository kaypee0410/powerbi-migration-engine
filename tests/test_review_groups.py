from types import SimpleNamespace

from pbi_insights.analyze.review_groups import dax_marks, group_key, group_reviews, sql_marks


def test_group_keys_from_translator_reasons():
    assert group_key("VALUES is not translated automatically") == "VALUES"
    assert group_key("time intelligence (TOTALYTD): needs a date-offset join to a date table") == "Time intelligence"
    assert group_key("measure [X] evaluated per row (context transition): needs a grouped subquery") == "Context transition"
    assert group_key("COUNTROWS over a computed table") == "COUNTROWS over a computed table"
    assert group_key("REMOVEFILTERS(...) removes filters: needs a window function or a separate dataset") \
        == "Removes filters (ALL / REMOVEFILTERS)"
    assert group_key("TREATAS changes how tables relate: build the join explicitly") == "TREATAS"
    assert group_key("SUMX over a grouped or computed table: needs a GROUP BY subquery") == "Iterator over a grouped table"
    assert group_key("unknown column Sales[Nope]") == "Unknown name"
    assert group_key("something new") == "Other"


def test_marks_point_at_the_blockers():
    dax = "CALCULATE([Total], VALUES(Item[SKU]))"
    (s, e), = dax_marks(dax, ["VALUES is not translated automatically"])
    assert dax[s:e] == "VALUES"
    sql = "SUM(x) + NULL /* REVIEW: VALUES is not translated automatically */"
    (s, e), = sql_marks(sql)
    assert sql[s:e].startswith("NULL /* REVIEW") and sql[s:e].endswith("*/")


def test_every_review_measure_is_in_exactly_one_group():
    def m(name, reviews):
        return {"name": name, "dax": "", "sql": "", "translation": SimpleNamespace(reviews=reviews)}
    groups = group_reviews([m("a", ["VALUES is not translated automatically"]),
                            m("b", ["VALUES used as a value", "COUNTROWS over a computed table"]),
                            m("c", ["something new"]), m("d", [])])
    names = [x["name"] for g in groups for x in g["measures"]]
    assert sorted(names) == ["a", "b", "c"]
    assert [g["pattern"] for g in groups] == ["VALUES", "Other"]          # biggest first, Other last
    assert all(g["why"] and g["fix"] for g in groups)
