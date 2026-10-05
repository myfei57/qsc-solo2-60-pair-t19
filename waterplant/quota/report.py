"""Quota projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccumState:
    """Current accumulator value within its cycle."""

    value: float
    cycle: int
    phase: str
    limit: float
    remaining: float
    settled_cycles: int
    total_dosed: float
    consistent: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "cycle": self.cycle,
            "phase": self.phase,
            "limit": self.limit,
            "remaining": self.remaining,
            "settled_cycles": self.settled_cycles,
            "total_dosed": self.total_dosed,
            "consistent": self.consistent,
        }


def validate_amount(amount: float) -> None:
    """Reject amounts outside the supported range."""

    if amount < 0:
        raise ValueError("amount must be non-negative")
    if amount > 1_000_000:
        raise ValueError("amount exceeds the supported range")


def validate_limit(limit: float) -> None:
    """Reject cycle limits outside the supported range."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    if limit > 1_000_000:
        raise ValueError("limit exceeds the supported range")
