from collections import defaultdict

from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.execution.params import ExecutionParams
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.replan.models import (
    Alternative,
    CancelEvent,
    CancellationCharge,
    CancellationTransfer,
    ReduceEvent,
    ReplanResult,
)

RULE = (
    "취소·감량 화주는 같은 컨테이너에 남은 화물의 운임·부대비 몫 증가분(데드프레이트)을 "
    "부담한다. 빈자리를 다른 화물로 채워 몫이 오르지 않으면 0원. 운영비 분담 화주는 실제로 "
    "늘어난 몫만큼 돌려받는다. 모든 품목에 같은 운영 원가 배분 기준을 적용한다"
)


def split_amount(total: int, weights: list[float]) -> list[int]:
    weight_sum = sum(weights)
    if not weights or weight_sum <= 0:
        return [0] * len(weights)
    raw = [total * w / weight_sum for w in weights]
    floors = [int(r) for r in raw]
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[: total - sum(floors)]:
        floors[i] += 1
    return floors


def _container_pallets(plan: Plan, container_id: str) -> dict[str, int]:
    by_shipper: dict[str, int] = defaultdict(int)
    for a in plan.assignments:
        if a.mode == "sea" and a.container_id == container_id:
            by_shipper[a.shipper_id] += a.pallets
    return dict(by_shipper)


def _recipient_kind(transfers: list[CancellationTransfer], consolidator_amount: int) -> str | None:
    received = any(t.amount > 0 for t in transfers)
    if received and consolidator_amount:
        return "mixed"
    if received:
        return "shippers"
    if consolidator_amount:
        return "consolidator"
    return None


def cancellation_charge(
    scenario_before: Scenario,
    plan_before: Plan,
    scenario_after: Scenario,
    alternative: Alternative,
    event: CancelEvent | ReduceEvent,
    params: ExecutionParams | None = None,
) -> CancellationCharge | None:
    if event.type == "increase":
        return None
    params = params or ExecutionParams()
    lot = next((x for x in scenario_before.lots if x.id == event.lot_id), None)
    assignment = next((a for a in plan_before.assignments if a.lot_id == event.lot_id), None)
    if lot is None or assignment is None or assignment.mode != "sea":
        return None
    container_id = assignment.container_id
    cancelled = lot.pallets if isinstance(event, CancelEvent) else lot.pallets - event.new_pallets
    before = _container_pallets(plan_before, container_id)
    after = _container_pallets(alternative.plan, container_id)
    before_total = sum(before.values())
    after_total = sum(after.values())
    model = CostModel(scenario_after)
    container = model.containers[container_id]
    plug = model.container_plug_cost(container)
    cost = model.container_fixed_cost + plug
    per_before = cost / before_total
    per_after = cost / after_total if after_total else 0.0
    step = 1 / after_total - 1 / before_total if after_total else 0.0
    refilled = max(0, min(cancelled, after_total - (before_total - cancelled)))
    rate = params.cancellation_fee_rate.value
    lots = {x.id: x for x in scenario_after.lots}
    shipper_increase: dict[str, float] = defaultdict(float)
    consolidator_increase = 0.0
    for a in alternative.plan.assignments:
        sid = a.shipper_id
        if a.mode != "sea" or a.container_id != container_id:
            continue
        if sid == lot.shipper_id or sid not in before:
            continue
        rise = max(0.0, a.pallets * step)
        tariff = a.lot_id in lots and model.tariff(lots[a.lot_id]) is not None
        if tariff:
            shipper_increase[sid] += rise * plug
            consolidator_increase += rise * model.container_fixed_cost
        else:
            shipper_increase[sid] += rise * cost
    others = sorted(shipper_increase)
    increases = [shipper_increase[sid] for sid in others]
    fee = round((sum(increases) + consolidator_increase) * rate)
    *amounts, consolidator_amount = split_amount(fee, [*increases, consolidator_increase])
    transfers = [
        CancellationTransfer(
            shipper_id=sid, pallets=after[sid], share_increase=round(increase), amount=amount
        )
        for sid, increase, amount in zip(others, increases, amounts, strict=True)
        if increase > 0
    ]
    return CancellationCharge(
        payer_shipper_id=lot.shipper_id,
        lot_id=lot.id,
        container_id=container_id,
        voyage_id=container.voyage_id,
        cancelled_pallets=cancelled,
        refilled_pallets=refilled,
        dead_pallets=cancelled - refilled,
        container_pallets_before=before_total,
        container_pallets_after=after_total,
        container_cost=round(cost),
        share_per_pallet_before=round(per_before),
        share_per_pallet_after=round(per_after),
        fee_rate=rate,
        fee=fee,
        transfers=transfers,
        recipient_kind=_recipient_kind(transfers, consolidator_amount),
        consolidator=params.cancellation_consolidator,
        consolidator_amount=consolidator_amount,
        rule=RULE,
        source=f"{params.cancellation_fee_rate.source}. {params.cancellation_consolidator_source}",
    )


def annotate_cancellations(
    result: ReplanResult,
    scenario_before: Scenario,
    plan_before: Plan,
    params: ExecutionParams | None = None,
) -> ReplanResult:
    event = result.event
    if not isinstance(event, CancelEvent | ReduceEvent) or result.scenario_after is None:
        return result
    for alternative in result.alternatives:
        charge = cancellation_charge(
            scenario_before, plan_before, result.scenario_after, alternative, event, params
        )
        alternative.diff.cancellation = charge
        if charge is None or charge.fee == 0:
            continue
        received = {t.shipper_id: t.amount for t in charge.transfers}
        for delta in alternative.diff.shippers:
            if delta.shipper_id == charge.payer_shipper_id:
                delta.cancellation_adjustment = charge.fee
            elif delta.shipper_id in received:
                delta.cancellation_adjustment = -received[delta.shipper_id]
    return result
