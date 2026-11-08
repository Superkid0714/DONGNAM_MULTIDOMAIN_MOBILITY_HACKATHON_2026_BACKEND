from fastapi import APIRouter, HTTPException

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.intake.models import IntakePreview, IntakeRequest
from freshswitch_backend.intake.service import IntakeError, preview_lot
from freshswitch_backend.lots import LotConflict, Reservation, lot_store
from freshswitch_backend.planning.store import store

router = APIRouter(prefix="/intake", tags=["intake"])


def intake_error(exc: IntakeError) -> HTTPException:
    return HTTPException(status_code=422, detail=[e.model_dump() for e in exc.errors])


def build_preview(
    body: IntakeRequest, reservation: Reservation | None = None
) -> tuple[IntakePreview, str]:
    plan = None
    if body.plan_id is not None:
        session = store.get(body.plan_id)
        if session is None:
            raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
        scenario, plan = session.scenario, session.plan
    else:
        scenario = get_scenario(body.scenario_id)
        if scenario is None:
            raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    reservation = reservation or lot_store.reservation()
    try:
        preview = preview_lot(
            scenario,
            body,
            plan,
            taken_ids=reservation.taken,
            reserved_ids=reservation.reserved,
        )
    except IntakeError as exc:
        raise intake_error(exc) from exc
    return preview, scenario.id


def register(body: IntakeRequest) -> IntakePreview:
    built: list[IntakePreview] = []

    def build(reservation: Reservation):
        preview, scenario_id = build_preview(body, reservation)
        built.append(preview)
        return preview.lot, scenario_id, preview.resolved.packhouse_id

    try:
        registered = lot_store.create(build)
    except LotConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return built[-1].model_copy(
        update={
            "registered": registered,
            "next_step": (
                "등록된 로트는 POST /api/plans 에 lot_ids 또는 include_registered=true 로 넣어 "
                "계획에 포함한다. plan_id가 있으면 event를 POST /api/plans/{plan_id}/events 에 보낸다"
            ),
        }
    )


@router.post("/lots")
def intake_lot(body: IntakeRequest) -> IntakePreview:
    if body.save:
        return register(body)
    return build_preview(body)[0]
