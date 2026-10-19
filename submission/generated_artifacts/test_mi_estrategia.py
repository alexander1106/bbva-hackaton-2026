"""Pruebas de la estrategia propia (no sustituyen a las del kit)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Sequence, Tuple

from candidate_strategy import CandidateStrategy
from interfaces import DeliveryAttemptResult, DispatchContext, DispatchReceipt, Shipment
from test_candidate_strategy_smoke import (
    LIMA,
    carrier_snapshot,
    failure_policy,
    rcp_policy,
    scenario_calendar,
)

T0 = datetime(2026, 1, 5, 10, 0, tzinfo=LIMA)  # lunes laborable


class Service:
    def __init__(self, carriers, reject: dict | None = None) -> None:
        self.carriers = tuple(carriers)
        self.dispatched: list = []
        self.reject = reject or {}

    def snapshot(self):
        return self.carriers

    def dispatch_batch(self, carrier_id: str, shipment_ids: Sequence[str]) -> DispatchReceipt:
        ids = tuple(shipment_ids)
        reason = self.reject.get(carrier_id)
        if reason:
            return DispatchReceipt(False, carrier_id, ids, T0, 0, rejection_reason=reason)
        self.dispatched.append((carrier_id, ids))
        return DispatchReceipt(True, carrier_id, ids, T0, 500, "B-1", T0)


def shipment(sid, ready_at=T0, product="STANDARD", cluster="C1", zone="URBANA", **kw):
    return Shipment(
        shipment_id=sid, available_at=ready_at, ready_at=ready_at,
        sla_deadline=ready_at + timedelta(days=3), product=product, zone=zone,
        delivery_cluster_id=cluster, is_metal=False, state="READY", attempt_number=1, **kw,
    )


def context(now, service):
    return DispatchContext(
        now=now, reference_budget_cents=1, spent_cents=0, contingency_threshold_cents=1,
        carriers=service.snapshot(), calendar=scenario_calendar(),
        failure_policy=failure_policy(), rcp_policy=rcp_policy(),
    )


def carrier(cid, **kw):
    return replace(carrier_snapshot(T0), carrier_id=cid, **kw)


class StrategyTest(unittest.TestCase):
    def test_waits_then_batches_same_cluster(self) -> None:
        service = Service([carrier("A")])
        strategy = CandidateStrategy(service)
        strategy.on_shipments_available(T0, (shipment("S1"),))
        strategy.on_dispatch_tick(context(T0, service))
        self.assertEqual(service.dispatched, [])  # aún dentro de la espera
        later = T0 + timedelta(minutes=10)
        strategy.on_shipments_available(later, (shipment("S2", ready_at=later),))
        due = T0 + timedelta(minutes=30)
        strategy.on_dispatch_tick(context(due, service))
        self.assertEqual(service.dispatched, [("A", ("S1", "S2"))])

    def test_never_holds_past_limit(self) -> None:
        service = Service([carrier("A")])
        strategy = CandidateStrategy(service)
        strategy.on_shipments_available(T0, (shipment("S1"),))
        for minutes in range(0, 45, 15):
            strategy.on_dispatch_tick(context(T0 + timedelta(minutes=minutes), service))
        self.assertEqual(len(service.dispatched), 1)

    def test_vip_is_not_held(self) -> None:
        service = Service([carrier("A")])
        strategy = CandidateStrategy(service)
        strategy.on_shipments_available(T0, (shipment("V1", product="VIP_BLACK"),))
        strategy.on_dispatch_tick(context(T0, service))
        self.assertEqual(service.dispatched, [("A", ("V1",))])

    def test_respects_batch_capacity_and_zone(self) -> None:
        service = Service([carrier("A", batch_capacity=2)])
        strategy = CandidateStrategy(service)
        strategy.on_shipments_available(T0, tuple(shipment(f"S{i}") for i in range(3)))
        strategy.on_dispatch_tick(context(T0 + timedelta(minutes=30), service))
        # Una sola unidad: despacha 2 y deja 1 pendiente.
        self.assertEqual(len(service.dispatched), 1)
        self.assertEqual(len(service.dispatched[0][1]), 2)
        self.assertTrue(strategy.groups)

    def test_requires_capability_after_inaccessible_route(self) -> None:
        plain = carrier("PLAIN", base_fee_cents=100)
        hard = carrier("HARD", capabilities=("HARD_ACCESS",), base_fee_cents=900)
        service = Service([plain, hard])
        strategy = CandidateStrategy(service)
        failed = shipment("S1", zone="RURAL", required_capabilities=("HARD_ACCESS",))
        failed = replace(failed, attempt_number=2, state="WAITING_RETRY")
        for c in service.carriers:
            pass
        service.carriers = (
            replace(plain, allowed_zones=("RURAL",)),
            replace(hard, allowed_zones=("RURAL",)),
        )
        strategy.on_delivery_failed(
            DeliveryAttemptResult(failed, "PLAIN", "B-0", 1, T0, False, "INACCESSIBLE_ROUTE")
        )
        strategy.on_dispatch_tick(context(T0, service))
        self.assertEqual(service.dispatched, [("HARD", ("S1",))])

    def test_retry_waits_until_ready(self) -> None:
        service = Service([carrier("A")])
        strategy = CandidateStrategy(service)
        failed = replace(shipment("S1", ready_at=T0 + timedelta(hours=24)), attempt_number=2)
        strategy.on_delivery_failed(DeliveryAttemptResult(failed, "A", "B-0", 1, T0, False, "CUSTOMER_ABSENT"))
        strategy.on_dispatch_tick(context(T0, service))
        self.assertEqual(service.dispatched, [])
        strategy.on_dispatch_tick(context(T0 + timedelta(hours=24), service))
        self.assertEqual(len(service.dispatched), 1)

    def test_rejections_do_not_loop_forever(self) -> None:
        service = Service([carrier("A"), carrier("B")], reject={"A": "OUTSIDE_OPERATING_WINDOW", "B": "NO_AVAILABLE_UNIT"})
        strategy = CandidateStrategy(service)
        strategy.on_shipments_available(T0, (shipment("S1", product="VIP_BLACK"),))
        strategy.on_dispatch_tick(context(T0, service))
        self.assertEqual(service.dispatched, [])
        self.assertTrue(strategy.groups)

    def test_monthly_planning_requests_nothing(self) -> None:
        self.assertEqual(CandidateStrategy(Service([carrier("A")])).on_monthly_planning(None), ())


if __name__ == "__main__":
    unittest.main()
