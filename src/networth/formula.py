"""Small formula language for computed columns.

Formulas reference other columns of the same entry by name and translate 1:1 to Excel formulas:

* ``{Girokonto}``                 value of the column in the same entry       -> ``B5``
* ``{Summe@prev}``                value in the previous entry (by date)       -> ``P4``
* ``{Summe@first}``               value in the first entry                    -> ``$P$2``
* ``SUM({Girokonto}:{Flatex Geld})``  all columns between the two (by position) -> ``SUM(B5:F5)``
* ``+ - * / ^``, comparisons ``= <> < > <= >=``, ``IF``, ``SUM``, ``MIN``, ``MAX``, ``ABS``, ``ROUND``

Empty cells count as 0 like in Excel. An error (division by zero, missing previous entry) makes the cell empty.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Union


class FormulaError(ValueError):
    """The formula can't be parsed or references something that doesn't exist."""


class EvalError(ArithmeticError):
    """The formula can't be evaluated for one entry (e.g. division by zero); the cell stays empty."""


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Num:
    value: float
    text: str


@dataclass(frozen=True)
class Ref:
    name: str
    mode: str = "row"  # row | prev | first


@dataclass(frozen=True)
class Range:
    start: Ref
    end: Ref


@dataclass(frozen=True)
class Unary:
    op: str
    operand: "Node"


@dataclass(frozen=True)
class Binary:
    op: str
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class Paren:
    inner: "Node"


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple["Node", ...]


Node = Union[Num, Ref, Range, Unary, Binary, Paren, Call]

FUNCTIONS = {"IF", "SUM", "MIN", "MAX", "ABS", "ROUND"}
COMPARISONS = {"=": "=", "==": "=", "<>": "<>", "!=": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">="}
MODES = {"": "row", "prev": "prev", "first": "first"}

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+)
  | (?P<ref>\{[^{}]*\})
  | (?P<ident>[A-Za-z_]\w*)
  | (?P<op><>|!=|<=|>=|==|[-+*/^(),:<>=])
""", re.VERBOSE)


def _tokenize(source: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(source):
        match = _TOKEN.match(source, pos)
        if not match:
            raise FormulaError(f"Unerwartetes Zeichen {source[pos]!r} an Position {pos + 1}")
        if match.lastgroup != "ws":
            tokens.append((match.lastgroup, match.group()))
        pos = match.end()
    return tokens


def _parse_ref(token: str) -> Ref:
    body = token[1:-1]
    name, _, mode = body.partition("@")
    name, mode = name.strip(), mode.strip().lower()
    if not name:
        raise FormulaError("Leerer Spaltenverweis {}")
    if mode not in MODES:
        raise FormulaError(f"Unbekannter Verweis-Modus @{mode} (erlaubt: @prev, @first)")
    return Ref(name, MODES[mode])


class _Parser:
    def __init__(self, source: str):
        self.tokens = _tokenize(source)
        self.pos = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if token is None or (value is not None and token[1] != value):
            raise FormulaError(f"Erwartet {value or 'Ausdruck'}, gefunden {token[1] if token else 'Ende'}")
        self.pos += 1
        return token

    def parse(self) -> Node:
        if not self.tokens:
            raise FormulaError("Leere Formel")
        node = self.comparison()
        if self.peek() is not None:
            raise FormulaError(f"Unerwartetes {self.peek()[1]!r}")
        return node

    def comparison(self) -> Node:
        node = self.additive()
        token = self.peek()
        if token and token[1] in COMPARISONS:
            self.take()
            node = Binary(COMPARISONS[token[1]], node, self.additive())
        return node

    def additive(self) -> Node:
        node = self.term()
        while (token := self.peek()) and token[1] in "+-":
            self.take()
            node = Binary(token[1], node, self.term())
        return node

    def term(self) -> Node:
        node = self.power()
        while (token := self.peek()) and token[1] in "*/":
            self.take()
            node = Binary(token[1], node, self.power())
        return node

    def power(self) -> Node:
        node = self.unary()
        while (token := self.peek()) and token[1] == "^":
            self.take()
            node = Binary("^", node, self.unary())
        return node

    def unary(self) -> Node:
        token = self.peek()
        if token and token[1] in "+-":
            self.take()
            return Unary(token[1], self.unary())
        return self.primary(allow_range=False)

    def primary(self, allow_range: bool) -> Node:
        kind, value = self.take()
        if kind == "num":
            return Num(float(value), value)
        if kind == "ref":
            ref = _parse_ref(value)
            if (token := self.peek()) and token[1] == ":":
                if not allow_range:
                    raise FormulaError("Bereiche wie {A}:{B} sind nur in SUM, MIN und MAX erlaubt")
                self.take()
                kind, value = self.take()
                if kind != "ref":
                    raise FormulaError("Nach ':' muss ein Spaltenverweis folgen")
                end = _parse_ref(value)
                if ref.mode != "row" or end.mode != "row":
                    raise FormulaError("Bereiche gehen nur über Spalten desselben Eintrags")
                return Range(ref, end)
            return ref
        if kind == "ident":
            name = value.upper()
            if name not in FUNCTIONS:
                raise FormulaError(f"Unbekannte Funktion {value}")
            self.take("(")
            args = []
            if self.peek() and self.peek()[1] != ")":
                args.append(self.argument(name))
                while self.peek() and self.peek()[1] == ",":
                    self.take()
                    args.append(self.argument(name))
            self.take(")")
            _check_arity(name, len(args))
            return Call(name, tuple(args))
        if value == "(":
            node = self.comparison()
            self.take(")")
            return Paren(node)
        raise FormulaError(f"Unerwartetes {value!r}")

    def argument(self, function: str) -> Node:
        if function in {"SUM", "MIN", "MAX"}:
            start = self.pos
            if self.peek() and self.peek()[0] == "ref":
                node = self.primary(allow_range=True)
                if isinstance(node, Range) and (self.peek() is None or self.peek()[1] in ",)"):
                    return node
                self.pos = start  # not a plain range: parse as a normal expression
        return self.comparison()


def _check_arity(name: str, count: int) -> None:
    limits = {"IF": (2, 3), "ABS": (1, 1), "ROUND": (1, 2)}
    low, high = limits.get(name, (1, 255))
    if not low <= count <= high:
        raise FormulaError(f"{name} erwartet {low}–{high} Argumente, nicht {count}")


def parse(source: str) -> Node:
    return _Parser(source).parse()


# ---------------------------------------------------------------------------
# Inspection and rendering
# ---------------------------------------------------------------------------

def walk(node: Node) -> Iterable[Node]:
    yield node
    if isinstance(node, Range):
        yield node.start
        yield node.end
    elif isinstance(node, Unary):
        yield from walk(node.operand)
    elif isinstance(node, Binary):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, Paren):
        yield from walk(node.inner)
    elif isinstance(node, Call):
        for arg in node.args:
            yield from walk(arg)


def references(node: Node) -> set[str]:
    """Names of all columns referenced directly (ranges: only their two ends)."""
    return {n.name for n in walk(node) if isinstance(n, Ref)}


def ranges(node: Node) -> list[Range]:
    return [n for n in walk(node) if isinstance(n, Range)]


def uses_previous(node: Node) -> bool:
    return any(isinstance(n, Ref) and n.mode == "prev" for n in walk(node))


def render(node: Node, ref: Callable[[Ref], str]) -> str:
    """Formula text; ``ref`` renders a column reference (used for our syntax and for Excel)."""
    if isinstance(node, Num):
        return node.text
    if isinstance(node, Ref):
        return ref(node)
    if isinstance(node, Range):
        return f"{ref(node.start)}:{ref(node.end)}"
    if isinstance(node, Unary):
        return node.op + render(node.operand, ref)
    if isinstance(node, Binary):
        return f"{render(node.left, ref)}{node.op}{render(node.right, ref)}"
    if isinstance(node, Paren):
        return f"({render(node.inner, ref)})"
    return f"{node.name}({','.join(render(arg, ref) for arg in node.args)})"


def source_ref(ref: Ref) -> str:
    return "{" + ref.name + ("" if ref.mode == "row" else "@" + ref.mode) + "}"


def rename(source: str, old: str, new: str) -> str:
    """Formula text with every reference to column ``old`` pointing to ``new``."""
    node = parse(source)
    return render(node, lambda r: source_ref(Ref(new, r.mode) if r.name == old else r))


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

Value = Union[float, None]


class Context:
    """Supplies column values while evaluating one entry."""

    def value(self, ref: Ref) -> Value:
        raise NotImplementedError

    def range_values(self, rng: Range) -> list[Value]:
        raise NotImplementedError


def _num(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    return float(value)


def evaluate(node: Node, ctx: Context):
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Ref):
        return ctx.value(node)
    if isinstance(node, Paren):
        return evaluate(node.inner, ctx)
    if isinstance(node, Unary):
        value = _num(evaluate(node.operand, ctx))
        return -value if node.op == "-" else value
    if isinstance(node, Binary):
        left, right = evaluate(node.left, ctx), evaluate(node.right, ctx)
        op = node.op
        if op in ("=", "<>", "<", ">", "<=", ">="):
            a, b = _num(left), _num(right)
            return {"=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
        a, b = _num(left), _num(right)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                raise EvalError("Division durch 0")
            return a / b
        try:
            return float(a ** b)
        except (OverflowError, ZeroDivisionError, TypeError) as exc:
            raise EvalError(str(exc)) from exc
    if isinstance(node, Call):
        return _call(node, ctx)
    raise FormulaError("Bereich außerhalb von SUM/MIN/MAX")


def _call(node: Call, ctx: Context):
    name = node.name
    if name == "IF":
        condition = evaluate(node.args[0], ctx)
        if _num(condition) != 0:
            return evaluate(node.args[1], ctx)
        return evaluate(node.args[2], ctx) if len(node.args) > 2 else False
    if name in {"SUM", "MIN", "MAX"}:
        values: list[float] = []
        for arg in node.args:
            if isinstance(arg, Range):
                values += [float(v) for v in ctx.range_values(arg) if v is not None]  # Excel skips empty cells
            else:
                values.append(_num(evaluate(arg, ctx)))
        if name == "SUM":
            return math.fsum(values)
        if not values:
            return 0.0
        return min(values) if name == "MIN" else max(values)
    if name == "ABS":
        return abs(_num(evaluate(node.args[0], ctx)))
    digits = int(_num(evaluate(node.args[1], ctx))) if len(node.args) > 1 else 0
    value = _num(evaluate(node.args[0], ctx))
    scaled = abs(value) * 10 ** digits
    return math.copysign(math.floor(scaled + 0.5) / 10 ** digits, value)  # Excel rounds half away from zero


# ---------------------------------------------------------------------------
# Plain numbers typed into input fields ("6604.12+895.2")
# ---------------------------------------------------------------------------

_NUMBER_EXPR = re.compile(r"^[\d\s.,+\-*/()^]+$")


class _NoRefs(Context):
    def value(self, ref: Ref) -> Value:
        raise FormulaError("Verweise sind in Eingabefeldern nicht erlaubt")

    def range_values(self, rng: Range) -> list[Value]:
        raise FormulaError("Verweise sind in Eingabefeldern nicht erlaubt")


def normalize_number_input(text: str) -> str:
    """Accept German notation: "1.234,56" -> "1234.56"; plain "1234.56" stays."""
    text = text.strip().lstrip("=").replace("€", "").replace(" ", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    return text


def parse_number_input(text: str) -> tuple[float | None, str]:
    """Value and the expression to keep (empty if the input is a plain number).

    Raises ``FormulaError`` for anything that isn't a number or a pure arithmetic expression.
    """
    text = normalize_number_input(text)
    if not text:
        return None, ""
    if not _NUMBER_EXPR.match(text):
        raise FormulaError(f"Keine Zahl: {text!r}")
    try:
        return float(text), ""
    except ValueError:
        pass
    try:
        value = evaluate(parse(text), _NoRefs())
    except EvalError as exc:
        raise FormulaError(str(exc)) from exc
    return float(value), text


def is_number_expression(text: str) -> bool:
    return bool(_NUMBER_EXPR.match(text)) and any(c.isdigit() for c in text)
