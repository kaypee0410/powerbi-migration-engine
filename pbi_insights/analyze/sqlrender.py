"""Render IR nodes as sqlglot expressions, so one translation prints in any SQL dialect."""
from __future__ import annotations

import re

from sqlglot import exp

from .ir import (Agg, BlankArith, Bin, Case, Col, Fn, InList, IsNull, Lit, Null, Review, SafeDiv, Scalar, Star, Un)

RESERVED = {
    "ADD", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "BETWEEN", "BY", "CASE", "CAST", "CHECK", "COLUMN",
    "CONSTRAINT", "CREATE", "CROSS", "CURRENT", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
    "CURRENT_USER", "DATABASE", "DATE", "DEFAULT", "DELETE", "DESC", "DISTINCT", "DROP", "ELSE", "END", "ESCAPE",
    "EXCEPT", "EXISTS", "FALSE", "FETCH", "FILE", "FOR", "FOREIGN", "FROM", "FULL", "FUNCTION", "GRANT", "GROUP",
    "HAVING", "IDENTITY", "IF", "IN", "INDEX", "INNER", "INSERT", "INTERSECT", "INTERVAL", "INTO", "IS", "JOIN",
    "KEY", "LEFT", "LIKE", "LIMIT", "NATURAL", "NOT", "NULL", "OF", "OFFSET", "ON", "OR", "ORDER", "OUTER", "OVER",
    "PERCENT", "PIVOT", "PLAN", "PRIMARY", "PROCEDURE", "PUBLIC", "REFERENCES", "RIGHT", "ROW", "ROWS", "SCHEMA",
    "SELECT", "SESSION_USER", "SET", "SOME", "TABLE", "TABLESAMPLE", "THEN", "TIME", "TIMESTAMP", "TO", "TOP",
    "TRUE", "UNION", "UNIQUE", "UNPIVOT", "UPDATE", "USER", "USING", "VALUES", "VIEW", "WHEN", "WHERE", "WINDOW",
    "WITH"}
_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PREC = {"OR": 1, "AND": 2, "NOT": 3, "=": 4, "<>": 4, "<": 4, ">": 4, "<=": 4, ">=": 4, "+": 5, "-": 5,
        "*": 6, "/": 6}
BIN = {"+": exp.Add, "-": exp.Sub, "*": exp.Mul, "/": exp.Div, "=": exp.EQ, "<>": exp.NEQ, "<": exp.LT,
       ">": exp.GT, "<=": exp.LTE, ">=": exp.GTE, "AND": exp.And, "OR": exp.Or}
AGG_CLS = {"SUM": exp.Sum, "AVG": exp.Avg, "MIN": exp.Min, "MAX": exp.Max, "MEDIAN": exp.Median}


def snake(name: str) -> str:
    s = str(name).replace("%", " pct ").replace("$", " usd ").replace("#", " num ").replace("/", " per ")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s if s and not s[0].isdigit() else f"c_{s}"


def needs_quote(name: str) -> bool:
    return not _SAFE.match(name or "") or name.upper() in RESERVED


def ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=needs_quote(name))


def column(name: str, table: str | None = None) -> exp.Column:
    return exp.Column(this=ident(name), table=ident(table) if table else None)


def table_ref(name: str, alias: str | None = None) -> exp.Table:
    parts = name.split(".")
    t = exp.Table(this=ident(parts[-1]), db=ident(parts[-2]) if len(parts) > 1 else None)
    if alias:
        t.set("alias", exp.TableAlias(this=ident(alias)))
    return t


def literal(v) -> exp.Expression:
    if v is None:
        return exp.Null()
    if isinstance(v, bool):
        return exp.Boolean(this=v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return exp.Literal.number(v)
    return exp.Literal.string(str(v))


def not_null(e: exp.Expression) -> exp.Expression:
    return exp.Not(this=exp.Is(this=e, expression=exp.Null()))


def _prec(node) -> int:
    if isinstance(node, Bin):
        return PREC.get(node.op, 9)
    if isinstance(node, BlankArith):
        return 5
    if isinstance(node, SafeDiv):
        return 6
    if isinstance(node, Un):
        return 3 if node.op == "NOT" else 8
    return 9


class Renderer:
    """IR -> sqlglot. `col_fn(Col)` places columns; `agg_fn(Agg)` may redirect an aggregate to a
    pre-computed column (multi-grain queries); `scalar_fn(Scalar)` builds filter-independent subqueries."""

    def __init__(self, dialect: str, col_fn, agg_fn=None, scalar_fn=None):
        self.dialect = dialect
        self.col_fn = col_fn
        self.agg_fn = agg_fn
        self.scalar_fn = scalar_fn

    def _child(self, node, parent_prec: int, right: bool = False, strict_ops=("-", "/")) -> exp.Expression:
        e = self.r(node)
        p = _prec(node)
        if p < parent_prec or (right and p == parent_prec):
            return exp.Paren(this=e)
        return e

    def r(self, n) -> exp.Expression:
        if isinstance(n, Col):
            return self.col_fn(n)
        if isinstance(n, Lit):
            return literal(n.value)
        if isinstance(n, Null):
            return exp.Null()
        if isinstance(n, Star):
            return exp.Star()
        if isinstance(n, Agg):
            return self.agg(n)
        if isinstance(n, Bin):
            p = PREC.get(n.op, 9)
            return BIN[n.op](this=self._child(n.left, p), expression=self._child(n.right, p, right=n.op in ("-", "/")))
        if isinstance(n, Un):
            if n.op == "NOT":
                return exp.Not(this=self._child(n.x, 4))
            return exp.Neg(this=self._child(n.x, 8))
        if isinstance(n, Case):
            return exp.Case(ifs=[exp.If(this=self.r(c), true=self.r(v)) for c, v in n.whens],
                            default=None if n.default is None or isinstance(n.default, Null) else self.r(n.default))
        if isinstance(n, SafeDiv):
            num = self._child(n.num, 6)
            den = self.r(n.den)
            if n.alt is not None:
                return exp.Case(ifs=[exp.If(this=exp.Or(this=exp.EQ(this=den, expression=literal(0)),
                                                        expression=exp.Is(this=den.copy(), expression=exp.Null())),
                                            true=self.r(n.alt))],
                                default=exp.Div(this=num, expression=exp.Paren(this=den.copy()) if _prec(n.den) < 7 else den.copy()))
            return exp.Div(this=num, expression=exp.Nullif(this=den, expression=literal(0)))
        if isinstance(n, BlankArith):
            cls = exp.Add if n.op == "+" else exp.Sub
            return cls(this=self._coalesce0(n.left), expression=self._coalesce0(n.right))
        if isinstance(n, IsNull):
            return exp.Is(this=self._child(n.x, 5), expression=exp.Null())
        if isinstance(n, InList):
            return exp.In(this=self.r(n.x), expressions=[self.r(v) for v in n.values])
        if isinstance(n, Fn):
            return self.fn(n)
        if isinstance(n, Scalar):
            if self.scalar_fn:
                return self.scalar_fn(n)
            e = exp.Null()
            e.add_comments([" REVIEW: value that ignores filters "])
            return e
        if isinstance(n, Review):
            e = exp.Null()
            e.add_comments([f" REVIEW: {n.reason} "])
            return e
        raise TypeError(f"Cannot render {n!r}")

    def _coalesce0(self, n) -> exp.Expression:
        if isinstance(n, Lit):
            return self.r(n)
        return exp.Coalesce(this=self.r(n), expressions=[literal(0)])

    def agg(self, a: Agg) -> exp.Expression:
        if self.agg_fn:
            res = self.agg_fn(a)
            if res is not None:
                return res
        if isinstance(a.arg, Star):
            inner = exp.Star() if a.pred is None else exp.Case(ifs=[exp.If(this=self.r(a.pred), true=literal(1))])
            return exp.Count(this=inner)
        x = self.r(a.arg)
        if a.pred is not None:
            x = exp.Case(ifs=[exp.If(this=self.r(a.pred), true=x)])
        if a.func == "COUNT":
            return exp.Count(this=exp.Distinct(expressions=[x]) if a.distinct else x)
        return AGG_CLS[a.func](this=x)

    def fn(self, n: Fn) -> exp.Expression:
        a = [self.r(x) for x in n.args]
        name, d = n.name, self.dialect
        if name == "COALESCE":
            return exp.Coalesce(this=a[0], expressions=a[1:])
        if name == "CONCAT":
            return exp.Concat(expressions=a, coalesce=True)
        if d == "sqlite":
            res = _sqlite_date_fn(name, a)
            if res is not None:
                return res
        if name in ("YEAR", "MONTH", "DAY"):
            return {"YEAR": exp.Year, "MONTH": exp.Month, "DAY": exp.Day}[name](this=a[0])
        if name in ("QUARTER", "HOUR", "MINUTE", "SECOND", "WEEKDAY", "WEEKNUM"):
            if d == "tsql":
                part = {"WEEKDAY": "WEEKDAY", "WEEKNUM": "WEEK"}.get(name, name)
                return exp.Anonymous(this="DATEPART", expressions=[exp.var(part), a[0]])
            if name == "QUARTER":
                return exp.Quarter(this=a[0])
            return exp.Anonymous(this={"WEEKDAY": "DAYOFWEEK", "WEEKNUM": "WEEKOFYEAR"}.get(name, name), expressions=a)
        if name == "TIMEFMT":
            return exp.TimeToStr(this=a[0], format=a[1])
        if name == "TOSTRING":
            return exp.Cast(this=a[0], to=exp.DataType.build("VARCHAR"))
        if name == "TONUMBER":
            return exp.Cast(this=a[0], to=exp.DataType.build("DOUBLE"))
        if name == "TIMESTAMP_LIT":
            return exp.Cast(this=a[0], to=exp.DataType.build("TIMESTAMP"))
        if name == "DATE":
            return exp.DateFromParts(year=a[0], month=a[1], day=a[2])
        if name == "CURRENT_DATE":
            return exp.CurrentDate()
        if name == "CURRENT_TIMESTAMP":
            return exp.CurrentTimestamp()
        if name == "DATEDIFF":
            unit = n.args[0].value if isinstance(n.args[0], Lit) else "DAY"
            return exp.DateDiff(this=a[2], expression=a[1], unit=exp.var(str(unit)))
        if name in ("LEFT", "RIGHT"):
            return (exp.Left if name == "LEFT" else exp.Right)(this=a[0], expression=a[1])
        if name == "SUBSTRING":
            return exp.Substring(this=a[0], start=a[1], length=a[2] if len(a) > 2 else None)
        if name == "LENGTH":
            return exp.Length(this=a[0])
        if name in ("UPPER", "LOWER", "TRIM"):
            return {"UPPER": exp.Upper, "LOWER": exp.Lower, "TRIM": exp.Trim}[name](this=a[0])
        if name == "ROUND":
            return exp.Round(this=a[0], decimals=a[1] if len(a) > 1 else None)
        if name in ("ABS", "FLOOR", "CEIL", "SQRT", "EXP", "LN"):
            return {"ABS": exp.Abs, "FLOOR": exp.Floor, "CEIL": exp.Ceil, "SQRT": exp.Sqrt, "EXP": exp.Exp,
                    "LN": exp.Ln}[name](this=a[0])
        if name == "POWER":
            return exp.Pow(this=a[0], expression=a[1])
        if name == "MOD":
            return exp.Mod(this=a[0], expression=a[1])
        if name in ("GREATEST", "LEAST"):
            return (exp.Greatest if name == "GREATEST" else exp.Least)(this=a[0], expressions=a[1:])
        if name.startswith("LIKE_"):
            v = n.args[1]
            pattern = {"LIKE_CONTAINS": "%{}%", "LIKE_STARTS": "{}%", "LIKE_ENDS": "%{}"}[name]
            if isinstance(v, Lit):
                return exp.Like(this=a[0], expression=literal(pattern.format(v.value)))
            pieces = {"LIKE_CONTAINS": [literal("%"), a[1], literal("%")], "LIKE_STARTS": [a[1], literal("%")],
                      "LIKE_ENDS": [literal("%"), a[1]]}[name]
            return exp.Like(this=a[0], expression=exp.Concat(expressions=pieces, coalesce=True))
        return exp.Anonymous(this=name, expressions=a)


_STRFTIME_PART = {"YEAR": "%Y", "MONTH": "%m", "DAY": "%d", "HOUR": "%H", "MINUTE": "%M", "SECOND": "%S"}


def _sqlite_date_fn(name: str, a: list) -> exp.Expression | None:
    """SQLite has no YEAR/MONTH/DATEFROMPARTS/EDATE/EOMONTH: build them from STRFTIME and DATETIME modifiers.

    Dates are stored as 'YYYY-MM-DD HH:MM:SS' text (see profile._sqlite_frame), so every date result is
    produced with DATETIME() in that same format and compares correctly as text.
    """
    def fn(f, *args):
        return exp.Anonymous(this=f, expressions=list(args))

    def int_(e):
        return exp.Cast(this=e, to=exp.DataType.build("INT"))

    def part(fmt, x):
        return int_(fn("STRFTIME", literal(fmt), x))

    def months(n):                         # '+3 months' / '-1 months'
        return fn("PRINTF", literal("%+d months"), n)

    def blank_safe(result, args):
        # PRINTF('%d', NULL) prints 0, so a BLANK argument would silently become 0: return NULL instead,
        # like DATEFROMPARTS / DATEADD do in the other dialects.
        cond = None
        for x in args:
            if isinstance(x, exp.Literal):
                continue
            c = exp.Is(this=x.copy(), expression=exp.Null())
            cond = c if cond is None else exp.Or(this=cond, expression=c)
        return result if cond is None else exp.Case(ifs=[exp.If(this=cond, true=exp.Null())], default=result)

    if name == "CURRENT_DATE":            # TODAY(): midnight, in the stored 'YYYY-MM-DD HH:MM:SS' format
        return fn("DATETIME", literal("now"), literal("localtime"), literal("start of day"))
    if name in _STRFTIME_PART:
        return part(_STRFTIME_PART[name], a[0])
    if name == "QUARTER":
        return int_(exp.Div(this=exp.Paren(this=exp.Add(this=part("%m", a[0]), expression=literal(2))),
                            expression=literal(3)))
    if name == "DATE":
        # DAX DATE rolls over like Excel: DATE(2025, 13, 1) = 2026-01-01, DATE(2025, 3, 0) = 2025-02-28.
        sub1 = lambda e: exp.Paren(this=exp.Sub(this=e, expression=literal(1)))  # noqa: E731
        return blank_safe(fn("DATETIME", fn("PRINTF", literal("%04d-01-01"), a[0]), months(sub1(a[1])),
                             fn("PRINTF", literal("%+d days"), sub1(a[2]))), a[:3])
    if name == "EOMONTH":
        return blank_safe(fn("DATETIME", a[0], literal("start of month"),
                             months(exp.Add(this=a[1], expression=literal(1))), literal("-1 day")), a[:2])
    if name == "EDATE":
        # Same day n months later, clamped to the end of a shorter month (EDATE(Jan 31, 1) = Feb 28).
        last_day = part("%d", fn("DATETIME", a[0], literal("start of month"),
                                 months(exp.Add(this=a[1], expression=literal(1))), literal("-1 day")))
        day = exp.Least(this=part("%d", a[0]), expressions=[last_day])   # SQLite renders this as MIN(a, b)
        return blank_safe(fn("DATETIME", a[0], literal("start of month"), months(a[1]),
                             fn("PRINTF", literal("%+d days"), exp.Sub(this=day, expression=literal(1)))), a[:2])
    return None


def to_sql(e: exp.Expression, dialect: str, pretty: bool = True) -> str:
    return e.sql(dialect=dialect, pretty=pretty)
