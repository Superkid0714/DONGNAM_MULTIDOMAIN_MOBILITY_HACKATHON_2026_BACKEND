"""Read-only conservative display amounts; never feed this copy to the engine."""

import math
from collections import defaultdict
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from types import SimpleNamespace

from freshswitch_backend.domain.models import CostBreakdown
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.planning.robust.estimate import EvaluateRequest, estimate
from freshswitch_backend.planning.robust.rates import seed_inputs

NOTICE = "예상 표시값: 항공 하한·해상 상한, 공개요율·가정 기반. 표시 성과보수는 양의 예상 차액의20%; 청구 승인·저장 수수료 변경 아님"


def display_fee(row):
    """Presentation only. Preserve billing exceptions and free pilot policy."""
    if row.billing_rule == "performance":
        row.fee = 0 if row.pilot else round(Decimal(max(0, row.savings)) * Decimal("0.20"))
        row.performance_fee = row.fee
        row.usage_fee = 0


def display_lot_amounts(scenario, lot, container_id, load, pull=0):
    """Shared display adapter for intake, assigned plans and hypothetical quotes."""
    model = CostModel(scenario)
    voyage = model.voyage_of(model.containers[container_id])
    warehouse_days = model.sea_wait_days(lot, voyage, pull)
    plug_days = 2 + model.terminal_extra_days(voyage)
    quote = SimpleNamespace(
        shipment_date=lot.ready_date,
        cargo=SimpleNamespace(pallets=lot.pallets, gross_weight_kg=Decimal(str(model.lot_kg(lot)))),
    )
    body = seed_inputs(quote, "medium")
    body.container_pallets = load
    for item in body.ocean:
        if item.key == "plug":
            rate = Decimal(str(scenario.cost_params.terminal_reefer_plug_per_container_day.value))
            if voyage.etd.month in (3, 4, 5):
                rate *= Decimal("1.2")
            item.minimum = item.maximum = rate * Decimal(str(plug_days))
    from freshswitch_backend.planning.robust.estimate import EstimateItem

    response = estimate(
        EvaluateRequest(
            **body.model_dump(),
            estimate_equipment="40ft",
            estimate_plug_days=min(365, math.ceil(plug_days)),
            estimate_items=[
                EstimateItem(
                    key="origin_warehouse",
                    supplier="engine_wait_days",
                    basis="per_shipper",
                    minimum=Decimal(
                        str(
                            lot.pallets
                            * warehouse_days
                            * scenario.cost_params.storage_per_pallet_day_krw.value
                        )
                    ),
                    maximum=Decimal(
                        str(
                            lot.pallets
                            * warehouse_days
                            * scenario.cost_params.storage_per_pallet_day_krw.value
                        )
                    ),
                    source="엔진 sea_wait_days × storage_per_pallet_day_krw",
                )
            ],
        )
    )
    if response["status"] != "estimated":
        return None
    ranges = response["logistics_ranges"]
    return (
        int(Decimal(ranges["air"]["low"]).to_integral_value(rounding=ROUND_FLOOR)),
        int(Decimal(ranges["ocean"]["high"]).to_integral_value(rounding=ROUND_CEILING)),
    )


def display_plan(scenario, plan):
    result = plan.model_copy(deep=True)
    products = {p.id: p for p in scenario.products}
    lots = {l.id: l for l in scenario.lots}
    loads = defaultdict(int)
    grouped = defaultdict(list)
    for a in plan.assignments:
        grouped[a.shipper_id].append(a)
        if a.mode == "sea":
            loads[a.container_id] += a.pallets
    projected = {}
    # Restrict to the strawberry Hong Kong pilot; no generic extrapolation to other products.
    for sid, assignments in grouped.items():
        if not all(
            a.product_id == "strawberry" and lots[a.lot_id].destination == "HKHKG"
            for a in assignments
        ):
            continue
        sea = [a for a in assignments if a.mode == "sea"]
        cids = {a.container_id for a in sea}
        if len(cids) > 1 or (sea and len(sea) != len(assignments)):
            continue
        pallets = sum(a.pallets for a in assignments)
        kg = sum(Decimal(str(products[a.product_id].pallet_kg)) * a.pallets for a in assignments)
        quote = SimpleNamespace(
            shipment_date=max(lots[a.lot_id].ready_date for a in assignments),
            cargo=SimpleNamespace(pallets=pallets, gross_weight_kg=kg),
        )
        body = seed_inputs(quote, "medium")
        body.container_pallets = loads[next(iter(cids))] if sea else pallets
        response = estimate(EvaluateRequest(**body.model_dump(), estimate_equipment="40ft"))
        if response["status"] != "estimated":
            continue
        ranges = response["logistics_ranges"]
        air = int(Decimal(ranges["air"]["low"]).to_integral_value(rounding=ROUND_FLOOR))
        shared = (
            int(Decimal(ranges["ocean"]["high"]).to_integral_value(rounding=ROUND_CEILING))
            if sea
            else air
        )
        projected[sid] = (air, shared)
        if len(assignments) == 1 and sea:
            a = assignments[0]
            values = display_lot_amounts(
                scenario, lots[a.lot_id], a.container_id, loads[a.container_id], a.pull_forward_days
            )
            if values is not None:
                projected[sid] = values
    for row in result.settlements:
        if row.shipper_id not in projected:
            continue
        air, shared = projected[row.shipper_id]
        row.baseline_cost = row.air_cost = air
        row.shared_cost = shared
        row.savings = air - shared
        display_fee(row)
        row.shipper_gain = row.shipper_net_saving = row.savings - row.fee
        row.savings_vs_air_pct = round(100 * row.shipper_gain / air, 2) if air else None
        row.sea_solo_cost = None
        row.savings_vs_solo_pct = None
        row.quote_assumptions = [*row.quote_assumptions, NOTICE]
    for container in result.container_settlements:
        for row in container.shippers:
            if row.shipper_id not in projected:
                continue
            air, shared = projected[row.shipper_id]
            row.baseline_cost = row.air_cost = air
            row.shared_cost = shared
            row.savings = air - shared
            display_fee(row)
            row.shipper_gain = row.shipper_net_saving = row.savings - row.fee
            row.sea_solo_cost = None
            row.savings_vs_air_pct = round(100 * row.shipper_gain / air, 2) if air else None
            row.savings_vs_solo_pct = None
            row.quote_assumptions = [*row.quote_assumptions, NOTICE]
        container.total_savings = sum(r.savings for r in container.shippers)
        container.total_shipper_gain = sum(r.shipper_gain for r in container.shippers)
        container.total_fee = sum(r.fee for r in container.shippers)
    for row in result.shipper_costs:
        if row.shipper_id in projected:
            air, shared = projected[row.shipper_id]
            row.baseline = CostBreakdown(freight=air, total=air)
            row.plan = CostBreakdown(freight=shared, total=shared)
    result.total_baseline_cost = sum(r.baseline_cost for r in result.settlements)
    result.total_shared_cost = sum(r.shared_cost for r in result.settlements)
    result.total_savings = sum(r.savings for r in result.settlements)
    result.total_fee = sum(r.fee for r in result.settlements)
    return result


def display_session(session):
    return session.model_copy(update={"plan": display_plan(session.scenario, session.plan)})


def display_replan(scenario, current_plan, result):
    """Project candidate summaries only after pending engine result has been stored."""
    from freshswitch_backend.replan.engine import apply_event

    projected = result.model_copy(deep=True)
    before = display_plan(scenario, current_plan)
    after_scenario = result.scenario_after or apply_event(scenario, result.event)
    before_rows = {r.shipper_id: r for r in before.settlements}
    for alternative in projected.alternatives:
        alternative.plan = display_plan(after_scenario, alternative.plan)
        alternative.total_cost = alternative.plan.total_shared_cost
        alternative.cost_delta = alternative.total_cost - before.total_shared_cost
        alternative.summary = (
            f"{alternative.label}: 예상 물류비 {alternative.total_cost:,}원, "
            f"변경 전 대비 {alternative.cost_delta:+,}원. {NOTICE}"
        )
        for delta in alternative.diff.shippers:
            old = before_rows.get(delta.shipper_id)
            new = next(
                (r for r in alternative.plan.settlements if r.shipper_id == delta.shipper_id), None
            )
            delta.cost_before = old.shared_cost if old else 0
            delta.cost_after = new.shared_cost if new else 0
            delta.cost_delta = delta.cost_after - delta.cost_before
        if alternative.id == projected.recommended_id:
            projected.diff = alternative.diff
    if projected.explanation is not None:
        projected.explanation.operator_summary = (
            "예상 표시 금액은 후보별 요약을 확인하세요. " + NOTICE
        )
        for message in projected.explanation.shipper_messages:
            message.message = "변경 전후 예상 물류비는 후보별 화주 비용을 확인하세요. " + NOTICE
    return projected
