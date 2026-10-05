"""Quota projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccumState:
    """Current accumulator value and its audited cycle boundary."""

    value: float
    cycle: dict[str, object] | None
    pending: dict[str, object] | None

    def as_dict(self) -> dict[str, object]:
        return {"value": self.value, "cycle": self.cycle, "pending": self.pending}


def validate_amount(amount: float) -> None:
    """Reject amounts outside the supported range."""

    if amount < 0:
        raise ValueError("amount must be non-negative")
    if amount > 1_000_000:
        raise ValueError("amount exceeds the supported range")
