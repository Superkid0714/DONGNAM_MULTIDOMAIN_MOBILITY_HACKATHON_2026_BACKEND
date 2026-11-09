from freshswitch_backend.domain.models import Assignment, BillingRule, Lot, Plan, Scenario
from freshswitch_backend.intake.models import FeasibilityHints, SavingsEstimate, SeaHint
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.settlement import billing_rule, charge, lot_settle_cost

TARGET_UTILIZATION = 0.85
BASELINE_NOTE = "기준선: 이 출하를 혼자 항공으로 보낼 때 비용(시나리오 정산 규칙)"
FINAL_NOTE = "확정 정산은 계획 생성 뒤 실제 적재·집하 경로로 다시 계산합니다"


def _rule(model: CostModel, lot: Lot) -> tuple[BillingRule, str]:
    billing = model.params.billing
    if billing_rule(model.params, lot.product_id) == "usage_fee":
        fee = f"{round(billing.usage_fee_per_pallet_krw.value):,}원"
        return (
            "usage_fee",
            f"파렛트 이용료 {fee} × 파렛트 수, 성과보수 없음({billing.usage_fee_source})",
        )
    rate = model.params.platform_fee_rate.value
    return "performance", f"절감액의 {rate:.0%} 성과보수(기획서 8-1)"


def _fee(model: CostModel, lot: Lot, billing: BillingRule, savings: int) -> tuple[int, int]:
    usage = billing == "usage_fee"
    bill = charge(
        model.params, billing, savings, 0 if usage else savings, lot.pallets if usage else 0
    )
    return bill.fee, bill.shipper_gain


def _load(model: CostModel, option: SeaHint, lot: Lot, plan: Plan | None) -> tuple[int, str]:
    capacity = model.containers[option.container_id].capacity_pallets
    if plan is not None and option.free_pallets is not None:
        loaded = capacity - option.free_pallets
        load = loaded + lot.pallets
        return load, (
            f"{option.container_id}에 이미 실린 {loaded}파렛트와 함께 적재"
            f"({load}/{capacity}파렛트)한다고 계산"
        )
    load = lot.pallets
    return load, (
        f"합승 실적재량 미확정: 요청 물량 {load}/{capacity}파렛트만으로 계산. "
        "계획이 확정되면 실제 적재량과 집하 경로로 재견적"
    )


def _option(plan: Plan | None, hints: FeasibilityHints) -> SeaHint | None:
    if plan is not None:
        return hints.earliest_feasible
    return next((o for o in hints.sea_options if o.feasible and o.pull_forward_days == 0), None)


def estimate_savings(
    scenario: Scenario,
    lot: Lot,
    plan: Plan | None,
    hints: FeasibilityHints,
) -> SavingsEstimate | None:
    model = CostModel(scenario)
    basis = "tariff" if model.tariff(lot) is not None else "operational"
    billing, rule = _rule(model, lot)
    baseline = round(model.settle_air_cost(lot).total)
    assumptions = [BASELINE_NOTE]
    option = _option(plan, hints)
    if option is not None:
        load, note = _load(model, option, lot, plan)
        assignment = Assignment(
            lot_id=lot.id,
            shipper_id=lot.shipper_id,
            product_id=lot.product_id,
            pallets=lot.pallets,
            mode="sea",
            container_id=option.container_id,
            voyage_id=option.voyage_id,
            pull_forward_days=option.pull_forward_days,
        )
        shared = round(lot_settle_cost(model, lot, assignment, {option.container_id: load}).total)
        savings = baseline - shared
        if savings > 0:
            fee, gain = _fee(model, lot, billing, savings)
            if basis == "operational":
                assumptions.append(
                    "컨테이너 운임·부대비는 파렛트 수 비율로 나누고, 집하비는 선과장→항만 거리로 추정"
                )
            else:
                assumptions.append("품목 파렛트당 정산표(운임·내륙·품질손실 위험)로 계산")
            if option.pull_forward_days > 0:
                assumptions.append(
                    f"출하를 {option.pull_forward_days}일 앞당긴다고 가정(조기 수확 손실 포함)"
                )
            return SavingsEstimate(
                mode="sea",
                container_id=option.container_id,
                voyage_id=option.voyage_id,
                pallets=lot.pallets,
                basis=basis,
                billing_rule=billing,
                rule=rule,
                baseline_cost=baseline,
                shared_cost=shared,
                savings=savings,
                savings_per_pallet=round(savings / lot.pallets),
                fee=fee,
                shipper_gain=gain,
                assumed_container_pallets=load,
                assumptions=[*assumptions, note, FINAL_NOTE],
            )
        assumptions.append(
            f"{option.container_id} 해상 분담비가 혼자 항공보다 커서 항공 배정을 예상"
        )
    elif hints.air.feasible:
        assumptions.append("실을 수 있는 해상 컨테이너가 없어 항공 배정을 예상")
    if not hints.air.feasible:
        return None
    return SavingsEstimate(
        mode="air",
        pallets=lot.pallets,
        basis=basis,
        billing_rule=billing,
        rule=rule,
        baseline_cost=baseline,
        shared_cost=baseline,
        savings=0,
        savings_per_pallet=0,
        fee=0,
        shipper_gain=0,
        assumptions=[*assumptions, "항공은 기준선과 같아 절감이 없습니다", FINAL_NOTE],
    )
