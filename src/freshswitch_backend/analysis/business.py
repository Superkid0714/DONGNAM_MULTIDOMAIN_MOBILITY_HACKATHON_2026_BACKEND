import time
from collections import defaultdict
from itertools import product
from typing import Literal

from freshswitch_backend.analysis.benchmark import routed_part, solo_plans
from freshswitch_backend.analysis.common import solve_routed
from freshswitch_backend.analysis.models import (
    AirRegime,
    Assumption,
    BasisComparison,
    BasisLine,
    BusinessMatrix,
    BusinessResult,
    ContainerEconomics,
    FixedLevel,
    MatrixHighlight,
    MatrixRow,
    SeasonLine,
    SeasonProjection,
    VoyageEconomics,
)
from freshswitch_backend.analysis.params import SPEC_BY_KEY, anchor_value, scaled
from freshswitch_backend.domain.models import Plan, Scenario

ANNUAL_FIXED_COST = 150_000_000
VOYAGES_PER_YEAR = 52
DEFAULT_FORWARDER_SHARE = 0.4
DEFAULT_OPS_COST_PER_VOYAGE = round(ANNUAL_FIXED_COST / VOYAGES_PER_YEAR)
USAGE_FEE_PER_PALLET = 20_000
USAGE_FEE_PRODUCTS = frozenset({"grape_so2"})
PALLET_KG = 415
PROPOSAL = "기획서 ㅍ3"
FREIGHT_DIFFERENCE_PER_PALLET = 664_000
MVP_CASH_FIXED_COST = 15_000_000
FEE_RATES = (0.20, 0.25, 0.30, 0.40)
BASE_FEES = (0, 20_000, 30_000)
FORWARDER_SHARES = (0.0, 0.4)
FIXED_LEVELS = (
    FixedLevel(
        key="full",
        label="사업 예산(인건비 포함)",
        annual_cost=ANNUAL_FIXED_COST,
        source="기획서 ㅍ3 8-1 고정비 연 1.5억 원",
    ),
    FixedLevel(
        key="mvp_cash",
        label="MVP 현금비용(가정)",
        annual_cost=MVP_CASH_FIXED_COST,
        source=(
            "가정: 클라우드·외부 API·도메인·운영 현금비 월 125만 원 × 12. "
            "인건비·사무실·영업비 제외, 창업지원금 반영 전"
        ),
    ),
)
BASELINE = {
    "fee_rate": 0.25,
    "base_fee_per_pallet": 0,
    "forwarder_share": DEFAULT_FORWARDER_SHARE,
    "fixed_level": "full",
    "air_regime": "normal",
}
BaseFeeMode = Literal["deducted", "additive"]
GrapeRule = Literal["usage_fee", "performance", "solo_baseline"]
SEASON_VOLUMES: tuple[tuple[str, float, str], ...] = (
    ("strawberry", 109.0, f"{PROPOSAL} 8-3: 1년 차 1단계 딸기(홍콩·베트남) 점유 10% = 109t"),
    ("grape_so2", 298.0, f"{PROPOSAL} 8-3: 1년 차 포도 합승 점유 3% = 298t"),
)


class _LotMoney:
    def __init__(self, plan: Plan) -> None:
        self.savings: dict[str, float] = {}
        self.fee: dict[str, float] = {}
        sea_pallets = {c.shipper_id: c.sea_pallets for c in plan.shipper_costs}
        by_shipper = {s.shipper_id: s for s in plan.settlements}
        for a in plan.assignments:
            if a.mode != "sea":
                continue
            settlement = by_shipper.get(a.shipper_id)
            total = sea_pallets.get(a.shipper_id, 0)
            share = a.pallets / total if settlement and total else 0.0
            self.savings[a.lot_id] = settlement.savings * share if settlement else 0.0
            self.fee[a.lot_id] = settlement.fee * share if settlement else 0.0


def _breakeven(numerator: float, margin: float) -> float | None:
    return round(numerator / margin, 2) if margin > 0 else None


def voyage_economics(
    scenario: Scenario,
    plan: Plan,
    forwarder_share: float,
    ops_cost_per_voyage: int,
    variable_cost_per_pallet: int = 0,
) -> list[VoyageEconomics]:
    fee_rate = scenario.cost_params.platform_fee_rate.value
    keep = fee_rate * (1 - forwarder_share)
    money = _LotMoney(plan)
    lots_in: dict[str, list] = defaultdict(list)
    for a in plan.assignments:
        if a.mode == "sea" and a.container_id:
            lots_in[a.container_id].append(a)
    result = []
    for voyage in scenario.voyages:
        used = [c for c in plan.containers if c.voyage_id == voyage.id and c.used]
        if not used:
            continue
        ops_per_container = ops_cost_per_voyage / len(used)
        rows = []
        fee_total = usage_rule_total = 0.0
        for load in used:
            items = lots_in[load.container_id]
            fixed = load.container_cost + getattr(load, "terminal_plug_cost", 0)
            savings = sum(money.savings[a.lot_id] for a in items)
            fee = sum(money.fee[a.lot_id] for a in items)
            fee_total += fee
            usage_rule_total += sum(
                USAGE_FEE_PER_PALLET * a.pallets
                if a.product_id in USAGE_FEE_PRODUCTS
                else money.fee[a.lot_id]
                for a in items
            )
            margin = (savings + fixed) / load.pallets if load.pallets else 0.0
            platform = _breakeven(ops_per_container / keep + fixed, margin) if keep > 0 else None
            rows.append(
                ContainerEconomics(
                    container_id=load.container_id,
                    pallets=load.pallets,
                    capacity_pallets=load.capacity_pallets,
                    fill=load.utilization,
                    container_cost=fixed,
                    savings=round(savings),
                    margin_per_pallet=round(margin),
                    breakeven_savings_pallets=_breakeven(fixed, margin),
                    breakeven_platform_pallets=platform,
                    breakeven_platform_fill=round(platform / load.capacity_pallets, 4)
                    if platform is not None
                    else None,
                )
            )
        pallets = sum(c.pallets for c in used)
        capacity = sum(c.capacity_pallets for c in used)
        fee_revenue = round(fee_total)
        forwarder = round(fee_total * forwarder_share)
        variable = variable_cost_per_pallet * pallets
        contribution = fee_revenue - forwarder - variable
        result.append(
            VoyageEconomics(
                voyage_id=voyage.id,
                vessel_name=voyage.vessel_name,
                etd=voyage.etd.isoformat(),
                containers_used=len(used),
                pallets=pallets,
                capacity_pallets=capacity,
                fill=round(pallets / capacity, 4),
                savings=sum(r.savings for r in rows),
                fee_revenue=fee_revenue,
                forwarder_share=forwarder,
                ops_cost=ops_cost_per_voyage,
                platform_net=contribution - ops_cost_per_voyage,
                variable_cost=variable,
                contribution_margin=contribution,
                fixed_cost_allocation=ops_cost_per_voyage,
                usage_fee_rule_revenue=round(usage_rule_total),
                breakeven_containers=_breakeven(
                    ops_cost_per_voyage, fee_total * (1 - forwarder_share) / len(used)
                ),
                containers=rows,
            )
        )
    return result


def season_projection(
    scenario: Scenario, plan: Plan, forwarder_share: float
) -> SeasonProjection | None:
    product_ids = {p.id for p in scenario.products}
    if not all(pid in product_ids for pid, _, _ in SEASON_VOLUMES):
        return None
    money = _LotMoney(plan)
    fee_rate = scenario.cost_params.platform_fee_rate.value
    lines = []
    for product_id, tonnes, source in SEASON_VOLUMES:
        sea = [a for a in plan.assignments if a.mode == "sea" and a.product_id == product_id]
        sea_pallets = sum(a.pallets for a in sea)
        if not sea_pallets:
            return None
        per_pallet = sum(money.savings[a.lot_id] for a in sea) / sea_pallets
        containers = {a.container_id for a in sea}
        per_container = sea_pallets / len(containers)
        pallets = round(tonnes * 1000 / PALLET_KG)
        if product_id in USAGE_FEE_PRODUCTS:
            rule = f"파렛트 이용료 {USAGE_FEE_PER_PALLET:,}원({PROPOSAL} 8-1: 이미 배로 가는 품목)"
            revenue = pallets * USAGE_FEE_PER_PALLET
        else:
            rule = f"절감액 × {fee_rate:.0%}({PROPOSAL} 8-1)"
            revenue = round(pallets * per_pallet * fee_rate)
        lines.append(
            SeasonLine(
                product_id=product_id,
                tonnes=tonnes,
                pallets=pallets,
                containers=round(pallets / per_container, 1),
                savings_per_pallet=round(per_pallet),
                revenue_rule=rule,
                revenue=revenue,
                source=source,
            )
        )
    revenue = sum(line.revenue for line in lines)
    forwarder = round(revenue * forwarder_share)
    net_per_pallet = (revenue - forwarder) / sum(line.pallets for line in lines)
    return SeasonProjection(
        lines=lines,
        revenue=revenue,
        forwarder_share=forwarder,
        fixed_cost=ANNUAL_FIXED_COST,
        platform_net=revenue - forwarder - ANNUAL_FIXED_COST,
        breakeven_pallets=round(ANNUAL_FIXED_COST / net_per_pallet) if net_per_pallet > 0 else None,
        source=f"{PROPOSAL} 8-3 물량(점유율은 기획서 가정), 8-1 고정비 연 1.5억 원(가정). "
        "파렛트당 절감액은 이 시나리오 최적화 결과 평균",
    )


def business(
    scenario: Scenario,
    forwarder_share: float = DEFAULT_FORWARDER_SHARE,
    ops_cost_per_voyage: int = DEFAULT_OPS_COST_PER_VOYAGE,
    variable_cost_per_pallet: int = 0,
) -> BusinessResult:
    plan = solve_routed(scenario)
    voyages = voyage_economics(
        scenario, plan, forwarder_share, ops_cost_per_voyage, variable_cost_per_pallet
    )
    fee_rate = scenario.cost_params.platform_fee_rate
    assumptions = [
        Assumption(
            key="platform_fee_rate", value=fee_rate.value, unit="비율", source=fee_rate.source
        ),
        Assumption(
            key="forwarder_share",
            value=forwarder_share,
            unit="수수료 대비 비율",
            source="가정: 문서에 포워더 배분 수치 없음. 1년 차는 포워더 명의 운송(기획서 8-1)",
        ),
        Assumption(
            key="ops_cost_per_voyage",
            value=ops_cost_per_voyage,
            unit="원/항차",
            source=f"가정: {PROPOSAL} 8-1 고정비 연 1.5억 원 ÷ 연 {VOYAGES_PER_YEAR}항차(주 1회)",
        ),
        Assumption(
            key="usage_fee_per_pallet",
            value=USAGE_FEE_PER_PALLET,
            unit="원/파렛트",
            source=f"{PROPOSAL} 8-1: 포도는 성과보수 없이 파렛트 이용료 2만 원",
        ),
        Assumption(
            key="variable_cost_per_pallet",
            value=variable_cost_per_pallet,
            unit="원/파렛트",
            source="가정: 항차마다 드는 플랫폼 변동비(결제·정산 수수료 등). 기본 0원",
        ),
    ]
    return BusinessResult(
        scenario_id=scenario.id,
        assumptions=assumptions,
        voyages=voyages,
        total_fee_revenue=sum(v.fee_revenue for v in voyages),
        total_platform_net=sum(v.platform_net for v in voyages),
        total_contribution_margin=sum(v.contribution_margin for v in voyages),
        total_fixed_cost_allocation=sum(v.fixed_cost_allocation for v in voyages),
        season=season_projection(scenario, plan, forwarder_share),
        basis=basis_comparison(scenario, plan, forwarder_share),
    )


def _season_pallets(scenario: Scenario) -> dict[str, int] | None:
    product_ids = {p.id for p in scenario.products}
    if not all(pid in product_ids for pid, _, _ in SEASON_VOLUMES):
        return None
    return {pid: round(tonnes * 1000 / PALLET_KG) for pid, tonnes, _ in SEASON_VOLUMES}


def savings_per_pallet(plan: Plan) -> dict[str, int]:
    money = _LotMoney(plan)
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    for a in plan.assignments:
        if a.mode == "sea":
            totals[a.product_id][0] += money.savings[a.lot_id]
            totals[a.product_id][1] += a.pallets
    return {pid: round(total / pallets) for pid, (total, pallets) in totals.items() if pallets}


def _proposal_revenue(pallets: dict[str, int], per_pallet: dict[str, int], fee_rate: float) -> int:
    return round(
        sum(
            n * USAGE_FEE_PER_PALLET
            if pid in USAGE_FEE_PRODUCTS
            else n * per_pallet[pid] * fee_rate
            for pid, n in pallets.items()
        )
    )


def basis_comparison(
    scenario: Scenario, plan: Plan, forwarder_share: float
) -> list[BasisComparison]:
    pallets = _season_pallets(scenario)
    if pallets is None:
        return []
    fee_rate = scenario.cost_params.platform_fee_rate.value
    keep = fee_rate * (1 - forwarder_share)
    measured = savings_per_pallet(plan)
    if not all(pid in measured for pid in pallets):
        return []
    bases = (
        (
            "freight_difference",
            "운임 차액 기준(기획서)",
            (
                f"기획서: 운임만 보면 해상이 파렛트당 {FREIGHT_DIFFERENCE_PER_PALLET:,}원 싸다. "
                "손실·보관·집하·부대비를 빼지 않은 값"
            ),
            dict.fromkeys(pallets, FREIGHT_DIFFERENCE_PER_PALLET),
        ),
        (
            "total_cost",
            "총비용 기준(최적화·경로비 정산)",
            "이 시나리오 최적화 결과의 품목별 파렛트당 화주 절감 평균(항공 대비 총비용 차이)",
            {pid: measured[pid] for pid in pallets},
        ),
    )
    result = []
    for basis, label, source, per_pallet in bases:
        total = sum(pallets.values())
        performance = round(sum(n * per_pallet[pid] * fee_rate for pid, n in pallets.items()))
        per_pallet_keep = sum(n * per_pallet[pid] for pid, n in pallets.items()) / total * keep
        result.append(
            BasisComparison(
                basis=basis,
                label=label,
                source=source,
                lines=[
                    BasisLine(product_id=pid, pallets=n, savings_per_pallet=per_pallet[pid])
                    for pid, n in pallets.items()
                ],
                fee_rate=fee_rate,
                season_revenue_performance=performance,
                season_revenue_proposal_rule=_proposal_revenue(pallets, per_pallet, fee_rate),
                breakeven_pallets=round(ANNUAL_FIXED_COST / per_pallet_keep)
                if per_pallet_keep > 0
                else None,
            )
        )
    return result


def _grape_solo_savings(scenario: Scenario, plan: Plan) -> int | None:
    grape_shippers = {
        lot.shipper_id for lot in scenario.lots if lot.product_id in USAGE_FEE_PRODUCTS
    }
    if not grape_shippers:
        return None
    solo_cost = 0
    for sub, solo in solo_plans(scenario):
        if sub.lots[0].shipper_id in grape_shippers:
            _, costed, _ = routed_part(sub, solo)
            solo_cost += costed.total_shared_cost
    shared = sum(s.shared_cost for s in plan.settlements if s.shipper_id in grape_shippers)
    pallets = sum(
        a.pallets
        for a in plan.assignments
        if a.shipper_id in grape_shippers and a.product_id in USAGE_FEE_PRODUCTS
    )
    return round((solo_cost - shared) / pallets) if pallets else None


def _air_regimes(scenario: Scenario) -> list[tuple[AirRegime, Scenario]]:
    spec = SPEC_BY_KEY["air_freight_per_kg_krw"]
    base = anchor_value(scenario, spec)
    regimes = []
    for key, label, factor, source in (
        ("normal", "평시", 1.0, "시나리오 기준값(cost_params)"),
        (
            "surcharge",
            "2026.4 할증",
            spec.high_factor,
            "2026.4.16 할증 후 파렛트 155.26만 원(한국농어민신문, 3,741원/kg)",
        ),
    ):
        sub = scenario if factor == 1.0 else scaled(scenario, spec, factor)
        plan = solve_routed(sub)
        regimes.append(
            (
                AirRegime(
                    key=key,
                    label=label,
                    air_freight_per_kg_krw=round(base * factor, 1),
                    source=source,
                    savings_per_pallet=savings_per_pallet(plan),
                    grape_solo_savings_per_pallet=_grape_solo_savings(sub, plan),
                ),
                sub,
            )
        )
    return regimes


def _per_pallet(performance: float, base_fee: int, mode: BaseFeeMode) -> float:
    return base_fee + performance if mode == "additive" else max(base_fee, performance)


def _product_revenue(
    product_id: str,
    regime: AirRegime,
    fee_rate: float,
    base_fee: int,
    mode: BaseFeeMode,
    grape_rule: GrapeRule,
) -> float:
    if product_id in USAGE_FEE_PRODUCTS:
        if grape_rule == "usage_fee":
            return max(USAGE_FEE_PER_PALLET, base_fee)
        if grape_rule == "solo_baseline":
            savings = max(0, regime.grape_solo_savings_per_pallet or 0)
            return _per_pallet(fee_rate * savings, base_fee, mode)
    savings = max(0, regime.savings_per_pallet.get(product_id, 0))
    return _per_pallet(fee_rate * savings, base_fee, mode)


def _needs(row: dict) -> list[str]:
    needs = []
    if row["fee_rate"] != BASELINE["fee_rate"]:
        needs.append(f"성과보수 {row['fee_rate']:.0%}")
    if row["base_fee_per_pallet"] != BASELINE["base_fee_per_pallet"]:
        needs.append(f"기본 이용료 파렛트당 {row['base_fee_per_pallet']:,}원")
    if row["forwarder_share"] != BASELINE["forwarder_share"]:
        needs.append(f"포워더 배분 {row['forwarder_share']:.0%}(SaaS형)")
    if row["fixed_level"] != BASELINE["fixed_level"]:
        needs.append(f"고정비 MVP 현금 연 {MVP_CASH_FIXED_COST:,}원(가정)")
    if row["air_regime"] != BASELINE["air_regime"]:
        needs.append("항공 할증 운임(3,741원/kg) 지속")
    return needs


def _describe(row: MatrixRow) -> str:
    return " + ".join(row.needs) if row.needs else "기획서 기준 그대로"


def _highlights(rows: list[MatrixRow], limit: int = 5) -> list[MatrixHighlight]:
    winners = sorted((r for r in rows if r.breaks_even), key=lambda r: (len(r.needs), -r.net))
    picked: list[MatrixRow] = []
    seen: list[set[str]] = []
    for row in winners:
        if any(prev <= set(row.needs) for prev in seen):
            continue
        seen.append(set(row.needs))
        picked.append(row)
        if len(picked) == limit:
            break
    result = [
        MatrixHighlight(
            summary=(
                f"흑자 {row.net:,}원, 손익분기 연 {row.breakeven_pallets:,}파렛트: {_describe(row)}"
            ),
            needs=row.needs,
            row=row,
        )
        for row in picked
    ]
    full = [r for r in rows if r.fixed_level == "full"]
    if full and not any(r.breaks_even for r in full):
        best = max(full, key=lambda r: (r.net, -len(r.needs)))
        result.append(
            MatrixHighlight(
                summary=(
                    f"사업 예산(연 {best.fixed_cost:,}원) 기준으로는 모든 조합이 1년 차 적자. "
                    f"가장 나은 조합도 {best.net:,}원(손익분기 연 {best.breakeven_pallets:,}파렛트): "
                    f"{_describe(best)}"
                ),
                needs=best.needs,
                row=best,
            )
        )
    return result


def business_matrix(
    scenario: Scenario,
    base_fee_mode: BaseFeeMode = "deducted",
    grape_rule: GrapeRule = "usage_fee",
    variable_cost_per_pallet: int = 0,
) -> BusinessMatrix | None:
    started = time.perf_counter()
    pallets = _season_pallets(scenario)
    if pallets is None:
        return None
    total_pallets = sum(pallets.values())
    regimes = _air_regimes(scenario)
    rows: list[MatrixRow] = []
    for regime_and_sub, fee_rate, base_fee, share, level in product(
        regimes, FEE_RATES, BASE_FEES, FORWARDER_SHARES, FIXED_LEVELS
    ):
        regime = regime_and_sub[0]
        revenue = round(
            sum(
                n * _product_revenue(pid, regime, fee_rate, base_fee, base_fee_mode, grape_rule)
                for pid, n in pallets.items()
            )
        )
        forwarder = round(revenue * share)
        variable = variable_cost_per_pallet * total_pallets
        contribution = revenue - forwarder - variable
        per_pallet = contribution / total_pallets
        fields = {
            "fee_rate": fee_rate,
            "base_fee_per_pallet": base_fee,
            "forwarder_share": share,
            "fixed_level": level.key,
            "air_regime": regime.key,
        }
        rows.append(
            MatrixRow(
                **fields,
                pallets=total_pallets,
                revenue=revenue,
                forwarder_cost=forwarder,
                variable_cost=variable,
                contribution=contribution,
                fixed_cost=level.annual_cost,
                net=contribution - level.annual_cost,
                breakeven_pallets=round(level.annual_cost / per_pallet) if per_pallet > 0 else None,
                breaks_even=contribution >= level.annual_cost,
                needs=_needs(fields),
            )
        )
    rules = [
        "매출 = Σ 품목별 시즌 파렛트 × 파렛트당 과금",
        (
            "기본 이용료 차감형(deducted): 파렛트당 max(기본 이용료, 성과보수)"
            if base_fee_mode == "deducted"
            else "기본 이용료 합산형(additive): 파렛트당 기본 이용료 + 성과보수"
        ),
        {
            "usage_fee": (
                f"포도: 이용료 {USAGE_FEE_PER_PALLET:,}원(기획서 8-1). "
                "기본 이용료가 더 크면 기본 이용료"
            ),
            "performance": "포도: 딸기와 같이 항공 대비 절감 × 성과보수율",
            "solo_baseline": (
                "포도: 화주 단독(전용 컨테이너 또는 항공 중 싼 쪽) 대비 합적 절감 × 성과보수율. "
                "포도는 원래 배로 가므로 항공이 아닌 단독 해상을 기준선으로 본다"
            ),
        }[grape_rule],
        "공헌이익 = 매출 − 포워더 배분 − 변동비. 순이익 = 공헌이익 − 연 고정비",
        "손익분기 파렛트 = 연 고정비 ÷ 파렛트당 공헌이익(같은 품목 구성)",
    ]
    return BusinessMatrix(
        scenario_id=scenario.id,
        base_fee_mode=base_fee_mode,
        grape_rule=grape_rule,
        variable_cost_per_pallet=variable_cost_per_pallet,
        season_pallets=pallets,
        baseline=BASELINE,
        air_regimes=[r for r, _ in regimes],
        fixed_levels=list(FIXED_LEVELS),
        rules=rules,
        rows=rows,
        highlights=_highlights(rows),
        break_even_rows=sum(1 for r in rows if r.breaks_even),
        solve_ms=round((time.perf_counter() - started) * 1000),
    )
