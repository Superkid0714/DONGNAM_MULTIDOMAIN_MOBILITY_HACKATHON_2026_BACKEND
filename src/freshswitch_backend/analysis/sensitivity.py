import time
from collections.abc import Callable
from typing import Literal

from freshswitch_backend.analysis.common import (
    PROPOSAL_EVENT,
    REPLAN_LIMIT_S,
    container_fill,
    proposal_event_applies,
    routes,
    shipper_savings,
    solve,
    solve_routed,
)
from freshswitch_backend.analysis.models import (
    AlternativeSummary,
    LotChange,
    ParamRange,
    ParamSensitivity,
    ReplanSensitivity,
    SensitivityCase,
    SensitivityResult,
    Threshold,
    TornadoBar,
)
from freshswitch_backend.analysis.params import SPEC_BY_KEY, SPECS, ParamSpec, anchor_value, scaled
from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.replan import ReplanResult, replan

UP_LIMIT = 5.0
DOWN_LIMIT = 0.0
FACTOR_TOLERANCE = 0.005
MAX_ITERATIONS = 30
REPLAN_THRESHOLD_PARAMS: tuple[tuple[str, Literal["down", "up"]], ...] = (
    ("early_harvest_loss_rate_per_day", "up"),
    ("loss_rate_per_wait_day", "up"),
    ("sea_ancillary_per_container_krw", "up"),
    ("sea_freight_per_container_krw", "up"),
    ("air_freight_per_kg_krw", "down"),
)


class _Counter:
    def __init__(self) -> None:
        self.count = 0

    def solve(self, scenario: Scenario) -> Plan:
        self.count += 1
        return solve(scenario)

    def replan(self, scenario: Scenario) -> tuple[Plan, ReplanResult]:
        plan = self.solve(scenario)
        self.count += 1
        return plan, replan(scenario, plan, PROPOSAL_EVENT, time_limit_s=REPLAN_LIMIT_S)


def bisect_factor(
    changed: Callable[[float], bool],
    end: float,
    tolerance: float = FACTOR_TOLERANCE,
    max_iterations: int = MAX_ITERATIONS,
) -> tuple[float | None, int]:
    if not changed(end):
        return None, 1
    lo, hi = 1.0, end
    iterations = 1
    while abs(hi - lo) > tolerance and iterations < max_iterations:
        mid = (lo + hi) / 2
        iterations += 1
        if changed(mid):
            hi = mid
        else:
            lo = mid
    return hi, iterations


def _changes(base: dict[str, str], case: dict[str, str]) -> list[LotChange]:
    return [
        LotChange(lot_id=lot_id, base=base.get(lot_id, "-"), case=case.get(lot_id, "-"))
        for lot_id in sorted(set(base) | set(case))
        if base.get(lot_id) != case.get(lot_id)
    ]


def _case(
    spec: ParamSpec, base_value: float, factor: float, base: Plan, plan: Plan
) -> SensitivityCase:
    changes = _changes(routes(base), routes(plan))
    return SensitivityCase(
        value=base_value * factor,
        factor=round(factor, 4),
        status=plan.status,
        total_savings=plan.total_savings,
        savings_delta=plan.total_savings - base.total_savings,
        total_fee=plan.total_fee,
        containers_used=sum(1 for c in plan.containers if c.used),
        shipper_savings=shipper_savings(plan),
        mode_changed=bool(changes),
        changed_lots=changes,
    )


def _param_range(spec: ParamSpec, base_value: float) -> ParamRange:
    return ParamRange(
        key=spec.key,
        label=spec.label,
        unit=spec.unit,
        base=base_value,
        low=base_value * spec.low_factor,
        high=base_value * spec.high_factor,
        source=spec.source,
    )


def _sweep(
    scenario: Scenario, base: Plan, counter: _Counter
) -> tuple[list[ParamSensitivity], list[TornadoBar]]:
    params: list[ParamSensitivity] = []
    for spec in SPECS:
        base_value = anchor_value(scenario, spec)
        low_plan = counter.solve(scaled(scenario, spec, spec.low_factor))
        high_plan = counter.solve(scaled(scenario, spec, spec.high_factor))
        low = _case(spec, base_value, spec.low_factor, base, low_plan)
        high = _case(spec, base_value, spec.high_factor, base, high_plan)
        low_worse = (low.total_savings, low.total_fee) <= (high.total_savings, high.total_fee)
        params.append(
            ParamSensitivity(
                param=_param_range(spec, base_value),
                low=low,
                high=high,
                conservative="low" if low_worse else "high",
                optimistic="high" if low_worse else "low",
                savings_impact=abs(high.total_savings - low.total_savings),
                fee_impact=abs(high.total_fee - low.total_fee),
            )
        )
    tornado = [
        TornadoBar(
            key=p.param.key,
            label=p.param.label,
            base_savings=base.total_savings,
            savings_low=p.low.total_savings,
            savings_high=p.high.total_savings,
            savings_impact=p.savings_impact,
            fee_low=p.low.total_fee,
            fee_high=p.high.total_fee,
        )
        for p in params
    ]
    tornado.sort(key=lambda bar: (-bar.savings_impact, -abs(bar.fee_high - bar.fee_low), bar.key))
    return params, tornado


def _describe_routes(changes: list[LotChange]) -> str:
    return ", ".join(f"{c.lot_id} {c.base}→{c.case}" for c in changes) or "변경 없음"


def _plan_thresholds(scenario: Scenario, base: Plan, counter: _Counter) -> list[Threshold]:
    base_routes = routes(base)
    thresholds: list[Threshold] = []
    for spec in SPECS:
        if not spec.affects_plan:
            continue
        base_value = anchor_value(scenario, spec)
        for direction, end in (("down", DOWN_LIMIT), ("up", UP_LIMIT)):
            cache: dict[float, Plan] = {}

            def changed(factor: float, spec: ParamSpec = spec, cache: dict = cache) -> bool:
                cache[factor] = counter.solve(scaled(scenario, spec, factor))
                return routes(cache[factor]) != base_routes

            factor, iterations = bisect_factor(changed, end)
            changes = _changes(base_routes, routes(cache[factor])) if factor is not None else []
            thresholds.append(
                Threshold(
                    key=spec.key,
                    label=spec.label,
                    unit=spec.unit,
                    direction=direction,
                    base=base_value,
                    threshold=base_value * factor if factor is not None else None,
                    factor=round(factor, 4) if factor is not None else None,
                    searched_to=base_value * end,
                    iterations=iterations,
                    base_outcome="기준 배정",
                    flipped_outcome=_describe_routes(changes) if factor is not None else None,
                    changed_lots=changes,
                )
            )
    return thresholds


def replan_outcome(result: ReplanResult) -> str:
    chosen = next((a for a in result.alternatives if a.id == result.recommended_id), None)
    if chosen is None:
        return "none"
    if chosen.id != "pull_forward":
        return chosen.id
    if any(a.pull_forward_days for a in chosen.plan.assignments):
        return "pull_forward"
    as_is = next((a for a in result.alternatives if a.id == "sail_as_is"), None)
    if as_is is not None and routes(as_is.plan) == routes(chosen.plan):
        return "sail_as_is"
    return "reassign"


def recommended_routes(result: ReplanResult) -> dict[str, str]:
    chosen = next((a for a in result.alternatives if a.id == result.recommended_id), None)
    return routes(chosen.plan) if chosen is not None else {}


def _alternatives(result: ReplanResult) -> list[AlternativeSummary]:
    return [
        AlternativeSummary(
            id=a.id,
            label=a.label,
            feasible=a.feasible,
            total_cost=a.total_cost,
            total_savings=a.plan.total_savings,
            total_fee=a.plan.total_fee,
            container_fill=container_fill(a.plan),
        )
        for a in result.alternatives
    ]


def _replan_sensitivity(scenario: Scenario, counter: _Counter) -> ReplanSensitivity | None:
    if not proposal_event_applies(scenario):
        return None
    _, base_result = counter.replan(scenario)
    base_outcome = replan_outcome(base_result)
    base_routes = recommended_routes(base_result)
    thresholds: list[Threshold] = []
    for key, direction in REPLAN_THRESHOLD_PARAMS:
        spec = SPEC_BY_KEY[key]
        base_value = anchor_value(scenario, spec)
        end = UP_LIMIT if direction == "up" else DOWN_LIMIT
        outcomes: dict[float, ReplanResult] = {}

        def changed(factor: float, spec: ParamSpec = spec, outcomes: dict = outcomes) -> bool:
            _, outcomes[factor] = counter.replan(scaled(scenario, spec, factor))
            return replan_outcome(outcomes[factor]) != base_outcome

        factor, iterations = bisect_factor(changed, end)
        flipped = outcomes[factor] if factor is not None else None
        thresholds.append(
            Threshold(
                key=spec.key,
                label=spec.label,
                unit=spec.unit,
                direction=direction,
                base=base_value,
                threshold=base_value * factor if factor is not None else None,
                factor=round(factor, 4) if factor is not None else None,
                searched_to=base_value * end,
                iterations=iterations,
                base_outcome=base_outcome,
                flipped_outcome=replan_outcome(flipped) if flipped is not None else None,
                changed_lots=_changes(base_routes, recommended_routes(flipped)) if flipped else [],
            )
        )
    return ReplanSensitivity(
        event=PROPOSAL_EVENT.model_dump(),
        base_outcome=base_outcome,
        alternatives=_alternatives(base_result),
        thresholds=thresholds,
    )


def sensitivity(scenario: Scenario, plan_id: str | None = None) -> SensitivityResult:
    started = time.perf_counter()
    counter = _Counter()
    base = counter.solve(scenario)
    params, tornado = _sweep(scenario, base, counter)
    thresholds = _plan_thresholds(scenario, base, counter)
    replan_part = _replan_sensitivity(scenario, counter)
    routed = solve_routed(scenario)
    return SensitivityResult(
        scenario_id=scenario.id,
        plan_id=plan_id,
        base_total_savings=base.total_savings,
        base_total_fee=base.total_fee,
        base_shipper_savings=shipper_savings(base),
        base_routes=routes(base),
        params=params,
        tornado=tornado,
        thresholds=thresholds,
        replan=replan_part,
        route_base_total_savings=routed.total_savings,
        route_base_total_fee=routed.total_fee,
        solve_count=counter.count,
        solve_ms=round((time.perf_counter() - started) * 1000),
    )
