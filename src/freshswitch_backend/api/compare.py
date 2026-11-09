from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from freshswitch_backend.api.analysis import PLAN_REQUIRED, plan_session
from freshswitch_backend.api.auth import OptionalPrincipal, Principal, app_settings
from freshswitch_backend.api.compute_limits import analysis_rate, analysis_slot
from freshswitch_backend.api.saas_access import plan_for
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.persistence.tables import DEMO_ORG
from freshswitch_backend.planning.compare import AirSeaComparison, compare_air_sea
from freshswitch_backend.planning.robust.display import display_plan
from freshswitch_backend.planning.service import build_plan

router = APIRouter(prefix="/compare", tags=["compare"])

LOT_NOT_FOUND = "로트를 찾을 수 없습니다"
FORWARDER_FORBIDDEN = "포워더는 항공·해상 비용 비교를 사용할 수 없습니다"
SHIPPER_PLAN_REQUIRED = "화주는 자기 계획의 출하만 비교할 수 있습니다"


class AirSeaRequest(BaseModel):
    scenario_id: str = "default"
    lot_id: str
    plan_id: str | None = None
    truck_departure_adjustment_h: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    air_fuel_surcharge_per_kg: float | None = Field(default=None, ge=0, allow_inf_nan=False)


def _shipper_source(body: AirSeaRequest, principal: Principal) -> tuple[Scenario, Plan]:
    if body.plan_id is None:
        raise HTTPException(status_code=403, detail=SHIPPER_PLAN_REQUIRED)
    access = plan_for(body.plan_id, principal)
    scenario = access.session.scenario
    lot = next((x for x in scenario.lots if x.id == body.lot_id), None)
    if lot is None or lot.shipper_id not in access.viewer.shipper_ids:
        raise HTTPException(status_code=404, detail=LOT_NOT_FOUND)
    return scenario, access.session.plan


def _source(body: AirSeaRequest, principal: Principal | None, saas: bool) -> tuple[Scenario, Plan]:
    if saas and principal is None:
        raise HTTPException(status_code=403, detail="금액 분석은 플랫폼 조직만 사용할 수 있습니다")
    if principal is not None and principal.org_type == "shipper":
        return _shipper_source(body, principal)
    if principal is not None and principal.org_type not in {"platform", "forwarder"}:
        raise HTTPException(status_code=403, detail=FORWARDER_FORBIDDEN)
    if saas and body.plan_id is None:
        raise HTTPException(status_code=422, detail=PLAN_REQUIRED)
    if body.plan_id is not None:
        session = plan_session(body.plan_id, principal.org_id if principal else DEMO_ORG)
        return session.scenario, session.plan
    scenario = get_scenario(body.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    with analysis_slot():
        return scenario, build_plan(scenario).plan


@router.post("/air-vs-sea", dependencies=[Depends(analysis_rate)])
def air_vs_sea(
    body: AirSeaRequest, request: Request, principal: OptionalPrincipal
) -> AirSeaComparison:
    scenario, plan = _source(body, principal, app_settings(request).saas_mode)
    scenario = scenario.model_copy(deep=True)
    if body.truck_departure_adjustment_h is not None:
        for voyage in scenario.voyages:
            voyage.truck_departure_adjustment_h = body.truck_departure_adjustment_h
    if body.air_fuel_surcharge_per_kg is not None:
        scenario.cost_params.air_fuel_surcharge_per_kg_krw.value = body.air_fuel_surcharge_per_kg
        scenario.cost_params.air_fuel_surcharge_per_kg_krw.source = (
            "견적 요청에 명시한 kg당 유류할증료; 실시간 항공사 요율 확인값 아님"
        )
        scenario.cost_params.air_fuel_surcharge_by_month = {}
    try:
        result = compare_air_sea(scenario, plan, body.lot_id)
        projected = display_plan(scenario, plan)
        row = next((s for s in projected.settlements if s.shipper_id == result.shipper_id), None)
        if row is not None and row.quote_assumptions != next(
            (s.quote_assumptions for s in plan.settlements if s.shipper_id == result.shipper_id), []
        ):
            for key in (
                "air_cost",
                "shared_cost",
                "fee",
                "shipper_net_saving",
                "savings_vs_air_pct",
                "sea_solo_cost",
                "savings_vs_solo_pct",
            ):
                setattr(result, key, getattr(row, key))
            result.assumptions = [*result.assumptions, *row.quote_assumptions]
            lot = next(l for l in scenario.lots if l.id == body.lot_id)
            own = [a for a in plan.assignments if a.shipper_id == lot.shipper_id]
            fraction = lot.pallets / sum(a.pallets for a in own)
            air_total = round(row.air_cost * fraction)
            result.air.cost = result.air.cost.model_copy(
                update={
                    "freight": air_total,
                    "inland": 0,
                    "handling": 0,
                    "collection": 0,
                    "storage": 0,
                    "loss_risk": 0,
                    "total": air_total,
                }
            )
            if result.sea.in_plan and result.sea.cost is not None:
                sea_total = round(row.shared_cost * fraction)
                result.sea.cost = result.air.cost.model_copy(
                    update={"freight": sea_total, "total": sea_total}
                )
                result.sea_savings = air_total - sea_total
                result.cheaper = "sea" if sea_total < air_total else "air"
        lot = next(l for l in scenario.lots if l.id == body.lot_id)
        if lot.product_id == "strawberry" and lot.destination == "HKHKG" and result.sea.feasible:
            from freshswitch_backend.planning.robust.display import NOTICE, display_lot_amounts

            values = display_lot_amounts(
                scenario,
                lot,
                result.sea.container_id,
                result.sea.container_pallets_after,
                result.sea.pull_forward_days,
            )
            if values is not None:
                air, sea = values
                from freshswitch_backend.domain.models import CostBreakdown

                result.air.cost = CostBreakdown(freight=air, total=air)
                result.sea.cost = CostBreakdown(freight=sea, total=sea)
                result.sea_savings = air - sea
                result.cheaper = "sea" if sea < air else "air"
                result.assumptions.append(NOTICE)
                if result.quote_lot_ids == [lot.id]:
                    result.air_cost, result.shared_cost = air, sea
                    result.shipper_net_saving = air - sea - result.fee
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=LOT_NOT_FOUND) from exc
