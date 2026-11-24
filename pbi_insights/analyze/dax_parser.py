"""A small DAX tokenizer and recursive-descent parser.

The parser covers the expression language used in measures and calculated columns (operators,
function calls, VAR/RETURN, table constructors). It produces a tuple-based AST:

  ("num", 1.5) ("str", "x") ("bool", True) ("col", table, column) ("ref", name) ("table", name)
  ("name", identifier) ("func", NAME, [args]) ("bin", op, left, right) ("neg", x) ("not", x)
  ("in", x, table_expr) ("var", [(name, expr), ...], body) ("tablector", [items]) ("tuple", [items]) ("empty",)
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class DaxSyntaxError(Exception):
    pass


@dataclass
class Tok:
    kind: str          # NUM STR COL TBL REF IDENT OP EOF
    val: object
    pos: int


_TOKEN_RE = re.compile(r"""
   (?P<ws>\s+)
  |(?P<comment>(?://|--)[^\n]*|/\*.*?\*/)
  |(?P<str>"(?:[^"]|"")*")
  |(?P<qtable>'(?:[^']|'')+')(?P<qcol>\[(?:[^\]]|\]\])*\])?
  |(?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  |(?P<ident>[A-Za-z_][A-Za-z0-9_.]*)(?P<icol>\[(?:[^\]]|\]\])*\])?
  |(?P<bracket>\[(?:[^\]]|\]\])*\])
  |(?P<op>&&|\|\||<=|>=|<>|==|[-+*/^&=<>(),{}])
""", re.S | re.X)


def _unbracket(s: str) -> str:
    return s[1:-1].replace("]]", "]")


def tokenize(text: str) -> list[Tok]:
    toks, pos = [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise DaxSyntaxError(f"Unexpected character {text[pos]!r} at position {pos}")
        kind = m.lastgroup
        if m.group("ws") or m.group("comment"):
            pass
        elif m.group("str") is not None:
            toks.append(Tok("STR", m.group("str")[1:-1].replace('""', '"'), pos))
        elif m.group("qtable") is not None:
            table = m.group("qtable")[1:-1].replace("''", "'")
            if m.group("qcol") is not None:
                toks.append(Tok("COL", (table, _unbracket(m.group("qcol"))), pos))
            else:
                toks.append(Tok("TBL", table, pos))
        elif m.group("num") is not None:
            raw = m.group("num")
            toks.append(Tok("NUM", float(raw) if any(c in raw for c in ".eE") else int(raw), pos))
        elif m.group("ident") is not None:
            if m.group("icol") is not None:
                toks.append(Tok("COL", (m.group("ident"), _unbracket(m.group("icol"))), pos))
            else:
                toks.append(Tok("IDENT", m.group("ident"), pos))
        elif m.group("bracket") is not None:
            toks.append(Tok("REF", _unbracket(m.group("bracket")), pos))
        elif m.group("op") is not None:
            toks.append(Tok("OP", m.group("op"), pos))
        else:  # pragma: no cover
            raise DaxSyntaxError(f"Unhandled token {kind} at {pos}")
        pos = m.end()
    toks.append(Tok("EOF", None, len(text)))
    return toks


class _Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.i = 0

    # -- token helpers -------------------------------------------------------------------------
    def peek(self, k: int = 0) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def is_op(self, t: Tok, *ops) -> bool:
        return t.kind == "OP" and t.val in ops

    def accept(self, *ops):
        t = self.peek()
        if self.is_op(t, *ops):
            self.i += 1
            return t
        return None

    def expect(self, op: str):
        t = self.next()
        if not self.is_op(t, op):
            raise DaxSyntaxError(f"Expected {op!r} at position {t.pos}, found {t.val!r}")
        return t

    @staticmethod
    def is_kw(t: Tok, kw: str) -> bool:
        return t.kind == "IDENT" and str(t.val).upper() == kw

    # -- grammar ----------------------------------------------------------------------------------
    def parse(self):
        e = self.expr()
        if self.peek().kind != "EOF":
            t = self.peek()
            raise DaxSyntaxError(f"Unexpected {t.val!r} at position {t.pos}")
        return e

    def expr(self):
        if self.is_kw(self.peek(), "VAR"):
            return self.var_block()
        return self.or_()

    def var_block(self):
        defs = []
        while self.is_kw(self.peek(), "VAR"):
            self.next()
            name = self.next()
            if name.kind != "IDENT":
                raise DaxSyntaxError(f"Expected a variable name at position {name.pos}")
            self.expect("=")
            defs.append((name.val, self.expr()))
        if not self.is_kw(self.peek(), "RETURN"):
            raise DaxSyntaxError(f"Expected RETURN at position {self.peek().pos}")
        self.next()
        return ("var", defs, self.expr())

    def or_(self):
        left = self.and_()
        while self.accept("||"):
            left = ("bin", "||", left, self.and_())
        return left

    def and_(self):
        left = self.not_()
        while self.accept("&&"):
            left = ("bin", "&&", left, self.not_())
        return left

    def not_(self):
        t = self.peek()
        if self.is_kw(t, "NOT") and not self.is_op(self.peek(1), "("):
            self.next()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        left = self.concat()
        t = self.peek()
        if self.is_op(t, "=", "==", "<>", "<", ">", "<=", ">="):
            self.next()
            return ("bin", t.val, left, self.concat())
        if self.is_kw(t, "IN"):
            self.next()
            return ("in", left, self.concat())
        if self.is_kw(t, "NOT") and self.is_kw(self.peek(1), "IN"):
            self.next()
            self.next()
            return ("not", ("in", left, self.concat()))
        return left

    def concat(self):
        left = self.add()
        while self.accept("&"):
            left = ("bin", "&", left, self.add())
        return left

    def add(self):
        left = self.mul()
        while True:
            t = self.accept("+", "-")
            if not t:
                return left
            left = ("bin", t.val, left, self.mul())

    def mul(self):
        left = self.unary()
        while True:
            t = self.accept("*", "/")
            if not t:
                return left
            left = ("bin", t.val, left, self.unary())

    def unary(self):
        if self.accept("-"):
            return ("neg", self.unary())
        if self.accept("+"):
            return self.unary()
        return self.power()

    def power(self):
        base = self.primary()
        if self.accept("^"):
            return ("bin", "^", base, self.unary())
        return base

    def primary(self):
        t = self.next()
        if t.kind == "NUM":
            return ("num", t.val)
        if t.kind == "STR":
            return ("str", t.val)
        if t.kind == "COL":
            return ("col", t.val[0], t.val[1])
        if t.kind == "REF":
            return ("ref", t.val)
        if t.kind == "TBL":
            return ("table", t.val)
        if t.kind == "IDENT":
            if self.is_op(self.peek(), "("):
                self.next()
                args = []
                if not self.accept(")"):
                    while True:
                        if self.is_op(self.peek(), ",", ")"):
                            args.append(("empty",))
                        else:
                            args.append(self.expr())
                        if self.accept(")"):
                            break
                        self.expect(",")
                return ("func", str(t.val).upper(), args)
            up = str(t.val).upper()
            if up in ("TRUE", "FALSE"):
                return ("bool", up == "TRUE")
            return ("name", t.val)
        if self.is_op(t, "("):
            e = self.expr()
            if self.accept(","):
                items = [e, self.expr()]
                while self.accept(","):
                    items.append(self.expr())
                self.expect(")")
                return ("tuple", items)
            self.expect(")")
            return e
        if self.is_op(t, "{"):
            items = []
            if not self.accept("}"):
                while True:
                    items.append(self.expr())
                    if self.accept("}"):
                        break
                    self.expect(",")
            return ("tablector", items)
        raise DaxSyntaxError(f"Unexpected {t.val!r} at position {t.pos}")


def parse_dax(text: str):
    """Parse a DAX expression. Leading '=' (as typed in the formula bar) is tolerated."""
    text = (text or "").strip()
    if text.startswith("=") and not text.startswith("=="):
        text = text[1:]
    if not text:
        raise DaxSyntaxError("Empty expression")
    return _Parser(text).parse()


def walk(node):
    """Yield every node of an AST (depth first)."""
    if not isinstance(node, tuple) or not node:
        return
    yield node
    if node[0] == "var":
        for _, e in node[1]:
            yield from walk(e)
        yield from walk(node[2])
        return
    for child in node[1:]:
        if isinstance(child, tuple):
            yield from walk(child)
        elif isinstance(child, list):
            for c in child:
                yield from walk(c)


def functions_used(node) -> set[str]:
    return {n[1] for n in walk(node) if n and n[0] == "func"}
