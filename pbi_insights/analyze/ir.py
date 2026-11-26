"""Intermediate representation between DAX and SQL.

A translated measure is a small tree of these nodes. The important idea is `Agg`: an aggregate over
one table, carrying the CALCULATE filters that apply to it as a row-level predicate (`pred`). The
SQL generator renders it as SUM(CASE WHEN pred THEN x END), and uses `Agg.table` to work out the
grain each aggregate must be computed at (fact vs dimension), which is how fan-out is avoided.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class Col:
    table: str
    column: str


@dataclass(frozen=True)
class Lit:
    value: object


@dataclass(frozen=True)
class Null:
    pass


@dataclass(frozen=True)
class Star:
    pass


@dataclass(frozen=True)
class Agg:
    func: str                    # SUM AVG MIN MAX COUNT MEDIAN
    arg: object                  # row-level expression or Star()
    table: str                   # table whose rows are aggregated (the grain)
    distinct: bool = False
    pred: object = None          # row-level predicate from CALCULATE / FILTER


@dataclass(frozen=True)
class Bin:
    op: str                      # + - * / = <> < > <= >= AND OR
    left: object
    right: object


@dataclass(frozen=True)
class Un:
    op: str                      # NEG NOT
    x: object


@dataclass(frozen=True)
class Case:
    whens: tuple                 # ((condition, value), ...)
    default: object = None


@dataclass(frozen=True)
class Fn:
    name: str
    args: tuple


@dataclass(frozen=True)
class SafeDiv:
    num: object
    den: object
    alt: object = None


@dataclass(frozen=True)
class BlankArith:
    """DAX + / - where a blank operand counts as 0."""
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class IsNull:
    x: object


@dataclass(frozen=True)
class InList:
    x: object
    values: tuple


@dataclass(frozen=True)
class Scalar:
    """Value computed on its own, ignoring the visual's filters (DAX ALL / FILTER(ALL(...)))."""
    expr: object
    table: str
    note: str = ""


@dataclass(frozen=True)
class Review:
    reason: str


IR_TYPES = (Col, Lit, Null, Star, Agg, Bin, Un, Case, Fn, SafeDiv, BlankArith, IsNull, InList, Scalar, Review)
BOOL_OPS = {"=", "<>", "<", ">", "<=", ">=", "AND", "OR"}


def children(node):
    if not isinstance(node, IR_TYPES):
        return
    for f in fields(node):
        v = getattr(node, f.name)
        if isinstance(v, IR_TYPES):
            yield v
        elif isinstance(v, tuple):
            for item in v:
                if isinstance(item, tuple):
                    for x in item:
                        if isinstance(x, IR_TYPES):
                            yield x
                elif isinstance(item, IR_TYPES):
                    yield item


def walk(node, into_scalar: bool = True):
    if not isinstance(node, IR_TYPES):
        return
    yield node
    if isinstance(node, Scalar) and not into_scalar:
        return
    for c in children(node):
        yield from walk(c, into_scalar)


def aggs_in(node, into_scalar: bool = False) -> list[Agg]:
    out = []
    for n in walk(node, into_scalar):
        if isinstance(n, Agg) and n not in out:
            out.append(n)
    return out


def map_aggs(node, fn):
    """Copy of `node` with every Agg replaced by fn(agg). Scalar values are left as they are."""
    if isinstance(node, Agg):
        return fn(node)
    if isinstance(node, Scalar) or not isinstance(node, IR_TYPES):
        return node
    changes = {}
    for f in fields(node):
        v = getattr(node, f.name)
        if isinstance(v, IR_TYPES):
            nv = map_aggs(v, fn)
        elif isinstance(v, tuple):
            nv = tuple(tuple(map_aggs(x, fn) for x in item) if isinstance(item, tuple) else map_aggs(item, fn)
                       for item in v)
        else:
            continue
        if nv is not v:
            changes[f.name] = nv
    return replace(node, **changes) if changes else node


def cols_in(node) -> list[Col]:
    out = []
    for n in walk(node):
        if isinstance(n, Col) and n not in out:
            out.append(n)
    return out


def reviews_in(node) -> list[str]:
    return [n.reason for n in walk(node) if isinstance(n, Review)]


def has_agg(node) -> bool:
    return any(isinstance(n, Agg) for n in walk(node))


def is_boolean(node) -> bool:
    return (isinstance(node, Bin) and node.op in BOOL_OPS) or isinstance(node, (IsNull, InList)) \
        or (isinstance(node, Un) and node.op == "NOT") or (isinstance(node, Lit) and isinstance(node.value, bool)) \
        or (isinstance(node, Fn) and node.name.startswith("LIKE_"))


def as_value(node, dialect: str):
    """A TRUE/FALSE calculated column as a value: T-SQL cannot SELECT a bare comparison, so use 1/0."""
    if dialect == "tsql" and is_boolean(node):
        return Case(((node, Lit(1)), (Un("NOT", node), Lit(0))), None)
    return node


def and_(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return Bin("AND", a, b)


def conjuncts(pred) -> list:
    if pred is None:
        return []
    if isinstance(pred, Bin) and pred.op == "AND":
        return conjuncts(pred.left) + conjuncts(pred.right)
    return [pred]


def override(existing, new):
    """CALCULATE semantics: a new filter replaces existing filters on the same columns."""
    if new is None:
        return existing
    new_cols = set(cols_in(new))
    kept = [c for c in conjuncts(existing) if not set(cols_in(c)) <= new_cols]
    out = None
    for c in kept:
        out = and_(out, c)
    return and_(out, new)
