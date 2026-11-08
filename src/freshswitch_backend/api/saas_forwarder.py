from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.saas_access import plan_for, visible_plans
from freshswitch_backend.api.saas_views import (
    ForwarderOrder,
    PlanListItem,
    forwarder_order_view,
    forwarders_of,
    plan_item,
    without_settlement,
)
from freshswitch_backend.coldchain.power_plan import power_plan
from freshswitch_backend.domain.inputs import Note
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.execution.card import (
    ContainerExecution,
    EnteredNumbers,
    ExecutionCard,
    ExecutionNumbersInput,
    build_execution_card,
)
from freshswitch_backend.execution.models import (
    ConfirmRequest,
    ExecutionOrder,
    RequestResultBody,
)
from freshswitch_backend.execution.numbers import (
    DuplicateContainerNo,
    entered_numbers,
    save_numbers,
)
from freshswitch_backend.execution.orders import genset_instructions, saas_requests
from freshswitch_backend.notifications import notify_order_committed
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PlanRow
from freshswitch_backend.planning.store import (
    OrderInputError,
    PlanSession,
    SessionConflict,
    store,
)

router = APIRouter(prefix="/saas/forwarder", tags=["saas-forwarder"])

ForwarderPrincipal = Annotated[Principal, Depends(require_role("forwarder"))]


class ForwarderDecision(BaseModel):
    accept: bool
    note: Note | None = None


def _order_view(
    order: ExecutionOrder, scenario: Scenario, numbers: dict[str, EnteredNumbers] | None = None
) -> ForwarderOrder:
    view = forwarder_order_view(order)
    entered = entered_numbers(order.plan_id) if numbers is None else numbers
    view.requests = saas_requests(order.plan_id, view.requests, scenario, numbers=entered)
    return view


def _card(session: PlanSession) -> ExecutionCard:
    card = build_execution_card(
        session.plan_id,
        session.version,
        session.scenario,
        session.plan,
        entered=entered_numbers(session.plan_id),
    )
    card.genset_instructions = genset_instructions(power_plan(session).candidates, card)
    return card


@router.get("/plans")
def list_plans(
    principal: ForwarderPrincipal,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlanListItem]:
    store.expire_due(visible_plans(principal))
    with database.begin() as db:
        rows = db.scalars(
            select(PlanRow)
            .where(visible_plans(principal))
            .order_by(PlanRow.created_at.desc(), PlanRow.seq.desc())
            .limit(limit)
            .offset(offset)
        )
        sessions = [PlanSession.model_validate(row.payload) for row in rows]
    forwarders = forwarders_of([s.plan_id for s in sessions])
    return [plan_item(s, forwarders.get(s.plan_id)) for s in sessions]


@router.get("/plans/{plan_id}")
def read_plan(plan_id: str, principal: ForwarderPrincipal) -> dict[str, Any]:
    session = plan_for(plan_id, principal).session
    payload = without_settlement(session)
    payload["execution_card"] = _card(session).model_dump(mode="json")
    return payload


@router.get("/plans/{plan_id}/execution-card")
def read_execution_card(plan_id: str, principal: ForwarderPrincipal) -> ExecutionCard:
    return _card(plan_for(plan_id, principal).session)


@router.put("/plans/{plan_id}/execution-card/containers/{container_id}")
def save_container_numbers(
    plan_id: str,
    container_id: str,
    body: ExecutionNumbersInput,
    principal: ForwarderPrincipal,
) -> ContainerExecution:
    session = plan_for(plan_id, principal).session
    active = {
        c.container_id
        for c in build_execution_card(
            session.plan_id, session.version, session.scenario, session.plan
        ).containers
    }
    if container_id not in active:
        raise HTTPException(status_code=404, detail="계획에 없는 컨테이너입니다")
    changes = body.model_dump(include=body.model_fields_set)
    if not changes:
        raise HTTPException(
            status_code=422, detail="booking_no·container_no·seal_no 중 하나 이상을 입력하세요"
        )
    try:
        save_numbers(plan_id, container_id, changes, active, principal.user_id, store.clock())
    except DuplicateContainerNo as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return next(c for c in _card(session).containers if c.container_id == container_id)


@router.get("/plans/{plan_id}/orders")
def list_orders(plan_id: str, principal: ForwarderPrincipal) -> list[ForwarderOrder]:
    access = plan_for(plan_id, principal)
    scenario = access.session.scenario
    numbers = entered_numbers(plan_id)
    return [
        _order_view(o, scenario, numbers) for o in store.orders(plan_id, org_id=access.owner_org_id)
    ]


@router.post("/plans/{plan_id}/orders/{order_id}/confirm")
def confirm_order(
    plan_id: str, order_id: str, body: ForwarderDecision, principal: ForwarderPrincipal
) -> ForwarderOrder:
    access = plan_for(plan_id, principal)
    request = ConfirmRequest(actor="forwarder", accept=body.accept, note=body.note)
    try:
        order = store.confirm(plan_id, order_id, request, org_id=access.owner_org_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다") from exc
    except OrderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    notify_order_committed(access.session, order, access.owner_org_id, store.clock())
    return _order_view(order, access.session.scenario)


@router.post("/plans/{plan_id}/orders/{order_id}/requests/{request_id}")
def answer_request(
    plan_id: str,
    order_id: str,
    request_id: str,
    body: RequestResultBody,
    principal: ForwarderPrincipal,
) -> ForwarderOrder:
    access = plan_for(plan_id, principal)
    try:
        order = store.answer_request(
            plan_id, order_id, request_id, body, org_id=access.owner_org_id
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="실행 지시 또는 요청을 찾을 수 없습니다"
        ) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _order_view(order, access.session.scenario)
