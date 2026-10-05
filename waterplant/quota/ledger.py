"""Cycle scoped persistence for the quota accumulator.

The quota period closes by accumulated amount, not by wall clock: chemical
allowances are managed as quantities, so a cycle settles as soon as the
accumulated dose reaches the cycle limit. A dose that straddles the boundary
is split at the boundary - the head segment settles the closing cycle and the
tail segment opens the next one. Nothing is rounded away, dropped or
invented, so the settled cycles and the live accumulator always reconcile
with the dose record.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

ACCUM_KEY = "quota:chlorine-accumulator"
CYCLE_KEY = "quota:cycle"
JOURNAL_KEY = "quota:settlement-journal"
SETTLEMENTS_KEY = "quota:settlements"
DOSES_KEY = "quota:doses"

QUOTA_KEYS = (ACCUM_KEY, CYCLE_KEY, JOURNAL_KEY, SETTLEMENTS_KEY, DOSES_KEY)

DEFAULT_CYCLE_LIMIT = 100.0

PHASE_OPEN = "open"
PHASE_SETTLING = "settling"

TRIGGER_LIMIT = "limit"
TRIGGER_MANUAL = "manual"
TRIGGER_RECOVERY = "recovery"


def boundary_timestamp() -> str:
    """The single instant a cycle closes and the next one opens."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class CycleStatus:
    """The cycle currently accumulating and the limit it settles at."""

    cycle_id: int
    phase: str
    limit: float

    def as_dict(self) -> dict[str, object]:
        return {"cycle": self.cycle_id, "phase": self.phase, "limit": self.limit}


@dataclass(frozen=True)
class Settlement:
    """The settled total for one closed cycle.

    Also serves as the settlement journal: the record is written before the
    cycle closes and is enough to deterministically replay the whole
    settlement, so recovery after a partial failure repeats the same steps
    and produces the same result.
    """

    cycle_id: int
    settled_total: float
    limit: float
    trigger: str
    closed_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "cycle": self.cycle_id,
            "settled_total": self.settled_total,
            "limit": self.limit,
            "trigger": self.trigger,
            "closed_at": self.closed_at,
        }


@dataclass(frozen=True)
class DoseSegment:
    """The part of one dose attributed to a single cycle."""

    cycle_id: int
    amount: float
    at: str

    def as_dict(self) -> dict[str, object]:
        return {"cycle": self.cycle_id, "amount": self.amount, "at": self.at}


def load_cycle(store: Store) -> CycleStatus:
    """Read the current cycle, falling back to a fresh first cycle."""

    raw, present = store.get(CYCLE_KEY)
    if present:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            try:
                cycle_id = int(payload.get("cycle", 1))
                limit = float(payload.get("limit", DEFAULT_CYCLE_LIMIT))
            except (TypeError, ValueError):
                cycle_id, limit = 0, 0.0
            phase = str(payload.get("phase", PHASE_OPEN))
            if cycle_id >= 1 and limit > 0 and phase in (PHASE_OPEN, PHASE_SETTLING):
                return CycleStatus(cycle_id=cycle_id, phase=phase, limit=limit)
    return CycleStatus(cycle_id=1, phase=PHASE_OPEN, limit=DEFAULT_CYCLE_LIMIT)


def save_cycle(store: Store, status: CycleStatus) -> None:
    store.put(CYCLE_KEY, json.dumps(status.as_dict(), ensure_ascii=False))


def load_journal(store: Store) -> Settlement | None:
    """Read the pending settlement, when a settlement was interrupted."""

    raw, present = store.get(JOURNAL_KEY)
    if not present:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return Settlement(
            cycle_id=int(payload["cycle"]),
            settled_total=float(payload["settled_total"]),
            limit=float(payload["limit"]),
            trigger=str(payload["trigger"]),
            closed_at=str(payload["closed_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def save_journal(store: Store, settlement: Settlement) -> None:
    store.put(JOURNAL_KEY, json.dumps(settlement.as_dict(), ensure_ascii=False))


def clear_journal(store: Store) -> None:
    store.delete(JOURNAL_KEY)


def load_settlements(store: Store) -> list[Settlement]:
    """Every settled cycle, in settlement order."""

    settlements: list[Settlement] = []
    for item in load_commands(store, SETTLEMENTS_KEY):
        try:
            payload = json.loads(item)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            settlements.append(
                Settlement(
                    cycle_id=int(payload["cycle"]),
                    settled_total=float(payload["settled_total"]),
                    limit=float(payload["limit"]),
                    trigger=str(payload["trigger"]),
                    closed_at=str(payload["closed_at"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return settlements


def append_settlement(store: Store, settlement: Settlement) -> None:
    append_command(store, SETTLEMENTS_KEY, json.dumps(settlement.as_dict(), ensure_ascii=False))


def settlement_for(store: Store, cycle_id: int) -> Settlement | None:
    """The settlement already recorded for a cycle, when it exists."""

    for settlement in load_settlements(store):
        if settlement.cycle_id == cycle_id:
            return settlement
    return None


def load_doses(store: Store) -> list[DoseSegment]:
    """Every recorded dose segment, in recording order."""

    segments: list[DoseSegment] = []
    for item in load_commands(store, DOSES_KEY):
        try:
            payload = json.loads(item)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            segments.append(
                DoseSegment(
                    cycle_id=int(payload["cycle"]),
                    amount=float(payload["amount"]),
                    at=str(payload.get("at", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return segments


def append_dose(store: Store, segment: DoseSegment) -> None:
    append_command(store, DOSES_KEY, json.dumps(segment.as_dict(), ensure_ascii=False))


def cycle_dose_total(store: Store, cycle_id: int) -> float:
    """Sum the dose segments attributed to one cycle."""

    return sum(segment.amount for segment in load_doses(store) if segment.cycle_id == cycle_id)


def dose_total(store: Store) -> float:
    """Sum every dose segment ever recorded."""

    return sum(segment.amount for segment in load_doses(store))
