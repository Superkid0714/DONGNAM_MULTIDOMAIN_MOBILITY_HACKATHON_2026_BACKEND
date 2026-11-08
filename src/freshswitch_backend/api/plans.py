from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from freshswitch_backend.api.computation import (
    compute_replan,
    explanation_budget,
    request_deadline,
)
from freshswitch_backend.api.vessels import (
    VesselClientFactory,
    fetch_busan_movements,
    vessel_client_factory,
)
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Lot, Scenario
from freshswitch_backend.execution.cancellation import annotate_cancellations
from freshswitch_backend.execution.models import (
    ConfirmRequest,
    ExecutionOrder,
    RequestResultBody,
)
from freshswitch_backend.integrations import (
    MAX_GAP_HOURS,
    PlannedVoyage,
    ScheduleChange,
    VesselMovement,
    detect_schedule_changes,
)
from freshswitch_backend.lots import LotFilter, lot_store
from freshswitch_backend.planning.store import (
    HistoryEntry,
    IdempotencyMismatch,
    OrderInputError,
    PlanSession,
    PlanSummary,
    SessionConflict,
    store,
)
from freshswitch_backend.quality import with_learned_loss
from freshswitch_backend.replan import DelayEvent, Event, ReplanResult, replan
from freshswitch_backend.replan.explanation import explain_replan, llm_enabled
from freshswitch_backend.replan.models import AlternativeId

router = APIRouter(prefix="/plans", tags=["plans"])


class CreatePlanRequest(BaseModel):
    scenario_id: str = "default"
    use_learned_loss: bool = False
    lots: list[Lot] = Field(default_factory=list)
    lot_ids: list[str] = Field(default_factory=list)
    include_registered: bool = False


class EventRequest(BaseModel):
    event: Event
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ReplanResponse(ReplanResult):
    plan_id: str
    replan_id: str
    base_version: int


class ApplyRequest(BaseModel):
    alternative_id: AlternativeId
    replan_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ScheduleCheckRequest(BaseModel):
    movements: list[VesselMovement] | None = None
    threshold_hours: float = 3.0
    max_gap_hours: float = Field(default=MAX_GAP_HOURS, gt=0)


class ScheduleCheckResponse(BaseModel):
    plan_id: str
    source: str
    checked_voyage_ids: list[str]
    movement_count: int
    changes: list[ScheduleChange]
    events: list[DelayEvent]


def _session(plan_id: str) -> PlanSession:
    session = store.get(plan_id)
    if session is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    return session


def _fingerprint(body: BaseModel) -> str:
    return body.model_dump_json(exclude={"idempotency_key"})


def _replayed(plan_id: str, key: str | None, kind: str, body: BaseModel, model=None):
    if key is None:
        return None
    try:
        return store.idempotent(plan_id, key, kind, _fingerprint(body), model)
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _remember(plan_id: str, key: str | None, kind: str, body: BaseModel, value):
    if key is None:
        return value
    try:
        return store.remember(plan_id, key, kind, _fingerprint(body), value)
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _lot_errors(scenario: Scenario, lots: list[Lot]) -> list[str]:
    products = {p.id for p in scenario.products}
    shippers = {s.id for s in scenario.shippers}
    ports = {p.id for p in scenario.ports}
    capacity = max((c.capacity_pallets for c in scenario.containers), default=20)
    errors: list[str] = []
    for lot in lots:
        if lot.product_id not in products:
            errors.append(f"{lot.id}: 시나리오에 없는 품목입니다: {lot.product_id}")
        if lot.shipper_id not in shippers:
            errors.append(f"{lot.id}: 시나리오에 없는 화주입니다: {lot.shipper_id}")
        if lot.destination not in ports:
            errors.append(f"{lot.id}: 시나리오에 없는 항구입니다: {lot.destination}")
        if lot.pallets > capacity:
            errors.append(f"{lot.id}: 컨테이너 1대 용량({capacity}파렛트)을 넘습니다")
        if lot.sell_by_date < lot.ready_date:
            errors.append(f"{lot.id}: 판매기한이 출하 가능일보다 빠릅니다")
    return errors


def _with_lots(scenario: Scenario, body: CreatePlanRequest) -> tuple[Scenario, list[str]]:
    registered = lot_store.get_many(list(dict.fromkeys(body.lot_ids)))
    missing = sorted(set(body.lot_ids) - {lot.id for lot in registered})
    if missing:
        raise HTTPException(
            status_code=422, detail=f"등록되지 않은 로트입니다: {', '.join(missing)}"
        )
    foreign = [lot.id for lot in registered if lot.scenario_id != scenario.id]
    if foreign:
        raise HTTPException(
            status_code=422,
            detail=f"다른 시나리오에 등록된 로트입니다: {', '.join(foreign)}",
        )
    if body.include_registered:
        chosen = {lot.id for lot in registered}
        registered += [
            lot
            for lot in lot_store.search(LotFilter(scenario_id=scenario.id, limit=10_000))
            if lot.status == "registered" and lot.id not in chosen
        ]
    added = [*body.lots, *(lot.to_lot() for lot in registered)]
    if not added:
        return scenario, []
    seen = {lot.id for lot in scenario.lots}
    clashes: list[str] = []
    for lot in added:
        if lot.id in seen:
            clashes.append(lot.id)
        seen.add(lot.id)
    if clashes:
        raise HTTPException(
            status_code=422,
            detail=f"로트 ID가 겹칩니다(시나리오·등록·직접 입력 로트 중): {', '.join(clashes)}",
        )
    errors = _lot_errors(scenario, added)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    merged = scenario.model_copy(update={"lots": [*scenario.lots, *added]})
    return merged, [lot.id for lot in registered]


@router.post("", status_code=201)
def create_plan(body: CreatePlanRequest) -> PlanSession:
    scenario = get_scenario(body.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    scenario, registered_ids = _with_lots(scenario, body)
    if body.use_learned_loss:
        scenario = with_learned_loss(scenario)
    return store.create(scenario, registered_ids)


@router.get("")
def list_plans(
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlanSummary]:
    return store.summaries(limit, offset)


@router.get("/{plan_id}")
def read_plan(plan_id: str) -> PlanSession:
    return _session(plan_id)


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(plan_id: str) -> None:
    if not store.delete(plan_id):
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")


@router.post("/{plan_id}/events")
async def post_event(
    plan_id: str,
    body: EventRequest,
    llm: Annotated[bool | None, Query()] = None,
) -> ReplanResponse:
    deadline = request_deadline()
    session = await run_in_threadpool(_session, plan_id)
    replayed = await run_in_threadpool(
        _replayed, plan_id, body.idempotency_key, "events", body, ReplanResponse
    )
    if replayed is not None:
        return replayed
    result = await compute_replan(
        replan,
        session.scenario,
        session.plan,
        body.event,
        air_locked=session.air_locked_lot_ids,
        deadline=deadline,
    )
    annotate_cancellations(result, session.scenario, session.plan)
    result.explanation = await explain_replan(
        session.scenario,
        session.plan,
        result,
        use_llm=llm_enabled(llm),
        timeout_s=explanation_budget(deadline),
    )
    pending = await run_in_threadpool(store.add_pending, session, result)
    response = ReplanResponse(
        **{name: getattr(result, name) for name in ReplanResult.model_fields},
        plan_id=plan_id,
        replan_id=pending.replan_id,
        base_version=pending.base_version,
    )
    return await run_in_threadpool(
        _remember, plan_id, body.idempotency_key, "events", body, response
    )


@router.post("/{plan_id}/apply")
def apply_alternative(plan_id: str, body: ApplyRequest) -> PlanSession:
    _session(plan_id)
    key = body.idempotency_key
    try:
        return store.apply(
            plan_id,
            body.alternative_id,
            body.replan_id,
            idempotency=(key, _fingerprint(body)) if key is not None else None,
        )
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="재최적화 결과 또는 대안을 찾을 수 없습니다"
        ) from exc


@router.get("/{plan_id}/orders")
def list_orders(plan_id: str) -> list[ExecutionOrder]:
    _session(plan_id)
    return store.orders(plan_id)


@router.get("/{plan_id}/orders/{order_id}")
def read_order(plan_id: str, order_id: str) -> ExecutionOrder:
    _session(plan_id)
    try:
        return store.order(plan_id, order_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다") from exc


@router.post("/{plan_id}/orders/{order_id}/confirm")
def confirm_order(plan_id: str, order_id: str, body: ConfirmRequest) -> ExecutionOrder:
    _session(plan_id)
    try:
        return store.confirm(plan_id, order_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다") from exc
    except OrderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{plan_id}/orders/{order_id}/requests/{request_id}")
def answer_request(
    plan_id: str, order_id: str, request_id: str, body: RequestResultBody
) -> ExecutionOrder:
    _session(plan_id)
    try:
        return store.answer_request(plan_id, order_id, request_id, body)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="실행 지시 또는 요청을 찾을 수 없습니다"
        ) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{plan_id}/history")
def read_history(plan_id: str) -> list[HistoryEntry]:
    return _session(plan_id).history


@router.post("/{plan_id}/schedule-check")
async def schedule_check(
    plan_id: str,
    factory: Annotated[VesselClientFactory, Depends(vessel_client_factory)],
    body: ScheduleCheckRequest | None = None,
    demo: Annotated[bool, Query()] = False,
) -> ScheduleCheckResponse:
    session = await run_in_threadpool(_session, plan_id)
    body = body or ScheduleCheckRequest()
    used = {c.voyage_id for c in session.plan.containers if c.used}
    voyages = [v for v in session.scenario.voyages if v.id in used] or session.scenario.voyages
    if body.movements is not None:
        movements, source = body.movements, "provided"
    else:
        start = min(v.etd for v in voyages).date() - timedelta(days=1)
        end = max(v.etd for v in voyages).date() + timedelta(days=3)
        movements = await fetch_busan_movements(factory, start, end, "O", demo=demo)
        source = "해수부"
    changes = detect_schedule_changes(
        [PlannedVoyage(voyage_id=v.id, vessel_name=v.vessel_name, etd=v.etd) for v in voyages],
        movements,
        threshold_hours=body.threshold_hours,
        max_gap_hours=body.max_gap_hours,
    )
    events = [
        DelayEvent(
            voyage_id=change.voyage_id,
            new_etd=change.observed_etd,
            source="해수부",
        )
        for change in changes
    ]
    return ScheduleCheckResponse(
        plan_id=plan_id,
        source=source,
        checked_voyage_ids=[v.id for v in voyages],
        movement_count=len(movements),
        changes=changes,
        events=events,
    )
