"""Relationship graph: which tables a table can reach through many-to-one relationships.

In Power BI a filter flows from the 'one' side of a relationship to the 'many' side. In SQL that is a
LEFT JOIN from the many-side table (fact) to the one-side table (dimension). A table T can be grouped
or filtered by columns of every table reachable from T this way, and by nothing else.
"""
from __future__ import annotations

from collections import deque


class RelGraph:
    def __init__(self, relationships: list[dict]):
        self.parents: dict[str, list[tuple]] = {}
        self.both_direction: list[dict] = []
        self.many_to_many: list[dict] = []
        self.inactive: list[dict] = []
        self.one_side: set[str] = set()
        for r in relationships:
            if r.get("auto_date"):
                continue
            if not r.get("active", True):
                self.inactive.append(r)
                continue
            fc, tc = r.get("from_cardinality", "many"), r.get("to_cardinality", "one")
            if fc == "many" and tc == "many":
                self.many_to_many.append(r)
                continue
            if fc == "one" and tc == "many":
                many, mcol, one, ocol = r["to_table"], r["to_column"], r["from_table"], r["from_column"]
            else:
                many, mcol, one, ocol = r["from_table"], r["from_column"], r["to_table"], r["to_column"]
            self.parents.setdefault(many, []).append((one, mcol, ocol))
            self.one_side.add(one)
            if r.get("cross_filter") == "Both":
                self.both_direction.append(r)

    def reachable(self, table: str) -> list[str]:
        seen, order, q = {table}, [], deque([table])
        while q:
            t = q.popleft()
            for parent, _, _ in self.parents.get(t, []):
                if parent not in seen:
                    seen.add(parent)
                    order.append(parent)
                    q.append(parent)
        return order

    def join_edges(self, base: str, needed: set[str]) -> list[tuple]:
        """Edges (child, child_col, parent, parent_col) that connect `base` to every table in `needed`."""
        prev, q, seen = {}, deque([base]), {base}
        while q:
            t = q.popleft()
            for parent, ccol, pcol in self.parents.get(t, []):
                if parent not in seen:
                    seen.add(parent)
                    prev[parent] = (t, ccol, parent, pcol)
                    q.append(parent)
        edges = []
        for target in needed:
            if target == base or target not in prev:
                continue
            chain, cur = [], target
            while cur != base:
                e = prev[cur]
                chain.append(e)
                cur = e[0]
            for e in reversed(chain):
                if e not in edges:
                    edges.append(e)
        order = {t: i for i, t in enumerate([base] + self.reachable(base))}
        return sorted(edges, key=lambda e: order.get(e[2], 99))

    def is_dimension(self, table: str) -> bool:
        return table in self.one_side

    def is_fact(self, table: str) -> bool:
        return table in self.parents and table not in self.one_side
