from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from freshswitch_backend.api.analysis import PLAN_REQUIRED, plan_session
from freshswitch_backend.api.auth import OptionalPrincipal, Principal, app_settings
from freshswitch_backend.api.compute_limits import analysis_rate, analysis_slot
from freshswitch_backend.api.saas_access import plan_for
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.persistence.tables import DEMO_ORG
from freshswitch_backend.planning.compare import AirSeaComparison, compare_air_sea
from freshswitch_backend.planning.service import build_plan

router = APIRouter(prefix="/compare", tags=["compare"])

LOT_NOT_FOUND = "로트를 찾을 수 없습니다"
FORWARDER_FORBIDDEN = "포워더는 항공·해상 비용 비교를 사용할 수 없습니다"
SHIPPER_PLAN_REQUIRED = "화주는 자기 계획의 출하만 비교할 수 있습니다"


class AirSeaRequest(BaseModel):
    scenario_id: str = "default"
    lot_id: str
    plan_id: str | None = None


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
    try:
        return compare_air_sea(scenario, plan, body.lot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=LOT_NOT_FOUND) from exc
