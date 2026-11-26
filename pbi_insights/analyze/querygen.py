"""Build the reference SQL for one insight (visual) from its dimensions, measures, filters and sort.

Grain rule (this is what keeps the SQL faithful to Power BI):
  * every aggregate is computed on its own table (`Agg.table`), joined only to tables that table can
    reach through many-to-one relationships (the tables that can filter it in Power BI);
  * if all aggregates live on one table and that table reaches every dimension, a single GROUP BY
    query is enough;
  * otherwise each table gets its own grouped CTE and the CTEs are joined on the shared dimensions
    (e.g. sales from the fact, selling area from the store dimension), so nothing is double counted;
  * filters a table cannot reach are not applied to it, which is how one-way filtering behaves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

import sqlglot
from sqlglot import exp

from .graph import RelGraph
from .ir import (Agg, BlankArith, Bin, Case, Col, Fn, Lit, Null, Review, SafeDiv, Scalar, Un, aggs_in, cols_in,
                 reviews_in)
from .sqlrender import Renderer, column, literal, not_null, snake, table_ref

DEFAULT_VIEW_PREFIX = "vw_"


@dataclass
class Dim:
    table: str
    column: str
    display: str
    out: str = ""
    sort_by: str | None = None
    sort_out: str | None = None


@dataclass
class MeasureSpec:
    name: str
    out: str
    ir: object


@dataclass
class FilterSpec:
    table: str
    column: str
    ir: object
    text: str


@dataclass
class SortSpec:
    kind: str                 # "dim" | "measure"
    target: object            # Dim or MeasureSpec
    desc: bool = False


@dataclass
class QueryPlan:
    sql: str = ""
    form: str = "none"        # single | multi-grain | distinct | constant | none
    agg_tables: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    joins: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    reviews: list = field(default_factory=list)


class Naming:
    def __init__(self, view_prefix: str = DEFAULT_VIEW_PREFIX):
        self.view_prefix = view_prefix

    def view(self, table: str) -> str:
        return f"{self.view_prefix}{snake(table)}"


class _Aliases:
    def __init__(self):
        self.by_table: dict[str, str] = {}

    def __call__(self, table: str) -> str:
        if table in self.by_table:
            return self.by_table[table]
        words = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", table) or [table]
        initials = "".join(w[0] for w in words).lower()
        base = re.sub(r"[^a-z0-9]", "", table.lower()) or "t"
        candidates = [initials] if len(words) > 1 else []
        candidates += [base[:n] for n in range(1, len(base) + 1)]
        used = set(self.by_table.values())
        alias = next((c for c in candidates if c and c not in used and not c[0].isdigit()), None)
        n = 2
        while alias is None or alias in used:
            alias = f"{(initials or base)[:3]}{n}"
            n += 1
        self.by_table[table] = alias
        return alias


class QueryBuilder:
    def __init__(self, graph: RelGraph, dialect: str = "tsql", naming: Naming | None = None):
        self.graph = graph
        self.dialect = dialect
        self.naming = naming or Naming()

    # ------------------------------------------------------------------------------------- helpers
    def _reach(self, table: str) -> set:
        return {table} | set(self.graph.reachable(table))

    def _from(self, sel: exp.Select, base: str, needed: set, al: _Aliases, plan: QueryPlan) -> exp.Select:
        sel = sel.from_(table_ref(self.naming.view(base), al(base)))
        for child, ccol, parent, pcol in self.graph.join_edges(base, needed):
            on = exp.EQ(this=column(ccol, al(child)), expression=column(pcol, al(parent)))
            sel = sel.join(table_ref(self.naming.view(parent), al(parent)), on=on, join_type="left")
            j = f"{child}[{ccol}] -> {parent}[{pcol}]"
            if j not in plan.joins:
                plan.joins.append(j)
        for t in [base] + [e[2] for e in self.graph.join_edges(base, needed)]:
            if t not in plan.tables:
                plan.tables.append(t)
        return sel

    def _renderer(self, al: _Aliases, agg_fn=None) -> Renderer:
        return Renderer(self.dialect, col_fn=lambda c: column(c.column, al(c.table)), agg_fn=agg_fn,
                        scalar_fn=lambda s: self._scalar(s, al))

    def _scalar(self, s: Scalar, al: _Aliases) -> exp.Expression:
        """Filter-independent value: its own subquery over the table, with no dashboard filters."""
        expr = s.expr
        hoist = None
        if isinstance(expr, Agg) and expr.pred is not None:
            hoist, expr = expr.pred, replace(expr, pred=None)
        needed = {c.table for c in cols_in(s.expr)} - {s.table}
        sub_plan = QueryPlan()
        sub = exp.select(self._renderer(al).r(expr))
        sub = self._from(sub, s.table, needed, al, sub_plan)
        if hoist is not None:
            sub = sub.where(self._renderer(al).r(hoist))
        return exp.Subquery(this=sub)

    def _members(self, table: str, dims: list, filters: list, plan: QueryPlan) -> exp.Select:
        """Distinct values of one table's columns, with the filters that reach that table.

        Power BI groups columns of different tables as a cross join (keeping only combinations that exist
        within one table), then drops rows where every measure is blank. One of these per table, CROSS
        JOINed, reproduces that for columns no measure table can reach.
        """
        al = _Aliases()
        al(table)
        reach = self._reach(table)
        flt = [f for f in filters if {c.table for c in cols_in(f.ir)} <= reach]
        cols = []
        for d in dims:
            cols.append(exp.alias_(column(d.column, al(d.table)), d.out) if d.out != d.column
                        else column(d.column, al(d.table)))
            if d.sort_by:
                cols.append(exp.alias_(column(d.sort_by, al(d.table)), d.sort_out) if d.sort_out != d.sort_by
                            else column(d.sort_by, al(d.table)))
        needed = {c.table for f in flt for c in cols_in(f.ir)} & reach
        sel = self._from(exp.select(*cols).distinct(), table, needed, al, plan)
        where = self._and([self._renderer(al).r(f.ir) for f in flt])
        return sel.where(where) if where is not None else sel

    @staticmethod
    def _and(conds: list) -> exp.Expression | None:
        out = None
        for c in conds:
            out = c if out is None else exp.And(this=out, expression=c)
        return out

    def ordered(self, e: exp.Expression, desc: bool = False) -> exp.Ordered:
        """ORDER BY term using the dialect's own NULL ordering, so no NULLS FIRST/LAST emulation is printed."""
        try:
            null_ordering = sqlglot.Dialect.get_or_raise(self.dialect).NULL_ORDERING
        except Exception:  # noqa: BLE001
            null_ordering = "nulls_are_small"
        if null_ordering == "nulls_are_small":
            nulls_first = not desc
        elif null_ordering == "nulls_are_large":
            nulls_first = desc
        else:
            nulls_first = False
        return exp.Ordered(this=e, desc=desc, nulls_first=nulls_first)

    def _order(self, sorts: list[SortSpec], dim_expr, meas_expr) -> list:
        out = []
        for s in sorts:
            if s.kind == "dim":
                e = dim_expr(s.target, sort_key=True)
            else:
                e = meas_expr(s.target)
            if e is not None:
                out.append(self.ordered(e, s.desc))
        return out

    # ------------------------------------------------------------------------------------- main
    def build(self, dims: list[Dim], measures: list[MeasureSpec], filters: list[FilterSpec],
              sorts: list[SortSpec]) -> QueryPlan:
        plan = QueryPlan()
        self._name_outputs(dims, measures)
        for m in measures:
            plan.reviews += [r for r in reviews_in(m.ir) if r not in plan.reviews]
        if not measures:
            return self._distinct(dims, filters, sorts, plan)
        groups: dict[str, list[Agg]] = {}
        for m in measures:
            for a in aggs_in(m.ir):
                groups.setdefault(a.table, [])
                if a not in groups[a.table]:
                    groups[a.table].append(a)
        plan.agg_tables = list(groups)
        if not groups:
            return self._constant(dims, measures, filters, sorts, plan)
        info = {}
        for t, aggs in groups.items():
            reach = self._reach(t)
            keys = [d for d in dims if d.table in reach]
            flt = [f for f in filters if {c.table for c in cols_in(f.ir)} <= reach]
            for f in filters:
                if f not in flt:
                    plan.notes.append(f"Filter {f.text} does not reach {t} (one-way relationship), so Power BI does "
                                      f"not apply it to the {t} figures; neither does the SQL.")
            for d in dims:
                if d not in keys:
                    plan.notes.append(f"{d.table}[{d.column}] is not related to {t}: Power BI repeats the same {t} "
                                      f"value on every {d.column} row.")
            needed = {d.table for d in keys} | {c.table for f in flt for c in cols_in(f.ir)}
            for a in aggs:
                needed |= {c.table for c in cols_in(a)}
                pred_cols = {(c.table, c.column) for c in cols_in(a.pred)} if a.pred is not None else set()
                for f in flt:
                    if any((c.table, c.column) in pred_cols for c in cols_in(f.ir)):
                        plan.notes.append(f"A measure's CALCULATE filter overrides the visual filter {f.text} in "
                                          f"Power BI; the SQL applies both. Check if results differ.")
            missing = needed - reach
            if missing:
                plan.reviews.append(f"aggregate over {t} uses columns of {', '.join(sorted(missing))}, "
                                    f"which {t} cannot reach through relationships")
            info[t] = {"aggs": aggs, "keys": keys, "filters": flt, "needed": needed & reach}
        if len(groups) == 1:
            t = next(iter(groups))
            if len(info[t]["keys"]) == len(dims):
                return self._single(t, info[t], dims, measures, sorts, plan)
        return self._multi(info, dims, measures, filters, sorts, plan)

    def _name_outputs(self, dims: list[Dim], measures: list[MeasureSpec]):
        used = set()
        for d in dims:
            name = d.column if d.column not in used else snake(f"{d.table}_{d.column}")
            d.out = name
            used.add(name.lower())
            if d.sort_by:
                sname = d.sort_by if d.sort_by.lower() not in used else snake(f"{d.column}_sort")
                d.sort_out = sname
                used.add(sname.lower())
        for m in measures:
            base, n = m.out or snake(m.name), 2
            name = base
            while name.lower() in used:
                name = f"{base}_{n}"
                n += 1
            m.out = name
            used.add(name.lower())

    # ---- single table: one GROUP BY -------------------------------------------------------------
    def _single(self, t, inf, dims, measures, sorts, plan: QueryPlan) -> QueryPlan:
        al = _Aliases()
        al(t)
        rnd = self._renderer(al)
        cols = []
        group = []
        for d in dims:
            c = column(d.column, al(d.table))
            cols.append(c if d.out == d.column else exp.alias_(c, d.out, quoted=None))
            group.append(column(d.column, al(d.table)))
            if d.sort_by:
                s = column(d.sort_by, al(d.table))
                cols.append(s if d.sort_out == d.sort_by else exp.alias_(s, d.sort_out))
                group.append(column(d.sort_by, al(d.table)))
        meas_exprs = {m.out: rnd.r(m.ir) for m in measures}
        cols += [exp.alias_(e, m.out) for m, e in zip(measures, meas_exprs.values())]
        sel = self._from(exp.select(*cols), t, inf["needed"], al, plan)
        where = self._and([rnd.r(f.ir) for f in inf["filters"]])
        if where is not None:
            sel = sel.where(where)
        if group:
            sel = sel.group_by(*group)
        order = self._order(sorts,
                            lambda d, sort_key=False: column(d.sort_by if sort_key and d.sort_by else d.column,
                                                             al(d.table)),
                            lambda m: column(m.out))
        if dims and measures and all(can_be_null(m.ir) for m in measures):
            if len(measures) == 1:
                sel = sel.having(not_null(rnd.r(measures[0].ir)))
            else:
                inner = sel
                sel = exp.select(*[column(c.alias_or_name, "q") for c in inner.selects]).from_(inner.subquery("q"))
                sel = sel.where(_or([not_null(column(m.out, "q")) for m in measures]))
                order = self._order(sorts,
                                    lambda d, sort_key=False: column(d.sort_out if sort_key and d.sort_out else d.out, "q"),
                                    lambda m: column(m.out, "q"))
        if order:
            sel = sel.order_by(*order)
        plan.form = "single"
        plan.sql = sel.sql(dialect=self.dialect, pretty=True)
        return plan

    # ---- several grains: one CTE per aggregated table ---------------------------------------------
    def _multi(self, info, dims, measures, filters, sorts, plan: QueryPlan) -> QueryPlan:
        al = _Aliases()
        for t in info:
            al(t)
        ctes, agg_cols = [], {}
        for i, (t, inf) in enumerate(info.items(), start=1):
            rnd = self._renderer(al)
            name = f"g{i}_{snake(t)}"
            cols, group = [], []
            for d in inf["keys"]:
                cols.append(exp.alias_(column(d.column, al(d.table)), d.out) if d.out != d.column
                            else column(d.column, al(d.table)))
                group.append(column(d.column, al(d.table)))
                if d.sort_by:
                    cols.append(exp.alias_(column(d.sort_by, al(d.table)), d.sort_out) if d.sort_out != d.sort_by
                                else column(d.sort_by, al(d.table)))
                    group.append(column(d.sort_by, al(d.table)))
            for k, a in enumerate(inf["aggs"], start=1):
                cname = f"agg_{i}_{k}"
                cols.append(exp.alias_(rnd.agg(a), cname))
                agg_cols[repr(a)] = (name, cname)
            sel = self._from(exp.select(*cols), t, inf["needed"], al, plan)
            where = self._and([rnd.r(f.ir) for f in inf["filters"]])
            if where is not None:
                sel = sel.where(where)
            if group:
                sel = sel.group_by(*group)
            ctes.append((name, t, inf, sel))
        # base CTE: covers the most dimensions; prefer a fact table on ties
        base = max(ctes, key=lambda c: (len(c[2]["keys"]), not self.graph.is_dimension(c[1]), -ctes.index(c)))
        # columns no measure table reaches: Power BI repeats each measure value for every member, which is
        # a CROSS JOIN with that table's distinct values
        unrelated: dict[str, list] = {}
        for d in dims:
            if not any(d in c[2]["keys"] for c in ctes):
                unrelated.setdefault(d.table, []).append(d)
        members = [(f"u{i}_{snake(t)}", t, self._members(t, ds, filters, plan))
                   for i, (t, ds) in enumerate(unrelated.items(), start=1)]
        member_of = {d.table: name for name, t, _ in members for d in unrelated[t]}
        missing = [d for d in dims if d not in base[2]["keys"] and d.table not in member_of]
        if missing:
            plan.reviews.append("dimensions " + ", ".join(f"{d.table}[{d.column}]" for d in missing)
                                + " are not related to the measure tables: add a dimension scaffold")
        rnd = Renderer(self.dialect, col_fn=lambda c: column(c.column, al(c.table)),
                       agg_fn=lambda a: column(agg_cols[repr(a)][1], agg_cols[repr(a)][0]) if repr(a) in agg_cols else None,
                       scalar_fn=lambda s: self._scalar(s, al))
        cols, src_of = [], {}
        for d in dims:
            src = base[0] if d in base[2]["keys"] else next((c[0] for c in ctes if d in c[2]["keys"]),
                                                            member_of.get(d.table))
            src_of[id(d)] = src
            cols.append(column(d.out, src))
            if d.sort_by:
                cols.append(column(d.sort_out, src))
        meas = [(m, rnd.r(m.ir)) for m in measures]
        cols += [exp.alias_(e, m.out) for m, e in meas]
        sel = exp.select(*cols).from_(exp.to_table(base[0]))
        for name, t, inf, _ in ctes:
            if name == base[0]:
                continue
            shared = [d for d in inf["keys"] if d in base[2]["keys"]]
            if shared:
                on = self._and([exp.EQ(this=column(d.out, base[0]), expression=column(d.out, name)) for d in shared])
                sel = sel.join(exp.to_table(name), on=on, join_type="left")
            else:
                sel = sel.join(exp.to_table(name), join_type="cross")
        for name, _, _ in members:
            sel = sel.join(exp.to_table(name), join_type="cross")
        if dims and all(can_be_null(m.ir) for m in measures):
            sel = sel.where(_or([not_null(e.copy()) for _, e in meas]))
        order = self._order(sorts,
                            lambda d, sort_key=False: column(d.sort_out if sort_key and d.sort_out else d.out,
                                                             src_of.get(id(d), base[0])),
                            lambda m: column(m.out))
        if order:
            sel = sel.order_by(*order)
        for name, _, _, cte_sel in ctes:
            sel = sel.with_(name, as_=cte_sel)
        for name, _, m_sel in members:
            sel = sel.with_(name, as_=m_sel)
        plan.form = "multi-grain"
        plan.notes.append("Aggregates come from different tables (" + ", ".join(info) + "): each is computed at its "
                          "own grain in a CTE and joined on the shared dimensions, so nothing is double counted.")
        plan.sql = sel.sql(dialect=self.dialect, pretty=True)
        return plan

    # ---- no measures: distinct list (slicers) -------------------------------------------------------------
    def _distinct(self, dims, filters, sorts, plan: QueryPlan) -> QueryPlan:
        if not dims:
            return plan
        tables = {d.table for d in dims}
        base = next((t for t in [d.table for d in dims] if tables <= self._reach(t)), dims[0].table)
        reach = self._reach(base)
        al = _Aliases()
        al(base)
        rnd = self._renderer(al)
        flt = [f for f in filters if {c.table for c in cols_in(f.ir)} <= reach]
        cols, keys = [], []
        for d in dims:
            cols.append(column(d.column, al(d.table)))
            if d.sort_by:
                cols.append(column(d.sort_by, al(d.table)))
        needed = (tables | {c.table for f in flt for c in cols_in(f.ir)}) & reach
        sel = self._from(exp.select(*cols).distinct(), base, needed, al, plan)
        where = self._and([rnd.r(f.ir) for f in flt])
        if where is not None:
            sel = sel.where(where)
        order = self._order(sorts, lambda d, sort_key=False: column(d.sort_by if sort_key and d.sort_by else d.column,
                                                                     al(d.table)), lambda m: None)
        if not order:
            order = [self.ordered(column(d.sort_by or d.column, al(d.table))) for d in dims]
        sel = sel.order_by(*order)
        plan.form = "distinct"
        plan.sql = sel.sql(dialect=self.dialect, pretty=True)
        return plan

    # ---- measures without aggregates (constants, filter-independent values) -------------------------------
    def _constant(self, dims, measures, filters, sorts, plan: QueryPlan) -> QueryPlan:
        al = _Aliases()
        rnd = self._renderer(al)
        cols = [exp.alias_(rnd.r(m.ir), m.out) for m in measures]
        sel = exp.select(*cols)
        if dims:
            # No measure is ever blank here, so Power BI shows every combination: the cross join of each
            # table's own distinct values (filtered by what reaches that table).
            by_table: dict[str, list] = {}
            for d in dims:
                by_table.setdefault(d.table, []).append(d)
            members = [(f"m{i}_{snake(t)}", t, self._members(t, ds, filters, plan))
                       for i, (t, ds) in enumerate(by_table.items(), start=1)]
            src = {t: name for name, t, _ in members}
            dim_cols = []
            for d in dims:
                dim_cols.append(column(d.out, src[d.table]))
                if d.sort_by:
                    dim_cols.append(column(d.sort_out, src[d.table]))
            sel = exp.select(*dim_cols, *cols).from_(exp.to_table(members[0][0]))
            for name, _, _ in members[1:]:
                sel = sel.join(exp.to_table(name), join_type="cross")
            if all(can_be_null(m.ir) for m in measures):
                # Power BI drops rows where every measure is blank; the values are constant here, so the
                # database can decide this once instead of building the whole cross join
                sel = sel.where(_or([not_null(c.this.copy()) for c in cols]))
            order = self._order(sorts, lambda d, sort_key=False: column(
                d.sort_out if sort_key and d.sort_out else d.out, src[d.table]), lambda m: column(m.out))
            if order:
                sel = sel.order_by(*order)
            for name, _, m_sel in members:
                sel = sel.with_(name, as_=m_sel)
        plan.form = "constant"
        plan.sql = sel.sql(dialect=self.dialect, pretty=True)
        return plan


def can_be_null(n) -> bool:
    """Can this measure be NULL for a group that has rows? (COUNT and COALESCE-based results cannot.)"""
    if isinstance(n, Lit):
        return n.value is None
    if isinstance(n, (Null, Review, SafeDiv)):
        return True
    if isinstance(n, Agg):
        return n.func != "COUNT"
    if isinstance(n, BlankArith):
        return False
    if isinstance(n, Fn):
        if n.name == "COALESCE":
            return all(can_be_null(a) for a in n.args)
        return any(can_be_null(a) for a in n.args)
    if isinstance(n, Bin):
        return can_be_null(n.left) or can_be_null(n.right)
    if isinstance(n, Un):
        return can_be_null(n.x)
    if isinstance(n, Case):
        return n.default is None or isinstance(n.default, Null) or any(can_be_null(v) for _, v in n.whens) \
            or can_be_null(n.default)
    if isinstance(n, Scalar):
        return can_be_null(n.expr)
    return True


def _or(conds: list):
    out = None
    for c in conds:
        out = c if out is None else exp.Or(this=out, expression=c)
    return out
