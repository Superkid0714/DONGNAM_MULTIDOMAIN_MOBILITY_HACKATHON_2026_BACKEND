from datetime import datetime

from pydantic import BaseModel, Field

from freshswitch_backend.domain.models import Plan, Scenario, Settlement
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.settlement import lot_settle_cost

RULE = (
    "실제 손실률이 계획 손실 가정을 넘으면 넘은 손실액만큼 그 화주의 성과보수에서 뺀다(하한 0). "
    "환급 한도는 그 화주가 낸 성과보수 전액, 넘는 손실은 적하보험 영역"
)
SOURCE = (
    "제안서 라(정산): 실제 손실이 예측을 넘으면 넘은 만큼만 성과보수에서 빼고(하한 0), "
    "큰 손실은 적하보험이 맡는다"
)


class ArrivalRequest(BaseModel):
    lot_id: str
    inspected_loss_pct: float = Field(ge=0, le=100)
    note: str | None = None


class ArrivalInspection(BaseModel):
    lot_id: str
    shipper_id: str
    planned_loss_rate: float
    inspected_loss_rate: float
    excess_loss_value: int
    note: str | None = None
    inspected_at: datetime


class QualitySettlement(Settlement):
    fee_before_refund: int
    quality_refund: int


class ArrivalSettlementResult(BaseModel):
    plan_id: str
    lot_id: str
    shipper_id: str
    mode: str
    pallets: int
    lot_value: int
    planned_loss_rate: float
    inspected_loss_rate: float
    excess_loss_rate: float
    excess_loss_value: int
    shipper_excess_loss_total: int
    refund_cap: int
    refund_before: int
    refund_after: int
    refund_change: int
    capped: bool
    uncovered_loss: int
    settlement_before: QualitySettlement
    settlement_after: QualitySettlement
    platform_fee_before: int
    platform_fee_after: int
    platform_net_change: int
    rule: str = RULE
    source: str = SOURCE
    note: str | None = None


class QualityError(ValueError):
    pass


def planned_loss_rate(scenario: Scenario, plan: Plan, lot_id: str) -> tuple[float, int, str, int]:
    model = CostModel(scenario)
    lot = next((x for x in scenario.lots if x.id == lot_id), None)
    assignment = next((a for a in plan.assignments if a.lot_id == lot_id), None)
    if lot is None or assignment is None:
        raise QualityError(f"계획에 실린 로트가 아닙니다: {lot_id}")
    value = model.lot_value(lot)
    loads = {c.container_id: c.pallets for c in plan.containers}
    loss = lot_settle_cost(model, lot, assignment, loads).loss_risk
    return loss / value, round(value), assignment.mode, lot.pallets


def current_inspections(
    plan: Plan, inspections: list[ArrivalInspection]
) -> list[ArrivalInspection]:
    assigned = {(a.lot_id, a.shipper_id) for a in plan.assignments}
    return [i for i in inspections if (i.lot_id, i.shipper_id) in assigned]


def _refund(fee: int, inspections: list[ArrivalInspection], shipper_id: str) -> tuple[int, int]:
    excess = sum(i.excess_loss_value for i in inspections if i.shipper_id == shipper_id)
    return min(max(fee, 0), excess), excess


def adjusted(settlement: Settlement, refund: int) -> QualitySettlement:
    return QualitySettlement(
        **settlement.model_dump(exclude={"fee", "shipper_gain"}),
        fee=settlement.fee - refund,
        shipper_gain=settlement.shipper_gain + refund,
        fee_before_refund=settlement.fee,
        quality_refund=refund,
    )


def adjusted_settlements(
    plan: Plan, inspections: list[ArrivalInspection]
) -> list[QualitySettlement]:
    inspections = current_inspections(plan, inspections)
    return [adjusted(s, _refund(s.fee, inspections, s.shipper_id)[0]) for s in plan.settlements]


def arrival_settlement(
    plan_id: str,
    scenario: Scenario,
    plan: Plan,
    inspections: list[ArrivalInspection],
    body: ArrivalRequest,
    now: datetime,
) -> tuple[ArrivalSettlementResult, list[ArrivalInspection]]:
    rate, value, mode, pallets = planned_loss_rate(scenario, plan, body.lot_id)
    lot = next(x for x in scenario.lots if x.id == body.lot_id)
    settlement = next((s for s in plan.settlements if s.shipper_id == lot.shipper_id), None)
    if settlement is None:
        raise QualityError(f"정산 내역이 없는 화주입니다: {lot.shipper_id}")
    inspected = body.inspected_loss_pct / 100
    rate = round(rate, 6)
    excess_rate = max(0.0, inspected - rate)
    excess_value = round(value * excess_rate)
    record = ArrivalInspection(
        lot_id=body.lot_id,
        shipper_id=lot.shipper_id,
        planned_loss_rate=round(rate, 6),
        inspected_loss_rate=round(inspected, 6),
        excess_loss_value=excess_value,
        note=body.note,
        inspected_at=now,
    )
    previous = [i for i in inspections if i.lot_id != body.lot_id]
    updated = [*previous, record]
    refund_before, _ = _refund(
        settlement.fee, current_inspections(plan, inspections), lot.shipper_id
    )
    refund_after, excess_total = _refund(
        settlement.fee, current_inspections(plan, updated), lot.shipper_id
    )
    total_before = sum(s.fee for s in adjusted_settlements(plan, inspections))
    total_after = sum(s.fee for s in adjusted_settlements(plan, updated))
    result = ArrivalSettlementResult(
        plan_id=plan_id,
        lot_id=body.lot_id,
        shipper_id=lot.shipper_id,
        mode=mode,
        pallets=pallets,
        lot_value=value,
        planned_loss_rate=round(rate, 6),
        inspected_loss_rate=round(inspected, 6),
        excess_loss_rate=round(excess_rate, 6),
        excess_loss_value=excess_value,
        shipper_excess_loss_total=excess_total,
        refund_cap=max(settlement.fee, 0),
        refund_before=refund_before,
        refund_after=refund_after,
        refund_change=refund_after - refund_before,
        capped=excess_total > refund_after,
        uncovered_loss=excess_total - refund_after,
        settlement_before=adjusted(settlement, refund_before),
        settlement_after=adjusted(settlement, refund_after),
        platform_fee_before=total_before,
        platform_fee_after=total_after,
        platform_net_change=total_after - total_before,
        note=body.note,
    )
    return result, updated
