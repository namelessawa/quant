"""Deterministic FASTEXPR parser, structural fingerprints and feature extraction.

The parser is a small recursive-descent reader for the BRAIN expression dialect:
function calls, binary/unary operators, numeric/string constants and the
multi-statement ``name = expr; expr`` form. It deliberately understands no
semantics — it only records *structure* — so it needs no network access and can
never disagree with BRAIN about what an expression means.

Everything built on top of the AST is used for research bookkeeping only
(fingerprints, novelty, families, combinations). A parse failure therefore never
blocks a simulation: callers get :class:`ExpressionFeatures` with whatever the
fallback regex tokenizer could recover plus an ``unknown_tokens`` list.

Three fingerprints are produced, matching the project brief::

    rank(ts_delta(close,5))

      exact      : rank(ts_delta(close,5))
      template   : rank(ts_delta(close,<WINDOW>))
      field_tmpl : rank(ts_delta(<FIELD>,<WINDOW>))
      family_tmpl: rank(ts_delta(PRICE,<WINDOW>))
      structure  : rank(ts_delta(X,N))
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Union

# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<number>\d+\.\d+(?:[eE][+-]?\d+)?|\.\d+|\d+(?:[eE][+-]?\d+)?)
  | (?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|==|!=|&&|\|\||[+\-*/%<>,;()=?!&|^~])
    """,
    re.VERBOSE,
)

#: Time-series call prefix. Integer arguments to these calls are window sizes.
TS_PREFIX = "ts_"

#: Multi-statement locals / language constants / grouping labels that are not
#: data fields. Grouping labels appear as the second argument of group_* calls
#: (``group_zscore(x, subindustry)``) and classify the cross-section rather than
#: carrying a value, so they must never be counted as fields.
LANGUAGE_CONSTANTS = frozenset(
    {"if", "else", "and", "or", "not", "true", "false", "nan", "inf", "null"}
)
GROUP_LABELS = frozenset(
    {
        "sector", "industry", "subindustry", "market", "country", "exchange",
        "currency", "group", "densify",
    }
)

#: Binary operators rendered with stable names inside fingerprints/features.
_BINOP_NAMES = {
    "+": "add",
    "-": "subtract",
    "*": "multiply",
    "/": "divide",
    "%": "modulo",
    "==": "eq",
    "!=": "neq",
    "<": "lt",
    "<=": "lte",
    ">": "gt",
    ">=": "gte",
    "&": "bit_and",
    "|": "bit_or",
    "&&": "logical_and",
    "||": "logical_or",
    "^": "bit_xor",
}
_COMMUTATIVE_OPS = frozenset({"+", "*", "&", "|", "&&", "||"})


class ParseError(ValueError):
    """Raised when an expression cannot be tokenized/parsed as FASTEXPR."""


# --------------------------------------------------------------------------- #
# AST node hierarchy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NumberNode:
    value: float
    raw: str
    is_integer: bool

    def _is_window(self, ctx: "RenderContext") -> bool:
        return self.is_integer and int(self.value) in ctx.window_literals

    def render(self, mode: str, ctx: "RenderContext") -> str:
        if mode == "exact":
            return self.raw
        if self._is_window(ctx):
            return "N" if mode == "structure" else "<WINDOW>"
        return "X" if mode == "structure" else self.raw


@dataclass(frozen=True)
class StringNode:
    value: str

    def render(self, mode: str, ctx: "RenderContext") -> str:
        return "X" if mode == "structure" else repr(self.value)


@dataclass(frozen=True)
class VarNode:
    name: str

    def render(self, mode: str, ctx: "RenderContext") -> str:
        if mode == "exact":
            return self.name
        if mode == "template":
            return self.name
        if self.name in ctx.assigned:
            # Locals keep a generic marker in field/structure templates.
            return "X" if mode in {"field", "structure"} else self.name
        if mode == "field":
            return "<FIELD>"
        if mode == "family":
            return ctx.family_of(self.name)
        return "X"  # structure


@dataclass(frozen=True)
class KeywordNode:
    """Boolean-ish operator keywords rendered like calls (``and(x, y)``)."""

    name: str
    args: tuple["Node", ...]

    def render(self, mode: str, ctx: "RenderContext") -> str:
        joined = ",".join(arg.render(mode, ctx) for arg in self.args)
        return f"{self.name}({joined})"


@dataclass(frozen=True)
class CallNode:
    name: str
    args: tuple["Node", ...]
    kwargs: tuple[tuple[str, "Node"], ...]

    def render(self, mode: str, ctx: "RenderContext") -> str:
        parts = [arg.render(mode, ctx) for arg in self.args]
        for key, value in self.kwargs:
            parts.append(f"{key}={value.render(mode, ctx)}")
        return f"{self.name}({','.join(parts)})"


@dataclass(frozen=True)
class BinOpNode:
    op: str
    left: "Node"
    right: "Node"

    def render(self, mode: str, ctx: "RenderContext") -> str:
        if mode == "exact":
            return f"({self.left.render(mode, ctx)}{self.op}{self.right.render(mode, ctx)})"
        return f"{_BINOP_NAMES.get(self.op, 'op')}({self.left.render(mode, ctx)},{self.right.render(mode, ctx)})"


@dataclass(frozen=True)
class UnaryOpNode:
    op: str
    operand: "Node"

    def render(self, mode: str, ctx: "RenderContext") -> str:
        if mode == "exact":
            return f"({self.op}{self.operand.render(mode, ctx)})"
        name = "negative" if self.op == "-" else f"unary_{self.op}"
        return f"{name}({self.operand.render(mode, ctx)})"


@dataclass(frozen=True)
class TernaryNode:
    condition: "Node"
    when_true: "Node"
    when_false: "Node"

    def render(self, mode: str, ctx: "RenderContext") -> str:
        return (
            f"ternary({self.condition.render(mode, ctx)},"
            f"{self.when_true.render(mode, ctx)},{self.when_false.render(mode, ctx)})"
        )


@dataclass(frozen=True)
class AssignNode:
    target: str
    value: "Node"

    def render(self, mode: str, ctx: "RenderContext") -> str:
        if mode == "exact":
            return f"{self.target}={self.value.render(mode, ctx)}"
        return f"assign({self.target},{self.value.render(mode, ctx)})"


Node = Union[
    NumberNode, StringNode, VarNode, KeywordNode, CallNode,
    BinOpNode, UnaryOpNode, TernaryNode, AssignNode,
]


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
@dataclass
class _Token:
    kind: str
    text: str
    pos: int


def _tokenize(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    while pos < len(expression):
        match = _TOKEN_RE.match(expression, pos)
        if not match:
            raise ParseError(f"unexpected character {expression[pos]!r} at position {pos}")
        kind = match.lastgroup or ""
        text = match.group()
        pos = match.end()
        if kind != "ws":
            tokens.append(_Token(kind, text, match.start()))
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.assigned: set[str] = set()

    def _peek(self) -> _Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self) -> _Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _accept(self, kind: str, text: str | None = None) -> _Token | None:
        token = self._peek()
        if token is None or token.kind != kind:
            return None
        if text is not None and token.text != text:
            return None
        return self._take()

    def _expect(self, text: str) -> None:
        token = self._accept("op", text)
        if token is None:
            current = self._peek()
            found = current.text if current else "<end>"
            raise ParseError(f"expected {text!r}, found {found!r}")

    def parse_program(self) -> list[Node]:
        statements = [self.parse_statement()]
        while self._accept("op", ";"):
            if self._peek() is None:
                break  # trailing semicolon
            statements.append(self.parse_statement())
        if self._peek() is not None:
            token = self._peek()
            raise ParseError(f"unexpected token {token.text!r} at position {token.pos}")
        return statements

    def parse_statement(self) -> Node:
        # Assignment: IDENT '=' expression  (but not '==' comparison)
        token = self._peek()
        if token is not None and token.kind == "ident":
            second = self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else None
            if second is not None and second.kind == "op" and second.text == "=":
                target = self._take().text
                self._take()  # '='
                self.assigned.add(target)
                return AssignNode(target=target, value=self.parse_expression())
        return self.parse_expression()

    def parse_expression(self) -> Node:
        return self.parse_ternary()

    def parse_ternary(self) -> Node:
        condition = self.parse_logical()
        if self._accept("op", "?"):
            when_true = self.parse_expression()
            self._expect(":")
            when_false = self.parse_expression()
            return TernaryNode(condition, when_true, when_false)
        return condition

    def parse_logical(self) -> Node:
        node = self.parse_equality()
        while True:
            token = self._peek()
            if token is None or token.kind != "op" or token.text not in {"&&", "||", "&", "|"}:
                return node
            self._take()
            node = BinOpNode(token.text, node, self.parse_equality())

    def parse_equality(self) -> Node:
        node = self.parse_comparison()
        while True:
            token = self._peek()
            if token is None or token.kind != "op" or token.text not in {"==", "!="}:
                return node
            self._take()
            node = BinOpNode(token.text, node, self.parse_comparison())

    def parse_comparison(self) -> Node:
        node = self.parse_additive()
        while True:
            token = self._peek()
            if token is None or token.kind != "op" or token.text not in {"<", "<=", ">", ">="}:
                return node
            self._take()
            node = BinOpNode(token.text, node, self.parse_additive())

    def parse_additive(self) -> Node:
        node = self.parse_multiplicative()
        while True:
            token = self._peek()
            if token is None or token.kind != "op" or token.text not in {"+", "-"}:
                return node
            self._take()
            node = BinOpNode(token.text, node, self.parse_multiplicative())

    def parse_multiplicative(self) -> Node:
        node = self.parse_unary()
        while True:
            token = self._peek()
            if token is None or token.kind != "op" or token.text not in {"*", "/", "%", "^"}:
                return node
            self._take()
            node = BinOpNode(token.text, node, self.parse_unary())

    def parse_unary(self) -> Node:
        token = self._peek()
        if token is not None and token.kind == "op" and token.text in {"-", "+", "!", "~"}:
            self._take()
            return UnaryOpNode(token.text, self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        node = self.parse_primary()
        # Function-style keyword operators: and(a,b), or(a,b), not(a)
        return node

    def parse_primary(self) -> Node:
        token = self._peek()
        if token is None:
            raise ParseError("unexpected end of expression")

        if token.kind == "number":
            self._take()
            return self._number(token.text)

        if token.kind == "string":
            self._take()
            return StringNode(value=token.text[1:-1])

        if token.kind == "op" and token.text == "(":
            self._take()
            inner = self.parse_expression()
            self._expect(")")
            return inner

        if token.kind == "ident":
            self._take()
            if self._accept("op", "("):
                return self._parse_call_tail(token.text)
            lowered = token.text.lower()
            if lowered in LANGUAGE_CONSTANTS:
                # Bare constants are rendered as variables of no family.
                return VarNode(token.text)
            return VarNode(token.text)

        raise ParseError(f"unexpected token {token.text!r} at position {token.pos}")

    @staticmethod
    def _number(text: str) -> NumberNode:
        is_integer = bool(re.fullmatch(r"\d+", text))
        return NumberNode(value=float(text), raw=text, is_integer=is_integer)

    def _parse_call_tail(self, name: str) -> Node:
        args: list[Node] = []
        kwargs: list[tuple[str, Node]] = []
        if not self._accept("op", ")"):
            while True:
                # Named argument: IDENT '=' expr  (but never '==')
                token = self._peek()
                second = self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else None
                if (
                    token is not None and token.kind == "ident"
                    and second is not None and second.kind == "op" and second.text == "="
                ):
                    key = self._take().text
                    self._take()
                    kwargs.append((key, self.parse_expression()))
                else:
                    args.append(self.parse_expression())
                if self._accept("op", ","):
                    continue
                break
            self._expect(")")
        lowered = name.lower()
        if lowered in {"and", "or"}:
            return KeywordNode(name=name, args=tuple(args))
        return CallNode(name=name, args=tuple(args), kwargs=tuple(kwargs))


def parse_expression_ast(expression: str) -> tuple[list[Node], set[str]]:
    """Parse a (possibly multi-statement) expression.

    Returns the statement list and the set of locally assigned variable names.
    Raises :class:`ParseError` on malformed input.
    """
    tokens = _tokenize(expression)
    if not tokens:
        raise ParseError("empty expression")
    parser = _Parser(tokens)
    return parser.parse_program(), parser.assigned


# --------------------------------------------------------------------------- #
# AST walking helpers
# --------------------------------------------------------------------------- #
def walk(node: Node):
    """Yield every AST node (pre-order)."""
    yield node
    if isinstance(node, (CallNode, KeywordNode)):
        for arg in node.args:
            yield from walk(arg)
        if isinstance(node, CallNode):
            for _, value in node.kwargs:
                yield from walk(value)
    elif isinstance(node, BinOpNode):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, UnaryOpNode):
        yield from walk(node.operand)
    elif isinstance(node, TernaryNode):
        yield from walk(node.condition)
        yield from walk(node.when_true)
        yield from walk(node.when_false)
    elif isinstance(node, AssignNode):
        yield from walk(node.value)


def final_expression(statements: list[Node]) -> Node:
    """The value-producing statement (the last one)."""
    return statements[-1]


def tree_depth(node: Node) -> int:
    """Longest operator/call chain; leaves (fields/constants) contribute 0.

    ``rank(ts_zscore(divide(ts_mean(volume), ts_mean(volume)), 120))`` -> 4.
    """
    if isinstance(node, (NumberNode, StringNode, VarNode)):
        return 0
    if isinstance(node, (CallNode, KeywordNode)):
        child_depths = [tree_depth(arg) for arg in node.args]
        if isinstance(node, CallNode):
            child_depths.extend(tree_depth(value) for _, value in node.kwargs)
        return 1 + (max(child_depths) if child_depths else 0)
    if isinstance(node, BinOpNode):
        return 1 + max(tree_depth(node.left), tree_depth(node.right))
    if isinstance(node, UnaryOpNode):
        return 1 + tree_depth(node.operand)
    if isinstance(node, TernaryNode):
        return 1 + max(
            tree_depth(node.condition), tree_depth(node.when_true), tree_depth(node.when_false)
        )
    if isinstance(node, AssignNode):
        return tree_depth(node.value)
    return 0


def root_operator(node: Node) -> str:
    """Outermost operator/call of the value-producing statement."""
    if isinstance(node, CallNode):
        return node.name
    if isinstance(node, KeywordNode):
        return node.name
    if isinstance(node, BinOpNode):
        return _BINOP_NAMES.get(node.op, node.op)
    if isinstance(node, UnaryOpNode):
        return "negative" if node.op == "-" else f"unary_{node.op}"
    if isinstance(node, TernaryNode):
        return "ternary"
    if isinstance(node, AssignNode):
        return root_operator(node.value)
    return ""


def operator_set(node: Node) -> set[str]:
    names: set[str] = set()
    for part in walk(node):
        if isinstance(part, CallNode):
            names.add(part.name)
        elif isinstance(part, KeywordNode):
            names.add(part.name)
        elif isinstance(part, BinOpNode):
            names.add(_BINOP_NAMES.get(part.op, part.op))
        elif isinstance(part, UnaryOpNode):
            names.add("negative" if part.op == "-" else f"unary_{part.op}")
        elif isinstance(part, TernaryNode):
            names.add("ternary")
    return names


# --------------------------------------------------------------------------- #
# Window / field extraction
# --------------------------------------------------------------------------- #
def _integer_value(node: Node) -> int | None:
    if isinstance(node, NumberNode) and node.is_integer:
        return int(node.value)
    return None


def extract_windows(node: Node, *, include_kwargs: bool = False) -> list[int]:
    """Integer literals passed to ``ts_*`` time-series calls.

    ``ts_zscore(ts_mean(volume,20) / ts_mean(volume,60), 120)`` -> [20, 60, 120].
    Numeric constants of ordinary calls (``std=3.0``) are parameters, not
    windows, and stay out.
    """
    windows: list[int] = []
    for part in walk(node):
        if not isinstance(part, CallNode) or not part.name.lower().startswith(TS_PREFIX):
            continue
        for arg in part.args:
            for sub in walk(arg):
                if isinstance(sub, NumberNode) and sub.is_integer:
                    windows.append(int(sub.value))
        if include_kwargs:
            for _, value in part.kwargs:
                for sub in walk(value):
                    if isinstance(sub, NumberNode) and sub.is_integer:
                        windows.append(int(sub.value))
    return windows


def extract_fields(node: Node, *, assigned: set[str]) -> list[str]:
    """Value-carrying data fields, preserving first-seen order.

    Excluded: local assignment targets in multi-statement expressions, language
    constants and grouping labels (``subindustry`` etc.).
    """
    fields: list[str] = []
    seen: set[str] = set()
    for part in walk(node):
        if not isinstance(part, VarNode):
            continue
        name = part.name
        lowered = name.lower()
        if lowered in LANGUAGE_CONSTANTS or lowered in GROUP_LABELS:
            continue
        if lowered in {token.lower() for token in assigned}:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        fields.append(name)
    return fields


# --------------------------------------------------------------------------- #
# Rendering contexts and fingerprints
# --------------------------------------------------------------------------- #
@dataclass
class RenderContext:
    assigned: set[str] = field(default_factory=set)
    window_literals: set[int] = field(default_factory=set)
    family_resolver: Any = None

    def family_of(self, field_name: str) -> str:
        if field_name.lower() in GROUP_LABELS:
            return "GROUP"
        if self.family_resolver is None:
            return "FAMILY"
        try:
            family = self.family_resolver(field_name)
        except Exception:  # noqa: BLE001 - rendering must never crash
            return "FAMILY"
        return family or "FAMILY"


def _collect_window_literals(statements: list[Node]) -> set[int]:
    literals: set[int] = set()
    for statement in statements:
        for window in extract_windows(statement):
            literals.add(window)
    return literals


def render_statements(
    statements: list[Node],
    mode: str,
    ctx: RenderContext,
) -> str:
    rendered = [statement.render(mode, ctx) for statement in statements]
    return ";".join(rendered)


# --------------------------------------------------------------------------- #
# Regex fallback (used only when the parser rejects an expression)
# --------------------------------------------------------------------------- #
_FALLBACK_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FALLBACK_NUMBER = re.compile(r"\b\d+\b")


def _fallback_features(expression: str, error: str) -> "ExpressionFeatures":
    """Recover what a regex scan can; mark uncertainty instead of crashing."""
    fields: list[str] = []
    operators: list[str] = []
    seen_f: set[str] = set()
    seen_o: set[str] = set()
    for match in _FALLBACK_IDENT.finditer(expression):
        token = match.group(0)
        if expression[match.end():].lstrip().startswith("("):
            if token.lower() not in seen_o:
                seen_o.add(token.lower())
                operators.append(token)
        elif token.lower() not in LANGUAGE_CONSTANTS and token.lower() not in GROUP_LABELS:
            if token.lower() not in seen_f:
                seen_f.add(token.lower())
                fields.append(token)
    windows = [int(n) for n in _FALLBACK_NUMBER.findall(expression)]
    return ExpressionFeatures(
        fields=fields,
        operators=operators,
        windows=windows,
        root_operator=operators[0] if operators else "",
        tree_depth=0,
        field_count=len(fields),
        operator_count=len(operators),
        unknown_tokens=[f"parse_error: {error}"],
        parsed=False,
    )


# --------------------------------------------------------------------------- #
# Public feature/fingerprint container
# --------------------------------------------------------------------------- #
@dataclass
class ExpressionFeatures:
    fields: list[str]
    operators: list[str]
    windows: list[int]
    root_operator: str
    tree_depth: int
    field_count: int
    operator_count: int
    unknown_tokens: list[str] = field(default_factory=list)
    parsed: bool = True
    #: Populated by :func:`analyze_expression`.
    canonical: str = ""
    template: str = ""
    field_template: str = ""
    family_template: str = ""
    structure: str = ""
    assigned_locals: list[str] = field(default_factory=list)

    def to_feature_json(self) -> dict[str, Any]:
        return {
            "fields": self.fields,
            "operators": self.operators,
            "windows": self.windows,
            "root_operator": self.root_operator,
            "tree_depth": self.tree_depth,
            "field_count": self.field_count,
            "operator_count": self.operator_count,
            "unknown_tokens": self.unknown_tokens,
            "parsed": self.parsed,
            "assigned_locals": self.assigned_locals,
        }


def analyze_expression(
    expression: str,
    *,
    family_resolver: Any = None,
) -> ExpressionFeatures:
    """Parse one expression and return every structural fact the registry uses.

    Never raises: a malformed expression degrades to regex-extracted features
    with ``parsed=False`` and an entry in ``unknown_tokens``, so the simulation
    pipeline (which owns the authoritative expression validation) is unaffected.
    """
    from ..hashing import normalize_expression

    canonical = normalize_expression(expression)
    try:
        statements, assigned = parse_expression_ast(canonical)
    except ParseError as exc:
        features = _fallback_features(canonical, str(exc))
        features.canonical = canonical
        return features

    value_node = final_expression(statements)
    window_literals = _collect_window_literals(statements)
    ctx = RenderContext(
        assigned=assigned,
        window_literals=window_literals,
        family_resolver=family_resolver,
    )

    # Fields/operators/windows are gathered across *every* statement: in the
    # multi-statement form the data fields live on the right-hand sides of
    # intermediate assignments (``ey = ts_backfill(anl4_ebit_value, 40) / cap``)
    # while the final statement only references the assigned locals.
    program_operators: set[str] = set()
    program_fields: list[str] = []
    program_windows: set[int] = set()
    seen_fields: set[str] = set()
    for statement in statements:
        program_operators.update(operator_set(statement))
        for field_name in extract_fields(statement, assigned=assigned):
            if field_name.lower() not in seen_fields:
                seen_fields.add(field_name.lower())
                program_fields.append(field_name)
        program_windows.update(extract_windows(statement))

    operators = sorted(program_operators)
    fields = program_fields
    windows = sorted(program_windows)

    return ExpressionFeatures(
        fields=fields,
        operators=operators,
        windows=windows,
        root_operator=root_operator(value_node),
        tree_depth=tree_depth(value_node),
        field_count=len(fields),
        operator_count=len(operators),
        unknown_tokens=[],
        parsed=True,
        canonical=canonical,
        template=render_statements(statements, "template", ctx),
        field_template=render_statements(statements, "field", ctx),
        family_template=render_statements(statements, "family", ctx),
        structure=render_statements(statements, "structure", ctx),
        assigned_locals=sorted(assigned),
    )
