"""Date functions must run on SQLite (used to test-run every insight query) and match DAX."""
import sqlite3

import pytest

from pbi_insights.analyze.ir import Col, Fn, Lit, Null
from pbi_insights.analyze.sqlrender import Renderer, column

D = Col("t", "d")


def value(fn, d=None):
    s = Renderer("sqlite", col_fn=lambda c: column(c.column, "t")).r(fn).sql(dialect="sqlite")
    return sqlite3.connect(":memory:").execute(f"SELECT {s} FROM (SELECT ? AS d) AS t", (d,)).fetchone()[0]


@pytest.mark.parametrize("name, d, want", [
    ("YEAR", "2025-01-31 13:45:10", 2025), ("MONTH", "2025-01-31 13:45:10", 1), ("DAY", "2025-01-31 13:45:10", 31),
    ("HOUR", "2025-01-31 13:45:10", 13), ("MINUTE", "2025-01-31 13:45:10", 45),
    ("SECOND", "2025-01-31 13:45:10", 10),
    ("QUARTER", "2025-03-31 00:00:00", 1), ("QUARTER", "2025-04-01 00:00:00", 2),
    ("QUARTER", "2025-12-01 00:00:00", 4),
])
def test_date_parts(name, d, want):
    assert value(Fn(name, (D,)), d) == want


@pytest.mark.parametrize("y, m, day, want", [
    (2025, 12, 31, "2025-12-31 00:00:00"),
    (2025, 13, 1, "2026-01-01 00:00:00"),     # DAX rolls months over
    (2025, 3, 0, "2025-02-28 00:00:00"),      # day 0 = last day of the previous month
    (2024, 0, 15, "2023-12-15 00:00:00"),
])
def test_date_constructor_matches_dax(y, m, day, want):
    assert value(Fn("DATE", (Lit(y), Lit(m), Lit(day)))) == want


@pytest.mark.parametrize("name, d, n, want", [
    ("EOMONTH", "2025-01-31 13:45:10", 0, "2025-01-31 00:00:00"),
    ("EOMONTH", "2025-01-15 00:00:00", 1, "2025-02-28 00:00:00"),
    ("EOMONTH", "2025-01-15 00:00:00", -2, "2024-11-30 00:00:00"),
    ("EDATE", "2025-01-31 00:00:00", 1, "2025-02-28 00:00:00"),   # clamped to the shorter month
    ("EDATE", "2024-03-31 00:00:00", -1, "2024-02-29 00:00:00"),
    ("EDATE", "2024-02-29 00:00:00", 12, "2025-02-28 00:00:00"),
    ("EDATE", "2025-01-15 00:00:00", 2, "2025-03-15 00:00:00"),
])
def test_month_arithmetic_matches_dax(name, d, n, want):
    assert value(Fn(name, (D, Lit(n))), d) == want


def test_dates_compare_with_stored_values():
    # saved data is stored as 'YYYY-MM-DD HH:MM:SS'; a DATE() result must compare correctly against it
    assert value(Fn("DATE", (Lit(2025), Lit(12), Lit(31)))) == "2025-12-31 00:00:00"


@pytest.mark.parametrize("fn", [
    Fn("YEAR", (Null(),)),
    Fn("DATE", (Null(), Lit(1), Lit(1))), Fn("DATE", (Lit(2025), Null(), Lit(1))), Fn("DATE", (Lit(2025), Lit(1), Null())),
    Fn("EOMONTH", (Null(), Lit(0))), Fn("EOMONTH", (D, Null())),
    Fn("EDATE", (Null(), Lit(1))), Fn("EDATE", (D, Null())),
])
def test_blank_argument_gives_null_not_year_zero(fn):
    # SQLite's PRINTF('%d', NULL) prints 0; a BLANK must not turn into 0000-01-01 or 'this month'
    # (NULL, like DATEFROMPARTS in the other dialects; DAX's BLANK-as-0 rules are not reproduced)
    assert value(fn, "2025-03-15 00:00:00") is None


def test_today_is_midnight_in_the_stored_format():
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} 00:00:00", value(Fn("CURRENT_DATE", ())))
