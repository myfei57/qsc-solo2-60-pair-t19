"""Append only audit entries backed by the durable dosing ledger."""

from __future__ import annotations

import time
from dataclasses import dataclass

from waterplant.dosing import DosingLedger
from waterplant.store.store import Store

from .report import AuditState

AUDIT_KEY = "audit:entries"


@dataclass(frozen=True)
class Entry:
    """One recorded dosing decision."""

    id: str
    time: str
    kind: str
    detail: str
    cycle_id: str = ""

    def as_dict(self) -> dict[str, str]:
        payload = {"id": self.id, "time": self.time, "kind": self.kind, "detail": self.detail}
        if self.cycle_id:
            payload["cycle_id"] = self.cycle_id
        return payload


class Auditor:
    """Records and queries dosing entries through the transactional ledger."""

    def __init__(self, store: Store, clock=None) -> None:
        self._ledger = DosingLedger(store, clock=clock)

    @property
    def ledger(self) -> DosingLedger:
        return self._ledger

    def record(self, kind: str, detail: str) -> Entry:
        amount = float(detail)
        result = self._ledger.record_dose(kind, amount, detail)
        return self._entry(result.entry)

    def entries(self) -> list[Entry]:
        return [self._entry(entry) for entry in self._ledger.doses()]

    def last(self) -> Entry | None:
        entries = self.entries()
        if not entries:
            return None
        return entries[-1]

    def count(self) -> int:
        return len(self.entries())

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts

    def filter(self, kind: str) -> list[Entry]:
        return [entry for entry in self.entries() if entry.kind == kind]

    def kinds(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries():
            if entry.kind not in seen:
                seen.append(entry.kind)
        return seen

    def totals(self) -> dict[str, float]:
        """Sum the numeric dose recorded for each chemical."""

        totals: dict[str, float] = {}
        for entry in self._ledger.doses():
            totals[entry.chemical] = round(totals.get(entry.chemical, 0.0) + entry.amount, 4)
        return totals

    def settlements(self, kind: str | None = None) -> list[object]:
        return self._ledger.settlements(kind)

    def state(self) -> AuditState:
        entries = self.entries()
        return AuditState(entries=[entry.as_dict() for entry in entries], count=len(entries))

    def describe(self) -> str:
        return f"audit entries={len(self.entries())}"

    def _entry(self, dose) -> Entry:
        return Entry(
            id=dose.id,
            time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(dose.recorded_at)),
            kind=dose.chemical,
            detail=dose.detail,
            cycle_id=dose.cycle_id,
        )
