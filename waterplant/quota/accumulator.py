"""Chlorine consumption accumulator with durable, audited cycles."""

from __future__ import annotations

from waterplant.dosing import DosingLedger, QUANTITY, CyclePolicy
from waterplant.store.store import Store

from .report import AccumState

ACCUM_KEY = "quota:chlorine-accumulator"
CHEMICAL = "chlorine"
EPOCH_CAP = 100.0


class Accumulator:
    """Tracks the current chlorine cycle without wrapping or losing usage."""

    def __init__(self, store: Store) -> None:
        self._ledger = DosingLedger(store)

    @property
    def ledger(self) -> DosingLedger:
        return self._ledger

    def add(self, amount: float) -> float:
        """Record chlorine consumption and return usage in the active cycle."""

        result = self._ledger.record_dose(CHEMICAL, amount)
        return float(result.cycle.get("usage", 0.0))

    def value(self) -> float:
        cycle = self._ledger.current_cycle(CHEMICAL)
        if cycle is None:
            return 0.0
        return float(cycle.get("usage", 0.0))

    def remaining(self, limit: float) -> float:
        remaining = limit - self.value()
        return remaining if remaining > 0 else 0.0

    def reset(self) -> None:
        """Settle the current chlorine cycle and start the next one at zero."""

        self.configure_cap(EPOCH_CAP)
        cycle = self._ledger.current_cycle(CHEMICAL)
        if cycle is not None and float(cycle.get("usage", 0.0)) > 0:
            self._ledger.settle(CHEMICAL, force=True)

    def settle(self) -> object | None:
        return self._ledger.settle(CHEMICAL, force=True)

    def pending(self) -> dict[str, object] | None:
        return self._ledger.state().pending

    def recover(self) -> object | None:
        return self._ledger.recover(CHEMICAL)

    def configure_cap(self, cap: float) -> None:
        if cap <= 0:
            raise ValueError("cap must be positive")
        self._ledger.set_policy(CyclePolicy(CHEMICAL, QUANTITY, cap=cap))

    def state(self) -> AccumState:
        return AccumState(
            value=self.value(),
            cycle=self._ledger.current_cycle(CHEMICAL),
            pending=self._ledger.state().pending,
        )

    def describe(self) -> str:
        return f"quota accumulator value={self.value():.4f}"
