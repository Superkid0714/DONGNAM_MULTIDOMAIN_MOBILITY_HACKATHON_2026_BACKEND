from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.intake import IntakeScope, register
from freshswitch_backend.api.lots import delete_unlocked_lot, update_locked_lot
from freshswitch_backend.api.saas_access import (
    PlanAccess,
    org_shipper_ids,
    plan_for,
    visible_plans,
)
from freshswitch_backend.api.saas_views import (
    ShipperContainerNumbers,
    ShipperPlanItem,
    ShipperSettlementRow,
    anonymized_orders,
    anonymized_session,
    replan_pending,
    shipper_container_numbers,
    shipper_genset_instructions,
    shipper_lot_ids,
    shipper_order_view,
    shipper_plan_item,
    shipper_settlements,
)
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.inputs import Note, StorableStr
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.execution.card import EnteredNumbers
from freshswitch_backend.execution.models import (
    ConfirmRequest,
    ExecutionOrder,
    RequestResultBody,
)
from freshswitch_backend.execution.numbers import entered_numbers
from freshswitch_backend.execution.orders import saas_requests
from freshswitch_backend.intake.models import IntakePreview, IntakeRequest
from freshswitch_backend.intake.tenants import load_catalog, tenant_scenario
from freshswitch_backend.lots import LotFilter, LotPatch, LotStatus, RegisteredLot, lot_store
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PlanRow
from freshswitch_backend.planning.store import (
    OrderInputError,
    PlanSession,
    SessionConflict,
    store,
)
from freshswitch_backend.quality import learning_scope, quality_history
from freshswitch_backend.quality.disclosure import LossDisclosure
from freshswitch_backend.quality.models import ExpectedLoss

THIRD_PARTY_ROLES = frozenset({"carrier", "terminal", "airline"})
THIRD_PARTY_ANSWER_FORBIDDEN = "선사·터미널·항공사 요청의 회신은 배정된 포워더가 기록합니다"
ALREADY_DECIDED = "이미 확인한 실행 지시입니다"

router = APIRouter(prefix="/saas/shipper", tags=["saas-shipper"])

ShipperPrincipal = Annotated[Principal, Depends(require_role("shipper"))]


class ShipperLotCreate(IntakeRequest):
    shipper: str | None = None
    shipper_org_id: str | None = None


class ShipperLot(RegisteredLot):
    expected_loss: ExpectedLoss


class ShipperDecision(BaseModel):
    accept: bool
    note: Note | None = None
    shipper_id: StorableStr | None = None


def _scope(principal: Principal) -> IntakeScope:
    return IntakeScope(
        org_id=principal.org_id,
        catalog_org=principal.org_id,
        lot_org=principal.org_id,
        shipper=principal,
    )


def _own_shipper(principal: Principal, requested: str | None) -> str:
    ids = org_shipper_ids(principal.org_id)
    if not ids:
        raise HTTPException(status_code=422, detail="조직에 등록된 화주 정보가 없습니다")
    return requested if requested in ids else ids[0]


def _access(plan_id: str, principal: Principal) -> PlanAccess:
    return plan_for(plan_id, principal)


def _saas_order(
    order: ExecutionOrder,
    session: PlanSession,
    numbers: dict[str, EnteredNumbers] | None = None,
) -> ExecutionOrder:
    entered = entered_numbers(order.plan_id) if numbers is None else numbers
    requests = saas_requests(order.plan_id, order.requests, session.scenario, numbers=entered)
    return order.model_copy(update={"requests": requests})


@router.get("/lots")
def list_lots(
    principal: ShipperPrincipal,
    status: LotStatus | None = None,
    ready_from: date | None = None,
    ready_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ShipperLot]:
    lots = lot_store.search(
        LotFilter(
            status=status, ready_from=ready_from, ready_to=ready_to, limit=limit, offset=offset
        ),
        org_id=principal.org_id,
    )
    disclosure = LossDisclosure(learning_scope(principal.org_id))
    scenarios: dict[str, Scenario] = {}

    def scenario_for(scenario_id: str) -> Scenario:
        if scenario_id not in scenarios:
            found = get_scenario(scenario_id)
            base = found or quality_history.reference_scenario()
            scenarios[scenario_id] = tenant_scenario(base, load_catalog(principal.org_id, base))
        return scenarios[scenario_id]

    return [
        ShipperLot(
            **lot.model_dump(),
            expected_loss=disclosure.for_lot(
                scenario_for(lot.scenario_id), lot.product_id, lot.destination
            ),
        )
        for lot in lots
    ]


@router.post("/lots", status_code=status.HTTP_201_CREATED)
def create_lot(body: ShipperLotCreate, principal: ShipperPrincipal) -> IntakePreview:
    fields = body.model_dump(exclude={"shipper", "shipper_org_id", "plan_id", "save"})
    owned = IntakeRequest(
        **fields, shipper=_own_shipper(principal, body.shipper), plan_id=None, save=True
    )
    return register(owned, _scope(principal))


@router.patch("/lots/{lot_id}")
def update_lot(lot_id: str, patch: LotPatch, principal: ShipperPrincipal) -> RegisteredLot:
    fields = patch.model_fields_set - {"shipper"}
    cleaned = LotPatch(**{name: getattr(patch, name) for name in fields})
    return update_locked_lot(lot_id, cleaned, _scope(principal), principal.org_id)


@router.delete("/lots/{lot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lot(lot_id: str, principal: ShipperPrincipal) -> None:
    delete_unlocked_lot(lot_id, principal.org_id)


def _sessions(principal: Principal) -> list[PlanSession]:
    store.expire_due(visible_plans(principal))
    with database.begin() as db:
        rows = db.scalars(
            select(PlanRow)
            .where(visible_plans(principal))
            .order_by(PlanRow.created_at.desc(), PlanRow.seq.desc())
        )
        return [PlanSession.model_validate(row.payload) for row in rows]


@router.get("/plans")
def list_plans(principal: ShipperPrincipal) -> list[ShipperPlanItem]:
    mine = lot_store.ids(org_id=principal.org_id)
    own = org_shipper_ids(principal.org_id)
    sessions = _sessions(principal)
    states = store.confirmation_states([s.plan_id for s in sessions])
    return [
        shipper_plan_item(session, mine, replan_pending(states.get(session.plan_id), own))
        for session in sessions
    ]


@router.get("/plans/{plan_id}")
def read_plan(plan_id: str, principal: ShipperPrincipal) -> dict[str, Any]:
    access = _access(plan_id, principal)
    own = access.viewer.shipper_ids
    orders = store.orders(plan_id, org_id=access.owner_org_id)
    entered = entered_numbers(plan_id)
    genset = shipper_genset_instructions(access.session, own, entered)
    payload = anonymized_session(
        access.session,
        own,
        orders,
        extra={"genset_instructions": [g.model_dump(mode="json") for g in genset]},
    )
    payload["container_numbers"] = [
        n.model_dump(mode="json") for n in _container_numbers(access, entered)
    ]
    state = store.confirmation_states([plan_id]).get(plan_id)
    payload["replan_pending"] = replan_pending(state, own)
    return payload


def _container_numbers(
    access: PlanAccess, entered: dict[str, EnteredNumbers] | None = None
) -> list[ShipperContainerNumbers]:
    numbers = entered_numbers(access.session.plan_id) if entered is None else entered
    return shipper_container_numbers(access.session, access.viewer.shipper_ids, numbers)


@router.get("/plans/{plan_id}/containers")
def read_container_numbers(
    plan_id: str, principal: ShipperPrincipal
) -> list[ShipperContainerNumbers]:
    return _container_numbers(_access(plan_id, principal))


@router.get("/plans/{plan_id}/orders")
def list_orders(plan_id: str, principal: ShipperPrincipal) -> list[dict[str, Any]]:
    access = _access(plan_id, principal)
    numbers = entered_numbers(plan_id)
    orders = [
        _saas_order(o, access.session, numbers)
        for o in store.orders(plan_id, org_id=access.owner_org_id)
    ]
    return anonymized_orders(access.session, orders, access.viewer.shipper_ids)


@router.post("/plans/{plan_id}/orders/{order_id}/requests/{request_id}")
def answer_request(
    plan_id: str,
    order_id: str,
    request_id: str,
    body: RequestResultBody,
    principal: ShipperPrincipal,
) -> dict[str, Any]:
    access = _access(plan_id, principal)
    owner = access.owner_org_id
    missing = HTTPException(status_code=404, detail="실행 지시 또는 요청을 찾을 수 없습니다")
    try:
        order = store.order(plan_id, order_id, org_id=owner)
    except KeyError as exc:
        raise missing from exc
    my_lots = shipper_lot_ids(access.session, access.viewer.shipper_ids)
    request = next((r for r in order.requests if r.request_id == request_id), None)
    if request is None or not my_lots & set(request.lot_ids):
        raise missing
    if request.counterparty_role in THIRD_PARTY_ROLES:
        raise HTTPException(status_code=403, detail=THIRD_PARTY_ANSWER_FORBIDDEN)
    try:
        updated = store.answer_request(plan_id, order_id, request_id, body, org_id=owner)
    except KeyError as exc:
        raise missing from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    orders = store.orders(plan_id, org_id=owner)
    return shipper_order_view(
        access.session, _saas_order(updated, access.session), access.viewer.shipper_ids, orders
    )


@router.post("/plans/{plan_id}/orders/{order_id}/confirm")
def confirm_order(
    plan_id: str, order_id: str, body: ShipperDecision, principal: ShipperPrincipal
) -> dict[str, Any]:
    access = _access(plan_id, principal)
    owner = access.owner_org_id
    missing = HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다")
    try:
        order = store.order(plan_id, order_id, org_id=owner)
    except KeyError as exc:
        raise missing from exc
    own = access.viewer.shipper_ids
    mine = {c.shipper_id: c.status for c in order.shipper_confirmations if c.shipper_id in own}
    if not mine:
        raise missing
    pending = [sid for sid, decision in mine.items() if decision == "pending"]
    if mine.get(body.shipper_id or "", "pending") != "pending" or not pending:
        raise HTTPException(status_code=409, detail=ALREADY_DECIDED)
    shipper_id = body.shipper_id if body.shipper_id in pending else pending[0]
    request = ConfirmRequest(
        actor="shipper", shipper_id=shipper_id, accept=body.accept, note=body.note
    )
    try:
        updated = store.confirm(plan_id, order_id, request, org_id=owner)
    except KeyError as exc:
        raise missing from exc
    except OrderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    orders = store.orders(plan_id, org_id=owner)
    return shipper_order_view(access.session, _saas_order(updated, access.session), own, orders)


@router.get("/settlements")
def list_settlements(principal: ShipperPrincipal) -> list[ShipperSettlementRow]:
    own = org_shipper_ids(principal.org_id)
    return [row for session in _sessions(principal) for row in shipper_settlements(session, own)]
