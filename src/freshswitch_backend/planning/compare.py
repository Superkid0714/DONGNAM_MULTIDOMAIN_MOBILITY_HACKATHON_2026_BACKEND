from typing import Literal

from pydantic import BaseModel, Field

from freshswitch_backend.domain.models import (
    Assignment,
    CostBreakdown,
    Plan,
    QuoteDelayInput,
    Scenario,
)
from freshswitch_backend.optimize.costs import CostModel, LotCost
from freshswitch_backend.optimize.feasibility import air_rejection, products_compatible, sea_option
from freshswitch_backend.optimize.settlement import (
    lot_plan_cost,
    lot_settle_cost,
    shipper_costs_and_settlements,
)
from freshswitch_backend.optimize.solo import sea_solo_cost

MAX_PULL_FORWARD_DAYS = 2


class SeaQuote(BaseModel):
    feasible: bool
    in_plan: bool
    container_id: str | None = None
    voyage_id: str | None = None
    pull_forward_days: int = 0
    container_pallets_after: int | None = None
    cost: CostBreakdown | None = None
    reason: str | None = None


class AirQuote(BaseModel):
    feasible: bool
    cost: CostBreakdown
    reason: str | None = None


class AirSeaComparison(BaseModel):
    scenario_id: str
    lot_id: str
    shipper_id: str
    product_id: str
    pallets: int
    kg: float
    plan_mode: Literal["sea", "air", "unassigned"]
    sea: SeaQuote
    air: AirQuote
    cheaper: Literal["sea", "air"] | None
    sea_savings: int | None
    quote_scope: Literal["shipper"] = "shipper"
    quote_lot_ids: list[str] = Field(default_factory=list)
    air_cost: int
    sea_solo_cost: int | None
    shared_cost: int | None
    fee: int = 0
    shipper_net_saving: int | None = None
    savings_vs_air_pct: float | None = None
    savings_vs_solo_pct: float | None = None
    air_switch_candidate: bool = False
    assumptions: list[str] = Field(default_factory=list)
    delay_inputs: list[QuoteDelayInput] = Field(default_factory=list)
    pricing_status: Literal["simulation"] = "simulation"
    price_guaranteed: bool = False
    logistics_only_comparison: bool = False
    pricing_warning: str = (
        "가정·보정 단가와 미검증 품질손실을 포함한 시뮬레이션입니다. "
        "실제 물류 청구액·확정 견적·AI 절감 실적·가격 보장이 아닙니다. "
        "근거 입력 비교는 /compare/evidence-quote를 사용하세요"
    )


def breakdown(cost: LotCost) -> CostBreakdown:
    parts = {
        "freight": round(cost.freight),
        "inland": round(cost.inland),
        "handling": round(cost.handling),
        "collection": round(cost.collection),
        "storage": round(cost.storage),
        "loss_risk": round(cost.loss_risk),
    }
    return CostBreakdown(**parts, total=sum(parts.values()))


def _container_pallets(plan: Plan) -> dict[str, int]:
    return {c.container_id: c.pallets for c in plan.containers}


def _sea_quote(model: CostModel, plan: Plan, lot_id: str) -> SeaQuote:
    lots = {lot.id: lot for lot in model.scenario.lots}
    lot = lots[lot_id]
    loads = _container_pallets(plan)
    assigned = next((a for a in plan.assignments if a.lot_id == lot_id), None)
    if assigned is not None and assigned.mode == "sea":
        routed = next((c.route for c in plan.lot_collections if c.lot_id == lot_id), None)
        cost = lot_settle_cost(model, lot, assigned, loads, routed)
        return SeaQuote(
            feasible=True,
            in_plan=True,
            container_id=assigned.container_id,
            voyage_id=assigned.voyage_id,
            pull_forward_days=assigned.pull_forward_days,
            container_pallets_after=loads[assigned.container_id],
            cost=breakdown(cost),
        )
    products = {c.container_id: c.product_ids for c in plan.containers}
    best: SeaQuote | None = None
    best_operational = 0.0
    reasons: list[str] = []
    for container in model.scenario.containers:
        option, reason = sea_option(model, lot, container, MAX_PULL_FORWARD_DAYS)
        if option is None:
            reasons.append(f"{container.id}: {reason}")
            continue
        load = loads.get(container.id, 0) + lot.pallets
        if load > container.capacity_pallets:
            reasons.append(f"{container.id}: 남은 자리 부족")
            continue
        clash = [
            p
            for p in products.get(container.id, [])
            if not products_compatible(model.products[p], model.product(lot))
        ]
        if clash:
            reasons.append(f"{container.id}: 혼적 불가({', '.join(clash)})")
            continue
        hypothetical = Assignment(
            lot_id=lot.id,
            shipper_id=lot.shipper_id,
            product_id=lot.product_id,
            pallets=lot.pallets,
            mode="sea",
            container_id=container.id,
            voyage_id=container.voyage_id,
            pull_forward_days=option.pull_forward_days,
        )
        loads_after = {container.id: load}
        operational = lot_plan_cost(model, lot, hypothetical, loads_after).total
        cost = breakdown(lot_settle_cost(model, lot, hypothetical, loads_after))
        if best is None or operational < best_operational:
            best_operational = operational
            best = SeaQuote(
                feasible=True,
                in_plan=False,
                container_id=container.id,
                voyage_id=container.voyage_id,
                pull_forward_days=option.pull_forward_days,
                container_pallets_after=load,
                cost=cost,
            )
    if best is not None:
        return best
    return SeaQuote(feasible=False, in_plan=False, reason="; ".join(reasons) or "해상 후보 없음")


def compare_air_sea(scenario: Scenario, plan: Plan, lot_id: str) -> AirSeaComparison:
    model = CostModel(scenario)
    lot = next((x for x in scenario.lots if x.id == lot_id), None)
    if lot is None:
        raise KeyError(lot_id)
    assigned = next((a for a in plan.assignments if a.lot_id == lot_id), None)
    sea = _sea_quote(model, plan, lot_id)
    air_reason = air_rejection(model, lot)
    air = AirQuote(
        feasible=air_reason is None, cost=breakdown(model.settle_air_cost(lot)), reason=air_reason
    )
    options = []
    if sea.feasible and sea.cost is not None:
        options.append(("sea", sea.cost.total))
    if air.feasible:
        options.append(("air", air.cost.total))
    cheaper = min(options, key=lambda o: o[1])[0] if options else None
    # Existing sea/air breakdowns retain their lot scope; new amounts consistently
    # cover this shipper's assigned lots plus the requested lot.
    assignments = [a for a in plan.assignments if a.lot_id != lot_id]
    if sea.feasible:
        assignments.append(
            Assignment(
                lot_id=lot.id,
                shipper_id=lot.shipper_id,
                product_id=lot.product_id,
                pallets=lot.pallets,
                mode="sea",
                container_id=sea.container_id,
                voyage_id=sea.voyage_id,
                pull_forward_days=sea.pull_forward_days,
            )
        )
    included = [a.lot_id for a in assignments if a.shipper_id == lot.shipper_id]
    included = sorted({*included, lot.id})
    metrics = {
        "air_cost": round(
            sum(model.air_lot_cost(x).total for x in scenario.lots if x.id in included)
        ),
        "sea_solo_cost": sea_solo_cost(model.scenario, included),
        "shared_cost": None,
    }
    if sea.feasible:
        repriced = shipper_costs_and_settlements(
            model,
            {x.id: x for x in scenario.lots},
            assignments,
            {c.lot_id: c.route for c in plan.lot_collections} if plan.lot_collections else None,
        )
        row = next(s for s in repriced.settlements if s.shipper_id == lot.shipper_id)
        metrics.update(
            {
                key: getattr(row, key)
                for key in (
                    "air_cost",
                    "sea_solo_cost",
                    "shared_cost",
                    "fee",
                    "shipper_net_saving",
                    "savings_vs_air_pct",
                    "savings_vs_solo_pct",
                    "air_switch_candidate",
                    "delay_inputs",
                )
            }
        )
    return AirSeaComparison(
        scenario_id=scenario.id,
        lot_id=lot.id,
        shipper_id=lot.shipper_id,
        product_id=lot.product_id,
        pallets=lot.pallets,
        kg=model.lot_kg(lot),
        plan_mode=assigned.mode if assigned else "unassigned",
        sea=sea,
        air=air,
        cheaper=cheaper,
        sea_savings=air.cost.total - sea.cost.total if sea.cost is not None else None,
        quote_lot_ids=included,
        assumptions=[
            "신규 금액 필드는 화주 단위, 기존 sea/air 세부내역과 sea_savings는 요청 로트 단위",
            "절감률은 공동비용과 수수료를 합친 화주 실부담 기준",
            "판매일이 1일 미만 남으면 항공 전환 후보; 자동 예약이나 전환 확정이 아님",
        ],
        **metrics,
    )
