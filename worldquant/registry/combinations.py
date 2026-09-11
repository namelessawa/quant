"""Factor-combination memory extraction.

From an expression AST we identify the principal *blend* nodes — ``add``,
``subtract``, ``multiply``, ``divide``, ``trade_when`` and
``group_neutralize`` — and label each leg with its factor family. This records
that e.g. ``PRICE_REVERSAL + LIQUIDITY`` has already been tried N times, even
when every concrete expression differs.

Order rules follow the math rather than the text:

* ``A + B`` and ``B + A`` normalize to one key (commutative).
* ``A - B`` and ``B - A`` stay distinct (non-commutative).
* ``trade_when(A, B, ...)`` is ordered: trigger first, alpha second.
"""

from __future__ import annotations

from dataclasses import dataclass

from .expr_parser import (
    GROUP_LABELS,
    LANGUAGE_CONSTANTS,
    AssignNode,
    BinOpNode,
    CallNode,
    Node,
    VarNode,
    walk,
)
from .families import FieldFamilyResolver, subexpression_family

#: Binary ops treated as factor combinations.
COMBINE_BINOPS = {
    "+": "ADD",
    "-": "SUBTRACT",
    "*": "MULTIPLY",
    "/": "DIVIDE",
}
#: Ops whose legs are unordered.
COMMUTATIVE = frozenset({"+", "*"})

#: Call-based combine operators: function name -> (index of leg A, index of leg B).
#: ``trade_when(trigger, alpha, exit)`` combines a regime trigger with an alpha;
#: ``group_neutralize(alpha, group)`` combines an alpha with a grouping label.
COMBINE_CALLS = {
    "trade_when": (0, 1, False),       # (leg_a_idx, leg_b_idx, commutative)
    "group_neutralize": (0, 1, False),
}

GROUP_LEG = "GROUP"


@dataclass(frozen=True)
class Combination:
    key: str
    family_a: str
    family_b: str
    operator: str


def _leg_fields(node: Node | None, assigned: set[str]) -> list[str]:
    if node is None:
        return []
    fields: list[str] = []
    seen: set[str] = set()
    for part in walk(node):
        if not isinstance(part, VarNode):
            continue
        lowered = part.name.lower()
        if (
            lowered in LANGUAGE_CONSTANTS
            or lowered in GROUP_LABELS
            or lowered in {token.lower() for token in assigned}
        ):
            continue
        if lowered not in seen:
            seen.add(lowered)
            fields.append(part.name)
    return fields


def _leg_family(node: Node | None, assigned: set[str], resolver: FieldFamilyResolver) -> str:
    if node is None:
        return "UNKNOWN"
    operators: set[str] = set()
    for part in walk(node):
        if isinstance(part, CallNode):
            operators.add(part.name)
    return subexpression_family(
        _leg_fields(node, assigned), operators, resolver=resolver
    )


def _make_combination(
    family_a: str,
    family_b: str,
    operator: str,
    *,
    commutative: bool,
) -> Combination | None:
    if family_a in {"UNKNOWN", "OTHER"} or family_b in {"UNKNOWN", "OTHER"}:
        return None
    if commutative and family_a > family_b:
        family_a, family_b = family_b, family_a
    return Combination(
        key=f"{family_a}|{operator}|{family_b}",
        family_a=family_a,
        family_b=family_b,
        operator=operator,
    )


def extract_combinations(
    statements: list[Node],
    assigned: set[str],
    *,
    resolver: FieldFamilyResolver | None = None,
) -> list[Combination]:
    """Distinct combinations appearing anywhere in the expression.

    A key is returned at most once per expression so a single trial cannot
    inflate a combination's trial counter through nested repeats.
    """
    resolver = resolver or FieldFamilyResolver()
    found: dict[str, Combination] = {}

    for statement in statements:
        scan_root = statement.value if isinstance(statement, AssignNode) else statement
        for node in walk(scan_root):
            combo: Combination | None = None
            if isinstance(node, BinOpNode) and node.op in COMBINE_BINOPS:
                family_a = _leg_family(node.left, assigned, resolver)
                family_b = _leg_family(node.right, assigned, resolver)
                combo = _make_combination(
                    family_a, family_b, COMBINE_BINOPS[node.op],
                    commutative=node.op in COMMUTATIVE,
                )
            elif isinstance(node, CallNode) and node.name.lower() in COMBINE_CALLS:
                idx_a, idx_b, commutative = COMBINE_CALLS[node.name.lower()]
                if node.name.lower() == "group_neutralize":
                    family_a = _leg_family(
                        node.args[idx_a] if idx_a < len(node.args) else None,
                        assigned, resolver,
                    )
                    family_b = GROUP_LEG
                else:
                    family_a = _leg_family(
                        node.args[idx_a] if idx_a < len(node.args) else None,
                        assigned, resolver,
                    )
                    family_b = _leg_family(
                        node.args[idx_b] if idx_b < len(node.args) else None,
                        assigned, resolver,
                    )
                combo = _make_combination(
                    family_a, family_b, node.name.lower().upper(),
                    commutative=commutative,
                )
            if combo is not None:
                found.setdefault(combo.key, combo)

    return list(found.values())
