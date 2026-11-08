import time

from freshswitch_backend.analysis.common import locations, solve_routed
from freshswitch_backend.analysis.models import (
    FuelShockCase,
    FuelShockResult,
    ModeSwitch,
    ShockContainer,
    ShockLot,
    ShockShipper,
    SurchargeThreshold,
)
from freshswitch_backend.analysis.params import ANCHOR_PRODUCT, PALLET_KG
from freshswitch_backend.analysis.sensitivity import bisect_factor
from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.optimize import AIR

SURCHARGE_BEFORE_2026_04 = 470.0
SURCHARGE_AFTER_2026_04 = 2060.0
DEFAULT_SURCHARGES: tuple[float, ...] = (SURCHARGE_BEFORE_2026_04, SURCHARGE_AFTER_2026_04)
DEFAULT_THRESHOLD_MAX = 5000.0
THRESHOLD_TOLERANCE_KRW = 1.0
UNASSIGNED = "UNASSIGNED"
SURCHARGE_SOURCE = (
    "한국농어민신문 2026.4: 항공 유류할증료 kg당 470원 → 2,060원(2026.4.16), "
    "딸기 1파렛트(415kg) 항공 운임 89만 → 155만 원"
)


class _Solver:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.count = 0
        self.cache: dict[float, Plan] = {}

    def at(self, surcharge: float) -> Plan:
        key = round(surcharge, 3)
        if key not in self.cache:
            self.count += 1
            self.cache[key] = solve_routed(with_surcharge(self.scenario, key))
        return self.cache[key]


def with_surcharge(scenario: Scenario, surcharge: float) -> Scenario:
    copy = scenario.model_copy(deep=True)
    copy.cost_params.air_fuel_surcharge_per_kg_krw.value = surcharge
    return copy


def _pallet_kg(scenario: Scenario) -> float:
    products = {p.id: p for p in scenario.products}
    anchor = products.get(ANCHOR_PRODUCT) or next(iter(products.values()), None)
    return anchor.net_pallet_kg if anchor is not None else PALLET_KG


def _mode(location: str) -> str:
    if location == AIR:
        return "air"
    if location == UNASSIGNED:
        return "unassigned"
    return "sea"


def _direction(base: str, case: str) -> str:
    before, after = _mode(base), _mode(case)
    if before == "unassigned":
        return "from_unassigned"
    if after == "unassigned":
        return "to_unassigned"
    if before == "air":
        return "air_to_sea"
    if after == "air":
        return "sea_to_air"
    return "sea_to_sea"


def _switches(scenario: Scenario, base: Plan, case: Plan) -> list[ModeSwitch]:
    before, after = locations(base), locations(case)
    lots = {lot.id: lot for lot in scenario.lots}
    return [
        ModeSwitch(
            lot_id=lot_id,
            shipper_id=lots[lot_id].shipper_id,
            pallets=lots[lot_id].pallets,
            base=before.get(lot_id, UNASSIGNED),
            case=after.get(lot_id, UNASSIGNED),
            direction=_direction(before.get(lot_id, UNASSIGNED), after.get(lot_id, UNASSIGNED)),
        )
        for lot_id in lots
        if before.get(lot_id, UNASSIGNED) != after.get(lot_id, UNASSIGNED)
    ]


def _lots(scenario: Scenario, plan: Plan) -> list[ShockLot]:
    where = locations(plan)
    voyage = {a.lot_id: a.voyage_id for a in plan.assignments if a.mode == "sea"}
    return [
        ShockLot(
            lot_id=lot.id,
            shipper_id=lot.shipper_id,
            product_id=lot.product_id,
            pallets=lot.pallets,
            mode=_mode(where.get(lot.id, UNASSIGNED)),
            location=where.get(lot.id, UNASSIGNED),
            voyage_id=voyage.get(lot.id),
        )
        for lot in scenario.lots
    ]


def _shippers(plan: Plan, base: Plan) -> list[ShockShipper]:
    costs = {c.shipper_id: c for c in plan.shipper_costs}
    base_savings = {s.shipper_id: s.savings for s in base.settlements}
    return [
        ShockShipper(
            shipper_id=s.shipper_id,
            pallets=costs[s.shipper_id].pallets if s.shipper_id in costs else 0,
            sea_pallets=costs[s.shipper_id].sea_pallets if s.shipper_id in costs else 0,
            air_pallets=costs[s.shipper_id].air_pallets if s.shipper_id in costs else 0,
            baseline_cost=s.baseline_cost,
            plan_cost=s.shared_cost,
            savings=s.savings,
            fee=s.fee,
            shipper_gain=s.shipper_gain,
            savings_delta=s.savings - base_savings.get(s.shipper_id, 0),
        )
        for s in plan.settlements
    ]


def _summary(surcharge: float, case: Plan, base: Plan, switches: list[ModeSwitch]) -> str:
    head = f"유류할증료 {surcharge:,.0f}원/kg"
    delta = f"총 절감 {case.total_savings - base.total_savings:+,}원"
    if not switches:
        return f"{head}: 배정 변화 없음, {delta}"
    parts = []
    for direction, label in (
        ("air_to_sea", "항공→해상"),
        ("sea_to_air", "해상→항공"),
        ("sea_to_sea", "컨테이너 이동"),
        ("to_unassigned", "미배정"),
        ("from_unassigned", "미배정 해소"),
    ):
        moved = [s for s in switches if s.direction == direction]
        if moved:
            pallets = sum(s.pallets for s in moved)
            parts.append(f"{label} {len(moved)}로트 {pallets}파렛트")
    base_used = {c.container_id for c in base.containers if c.used}
    case_used = {c.container_id: c for c in case.containers if c.used}
    opened = [
        f"{c.container_id} {c.pallets}/{c.capacity_pallets}"
        for cid, c in case_used.items()
        if cid not in base_used
    ]
    closed = sorted(base_used - set(case_used))
    if opened:
        parts.append("컨테이너 추가 " + ", ".join(opened))
    if closed:
        parts.append("컨테이너 안 씀 " + ", ".join(closed))
    return f"{head}: {', '.join(parts)}, {delta}"


def _case(
    scenario: Scenario,
    surcharge: float,
    base_freight: float,
    pallet_kg: float,
    plan: Plan,
    base: Plan,
    is_baseline: bool,
) -> FuelShockCase:
    switches = _switches(scenario, base, plan)
    lots = _lots(scenario, plan)
    rate = base_freight + surcharge
    return FuelShockCase(
        surcharge_per_kg=surcharge,
        air_rate_per_kg=rate,
        air_freight_per_pallet=round(rate * pallet_kg),
        is_baseline=is_baseline,
        status=plan.status,
        lots=lots,
        containers=[
            ShockContainer(
                container_id=c.container_id,
                voyage_id=c.voyage_id,
                used=c.used,
                pallets=c.pallets,
                capacity_pallets=c.capacity_pallets,
                utilization=c.utilization,
                lot_ids=c.lot_ids,
            )
            for c in plan.containers
        ],
        shippers=_shippers(plan, base),
        containers_used=sum(1 for c in plan.containers if c.used),
        sea_pallets=sum(x.pallets for x in lots if x.mode == "sea"),
        air_pallets=sum(x.pallets for x in lots if x.mode == "air"),
        unassigned_pallets=sum(x.pallets for x in lots if x.mode == "unassigned"),
        total_baseline_cost=plan.total_baseline_cost,
        total_shared_cost=plan.total_shared_cost,
        total_savings=plan.total_savings,
        total_fee=plan.total_fee,
        plan_changed=bool(switches),
        switches=switches,
        savings_delta=plan.total_savings - base.total_savings,
        fee_delta=plan.total_fee - base.total_fee,
        shared_cost_delta=plan.total_shared_cost - base.total_shared_cost,
        summary=_summary(surcharge, plan, base, switches),
    )


def _threshold(
    scenario: Scenario, solver: _Solver, baseline: float, end: float, base: Plan
) -> SurchargeThreshold:
    direction = "up" if end > baseline else "down"
    span = end - baseline
    base_where = locations(base)
    if span == 0:
        return SurchargeThreshold(
            direction=direction,
            searched_from=baseline,
            searched_to=end,
            surcharge_per_kg=None,
            iterations=0,
            switches=[],
        )

    def value(t: float) -> float:
        return round(baseline + (t - 1.0) * span, 3)

    def changed(t: float) -> bool:
        return locations(solver.at(value(t))) != base_where

    t, iterations = bisect_factor(changed, 2.0, tolerance=THRESHOLD_TOLERANCE_KRW / abs(span))
    found = value(t) if t is not None else None
    return SurchargeThreshold(
        direction=direction,
        searched_from=baseline,
        searched_to=end,
        surcharge_per_kg=round(found) if found is not None else None,
        iterations=iterations,
        switches=_switches(scenario, base, solver.at(found)) if found is not None else [],
    )


def fuel_shock(
    scenario: Scenario,
    surcharges: list[float] | None = None,
    baseline_surcharge: float | None = None,
    find_thresholds: bool = True,
    threshold_max: float = DEFAULT_THRESHOLD_MAX,
    plan_id: str | None = None,
) -> FuelShockResult:
    started = time.perf_counter()
    params = scenario.cost_params
    base_freight = params.air_freight_per_kg_krw.value
    baseline = (
        params.air_fuel_surcharge_per_kg_krw.value
        if baseline_surcharge is None
        else baseline_surcharge
    )
    values = list(dict.fromkeys(surcharges if surcharges else DEFAULT_SURCHARGES))
    pallet_kg = _pallet_kg(scenario)
    solver = _Solver(scenario)
    base = solver.at(baseline)
    cases = [
        _case(
            scenario,
            v,
            base_freight,
            pallet_kg,
            solver.at(v),
            base,
            round(v, 3) == round(baseline, 3),
        )
        for v in values
    ]
    thresholds: list[SurchargeThreshold] = []
    if find_thresholds:
        top = max([threshold_max, *values])
        thresholds = [
            _threshold(scenario, solver, baseline, 0.0, base),
            _threshold(scenario, solver, baseline, top, base),
        ]
    return FuelShockResult(
        scenario_id=scenario.id,
        plan_id=plan_id,
        base_freight_per_kg=base_freight,
        baseline_surcharge_per_kg=baseline,
        pallet_kg=pallet_kg,
        sources={
            "air_freight_per_kg_krw": params.air_freight_per_kg_krw.source,
            "air_fuel_surcharge_per_kg_krw": params.air_fuel_surcharge_per_kg_krw.source,
            "surcharge_values": SURCHARGE_SOURCE,
        },
        cases=cases,
        thresholds=thresholds,
        solve_count=solver.count,
        solve_ms=round((time.perf_counter() - started) * 1000),
    )
