from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.planning.compare import AirSeaComparison, compare_air_sea
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.planning.store import store

router = APIRouter(prefix="/compare", tags=["compare"])


class AirSeaRequest(BaseModel):
    scenario_id: str = "default"
    lot_id: str
    plan_id: str | None = None


@router.post("/air-vs-sea")
def air_vs_sea(body: AirSeaRequest) -> AirSeaComparison:
    if body.plan_id is not None:
        session = store.get(body.plan_id)
        if session is None:
            raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
        scenario, plan = session.scenario, session.plan
    else:
        scenario = get_scenario(body.scenario_id)
        if scenario is None:
            raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
        plan = build_plan(scenario).plan
    try:
        return compare_air_sea(scenario, plan, body.lot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="로트를 찾을 수 없습니다") from exc
