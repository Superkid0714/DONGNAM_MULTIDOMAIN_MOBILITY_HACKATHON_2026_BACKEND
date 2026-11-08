from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.saas_access import plan_for, visible_plans
from freshswitch_backend.execution.quantity_policy import QuantityPolicy, confirm_quantity
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PlanRow, QuantityPolicyRow
from freshswitch_backend.planning.quantity_policy import (
    QuantityChangeResponse,
    QuoteResponse,
    ReservationState,
    StatementRow,
    VoyageQuantityState,
    auto_confirm,
    departure_blocked,
    execution_ready,
    initialize_policies,
    load_ledger,
    make_quote,
    policy_rows,
    queue_change,
    reservation_state,
    save_ledger,
    statement,
    voyage_state,
)
from freshswitch_backend.planning.refill import (
    RefillOffer,
    RefillState,
    join_waitlist,
    respond_offer,
)
from freshswitch_backend.planning.store import SessionConflict, store

router = APIRouter(prefix="/saas", tags=["saas-quantity-policy"])
Shipper = Annotated[Principal, Depends(require_role("shipper"))]
Forwarder = Annotated[Principal, Depends(require_role("forwarder"))]


class QuantityBody(BaseModel):
    quantity: int = Field(ge=0, le=10000)


class SubmitQuantityBody(BaseModel):
    quote_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ConfirmQuantityBody(BaseModel):
    revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=120)


class AcceptRefillBody(BaseModel):
    quantity: int = Field(gt=0, le=10000)
    idempotency_key: str = Field(min_length=1, max_length=120)


class DeclineRefillBody(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)


class WaitlistBody(BaseModel):
    lot_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(gt=0, le=10000)


class DepartureBody(BaseModel):
    departure_observed: Literal[True]
    idempotency_key: str = Field(min_length=1, max_length=120)


class PolicyUpdateBody(QuantityPolicy):
    expected_revision: int | None = Field(default=None, ge=1)


class RefillOfferView(RefillOffer):
    plan_id: str
    server_now: datetime
    shipper_org_name: str | None = None


class RefillOverview(BaseModel):
    plan_id: str
    offers: list[RefillOfferView]


def _read_states(plan_id: str, principal: Principal) -> list[VoyageQuantityState]:
    access = plan_for(plan_id, principal)
    now = store.clock()
    with database.begin() as db:
        plan_row = db.get(PlanRow, plan_id, with_for_update=True)
        result = []
        for row in policy_rows(db, plan_id, lock=True):
            ledger = load_ledger(row)
            from freshswitch_backend.planning.quantity_policy import refresh_ledger

            ledger = refresh_ledger(db, plan_row, row, ledger, now)
            result.append(
                voyage_state(
                    row,
                    ledger,
                    access.session,
                    now,
                    principal.org_id if principal.org_type == "shipper" else None,
                    execution_ready=execution_ready(db, plan_row),
                )
            )
        return result


@router.get("/shipper/plans/{plan_id}/quantity-policy")
def shipper_states(plan_id: str, principal: Shipper) -> list[VoyageQuantityState]:
    return _read_states(plan_id, principal)


@router.get("/forwarder/plans/{plan_id}/quantity-policy")
def forwarder_states(plan_id: str, principal: Forwarder) -> list[VoyageQuantityState]:
    return _read_states(plan_id, principal)


@router.get("/forwarder/quantity-policies")
def forwarder_policies(principal: Forwarder) -> list[VoyageQuantityState]:
    with database.begin() as db:
        ids = list(db.scalars(select(PlanRow.plan_id).where(visible_plans(principal))))
    return [item for plan_id in ids for item in _read_states(plan_id, principal)]


def _reservation(db, plan_id: str, lot_id: str, principal: Principal):
    plan_row = db.get(PlanRow, plan_id, with_for_update=True)
    if plan_row is None:
        raise HTTPException(404, "계획을 찾을 수 없습니다")
    for row in policy_rows(db, plan_id, lock=True):
        ledger = load_ledger(row)
        reservation = ledger.reservations.get(lot_id)
        if reservation and reservation.shipper_org_id == principal.org_id:
            from freshswitch_backend.planning.quantity_policy import refresh_ledger

            ledger = refresh_ledger(db, plan_row, row, ledger, store.clock(), advance=False)
            return plan_row, row, ledger
    raise HTTPException(404, "수량 예약을 찾을 수 없습니다")


@router.get("/shipper/plans/{plan_id}/lots/{lot_id}/quantity")
def quantity_state(plan_id: str, lot_id: str, principal: Shipper) -> ReservationState:
    with database.begin() as db:
        _, row, ledger = _reservation(db, plan_id, lot_id, principal)
        return reservation_state(row, ledger, lot_id, store.clock())


@router.post("/shipper/plans/{plan_id}/lots/{lot_id}/quantity/quote")
def quantity_quote(
    plan_id: str, lot_id: str, body: QuantityBody, principal: Shipper
) -> QuoteResponse:
    with database.begin() as db:
        plan_row, row, ledger = _reservation(db, plan_id, lot_id, principal)
        return make_quote(db, row, ledger, plan_row, lot_id, body.quantity, store.clock())


@router.post("/shipper/plans/{plan_id}/lots/{lot_id}/quantity")
def submit_quantity(
    plan_id: str, lot_id: str, body: SubmitQuantityBody, principal: Shipper
) -> QuantityChangeResponse:
    with database.begin() as db:
        plan_row, row, ledger = _reservation(db, plan_id, lot_id, principal)
        return queue_change(
            db,
            row,
            ledger,
            plan_row,
            lot_id,
            body.quote_id,
            body.idempotency_key,
            principal,
            store.clock(),
        )


@router.post("/shipper/plans/{plan_id}/lots/{lot_id}/quantity/confirm")
def confirm_reserved_quantity(
    plan_id: str, lot_id: str, body: ConfirmQuantityBody, principal: Shipper
) -> ReservationState:
    with database.begin() as db:
        _, row, ledger = _reservation(db, plan_id, lot_id, principal)
        reservation = ledger.reservations[lot_id]
        if reservation.confirmed_quantity is not None:
            return reservation_state(row, ledger, lot_id, store.clock())
        if body.revision != row.revision:
            raise HTTPException(409, "수량 상태가 바뀌었습니다. 다시 확인해 주세요")
        if store.clock() >= ledger.policy.cutoff:
            raise HTTPException(409, "수량 확정 마감이 지났습니다")
        ledger.reservations[lot_id] = confirm_quantity(
            reservation, ledger.policy, store.clock(), actor="shipper"
        )
        save_ledger(db, row, ledger, store.clock())
        return reservation_state(row, ledger, lot_id, store.clock())


@router.get("/forwarder/plans/{plan_id}/voyages/{voyage_id}/quantity-policy")
def forwarder_policy(plan_id: str, voyage_id: str, principal: Forwarder) -> VoyageQuantityState:
    found = next(
        (item for item in _read_states(plan_id, principal) if item.voyage_id == voyage_id), None
    )
    if found is None:
        raise HTTPException(404, "수량 정책을 찾을 수 없습니다")
    return found


@router.put("/forwarder/plans/{plan_id}/voyages/{voyage_id}/quantity-policy")
def update_policy(
    plan_id: str, voyage_id: str, body: PolicyUpdateBody, principal: Forwarder
) -> VoyageQuantityState:
    access = plan_for(plan_id, principal)
    now = store.clock()
    with database.begin() as db:
        plan_row = db.get(PlanRow, plan_id, with_for_update=True)
        row = db.get(QuantityPolicyRow, (plan_id, voyage_id), with_for_update=True)
        if row is None:
            if access.session.history or access.session.active_order_id:
                raise HTTPException(
                    409, "기존 실행 이력이 있는 계획에는 새 정책을 소급 적용할 수 없습니다"
                )
            initialize_policies(db, plan_row, access.session, now)
            db.flush()
            row = db.get(QuantityPolicyRow, (plan_id, voyage_id))
        if row is None:
            raise HTTPException(404, "계획에 배정된 해상 항차를 찾을 수 없습니다")
        ledger = load_ledger(row)
        auto_confirm(ledger, now)
        if any(r.confirmed_quantity is not None or r.events for r in ledger.reservations.values()):
            raise HTTPException(409, "확정된 수량의 정책은 변경할 수 없습니다")
        if any(r.get("status") == "queued" for r in ledger.requests.values()):
            raise HTTPException(409, "수량 변경 요청 처리 후 정책을 변경해 주세요")
        if body.expected_revision is not None and body.expected_revision != row.revision:
            raise HTTPException(409, "항차 정책이 바뀌었습니다. 다시 확인해 주세요")
        voyage = next(v for v in access.session.scenario.voyages if v.id == voyage_id)
        if body.departure != voyage.etd or body.cutoff > voyage.cy_cutoff:
            raise HTTPException(
                422, "수량 마감은 실제 CY 마감 이하여야 하며 출항 시각은 항차와 같아야 합니다"
            )
        ledger.policy = QuantityPolicy.model_validate(
            body.model_dump(exclude={"expected_revision"})
        )
        save_ledger(db, row, ledger, now)
        return voyage_state(row, ledger, access.session, now)


@router.get("/shipper/plans/{plan_id}/quantity-statement")
def quantity_statement(plan_id: str, principal: Shipper) -> list[StatementRow]:
    with database.begin() as db:
        result = [
            item
            for row in policy_rows(db, plan_id)
            for item in statement(row, load_ledger(row), principal.org_id)
        ]
        if not result:
            raise HTTPException(404, "수량 정산 내역을 찾을 수 없습니다")
        return result


@router.get("/shipper/quantity-statements")
def quantity_statements(principal: Shipper) -> list[StatementRow]:
    with database.begin() as db:
        return [
            item
            for row in db.scalars(select(QuantityPolicyRow))
            for item in statement(row, load_ledger(row), principal.org_id)
        ]


@router.post("/forwarder/plans/{plan_id}/voyages/{voyage_id}/quantity-departure")
def confirm_departure(
    plan_id: str, voyage_id: str, body: DepartureBody, principal: Forwarder
) -> list[StatementRow]:
    from freshswitch_backend.planning.quantity_policy import refresh_ledger

    plan_for(plan_id, principal)
    now = store.clock()
    with database.begin() as db:
        plan_row = db.get(PlanRow, plan_id, with_for_update=True)
        row = db.get(QuantityPolicyRow, (plan_id, voyage_id), with_for_update=True)
        if row is None:
            raise HTTPException(404, "수량 정책을 찾을 수 없습니다")
        ledger = refresh_ledger(db, plan_row, row, load_ledger(row), now, advance=False)
        if ledger.finalized_statement is not None:
            return [
                StatementRow.model_validate(item) for item in ledger.finalized_statement["rows"]
            ]
        blocked = departure_blocked(ledger, now, execution_ready(db, plan_row))
        if blocked:
            raise HTTPException(409, blocked)
        ledger.departure_confirmed_at = now
        rows = statement(row, ledger)
        ledger.finalized_statement = {
            "idempotency_key": body.idempotency_key,
            "rows": [item.model_dump(mode="json") for item in rows],
        }
        save_ledger(db, row, ledger, now)
        return rows


@router.get("/shipper/refill-offers")
def shipper_refill_offers(principal: Shipper) -> list[RefillOfferView]:
    from freshswitch_backend.planning.quantity_policy import tick_policies

    tick_policies(shipper_org_id=principal.org_id)
    now = store.clock()
    with database.begin() as db:
        return [
            RefillOfferView(**offer.model_dump(), plan_id=row.plan_id, server_now=now)
            for row in db.scalars(select(QuantityPolicyRow))
            for offer in RefillState.model_validate(load_ledger(row).refill).offers
            if offer.shipper_org_id == principal.org_id
        ]


def _respond(offer_id: str, principal: Principal, accept: bool, quantity: int | None, key: str):
    from freshswitch_backend.planning.quantity_policy import vacancies

    now = store.clock()
    with database.begin() as db:
        candidates = list(db.scalars(select(QuantityPolicyRow)))
        identity = next(
            (
                (r.plan_id, r.voyage_id)
                for r in candidates
                if any(
                    o.offer_id == offer_id and o.shipper_org_id == principal.org_id
                    for o in RefillState.model_validate(load_ledger(r).refill).offers
                )
            ),
            None,
        )
        if identity is None:
            raise HTTPException(404, "충원 제안을 찾을 수 없습니다")
        plan_row = db.get(PlanRow, identity[0], with_for_update=True)
        row = db.get(QuantityPolicyRow, identity, with_for_update=True, populate_existing=True)
        ledger = load_ledger(row)
        state = RefillState.model_validate(ledger.refill)
        previous = ledger.requests.get(f"offer:{key}")
        if previous and (
            previous["offer_id"] != offer_id
            or previous["accept"] != accept
            or previous["quantity"] != quantity
        ):
            raise HTTPException(409, "같은 요청 키에 다른 응답을 사용할 수 없습니다")
        if not previous:
            try:
                state = respond_offer(
                    db,
                    plan_row,
                    state,
                    vacancies(db, plan_row, row, ledger),
                    ledger.policy,
                    offer_id,
                    principal.org_id,
                    accept,
                    quantity,
                    now,
                )
            except (SessionConflict, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc
            ledger.refill = state.model_dump(mode="json")
            ledger.requests[f"offer:{key}"] = {
                "offer_id": offer_id,
                "accept": accept,
                "quantity": quantity,
            }
            save_ledger(db, row, ledger, now)
        offer = next(o for o in state.offers if o.offer_id == offer_id)
        return RefillOfferView(**offer.model_dump(), plan_id=row.plan_id, server_now=now)


@router.post("/shipper/refill-offers/{offer_id}/accept")
def accept_refill(offer_id: str, body: AcceptRefillBody, principal: Shipper) -> RefillOfferView:
    return _respond(offer_id, principal, True, body.quantity, body.idempotency_key)


@router.post("/shipper/refill-offers/{offer_id}/decline")
def decline_refill(offer_id: str, body: DeclineRefillBody, principal: Shipper) -> RefillOfferView:
    return _respond(offer_id, principal, False, None, body.idempotency_key)


@router.get("/forwarder/plans/{plan_id}/refill")
def forwarder_refill(plan_id: str, principal: Forwarder) -> RefillOverview:
    _read_states(plan_id, principal)
    now = store.clock()
    with database.begin() as db:
        from freshswitch_backend.persistence.tables import OrganizationRow

        names = dict(db.execute(select(OrganizationRow.id, OrganizationRow.name)).all())
        return RefillOverview(
            plan_id=plan_id,
            offers=[
                RefillOfferView(
                    **offer.model_dump(),
                    plan_id=plan_id,
                    server_now=now,
                    shipper_org_name=names.get(offer.shipper_org_id),
                )
                for row in policy_rows(db, plan_id)
                for offer in RefillState.model_validate(load_ledger(row).refill).offers
            ],
        )


@router.post("/shipper/plans/{plan_id}/voyages/{voyage_id}/refill-waitlist")
def add_waitlist(
    plan_id: str, voyage_id: str, body: WaitlistBody, principal: Shipper
) -> RefillState:
    with database.begin() as db:
        plan_row = db.get(PlanRow, plan_id, with_for_update=True)
        row = db.get(QuantityPolicyRow, (plan_id, voyage_id), with_for_update=True)
        if plan_row is None or row is None:
            raise HTTPException(404, "항차를 찾을 수 없습니다")
        ledger = load_ledger(row)
        try:
            state = join_waitlist(
                db,
                plan_row,
                RefillState.model_validate(ledger.refill),
                ledger.policy,
                voyage_id,
                body.lot_id,
                principal.org_id,
                body.quantity,
                store.clock(),
            )
        except (KeyError, SessionConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        ledger.refill = state.model_dump(mode="json")
        save_ledger(db, row, ledger, store.clock())
        return state.model_copy(
            update={
                "waitlist": [e for e in state.waitlist if e.shipper_org_id == principal.org_id],
                "offers": [o for o in state.offers if o.shipper_org_id == principal.org_id],
            }
        )
