from collections import Counter
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from freshswitch_backend.analysis.benchmark import empty_slot_cost, tariff_empty_slot_cost
from freshswitch_backend.analysis.business import (
    ANNUAL_FIXED_COST,
    DEFAULT_BASE_FEE_FROM_YEAR,
    DEFAULT_BASE_FEE_PER_PALLET,
    DEFAULT_FORWARDER_SHARE,
    DEFAULT_OPS_COST_PER_VOYAGE,
    FORWARDER_SENSITIVITY_SHARE,
    FORWARDER_SHARE_SENSITIVITY_LABEL,
    FORWARDER_SHARE_SOURCE,
    MAX_ANNUAL_AMOUNT,
    MAX_BASE_FEE_PER_PALLET,
    MAX_OPS_COST_PER_VOYAGE,
    MAX_VARIABLE_COST_PER_PALLET,
    MVP_CASH_FIXED_COST,
    PROPOSAL,
    VOYAGES_PER_YEAR,
    BusinessYears,
    business_years,
    voyage_economics,
)
from freshswitch_backend.analysis.emissions import EmissionResult, avoided_emissions
from freshswitch_backend.analysis.models import Assumption, VoyageEconomics
from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.compute_limits import analysis_rate, analysis_slot
from freshswitch_backend.api.saas_access import visible_plans
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST, Plan, Scenario
from freshswitch_backend.execution.quality import adjusted_settlements
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import OrderRow, PlanRow
from freshswitch_backend.planning.store import PlanSession, store

router = APIRouter(prefix="/saas/platform", tags=["saas-platform"])

PlatformPrincipal = Annotated[Principal, Depends(require_role("platform"))]
MonthQuery = Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
CONFIRMED_ORDERS = frozenset({"forwarder_confirmed", "executed"})
REGIONS = (("부울경", ("부산", "울산", "경남", "경상남도")), ("경북", ("경북", "대구", "경상북도")))
OTHER_REGION = "기타"


class Metric(BaseModel):
    value: float | None
    numerator: float | None = None
    denominator: float | None = None
    unit: str
    definition: str
    reason: str | None = None


class RegionMetrics(BaseModel):
    region: str
    shipper_count: int
    pallets: int
    sea_pallets: int
    savings_krw: int
    reefer_containers: int


class PlatformMetrics(BaseModel):
    month: str
    month_basis: str
    plan_count: int
    shipment_count: int
    shipment_pallets: int
    sea_pallets: int
    air_pallets: int
    reefer_containers: int
    average_fill: Metric
    shipper_savings_krw: int
    platform_fee_krw: int
    performance_fee_krw: int
    usage_fee_krw: int
    fee_before_refund_krw: int
    quality_refund_krw: int
    air_avoided_pallets: int
    air_avoided_definition: str
    order_confirmation_rate: Metric
    plan_confirmation_rate: Metric
    shipper_reuse_rate: Metric
    average_replan_ms: Metric
    average_arrival_loss_gap: Metric
    co2: EmissionResult
    regions: list[RegionMetrics]


class PlanEconomics(BaseModel):
    plan_id: str
    departure: datetime | None
    voyages: list[VoyageEconomics]
    fee_revenue: int
    contribution_margin: int
    empty_slot_cost: int
    tariff_empty_slot_cost: int
    tariff_operational_gap: int


class BusinessLine(BaseModel):
    forwarder_share: float
    label: str
    plans: list[PlanEconomics]
    voyage_count: int
    pallets: int
    fee_revenue: int
    forwarder_cost: int
    variable_cost: int
    contribution_margin: int
    fixed_cost_allocation: int
    platform_net: int
    settlement_gap: int
    platform_net_with_settlement: int
    annual_contribution: int | None
    annual_fixed_cost: int
    annual_net: int | None
    annual_settlement_gap: int | None
    annual_net_with_settlement: int | None
    breakeven_pallets_per_voyage: float | None
    remaining_pallets_per_voyage: float | None


class PlatformBusiness(BaseModel):
    month: str
    plan_count: int
    assumptions: list[Assumption]
    empty_slot_cost: int
    tariff_empty_slot_cost: int
    empty_slot_definition: str
    tariff_operational_gap: int
    tariff_operational_definition: str
    lines: list[BusinessLine]


def _month(month: str | None) -> str:
    return month or store.clock().astimezone(KST).strftime("%Y-%m")


def departure(session: PlanSession) -> datetime | None:
    used = {c.voyage_id for c in session.plan.containers if c.used}
    etds = [v.etd for v in session.scenario.voyages if v.id in used]
    return min(etds) if etds else None


def plan_month(session: PlanSession) -> str:
    moment = departure(session) or session.created_at
    return moment.astimezone(KST).strftime("%Y-%m")


def org_sessions(principal: Principal) -> list[PlanSession]:
    visible = visible_plans(principal)
    store.expire_due(visible)
    with database.begin() as db:
        rows = db.scalars(select(PlanRow).where(visible).order_by(PlanRow.seq)).all()
        return [PlanSession.model_validate(row.payload) for row in rows]


def _order_statuses(plan_ids: list[str]) -> list[str]:
    if not plan_ids:
        return []
    with database.begin() as db:
        return list(db.scalars(select(OrderRow.status).where(OrderRow.plan_id.in_(plan_ids))))


def _rate(numerator: int, denominator: int, definition: str, empty: str) -> Metric:
    return Metric(
        value=round(numerator / denominator, 4) if denominator else None,
        numerator=numerator,
        denominator=denominator,
        unit="비율",
        definition=definition,
        reason=None if denominator else empty,
    )


def region_of(label: str) -> str:
    return next(
        (name for name, prefixes in REGIONS if label.strip().startswith(prefixes)),
        OTHER_REGION,
    )


def _shipper_regions(scenario: Scenario) -> dict[str, str]:
    packhouses = {p.id: p.region for p in scenario.packhouses}
    return {s.id: region_of(packhouses.get(s.packhouse_id, "")) for s in scenario.shippers}


def _participants(plan: Plan) -> set[str]:
    return {a.shipper_id for a in plan.assignments if a.pallets > 0}


def _regions(sessions: list[PlanSession]) -> list[RegionMetrics]:
    acc: dict[str, dict] = {}
    for session in sessions:
        regions = _shipper_regions(session.scenario)
        savings = {
            r.shipper_id: r.savings
            for r in adjusted_settlements(
                session.plan, session.arrival_inspections, session.scenario
            )
        }
        for shipper_id in _participants(session.plan):
            row = acc.setdefault(
                regions.get(shipper_id, OTHER_REGION),
                {"shippers": set(), "pallets": 0, "sea": 0, "savings": 0, "containers": set()},
            )
            row["shippers"].add(shipper_id)
            row["savings"] += savings.get(shipper_id, 0)
        for a in session.plan.assignments:
            if a.pallets <= 0:
                continue
            row = acc[regions.get(a.shipper_id, OTHER_REGION)]
            row["pallets"] += a.pallets
            if a.mode == "sea":
                row["sea"] += a.pallets
        for c in session.plan.containers:
            if not c.used:
                continue
            for name in {regions.get(s, OTHER_REGION) for s in c.shipper_ids}:
                if name in acc:
                    acc[name]["containers"].add((session.plan_id, c.container_id))
    order = [name for name, _ in REGIONS] + [OTHER_REGION]
    return [
        RegionMetrics(
            region=name,
            shipper_count=len(acc[name]["shippers"]),
            pallets=acc[name]["pallets"],
            sea_pallets=acc[name]["sea"],
            savings_krw=acc[name]["savings"],
            reefer_containers=len(acc[name]["containers"]),
        )
        for name in order
        if name in acc
    ]


def _fill(sessions: list[PlanSession]) -> Metric:
    used = [c for s in sessions for c in s.plan.containers if c.used]
    pallets = sum(c.pallets for c in used)
    capacity = sum(c.capacity_pallets for c in used)
    return Metric(
        value=round(pallets / capacity, 4) if capacity else None,
        numerator=pallets,
        denominator=capacity,
        unit="비율",
        definition="쓴 리퍼 컨테이너의 적재 파렛트 합 ÷ 용량 합(파렛트 가중 평균)",
        reason=None if capacity else "이번 달 계획에 쓴 컨테이너가 없다",
    )


def _reuse(month: str, month_sessions: list[PlanSession], every: list[PlanSession]) -> Metric:
    counts = Counter(
        shipper_id
        for session in every
        if plan_month(session) <= month
        for shipper_id in _participants(session.plan)
    )
    shippers = {s for session in month_sessions for s in _participants(session.plan)}
    return _rate(
        sum(1 for s in shippers if counts[s] >= 2),
        len(shippers),
        "이번 달 계획에 참여한 화주 중 이 달 말까지 계획 2건 이상에 참여한 화주 비율",
        "이번 달 계획에 참여한 화주가 없다",
    )


def _replan_ms(sessions: list[PlanSession]) -> Metric:
    values = [h.solve_ms for s in sessions for h in s.history if h.kind == "apply"]
    return Metric(
        value=round(sum(values) / len(values), 1) if values else None,
        numerator=sum(values),
        denominator=len(values),
        unit="ms",
        definition="이번 달 계획에 적용된 재최적화(apply)의 계산 시간 평균",
        reason=None if values else "이번 달 계획에 적용된 재계산 이력이 없다",
    )


def _loss_gap(sessions: list[PlanSession]) -> Metric:
    gaps = [
        i.inspected_loss_rate - i.planned_loss_rate for s in sessions for i in s.arrival_inspections
    ]
    return Metric(
        value=round(sum(gaps) / len(gaps), 4) if gaps else None,
        denominator=len(gaps),
        unit="손실률 차이(검수 − 예측)",
        definition="도착 검수 손실률에서 계획 예측 손실률을 뺀 값의 로트 평균",
        reason=None if gaps else "이번 달 계획에 도착 검수 기록이 없다",
    )


@router.get("/metrics")
def platform_metrics(principal: PlatformPrincipal, month: MonthQuery = None) -> PlatformMetrics:
    target = _month(month)
    every = org_sessions(principal)
    sessions = [s for s in every if plan_month(s) == target]
    rows = [
        r for s in sessions for r in adjusted_settlements(s.plan, s.arrival_inspections, s.scenario)
    ]
    assignments = [a for s in sessions for a in s.plan.assignments]
    emissions = avoided_emissions((s.scenario, s.plan) for s in sessions)
    statuses = _order_statuses([s.plan_id for s in sessions])
    return PlatformMetrics(
        month=target,
        month_basis="계획의 첫 출항일(KST) 기준 월. 쓴 항차가 없으면 계획 생성일",
        plan_count=len(sessions),
        shipment_count=len(assignments),
        shipment_pallets=sum(a.pallets for a in assignments),
        sea_pallets=sum(a.pallets for a in assignments if a.mode == "sea"),
        air_pallets=sum(a.pallets for a in assignments if a.mode == "air"),
        reefer_containers=sum(1 for s in sessions for c in s.plan.containers if c.used),
        average_fill=_fill(sessions),
        shipper_savings_krw=sum(r.savings for r in rows),
        platform_fee_krw=sum(r.fee for r in rows),
        performance_fee_krw=sum(r.performance_fee for r in rows),
        usage_fee_krw=sum(r.usage_fee for r in rows),
        fee_before_refund_krw=sum(r.fee_before_refund for r in rows),
        quality_refund_krw=sum(r.quality_refund for r in rows),
        air_avoided_pallets=emissions.air_avoided_pallets,
        air_avoided_definition=(
            "혼자면 항공이었을 파렛트(단독 항공 기준선 품목, 이용료 품목 제외) 중 해상으로 간 것"
        ),
        order_confirmation_rate=_rate(
            sum(1 for s in statuses if s in CONFIRMED_ORDERS),
            len(statuses),
            "이번 달 계획의 실행 지시 중 포워더 확정(forwarder_confirmed)·실행(executed) 비율",
            "이번 달 계획에 실행 지시가 없다",
        ),
        plan_confirmation_rate=_rate(
            sum(1 for s in sessions if s.status == "committed"),
            len(sessions),
            "이번 달 계획 중 화주 확인 대기(pending_shipper_confirmation·pending_confirmation)가 아닌 확정(committed) 비율",
            "이번 달 계획이 없다",
        ),
        shipper_reuse_rate=_reuse(target, sessions, every),
        average_replan_ms=_replan_ms(sessions),
        average_arrival_loss_gap=_loss_gap(sessions),
        co2=emissions,
        regions=_regions(sessions),
    )


def _empty_slot_definition(total: int, tariff: int) -> str:
    return (
        "쓴 컨테이너 전체 빈칸 비용 = 컨테이너 비용 × 빈 파렛트 ÷ 용량. "
        f"쓴 컨테이너 전체 빈칸 비용 {total:,}원 중 정산표 품목 기준 빈칸 몫 {tariff:,}원은 "
        f"정산 차액(settlement_gap)에, 운영비 기준 화주 몫 {total - tariff:,}원은 해당 화주 "
        "청구서에 이미 반영돼 따로 빼지 않는 참고 항목"
    )


def _refunded(session: PlanSession) -> Plan:
    fees = {
        r.shipper_id: r.fee
        for r in adjusted_settlements(session.plan, session.arrival_inspections, session.scenario)
    }
    return session.plan.model_copy(
        update={
            "settlements": [
                s.model_copy(update={"fee": fees.get(s.shipper_id, s.fee)})
                for s in session.plan.settlements
            ]
        }
    )


def _business_line(
    sessions: list[PlanSession],
    share: float,
    label: str,
    ops_cost_per_voyage: int,
    variable_cost_per_pallet: int,
) -> BusinessLine:
    plans = []
    voyage_ids: set[str] = set()
    for session in sessions:
        voyages = voyage_economics(
            session.scenario,
            _refunded(session),
            share,
            ops_cost_per_voyage,
            variable_cost_per_pallet,
        )
        voyage_ids.update(v.voyage_id for v in voyages)
        plans.append(
            PlanEconomics(
                plan_id=session.plan_id,
                departure=departure(session),
                voyages=voyages,
                fee_revenue=sum(v.fee_revenue for v in voyages),
                contribution_margin=sum(v.contribution_margin for v in voyages),
                empty_slot_cost=empty_slot_cost(session.plan),
                tariff_empty_slot_cost=tariff_empty_slot_cost(session.plan),
                tariff_operational_gap=session.plan.total_shared_cost
                - session.plan.total_operational_cost,
            )
        )
    every = [v for p in plans for v in p.voyages]
    pallets = sum(v.pallets for v in every)
    contribution = sum(v.contribution_margin for v in every)
    fixed = ops_cost_per_voyage * len(voyage_ids)
    count = len(voyage_ids)
    annual_fixed = ops_cost_per_voyage * VOYAGES_PER_YEAR
    annual = round(contribution / count * VOYAGES_PER_YEAR) if count else None
    gap = sum(p.tariff_operational_gap for p in plans)
    annual_gap = round(gap / count * VOYAGES_PER_YEAR) if count else None
    annual_net = annual - annual_fixed if annual is not None else None
    per_pallet = contribution / pallets if pallets else 0
    breakeven = round(ops_cost_per_voyage / per_pallet, 1) if per_pallet > 0 else None
    return BusinessLine(
        forwarder_share=share,
        label=label,
        plans=plans,
        voyage_count=count,
        pallets=pallets,
        fee_revenue=sum(v.fee_revenue for v in every),
        forwarder_cost=sum(v.forwarder_share for v in every),
        variable_cost=sum(v.variable_cost for v in every),
        contribution_margin=contribution,
        fixed_cost_allocation=fixed,
        platform_net=contribution - fixed,
        settlement_gap=gap,
        platform_net_with_settlement=contribution - fixed + gap,
        annual_contribution=annual,
        annual_fixed_cost=annual_fixed,
        annual_net=annual_net,
        annual_settlement_gap=annual_gap,
        annual_net_with_settlement=annual_net + annual_gap
        if annual_net is not None and annual_gap is not None
        else None,
        breakeven_pallets_per_voyage=breakeven,
        remaining_pallets_per_voyage=round(max(0.0, breakeven - pallets / count), 1)
        if breakeven is not None and count
        else None,
    )


@router.get("/business")
def platform_business(
    principal: PlatformPrincipal,
    month: MonthQuery = None,
    sensitivity_forwarder_share: float = Query(FORWARDER_SENSITIVITY_SHARE, ge=0, le=1),
    ops_cost_per_voyage: int = Query(DEFAULT_OPS_COST_PER_VOYAGE, ge=0, le=MAX_OPS_COST_PER_VOYAGE),
    variable_cost_per_pallet: int = Query(0, ge=0, le=MAX_VARIABLE_COST_PER_PALLET),
) -> PlatformBusiness:
    target = _month(month)
    sessions = [s for s in org_sessions(principal) if plan_month(s) == target]
    lines = [
        _business_line(
            sessions,
            share,
            label,
            ops_cost_per_voyage,
            variable_cost_per_pallet,
        )
        for share, label in (
            (DEFAULT_FORWARDER_SHARE, f"기준선({FORWARDER_SHARE_SOURCE})"),
            (sensitivity_forwarder_share, f"민감도({FORWARDER_SHARE_SENSITIVITY_LABEL})"),
        )
    ]
    empty_total = sum(p.empty_slot_cost for p in lines[0].plans)
    tariff_empty = sum(p.tariff_empty_slot_cost for p in lines[0].plans)
    return PlatformBusiness(
        month=target,
        plan_count=len(sessions),
        assumptions=[
            Assumption(
                key="ops_cost_per_voyage",
                value=ops_cost_per_voyage,
                unit="원/항차",
                source=f"가정: {PROPOSAL} 8-1 고정비 연 {ANNUAL_FIXED_COST:,}원 ÷ 연 "
                f"{VOYAGES_PER_YEAR}항차(주 1회)"
                if ops_cost_per_voyage == DEFAULT_OPS_COST_PER_VOYAGE
                else "쿼리로 넣은 가정",
            ),
            Assumption(
                key="variable_cost_per_pallet",
                value=variable_cost_per_pallet,
                unit="원/파렛트",
                source="가정: 항차마다 드는 플랫폼 변동비. 기본 0원"
                if variable_cost_per_pallet == 0
                else "쿼리로 넣은 가정",
            ),
            Assumption(
                key="voyages_per_year",
                value=VOYAGES_PER_YEAR,
                unit="항차/년",
                source="가정: 주 1회 출항. 연 환산 = 항차당 공헌이익 평균 × 연 항차",
            ),
        ],
        empty_slot_cost=empty_total,
        tariff_empty_slot_cost=tariff_empty,
        empty_slot_definition=_empty_slot_definition(empty_total, tariff_empty),
        tariff_operational_gap=sum(p.tariff_operational_gap for p in lines[0].plans),
        tariff_operational_definition=(
            "화주 정산액(total_shared_cost) − 운영비(total_operational_cost). "
            "음수면 정산 단가가 실제 운영비를 못 덮어 플랫폼(혼적 주선자)이 진다. "
            "각 line 의 settlement_gap 으로 들어가 platform_net_with_settlement·"
            "annual_net_with_settlement 에 더한다. "
            "platform_net·annual_net 은 이 차이를 반영하지 않은 값"
        ),
        lines=lines,
    )


@router.get("/business/years", dependencies=[Depends(analysis_rate)])
def platform_business_years(
    principal: PlatformPrincipal,
    scenario_id: str = "default",
    years: int = Query(5, ge=1, le=5),
    fee_rate: float | None = Query(None, ge=0, le=1),
    base_fee_per_pallet: int = Query(DEFAULT_BASE_FEE_PER_PALLET, ge=0, le=MAX_BASE_FEE_PER_PALLET),
    base_fee_from_year: int = Query(DEFAULT_BASE_FEE_FROM_YEAR, ge=1, le=6),
    sensitivity_forwarder_share: float = Query(FORWARDER_SENSITIVITY_SHARE, ge=0, le=1),
    fixed_cost: int = Query(ANNUAL_FIXED_COST, ge=0, le=MAX_ANNUAL_AMOUNT),
    mvp_cash_fixed_cost: int = Query(MVP_CASH_FIXED_COST, ge=0, le=MAX_ANNUAL_AMOUNT),
    subsidies: Annotated[list[int] | None, Query(max_length=5)] = None,
    volume_scale: float = Query(1.0, gt=0, le=20),
) -> BusinessYears:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    if subsidies is not None and any(v < 0 or v > MAX_ANNUAL_AMOUNT for v in subsidies):
        raise HTTPException(
            status_code=422, detail=f"지원금은 0 이상 {MAX_ANNUAL_AMOUNT:,} 이하여야 합니다"
        )
    with analysis_slot():
        result = business_years(
            scenario,
            years=years,
            fee_rate=fee_rate,
            base_fee_per_pallet=base_fee_per_pallet,
            base_fee_from_year=base_fee_from_year,
            sensitivity_share=sensitivity_forwarder_share,
            fixed_cost=fixed_cost,
            mvp_cash_fixed_cost=mvp_cash_fixed_cost,
            subsidies=subsidies,
            volume_scale=volume_scale,
        )
    if result is None:
        raise HTTPException(
            status_code=422, detail="시즌 물량 품목(딸기·포도)이 없는 시나리오입니다"
        )
    return result
