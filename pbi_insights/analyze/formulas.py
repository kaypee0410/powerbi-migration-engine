"""Draft calculated-field formulas for Tableau, QuickSight and Incorta, rendered from the same IR as the SQL.

These are starting points (labelled 'draft' in the workbook): names, quoting and null handling differ by
tool version, so check each in the tool before relying on it.
"""
from __future__ import annotations

from .ir import (Agg, BlankArith, Bin, Case, Col, Fn, InList, IsNull, Lit, Null, Review, SafeDiv, Scalar, Star, Un)

AGG_NAMES = {
    "tableau": {"SUM": "SUM", "AVG": "AVG", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT", "MEDIAN": "MEDIAN"},
    "quicksight": {"SUM": "sum", "AVG": "avg", "MIN": "min", "MAX": "max", "COUNT": "count", "MEDIAN": "median"},
    "incorta": {"SUM": "sum", "AVG": "average", "MIN": "min", "MAX": "max", "COUNT": "count", "MEDIAN": "median"},
}
MAX_LEN = 700


class _Unsupported(Exception):
    pass


class ToolFormula:
    def __init__(self, tool: str):
        self.tool = tool

    # ---- leaves -------------------------------------------------------------------------------------------
    def col(self, c: Col) -> str:
        if self.tool == "tableau":
            return f"[{c.column}]"
        if self.tool == "quicksight":
            return "{" + c.column + "}"
        return f"{c.table}.{c.column}" if " " not in c.column else f"{c.table}.`{c.column}`"

    def lit(self, v) -> str:
        if v is None:
            return "NULL" if self.tool != "incorta" else "null"
        if isinstance(v, bool):
            return ("TRUE" if v else "FALSE") if self.tool == "tableau" else ("true" if v else "false")
        if isinstance(v, (int, float)):
            return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
        return "'" + str(v).replace("'", "\\'") + "'"

    # ---- tree ---------------------------------------------------------------------------------------------
    def r(self, n) -> str:
        t = self.tool
        if isinstance(n, Col):
            return self.col(n)
        if isinstance(n, Lit):
            return self.lit(n.value)
        if isinstance(n, Null):
            return self.lit(None)
        if isinstance(n, Agg):
            return self.agg(n)
        if isinstance(n, Bin):
            op = n.op
            if op in ("AND", "OR"):
                if t == "incorta":
                    return f"{op.lower()}({self.r(n.left)}, {self.r(n.right)})"
                return f"({self.r(n.left)} {op} {self.r(n.right)})"
            if op == "<>" and t == "incorta":
                op = "!="
            return f"({self.r(n.left)} {op} {self.r(n.right)})"
        if isinstance(n, Un):
            if n.op == "NEG":
                return f"-{self.r(n.x)}"
            return f"not({self.r(n.x)})" if t == "incorta" else f"NOT {self.r(n.x)}"
        if isinstance(n, Case):
            return self.case(n)
        if isinstance(n, SafeDiv):
            a, b = self.r(n.num), self.r(n.den)
            if t == "quicksight":
                return f"ifelse({b} <> 0, {a} / {b}, NULL)"
            return f"{a} / {b}"
        if isinstance(n, BlankArith):
            a, b = self.r(n.left), self.r(n.right)
            if t == "tableau":
                return f"(ZN({a}) {n.op} ZN({b}))"
            if t == "quicksight":
                return f"(coalesce({a}, 0) {n.op} coalesce({b}, 0))"
            return f"({a} {n.op} {b})"
        if isinstance(n, IsNull):
            return f"ISNULL({self.r(n.x)})" if t == "tableau" else f"isNull({self.r(n.x)})"
        if isinstance(n, InList):
            vals = ", ".join(self.r(v) for v in n.values)
            if t == "tableau":
                return f"{self.r(n.x)} IN ({vals})"
            if t == "quicksight":
                return f"in({self.r(n.x)}, [{vals}])"
            return f"inList({self.r(n.x)}, {vals})"
        if isinstance(n, Fn):
            return self.fn(n)
        if isinstance(n, Scalar):
            inner = self.r(n.expr)
            if t == "tableau":
                return "{FIXED : " + inner + "}"
            return inner
        if isinstance(n, (Review, Star)):
            raise _Unsupported()
        raise _Unsupported()

    def case(self, n: Case) -> str:
        t = self.tool
        default = n.default if n.default is not None else Null()
        if t == "tableau":
            parts = [f"IF {self.r(c)} THEN {self.r(v)}" if i == 0 else f"ELSEIF {self.r(c)} THEN {self.r(v)}"
                     for i, (c, v) in enumerate(n.whens)]
            if not isinstance(default, Null):
                parts.append(f"ELSE {self.r(default)}")
            return " ".join(parts) + " END"
        if t == "quicksight":
            args = [x for c, v in n.whens for x in (self.r(c), self.r(v))]
            return f"ifelse({', '.join(args)}, {self.r(default)})"
        out = self.r(default)
        for c, v in reversed(n.whens):
            out = f"if({self.r(c)}, {self.r(v)}, {out})"
        return out

    def agg(self, a: Agg) -> str:
        t = self.tool
        name = AGG_NAMES[t][a.func]
        if isinstance(a.arg, Star):
            key = {"tableau": f"[{a.table} (Count)]", "quicksight": "{" + f"{a.table} key" + "}",
                   "incorta": f"{a.table}.<key column>"}[t]
            pred = f" where {self.r(a.pred)}" if a.pred is not None else ""
            return (f"SUM({key})" if t == "tableau" else f"count({key})") + pred
        x = self.r(a.arg)
        if t == "tableau":
            if a.pred is not None:
                x = f"IF {self.r(a.pred)} THEN {x} END"
            return f"COUNTD({x})" if a.distinct else f"{name}({x})"
        if t == "quicksight":
            if a.distinct:
                return f"distinct_countIf({x}, {self.r(a.pred)})" if a.pred is not None else f"distinct_count({x})"
            if a.pred is not None:
                return f"{name}If({x}, {self.r(a.pred)})"
            return f"{name}({x})"
        if a.distinct:
            if a.pred is not None:
                raise _Unsupported()
            return f"distinct({x})"
        if a.pred is not None:
            if a.func == "SUM":
                return f"sum(if({self.r(a.pred)}, {x}, 0))"
            if a.func == "COUNT":
                return f"sum(if({self.r(a.pred)}, 1, 0))"
            raise _Unsupported()
        return f"{name}({x})"

    def fn(self, n: Fn) -> str:
        t = self.tool
        a = [self.r(x) for x in n.args]
        name = n.name
        if name == "COALESCE":
            if t == "tableau":
                return f"ZN({a[0]})" if len(a) == 2 and a[1] == "0" else f"IFNULL({a[0]}, {a[1]})"
            if t == "quicksight":
                return f"coalesce({', '.join(a)})"
            return f"if(isNull({a[0]}), {a[1]}, {a[0]})"
        if name == "CONCAT":
            if t == "tableau":
                return " + ".join(a)
            return f"concat({', '.join(a)})"
        if name in ("YEAR", "MONTH", "DAY"):
            if t == "quicksight":
                part = {"YEAR": "YYYY", "MONTH": "MM", "DAY": "DD"}[name]
                return f"extract('{part}', {a[0]})"
            return f"{name}({a[0]})" if t == "tableau" else f"{name.lower()}({a[0]})"
        if name == "TIMEFMT":
            fmt = n.args[1].value if isinstance(n.args[1], Lit) else ""
            java = fmt.replace("%Y", "yyyy").replace("%y", "yy").replace("%B", "MMMM").replace("%b", "MMM") \
                .replace("%m", "MM").replace("%d", "dd").replace("%A", "EEEE").replace("%a", "EEE")
            if t == "tableau":
                if fmt == "%b":
                    return f"LEFT(DATENAME('month', {a[0]}), 3)"
                if fmt == "%B":
                    return f"DATENAME('month', {a[0]})"
                return f"DATENAME('{java}', {a[0]})"
            return f"formatDate({a[0]}, '{java}')"
        if name == "ROUND":
            return f"{'ROUND' if t == 'tableau' else 'round'}({', '.join(a)})"
        if name in ("ABS", "FLOOR", "CEIL", "SQRT", "UPPER", "LOWER", "TRIM", "LENGTH", "LEFT", "RIGHT"):
            fname = {"LENGTH": "LEN" if t == "tableau" else "strlen", "CEIL": "CEILING" if t == "tableau" else "ceil"}.get(name, name)
            return f"{fname if t == 'tableau' else fname.lower()}({', '.join(a)})"
        if name.startswith("LIKE_"):
            if t == "tableau":
                fn = {"LIKE_CONTAINS": "CONTAINS", "LIKE_STARTS": "STARTSWITH", "LIKE_ENDS": "ENDSWITH"}[name]
                return f"{fn}({a[0]}, {a[1]})"
            fn = {"LIKE_CONTAINS": "contains", "LIKE_STARTS": "startsWith", "LIKE_ENDS": "endsWith"}[name]
            return f"{fn}({a[0]}, {a[1]})"
        raise _Unsupported()


def tool_formulas(ir) -> dict:
    out = {}
    for tool in ("tableau", "quicksight", "incorta"):
        try:
            text = ToolFormula(tool).r(ir) if ir is not None else ""
            out[tool] = text if len(text) <= MAX_LEN else "(long expression: build from the SQL)"
        except _Unsupported:
            out[tool] = "(not expressible as a simple calculated field: use the SQL)"
        except Exception:  # noqa: BLE001
            out[tool] = "(see the SQL)"
    return out
