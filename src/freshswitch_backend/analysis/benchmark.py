import math
import time
from collections import defaultdict
from collections.abc import Iterable

from freshswitch_backend.analysis.common import (
    REPLAN_LIMIT_S,
    container_fill,
    locations,
    proposal_event,
    route_costed,
    solve,
    solve_routed,
)
from freshswitch_backend.analysis.models import (
    BenchmarkBasis,
    BenchmarkResult,
    MethodBasisGap,
    MethodResult,
    ReplanBenchmark,
    ReplanBenchmarkRow,
)
from freshswitch_backend.domain.models import Container, Lot, Plan, Scenario
from freshswitch_backend.optimize import AIR
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.feasibility import air_rejection, products_compatible, sea_option
from freshswitch_backend.optimize.settlement import lot_settle_cost
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


def _method_parts(
    scenario: Scenario, plan: Plan | None = None
) -> list[tuple[str, list[Part], int]]:
    if plan is None:
        optimizer, opt_ms = _timed(lambda: [(scenario, solve_routed(scenario), [])])
    else:
        optimizer, opt_ms = [(scenario, plan, [])], plan.solve_ms
    fcfs, fcfs_ms = _timed(lambda: [routed_part(scenario, fcfs_plan(scenario))])
    greedy, greedy_ms = _timed(lambda: [routed_part(scenario, greedy_plan(scenario))])
    solo, solo_ms = _timed(lambda: [routed_part(sub, plan) for sub, plan in solo_plans(scenario)])
    return [
        ("optimizer", optimizer, opt_ms),
        ("fcfs", fcfs, fcfs_ms),
        ("greedy", greedy, greedy_ms),
        ("solo", solo, solo_ms),
    ]


def compare_methods(scenario: Scenario, plan: Plan | None = None) -> list[MethodResult]:
    return [_result(method, parts, ms) for method, parts, ms in _method_parts(scenario, plan)]


def empty_slot_cost(plan: Plan) -> int:
    return sum(
        round(c.container_cost * (c.capacity_pallets - c.pallets) / c.capacity_pallets)
        for c in plan.containers
        if c.used and c.capacity_pallets
    )


def tariff_empty_slot_cost(plan: Plan) -> int:
    tariff_products = {t.product_id for t in plan.settlement_tariffs}
    tariff_pallets: defaultdict[str, int] = defaultdict(int)
    for a in plan.assignments:
        if a.container_id is not None and a.product_id in tariff_products:
            tariff_pallets[a.container_id] += a.pallets
    return sum(
        round(
            c.container_cost
            * (c.capacity_pallets - c.pallets)
            / c.capacity_pallets
            * tariff_pallets[c.container_id]
            / c.pallets
        )
        for c in plan.containers
        if c.used and c.capacity_pallets and c.pallets and tariff_pallets[c.container_id]
    )


def _mixed_tariff_rows(scenario: Scenario, plan: Plan) -> tuple[int, int, int]:
    mixed = {s.shipper_id for s in plan.settlements if s.basis == "mixed"}
    if not mixed:
        return 0, 0, 0
    model = CostModel(scenario)
    lots = {lot.id: lot for lot in scenario.lots}
    container_pallets: defaultdict[str, int] = defaultdict(int)
    for a in plan.assignments:
        if a.mode == "sea" and a.container_id:
            container_pallets[a.container_id] += a.pallets
    pallets = sea_pallets = 0
    savings = 0.0
    for a in plan.assignments:
        lot = lots.get(a.lot_id)
        if a.shipper_id not in mixed or lot is None or model.tariff(lot) is None:
            continue
        pallets += lot.pallets
        sea_pallets += lot.pallets if a.mode == "sea" else 0
        settled = lot_settle_cost(model, lot, a, container_pallets)
        savings += model.settle_air_cost(lot).total - settled.total
    return pallets, sea_pallets, round(savings)


def _basis_gap(result: MethodResult, parts: list[Part]) -> MethodBasisGap:
    tariffed = [s for _, plan, _ in parts for s in plan.settlements if s.basis == "tariff"]
    mixed = [_mixed_tariff_rows(scenario, plan) for scenario, plan, _ in parts]
    pallets = sum(s.sea_pallets + s.air_pallets for s in tariffed) + sum(m[0] for m in mixed)
    sea_pallets = sum(s.sea_pallets for s in tariffed) + sum(m[1] for m in mixed)
    tariff_savings = sum(s.savings for s in tariffed) + sum(m[2] for m in mixed)
    cost_gap = result.total_shipper_cost - result.total_operational_cost
    return MethodBasisGap(
        method=result.method,
        settlement_savings=result.total_savings,
        operational_savings=result.total_operational_savings,
        savings_gap=result.total_savings - result.total_operational_savings,
        baseline_gap=result.total_baseline_cost - result.total_operational_baseline_cost,
        settlement_cost_gap=cost_gap,
        tariff_pallets=pallets,
        tariff_sea_pallets=sea_pallets,
        settlement_cost_gap_per_tariff_pallet=round(cost_gap / pallets) if pallets else None,
        empty_slot_cost=sum(empty_slot_cost(plan) for _, plan, _ in parts),
        tariff_settlement_savings=tariff_savings,
        tariff_empty_slot_cost=sum(tariff_empty_slot_cost(plan) for _, plan, _ in parts),
    )


def _tariff_names(scenario: Scenario, plan: Plan) -> list[str]:
    names = {p.id: p.name for p in scenario.products}
    return [names.get(t.product_id, t.product_id) for t in plan.settlement_tariffs]


def _bases(scenario: Scenario, plan: Plan) -> list[BenchmarkBasis]:
    names = ", ".join(_tariff_names(scenario, plan)) or "없음"
    assumed = sorted({item for t in plan.settlement_tariffs for item in t.assumed_items})
    return [
        BenchmarkBasis(
            key="operational",
            label="운영비 기준",
            fields=[
                "total_operational_baseline_cost",
                "total_operational_cost",
                "total_operational_savings",
            ],
            definition=(
                "실제로 드는 비용. 쓴 컨테이너 비용 전부(빈칸 포함)를 실린 로트에 파렛트 비례로 "
                "나누고 집하 경로비·보관·품질손실 위험을 더한다. 기준선은 전 로트 항공. "
                "방법 비교 화면은 이 기준을 쓴다"
            ),
            source="시나리오 cost_params(항목별 source, '가정' 표시 항목 포함)",
        ),
        BenchmarkBasis(
            key="settlement",
            label="정산 기준",
            fields=[
                "total_baseline_cost",
                "total_shipper_cost",
                "total_savings",
                "total_fee",
                "shippers_with_loss",
            ],
            definition=(
                f"화주에게 청구하는 값. 정산표 품목({names})은 파렛트당 정액(항공·해상 단가)으로 "
                "매기고 나머지 품목은 운영비를 그대로 쓴다. 정산표 단가에는 컨테이너가 얼마나 "
                "찼는지가 들어가지 않는다. 수수료는 이 기준 절감으로 매긴다"
            ),
            source=(
                "계획 settlement_tariffs(항목별 lines[].air_source·sea_source)"
                + (f". 가정 항목: {', '.join(assumed)}" if assumed else "")
            ),
        ),
    ]


def _tariff_phrase(scenario: Scenario, plan: Plan) -> str:
    sheets = zip(_tariff_names(scenario, plan), plan.settlement_tariffs, strict=True)
    return ", ".join(
        f"{name} 항공 {t.air_total:,}원 − 해상 {t.sea_total:,}원 = {t.savings_per_pallet:,}원"
        for name, t in sheets
    )


def _cost_gap_phrase(gap: MethodBasisGap) -> str:
    if gap.settlement_cost_gap_per_tariff_pallet is None:
        return f"{gap.settlement_cost_gap:,}원"
    return (
        f"{gap.settlement_cost_gap:,}원(정산표 화주 {gap.tariff_pallets}파렛트, 파렛트당 "
        f"{gap.settlement_cost_gap_per_tariff_pallet:,}원)"
    )


def _savings_verdict(opt: MethodBasisGap, fcfs: MethodBasisGap) -> str:
    diff = opt.operational_savings - fcfs.operational_savings
    if diff > 0:
        return f"최적화가 {diff:,}원 더 아낀다"
    if diff < 0:
        return f"선착순이 {-diff:,}원 더 아낀다"
    return "두 방법이 같다"


def _tariff_share_phrase(opt: MethodBasisGap, fcfs: MethodBasisGap) -> str:
    d_tariff = fcfs.tariff_settlement_savings - opt.tariff_settlement_savings
    d_sea = fcfs.tariff_sea_pallets - opt.tariff_sea_pallets
    if d_tariff > 0 and d_sea > 0:
        return (
            f"선착순은 정산표 화주 해상 파렛트가 {d_sea}개 더 많아"
            f"({fcfs.tariff_sea_pallets} vs {opt.tariff_sea_pallets}) 정산표 몫 정산 절감이 "
            f"{d_tariff:,}원 늘지만, "
        )
    return (
        f"정산표 화주 정산 절감은 선착순 {fcfs.tariff_settlement_savings:,}원, 최적화 "
        f"{opt.tariff_settlement_savings:,}원이고, "
    )


def _rest_phrase(opt: MethodBasisGap, fcfs: MethodBasisGap) -> str:
    d_total = fcfs.settlement_savings - opt.settlement_savings
    d_rest = d_total - (fcfs.tariff_settlement_savings - opt.tariff_settlement_savings)
    if d_rest > 0:
        return (
            f"나머지 {d_rest:,}원은 정산표 밖 품목(운영비 정산)·고정 배정 차이에서 선착순이 "
            "더 아낀 몫이다. "
        )
    if d_rest < 0:
        return (
            f"정산표 밖 품목(운영비 정산)·고정 배정 차이에서는 최적화가 {-d_rest:,}원 더 아낀다. "
        )
    return ""


def _empty_slot_phrase(opt: MethodBasisGap, fcfs: MethodBasisGap) -> str:
    diff = fcfs.tariff_empty_slot_cost - opt.tariff_empty_slot_cost
    if diff > 0:
        compare = f"선착순이 {diff:,}원 크고"
    elif diff < 0:
        compare = f"최적화가 {-diff:,}원 크고"
    else:
        compare = "같고"
    return (
        f"쓴 컨테이너 빈칸 비용(컨테이너 비용 × 빈 파렛트 ÷ 용량)은 선착순 "
        f"{fcfs.empty_slot_cost:,}원, 최적화 {opt.empty_slot_cost:,}원이다. 그중 정산표 품목 "
        f"파렛트 몫(빈칸 비용 × 정산표 품목 파렛트 ÷ 실린 파렛트)은 선착순 "
        f"{fcfs.tariff_empty_slot_cost:,}원, 최적화 {opt.tariff_empty_slot_cost:,}원으로 "
        f"{compare} 이 몫은 정산 단가에 잡히지 않는다. "
    )


def settlement_fcfs_explanation(scenario: Scenario, plan: Plan, gaps: list[MethodBasisGap]) -> str:
    by = {g.method: g for g in gaps}
    opt, fcfs = by["optimizer"], by["fcfs"]
    tail = (
        f"화주 정산액 − 운영비(음수면 플랫폼이 떠안는 몫)는 선착순 {_cost_gap_phrase(fcfs)}, "
        f"최적화 {_cost_gap_phrase(opt)}이다. 운영비 기준 절감은 최적화 {opt.operational_savings:,}원, "
        f"선착순 {fcfs.operational_savings:,}원으로 {_savings_verdict(opt, fcfs)}"
    )
    if fcfs.settlement_savings <= opt.settlement_savings:
        return (
            f"이 계획에서는 정산 기준에서도 선착순 절감({fcfs.settlement_savings:,}원)이 "
            f"최적화({opt.settlement_savings:,}원)보다 크지 않다. {tail}"
        )
    tariff = _tariff_phrase(scenario, plan)
    unit = f"파렛트당 정액({tariff})" if tariff else "파렛트당 정액"
    return (
        f"정산 기준 절감은 선착순 {fcfs.settlement_savings:,}원이 최적화 "
        f"{opt.settlement_savings:,}원보다 {fcfs.settlement_savings - opt.settlement_savings:,}원 "
        f"크다. 정산표는 {unit}으로 매겨 컨테이너가 얼마나 찼는지 보지 않는다. "
        f"{_tariff_share_phrase(opt, fcfs)}{_empty_slot_phrase(opt, fcfs)}{_rest_phrase(opt, fcfs)}"
        f"{tail}"
    )


def replan_benchmark(
    scenario: Scenario, plan: Plan | None = None, air_locked: Iterable[str] = ()
) -> ReplanBenchmark | None:
    event = proposal_event(scenario)
    if event is None:
        return None
    base = plan if plan is not None else solve(scenario)
    result = replan(scenario, base, event, air_locked=air_locked, time_limit_s=REPLAN_LIMIT_S)
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
        event=event.model_dump(),
        recommended_id=result.recommended_id,
        rows=rows,
        improvement_vs_manual=manual.total_cost - chosen.total_cost,
    )


def benchmark(
    scenario: Scenario, plan: Plan | None = None, air_locked: Iterable[str] = ()
) -> BenchmarkResult:
    started = time.perf_counter()
    runs = _method_parts(scenario, plan)
    methods = [_result(method, parts, ms) for method, parts, ms in runs]
    gaps = [_basis_gap(m, parts) for m, (_, parts, _) in zip(methods, runs, strict=True)]
    optimizer = runs[0][1][0][1]
    replan_part = replan_benchmark(scenario, plan, air_locked)
    return BenchmarkResult(
        scenario_id=scenario.id,
        methods=methods,
        replan=replan_part,
        solve_ms=round((time.perf_counter() - started) * 1000),
        bases=_bases(scenario, optimizer),
        basis_gaps=gaps,
        settlement_fcfs_explanation=settlement_fcfs_explanation(scenario, optimizer, gaps),
    )
