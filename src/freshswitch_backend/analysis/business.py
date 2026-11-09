import time
from collections import defaultdict
from itertools import product
from typing import Literal

from pydantic import BaseModel

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
    PlatformBasis,
    SeasonLine,
    SeasonProjection,
    VoyageEconomics,
)
from freshswitch_backend.analysis.params import SPEC_BY_KEY, anchor_value, scaled
from freshswitch_backend.domain.models import CostParams, Plan, Scenario
from freshswitch_backend.optimize.settlement import usage_fee, uses_usage_fee

ANNUAL_FIXED_COST = 150_000_000
VOYAGES_PER_YEAR = 52
DEFAULT_FORWARDER_SHARE = 0.0
FORWARDER_SHARE_SOURCE = (
    "최종 기획서: 포워더는 실행 파트너, 자기 운임·수수료를 화주에게 별도 청구. 성과보수 배분 없음"
)
FORWARDER_SHARE_SENSITIVITY_LABEL = "가정(포워더 배분형)"
DEFAULT_OPS_COST_PER_VOYAGE = round(ANNUAL_FIXED_COST / VOYAGES_PER_YEAR)
PALLET_KG = 415
PROPOSAL = "기획서 ㅍ3"
FREIGHT_DIFFERENCE_PER_PALLET = 664_000
MVP_CASH_FIXED_COST = 15_000_000
MAX_VARIABLE_COST_PER_PALLET = 1_000_000
MAX_OPS_COST_PER_VOYAGE = 1_000_000_000
MAX_BASE_FEE_PER_PALLET = 1_000_000
MAX_ANNUAL_AMOUNT = 1_000_000_000_000
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
    "fee_rate": 0.20,
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
        self.operational: dict[str, float] = {}
        sea_pallets = {c.shipper_id: c.sea_pallets for c in plan.shipper_costs}
        operational = {
            c.shipper_id: c.operational_baseline.total - c.operational.total
            for c in plan.shipper_costs
            if c.operational is not None and c.operational_baseline is not None
        }
        by_shipper = {s.shipper_id: s for s in plan.settlements}
        for a in plan.assignments:
            if a.mode != "sea":
                continue
            settlement = by_shipper.get(a.shipper_id)
            total = sea_pallets.get(a.shipper_id, 0)
            share = a.pallets / total if settlement and total else 0.0
            self.savings[a.lot_id] = settlement.savings * share if settlement else 0.0
            self.fee[a.lot_id] = settlement.fee * share if settlement else 0.0
            op_savings = operational.get(a.shipper_id)
            if op_savings is None:
                op_savings = settlement.savings if settlement else 0.0
            self.operational[a.lot_id] = op_savings * share


def _breakeven(numerator: float, margin: float) -> float | None:
    return round(numerator / margin, 2) if margin > 0 else None


def _platform_basis(rules: set[PlatformBasis]) -> PlatformBasis:
    return next(iter(rules)) if len(rules) == 1 else "mixed"


def voyage_economics(
    scenario: Scenario,
    plan: Plan,
    forwarder_share: float,
    ops_cost_per_voyage: int,
    variable_cost_per_pallet: int = 0,
) -> list[VoyageEconomics]:
    params = scenario.cost_params
    fee_rate = params.platform_fee_rate.value
    kept = 1 - forwarder_share
    usage_per_pallet = params.billing.usage_fee_per_pallet_krw.value
    tariff_fee = {t.product_id: t.fee_per_pallet for t in plan.settlement_tariffs}
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
                usage_fee(params, a.pallets)
                if uses_usage_fee(params, a.product_id)
                else money.fee[a.lot_id]
                for a in items
            )
            operational = sum(money.operational[a.lot_id] for a in items)
            margin = (operational + fixed) / load.pallets if load.pallets else 0.0
            per_pallet = offset = 0.0
            rules: set[PlatformBasis] = set()
            for a in items:
                if uses_usage_fee(params, a.product_id):
                    rules.add("usage_fee")
                    per_pallet += usage_per_pallet * a.pallets
                elif a.product_id in tariff_fee:
                    rules.add("tariff")
                    per_pallet += tariff_fee[a.product_id] * a.pallets
                else:
                    rules.add("performance")
                    share = fixed * a.pallets / load.pallets
                    per_pallet += fee_rate * (money.operational[a.lot_id] + share)
                    offset += fee_rate * share
            platform = None
            if load.pallets:
                platform = _breakeven(
                    load.pallets * (ops_per_container + kept * offset),
                    kept * per_pallet - variable_cost_per_pallet * load.pallets,
                )
            rows.append(
                ContainerEconomics(
                    container_id=load.container_id,
                    pallets=load.pallets,
                    capacity_pallets=load.capacity_pallets,
                    fill=load.utilization,
                    container_cost=fixed,
                    savings=round(savings),
                    operational_savings=round(operational),
                    margin_per_pallet=round(margin),
                    margin_basis="operational",
                    platform_basis=_platform_basis(rules),
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
                    ops_cost_per_voyage, (fee_total * kept - variable) / len(used)
                ),
                containers=rows,
            )
        )
    return result


def season_projection(
    scenario: Scenario, plan: Plan, forwarder_share: float, variable_cost_per_pallet: int = 0
) -> SeasonProjection | None:
    product_ids = {p.id for p in scenario.products}
    if not all(pid in product_ids for pid, _, _ in SEASON_VOLUMES):
        return None
    money = _LotMoney(plan)
    params = scenario.cost_params
    fee_rate = params.platform_fee_rate.value
    per_pallet_fee = usage_fee(params, 1)
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
        if uses_usage_fee(params, product_id):
            rule = f"파렛트 이용료 {per_pallet_fee:,}원({PROPOSAL} 8-1: 이미 배로 가는 품목)"
            revenue = usage_fee(params, pallets)
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
    total_pallets = sum(line.pallets for line in lines)
    variable = variable_cost_per_pallet * total_pallets
    net_per_pallet = (revenue - forwarder - variable) / total_pallets
    return SeasonProjection(
        lines=lines,
        revenue=revenue,
        forwarder_share=forwarder,
        variable_cost=variable,
        fixed_cost=ANNUAL_FIXED_COST,
        platform_net=revenue - forwarder - variable - ANNUAL_FIXED_COST,
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
    usage = scenario.cost_params.billing.usage_fee_per_pallet_krw
    assumptions = [
        Assumption(
            key="platform_fee_rate", value=fee_rate.value, unit="비율", source=fee_rate.source
        ),
        Assumption(
            key="forwarder_share",
            value=forwarder_share,
            unit="수수료 대비 비율",
            source=FORWARDER_SHARE_SOURCE,
        ),
        Assumption(
            key="ops_cost_per_voyage",
            value=ops_cost_per_voyage,
            unit="원/항차",
            source=f"가정: {PROPOSAL} 8-1 고정비 연 1.5억 원 ÷ 연 {VOYAGES_PER_YEAR}항차(주 1회)",
        ),
        Assumption(
            key="usage_fee_per_pallet",
            value=usage.value,
            unit=usage.unit,
            source=usage.source,
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
        season=season_projection(scenario, plan, forwarder_share, variable_cost_per_pallet),
        basis=basis_comparison(scenario, plan, forwarder_share, variable_cost_per_pallet),
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


def _proposal_revenue(
    params: CostParams, pallets: dict[str, int], per_pallet: dict[str, int], fee_rate: float
) -> int:
    return round(
        sum(
            usage_fee(params, n) if uses_usage_fee(params, pid) else n * per_pallet[pid] * fee_rate
            for pid, n in pallets.items()
        )
    )


def basis_comparison(
    scenario: Scenario, plan: Plan, forwarder_share: float, variable_cost_per_pallet: int = 0
) -> list[BasisComparison]:
    pallets = _season_pallets(scenario)
    if pallets is None:
        return []
    fee_rate = scenario.cost_params.platform_fee_rate.value
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
            "정산 기준(정산표 단가)",
            "이 시나리오 최적화 결과의 품목별 파렛트당 화주 절감 평균(항공 대비 총비용 차이)",
            {pid: measured[pid] for pid in pallets},
        ),
    )
    result = []
    for basis, label, source, per_pallet in bases:
        total = sum(pallets.values())
        performance = round(sum(n * per_pallet[pid] * fee_rate for pid, n in pallets.items()))
        proposal = _proposal_revenue(scenario.cost_params, pallets, per_pallet, fee_rate)
        per_pallet_keep = proposal / total * (1 - forwarder_share) - variable_cost_per_pallet
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
                season_revenue_proposal_rule=proposal,
                variable_cost_per_pallet=variable_cost_per_pallet,
                breakeven_pallets=round(ANNUAL_FIXED_COST / per_pallet_keep)
                if per_pallet_keep > 0
                else None,
            )
        )
    return result


def _grape_solo_savings(scenario: Scenario, plan: Plan) -> int | None:
    params = scenario.cost_params
    grape_shippers = {
        lot.shipper_id for lot in scenario.lots if uses_usage_fee(params, lot.product_id)
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
        if a.shipper_id in grape_shippers and uses_usage_fee(params, a.product_id)
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
    params: CostParams,
    product_id: str,
    regime: AirRegime,
    fee_rate: float,
    base_fee: int,
    mode: BaseFeeMode,
    grape_rule: GrapeRule,
) -> float:
    if uses_usage_fee(params, product_id):
        if grape_rule == "usage_fee":
            return max(usage_fee(params, 1), base_fee)
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
        needs.append(
            f"포워더 배분 {row['forwarder_share']:.0%}({FORWARDER_SHARE_SENSITIVITY_LABEL})"
        )
    if row["fixed_level"] != BASELINE["fixed_level"]:
        needs.append(f"고정비 MVP 현금 연 {MVP_CASH_FIXED_COST:,}원(가정)")
    if row["air_regime"] != BASELINE["air_regime"]:
        needs.append("항공 할증 운임(3,741원/kg) 지속")
    return needs


def _describe(row: MatrixRow) -> str:
    return " + ".join(row.needs) if row.needs else "기획서 기준 그대로"


NO_BREAKEVEN = "손익분기 없음(파렛트당 공헌이익 ≤ 0)"


def _breakeven_text(row: MatrixRow) -> str:
    if row.breakeven_pallets is None:
        return NO_BREAKEVEN
    return f"손익분기 연 {row.breakeven_pallets:,}파렛트"


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
            summary=f"흑자 {row.net:,}원, {_breakeven_text(row)}: {_describe(row)}",
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
                    f"가장 나은 조합도 {best.net:,}원({_breakeven_text(best)}): "
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
    params = scenario.cost_params
    per_pallet_fee = usage_fee(params, 1)
    regimes = _air_regimes(scenario)
    rows: list[MatrixRow] = []
    for regime_and_sub, fee_rate, base_fee, share, level in product(
        regimes, FEE_RATES, BASE_FEES, FORWARDER_SHARES, FIXED_LEVELS
    ):
        regime = regime_and_sub[0]
        revenue = round(
            sum(
                n
                * _product_revenue(
                    params, pid, regime, fee_rate, base_fee, base_fee_mode, grape_rule
                )
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
                f"포도: 이용료 {per_pallet_fee:,}원(기획서 8-1). 기본 이용료가 더 크면 기본 이용료"
            ),
            "performance": "포도: 딸기와 같이 항공 대비 절감 × 성과보수율",
            "solo_baseline": (
                "포도: 화주 단독(전용 컨테이너 또는 항공 중 싼 쪽) 대비 합적 절감 × 성과보수율. "
                "포도는 원래 배로 가므로 항공이 아닌 단독 해상을 기준선으로 본다"
            ),
        }[grape_rule],
        "공헌이익 = 매출 − 포워더 배분 − 변동비. 순이익 = 공헌이익 − 연 고정비",
        (
            f"포워더 배분 기본 {DEFAULT_FORWARDER_SHARE:.0%}({FORWARDER_SHARE_SOURCE}). "
            f"40%는 민감도용 {FORWARDER_SHARE_SENSITIVITY_LABEL}"
        ),
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


FORWARDER_SENSITIVITY_SHARE = 0.4
DEFAULT_YEARS = 5
DEFAULT_BASE_FEE_PER_PALLET = 20_000
DEFAULT_BASE_FEE_FROM_YEAR = 2
SOM_SOURCE = "changelog/03 SOM: 5년 차 딸기 1,316t + 포도 1,990t(기획서 점유율 가정)"
YEAR_ONE_TONNES = {pid: tonnes for pid, tonnes, _ in SEASON_VOLUMES}
YEAR_FIVE_TONNES = {"strawberry": 1_316.0, "grape_so2": 1_990.0}
DEFAULT_SUBSIDIES = (100_000_000, 100_000_000, 0, 0, 0)
SUBSIDY_SOURCE = (
    "가정: 1년 차 예비창업패키지·2년 차 초기창업패키지 각 최대 1억 원 수준을 받는다고 본 값. "
    "선정 여부·금액 확인 필요(https://www.k-startup.go.kr)"
)
SPACE_SALE_UPSIDE = 190_000_000
SPACE_SALE_SOURCE = (
    "D13: 5년 차 13.2억 중 공간 판매 마진 약 1.9억은 매출에서 빼고 '확장 시 +1.9억'으로 따로 적는다"
)


class YearVolume(BaseModel):
    year: int
    tonnes: dict[str, float]
    pallets: dict[str, int]
    source: str


class YearSubsidy(BaseModel):
    year: int
    amount: int
    source: str


class YearRow(BaseModel):
    year: int
    stage: str
    pallets: int
    revenue: int
    performance_fee: int
    usage_fee: int
    forwarder_cost: int
    contribution: int
    fixed_cost: int
    subsidy: int
    net_before_subsidy: int
    net_after_subsidy: int
    cumulative_before_subsidy: int
    cumulative_after_subsidy: int
    excluded_upside: int = 0


class YearLine(BaseModel):
    forwarder_share: float
    forwarder_label: str
    base_fee_mode: BaseFeeMode
    fixed_level: Literal["full", "mvp_cash"]
    fixed_label: str
    rows: list[YearRow]
    breakeven_year_before_subsidy: int | None
    breakeven_year_after_subsidy: int | None
    payback_year_before_subsidy: int | None
    payback_year_after_subsidy: int | None


class BusinessYears(BaseModel):
    scenario_id: str
    years: int
    fee_rate: float
    base_fee_per_pallet: int
    base_fee_from_year: int
    savings_per_pallet: dict[str, int]
    savings_source: str
    volumes: list[YearVolume]
    subsidies: list[YearSubsidy]
    fixed_levels: list[FixedLevel]
    rules: list[str]
    lines: list[YearLine]
    excluded_upside_source: str


def _stage(year: int, base_fee_from_year: int) -> str:
    if year >= 4:
        return "3단계: 공간 판매 시작(D13에 따라 공간 판매 마진은 매출에서 뺌)"
    if year >= base_fee_from_year:
        return "2단계: 성과보수 + 파렛트 이용료"
    return "1단계: 성과보수(포도는 이용료), 운송은 포워더 실행"


def year_volumes(years: int, volume_scale: float = 1.0) -> list[YearVolume]:
    result = []
    for year in range(1, years + 1):
        weight = (year - 1) / 4
        tonnes = {
            pid: round(
                (YEAR_ONE_TONNES[pid] + (YEAR_FIVE_TONNES[pid] - YEAR_ONE_TONNES[pid]) * weight)
                * volume_scale,
                1,
            )
            for pid in YEAR_ONE_TONNES
        }
        if year == 1:
            source = f"{PROPOSAL} 8-3: 1년 차 딸기 109t·포도 298t(점유율은 기획서 가정)"
        elif year == 5:
            source = SOM_SOURCE
        else:
            source = "가정: 1년 차(기획서 8-3)와 5년 차(SOM) 사이 직선 보간"
        if volume_scale != 1.0:
            source += f". 물량 배율 {volume_scale:g}(쿼리 가정)"
        result.append(
            YearVolume(
                year=year,
                tonnes=tonnes,
                pallets={pid: round(t * 1000 / PALLET_KG) for pid, t in tonnes.items()},
                source=source,
            )
        )
    return result


def _year_fee(
    params: CostParams,
    product_id: str,
    savings: int,
    fee_rate: float,
    base_fee: int,
    mode: BaseFeeMode,
) -> tuple[float, float]:
    if uses_usage_fee(params, product_id):
        return 0.0, float(max(usage_fee(params, 1), base_fee))
    performance = fee_rate * max(0, savings)
    if mode == "additive":
        return performance, float(base_fee)
    if base_fee > performance:
        return 0.0, float(base_fee)
    return performance, 0.0


def _first_year(values: list[tuple[int, int]]) -> int | None:
    return next((year for year, value in values if value >= 0), None)


def _year_levels(fixed_cost: int, mvp_cash_fixed_cost: int) -> tuple[FixedLevel, FixedLevel]:
    full, mvp = FIXED_LEVELS
    return (
        full.model_copy(
            update={
                "annual_cost": fixed_cost,
                "source": full.source if fixed_cost == full.annual_cost else "쿼리로 넣은 가정",
            }
        ),
        mvp.model_copy(
            update={
                "annual_cost": mvp_cash_fixed_cost,
                "source": mvp.source
                if mvp_cash_fixed_cost == mvp.annual_cost
                else "쿼리로 넣은 가정",
            }
        ),
    )


def _year_line(
    params: CostParams,
    measured: dict[str, int],
    volumes: list[YearVolume],
    amounts: list[int],
    rate: float,
    base_fee_per_pallet: int,
    base_fee_from_year: int,
    share: tuple[float, str],
    mode: BaseFeeMode,
    level: FixedLevel,
) -> YearLine:
    rows = []
    cumulative_before = cumulative_after = 0
    for volume in volumes:
        base_fee = base_fee_per_pallet if volume.year >= base_fee_from_year else 0
        performance = usage = 0.0
        for pid, n in volume.pallets.items():
            perf, use = _year_fee(params, pid, measured.get(pid, 0), rate, base_fee, mode)
            performance += n * perf
            usage += n * use
        revenue = round(performance + usage)
        performance_fee = round(performance)
        forwarder = round(revenue * share[0])
        contribution = revenue - forwarder
        net_before = contribution - level.annual_cost
        subsidy = amounts[volume.year - 1]
        cumulative_before += net_before
        cumulative_after += net_before + subsidy
        rows.append(
            YearRow(
                year=volume.year,
                stage=_stage(volume.year, base_fee_from_year),
                pallets=sum(volume.pallets.values()),
                revenue=revenue,
                performance_fee=performance_fee,
                usage_fee=revenue - performance_fee,
                forwarder_cost=forwarder,
                contribution=contribution,
                fixed_cost=level.annual_cost,
                subsidy=subsidy,
                net_before_subsidy=net_before,
                net_after_subsidy=net_before + subsidy,
                cumulative_before_subsidy=cumulative_before,
                cumulative_after_subsidy=cumulative_after,
                excluded_upside=SPACE_SALE_UPSIDE if volume.year == 5 else 0,
            )
        )
    return YearLine(
        forwarder_share=share[0],
        forwarder_label=share[1],
        base_fee_mode=mode,
        fixed_level=level.key,
        fixed_label=level.label,
        rows=rows,
        breakeven_year_before_subsidy=_first_year([(r.year, r.net_before_subsidy) for r in rows]),
        breakeven_year_after_subsidy=_first_year([(r.year, r.net_after_subsidy) for r in rows]),
        payback_year_before_subsidy=_first_year(
            [(r.year, r.cumulative_before_subsidy) for r in rows]
        ),
        payback_year_after_subsidy=_first_year(
            [(r.year, r.cumulative_after_subsidy) for r in rows]
        ),
    )


def business_years(
    scenario: Scenario,
    years: int = DEFAULT_YEARS,
    fee_rate: float | None = None,
    base_fee_per_pallet: int = DEFAULT_BASE_FEE_PER_PALLET,
    base_fee_from_year: int = DEFAULT_BASE_FEE_FROM_YEAR,
    sensitivity_share: float = FORWARDER_SENSITIVITY_SHARE,
    fixed_cost: int = ANNUAL_FIXED_COST,
    mvp_cash_fixed_cost: int = MVP_CASH_FIXED_COST,
    subsidies: list[int] | None = None,
    volume_scale: float = 1.0,
) -> BusinessYears | None:
    if _season_pallets(scenario) is None:
        return None
    params = scenario.cost_params
    rate = params.platform_fee_rate.value if fee_rate is None else fee_rate
    measured = savings_per_pallet(solve_routed(scenario))
    volumes = year_volumes(years, volume_scale)
    given = list(DEFAULT_SUBSIDIES if subsidies is None else subsidies)
    amounts = [given[i] if i < len(given) else 0 for i in range(years)]
    source = SUBSIDY_SOURCE if subsidies is None else "쿼리로 넣은 가정"
    levels = _year_levels(fixed_cost, mvp_cash_fixed_cost)
    shares = (
        (
            DEFAULT_FORWARDER_SHARE,
            f"기준선 {DEFAULT_FORWARDER_SHARE:.0%}({FORWARDER_SHARE_SOURCE})",
        ),
        (sensitivity_share, f"민감도 {sensitivity_share:.0%}({FORWARDER_SHARE_SENSITIVITY_LABEL})"),
    )
    modes: tuple[BaseFeeMode, ...] = ("deducted", "additive")
    lines = [
        _year_line(
            params,
            measured,
            volumes,
            amounts,
            rate,
            base_fee_per_pallet,
            base_fee_from_year,
            share,
            mode,
            level,
        )
        for share, mode, level in product(shares, modes, levels)
    ]
    rules = [
        "매출 = Σ 품목별 연 파렛트 × 파렛트당 과금(파렛트당 절감은 이 시나리오 최적화 결과 평균)",
        f"포도: 파렛트 이용료 {usage_fee(params, 1):,}원(기획서 8-1). 기본 이용료가 더 크면 그 값",
        (
            f"{base_fee_from_year}년 차부터 딸기에도 기본 이용료 파렛트당 {base_fee_per_pallet:,}원. "
            "차감형(deducted) = max(기본 이용료, 성과보수), 가산형(additive) = 기본 이용료 + 성과보수"
        ),
        "공헌이익 = 매출 − 포워더 배분. 순이익 = 공헌이익 − 연 고정비(지원금 후는 + 지원금)",
        "손익분기 연차 = 그해 순이익이 처음 0 이상인 해. 회수 연차 = 누적 손익이 처음 0 이상인 해",
        f"공간 판매 마진은 매출에 넣지 않는다({SPACE_SALE_SOURCE})",
    ]
    return BusinessYears(
        scenario_id=scenario.id,
        years=years,
        fee_rate=rate,
        base_fee_per_pallet=base_fee_per_pallet,
        base_fee_from_year=base_fee_from_year,
        savings_per_pallet=measured,
        savings_source="이 시나리오 최적화 결과의 품목별 파렛트당 화주 절감 평균(평시 운임, 총비용 기준)",
        volumes=volumes,
        subsidies=[YearSubsidy(year=i + 1, amount=amounts[i], source=source) for i in range(years)],
        fixed_levels=list(levels),
        rules=rules,
        lines=lines,
        excluded_upside_source=SPACE_SALE_SOURCE,
    )
