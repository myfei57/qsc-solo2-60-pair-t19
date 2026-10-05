"""Durable per-chemical dosing ledger with audited cycle settlement."""

from __future__ import annotations

import calendar
import json
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from waterplant.store.store import Store

LEDGER_KEY = "dosing:ledger"
LEGACY_AUDIT_KEY = "audit:entries"
LEGACY_ACCUMULATOR_KEY = "quota:chlorine-accumulator"

QUANTITY = "quantity"
TIME = "time"
OPEN = "open"
DOSE_START = "dose_start"

DEFAULT_CHLORINE_CAP = 100.0


class SettlementInProgress(ValueError):
    """Raised while an interrupted settlement must be recovered first."""


@dataclass(frozen=True)
class CyclePolicy:
    """Rule that decides when a chemical cycle is due."""

    chemical: str
    mode: str = OPEN
    cap: float = 0.0
    duration_seconds: int = 0
    boundary_rule: str = DOSE_START

    def as_dict(self) -> dict[str, object]:
        return {
            "chemical": self.chemical,
            "mode": self.mode,
            "cap": self.cap,
            "duration_seconds": self.duration_seconds,
            "boundary_rule": self.boundary_rule,
        }


@dataclass(frozen=True)
class DoseEntry:
    """One indivisible chemical dosing record."""

    id: str
    seq: int
    chemical: str
    amount: float
    detail: str
    recorded_at: int
    business_at: int
    cycle_id: str
    boundary_rule: str = DOSE_START

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "seq": self.seq,
            "chemical": self.chemical,
            "amount": self.amount,
            "detail": self.detail,
            "recorded_at": self.recorded_at,
            "business_at": self.business_at,
            "cycle_id": self.cycle_id,
            "boundary_rule": self.boundary_rule,
        }


@dataclass(frozen=True)
class Settlement:
    """Auditable usage result for a completed cycle."""

    id: str
    chemical: str
    cycle_id: str
    usage: float
    entry_count: int
    started_at: int
    finished_at: int
    boundary_time: int
    triggered_by: str
    status: str
    next_cycle_id: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "chemical": self.chemical,
            "cycle_id": self.cycle_id,
            "usage": self.usage,
            "entry_count": self.entry_count,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "boundary_time": self.boundary_time,
            "triggered_by": self.triggered_by,
            "status": self.status,
            "next_cycle_id": self.next_cycle_id,
        }


@dataclass(frozen=True)
class DoseResult:
    """Result of accepting a dose in the active cycle."""

    entry: DoseEntry
    cycle: dict[str, object]
    settlement: Settlement | None

    def as_dict(self) -> dict[str, object]:
        return {
            "entry": self.entry.as_dict(),
            "cycle": self.cycle,
            "settlement": None if self.settlement is None else self.settlement.as_dict(),
        }


@dataclass(frozen=True)
class LedgerState:
    """Read model used by the control console."""

    policies: dict[str, CyclePolicy]
    cycles: dict[str, dict[str, object]]
    doses: list[DoseEntry]
    settlements: list[Settlement]
    pending: dict[str, object] | None

    def as_dict(self) -> dict[str, object]:
        return {
            "policies": {key: value.as_dict() for key, value in self.policies.items()},
            "cycles": self.cycles,
            "doses": [entry.as_dict() for entry in self.doses],
            "settlements": [settlement.as_dict() for settlement in self.settlements],
            "pending": self.pending,
        }


def _amount_text(amount: float) -> str:
    return f"{amount:.4f}"


def _parse_amount(detail: str) -> float:
    try:
        amount = float(detail)
    except (TypeError, ValueError) as exc:
        raise ValueError("dosing audit detail must be numeric") from exc
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if amount > 1_000_000:
        raise ValueError("amount exceeds the supported range")
    return amount


class DosingLedger:
    """Transactionally maintains doses, cycles and settlement results."""

    def __init__(
        self,
        store: Store,
        clock: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or time.time
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def record_dose(self, chemical: str, amount: float, detail: str | None = None) -> DoseResult:
        """Append one dose, settling a due cycle before a new cycle accepts it."""

        if amount < 0:
            raise ValueError("amount must be non-negative")
        if amount > 1_000_000:
            raise ValueError("amount exceeds the supported range")
        text = _amount_text(amount) if detail is None else str(detail)
        _parse_amount(text)

        time_settlement: Settlement | None = None
        while True:
            outcome = self._store.mutate(
                lambda data: self._append_or_begin_locked(data, chemical, amount, text)
            )
            if outcome.get("began"):
                settlement = self.recover(chemical)
                if settlement is None:
                    raise SettlementInProgress("settlement recovery did not complete")
                if outcome.get("trigger") == TIME:
                    time_settlement = settlement
                    continue
                entry = self._entry(outcome["entry"])
                cycle = self.current_cycle(chemical) or {}
                return DoseResult(entry=entry, cycle=cycle, settlement=settlement)
            result = outcome["result"]
            if time_settlement is not None and result.settlement is None:
                return DoseResult(
                    entry=result.entry, cycle=result.cycle, settlement=time_settlement
                )
            return result

    def _append_or_begin_locked(
        self,
        data: dict[str, str],
        chemical: str,
        amount: float,
        text: str,
    ) -> dict[str, object]:
        ledger = self._loaded_locked(data)
        now = self._now()
        self._reject_settling_locked(ledger, chemical)
        cycle = self._active_cycle_locked(ledger, chemical)
        policy = self._policy_locked(ledger, chemical)
        if (
            cycle is not None
            and policy.mode == TIME
            and isinstance(cycle.get("end_at"), int)
            and cycle["end_at"] <= now
        ):
            self._begin_settlement_locked(ledger, chemical, now, int(cycle["end_at"]), "time-due")
            self._save_locked(data, ledger)
            return {"began": True, "trigger": TIME}

        cycle = self._open_cycle_locked(ledger, chemical, now)
        seq = len(ledger["doses"]) + 1
        entry = {
            "id": self._id_factory(),
            "seq": seq,
            "chemical": chemical,
            "amount": amount,
            "detail": text,
            "recorded_at": now,
            "business_at": now,
            "cycle_id": cycle["id"],
            "boundary_rule": DOSE_START,
        }
        ledger["doses"].append(entry)
        cycle["usage"] = round(float(cycle.get("usage", 0.0)) + amount, 4)
        cycle["entry_count"] = int(cycle.get("entry_count", 0)) + 1
        if not cycle.get("first_dose_at"):
            cycle["first_dose_at"] = now
        cycle["last_dose_at"] = now

        result = DoseResult(entry=self._entry(entry), cycle=dict(cycle), settlement=None)
        if self._quantity_due_locked(ledger, chemical, cycle):
            self._begin_settlement_locked(ledger, chemical, now, now, "quantity-due")
            self._save_locked(data, ledger)
            return {"began": True, "trigger": QUANTITY, "entry": entry}
        self._save_locked(data, ledger)
        return {"began": False, "result": result}

    def recover(self, chemical: str, now: int | None = None) -> Settlement | None:
        """Finish a durable pending settlement; repeating it does not change facts."""

        def change(data: dict[str, str]) -> Settlement | None:
            ledger = self._loaded_locked(data)
            pending = ledger["pending"]
            if pending is None or pending.get("chemical") != chemical:
                return None
            current_now = self._now() if now is None else now
            settlement = self._finish_pending_locked(ledger, current_now)
            self._save_locked(data, ledger)
            return settlement

        return self._store.mutate(change)

    def settle(self, chemical: str, force: bool = False, cycle_id: str | None = None) -> Settlement | None:
        """Settle due/recoverable work idempotently, then open the next cycle."""

        existing = self.recover(chemical)
        if existing is not None:
            if cycle_id in (None, existing.cycle_id):
                return existing
            raise ValueError("settlement request does not match the recovering cycle")

        started = self.begin_settlement(chemical, force=force, cycle_id=cycle_id)
        if isinstance(started, Settlement):
            return started
        if started is None:
            return None
        return self.recover(chemical)

    def begin_settlement(
        self, chemical: str, force: bool = False, cycle_id: str | None = None
    ) -> str | Settlement | None:
        """Durably mark a due cycle as settling; safe to repeat."""

        def change(data: dict[str, str]) -> str | Settlement | None:
            ledger = self._loaded_locked(data)
            now = self._now()
            pending = ledger["pending"]
            if pending is not None:
                if pending.get("chemical") != chemical:
                    raise SettlementInProgress("another chemical settlement is in progress")
                return str(pending.get("cycle_id", ""))
            cycle = self._active_cycle_locked(ledger, chemical)
            if cycle is None:
                return None
            if cycle_id and cycle.get("id") != cycle_id:
                previous = self._settlement_for_cycle_locked(ledger, cycle_id)
                if previous is not None and previous.status == "completed":
                    return previous
                raise ValueError("cycle is not active")

            policy = self._policy_locked(ledger, chemical)
            boundary_time: int | None = None
            triggered_by = "manual"
            if policy.mode == TIME and isinstance(cycle.get("end_at"), int) and cycle["end_at"] <= now:
                boundary_time = int(cycle["end_at"])
                triggered_by = "time-due"
            elif policy.mode == QUANTITY and policy.cap > 0 and cycle.get("usage", 0.0) >= policy.cap:
                boundary_time = now
                triggered_by = "quantity-due"
            elif force:
                boundary_time = now
            if (
                force
                and not (
                    policy.mode == TIME
                    and isinstance(cycle.get("end_at"), int)
                    and cycle["end_at"] <= now
                )
                and not (policy.mode == QUANTITY and policy.cap > 0 and cycle.get("usage", 0.0) >= policy.cap)
                and float(cycle.get("usage", 0.0)) == 0.0
            ):
                return None
            if boundary_time is None:
                return None
            self._begin_settlement_locked(ledger, chemical, now, boundary_time, triggered_by)
            self._save_locked(data, ledger)
            return str(cycle["id"])

        return self._store.mutate(change)

    def policy(self, chemical: str) -> CyclePolicy:
        def read(data: dict[str, str]) -> CyclePolicy:
            ledger = self._loaded_locked(data)
            return self._policy_locked(ledger, chemical)

        return self._store.mutate(read)

    def set_policy(self, policy: CyclePolicy) -> CyclePolicy:
        self._validate_policy(policy)

        def change(data: dict[str, str]) -> CyclePolicy:
            ledger = self._loaded_locked(data)
            self._reject_settling_locked(ledger, policy.chemical)
            policies = ledger["policies"]
            policies[policy.chemical] = self._policy_to_dict(policy)
            cycle = self._active_cycle_locked(ledger, policy.chemical)
            if cycle is not None:
                if policy.mode == TIME:
                    cycle["end_at"] = int(cycle["started_at"]) + policy.duration_seconds
                else:
                    cycle["end_at"] = None
            self._save_locked(data, ledger)
            return policy

        return self._store.mutate(change)

    def current_cycle(self, chemical: str) -> dict[str, object] | None:
        def read(data: dict[str, str]) -> dict[str, object] | None:
            ledger = self._loaded_locked(data)
            cycle = self._active_cycle_locked(ledger, chemical)
            return None if cycle is None else dict(cycle)

        return self._store.mutate(read)

    def doses(self, chemical: str | None = None) -> list[DoseEntry]:
        def read(data: dict[str, str]) -> list[DoseEntry]:

            ledger = self._loaded_locked(data)
            entries = [self._entry(item) for item in ledger["doses"]]
            if chemical is not None:
                entries = [entry for entry in entries if entry.chemical == chemical]
            return entries

        return self._store.mutate(read)

    def settlements(self, chemical: str | None = None) -> list[Settlement]:
        def read(data: dict[str, str]) -> list[Settlement]:
            ledger = self._loaded_locked(data)
            values = [self._settlement(item) for item in ledger["settlements"]]
            if chemical is not None:
                values = [item for item in values if item.chemical == chemical]
            return values

        return self._store.mutate(read)

    def state(self) -> LedgerState:
        def read(data: dict[str, str]) -> LedgerState:
            ledger = self._loaded_locked(data)
            return LedgerState(
                policies={
                    chemical: self._policy(item) for chemical, item in ledger["policies"].items()
                },
                cycles={item["id"]: dict(item) for item in ledger["cycles"]},
                doses=[self._entry(item) for item in ledger["doses"]],
                settlements=[self._settlement(item) for item in ledger["settlements"]],
                pending=None if ledger["pending"] is None else dict(ledger["pending"]),
            )

        return self._store.mutate(read)

    def _now(self) -> int:
        return int(self._clock())

    def _loaded_locked(self, data: dict[str, str]) -> dict[str, object]:
        raw = data.get(LEDGER_KEY)
        if raw is None:
            ledger = self._new_ledger_locked()
            self._migrate_locked(data, ledger)
            return ledger
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("dosing ledger document must be an object")
        loaded.setdefault("policies", {})
        loaded.setdefault("cycles", [])
        loaded.setdefault("doses", [])
        loaded.setdefault("settlements", [])
        loaded["pending"] = loaded.get("pending")
        return loaded

    def _new_ledger_locked(self) -> dict[str, object]:
        return {
            "version": 1,
            "policies": {},
            "cycles": [],
            "doses": [],
            "settlements": [],
            "pending": None,
        }

    def _migrate_locked(self, data: dict[str, str], ledger: dict[str, object]) -> None:
        """Move legacy audit and wrapped counter facts into one event order."""

        legacy_doses: list[tuple[str, str, int]] = []
        raw_audit = data.get(LEGACY_AUDIT_KEY)
        if raw_audit:
            try:
                items = json.loads(raw_audit)
            except ValueError:
                items = []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    legacy_doses.append(
                        (
                            str(item.get("kind", "")),
                            str(item.get("detail", "0")),
                            self._timestamp(str(item.get("time", ""))),
                        )
                    )

        raw_balance, present = self._legacy_float(data.get(LEGACY_ACCUMULATOR_KEY))
        if present and raw_balance > 0:
            legacy_doses.insert(0, ("chlorine", _amount_text(raw_balance), self._now()))

        for chemical, detail, timestamp in legacy_doses:
            try:
                amount = _parse_amount(detail)
            except ValueError:
                amount = 0.0
            self._policy_locked(ledger, chemical)
            cycle = self._open_cycle_locked(ledger, chemical, timestamp)
            cycle["usage"] = round(float(cycle.get("usage", 0.0)) + amount, 4)
            cycle["entry_count"] = int(cycle.get("entry_count", 0)) + 1
            cycle["first_dose_at"] = cycle.get("first_dose_at") or timestamp
            cycle["last_dose_at"] = timestamp
            ledger["doses"].append(
                {
                    "id": self._id_factory(),
                    "seq": len(ledger["doses"]) + 1,
                    "chemical": chemical,
                    "amount": amount,
                    "detail": detail,
                    "recorded_at": timestamp,
                    "business_at": timestamp,
                    "cycle_id": cycle["id"],
                    "boundary_rule": DOSE_START,
                }
            )
            policy = self._policy_locked(ledger, chemical)
            if policy.mode == QUANTITY and policy.cap > 0 and cycle["usage"] >= policy.cap:
                self._begin_settlement_locked(ledger, chemical, timestamp, timestamp, "quantity-due")
                self._finish_pending_locked(ledger, timestamp)

        data.pop(LEGACY_AUDIT_KEY, None)
        data.pop(LEGACY_ACCUMULATOR_KEY, None)
        data[LEDGER_KEY] = json.dumps(ledger, ensure_ascii=False, sort_keys=True)

    def _legacy_float(self, raw: str | None) -> tuple[float, bool]:
        if raw is None:
            return 0.0, False
        try:
            return float(raw), True
        except ValueError:
            return 0.0, False

    def _timestamp(self, value: str) -> int:
        if not value:
            return self._now()
        try:
            parsed = time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            return int(calendar.timegm(parsed))
        except ValueError:
            return self._now()

    def _default_policy(self, chemical: str) -> CyclePolicy:
        if chemical == "chlorine":
            return CyclePolicy(chemical, QUANTITY, cap=DEFAULT_CHLORINE_CAP)
        return CyclePolicy(chemical, OPEN)

    def _policy_locked(self, ledger: dict[str, object], chemical: str) -> CyclePolicy:
        policies = ledger["policies"]
        if chemical not in policies:
            policy = self._default_policy(chemical)
            policies[chemical] = self._policy_to_dict(policy)
        return self._policy(policies[chemical])

    def _policy_to_dict(self, policy: CyclePolicy) -> dict[str, object]:
        return {
            "chemical": policy.chemical,
            "mode": policy.mode,
            "cap": round(policy.cap, 4),
            "duration_seconds": policy.duration_seconds,
            "boundary_rule": DOSE_START,
        }

    def _policy(self, payload: dict[str, object]) -> CyclePolicy:
        return CyclePolicy(
            chemical=str(payload.get("chemical", "")),
            mode=str(payload.get("mode", OPEN)),
            cap=float(payload.get("cap", 0.0)),
            duration_seconds=int(payload.get("duration_seconds", 0)),
            boundary_rule=str(payload.get("boundary_rule", DOSE_START)),
        )

    def _validate_policy(self, policy: CyclePolicy) -> None:
        if not policy.chemical:
            raise ValueError("chemical is required")
        if policy.boundary_rule != DOSE_START:
            raise ValueError("cycle boundary rule must be dose_start")
        if policy.mode == QUANTITY:
            if policy.cap <= 0:
                raise ValueError("quantity cap must be positive")
        elif policy.mode == TIME:
            if policy.duration_seconds <= 0:
                raise ValueError("cycle duration must be positive")
        elif policy.mode != OPEN:
            raise ValueError("cycle mode must be quantity, time or open")

    def _open_cycle_locked(
        self, ledger: dict[str, object], chemical: str, now: int
    ) -> dict[str, object]:
        cycle = self._active_cycle_locked(ledger, chemical)
        if cycle is not None:
            return cycle
        policy = self._policy_locked(ledger, chemical)
        number = 1 + len([item for item in ledger["cycles"] if item.get("chemical") == chemical])
        cycle = {
            "id": f"{chemical}-cycle-{number}",
            "chemical": chemical,
            "number": number,
            "status": "open",
            "started_at": now,
            "end_at": now + policy.duration_seconds if policy.mode == TIME else None,
            "closed_at": None,
            "usage": 0.0,
            "entry_count": 0,
            "first_dose_at": None,
            "last_dose_at": None,
            "boundary_rule": DOSE_START,
            "policy": policy.as_dict(),
        }
        ledger["cycles"].append(cycle)
        return cycle

    def _active_cycle_locked(
        self, ledger: dict[str, object], chemical: str
    ) -> dict[str, object] | None:
        for cycle in reversed(ledger["cycles"]):
            if cycle.get("chemical") != chemical:
                continue
            if cycle.get("status") in {"open", "settling"}:
                return cycle
        return None

    def _quantity_due_locked(
        self, ledger: dict[str, object], chemical: str, cycle: dict[str, object]
    ) -> bool:
        policy = self._policy_locked(ledger, chemical)
        return policy.mode == QUANTITY and policy.cap > 0 and float(cycle.get("usage", 0.0)) >= policy.cap

    def _begin_settlement_locked(
        self,
        ledger: dict[str, object],
        chemical: str,
        now: int,
        boundary_time: int,
        triggered_by: str,
    ) -> None:
        if ledger["pending"] is not None:
            pending = ledger["pending"]
            if pending.get("chemical") != chemical:
                raise SettlementInProgress("another chemical settlement is in progress")
            return
        cycle = self._active_cycle_locked(ledger, chemical)
        if cycle is None or cycle.get("status") != "open":
            raise SettlementInProgress("cycle settlement is already in progress")
        settlement_id = self._id_factory()
        cycle["status"] = "settling"
        payload = {
            "id": settlement_id,
            "chemical": chemical,
            "cycle_id": cycle["id"],
            "usage": float(cycle.get("usage", 0.0)),
            "entry_count": int(cycle.get("entry_count", 0)),
            "started_at": now,
            "finished_at": None,
            "boundary_time": boundary_time,
            "triggered_by": triggered_by,
            "status": "pending",
            "next_cycle_id": "",
        }
        ledger["settlements"].append(payload)
        ledger["pending"] = payload

    def _finish_pending_locked(
        self, ledger: dict[str, object], now: int
    ) -> Settlement | None:
        pending = ledger["pending"]
        if pending is None:
            return None
        existing = next(
            item
            for item in reversed(ledger["settlements"])
            if item.get("id") == pending.get("id")
        )
        if existing.get("status") == "completed":
            ledger["pending"] = None
            return self._settlement(existing)

        chemical = str(existing["chemical"])
        boundary_time = int(existing["boundary_time"])
        cycle = self._active_cycle_locked(ledger, chemical)
        if cycle is None or cycle.get("id") != existing["cycle_id"]:
            raise ValueError("settlement cycle no longer matches ledger state")
        cycle["status"] = "settled"
        cycle["closed_at"] = boundary_time
        existing["finished_at"] = now
        existing["status"] = "completed"

        policy = self._policy_locked(ledger, chemical)
        number = 1 + len([item for item in ledger["cycles"] if item.get("chemical") == chemical])
        next_cycle = {
            "id": f"{chemical}-cycle-{number}",
            "chemical": chemical,
            "number": number,
            "status": "open",
            "started_at": boundary_time,
            "end_at": boundary_time + policy.duration_seconds
            if policy.mode == TIME
            else None,
            "closed_at": None,
            "usage": 0.0,
            "entry_count": 0,
            "first_dose_at": None,
            "last_dose_at": None,
            "boundary_rule": DOSE_START,
            "policy": policy.as_dict(),
        }
        ledger["cycles"].append(next_cycle)
        existing["next_cycle_id"] = next_cycle["id"]
        ledger["pending"] = None
        return self._settlement(existing)

    def _settlement_for_cycle_locked(
        self, ledger: dict[str, object], cycle_id: str
    ) -> Settlement | None:
        for item in reversed(ledger["settlements"]):
            if item.get("cycle_id") == cycle_id:
                return self._settlement(item)
        return None

    def _reject_settling_locked(self, ledger: dict[str, object], chemical: str) -> None:
        pending = ledger["pending"]
        if pending is not None:
            if pending.get("chemical") == chemical:
                raise SettlementInProgress("settlement must be recovered before accepting doses")
            raise SettlementInProgress("another chemical settlement is in progress")

    def _entry(self, payload: dict[str, object]) -> DoseEntry:
        return DoseEntry(
            id=str(payload.get("id", "")),
            seq=int(payload.get("seq", 0)),
            chemical=str(payload.get("chemical", "")),
            amount=float(payload.get("amount", 0.0)),
            detail=str(payload.get("detail", "0.0000")),
            recorded_at=int(payload.get("recorded_at", 0)),
            business_at=int(payload.get("business_at", 0)),
            cycle_id=str(payload.get("cycle_id", "")),
            boundary_rule=str(payload.get("boundary_rule", DOSE_START)),
        )

    def _settlement(self, payload: dict[str, object]) -> Settlement:
        return Settlement(
            id=str(payload.get("id", "")),
            chemical=str(payload.get("chemical", "")),
            cycle_id=str(payload.get("cycle_id", "")),
            usage=float(payload.get("usage", 0.0)),
            entry_count=int(payload.get("entry_count", 0)),
            started_at=int(payload.get("started_at", 0)),
            finished_at=int(payload.get("finished_at") or 0),
            boundary_time=int(payload.get("boundary_time", 0)),
            triggered_by=str(payload.get("triggered_by", "")),
            status=str(payload.get("status", "")),
            next_cycle_id=str(payload.get("next_cycle_id", "")),
        )

    def _save_locked(self, data: dict[str, str], ledger: dict[str, object]) -> None:
        data[LEDGER_KEY] = json.dumps(ledger, ensure_ascii=False, sort_keys=True)
