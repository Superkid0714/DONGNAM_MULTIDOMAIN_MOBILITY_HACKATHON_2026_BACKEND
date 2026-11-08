import time
from collections import defaultdict
from collections.abc import Iterable, Mapping

from ortools.sat.python import cp_model

from freshswitch_backend.domain.models import Assignment, Lot, Plan, Scenario, UnassignedLot
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.feasibility import (
    SeaOption,
    air_rejection,
    products_compatible,
    sea_option,
)
from freshswitch_backend.optimize.settlement import (
    SettlementResult,
    container_loads,
    shipper_costs_and_settlements,
)

AIR = "AIR"
COST_SCALE = 1000


def solve_plan(
    scenario: Scenario,
    *,
    fixed: Mapping[str, str] | None = None,
    forbidden: Iterable[tuple[str, str]] | None = None,
    forbidden_reasons: Mapping[tuple[str, str], str] | None = None,
    exclude_lot_ids: Iterable[str] | None = None,
    allow_air: bool = True,
    require_savings: bool = True,
    max_pull_forward_days: int = 0,
    time_limit_s: float = 5.0,
    num_workers: int = 1,
    random_seed: int = 0,
) -> Plan:
    started = time.perf_counter()
    fixed = dict(fixed or {})
    forbidden_set = set(forbidden or ())
    forbidden_reasons = dict(forbidden_reasons or {})
    excluded = set(exclude_lot_ids or ())
    model = CostModel(scenario)
    lots = [lot for lot in scenario.lots if lot.id not in excluded]
    lot_by_id = {lot.id: lot for lot in lots}
    containers = scenario.containers
    unknown = [lid for lid in fixed if lid not in lot_by_id]
    if unknown:
        raise ValueError(f"고정 대상 로트가 없음: {unknown}")

    options: dict[str, dict[str, SeaOption]] = {}
    rejections: dict[str, list[str]] = defaultdict(list)
    air_ok: dict[str, bool] = {}
    for lot in lots:
        target = fixed.get(lot.id)
        options[lot.id] = {}
        for container in containers:
            if (lot.id, container.id) in forbidden_set:
                why = forbidden_reasons.get((lot.id, container.id), "배정 금지")
                rejections[lot.id].append(f"{container.id}: {why}")
                continue
            option, reason = sea_option(model, lot, container, max_pull_forward_days)
            if option is None and target == container.id:
                late = (lot.ready_date - model.cutoff_date(model.voyage_of(container))).days
                option = SeaOption(container.id, max(0, late))
            if option is None:
                rejections[lot.id].append(f"{container.id}: {reason}")
            else:
                options[lot.id][container.id] = option
        reason = None if allow_air else "항공 미허용"
        if reason is None and (lot.id, AIR) in forbidden_set:
            reason = forbidden_reasons.get((lot.id, AIR), "항공 배정 금지")
        if reason is None:
            reason = air_rejection(model, lot)
        air_ok[lot.id] = reason is None or target == AIR
        if not air_ok[lot.id]:
            rejections[lot.id].append(f"항공: {reason}")

    cp = cp_model.CpModel()
    x: dict[tuple[str, str], cp_model.IntVar] = {}
    y: dict[str, cp_model.IntVar] = {}
    u: dict[str, cp_model.IntVar] = {}
    z: dict[str, cp_model.IntVar] = {}
    objective: list = []
    tie_break: list = []
    fixed_cost = {c.id: round(model.container_total_fixed_cost(c)) for c in containers}

    for container in containers:
        z[container.id] = cp.new_bool_var(f"z[{container.id}]")
        objective.append(fixed_cost[container.id] * z[container.id])

    variable_cost: dict[tuple[str, str], int] = {}
    tariff_margin: dict[tuple[str, str], int] = {}
    air_cost: dict[str, int] = {}
    n = len(lots)
    container_index = {c.id: i for i, c in enumerate(containers)}
    for l_idx, lot in enumerate(lots):
        air_cost[lot.id] = round(model.air_lot_cost(lot).total)
        choices = []
        for cid, option in options[lot.id].items():
            var = cp.new_bool_var(f"x[{lot.id},{cid}]")
            x[lot.id, cid] = var
            voyage = model.voyage_of(model.containers[cid])
            cost = round(model.sea_lot_cost(lot, voyage, option.pull_forward_days).total)
            variable_cost[lot.id, cid] = cost
            if model.tariff(lot) is not None:
                sea = model.tariff_sea_cost(lot, voyage, option.pull_forward_days).total
                tariff_margin[lot.id, cid] = round(model.tariff_air_cost(lot).total - sea)
            objective.append(cost * var)
            tie_break.append(container_index[cid] * (n - l_idx) * var)
            cp.add_implication(var, z[cid])
            choices.append(var)
        if air_ok[lot.id]:
            y[lot.id] = cp.new_bool_var(f"y[{lot.id}]")
            objective.append(air_cost[lot.id] * y[lot.id])
            choices.append(y[lot.id])
        u[lot.id] = cp.new_bool_var(f"u[{lot.id}]")
        objective.append(round(model.unassigned_penalty(lot)) * u[lot.id])
        choices.append(u[lot.id])
        cp.add_exactly_one(choices)
        target = fixed.get(lot.id)
        if target == AIR:
            cp.add(y[lot.id] == 1)
        elif target is not None:
            if (lot.id, target) not in x:
                raise ValueError(f"{lot.id}를 {target}에 고정할 수 없음")
            cp.add(x[lot.id, target] == 1)

    for container in containers:
        cid = container.id
        members = [(lot, x[lot.id, cid]) for lot in lots if (lot.id, cid) in x]
        load = sum(lot.pallets * var for lot, var in members)
        cp.add(load <= container.capacity_pallets * z[cid])
        if not members:
            cp.add(z[cid] == 0)
            continue
        cp.add(z[cid] <= sum(var for _, var in members))
        has: dict[str, cp_model.IntVar] = {}
        for lot, var in members:
            if lot.product_id not in has:
                has[lot.product_id] = cp.new_bool_var(f"has[{cid},{lot.product_id}]")
            cp.add_implication(var, has[lot.product_id])
        product_ids = sorted(has)
        for i, a in enumerate(product_ids):
            for b in product_ids[i + 1 :]:
                if not products_compatible(model.products[a], model.products[b]):
                    cp.add(has[a] + has[b] <= 1)
        for lot, var in members:
            if lot.id not in fixed and tariff_margin.get((lot.id, cid), 1) <= 0:
                cp.add(var == 0)
        if require_savings:
            for lot, var in members:
                if lot.id in fixed or (lot.id, cid) in tariff_margin:
                    continue
                margin = air_cost[lot.id] - variable_cost[lot.id, cid]
                if margin <= 0:
                    cp.add(var == 0)
                else:
                    cp.add(lot.pallets * fixed_cost[cid] <= margin * load).only_enforce_if(var)

    cp.minimize(COST_SCALE * sum(objective) + sum(tie_break))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = num_workers
    solver.parameters.random_seed = random_seed
    status = solver.solve(cp)
    status_name = solver.status_name(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _empty_plan(scenario, model, lots, status_name, started)

    assignments: list[Assignment] = []
    unassigned: list[UnassignedLot] = []
    for lot in lots:
        chosen = next(
            (cid for cid in options[lot.id] if solver.value(x[lot.id, cid])),
            None,
        )
        if chosen is not None:
            container = model.containers[chosen]
            assignments.append(
                _assignment(
                    lot,
                    "sea",
                    chosen,
                    container.voyage_id,
                    options[lot.id][chosen].pull_forward_days,
                )
            )
        elif lot.id in y and solver.value(y[lot.id]):
            assignments.append(_assignment(lot, "air", None, None, 0))
        else:
            unassigned.append(
                UnassignedLot(
                    lot_id=lot.id,
                    shipper_id=lot.shipper_id,
                    pallets=lot.pallets,
                    reason=_unassigned_reason(lot, options, air_ok, rejections),
                )
            )
    return _build_plan(
        scenario,
        model,
        lot_by_id,
        assignments,
        unassigned,
        status_name,
        round(solver.objective_value / COST_SCALE),
        started,
    )


def _assignment(
    lot: Lot, mode: str, container_id: str | None, voyage_id: str | None, pull: int
) -> Assignment:
    return Assignment(
        lot_id=lot.id,
        shipper_id=lot.shipper_id,
        product_id=lot.product_id,
        pallets=lot.pallets,
        mode=mode,
        container_id=container_id,
        voyage_id=voyage_id,
        pull_forward_days=pull,
    )


def _unassigned_reason(
    lot: Lot,
    options: dict[str, dict[str, SeaOption]],
    air_ok: dict[str, bool],
    rejections: dict[str, list[str]],
) -> str:
    if options[lot.id] or air_ok[lot.id]:
        return "용량·혼적 제약 또는 절감 조건(해상 총비용이 항공보다 큼)으로 배정되지 않음"
    return "; ".join(rejections[lot.id]) or "배정 가능한 수단 없음"


def _build_plan(
    scenario: Scenario,
    model: CostModel,
    lot_by_id: dict[str, Lot],
    assignments: list[Assignment],
    unassigned: list[UnassignedLot],
    status: str,
    objective: int,
    started: float,
) -> Plan:
    result = shipper_costs_and_settlements(model, lot_by_id, assignments)
    return Plan(
        scenario_id=scenario.id,
        status=status,
        objective=objective,
        solve_ms=round((time.perf_counter() - started) * 1000),
        assignments=assignments,
        unassigned=unassigned,
        containers=container_loads(model, assignments),
        **_settlement_fields(result),
    )


def _settlement_fields(result: SettlementResult) -> dict:
    settlements = result.settlements
    return {
        "shipper_costs": result.costs,
        "settlements": settlements,
        "total_baseline_cost": sum(s.baseline_cost for s in settlements),
        "total_shared_cost": sum(s.shared_cost for s in settlements),
        "total_savings": sum(s.savings for s in settlements),
        "total_fee": sum(s.fee for s in settlements),
        "total_operational_baseline_cost": result.operational_baseline,
        "total_operational_cost": result.operational_cost,
        "total_operational_savings": result.operational_baseline - result.operational_cost,
        "container_settlements": result.containers,
        "settlement_tariffs": result.tariffs,
        "total_collection_estimate": result.collection_estimate,
    }


def resettle_plan(
    scenario: Scenario, plan: Plan, route_collection: Mapping[str, int], **updates
) -> Plan:
    model = CostModel(scenario)
    lot_by_id = {lot.id: lot for lot in scenario.lots}
    result = shipper_costs_and_settlements(model, lot_by_id, plan.assignments, route_collection)
    return plan.model_copy(
        update={
            **_settlement_fields(result),
            "collection_basis": "route",
            "total_collection_route": result.collection_route,
            **updates,
        }
    )


def _empty_plan(
    scenario: Scenario, model: CostModel, lots: list[Lot], status: str, started: float
) -> Plan:
    unassigned = [
        UnassignedLot(
            lot_id=lot.id,
            shipper_id=lot.shipper_id,
            pallets=lot.pallets,
            reason=f"해 없음({status}): 고정 배정이 제약과 충돌",
        )
        for lot in lots
    ]
    return _build_plan(scenario, model, {}, [], unassigned, status, 0, started)
