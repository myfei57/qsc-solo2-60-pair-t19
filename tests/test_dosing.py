"""Behavioural tests for durable chemical dosing cycles."""

from __future__ import annotations

import tempfile
import unittest

from waterplant.audit import Auditor
from waterplant.dosing import (
    OPEN,
    QUANTITY,
    TIME,
    CyclePolicy,
    DosingLedger,
    Settlement,
    SettlementInProgress,
)
from waterplant.quota import Accumulator
from waterplant.store import Store


class DosingLedgerCase(unittest.TestCase):
    def test_quantity_cycle_settles_full_overshoot_then_starts_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = DosingLedger(
                Store.open(f"{tmp}/state.json"),
                clock=iter([100, 200, 300, 400]).__next__,
                id_factory=iter(["a", "b", "c", "d"]).__next__,
            )
            first = ledger.record_dose("chlorine", 60.0)
            self.assertIsNone(first.settlement)
            self.assertEqual(first.cycle["usage"], 60.0)

            second = ledger.record_dose("chlorine", 50.0)
            self.assertEqual(second.entry.cycle_id, "chlorine-cycle-1")
            self.assertEqual(second.settlement.usage, 110.0)
            self.assertEqual(second.settlement.entry_count, 2)
            self.assertEqual(second.settlement.next_cycle_id, "chlorine-cycle-2")
            self.assertEqual(second.cycle["id"], "chlorine-cycle-2")
            self.assertEqual(second.cycle["usage"], 0.0)
            self.assertGreaterEqual(second.cycle["usage"], 0.0)

            third = ledger.record_dose("chlorine", 1.0)
            self.assertEqual(third.entry.cycle_id, "chlorine-cycle-2")
            self.assertEqual(third.cycle["usage"], 1.0)
            self.assertEqual([dose.cycle_id for dose in ledger.doses()], [
                "chlorine-cycle-1",
                "chlorine-cycle-1",
                "chlorine-cycle-2",
            ])

    def test_pending_settlement_blocks_doses_and_recovery_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            ledger = DosingLedger(
                store,
                clock=iter([100, 200, 201, 202, 203, 204, 205]).__next__,
                id_factory=iter(["a", "b", "c", "d", "e"]).__next__,
            )
            ledger.record_dose("chlorine", 40.0)
            cycle_id = ledger.begin_settlement("chlorine", force=True)
            self.assertEqual(cycle_id, "chlorine-cycle-1")
            self.assertEqual(ledger.begin_settlement("chlorine", force=True), cycle_id)

            with self.assertRaises(SettlementInProgress):
                ledger.record_dose("chlorine", 1.0)
            self.assertEqual(len(ledger.doses()), 1)

            recovered = ledger.recover("chlorine")
            self.assertIsInstance(recovered, Settlement)
            self.assertEqual(recovered.usage, 40.0)
            self.assertEqual(recovered.next_cycle_id, "chlorine-cycle-2")
            self.assertIsNone(ledger.recover("chlorine"))

            accepted = ledger.record_dose("chlorine", 2.0)
            self.assertEqual(accepted.entry.cycle_id, "chlorine-cycle-2")
            self.assertEqual(len(ledger.settlements()), 1)
            self.assertEqual(len(ledger.doses()), 2)

    def test_fixed_time_boundary_attributes_full_dose_to_next_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = DosingLedger(
                Store.open(f"{tmp}/state.json"),
                clock=iter([100, 200, 201, 202, 350, 351, 352]).__next__,
                id_factory=iter(list("abcdefg")).__next__,
            )
            ledger.set_policy(CyclePolicy("chlorine", TIME, duration_seconds=100))
            first = ledger.record_dose("chlorine", 10.0)
            self.assertEqual(first.entry.cycle_id, "chlorine-cycle-1")

            boundary = ledger.record_dose("chlorine", 5.0)
            self.assertEqual(boundary.settlement.cycle_id, "chlorine-cycle-1")
            self.assertEqual(boundary.settlement.usage, 10.0)
            self.assertEqual(boundary.settlement.boundary_time, 200)
            self.assertEqual(boundary.entry.cycle_id, "chlorine-cycle-2")
            self.assertEqual(boundary.entry.boundary_rule, "dose_start")

            late = ledger.record_dose("chlorine", 7.0)
            self.assertEqual(late.settlement.cycle_id, "chlorine-cycle-2")
            self.assertEqual(late.settlement.boundary_time, 300)
            self.assertEqual(late.settlement.usage, 5.0)
            self.assertEqual(late.entry.cycle_id, "chlorine-cycle-3")

    def test_auditor_and_accumulator_share_one_consistent_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            auditor = Auditor(store)
            accumulator = Accumulator(store)
            entry = auditor.record("chlorine", "40.0000")
            self.assertEqual(entry.kind, "chlorine")
            self.assertEqual(entry.cycle_id, "chlorine-cycle-1")
            self.assertEqual(accumulator.value(), 40.0)
            self.assertEqual(auditor.totals(), {"chlorine": 40.0})

            result = accumulator.ledger.record_dose("chlorine", 70.0)
            self.assertIsNotNone(result.settlement)
            self.assertEqual(auditor.count(), 2)
            self.assertEqual(accumulator.value(), 0.0)
            self.assertEqual([item.chemical for item in accumulator.ledger.settlements()], ["chlorine"])

    def test_policy_validation_and_open_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = DosingLedger(Store.open(f"{tmp}/state.json"))
            ledger.set_policy(CyclePolicy("reagent", OPEN))
            ledger.record_dose("reagent", 1_000_000.0)
            self.assertIsNone(ledger.settle("reagent"))
            with self.assertRaises(ValueError):
                ledger.set_policy(CyclePolicy("reagent", QUANTITY, cap=0.0))
            with self.assertRaises(ValueError):
                ledger.set_policy(CyclePolicy("reagent", TIME, duration_seconds=0))


if __name__ == "__main__":
    unittest.main()
