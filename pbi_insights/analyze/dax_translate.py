"""DAX -> IR translator (rule-based).

Covers the patterns most report measures use: aggregations, CALCULATE with boolean / FILTER filters,
DIVIDE and arithmetic with DAX BLANK rules, IF / SWITCH, iterators over a table (SUMX ...),
SELECTEDVALUE / HASONEVALUE, FILTER(ALL(T), ...) as a filter-independent value, and the common scalar
text / date / math functions. Anything else becomes a `Review` node with a reason, so the workbook
can flag it instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .dax_parser import DaxSyntaxError, functions_used, parse_dax
from .ir import (Agg, BlankArith, Bin, Case, Col, Fn, InList, IsNull, Lit, Null, Review, SafeDiv, Scalar, Star,
                 Un, aggs_in, and_, has_agg, is_boolean, override, reviews_in)

AGG = {"SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT", "COUNTA": "COUNT",
       "MEDIAN": "MEDIAN"}
ITER = {"SUMX": "SUM", "AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX", "COUNTX": "COUNT", "COUNTAX": "COUNT",
        "MEDIANX": "MEDIAN"}
TIME_INTEL = {"DATEADD", "SAMEPERIODLASTYEAR", "PARALLELPERIOD", "DATESYTD", "DATESQTD", "DATESMTD",
              "DATESBETWEEN", "DATESINPERIOD", "TOTALYTD", "TOTALQTD", "TOTALMTD", "PREVIOUSDAY", "PREVIOUSMONTH",
              "PREVIOUSQUARTER", "PREVIOUSYEAR", "NEXTDAY", "NEXTMONTH", "NEXTQUARTER", "NEXTYEAR", "STARTOFMONTH",
              "STARTOFQUARTER", "STARTOFYEAR", "ENDOFMONTH", "ENDOFQUARTER", "ENDOFYEAR", "FIRSTDATE", "LASTDATE",
              "OPENINGBALANCEMONTH", "OPENINGBALANCEQUARTER", "OPENINGBALANCEYEAR", "CLOSINGBALANCEMONTH",
              "CLOSINGBALANCEQUARTER", "CLOSINGBALANCEYEAR"}
FILTER_MODIFIERS = {"ALL", "ALLEXCEPT", "ALLSELECTED", "ALLNOBLANKROW", "REMOVEFILTERS", "ALLCROSSFILTERED"}
RELATIONSHIP_FNS = {"USERELATIONSHIP", "CROSSFILTER", "TREATAS"}
SIMPLE_SCALAR = {"ABS": "ABS", "SQRT": "SQRT", "EXP": "EXP", "LN": "LN", "LOG10": "LOG10", "SIGN": "SIGN",
                 "INT": "FLOOR", "YEAR": "YEAR", "MONTH": "MONTH", "DAY": "DAY", "QUARTER": "QUARTER",
                 "HOUR": "HOUR", "MINUTE": "MINUTE", "SECOND": "SECOND", "UPPER": "UPPER", "LOWER": "LOWER",
                 "TRIM": "TRIM", "LEN": "LENGTH", "VALUE": "TONUMBER", "WEEKDAY": "WEEKDAY", "WEEKNUM": "WEEKNUM",
                 "EOMONTH": "EOMONTH", "EDATE": "EDATE"}
CMP_OPS = {"=", "==", "<>", "<", ">", "<=", ">="}
DATE_TOKENS = [("yyyy", "%Y"), ("yy", "%y"), ("mmmm", "%B"), ("MMMM", "%B"), ("mmm", "%b"), ("MMM", "%b"),
               ("dddd", "%A"), ("ddd", "%a"), ("mm", "%m"), ("MM", "%m"), ("dd", "%d"), ("hh", "%H"),
               ("HH", "%H"), ("nn", "%M"), ("ss", "%S"), ("m", "%m"), ("M", "%m"), ("d", "%d")]


# ----------------------------------------------------------------------------------------- model index
class ModelIndex:
    """Case-insensitive lookups over a normalized model (DAX names are case-insensitive)."""

    def __init__(self, model: dict):
        self.model = model
        self.tables = {t["name"]: t for t in model.get("tables", [])}
        self._tables_ci = {n.lower(): n for n in self.tables}
        self.columns = {(c["table"], c["column"]): c for c in model.get("columns", [])}
        self._cols_ci = {(t.lower(), c.lower()): (t, c) for (t, c) in self.columns}
        self.measures = {m["name"]: m for m in model.get("measures", [])}
        self._meas_ci = {n.lower(): n for n in self.measures}

    def table(self, name: str | None) -> str | None:
        return self._tables_ci.get((name or "").lower())

    def column(self, table: str | None, column: str | None) -> tuple[str, str] | None:
        return self._cols_ci.get(((table or "").lower(), (column or "").lower()))

    def measure(self, name: str | None) -> str | None:
        return self._meas_ci.get((name or "").lower())

    def is_calculated(self, table: str, column: str) -> bool:
        c = self.columns.get((table, column))
        return bool(c and c.get("dax"))


@dataclass
class Translation:
    ir: object = None
    status: str = "Translated"
    notes: list = field(default_factory=list)
    reviews: list = field(default_factory=list)
    patterns: list = field(default_factory=list)
    measure_refs: list = field(default_factory=list)
    column_refs: list = field(default_factory=list)
    functions: list = field(default_factory=list)
    error: str | None = None
    explanation: str = ""
    is_alias: bool = False


@dataclass
class _Ctx:
    host: str
    mode: str = "measure"            # "measure" or "row"
    pred: object = None
    row_tables: tuple = ()
    vars: tuple = ()                 # ((name, ir), ...)
    any_table: bool = False          # predicate context: any column may be referenced row by row
    inline_calc: bool = False        # views: expand calculated columns into their SQL

    def var(self, name: str):
        for n, v in reversed(self.vars):
            if n.lower() == name.lower():
                return v
        return None


class _Acc:
    def __init__(self):
        self.notes, self.reviews, self.patterns = [], [], []
        self.measure_refs, self.column_refs = [], []

    def add(self, bucket: list, item):
        if item not in bucket:
            bucket.append(item)


# ----------------------------------------------------------------------------------------- translator
class Translator:
    def __init__(self, idx: ModelIndex, graph=None):
        self.idx = idx
        self.graph = graph
        self._ast_cache: dict = {}
        self._measure_cache: dict = {}
        self._ir_cache: dict = {}
        self._stack: list = []
        self.acc = _Acc()

    # ---- public ---------------------------------------------------------------------------------
    def measure(self, name: str) -> Translation:
        canon = self.idx.measure(name) or name
        if canon in self._measure_cache:
            return self._measure_cache[canon]
        m = self.idx.measures.get(canon)
        if not m:
            return Translation(status="Not found", error=f"Measure {name} not found")
        res = self._run(m["dax"], _Ctx(host=m["table"]), self_name=canon)
        self._measure_cache[canon] = res
        return res

    def calc_column(self, table: str, column: str, inline_calc: bool = False) -> Translation:
        c = self.idx.columns.get((table, column))
        if not c or not c.get("dax"):
            return Translation(status="Not calculated")
        return self._run(c["dax"], _Ctx(host=table, mode="row", row_tables=(table,), inline_calc=inline_calc),
                         self_name=f"{table}[{column}]")

    def expression(self, dax: str, host: str, mode: str = "measure") -> Translation:
        return self._run(dax, _Ctx(host=host, mode=mode, row_tables=(host,) if mode == "row" else ()))

    # ---- machinery ---------------------------------------------------------------------------------
    def _parse(self, dax: str):
        if dax not in self._ast_cache:
            self._ast_cache[dax] = parse_dax(dax)
        return self._ast_cache[dax]

    def _run(self, dax: str, ctx: _Ctx, self_name: str | None = None) -> Translation:
        res = Translation()
        try:
            ast = self._parse(dax)
        except DaxSyntaxError as ex:
            res.status, res.error = "Parse error", str(ex)
            res.reviews.append(f"DAX could not be parsed: {ex}")
            return res
        outer_acc, self.acc = self.acc, _Acc()
        if self_name:
            self._stack.append(self_name)
        try:
            res.ir = self.tr(ast, ctx)
        except RecursionError:
            res.ir = Review("expression too deeply nested")
        finally:
            if self_name:
                self._stack.pop()
            acc, self.acc = self.acc, outer_acc
        res.notes, res.patterns = acc.notes, acc.patterns
        res.reviews = acc.reviews + [r for r in reviews_in(res.ir) if r not in acc.reviews]
        res.measure_refs, res.column_refs = acc.measure_refs, acc.column_refs
        res.functions = sorted(functions_used(ast))
        res.explanation = explain(ast)
        if ast[0] == "ref" and self.idx.measure(ast[1]):
            res.is_alias = True
            res.patterns.insert(0, "Alias")
        res.status = "Needs review" if res.reviews else ("Translated with notes" if res.notes else "Translated")
        return res

    def note(self, text: str):
        self.acc.add(self.acc.notes, text)

    def pattern(self, text: str):
        self.acc.add(self.acc.patterns, text)

    def review(self, reason: str) -> Review:
        self.acc.add(self.acc.reviews, reason)
        return Review(reason)

    # ---- node dispatch -----------------------------------------------------------------------------
    def tr(self, n, ctx: _Ctx):
        k = n[0]
        if k == "num":
            return Lit(n[1])
        if k in ("str", "bool"):
            return Lit(n[1])
        if k == "empty":
            return Null()
        if k == "col":
            return self.column_ref(n[1], n[2], ctx)
        if k == "ref":
            m = self.idx.measure(n[1])
            if m:
                return self.measure_ref(m, ctx)
            return self.column_ref(ctx.host, n[1], ctx)
        if k == "name":
            v = ctx.var(n[1])
            if v is not None:
                return v
            if self.idx.table(n[1]):
                return self.review(f"table {n[1]} used where a value is expected")
            return self.review(f"unknown name {n[1]}")
        if k == "table":
            return self.review(f"table '{n[1]}' used where a value is expected")
        if k == "var":
            c = ctx
            for name, e in n[1]:
                c = replace(c, vars=c.vars + ((name, self.tr(e, c)),))
            return self.tr(n[2], c)
        if k == "neg":
            x = self.tr(n[1], ctx)
            return Lit(-x.value) if isinstance(x, Lit) and isinstance(x.value, (int, float)) else Un("NEG", x)
        if k == "not":
            return Un("NOT", self.cond(n[1], ctx))
        if k == "in":
            return self.in_list(n[1], n[2], ctx)
        if k == "bin":
            return self.binary(n[1], n[2], n[3], ctx)
        if k == "func":
            return self.func(n[1], n[2], ctx)
        return self.review(f"unsupported construct {k}")

    # ---- references ----------------------------------------------------------------------------------
    def column_ref(self, table, column, ctx: _Ctx):
        res = self.idx.column(table, column)
        if not res:
            return self.review(f"unknown column {table}[{column}]")
        t, c = res
        self.acc.add(self.acc.column_refs, f"{t}[{c}]")
        if ctx.mode == "row" or ctx.any_table or t in ctx.row_tables:
            if ctx.inline_calc and self.idx.is_calculated(t, c):
                key = f"{t}[{c}]"
                if key in self._stack:
                    return self.review(f"circular calculated column {key}")
                self._stack.append(key)
                try:
                    return self.tr(self._parse(self.idx.columns[(t, c)]["dax"]), replace(ctx, host=t))
                finally:
                    self._stack.pop()
            return Col(t, c)
        return self.review(f"column {t}[{c}] used outside an aggregation (needs row context)")

    def measure_ref(self, name: str, ctx: _Ctx):
        self.acc.add(self.acc.measure_refs, name)
        if ctx.mode == "row":
            return self.review(f"measure [{name}] evaluated per row (context transition): needs a grouped subquery")
        if name in self._stack:
            return self.review(f"circular reference to [{name}]")
        key = (name, repr(ctx.pred))
        if key in self._ir_cache:
            ir, notes, patterns = self._ir_cache[key]
            for x in notes:
                self.note(x)
            for x in patterns:
                self.pattern(x)
            return ir
        m = self.idx.measures[name]
        n0, p0 = len(self.acc.notes), len(self.acc.patterns)
        self._stack.append(name)
        try:
            ir = self.tr(self._parse(m["dax"]), _Ctx(host=m["table"], pred=ctx.pred))
        except DaxSyntaxError as ex:
            ir = self.review(f"measure [{name}] could not be parsed: {ex}")
        finally:
            self._stack.pop()
        self._ir_cache[key] = (ir, self.acc.notes[n0:], self.acc.patterns[p0:])
        return ir

    def column_arg(self, n, ctx: _Ctx):
        """Argument that must be a column (SUM(T[c]), DISTINCTCOUNT([c]))."""
        if n[0] == "col":
            res = self.idx.column(n[1], n[2])
        elif n[0] == "ref" and not self.idx.measure(n[1]):
            res = self.idx.column(ctx.host, n[1])
        else:
            return None
        if res:
            self.acc.add(self.acc.column_refs, f"{res[0]}[{res[1]}]")
        return res

    def table_arg(self, n) -> str | None:
        if n[0] in ("table", "name"):
            return self.idx.table(n[1])
        return None

    # ---- operators ---------------------------------------------------------------------------------
    def cond(self, n, ctx: _Ctx):
        ir = self.tr(n, ctx)
        if is_boolean(ir) or isinstance(ir, Review):
            return ir
        return Bin("<>", Fn("COALESCE", (ir, Lit(0))), Lit(0))

    def binary(self, op, l, r, ctx: _Ctx):
        if op in ("&&", "||"):
            return Bin("AND" if op == "&&" else "OR", self.cond(l, ctx), self.cond(r, ctx))
        L, R = self.tr(l, ctx), self.tr(r, ctx)
        if op in ("+", "-"):
            return self.arith(op, L, R)
        if op == "*":
            return Bin("*", L, R)
        if op == "/":
            self.note("'/' in DAX returns Infinity/NaN on division by zero; the SQL returns NULL (NULLIF).")
            return SafeDiv(L, R)
        if op == "^":
            return Fn("POWER", (L, R))
        if op == "&":
            return self.concat(L, R)
        if op in CMP_OPS:
            return self.compare(op, L, R)
        return self.review(f"operator {op}")

    @staticmethod
    def concat(L, R):
        parts = []
        for x in (L, R):
            parts.extend(x.args if isinstance(x, Fn) and x.name == "CONCAT" else (x,))
        return Fn("CONCAT", tuple(parts))

    def arith(self, op, L, R):
        if isinstance(L, Agg) and isinstance(R, Agg) and L.func == R.func == "SUM" and L.table == R.table \
                and L.pred == R.pred and not L.distinct and not R.distinct:
            return Agg("SUM", Bin(op, _coalesce0(L.arg), _coalesce0(R.arg)), L.table, False, L.pred)
        if isinstance(L, Lit) and isinstance(R, Lit) and all(isinstance(x.value, (int, float)) for x in (L, R)):
            return Lit(L.value + R.value if op == "+" else L.value - R.value)
        if has_agg(L) or has_agg(R):
            self.note("Blank + / - blank gives 0 in the SQL where DAX gives BLANK (only matters when a row has no data on either side).")
        return BlankArith(op, L, R)

    def compare(self, op, L, R):
        sql_op = "=" if op in ("=", "==") else op
        if op != "==":
            if has_agg(L) and isinstance(R, Lit) and not isinstance(L, Lit):
                L = Fn("COALESCE", (L, Lit(0 if isinstance(R.value, (int, float)) else "")))
            elif has_agg(R) and isinstance(L, Lit):
                R = Fn("COALESCE", (R, Lit(0 if isinstance(L.value, (int, float)) else "")))
        return Bin(sql_op, L, R)

    def in_list(self, x, values, ctx: _Ctx):
        X = self.tr(x, ctx)
        if values[0] != "tablector":
            return self.review("IN against a table expression")
        items = []
        for v in values[1]:
            if v[0] == "tuple":
                return self.review("multi-column IN")
            items.append(self.tr(v, ctx))
        return InList(X, tuple(items))

    # ---- functions ---------------------------------------------------------------------------------
    def func(self, name: str, args: list, ctx: _Ctx):
        a = args
        if name in AGG:
            if name in ("MIN", "MAX") and len(a) == 2:
                return Fn("LEAST" if name == "MIN" else "GREATEST", (self.tr(a[0], ctx), self.tr(a[1], ctx)))
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review(f"{name} over an expression (use {name}X)")
            return Agg(AGG[name], Col(*col), col[0], False, ctx.pred)
        if name in ("DISTINCTCOUNT", "DISTINCTCOUNTNOBLANK", "APPROXIMATEDISTINCTCOUNT"):
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review(f"{name} over an expression")
            self.pattern("Distinct count")
            if name == "DISTINCTCOUNT":
                self.note("DISTINCTCOUNT counts BLANK as a value; COUNT(DISTINCT) ignores NULL.")
            return Agg("COUNT", Col(*col), col[0], True, ctx.pred)
        if name == "COUNTBLANK":
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review("COUNTBLANK over an expression")
            return Agg("SUM", Case(((IsNull(Col(*col)), Lit(1)),), Lit(0)), col[0], False, ctx.pred)
        if name == "COUNTROWS":
            return self.countrows(a[0] if a else ("empty",), ctx)
        if name in ITER:
            return self.iterator(name, a, ctx)
        if name == "CALCULATE":
            return self.calculate(a, ctx)
        if name == "DIVIDE":
            num, den = self.tr(a[0], ctx), self.tr(a[1], ctx)
            alt = self.tr(a[2], ctx) if len(a) > 2 and a[2][0] != "empty" else None
            self.pattern("Ratio")
            return SafeDiv(num, den, alt)
        if name == "IF":
            c = self.cond(a[0], ctx)
            then = self.tr(a[1], ctx) if len(a) > 1 else Null()
            other = self.tr(a[2], ctx) if len(a) > 2 else Null()
            simple = _guarded_division(c, then, other)
            if simple is not None:
                self.pattern("Ratio")
                return simple
            self.pattern("Conditional")
            return Case(((c, then),), other)
        if name == "SWITCH":
            return self.switch(a, ctx)
        if name in ("AND", "OR"):
            return Bin(name, self.cond(a[0], ctx), self.cond(a[1], ctx))
        if name == "NOT":
            return Un("NOT", self.cond(a[0], ctx))
        if name == "ISBLANK":
            return IsNull(self.tr(a[0], ctx))
        if name == "BLANK":
            return Null()
        if name in ("TRUE", "FALSE"):
            return Lit(name == "TRUE")
        if name == "COALESCE":
            return Fn("COALESCE", tuple(self.tr(x, ctx) for x in a))
        if name == "IFERROR":
            self.note("IFERROR dropped: SQL has no error trap; divisions are already NULL-safe.")
            return self.tr(a[0], ctx)
        if name == "SELECTEDVALUE":
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review("SELECTEDVALUE over an expression")
            alt = self.tr(a[1], ctx) if len(a) > 1 else Null()
            self.note("SELECTEDVALUE becomes 'the single value in each row of the visual' (COUNT DISTINCT = 1).")
            return Case(((Bin("=", Agg("COUNT", Col(*col), col[0], True, ctx.pred), Lit(1)),
                          Agg("MAX", Col(*col), col[0], False, ctx.pred)),), alt)
        if name == "HASONEVALUE":
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review("HASONEVALUE over an expression")
            return Bin("=", Agg("COUNT", Col(*col), col[0], True, ctx.pred), Lit(1))
        if name == "RELATED":
            col = self.column_arg(a[0], ctx) if a else None
            if not col:
                return self.review("RELATED over an expression")
            self.pattern("Uses RELATED (join)")
            return Col(*col) if ctx.mode == "row" or ctx.row_tables else self.review("RELATED outside row context")
        if name == "FORMAT":
            return self.format_(a, ctx)
        if name in ("CONCATENATE",):
            return self.concat(self.tr(a[0], ctx), self.tr(a[1], ctx))
        if name == "LEFT" or name == "RIGHT":
            n = self.tr(a[1], ctx) if len(a) > 1 else Lit(1)
            return Fn(name, (self.tr(a[0], ctx), n))
        if name == "MID":
            return Fn("SUBSTRING", tuple(self.tr(x, ctx) for x in a[:3]))
        if name == "SUBSTITUTE":
            return Fn("REPLACE", tuple(self.tr(x, ctx) for x in a[:3]))
        if name in ("ROUND", "ROUNDUP", "ROUNDDOWN", "MROUND"):
            digits = self.tr(a[1], ctx) if len(a) > 1 else Lit(0)
            if name != "ROUND":
                self.note(f"{name} approximated with ROUND.")
            return Fn("ROUND", (self.tr(a[0], ctx), digits))
        if name in ("CEILING", "FLOOR", "ISO.CEILING"):
            if len(a) > 1:
                self.note(f"{name} significance argument ignored (rounds to whole numbers).")
            return Fn("CEIL" if "CEILING" in name else "FLOOR", (self.tr(a[0], ctx),))
        if name in ("MOD", "POWER"):
            return Fn(name, (self.tr(a[0], ctx), self.tr(a[1], ctx)))
        if name == "TRUNC":
            return Fn("FLOOR", (self.tr(a[0], ctx),))
        if name == "DATE":
            return Fn("DATE", tuple(self.tr(x, ctx) for x in a[:3]))
        if name == "TODAY":
            return Fn("CURRENT_DATE", ())
        if name in ("NOW", "UTCNOW"):
            return Fn("CURRENT_TIMESTAMP", ())
        if name == "DATEDIFF":
            unit = a[2][1] if len(a) > 2 and a[2][0] in ("name", "str") else "DAY"
            return Fn("DATEDIFF", (Lit(str(unit).upper()), self.tr(a[0], ctx), self.tr(a[1], ctx)))
        if name in SIMPLE_SCALAR:
            if name in ("WEEKDAY", "WEEKNUM", "EOMONTH", "EDATE"):
                self.note(f"{name} has dialect-specific SQL: check the generated function.")
            return Fn(SIMPLE_SCALAR[name], tuple(self.tr(x, ctx) for x in a))
        if name == "CONTAINSSTRING" and len(a) == 2:
            return Fn("LIKE_CONTAINS", (self.tr(a[0], ctx), self.tr(a[1], ctx)))
        if name in TIME_INTEL:
            self.pattern("Time intelligence")
            return self.review(f"time intelligence ({name}): needs a date-offset join to a date table")
        if name in FILTER_MODIFIERS:
            return self.review(f"{name} used as a value")
        return self.review(f"{name} is not translated automatically")

    def switch(self, a, ctx: _Ctx):
        self.pattern("Conditional")
        if not a:
            return Null()
        first, rest = a[0], a[1:]
        default = self.tr(rest[-1], ctx) if len(rest) % 2 == 1 else Null()
        pairs = rest[:-1] if len(rest) % 2 == 1 else rest
        whens = []
        if first[0] == "bool" and first[1] is True or (first[0] == "func" and first[1] == "TRUE"):
            for i in range(0, len(pairs), 2):
                whens.append((self.cond(pairs[i], ctx), self.tr(pairs[i + 1], ctx)))
        else:
            subject = self.tr(first, ctx)
            for i in range(0, len(pairs), 2):
                whens.append((Bin("=", subject, self.tr(pairs[i], ctx)), self.tr(pairs[i + 1], ctx)))
        return Case(tuple(whens), default)

    def format_(self, a, ctx: _Ctx):
        x = self.tr(a[0], ctx)
        fmt = a[1][1] if len(a) > 1 and a[1][0] == "str" else None
        if fmt and fmt.strip() and all(ch.isalpha() or ch in " -/.,:" for ch in fmt) \
                and any(t in fmt for t in ("y", "m", "M", "d", "h", "H")):
            return Fn("TIMEFMT", (x, Lit(dax_date_format_to_strftime(fmt))))
        self.note(f"FORMAT(..., \"{fmt}\") is display formatting: set the number format in the BI tool instead.")
        return Fn("TOSTRING", (x,))

    # ---- aggregation over tables -------------------------------------------------------------------
    def countrows(self, n, ctx: _Ctx):
        t = self.table_arg(n)
        if t:
            return Agg("COUNT", Star(), t, False, ctx.pred)
        if n[0] == "func" and n[1] == "FILTER" and len(n[2]) == 2:
            t = self.table_arg(n[2][0])
            if t:
                cond = self.cond(n[2][1], replace(ctx, mode="row", row_tables=ctx.row_tables + (t,)))
                return Agg("COUNT", Star(), t, False, and_(ctx.pred, cond))
        if n[0] == "func" and n[1] in ("VALUES", "DISTINCT") and n[2]:
            col = self.column_arg(n[2][0], ctx)
            if col:
                return Agg("COUNT", Col(*col), col[0], True, ctx.pred)
        return self.review("COUNTROWS over a computed table")

    def iterator(self, name: str, a, ctx: _Ctx):
        self.pattern("Iterator (row by row)")
        if len(a) < 2:
            return self.review(f"{name} needs a table and an expression")
        tbl, expr = a[0], a[1]
        t, extra = self.table_arg(tbl), None
        if not t and tbl[0] == "func" and tbl[1] == "FILTER" and len(tbl[2]) == 2:
            t = self.table_arg(tbl[2][0])
            if t:
                extra = self.cond(tbl[2][1], replace(ctx, mode="row", row_tables=ctx.row_tables + (t,)))
        if not t:
            return self.review(f"{name} over a grouped or computed table: needs a GROUP BY subquery")
        row = self.tr(expr, replace(ctx, mode="row", row_tables=ctx.row_tables + (t,), any_table=False))
        if has_agg(row):
            return self.review(f"{name} with a nested aggregation: needs a grouped subquery")
        return Agg(ITER[name], row, t, False, and_(ctx.pred, extra))

    def calculate(self, a, ctx: _Ctx):
        if not a:
            return Null()
        expr, filters = a[0], a[1:]
        pred, independent = ctx.pred, None
        pctx = replace(ctx, mode="row", any_table=True)
        for f in filters:
            kind, payload = self.filter_arg(f, pctx)
            if kind == "pred":
                pred = override(pred, payload)
            elif kind == "all":
                independent = payload
            else:
                return self.review(payload)
        if independent:
            table, cond = independent
            inner = self.tr(expr, replace(ctx, pred=cond))
            agg_tables = {x.table for x in aggs_in(inner)}
            lookup = set(self.graph.reachable(table)) if self.graph else set()
            if agg_tables and agg_tables <= ({table} | lookup):
                self.pattern("Filter-independent (ALL)")
                note = (f"ALL({table}) removes every filter on {table} and the tables it looks up, "
                        f"so this value ignores slicers and filters on them.")
                self.note(note)
                return Scalar(inner, table, note)
            return self.review(f"FILTER(ALL({table}), ...) around an aggregation of another table: "
                               f"needs a separate dataset or a window function")
        if pred is not ctx.pred:
            self.pattern("Filtered aggregate (CALCULATE)")
        return self.tr(expr, replace(ctx, pred=pred))

    def filter_arg(self, f, pctx: _Ctx):
        if f[0] == "func":
            name, a = f[1], f[2]
            if name == "FILTER" and len(a) == 2:
                tbl, cond = a
                t = self.table_arg(tbl)
                if t:
                    return "pred", self.cond(cond, replace(pctx, row_tables=(t,)))
                if tbl[0] == "func" and tbl[1] in ("ALL", "ALLNOBLANKROW", "REMOVEFILTERS") and tbl[2]:
                    ts = [self.table_arg(x) for x in tbl[2]]
                    if len(ts) == 1 and ts[0]:
                        return "all", (ts[0], self.cond(cond, replace(pctx, row_tables=(ts[0],))))
                    if all(x[0] in ("col", "ref") for x in tbl[2]):
                        return "pred", self.cond(cond, pctx)          # FILTER(ALL(col), cond) == boolean filter
                return "review", "FILTER over a computed table (VALUES, ALLSELECTED ...)"
            if name == "KEEPFILTERS" and a:
                self.note("KEEPFILTERS: the filter intersects instead of replacing; SQL does the same.")
                return self.filter_arg(a[0], pctx)
            if name in FILTER_MODIFIERS:
                return "review", f"{name}(...) removes filters: needs a window function or a separate dataset"
            if name in RELATIONSHIP_FNS:
                return "review", f"{name} changes how tables relate: build the join explicitly"
            if name in TIME_INTEL:
                self.pattern("Time intelligence")
                return "review", f"time intelligence ({name}): needs a date-offset join to a date table"
            if name in ("VALUES", "DISTINCT", "SUMMARIZE", "CALCULATETABLE", "TOPN", "DATATABLE"):
                return "review", f"{name}(...) as a filter table"
        if f[0] in ("table", "name") and self.table_arg(f):
            return "review", "a whole table used as a filter"
        return "pred", self.cond(f, pctx)


def dax_date_format_to_strftime(fmt: str) -> str:
    """'MMM yyyy' -> '%b %Y' (longest token first, left to right)."""
    tokens = sorted(DATE_TOKENS, key=lambda t: -len(t[0]))
    out, i = [], 0
    while i < len(fmt):
        for tok, rep in tokens:
            if fmt.startswith(tok, i):
                out.append(rep)
                i += len(tok)
                break
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)


def _guarded_division(c, then, other):
    """IF(d <> 0, n / d, BLANK()) and IF(d = 0, BLANK(), n / d) are just a NULL-safe division."""
    def is_zero_test(node, op):
        return (isinstance(node, Bin) and node.op == op and node.right == Lit(0)
                and isinstance(node.left, Fn) and node.left.name == "COALESCE" and node.left.args[1] == Lit(0))
    if is_zero_test(c, "<>") and isinstance(then, SafeDiv) and then.alt is None and then.den == c.left.args[0] \
            and isinstance(other, Null):
        return then
    if is_zero_test(c, "=") and isinstance(other, SafeDiv) and other.alt is None and other.den == c.left.args[0] \
            and isinstance(then, Null):
        return other
    return None


def _coalesce0(x):
    return x if isinstance(x, (Lit, Null)) else Fn("COALESCE", (x, Lit(0)))


# ----------------------------------------------------------------------------------------- explanation
_WORD = {"SUM": "sum of", "AVERAGE": "average of", "MIN": "minimum of", "MAX": "maximum of", "COUNT": "count of",
         "COUNTA": "count of", "DISTINCTCOUNT": "distinct count of", "MEDIAN": "median of"}


def explain(n, depth: int = 0) -> str:
    """Plain-English template of a DAX expression (measure names kept, not expanded)."""
    if depth > 6:
        return "..."
    k = n[0]
    if k == "num":
        return str(n[1])
    if k == "str":
        return f"'{n[1]}'"
    if k == "bool":
        return str(n[1]).upper()
    if k == "col":
        return f"{n[1]}[{n[2]}]"
    if k == "ref":
        return f"[{n[1]}]"
    if k in ("table", "name"):
        return str(n[1])
    if k == "neg":
        return f"-{explain(n[1], depth + 1)}"
    if k == "not":
        return f"not ({explain(n[1], depth + 1)})"
    if k == "var":
        return explain(n[2], depth + 1) + " (using variables " + ", ".join(v for v, _ in n[1]) + ")"
    if k == "in":
        return f"{explain(n[1], depth + 1)} in {explain(n[2], depth + 1)}"
    if k == "tablector":
        return "{" + ", ".join(explain(x, depth + 1) for x in n[1]) + "}"
    if k == "bin":
        words = {"+": "+", "-": "-", "*": "x", "/": "÷", "&": "&", "&&": "and", "||": "or"}
        return f"{explain(n[2], depth + 1)} {words.get(n[1], n[1])} {explain(n[3], depth + 1)}"
    if k == "func":
        name, a = n[1], n[2]
        ex = [explain(x, depth + 1) for x in a]
        if name in _WORD and a:
            return f"{_WORD[name]} {ex[0]}"
        if name == "COUNTROWS" and a:
            return f"number of rows of {ex[0]}"
        if name == "CALCULATE" and a:
            return f"{ex[0]}" + (f" where {' and '.join(ex[1:])}" if len(ex) > 1 else "")
        if name == "FILTER" and len(a) == 2:
            inner = a[0]
            if inner[0] == "func" and inner[1] in ("ALL", "REMOVEFILTERS"):
                return f"all {explain(inner[2][0], depth + 1) if inner[2] else ''} rows with {ex[1]} (ignoring report filters)"
            return f"{ex[0]} rows with {ex[1]}"
        if name == "DIVIDE" and len(a) >= 2:
            return f"{ex[0]} ÷ {ex[1]}" + (" (blank when divided by zero)" if len(a) == 2 else f" ({ex[2]} when divided by zero)")
        if name == "IF" and len(a) >= 2:
            return f"if {ex[0]} then {ex[1]}" + (f", otherwise {ex[2]}" if len(a) > 2 else ", otherwise blank")
        if name == "BLANK":
            return "blank"
        if name.endswith("X") and name[:-1] in ("SUM", "AVERAGE", "MIN", "MAX", "COUNT") and len(a) == 2:
            return f"{_WORD.get(name[:-1], name[:-1].lower())} {ex[1]} for each row of {ex[0]}"
        return f"{name}(" + ", ".join(ex) + ")"
    return str(n)


# ----------------------------------------------------------------------------------------- classification
def classify_measure(tr: Translation, ast_is_alias: bool, graph=None) -> tuple[str, str]:
    """Primary pattern and complexity (Low / Medium / High) of a translated measure."""
    ir = tr.ir
    if tr.status in ("Parse error", "Not found"):
        return "Could not parse", "High"
    if ast_is_alias:
        return "Alias", "Low"
    if tr.reviews:
        if "Time intelligence" in tr.patterns:
            return "Time intelligence (needs review)", "High"
        return "Needs review", "High"
    if isinstance(ir, Lit):
        return "Constant", "Low"
    if isinstance(ir, Scalar) or any(isinstance(x, Scalar) for x in _walk_top(ir)):
        return "Filter-independent (ALL)", "Medium"
    aggs = aggs_in(ir)
    tables = {a.table for a in aggs}
    if graph and len(tables) > 1 and any(t2 in graph.reachable(t1) for t1 in tables for t2 in tables if t1 != t2):
        return "Mixed grain (fact + dimension)", "High"
    if graph and tables and all(graph.is_dimension(t) for t in tables):
        return "Dimension-grain aggregate", "Medium"
    if "Iterator (row by row)" in tr.patterns:
        return "Iterator (row by row)", "Medium"
    if isinstance(ir, Agg):
        if ir.pred is not None:
            return "Filtered aggregate (CALCULATE)", "Low"
        return ("Distinct count" if ir.distinct else "Simple aggregate"), "Low"
    if isinstance(ir, SafeDiv) or (isinstance(ir, Bin) and ir.op == "/"):
        return "Ratio of aggregates", "Low"
    if isinstance(ir, Case):
        return "Conditional", "Medium"
    if isinstance(ir, (BlankArith, Bin)):
        return "Composite (arithmetic)", "Low"
    return "Expression", "Medium"


def _walk_top(ir):
    from .ir import walk as _w
    return _w(ir, into_scalar=False)
