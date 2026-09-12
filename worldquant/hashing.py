"""Expression normalization and de-duplication hashing.

Normalization exists **only** to compute stable hashes. The runner always
submits the original, untouched expression to BRAIN — a normalized string is
never sent over the wire, so aggressive whitespace collapsing cannot change
what gets backtested.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .api import canonical_settings_json, normalize_settings

_QUOTE_CHARS = frozenset("'\"")
_WORD_EXTRA = frozenset("_.")
#: Separates expression from settings inside the dedup hash so that
#: ``hash("a" + "bc")`` can never collide with ``hash("ab" + "c")``.
_HASH_SEPARATOR = "\x00"

#: Settings that define *what is predicted, over which instruments, with how much
#: latency*. Two runs differing only here are different alphas; two runs
#: differing only in the remaining portfolio-construction settings are not.
SCOPE_SETTINGS: tuple[str, ...] = ("region", "universe", "delay")


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char in _WORD_EXTRA


def normalize_expression(expression: str) -> str:
    """Collapse whitespace that carries no meaning, preserving string literals.

    ``"rank( ts_delta(close,  5) ) "`` and ``"rank(ts_delta(close,5))"`` are the
    same alpha and normalize to the same string. Whitespace between two word
    characters is kept as a single space so ``ts_delta close`` does not become
    ``ts_deltaclose``.

    Case is preserved on purpose: BRAIN identifiers are case sensitive, and
    lower-casing would silently change the expression's meaning.
    """
    if not expression:
        return ""

    out: list[str] = []
    quote: str | None = None
    i = 0
    length = len(expression)

    while i < length:
        char = expression[i]

        if quote is not None:
            out.append(char)
            if char == "\\" and i + 1 < length:
                out.append(expression[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue

        if char in _QUOTE_CHARS:
            quote = char
            out.append(char)
            i += 1
            continue

        if char.isspace():
            while i < length and expression[i].isspace():
                i += 1
            if out and i < length and _is_word_char(out[-1]) and _is_word_char(expression[i]):
                out.append(" ")
            continue

        out.append(char)
        i += 1

    return "".join(out).strip()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def expression_hash(expression: str) -> str:
    """Stable id for an expression alone, independent of backtest settings."""
    return _sha256(normalize_expression(expression))


def scope_hash(expression: str, settings: dict[str, Any] | None = None) -> str:
    """Stable id for an expression plus its information-set settings.

    Sits deliberately between :func:`expression_hash` and :func:`dedup_key`:

    - Expression alone is **too strict**. The same signal over TOP1000 rather
      than TOP3000, or at delay 0 rather than delay 1, predicts a different
      instrument set with different information timing. Those are different
      alphas, and blocking them would make a universe sweep impossible.
    - Every setting is **too loose**. One expression run under two
      ``truncation`` values produced effectively identical alphas (both fitness
      2.07) and wasted quota.

    So the scope is ``region`` / ``universe`` / ``delay`` — what is predicted,
    over which instruments, with how much latency. ``decay``, ``truncation``,
    ``neutralization`` and the handling flags are portfolio construction:
    varying only those does not make a new alpha.
    """
    normalized = normalize_settings(settings)
    scope = _HASH_SEPARATOR.join(str(normalized.get(key, "")) for key in SCOPE_SETTINGS)
    return _sha256(normalize_expression(expression) + _HASH_SEPARATOR + scope)


def dedup_key(expression: str, settings: dict[str, Any] | None = None) -> str:
    """Unique key for ``expression + settings``.

    Two runs are the same alpha only when both match: the same expression under
    a different region/universe/delay is a different backtest and must not be
    skipped by the resume logic.
    """
    normalized = normalize_expression(expression)
    canonical = canonical_settings_json(settings)
    return _sha256(normalized + _HASH_SEPARATOR + canonical)


# --------------------------------------------------------------------------- #
# Unified three-layer research identity
# --------------------------------------------------------------------------- #
# Both :class:`~worldquant.storage.ResultStore` (execution dedup) and
# :class:`~worldquant.registry.store.FactorRegistry` (research memory) MUST use
# these functions — never re-derive an identity hash inline, so the two stores
# can never disagree about what counts as "the same alpha".
#
#   expression_identity  — mathematical expression alone
#   signal_identity      — expression + information set (region/universe/delay)
#   experiment_identity  — expression + every normalized simulation setting
def expression_identity(expression: str) -> str:
    """Identity of the mathematical expression, independent of any settings."""
    return expression_hash(expression)


def signal_identity(expression: str, settings: dict[str, Any] | None = None) -> str:
    """Identity of a *research signal*.

    Same expression over a different universe (TOP3000 vs TOP1000) or at a
    different delay predicts a different instrument set with different
    information timing — a different signal. Portfolio-construction settings
    (decay/truncation/neutralization/...) do NOT change the signal.
    """
    return scope_hash(expression, settings)


def experiment_identity(expression: str, settings: dict[str, Any] | None = None) -> str:
    """Identity of a complete simulation experiment (expression + all settings)."""
    return dedup_key(expression, settings)


def auto_alpha_id(expression: str, settings: dict[str, Any] | None = None) -> str:
    """Generate a short, stable id for an alpha that has no explicit name."""
    return "alpha_" + dedup_key(expression, settings)[:10]
