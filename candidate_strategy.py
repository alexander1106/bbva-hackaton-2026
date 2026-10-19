"""Estrategia de despacho de LOG-8092.

Idea general:
  * Las tarjetas se agrupan por (destino, zona, intento, capacidades requeridas),
    que es lo que el motor exige para formar un lote.
  * Un grupo se despacha cuando su tarjeta más antigua lleva ``HOLD_MINUTES``
    pendiente (siempre por debajo de los 45 min permitidos) o cuando es urgente.
  * El transportista se elige por coste esperado: precio exacto del lote más el
    valor del tiempo (ETA) y del riesgo de fallo (reintento: 24 h y otro cargo).
  * No se piden mejoras de flota: solo se usan las unidades que ya existen.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from interfaces import (
    BaseDispatcherStrategy,
    CarrierSnapshot,
    DeliveryAttemptResult,
    DispatchContext,
    FleetUpgradeDecision,
    MonthlyPlanningContext,
    Shipment,
)

# Minutos que se espera, como máximo, a que lleguen más tarjetas del mismo destino.
# Debe ser < 45 - tick (15) para no incumplir la retención voluntaria.
HOLD_MINUTES = 15
# Valor de un minuto de entrega, en céntimos por tarjeta, según producto
# (valor base por VALUE_SCALE; el factor se calibró barriendo 0,3-8 en data/).
VALUE_SCALE = 3.0
MINUTE_VALUE = {
    "VIP_BLACK": 6.0 * VALUE_SCALE,
    "BUSINESS": 1.5 * VALUE_SCALE,
    "SME": 1.5 * VALUE_SCALE,
    "STANDARD": 1.0 * VALUE_SCALE,
}
# Minutos que cuesta, de media, un fallo operativo o de ruta (reintento a +24 h).
RETRY_DELAY_MINUTES = 1440 + 120
# Fracción del precio que cuesta un reintento (el intento 2 vale la mitad).
RETRY_COST_FACTOR = 0.5
# Multiplicador del valor del tiempo cuando el plazo está comprometido.
URGENT_FACTOR = 5.0

_PRODUCT_ORDER = {"VIP_BLACK": 0, "BUSINESS": 1, "SME": 2, "STANDARD": 3}
_TICK = timedelta(minutes=15)


class CandidateStrategy(BaseDispatcherStrategy):
    """Despacho por grupos de destino con elección de transportista por coste esperado."""

    def __init__(self, carrier_service) -> None:
        super().__init__(carrier_service)
        # clave de grupo -> tarjetas (listas, para poder retirarlas al despachar)
        self.groups: Dict[tuple, List[Shipment]] = {}
        self._holidays: frozenset = frozenset()
        self._route_bps: Dict[str, int] = {}
        self._sunday_bps = 0
        self._holiday_bps = 0
        self._distances: Dict[str, int] = {}
        self._attempt_bps: Tuple[int, ...] = (10_000, 5_000, 10_000)
        self._extra_card = 120
        self._metal = 450
        self._policy_ready = False

    # ------------------------------------------------------------------ hooks
    def on_shipments_available(
        self, now: datetime, shipments: Tuple[Shipment, ...]
    ) -> None:
        for shipment in shipments:
            self._add(shipment)

    def on_delivery_succeeded(self, result: DeliveryAttemptResult) -> None:
        return None

    def on_delivery_failed(self, result: DeliveryAttemptResult) -> None:
        if not result.terminal:
            self._add(result.shipment)

    def on_monthly_planning(
        self, context: MonthlyPlanningContext
    ) -> Tuple[FleetUpgradeDecision, ...]:
        return ()

    def on_dispatch_tick(self, context: DispatchContext) -> None:
        if not self.groups:
            return
        if not self._policy_ready:
            self._load_policy(context)
        now = context.now
        carriers = context.carriers
        units = {c.carrier_id: c.available_units for c in carriers}
        hold = timedelta(minutes=HOLD_MINUTES)

        due: List[Tuple[tuple, List[Shipment]]] = []
        for key, members in self.groups.items():
            ready = [s for s in members if s.ready_at <= now]
            if not ready:
                continue
            oldest = min(s.ready_at for s in ready)
            # Reintentos y VIP no esperan: los primeros ya acumulan 24 h de retraso
            # y los VIP tienen un plazo de 24 h naturales.
            if (
                key[2] > 1
                or ready[0].product == "VIP_BLACK"
                or now >= oldest + hold
                or self._is_urgent(ready, now)
            ):
                due.append((key, ready))
        if not due:
            return

        # Primero lo más urgente: VIP y plazos más próximos.
        due.sort(
            key=lambda item: (
                min(s.sla_deadline for s in item[1]),
                _PRODUCT_ORDER[item[1][0].product],
            )
        )
        for key, ready in due:
            self._dispatch_group(key, ready, now, carriers, units)

    # --------------------------------------------------------------- internals
    def _add(self, shipment: Shipment) -> None:
        key = (
            shipment.delivery_cluster_id,
            shipment.zone,
            shipment.attempt_number,
            tuple(sorted(shipment.required_capabilities)),
        )
        self.groups.setdefault(key, []).append(shipment)

    def _load_policy(self, context: DispatchContext) -> None:
        self._holidays = frozenset(context.calendar.holidays)
        self._route_bps = {
            z.zone: z.inaccessible_route_bps for z in context.failure_policy.zones
        }
        self._sunday_bps = context.failure_policy.sunday_operation_modifier_bps
        self._holiday_bps = context.failure_policy.holiday_operation_modifier_bps
        policy = context.decision_policy
        if policy is not None:
            self._distances = {d.zone: d.distance_km for d in policy.distances}
            self._attempt_bps = tuple(policy.pricing.attempt_multipliers_bps)
            self._extra_card = policy.pricing.additional_card_cents
            self._metal = policy.pricing.metal_security_cents
        if not self._distances:
            self._distances = {"URBANA": 12, "SUBURBANA": 35, "RURAL": 80, "REMOTA": 150}
        self._policy_ready = True

    @staticmethod
    def _is_urgent(ready: List[Shipment], now: datetime) -> bool:
        # Si el plazo vence antes de que podamos esperar otro tick, no se retiene.
        return min(s.sla_deadline for s in ready) <= now + timedelta(minutes=90)

    def _dispatch_group(
        self,
        key: tuple,
        ready: List[Shipment],
        now: datetime,
        carriers: Tuple[CarrierSnapshot, ...],
        units: Dict[str, int],
    ) -> None:
        zone = key[1]
        attempt = key[2]
        required = set(key[3])
        pending = sorted(ready, key=lambda s: (s.sla_deadline, s.shipment_id))
        banned: set = set()
        is_sunday = now.isoweekday() == 7
        is_holiday = now.date() in self._holidays
        distance = self._distances[zone]
        attempt_bps = self._attempt_bps[min(attempt, len(self._attempt_bps)) - 1]
        route_bps = self._route_bps.get(zone, 0)

        while pending:
            best = None
            for c in carriers:
                if c.carrier_id in banned or units.get(c.carrier_id, 0) <= 0:
                    continue
                if not c.operates_now or zone not in c.allowed_zones:
                    continue
                if not required.issubset(c.capabilities):
                    continue
                k = min(len(pending), c.batch_capacity)
                if k <= 0:
                    continue
                chunk = pending[:k]
                score = self._score(
                    c, chunk, zone, distance, attempt_bps, route_bps, is_sunday,
                    is_holiday, now,
                )
                per_card = score / k
                if best is None or per_card < best[0]:
                    best = (per_card, c, k)
            if best is None:
                return
            _, carrier, k = best
            chunk = pending[:k]
            receipt = self.carrier_service.dispatch_batch(
                carrier.carrier_id, tuple(s.shipment_id for s in chunk)
            )
            if receipt.accepted:
                units[carrier.carrier_id] -= 1
                done = {s.shipment_id for s in chunk}
                pending = pending[k:]
                members = self.groups.get(key, [])
                members = [s for s in members if s.shipment_id not in done]
                if members:
                    self.groups[key] = members
                else:
                    self.groups.pop(key, None)
            else:
                reason = receipt.rejection_reason
                if reason == "NO_AVAILABLE_UNIT":
                    units[carrier.carrier_id] = 0
                banned.add(carrier.carrier_id)

    def _score(
        self,
        c: CarrierSnapshot,
        chunk: List[Shipment],
        zone: str,
        distance: int,
        attempt_bps: int,
        route_bps: int,
        is_sunday: bool,
        is_holiday: bool,
        now: datetime,
    ) -> float:
        k = len(chunk)
        surcharge = 0
        for zp in c.zone_prices:
            if zp.zone == zone:
                surcharge = zp.surcharge_cents
                break
        linehaul = c.base_fee_cents + surcharge
        adjusted = linehaul * attempt_bps * c.current_time_multiplier_bps / 100_000_000
        metals = sum(1 for s in chunk if s.is_metal)
        price = adjusted + self._extra_card * (k - 1) + self._metal * metals

        speed = c.speed_kmh * c.speed_multiplier_bps / 10_000
        outbound = distance * 60 / speed
        eta = c.pickup_minutes + outbound

        op_bps = c.operational_failure_bps
        if is_sunday:
            op_bps += self._sunday_bps
        if is_holiday:
            op_bps += self._holiday_bps
        p_op = min(op_bps, 10_000) / 10_000
        p_route = 0.0 if "HARD_ACCESS" in c.capabilities else route_bps / 10_000
        p_fail = 1 - (1 - p_op) * (1 - p_route)

        value = 0.0
        for s in chunk:
            v = MINUTE_VALUE[s.product]
            if s.sla_deadline <= now + timedelta(minutes=eta + 60):
                v *= URGENT_FACTOR
            value += v
        return (
            price
            + value * eta
            + p_fail * (RETRY_COST_FACTOR * price + value * RETRY_DELAY_MINUTES)
        )
