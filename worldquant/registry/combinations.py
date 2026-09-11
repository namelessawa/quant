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

import hashlib
from dataclasses import dataclass

from .expr_parser import (
    GROUP_LABELS,
    LANGUAGE_CONSTANTS,
    AssignNode,
    BinOpNode,
    CallNode,
    Node,
    RenderContext,
    VarNode,
    _collect_window_literals,
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
    #: Per-leg fingerprints (ordered like family_a/family_b). A group/sector
    #: leg (no value expression) is represented by ``None``.
    left_leg: dict | None = None
    right_leg: dict | None = None
    #: Hash of the whole blend shape: operator + ordered leg structure hashes.
    #: Commutative operators sort the legs first, so ``A+B`` and ``B+A`` share
    #: it while ``A-B`` and ``B-A`` do not.
    structure_hash: str | None = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _leg_fingerprint(
    node: Node | None,
    assigned: set[str],
    resolver: FieldFamilyResolver,
    ctx: RenderContext,
) -> dict | None:
    if node is None:
        return None
    operators = {part.name for part in walk(node) if isinstance(part, CallNode)}
    family = subexpression_family(
        _leg_fields(node, assigned), operators, resolver=resolver
    )
    return {
        "family": family,
        "template_hash": _sha256(node.render("template", ctx)),
        "structure_hash": _sha256(node.render("structure", ctx)),
        "template": node.render("template", ctx),
        "structure": node.render("structure", ctx),
    }


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
    left_leg: dict | None = None,
    right_leg: dict | None = None,
) -> Combination | None:
    if family_a in {"UNKNOWN", "OTHER"} or family_b in {"UNKNOWN", "OTHER"}:
        return None
    if commutative:
        # Normalize the family order; re-order the leg fingerprints in lockstep
        # so left_leg always describes family_a.
        pairs = [(family_a, left_leg), (family_b, right_leg)]
        pairs.sort(key=lambda item: item[0])
        family_a, left_leg = pairs[0]
        family_b, right_leg = pairs[1]
    left_hash = (left_leg or {}).get("structure_hash", "")
    right_hash = (right_leg or {}).get("structure_hash", "")
    if commutative:
        ordered = sorted([left_hash, right_hash])
        shape = f"{operator}|{ordered[0]}|{ordered[1]}"
    else:
        # Non-commutative blends keep leg order. Pure structure renders are
        # field-agnostic (``rank(close)`` and ``rank(volume)`` share one
        # structure), so fold each leg's family into the ordered identity:
        # A-B and B-A must not collapse onto the same shape hash.
        shape = (
            f"{operator}|{family_a}:{left_hash}|{family_b}:{right_hash}"
        )
    return Combination(
        key=f"{family_a}|{operator}|{family_b}",
        family_a=family_a,
        family_b=family_b,
        operator=operator,
        left_leg=left_leg,
        right_leg=right_leg,
        structure_hash=_sha256(shape),
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
    ctx = RenderContext(
        assigned=assigned,
        window_literals=_collect_window_literals(statements),
        family_resolver=resolver,
    )
    found: dict[str, Combination] = {}

    for statement in statements:
        scan_root = statement.value if isinstance(statement, AssignNode) else statement
        for node in walk(scan_root):
            combo: Combination | None = None
            if isinstance(node, BinOpNode) and node.op in COMBINE_BINOPS:
                commutative = node.op in COMMUTATIVE
                family_a = _leg_family(node.left, assigned, resolver)
                family_b = _leg_family(node.right, assigned, resolver)
                left_leg = _leg_fingerprint(node.left, assigned, resolver, ctx)
                right_leg = _leg_fingerprint(node.right, assigned, resolver, ctx)
                combo = _make_combination(
                    family_a, family_b, COMBINE_BINOPS[node.op],
                    commutative=commutative,
                    left_leg=left_leg, right_leg=right_leg,
                )
            elif isinstance(node, CallNode) and node.name.lower() in COMBINE_CALLS:
                idx_a, idx_b, commutative = COMBINE_CALLS[node.name.lower()]
                leg_node_a = node.args[idx_a] if idx_a < len(node.args) else None
                family_a = _leg_family(leg_node_a, assigned, resolver)
                left_leg = _leg_fingerprint(leg_node_a, assigned, resolver, ctx)
                if node.name.lower() == "group_neutralize":
                    family_b = GROUP_LEG
                    right_leg = None
                else:
                    leg_node_b = node.args[idx_b] if idx_b < len(node.args) else None
                    family_b = _leg_family(leg_node_b, assigned, resolver)
                    right_leg = _leg_fingerprint(
                        leg_node_b, assigned, resolver, ctx
                    )
                combo = _make_combination(
                    family_a, family_b, node.name.lower().upper(),
                    commutative=commutative,
                    left_leg=left_leg, right_leg=right_leg,
                )
            if combo is not None:
                found.setdefault(combo.key, combo)

    return list(found.values())
