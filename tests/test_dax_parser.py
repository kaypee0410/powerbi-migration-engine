import pytest

from pbi_insights.analyze.dax_parser import DaxSyntaxError, functions_used, parse_dax


def test_parses_calculate_with_filter():
    ast = parse_dax("CALCULATE(SUM(Sales[Amount]), FILTER(ALL(Store), Store[Area] > 100))")
    assert ast[0] == "func" and ast[1] == "CALCULATE"
    assert functions_used(ast) == {"CALCULATE", "SUM", "FILTER", "ALL"}


def test_parses_var_return_and_operators():
    ast = parse_dax("VAR x = [Total] RETURN IF(x <> 0, x * 2, BLANK())")
    assert ast[0] == "var"
    assert {"IF", "BLANK"} <= functions_used(ast)


def test_quoted_table_and_string_concat():
    ast = parse_dax("'Date Table'[Year] & \"-\" & FORMAT('Date Table'[Date], \"MMM\")")
    assert ast[0] == "bin"
    assert "FORMAT" in functions_used(ast)


def test_syntax_error_is_reported():
    with pytest.raises(DaxSyntaxError):
        parse_dax("SUM(Sales[Amount]")
