"""Chlorine consumption accumulator with journalled cycle settlement.

A cycle closes by accumulated amount: when the total reaches the cycle limit
the cycle settles and the next one opens. A dose that straddles the boundary
is split at the boundary, so the closing cycle settles at exactly its limit
and the remainder opens the next cycle - the accumulator never goes negative
and never invents a segment.

Settlement is a journalled transition with a strict order: the journal is
written first, the settlement is recorded and audited, the next cycle opens,
and only then is the journal cleared. New accumulation is never accepted
while a settlement is pending: every write path recovers first, completing an
interrupted settlement from its journal. Recovery and repeated settlement
are idempotent because every step checks the cycle it applies to before
writing.
"""

from __future__ import annotations

import json
import threading

from waterplant.audit import Auditor
from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .ledger import (
    ACCUM_KEY,
    DEFAULT_CYCLE_LIMIT,
    PHASE_OPEN,
    PHASE_SETTLING,
    QUOTA_KEYS,
    TRIGGER_LIMIT,
    TRIGGER_MANUAL,
    TRIGGER_RECOVERY,
    CycleStatus,
    DoseSegment,
    Settlement,
    append_dose,
    append_settlement,
    boundary_timestamp,
    clear_journal,
    cycle_dose_total,
    dose_total,
    load_cycle,
    load_doses,
    load_journal,
    load_settlements,
    save_cycle,
    save_journal,
    settlement_for,
)
from .report import AccumState, validate_limit

EPOCH_CAP = DEFAULT_CYCLE_LIMIT

AUDIT_SETTLEMENT = "quota:settlement"
AUDIT_CYCLE_OPEN = "quota:cycle-open"

# The cached value is rounded to four decimals on every write, so the dose
# record and the cache can drift apart by a fraction of a rounding step per
# dose. Anything below this per-dose tolerance is representation noise.
ROUNDING_STEP = 1e-4


class Accumulator:
    """Accumulates doses into cycles that settle at the cycle limit."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._auditor = Auditor(store)

    def add(self, amount: float) -> float:
        """Accumulate a dose, settling cycles as the limit is reached.

        Returns the live value of the cycle that ends up holding the tail of
        the dose.
        """

        with self._lock:
            self._recover_locked()
            pending = amount
            while True:
                status = load_cycle(self._store)
                space = status.limit - self._value_locked()
                if space < 0:
                    space = 0.0
                if pending < space:
                    if pending > 0:
                        self._append_segment(status.cycle_id, pending)
                    return self._value_locked()
                if space > 0:
                    # Head segment fills this cycle to exactly its limit.
                    self._append_segment(status.cycle_id, space)
                pending -= space
                self._settle_locked(status, TRIGGER_LIMIT)

    def settle(self, cycle_id: int | None = None) -> Settlement | None:
        """Settle a cycle on demand and open the next one.

        Settling the current cycle closes it at whatever it accumulated.
        Settling an already settled cycle replays its record, and settling an
        empty cycle is a no-op, so repeated calls never create phantom
        settlements.
        """

        with self._lock:
            self._recover_locked()
            status = load_cycle(self._store)
            target = status.cycle_id if cycle_id is None else cycle_id
            if target > status.cycle_id:
                raise ValueError(f"cycle {target} has not started")
            if target < status.cycle_id:
                return settlement_for(self._store, target)
            if cycle_dose_total(self._store, status.cycle_id) <= 0:
                return None
            return self._settle_locked(status, TRIGGER_MANUAL)

    def recover(self) -> Settlement | None:
        """Complete an interrupted settlement, repairing the cached value.

        Returns the settlement that was completed, or ``None`` when no
        settlement was pending. Safe to call repeatedly.
        """

        with self._lock:
            return self._recover_locked()

    def value(self) -> float:
        with self._lock:
            return self._value_locked()

    def cycle_id(self) -> int:
        with self._lock:
            return load_cycle(self._store).cycle_id

    def limit(self) -> float:
        with self._lock:
            return load_cycle(self._store).limit

    def set_limit(self, limit: float) -> float:
        """Apply a new cycle limit from the current cycle onward."""

        validate_limit(limit)
        with self._lock:
            self._recover_locked()
            status = load_cycle(self._store)
            save_cycle(self._store, CycleStatus(status.cycle_id, status.phase, limit))
            return limit

    def remaining(self, limit: float) -> float:
        remaining = limit - self.value()
        return remaining if remaining > 0 else 0.0

    def settlements(self) -> list[Settlement]:
        with self._lock:
            return load_settlements(self._store)

    def doses(self) -> list[DoseSegment]:
        with self._lock:
            return load_doses(self._store)

    def verify(self) -> dict[str, object]:
        """Reconcile the accumulator, the settlements and the dose record.

        Consistent means: settled cycles are exactly the cycles before the
        current one with no gaps or duplicates, every settled total matches
        the doses attributed to its cycle, and the cached value matches the
        doses of the open cycle. No negative or invented amounts anywhere.
        """

        with self._lock:
            status = load_cycle(self._store)
            doses = load_doses(self._store)
            settlements = load_settlements(self._store)
            by_cycle: dict[int, float] = {}
            for segment in doses:
                by_cycle[segment.cycle_id] = by_cycle.get(segment.cycle_id, 0.0) + segment.amount
            settled_ids = sorted(item.cycle_id for item in settlements)
            contiguous = settled_ids == list(range(1, status.cycle_id))
            settled_match = all(
                abs(item.settled_total - by_cycle.get(item.cycle_id, 0.0)) <= ROUNDING_STEP
                for item in settlements
            )
            total_dosed = sum(by_cycle.values())
            settled_total = sum(item.settled_total for item in settlements)
            current = self._value_locked()
            tolerance = ROUNDING_STEP * max(1, len(doses))
            value_match = abs(current - by_cycle.get(status.cycle_id, 0.0)) <= tolerance
            non_negative = (
                current >= 0
                and all(segment.amount >= 0 for segment in doses)
                and all(item.settled_total >= 0 for item in settlements)
            )
            consistent = (
                contiguous
                and settled_match
                and value_match
                and non_negative
                and abs(settled_total + current - total_dosed) <= tolerance
            )
            return {
                "cycle": status.cycle_id,
                "phase": status.phase,
                "total_dosed": total_dosed,
                "settled_total": settled_total,
                "current_value": current,
                "current_cycle_dosed": by_cycle.get(status.cycle_id, 0.0),
                "settled_cycles": len(settlements),
                "dose_segments": len(doses),
                "consistent": consistent,
            }

    def reset(self) -> None:
        """Factory reset: drop every quota key and start a fresh first cycle."""

        with self._lock:
            for key in QUOTA_KEYS:
                self._store.delete(key)

    def state(self) -> AccumState:
        with self._lock:
            status = load_cycle(self._store)
            verification = self.verify()
            return AccumState(
                value=self._value_locked(),
                cycle=status.cycle_id,
                phase=status.phase,
                limit=status.limit,
                remaining=self.remaining(status.limit),
                settled_cycles=int(verification["settled_cycles"]),
                total_dosed=float(verification["total_dosed"]),
                consistent=bool(verification["consistent"]),
            )

    def describe(self) -> str:
        status = load_cycle(self._store)
        return (
            f"quota accumulator value={self.value():.4f} cycle={status.cycle_id} "
            f"phase={status.phase} remaining={self.remaining(status.limit):.4f}"
        )

    def _value_locked(self) -> float:
        value, _ = load_float(self._store, ACCUM_KEY)
        return value

    def _append_segment(self, cycle_id: int, amount: float) -> None:
        """Record one dose segment and update the cached cycle value."""

        segment = DoseSegment(
            cycle_id=cycle_id, amount=round(amount, 4), at=boundary_timestamp()
        )
        append_dose(self._store, segment)
        save_float(self._store, ACCUM_KEY, self._value_locked() + segment.amount)

    def _settle_locked(self, status: CycleStatus, trigger: str) -> Settlement:
        """Journal the settlement of the current cycle, then complete it."""

        settlement = Settlement(
            cycle_id=status.cycle_id,
            settled_total=cycle_dose_total(self._store, status.cycle_id),
            limit=status.limit,
            trigger=trigger,
            closed_at=boundary_timestamp(),
        )
        save_journal(self._store, settlement)
        save_cycle(
            self._store, CycleStatus(status.cycle_id, PHASE_SETTLING, status.limit)
        )
        self._complete_settlement_locked(settlement)
        return settlement

    def _complete_settlement_locked(self, settlement: Settlement) -> None:
        """Run every settlement step; each is a no-op when already applied."""

        if settlement_for(self._store, settlement.cycle_id) is None:
            append_settlement(self._store, settlement)
        self._audit_once(AUDIT_SETTLEMENT, settlement.cycle_id, settlement.as_dict())
        next_cycle = settlement.cycle_id + 1
        save_cycle(
            self._store, CycleStatus(next_cycle, PHASE_OPEN, settlement.limit)
        )
        save_float(self._store, ACCUM_KEY, cycle_dose_total(self._store, next_cycle))
        self._audit_once(
            AUDIT_CYCLE_OPEN,
            next_cycle,
            {
                "cycle": next_cycle,
                "opened_at": settlement.closed_at,
                "limit": settlement.limit,
                "previous": settlement.cycle_id,
            },
        )
        clear_journal(self._store)

    def _recover_locked(self) -> Settlement | None:
        journal = load_journal(self._store)
        if journal is not None:
            self._complete_settlement_locked(journal)
            recovered: Settlement | None = journal
        else:
            recovered = None
            status = load_cycle(self._store)
            total = cycle_dose_total(self._store, status.cycle_id)
            if total >= status.limit and total > 0:
                # The cycle filled up but the settlement was interrupted
                # before its journal was even written; rebuild it from the
                # dose record and settle now.
                recovered = self._settle_locked(status, TRIGGER_RECOVERY)
        status = load_cycle(self._store)
        total = cycle_dose_total(self._store, status.cycle_id)
        if abs(self._value_locked() - total) > ROUNDING_STEP:
            save_float(self._store, ACCUM_KEY, total)
        return recovered

    def _audit_once(self, kind: str, cycle_id: int, payload: dict[str, object]) -> None:
        """Append an audit entry unless this cycle already has one."""

        for entry in self._auditor.filter(kind):
            try:
                detail = json.loads(entry.detail)
            except ValueError:
                continue
            if isinstance(detail, dict) and detail.get("cycle") == cycle_id:
                return
        self._auditor.record(kind, json.dumps(payload, ensure_ascii=False))
