from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select

from freshswitch_backend.api.auth import OptionalPrincipal, app_settings, current_org_id
from freshswitch_backend.api.computation import (
    compute_replan,
    explanation_budget,
    request_deadline,
)
from freshswitch_backend.api.compute_limits import check_lot_count, compute_slot, replan_rate
from freshswitch_backend.api.intake import intake_today
from freshswitch_backend.api.saas_access import plan_for
from freshswitch_backend.api.vessels import (
    VesselClientFactory,
    fetch_busan_sourced,
    vessel_client_factory,
)
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.inputs import FiniteFloat
from freshswitch_backend.domain.models import Lot, Scenario
from freshswitch_backend.execution.models import (
    ConfirmRequest,
    ExecutionOrder,
    RequestResultBody,
)
from freshswitch_backend.execution.numbers import entered_numbers
from freshswitch_backend.execution.orders import saas_requests, without_saas_basis
from freshswitch_backend.forecast.observations import planned_voyages, record_schedule_check
from freshswitch_backend.intake.service import date_range_errors
from freshswitch_backend.intake.tenants import (
    eligible_cargo_scenario,
    load_catalog,
    tenant_scenario,
)
from freshswitch_backend.integrations import (
    MAX_GAP_HOURS,
    ScheduleChange,
    VesselMovement,
    detect_schedule_changes,
    origin_provenance,
    provided_provenance,
)
from freshswitch_backend.integrations.mof_provider import DataSource
from freshswitch_backend.lots import ANY_ORG, LotFilter, LotUnavailable, RegisteredLot, lot_store
from freshswitch_backend.notifications import notify_order_confirmation
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import same_sandbox
from freshswitch_backend.persistence.tables import DEMO_ORG, LotRow, OrganizationRow
from freshswitch_backend.planning.store import (
    ContainerBooked,
    HistoryEntry,
    IdempotencyMismatch,
    OrderInputError,
    PlanSession,
    PlanSummary,
    SessionConflict,
    store,
)
from freshswitch_backend.quality import learning_scope, quality_history, with_learned_loss
from freshswitch_backend.replan import (
    AddLotEvent,
    DelayEvent,
    Event,
    ReplanResult,
    executed_dispatches,
    replan,
)
from freshswitch_backend.replan.explanation import explain_replan, llm_enabled
from freshswitch_backend.replan.models import AlternativeId


def _operational_access(request: Request, principal: OptionalPrincipal) -> None:
    if (
        principal is not None
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and principal.org_type != "forwarder"
    ):
        raise HTTPException(status_code=403, detail="운송 계획은 포워더 조직이 관리합니다")


router = APIRouter(prefix="/plans", tags=["plans"], dependencies=[Depends(_operational_access)])

OrgId = Annotated[str, Depends(current_org_id)]
INLINE_LOTS_FORBIDDEN = "SaaS 계획은 등록 출하(lot_ids)로만 만들 수 있습니다"
INLINE_ADD_LOT_FORBIDDEN = (
    "SaaS 계획에는 등록 출하만 추가할 수 있습니다. "
    "POST /saas/forwarder/plans/{plan_id}/add-lot 에 lot_id를 보내세요"
)


class CreatePlanRequest(BaseModel):
    scenario_id: str = "default"
    use_learned_loss: bool = False
    lots: list[Lot] = Field(default_factory=list)
    lot_ids: list[str] = Field(default_factory=list)
    include_registered: bool = False
    forwarder_org_id: str | None = Field(default=None, min_length=1, max_length=64)


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


MAX_SCHEDULE_HOURS = 240.0


class ScheduleCheckRequest(BaseModel):
    movements: list[VesselMovement] | None = None
    threshold_hours: FiniteFloat = Field(default=3.0, ge=0, le=MAX_SCHEDULE_HOURS)
    max_gap_hours: FiniteFloat = Field(default=MAX_GAP_HOURS, gt=0, le=MAX_SCHEDULE_HOURS)


class ScheduleCheckResponse(BaseModel):
    plan_id: str
    source: str
    checked_voyage_ids: list[str]
    movement_count: int
    changes: list[ScheduleChange]
    events: list[DelayEvent]
    data_source: DataSource
    fetched_at: datetime | None = None
    snapshot_at: datetime | None = None
    snapshot_name: str | None = None
    demo_mixed: bool = False
    source_note: str


def _session(plan_id: str, org_id: str = DEMO_ORG) -> PlanSession:
    session = store.get(plan_id, org_id=org_id)
    if session is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    return session


def _fingerprint(body: BaseModel) -> str:
    return body.model_dump_json(exclude={"idempotency_key"})


def _replayed(
    plan_id: str,
    key: str | None,
    kind: str,
    body: BaseModel,
    model=None,
    org_id: str = DEMO_ORG,
):
    if key is None:
        return None
    try:
        return store.idempotent(plan_id, key, kind, _fingerprint(body), model, org_id=org_id)
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _remember(
    plan_id: str, key: str | None, kind: str, body: BaseModel, value, org_id: str = DEMO_ORG
):
    if key is None:
        return value
    try:
        return store.remember(plan_id, key, kind, _fingerprint(body), value, org_id=org_id)
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _lot_errors(scenario: Scenario, lots: list[Lot]) -> list[str]:
    products = {p.id for p in scenario.products}
    shippers = {s.id for s in scenario.shippers}
    ports = {p.id for p in scenario.ports}
    packhouses = {p.id for p in scenario.packhouses}
    capacity = max((c.capacity_pallets for c in scenario.containers), default=20)
    errors: list[str] = []
    for lot in lots:
        if lot.product_id not in products:
            errors.append(f"{lot.id}: 시나리오에 없는 품목입니다: {lot.product_id}")
        if lot.shipper_id not in shippers:
            errors.append(f"{lot.id}: 시나리오에 없는 화주입니다: {lot.shipper_id}")
        if lot.packhouse_id is not None and lot.packhouse_id not in packhouses:
            errors.append(f"{lot.id}: 시나리오에 없는 출하 장소입니다: {lot.packhouse_id}")
        if lot.destination not in ports:
            errors.append(f"{lot.id}: 시나리오에 없는 항구입니다: {lot.destination}")
        if lot.pallets > capacity:
            errors.append(f"{lot.id}: 컨테이너 1대 용량({capacity}파렛트)을 넘습니다")
        if lot.sell_by_date < lot.ready_date:
            errors.append(f"{lot.id}: 판매기한이 출하 가능일보다 빠릅니다")
    return errors


def _date_errors(scenario: Scenario, lots: list[Lot]) -> list[str]:
    today = intake_today()
    return [
        f"{lot.id}: {error.message}"
        for lot in lots
        for error in date_range_errors(
            scenario, today, ready_date=lot.ready_date, sell_by_date=lot.sell_by_date
        )
    ]


def _shipper_org_lots(lots: list[RegisteredLot], peer_of: str) -> list[RegisteredLot]:
    if not lots:
        return []
    with database.begin() as db:
        owned = set(
            db.scalars(
                select(LotRow.id)
                .join(OrganizationRow, OrganizationRow.id == LotRow.shipper_org_id)
                .where(
                    LotRow.id.in_([lot.id for lot in lots]),
                    OrganizationRow.type == "shipper",
                    same_sandbox(OrganizationRow.sandbox, peer_of),
                )
            )
        )
    return [lot for lot in lots if lot.id in owned]


def _with_lots(
    scenario: Scenario,
    body: CreatePlanRequest,
    lot_org: str = DEMO_ORG,
    registered_only: bool = False,
    peer_of: str | None = None,
) -> tuple[Scenario, list[str]]:
    registered = lot_store.get_many(list(dict.fromkeys(body.lot_ids)), org_id=lot_org)
    if peer_of is not None:
        registered = _shipper_org_lots(registered, peer_of)
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
    taken = [lot.id for lot in registered if lot.status != "registered"]
    if registered_only and taken:
        raise HTTPException(
            status_code=409,
            detail=f"이미 계획에 들어갔거나 취소된 출하입니다: {', '.join(taken)}",
        )
    if body.include_registered:
        chosen = {lot.id for lot in registered}
        extra = [
            lot
            for lot in lot_store.search(
                LotFilter(scenario_id=scenario.id, limit=10_000), org_id=lot_org
            )
            if lot.status == "registered" and lot.id not in chosen
        ]
        registered += _shipper_org_lots(extra, peer_of) if peer_of is not None else extra
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
    errors = [*_lot_errors(scenario, added), *_date_errors(scenario, body.lots)]
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    merged = scenario.model_copy(update={"lots": [*scenario.lots, *added]})
    return merged, [lot.id for lot in registered]


def _base_scenario(scenario_id: str) -> Scenario:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    return scenario


def tenant_plan_scenario(body: CreatePlanRequest, org_id: str) -> tuple[Scenario, list[str]]:
    if body.lots:
        raise HTTPException(status_code=422, detail=INLINE_LOTS_FORBIDDEN)
    check_lot_count(len(dict.fromkeys(body.lot_ids)))
    base = _base_scenario(body.scenario_id)
    scenario = tenant_scenario(base, load_catalog(ANY_ORG, base, peer_of=org_id))
    with database.begin() as db:
        operator = db.get(OrganizationRow, org_id)
        if operator is not None and operator.type == "forwarder":
            scenario = eligible_cargo_scenario(scenario, org_id)
    from freshswitch_backend.persistence.resources import resource_scenario

    scenario = resource_scenario(scenario, org_id, body.forwarder_org_id, store.clock())
    scenario, registered_ids = _with_lots(
        scenario, body, lot_org=ANY_ORG, registered_only=True, peer_of=org_id
    )
    check_lot_count(len(scenario.lots))
    if not registered_ids:
        raise HTTPException(status_code=422, detail="계획에 넣을 등록 출하가 없습니다")
    if body.use_learned_loss:
        scenario = with_learned_loss(scenario, scope=learning_scope(org_id))
    return _free_containers(scenario, org_id), registered_ids


def _free_containers(scenario: Scenario, org_id: str) -> Scenario:
    booked = store.booked_containers(org_id, scenario.id)
    if not booked:
        return scenario
    free = [c for c in scenario.containers if c.id not in booked]
    if not free:
        raise HTTPException(
            status_code=422,
            detail=(
                "이 시나리오의 컨테이너가 모두 다른 계획에 배정되어 있습니다"
                f"({', '.join(sorted(booked))}). 기존 계획을 지우거나 초기화한 뒤 다시 만드세요"
            ),
        )
    return scenario.model_copy(update={"containers": free})


def create_tenant_plan(body: CreatePlanRequest, org_id: str) -> PlanSession:
    scenario, registered_ids = tenant_plan_scenario(body, org_id)
    try:
        with compute_slot():
            return store.create(
                scenario, registered_ids, org_id=org_id, lot_org_id=ANY_ORG, exclusive=True,
                quantity_policy_v1=True,
            )
    except LotUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail=f"이미 계획에 들어갔거나 그사이 바뀐 출하입니다: {exc}. 다시 시도하세요",
        ) from exc
    except ContainerBooked as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "그사이 다른 계획이 컨테이너를 배정했습니다: "
                f"{', '.join(exc.container_ids)}. 계획을 다시 만드세요"
            ),
        ) from exc


@router.post("", status_code=201)
def create_plan(
    body: CreatePlanRequest, request: Request, principal: OptionalPrincipal
) -> PlanSession:
    scenario = _base_scenario(body.scenario_id)
    if body.lots and app_settings(request).saas_mode:
        raise HTTPException(status_code=422, detail=INLINE_LOTS_FORBIDDEN)
    if principal is not None:
        if principal.org_type != "forwarder":
            raise HTTPException(status_code=403, detail="계획은 포워더 조직만 만들 수 있습니다")
        return create_tenant_plan(body, principal.org_id)
    scenario, registered_ids = _with_lots(scenario, body)
    if body.use_learned_loss:
        scenario = with_learned_loss(scenario)
    return store.create(scenario, registered_ids)


@router.get("")
def list_plans(
    org_id: OrgId,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlanSummary]:
    return store.summaries(limit, offset, org_id=org_id)


@router.get("/{plan_id}")
def read_plan(plan_id: str, org_id: OrgId, principal: OptionalPrincipal) -> PlanSession:
    if principal is not None and principal.org_type == "platform":
        return plan_for(plan_id, principal).session
    return _session(plan_id, org_id)


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(plan_id: str, org_id: OrgId) -> None:
    deleted = store.delete(plan_id, org_id=org_id)
    quality_history.prune_orphans()
    if not deleted:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")


def replan_inputs(session: PlanSession, org_id: str) -> dict:
    return {
        "dispatched": executed_dispatches(store.orders(session.plan_id, org_id=org_id)),
        "unavailable_containers": store.booked_containers(
            org_id, session.scenario.id, session.plan_id
        ),
    }


@router.post("/{plan_id}/events", dependencies=[Depends(replan_rate)])
async def post_event(
    plan_id: str,
    body: EventRequest,
    org_id: OrgId,
    llm: Annotated[bool | None, Query()] = None,
) -> ReplanResponse:
    if isinstance(body.event, AddLotEvent):
        if org_id != DEMO_ORG:
            raise HTTPException(status_code=422, detail=INLINE_ADD_LOT_FORBIDDEN)
        session = await run_in_threadpool(_session, plan_id, org_id)
        errors = _date_errors(session.scenario, [body.event.lot])
        if errors:
            raise HTTPException(status_code=422, detail=errors)
    return await replan_event(plan_id, body, org_id, llm)


async def replan_event(
    plan_id: str,
    body: EventRequest,
    org_id: str,
    llm: bool | None = None,
) -> ReplanResponse:
    deadline = request_deadline()
    session = await run_in_threadpool(_session, plan_id, org_id)
    replayed = await run_in_threadpool(
        _replayed, plan_id, body.idempotency_key, "events", body, ReplanResponse, org_id
    )
    if replayed is not None:
        return replayed
    inputs = await run_in_threadpool(replan_inputs, session, org_id)
    result = await compute_replan(
        replan,
        session.scenario,
        session.plan,
        body.event,
        air_locked=session.air_locked_lot_ids,
        routes=session.routes,
        now=store.clock(),
        deadline=deadline,
        **inputs,
    )
    from freshswitch_backend.planning.quantity_policy import annotate_for_policy

    annotate_for_policy(session, result)
    result.explanation = await explain_replan(
        session.scenario,
        session.plan,
        result,
        use_llm=llm_enabled(llm),
        timeout_s=explanation_budget(deadline),
    )
    pending = await run_in_threadpool(store.add_pending, session, result, org_id)
    response = ReplanResponse(
        **{name: getattr(result, name) for name in ReplanResult.model_fields},
        plan_id=plan_id,
        replan_id=pending.replan_id,
        base_version=pending.base_version,
    )
    return await run_in_threadpool(
        _remember, plan_id, body.idempotency_key, "events", body, response, org_id
    )


def apply_alternative(plan_id: str, body: ApplyRequest, org_id: str) -> PlanSession:
    _session(plan_id, org_id)
    key = body.idempotency_key
    try:
        session = store.apply(
            plan_id,
            body.alternative_id,
            body.replan_id,
            idempotency=(key, _fingerprint(body)) if key is not None else None,
            org_id=org_id,
        )
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="재최적화 결과 또는 대안을 찾을 수 없습니다"
        ) from exc
    return session


@router.post("/{plan_id}/apply")
def apply_and_notify(plan_id: str, body: ApplyRequest, org_id: OrgId) -> PlanSession:
    session = apply_alternative(plan_id, body, org_id)
    if org_id != DEMO_ORG and session.active_order_id is not None:
        order = store.order(plan_id, session.active_order_id, org_id=org_id)
        notify_order_confirmation(session, order, org_id, store.clock())
    return session


def _order_view(
    order: ExecutionOrder, session: PlanSession, org_id: str, request: Request
) -> ExecutionOrder:
    if org_id == DEMO_ORG and not app_settings(request).saas_mode:
        return without_saas_basis(order)
    numbers = entered_numbers(order.plan_id)
    requests = saas_requests(order.plan_id, order.requests, session.scenario, numbers=numbers)
    return order.model_copy(update={"requests": requests})


@router.get("/{plan_id}/orders")
def list_orders(
    plan_id: str,
    org_id: OrgId,
    request: Request,
    principal: OptionalPrincipal,
) -> list[ExecutionOrder]:
    if principal is not None and principal.org_type == "platform":
        org_id = plan_for(plan_id, principal).owner_org_id
    session = _session(plan_id, org_id)
    return [_order_view(o, session, org_id, request) for o in store.orders(plan_id, org_id=org_id)]


@router.get("/{plan_id}/orders/{order_id}")
def read_order(
    plan_id: str,
    order_id: str,
    org_id: OrgId,
    request: Request,
    principal: OptionalPrincipal,
) -> ExecutionOrder:
    if principal is not None and principal.org_type == "platform":
        org_id = plan_for(plan_id, principal).owner_org_id
    session = _session(plan_id, org_id)
    try:
        order = store.order(plan_id, order_id, org_id=org_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다") from exc
    return _order_view(order, session, org_id, request)


@router.post("/{plan_id}/orders/{order_id}/confirm")
def confirm_order(
    plan_id: str,
    order_id: str,
    body: ConfirmRequest,
    org_id: OrgId,
    principal: OptionalPrincipal,
) -> ExecutionOrder:
    _session(plan_id, org_id)
    if principal is not None:
        raise HTTPException(
            status_code=403,
            detail="로그인 상태에서는 대신 확정할 수 없습니다. "
            "화주는 화주 화면, 포워더는 배정된 포워더 화면에서 확정합니다",
        )
    try:
        return without_saas_basis(store.confirm(plan_id, order_id, body, org_id=org_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다") from exc
    except OrderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{plan_id}/orders/{order_id}/requests/{request_id}")
def answer_request(
    plan_id: str,
    order_id: str,
    request_id: str,
    body: RequestResultBody,
    org_id: OrgId,
    principal: OptionalPrincipal,
) -> ExecutionOrder:
    _session(plan_id, org_id)
    if principal is not None:
        raise HTTPException(
            status_code=403,
            detail="로그인 상태에서는 대신 회신을 기록할 수 없습니다. "
            "선사·터미널·항공사 회신은 배정된 포워더 화면에서 기록합니다",
        )
    try:
        return without_saas_basis(
            store.answer_request(plan_id, order_id, request_id, body, org_id=org_id)
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="실행 지시 또는 요청을 찾을 수 없습니다"
        ) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{plan_id}/history")
def read_history(plan_id: str, org_id: OrgId, principal: OptionalPrincipal) -> list[HistoryEntry]:
    if principal is not None and principal.org_type == "platform":
        org_id = plan_for(plan_id, principal).owner_org_id
    return _session(plan_id, org_id).history


@router.post("/{plan_id}/schedule-check")
async def schedule_check(
    plan_id: str,
    factory: Annotated[VesselClientFactory, Depends(vessel_client_factory)],
    org_id: OrgId,
    body: ScheduleCheckRequest | None = None,
    demo: Annotated[bool, Query()] = False,
) -> ScheduleCheckResponse:
    session = await run_in_threadpool(_session, plan_id, org_id)
    body = body or ScheduleCheckRequest()
    used = {c.voyage_id for c in session.plan.containers if c.used}
    voyages = [v for v in session.scenario.voyages if v.id in used] or session.scenario.voyages
    if body.movements is not None:
        movements, source, trusted = body.movements, "provided", False
        provenance = provided_provenance(len(movements))
    else:
        start = min(v.etd for v in voyages).date() - timedelta(days=1)
        end = max(v.etd for v in voyages).date() + timedelta(days=3)
        sourced = await fetch_busan_sourced(factory, start, end, "O", demo=demo)
        movements = sourced.feed.movements
        source, trusted = "해수부", not demo
        provenance = origin_provenance(sourced.origin)
    changes = detect_schedule_changes(
        planned_voyages(voyages),
        movements,
        threshold_hours=body.threshold_hours,
        max_gap_hours=body.max_gap_hours,
    )
    await run_in_threadpool(
        record_schedule_check,
        org_id,
        voyages,
        movements,
        trusted=trusted,
        plan_id=plan_id,
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
        **provenance.model_dump(),
    )
