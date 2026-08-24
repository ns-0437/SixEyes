"""Typed quantities.

Money is integer micro-USD. Floating point dollars accumulate error across millions of
spans and produce report totals that do not reconcile with a customer's provider invoice,
which is the fastest way to lose a technical buyer. Rounding happens once, at render time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

MICROS_PER_USD = 1_000_000


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An exact monetary amount held as integer micro-USD."""

    micros: int

    def __post_init__(self) -> None:
        if not isinstance(self.micros, int) or isinstance(self.micros, bool):
            raise TypeError(f"Money.micros must be int, got {type(self.micros).__name__}")

    @classmethod
    def zero(cls) -> Self:
        return cls(0)

    @classmethod
    def from_usd(cls, usd: float | int | str) -> Self:
        """Construct from dollars. Accepts str for exactness when the source is textual."""
        from decimal import Decimal

        return cls(int((Decimal(str(usd)) * MICROS_PER_USD).to_integral_value()))

    def __add__(self, other: Money) -> Money:
        return Money(self.micros + other.micros)

    def __sub__(self, other: Money) -> Money:
        return Money(self.micros - other.micros)

    def __mul__(self, factor: int) -> Money:
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise TypeError("Money may only be multiplied by int; use scale() for ratios")
        return Money(self.micros * factor)

    def scale(self, numerator: int, denominator: int) -> Money:
        """Exact proportional scaling. Used for attributing spend across causes."""
        if denominator == 0:
            raise ZeroDivisionError("Money.scale denominator must be non-zero")
        return Money(self.micros * numerator // denominator)

    @property
    def usd(self) -> float:
        """Lossy. Render only — never feed this back into arithmetic."""
        return self.micros / MICROS_PER_USD

    def __str__(self) -> str:
        return f"${self.usd:,.2f}"

    def __repr__(self) -> str:
        return f"Money.from_usd({self.usd!r})"


def sum_money(amounts: object) -> Money:
    total = 0
    for amount in amounts:  # type: ignore[attr-defined]
        total += amount.micros
    return Money(total)
