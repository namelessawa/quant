"""Alpha candidate generation.

Generation is deliberately **decoupled from submission**: a generator only
produces expression strings (or writes them to a file). Nothing here talks to
BRAIN, so thousands of candidates can be produced, reviewed and trimmed before
a batch run ever spends simulation quota.

:class:`AlphaGenerator` is the extension point for a future LLM-backed
generator — subclass it and implement :meth:`generate`.
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable, Sequence

from .loader import specs_from_expressions
from .models import AlphaSpec

DEFAULT_OPERATORS: tuple[str, ...] = ("ts_mean", "ts_std_dev", "ts_delta")
DEFAULT_FIELDS: tuple[str, ...] = ("close", "volume", "returns")
DEFAULT_WINDOWS: tuple[int, ...] = (5, 10, 20, 60)
DEFAULT_WRAPPER = "rank"
DEFAULT_TEMPLATE = "{wrapper}({operator}({field}, {window}))"


class AlphaGenerator(ABC):
    """Produces candidate alpha expressions."""

    @abstractmethod
    def generate(self) -> list[str]:
        """Return candidate expressions. Implementations must not submit them."""

    def to_specs(self, settings: dict[str, Any] | None = None) -> list[AlphaSpec]:
        """Wrap generated expressions into runnable specs."""
        return specs_from_expressions(self.generate(), settings=settings)

    def write(self, path: str | Path, *, settings: dict[str, Any] | None = None) -> Path:
        """Write candidates to ``.csv`` (name,expression) or ``.txt``.

        Writing to a file is the hand-off point: inspect and prune the output
        before feeding it to ``scripts/run_backtest.py --input``.
        """
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        expressions = self.generate()

        if out_path.suffix.lower() == ".csv":
            specs = specs_from_expressions(expressions, settings=settings)
            with out_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["name", "expression"])
                writer.writeheader()
                for spec in specs:
                    writer.writerow({"name": spec.name, "expression": spec.expression})
        else:
            out_path.write_text("\n".join(expressions) + "\n", encoding="utf-8")
        return out_path


class CombinatorialGenerator(AlphaGenerator):
    """Cross-product generator over operators, fields and windows.

    >>> gen = CombinatorialGenerator(operators=["ts_delta"], fields=["close"], windows=[5, 10])
    >>> gen.generate()
    ['rank(ts_delta(close, 5))', 'rank(ts_delta(close, 10))']
    """

    def __init__(
        self,
        operators: Sequence[str] = DEFAULT_OPERATORS,
        fields: Sequence[str] = DEFAULT_FIELDS,
        windows: Sequence[int] = DEFAULT_WINDOWS,
        *,
        wrapper: str = DEFAULT_WRAPPER,
        template: str = DEFAULT_TEMPLATE,
        max_count: int | None = None,
    ) -> None:
        self.operators = tuple(operators)
        self.fields = tuple(fields)
        self.windows = tuple(windows)
        self.wrapper = wrapper
        self.template = template
        self.max_count = max_count

    def generate(self) -> list[str]:
        seen: set[str] = set()
        expressions: list[str] = []
        for operator in self.operators:
            for field in self.fields:
                for window in self.windows:
                    expression = self.template.format(
                        wrapper=self.wrapper,
                        operator=operator,
                        field=field,
                        window=window,
                    )
                    if expression in seen:
                        continue
                    seen.add(expression)
                    expressions.append(expression)
                    if self.max_count is not None and len(expressions) >= self.max_count:
                        return expressions
        return expressions


class StaticGenerator(AlphaGenerator):
    """Wraps an explicit list of expressions, for uniform handling in scripts."""

    def __init__(self, expressions: Iterable[str]) -> None:
        self._expressions = [str(item).strip() for item in expressions if str(item).strip()]

    def generate(self) -> list[str]:
        return list(self._expressions)
