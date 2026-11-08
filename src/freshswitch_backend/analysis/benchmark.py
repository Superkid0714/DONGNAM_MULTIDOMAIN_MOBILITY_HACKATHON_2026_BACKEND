import math
import time
from collections import defaultdict

from freshswitch_backend.analysis.common import (
    PROPOSAL_EVENT,
    REPLAN_LIMIT_S,
    container_fill,
    locations,
    proposal_event_applies,
    route_costed,
    solve,
    solve_routed,
)
from freshswitch_backend.analysis.models import (
    BenchmarkResult,
    MethodResult,
    ReplanBenchmark,
    ReplanBenchmarkRow,
)
from freshswitch_backend.domain.models import Container, Lot, Plan, Scenario
from freshswitch_backend.optimize import AIR
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.feasibility import air_rejection, products_compatible, sea_option
from freshswitch_backend.planning.service import CUTOFF_REASON
from freshswitch_backend.replan import replan

Part = tuple[Scenario, Plan, list[str]]

METHODS = {
    "optimizer": (
        "최적화(CP-SAT)",
        "전 로트·전 컨테이너를 한 번에 풀어 총비용 최소. 화주별 절감 보장(해상 몫 ≤ 항공) 제약 포함",
    ),
    "fcfs": (
        "수작업 ① 선착순 채우기",
        (
            "출하일 순으로 ETD가 빠른 첫 컨테이너 중 자리·품목 궁합·마감이 맞는 곳에 싣는다. "
            "당겨 싣기 없음, 어디에도 안 맞을 때만 항공"
        ),
    ),
    "greedy": (
        "수작업 ② 건별 최저비용",
        (
            "출하일 순으로 로트마다 '이미 연 컨테이너 합류 / 새 컨테이너 / 항공' 중 그 시점 총비용이 "
            "가장 낮은 쪽을 고른다. 뒤에 올 로트는 모른다(근시안 견적)"
        ),
    ),
    "solo": (
        "수작업 ③ 화주 단독",
        "화주마다 전용 컨테이너(항차별)와 항공 중 싼 쪽. 합적 없음. 컨테이너 수 제한 없이 가정",
    ),
}


def _arrival_order(scenario: Scenario) -> list[Lot]:
    index = {lot.id: i for i, lot in enumerate(scenario.lots)}
    return sorted(scenario.lots, key=lambda lot: (lot.ready_date, index[lot.id]))


def _containers_by_etd(scenario: Scenario) -> list[Container]:
    etd = {v.id: v.etd for v in scenario.voyages}
    index = {c.id: i for i, c in enumerate(scenario.containers)}
    return sorted(scenario.containers, key=lambda c: (etd[c.voyage_id], index[c.id]))


class _Loader:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.model = CostModel(scenario)
        self.containers = _containers_by_etd(scenario)
        self.load: dict[str, int] = defaultdict(int)
        self.products: dict[str, set[str]] = defaultdict(set)
        self.placed: dict[str, str] = {}
        self.unassigned: list[str] = []

    def sea_candidates(self, lot: Lot) -> list[str]:
        product = self.model.product(lot)
        result = []
        for container in self.containers:
            option, _ = sea_option(self.model, lot, container, 0)
            if option is None:
                continue
            if self.load[container.id] + lot.pallets > container.capacity_pallets:
                continue
            if any(
                not products_compatible(self.model.products[p], product)
                for p in self.products[container.id]
            ):
                continue
            result.append(container.id)
        return result

    def air_ok(self, lot: Lot) -> bool:
        return air_rejection(self.model, lot) is None

    def place(self, lot: Lot, target: str | None) -> None:
        if target is None:
            self.unassigned.append(lot.id)
            return
        self.placed[lot.id] = target
        if target != AIR:
            self.load[target] += lot.pallets
            self.products[target].add(lot.product_id)

    def plan(self) -> Plan:
        forbidden = [(lid, c.id) for lid in self.unassigned for c in self.scenario.containers]
        forbidden += [(lid, AIR) for lid in self.unassigned]
        return solve(self.scenario, fixed=self.placed, forbidden=forbidden)


def fcfs_plan(scenario: Scenario) -> Plan:
    loader = _Loader(scenario)
    for lot in _arrival_order(scenario):
        sea = loader.sea_candidates(lot)
        if sea:
            loader.place(lot, sea[0])
        else:
            loader.place(lot, AIR if loader.air_ok(lot) else None)
    return loader.plan()


def greedy_plan(scenario: Scenario) -> Plan:
    loader = _Loader(scenario)
    order = _arrival_order(scenario)
    for i, lot in enumerate(order):
        options = loader.sea_candidates(lot) + ([AIR] if loader.air_ok(lot) else [])
        later = [x.id for x in order[i + 1 :]] + loader.unassigned
        best: tuple[int, str] | None = None
        for option in options:
            trial = solve(scenario, fixed={**loader.placed, lot.id: option}, exclude_lot_ids=later)
            if trial.unassigned:
                continue
            if best is None or trial.total_operational_cost < best[0]:
                best = (trial.total_operational_cost, option)
        loader.place(lot, best[1] if best else None)
    return loader.plan()


def _solo_scenario(scenario: Scenario, shipper_id: str) -> Scenario:
    lots = [lot for lot in scenario.lots if lot.shipper_id == shipper_id]
    total = sum(lot.pallets for lot in lots)
    containers = []
    for voyage in scenario.voyages:
        template = next((c for c in scenario.containers if c.voyage_id == voyage.id), None)
        if template is None:
            continue
        count = max(1, math.ceil(total / template.capacity_pallets))
        containers += [
            template.model_copy(update={"id": f"{shipper_id}/{voyage.id}/{i + 1}"})
            for i in range(count)
        ]
    return scenario.model_copy(update={"lots": lots, "containers": containers})


def solo_plans(scenario: Scenario) -> list[tuple[Scenario, Plan]]:
    result = []
    for shipper in scenario.shippers:
        sub = _solo_scenario(scenario, shipper.id)
        if sub.lots:
            result.append((sub, solve(sub)))
    return result


def routed_part(scenario: Scenario, plan: Plan) -> Part:
    costed, dropped = route_costed(scenario, plan)
    return scenario, costed, dropped


def violations(scenario: Scenario, plan: Plan) -> list[str]:
    model = CostModel(scenario)
    lots = {lot.id: lot for lot in scenario.lots}
    found: list[str] = []
    members: dict[str, list[Lot]] = defaultdict(list)
    for a in plan.assignments:
        lot = lots[a.lot_id]
        if a.mode == "air":
            reason = air_rejection(model, lot)
            if reason:
                found.append(f"{lot.id}: {reason}")
            continue
        container = model.containers.get(a.container_id)
        if container is None:
            found.append(f"{lot.id}: 없는 컨테이너 {a.container_id}")
            continue
        option, reason = sea_option(model, lot, container, a.pull_forward_days)
        if option is None:
            found.append(f"{lot.id}@{container.id}: {reason}")
        members[container.id].append(lot)
    for cid, items in members.items():
        container = model.containers[cid]
        pallets = sum(lot.pallets for lot in items)
        if pallets > container.capacity_pallets:
            found.append(f"{cid}: 용량 초과 {pallets}/{container.capacity_pallets}")
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                if not products_compatible(model.product(a), model.product(b)):
                    found.append(f"{cid}: 혼적 불가 {a.id}+{b.id}")
    return found


def _result(method: str, parts: list[Part], elapsed_ms: int) -> MethodResult:
    label, description = METHODS[method]
    used = [c for _, plan, _ in parts for c in plan.containers if c.used]
    capacity = sum(c.capacity_pallets for c in used)
    pallets = sum(c.pallets for c in used)
    assignments = [a for _, plan, _ in parts for a in plan.assignments]
    unassigned = [u for _, plan, _ in parts for u in plan.unassigned]
    settlements = [s for _, plan, _ in parts for s in plan.settlements]
    found = [v for scenario, plan, _ in parts for v in violations(scenario, plan)]
    found += [f"{lid}: {CUTOFF_REASON}" for _, _, dropped in parts for lid in dropped]
    where: dict[str, str] = {}
    for _, plan, _ in parts:
        where.update(locations(plan))
    return MethodResult(
        method=method,
        label=label,
        description=description,
        containers_used=len(used),
        avg_fill=round(pallets / capacity, 4) if capacity else 0.0,
        sea_pallets=sum(a.pallets for a in assignments if a.mode == "sea"),
        air_pallets=sum(a.pallets for a in assignments if a.mode == "air"),
        unassigned_lots=len(unassigned),
        unassigned_pallets=sum(u.pallets for u in unassigned),
        total_baseline_cost=sum(p.total_baseline_cost for _, p, _ in parts),
        total_shipper_cost=sum(p.total_shared_cost for _, p, _ in parts),
        total_savings=sum(p.total_savings for _, p, _ in parts),
        total_fee=sum(p.total_fee for _, p, _ in parts),
        total_operational_baseline_cost=sum(p.total_operational_baseline_cost for _, p, _ in parts),
        total_operational_cost=sum(p.total_operational_cost for _, p, _ in parts),
        total_operational_savings=sum(p.total_operational_savings for _, p, _ in parts),
        total_collection_estimate=sum(p.total_collection_estimate or 0 for _, p, _ in parts),
        total_collection_route=sum(p.total_collection_route or 0 for _, p, _ in parts),
        shippers_with_loss=sum(1 for s in settlements if s.savings < 0),
        constraint_violations=len(found),
        violation_details=found,
        solve_ms=elapsed_ms,
        locations=dict(sorted(where.items())),
    )


def _timed(fn):
    started = time.perf_counter()
    value = fn()
    return value, round((time.perf_counter() - started) * 1000)


def compare_methods(scenario: Scenario) -> list[MethodResult]:
    optimizer, opt_ms = _timed(lambda: [(scenario, solve_routed(scenario), [])])
    fcfs, fcfs_ms = _timed(lambda: [routed_part(scenario, fcfs_plan(scenario))])
    greedy, greedy_ms = _timed(lambda: [routed_part(scenario, greedy_plan(scenario))])
    solo, solo_ms = _timed(lambda: [routed_part(sub, plan) for sub, plan in solo_plans(scenario)])
    return [
        _result("optimizer", optimizer, opt_ms),
        _result("fcfs", fcfs, fcfs_ms),
        _result("greedy", greedy, greedy_ms),
        _result("solo", solo, solo_ms),
    ]


def replan_benchmark(scenario: Scenario) -> ReplanBenchmark | None:
    if not proposal_event_applies(scenario):
        return None
    base = solve(scenario)
    result = replan(scenario, base, PROPOSAL_EVENT, time_limit_s=REPLAN_LIMIT_S)
    manual = next(a for a in result.alternatives if a.id == "sail_as_is")
    rows = [
        ReplanBenchmarkRow(
            id=a.id,
            label=a.label,
            role="manual"
            if a.id == "sail_as_is"
            else "recommended"
            if a.id == result.recommended_id
            else "other",
            feasible=a.feasible,
            total_cost=a.total_cost,
            cost_vs_manual=a.total_cost - manual.total_cost,
            total_savings=a.plan.total_savings,
            total_fee=a.plan.total_fee,
            container_fill=container_fill(a.plan),
            summary=a.summary,
        )
        for a in result.alternatives
    ]
    chosen = next((a for a in result.alternatives if a.id == result.recommended_id), manual)
    return ReplanBenchmark(
        event=PROPOSAL_EVENT.model_dump(),
        recommended_id=result.recommended_id,
        rows=rows,
        improvement_vs_manual=manual.total_cost - chosen.total_cost,
    )


def benchmark(scenario: Scenario) -> BenchmarkResult:
    started = time.perf_counter()
    methods = compare_methods(scenario)
    replan_part = replan_benchmark(scenario)
    return BenchmarkResult(
        scenario_id=scenario.id,
        methods=methods,
        replan=replan_part,
        solve_ms=round((time.perf_counter() - started) * 1000),
    )
