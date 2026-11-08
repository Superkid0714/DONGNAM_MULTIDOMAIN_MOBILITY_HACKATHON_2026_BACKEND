import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from freshswitch_backend.execution.quantity_policy import (
    QuantityPolicy,
    QuantityQuote,
    QuantityReservation,
    allocate_refill,
    amounts,
    change_quantity,
    confirm_quantity,
    distribute_credits,
    quote_quantity,
)
from freshswitch_backend.persistence.tables import (
    ChangeRequestRow,
    LotRow,
    PlanRow,
    QuantityLedgerEventRow,
    QuantityPolicyRow,
)


class StoredQuote(BaseModel):
    quote_id: str
    lot_id: str
    revision: int
    plan_version: int
    created_at: datetime
    expires_at: datetime
    quote: QuantityQuote


class VoyageLedger(BaseModel):
    policy: QuantityPolicy
    reservations: dict[str, QuantityReservation] = Field(default_factory=dict)
    quotes: dict[str, StoredQuote] = Field(default_factory=dict)
    requests: dict[str, dict[str, Any]] = Field(default_factory=dict)
    refill: dict[str, Any] = Field(default_factory=dict)
    ai_air_recommendations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    departure_confirmed_at: datetime | None = None
    finalized_statement: dict[str, Any] | None = None


class ReservationState(BaseModel):
    plan_id: str
    voyage_id: str
    lot_id: str
    reservation_id: str
    revision: int
    server_now: datetime
    policy: QuantityPolicy
    reservation_opens_at: datetime
    quantity_status: Literal["scheduled", "reserved", "confirmed", "cancelled"]
    current_quantity: int
    confirmed_quantity: int | None
    free_adjust_remaining: int
    cumulative_reduced_quantity: int
    gross_fee_krw: int
    refill_credit_krw: int
    final_fee_krw: int
    pending_adjustment: bool
    can_adjust: bool
    can_confirm_quantity: bool
    events: list[dict[str, Any]]


class VoyageQuantityState(BaseModel):
    plan_id: str
    voyage_id: str
    revision: int
    server_now: datetime
    policy: QuantityPolicy
    policy_locked: bool
    capacity_quantity: int
    occupied_quantity: int
    vacant_quantity: int
    reservations: list[ReservationState]
    deficit_policy: Literal["requires_reconfirmation"] = "requires_reconfirmation"
    platform_revenue_krw: int = 0
    can_confirm_departure: bool = False
    departure_blocked_reason: str | None = None
    departure_confirmed_at: datetime | None = None


class QuoteResponse(QuantityQuote):
    quote_id: str
    plan_id: str
    lot_id: str
    voyage_id: str
    revision: int
    server_now: datetime
    expires_at: datetime
    requires_execution_approval: bool = True


class QuantityChangeResponse(BaseModel):
    status: Literal["queued", "applied"]
    change_request_id: str | None = None
    state: ReservationState


class StatementRow(BaseModel):
    plan_id: str
    lot_id: str
    voyage_id: str
    cancellation_charge_krw: int
    refill_credit_krw: int
    compensation_credit_krw: int
    net_adjustment_krw: int
    finalized: bool
    platform_revenue_krw: int = 0


def load_ledger(row: QuantityPolicyRow) -> VoyageLedger:
    return VoyageLedger.model_validate(row.payload)


def save_ledger(db: Session, row: QuantityPolicyRow, ledger: VoyageLedger, now: datetime) -> None:
    for reservation in ledger.reservations.values():
        for kind, entries in (("reduction", reservation.events), ("refill", reservation.offsets)):
            for event in entries:
                payload = event.model_dump(mode="json")
                stored = db.get(QuantityLedgerEventRow, event.event_id)
                if stored is None:
                    db.add(
                        QuantityLedgerEventRow(
                            event_id=event.event_id,
                            plan_id=row.plan_id,
                            voyage_id=row.voyage_id,
                            kind=kind,
                            at=event.at,
                            payload=payload,
                        )
                    )
                elif stored.payload != payload:
                    raise ValueError("확정된 취소·충원 기록은 변경할 수 없습니다")
    row.payload = ledger.model_dump(mode="json")
    row.revision += 1
    row.updated_at = now


def initialize_policies(db: Session, row: PlanRow, session, now: datetime) -> None:
    if row.org_id == "demo" or not session.registered_lot_ids:
        return
    assignments = [a for a in session.plan.assignments if a.mode == "sea"]
    containers = {c.id: c for c in session.scenario.containers}
    for voyage in session.scenario.voyages:
        chosen = [a for a in assignments if containers[a.container_id].voyage_id == voyage.id]
        if not chosen:
            continue
        if db.get(QuantityPolicyRow, (row.plan_id, voyage.id)) is not None:
            continue
        policy = QuantityPolicy(
            cutoff=voyage.cy_cutoff,
            departure=voyage.etd,
            pilot=session.scenario.cost_params.billing.pilot,
        )
        reservations = {}
        for assignment in chosen:
            lot = db.get(LotRow, assignment.lot_id)
            if lot is None:
                continue
            reservation = QuantityReservation(
                reservation_id=f"{row.plan_id}:{lot.id}",
                lot_id=lot.id,
                shipper_id=assignment.shipper_id,
                shipper_org_id=lot.shipper_org_id,
                container_id=assignment.container_id,
                current_quantity=assignment.pallets,
            )
            if now >= policy.confirms_at:
                reservation = confirm_quantity(reservation, policy, now)
            reservations[lot.id] = reservation
        db.add(
            QuantityPolicyRow(
                plan_id=row.plan_id,
                voyage_id=voyage.id,
                org_id=row.org_id,
                revision=1,
                payload=VoyageLedger(policy=policy, reservations=reservations).model_dump(
                    mode="json"
                ),
                updated_at=now,
            )
        )


def policy_rows(db: Session, plan_id: str, *, lock: bool = False) -> list[QuantityPolicyRow]:
    query = (
        select(QuantityPolicyRow)
        .where(QuantityPolicyRow.plan_id == plan_id)
        .order_by(QuantityPolicyRow.voyage_id)
    )
    return list(db.scalars(query.with_for_update() if lock else query))


def has_policy(db: Session, plan_id: str) -> bool:
    return (
        db.scalar(
            select(QuantityPolicyRow.plan_id).where(QuantityPolicyRow.plan_id == plan_id).limit(1)
        )
        is not None
    )


def annotate_for_policy(session, result) -> None:
    from freshswitch_backend.execution.cancellation import annotate_cancellations
    from freshswitch_backend.persistence import database

    with database.begin() as db:
        enabled = has_policy(db, session.plan_id)
    if not enabled:
        annotate_cancellations(result, session.scenario, session.plan)
        return
    from freshswitch_backend.planning.store import store
    from freshswitch_backend.replan.engine import executed_dispatches, fixed_routes

    with database.begin() as db:
        owner = db.get(PlanRow, session.plan_id).org_id
    held, _ = fixed_routes(
        session.routes,
        store.clock(),
        executed_dispatches(store.orders(session.plan_id, org_id=owner)),
    )
    for alternative in result.alternatives:
        by_voyage = {group.voyage_id: group.pickup.routes for group in alternative.routes}
        changed = any(
            route not in by_voyage.get(voyage_id, [])
            for voyage_id, routes in held.items()
            for route in routes
        )
        if changed:
            alternative.feasible = False
            alternative.infeasible_reasons.append(
                "이미 출발했거나 실행 확정된 배차를 변경할 수 없습니다"
            )
    if not any(a.id == result.recommended_id and a.feasible for a in result.alternatives):
        feasible = [a for a in result.alternatives if a.feasible]
        result.recommended_id = min(feasible, key=lambda a: a.total_cost).id if feasible else None


def guard_quantity_apply(db: Session, plan_row: PlanRow, event, rejecting) -> None:
    from freshswitch_backend.planning.store import store

    if event.type not in {"cancel", "reduce", "increase", "air_switch"} or rejecting:
        return
    for row in policy_rows(db, plan_row.plan_id, lock=True):
        ledger = load_ledger(row)
        ledger = refresh_ledger(db, plan_row, row, ledger, store.clock(), advance=False)
        if event.lot_id not in ledger.reservations:
            continue
        quantity = event.new_pallets if event.type in {"reduce", "increase"} else 0
        approved = any(
            r.get("lot_id") == event.lot_id
            and r.get("status") == "queued"
            and r.get("quantity") == quantity
            for r in ledger.requests.values()
        )
        proof = ledger.ai_air_recommendations.get(event.lot_id)
        if event.type == "air_switch" and proof and proof.get("base_version") == plan_row.version:
            approved = True
        if event.type == "air_switch" and not approved:
            quote = quote_quantity(
                ledger.reservations[event.lot_id], ledger.policy, 0, store.clock()
            )
            if quote.fee_krw == 0:
                event_id = str(uuid.uuid4())
                ledger.requests[f"free-air:{event_id}"] = {
                    "request_id": event_id,
                    "lot_id": event.lot_id,
                    "quantity": 0,
                    "at": store.clock().isoformat(),
                    "status": "queued",
                    "fee_krw": 0,
                    "source": "zero_fee_air_change",
                    "target_version": plan_row.version + 1,
                }
                save_ledger(db, row, ledger, store.clock())
                approved = True
        if not approved:
            raise HTTPException(409, "화주가 수량 변경 견적을 확인한 요청으로 적용해 주세요")


def policy_deadlines(db: Session, plan_id: str, order, alternative, now: datetime) -> None:
    cutoffs = [load_ledger(row).policy.cutoff for row in policy_rows(db, plan_id)]
    if not cutoffs:
        return
    future_departures = [
        route.earliest_departure
        for group in alternative.routes
        for route in group.pickup.routes
        if route.earliest_departure > now
    ]
    deadline = min([*cutoffs, *future_departures])
    order.deadlines.forwarder = deadline
    order.deadlines.shipper = deadline
    order.deadlines.basis = (
        "확정 수량 정책: 변경 마감과 아직 출발하지 않은 배차 중 빠른 시각까지 개별 승인"
    )
    for confirmation in order.shipper_confirmations:
        confirmation.deadline = deadline


def auto_confirm(ledger: VoyageLedger, now: datetime) -> bool:
    changed = False
    for key, reservation in list(ledger.reservations.items()):
        if reservation.confirmed_quantity is None and now >= ledger.policy.confirms_at:
            ledger.reservations[key] = confirm_quantity(reservation, ledger.policy, now)
            changed = True
    return changed


def reservation_state(
    row: QuantityPolicyRow, ledger: VoyageLedger, lot_id: str, now: datetime
) -> ReservationState:
    reservation = ledger.reservations[lot_id]
    policy = reservation.policy_snapshot or ledger.policy
    cumulative = max(0, (reservation.confirmed_quantity or 0) - reservation.current_quantity)
    maximum_reduced = max(
        [
            cumulative,
            *[
                max(0, (reservation.confirmed_quantity or 0) - event.after_quantity)
                for event in reservation.events
            ],
        ]
    )
    gross, credit, final = amounts(reservation)
    pending = any(
        request.get("lot_id") == lot_id and request.get("status") == "queued"
        for request in ledger.requests.values()
    )
    return ReservationState(
        plan_id=row.plan_id,
        voyage_id=row.voyage_id,
        lot_id=lot_id,
        reservation_id=reservation.reservation_id,
        revision=row.revision,
        server_now=now,
        policy=policy,
        reservation_opens_at=policy.holds_at,
        quantity_status="cancelled"
        if not reservation.current_quantity
        else "confirmed"
        if reservation.confirmed_quantity is not None
        else "scheduled"
        if now < policy.holds_at
        else "reserved",
        current_quantity=reservation.current_quantity,
        confirmed_quantity=reservation.confirmed_quantity,
        free_adjust_remaining=max(0, policy.free_adjust - maximum_reduced),
        cumulative_reduced_quantity=cumulative,
        gross_fee_krw=gross,
        refill_credit_krw=credit,
        final_fee_krw=final,
        pending_adjustment=pending,
        can_adjust=policy.holds_at <= now < policy.cutoff and not pending,
        can_confirm_quantity=reservation.confirmed_quantity is None
        and policy.holds_at <= now < policy.cutoff,
        events=[event.model_dump(mode="json") for event in reservation.events],
    )


def voyage_state(
    row: QuantityPolicyRow,
    ledger: VoyageLedger,
    session,
    now: datetime,
    shipper_org_id: str | None = None,
    *,
    execution_ready: bool = False,
) -> VoyageQuantityState:
    containers = [c for c in session.scenario.containers if c.voyage_id == row.voyage_id]
    ids = {c.id for c in containers}
    capacity = sum(c.capacity_pallets for c in containers)
    occupied = sum(
        a.pallets for a in session.plan.assignments if a.mode == "sea" and a.container_id in ids
    )
    blocked = departure_blocked(ledger, now, execution_ready and session.status == "committed")
    return VoyageQuantityState(
        plan_id=row.plan_id,
        voyage_id=row.voyage_id,
        revision=row.revision,
        server_now=now,
        policy=ledger.policy,
        policy_locked=any(r.confirmed_quantity is not None for r in ledger.reservations.values()),
        capacity_quantity=capacity,
        occupied_quantity=occupied,
        vacant_quantity=max(0, capacity - occupied),
        can_confirm_departure=blocked is None,
        departure_blocked_reason=blocked,
        departure_confirmed_at=ledger.departure_confirmed_at,
        reservations=[
            reservation_state(row, ledger, lot_id, now)
            for lot_id, r in ledger.reservations.items()
            if shipper_org_id is None or r.shipper_org_id == shipper_org_id
        ],
    )


def departure_blocked(ledger: VoyageLedger, now: datetime, execution_ready: bool) -> str | None:
    from freshswitch_backend.planning.refill import RefillState

    if ledger.departure_confirmed_at is not None:
        return "이미 출항 확인과 정산 확정이 완료되었습니다"
    if now < ledger.policy.departure:
        return "예정 출항 시각 이후 실제 출항을 확인해 주세요"
    if not execution_ready:
        return "계획과 실행 지시의 화주·포워더 승인을 먼저 완료해 주세요"
    if any(r.get("status") == "queued" for r in ledger.requests.values()):
        return "처리 중인 수량 변경 요청을 먼저 마무리해 주세요"
    if any(
        o.status == "pending_execution" for o in RefillState.model_validate(ledger.refill).offers
    ):
        return "충원 실행 확인을 먼저 마무리해 주세요"
    return None


def execution_ready(db: Session, plan_row: PlanRow) -> bool:
    from freshswitch_backend.planning.store import (
        SessionConflict,
        _require_execution_consent,
        store,
    )

    state = store._require(db, plan_row.plan_id, plan_row.org_id)
    order = state.orders.get(state.session.active_order_id)
    try:
        _require_execution_consent(state, order)
    except SessionConflict:
        return False
    return plan_row.status == "committed"


def make_quote(
    db: Session,
    row: QuantityPolicyRow,
    ledger: VoyageLedger,
    plan_row: PlanRow,
    lot_id: str,
    quantity: int,
    now: datetime,
) -> QuoteResponse:
    reservation = ledger.reservations[lot_id]
    if now < (reservation.policy_snapshot or ledger.policy).holds_at:
        raise HTTPException(409, "아직 수량 예약 기간이 아닙니다")
    if quantity > reservation.current_quantity:
        from freshswitch_backend.planning.store import PlanSession

        session = PlanSession.model_validate(plan_row.payload)
        container = next(c for c in session.scenario.containers if c.id == reservation.container_id)
        occupied = sum(
            a.pallets
            for a in session.plan.assignments
            if a.mode == "sea" and a.container_id == container.id
        )
        if occupied + quantity - reservation.current_quantity > container.capacity_pallets:
            raise HTTPException(409, "남은 컨테이너 공간보다 많은 수량을 증량할 수 없습니다")
    if any(
        r.get("lot_id") == lot_id and r.get("status") == "queued" for r in ledger.requests.values()
    ):
        raise HTTPException(409, "이 출하에 처리 중인 수량 변경 요청이 있습니다")
    try:
        quote = quote_quantity(reservation, ledger.policy, quantity, now)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    active = reservation.policy_snapshot or ledger.policy
    expires = min(
        [
            now + timedelta(minutes=5),
            *[at for at in (active.confirms_at, active.late_at, active.cutoff) if at > now],
        ]
    )
    quote_id = str(uuid.uuid4())
    stored = StoredQuote(
        quote_id=quote_id,
        lot_id=lot_id,
        revision=row.revision,
        plan_version=plan_row.version,
        created_at=now,
        expires_at=expires,
        quote=quote,
    )
    ledger.quotes = {key: item for key, item in ledger.quotes.items() if item.expires_at > now}
    ledger.quotes[quote_id] = stored
    row.payload = ledger.model_dump(mode="json")
    return QuoteResponse(
        **quote.model_dump(),
        quote_id=quote_id,
        plan_id=row.plan_id,
        lot_id=lot_id,
        voyage_id=row.voyage_id,
        revision=row.revision,
        server_now=now,
        expires_at=expires,
    )


def queue_change(
    db: Session,
    row: QuantityPolicyRow,
    ledger: VoyageLedger,
    plan_row: PlanRow,
    lot_id: str,
    quote_id: str,
    key: str,
    principal,
    now: datetime,
):
    previous = ledger.requests.get(key)
    if previous:
        if previous.get("quote_id") != quote_id or previous.get("lot_id") != lot_id:
            raise HTTPException(409, "같은 요청 키에 다른 변경을 사용할 수 없습니다")
        if previous.get("status") == "rejected":
            raise HTTPException(409, "거절되거나 만료된 변경 요청입니다. 새 견적을 확인해 주세요")
        return QuantityChangeResponse(
            status=previous["status"],
            change_request_id=previous.get("request_id"),
            state=reservation_state(row, ledger, lot_id, now),
        )
    quote = ledger.quotes.get(quote_id)
    if (
        quote is None
        or quote.lot_id != lot_id
        or quote.expires_at <= now
        or quote.revision != row.revision
        or quote.plan_version != plan_row.version
    ):
        raise HTTPException(409, "견적이 만료되었거나 수량이 바뀌었습니다. 다시 확인해 주세요")
    reservation = ledger.reservations[lot_id]
    fresh = quote_quantity(reservation, ledger.policy, quote.quote.quantity, now)
    if fresh != quote.quote:
        raise HTTPException(409, "취소 수수료가 바뀌었습니다. 다시 확인해 주세요")
    pending = db.scalar(
        select(ChangeRequestRow.id).where(
            ChangeRequestRow.plan_id == row.plan_id,
            ChangeRequestRow.lot_id == lot_id,
            ChangeRequestRow.status == "open",
        )
    )
    if pending or any(
        r.get("lot_id") == lot_id and r.get("status") == "queued" for r in ledger.requests.values()
    ):
        raise HTTPException(409, "이 출하에 처리 중인 변경 요청이 있습니다")
    if quote.quote.quantity == reservation.current_quantity:
        return QuantityChangeResponse(
            status="applied", state=reservation_state(row, ledger, lot_id, now)
        )
    request_id = str(uuid.uuid4())
    db.add(
        ChangeRequestRow(
            id=request_id,
            plan_id=row.plan_id,
            org_id=row.org_id,
            shipper_org_id=principal.org_id,
            shipper_id=reservation.shipper_id,
            lot_id=lot_id,
            type="cancel"
            if quote.quote.quantity == 0
            else "increase"
            if quote.quote.quantity > reservation.current_quantity
            else "reduce",
            new_pallets=quote.quote.quantity or None,
            reason="확정 수량 변경",
            status="open",
            estimate=None,
            requested_by=principal.user_id,
            created_at=now,
            updated_at=now,
        )
    )
    ledger.requests[key] = {
        "request_id": request_id,
        "quote_id": quote_id,
        "lot_id": lot_id,
        "quantity": quote.quote.quantity,
        "at": now.isoformat(),
        "status": "queued",
        "fee_krw": quote.quote.fee_krw,
    }
    save_ledger(db, row, ledger, now)
    return QuantityChangeResponse(
        status="queued",
        change_request_id=request_id,
        state=reservation_state(row, ledger, lot_id, now),
    )


def sync_committed(db: Session, plan_row: PlanRow, session, now: datetime) -> None:
    assignments = {a.lot_id: a for a in session.plan.assignments if a.mode == "sea"}
    for row in policy_rows(db, plan_row.plan_id, lock=True):
        ledger = load_ledger(row)
        changed = auto_confirm(ledger, now)
        for lot_id, reservation in list(ledger.reservations.items()):
            assigned = assignments.get(lot_id)
            same_voyage = assigned and assigned.voyage_id == row.voyage_id
            if assigned and not same_voyage:
                reservation.current_quantity = 0
                changed = True
                continue
            quantity = assigned.pallets if same_voyage else 0
            if quantity == reservation.current_quantity:
                if assigned and reservation.container_id != assigned.container_id:
                    reservation.container_id = assigned.container_id
                    changed = True
                continue
            request = next(
                (
                    r
                    for r in ledger.requests.values()
                    if r.get("lot_id") == lot_id
                    and r.get("status") == "queued"
                    and r.get("quantity") == quantity
                ),
                None,
            )
            if request is None and quantity < reservation.current_quantity:
                from freshswitch_backend.persistence.tables import PlanConfirmationRow

                initial_rejection = db.scalar(
                    select(PlanConfirmationRow).where(
                        PlanConfirmationRow.plan_id == plan_row.plan_id,
                        PlanConfirmationRow.round == 1,
                        PlanConfirmationRow.shipper_id == reservation.shipper_id,
                        PlanConfirmationRow.status == "rejected",
                    )
                )
                ever_accepted = db.scalar(
                    select(PlanConfirmationRow.plan_id)
                    .where(
                        PlanConfirmationRow.plan_id == plan_row.plan_id,
                        PlanConfirmationRow.shipper_id == reservation.shipper_id,
                        PlanConfirmationRow.status == "accepted",
                    )
                    .limit(1)
                )
                if initial_rejection and not ever_accepted and not reservation.events:
                    reservation.current_quantity = quantity
                    changed = True
                    continue
            event_at = datetime.fromisoformat(request["at"]) if request else now
            event_id = (
                request["request_id"]
                if request
                else f"{plan_row.plan_id}:{session.committed_version}:{lot_id}"
            )
            if event_at >= (reservation.policy_snapshot or ledger.policy).cutoff:
                raise HTTPException(409, "수량 변경 마감 이후에는 새 취소를 확정할 수 없습니다")
            proof = ledger.ai_air_recommendations.get(lot_id)
            ai_air = bool(
                proof
                and proof.get("base_version") == session.committed_version - 1
                and any(a.lot_id == lot_id and a.mode == "air" for a in session.plan.assignments)
            )
            if request is None and quantity < reservation.current_quantity and not ai_air:
                raise HTTPException(409, "화주가 확인하지 않은 취소 비용은 확정할 수 없습니다")
            ledger.reservations[lot_id] = change_quantity(
                reservation, ledger.policy, quantity, event_at, event_id, ai_air=ai_air
            )
            if same_voyage:
                ledger.reservations[lot_id].container_id = assigned.container_id
            if request:
                request["status"] = "applied"
            changed = True
        for assigned in assignments.values():
            if assigned.voyage_id != row.voyage_id or assigned.lot_id in ledger.reservations:
                continue
            lot = db.get(LotRow, assigned.lot_id)
            if lot is None:
                continue
            reservation = QuantityReservation(
                reservation_id=f"{row.plan_id}:{lot.id}",
                lot_id=lot.id,
                shipper_id=lot.shipper_id,
                shipper_org_id=lot.shipper_org_id,
                container_id=assigned.container_id,
                current_quantity=assigned.pallets,
            )
            if now >= ledger.policy.confirms_at:
                reservation = confirm_quantity(reservation, ledger.policy, now)
            ledger.reservations[lot.id] = reservation
            changed = True
        if changed:
            save_ledger(db, row, ledger, now)


def statement(
    row: QuantityPolicyRow, ledger: VoyageLedger, shipper_org_id: str | None = None
) -> list[StatementRow]:
    if ledger.finalized_statement is not None:
        return [
            StatementRow.model_validate(item)
            for item in ledger.finalized_statement["rows"]
            if shipper_org_id is None
            or ledger.reservations[item["lot_id"]].shipper_org_id == shipper_org_id
        ]
    total = sum(amounts(r)[2] for r in ledger.reservations.values())
    credits = distribute_credits(
        total, {key: r.current_quantity for key, r in ledger.reservations.items()}
    )
    result = []
    for lot_id, reservation in ledger.reservations.items():
        if shipper_org_id is not None and reservation.shipper_org_id != shipper_org_id:
            continue
        gross, refill, final = amounts(reservation)
        compensation = credits.get(lot_id, 0)
        result.append(
            StatementRow(
                plan_id=row.plan_id,
                lot_id=lot_id,
                voyage_id=row.voyage_id,
                cancellation_charge_krw=gross,
                refill_credit_krw=refill,
                compensation_credit_krw=compensation,
                net_adjustment_krw=final - compensation,
                finalized=ledger.departure_confirmed_at is not None,
            )
        )
    return result


def vacancies(db: Session, plan_row: PlanRow, row: QuantityPolicyRow, ledger: VoyageLedger):
    from freshswitch_backend.planning.refill import RefillVacancy
    from freshswitch_backend.planning.store import PlanSession

    session = PlanSession.model_validate(plan_row.payload)
    source = {lot.id: lot for lot in session.scenario.lots}
    result = []
    for lot_id, reservation in ledger.reservations.items():
        lot = source.get(lot_id) or db.get(LotRow, lot_id)
        if lot is None:
            continue
        for event in reservation.events:
            consumed = sum(
                o.quantity for o in reservation.offsets if o.reduction_event_id == event.event_id
            )
            if event.reduced_quantity <= consumed:
                continue
            result.append(
                RefillVacancy(
                    vacancy_id=event.event_id,
                    reservation_id=reservation.reservation_id,
                    container_id=event.container_id,
                    voyage_id=row.voyage_id,
                    product_id=lot.product_id,
                    destination=lot.destination,
                    quantity=event.reduced_quantity,
                    refilled_quantity=consumed,
                )
            )
    return result


def refresh_ledger(
    db: Session,
    plan_row: PlanRow,
    row: QuantityPolicyRow,
    ledger: VoyageLedger,
    now: datetime,
    *,
    advance: bool = True,
) -> VoyageLedger:
    from freshswitch_backend.persistence.tables import NotificationRow, OrderRow
    from freshswitch_backend.planning.refill import RefillState, advance_offers, reconcile_refills

    original = ledger.model_dump(mode="json")
    auto_confirm(ledger, now)
    for request in ledger.requests.values():
        if request.get("status") != "queued":
            continue
        change = db.get(ChangeRequestRow, request["request_id"])
        order = db.get(OrderRow, change.order_id) if change and change.order_id else None
        if change is None and request.get("source") == "zero_fee_air_change":
            order = db.scalar(
                select(OrderRow)
                .where(
                    OrderRow.plan_id == plan_row.plan_id,
                    OrderRow.payload["target_version"].as_integer() == request["target_version"],
                )
                .limit(1)
            )
        if (change and change.status == "rejected") or (
            order and order.status in {"rejected", "expired"}
        ):
            request["status"] = "rejected"
    state = RefillState.model_validate(ledger.refill)
    previous_ids = {o.offer_id for o in state.offers}
    state, additions = reconcile_refills(db, plan_row, state, now)
    for addition in additions:
        updated = allocate_refill(
            list(ledger.reservations.values()),
            addition.container_id,
            addition.quantity,
            addition.offer_id,
            now,
        )
        ledger.reservations = {r.lot_id: r for r in updated}
        if addition.lot_id not in ledger.reservations:
            lot = db.get(LotRow, addition.lot_id)
            if lot:
                reservation = QuantityReservation(
                    reservation_id=f"{row.plan_id}:{lot.id}",
                    lot_id=lot.id,
                    shipper_id=lot.shipper_id,
                    shipper_org_id=lot.shipper_org_id,
                    container_id=addition.container_id,
                    current_quantity=addition.quantity,
                )
                if now >= ledger.policy.confirms_at:
                    reservation = confirm_quantity(reservation, ledger.policy, now)
                ledger.reservations[lot.id] = reservation
    if advance and ledger.departure_confirmed_at is None:
        state = advance_offers(
            db, plan_row, state, vacancies(db, plan_row, row, ledger), ledger.policy, now
        )
    ledger.refill = state.model_dump(mode="json")
    for offer in state.offers:
        if offer.offer_id in previous_ids:
            continue
        lot = db.get(LotRow, offer.lot_id)
        db.add(
            NotificationRow(
                id=str(uuid.uuid4()),
                org_id=offer.shipper_org_id,
                shipper_id=lot.shipper_id if lot else None,
                plan_id=plan_row.plan_id,
                order_id=None,
                change_request_id=None,
                kind="refill_offer",
                channel="앱 내부 알림",
                title="빈자리 충원 제안",
                body=f"{offer.offered_quantity}파렛트 제안입니다. 만료 전에 수량을 확인해 주세요.",
                link="/app/shipper/refill-offers",
                dedupe_key=f"refill:{offer.offer_id}",
                created_at=now,
            )
        )
    if ledger.model_dump(mode="json") != original:
        save_ledger(db, row, ledger, now)
    return ledger


def tick_policies(*, shipper_org_id: str | None = None) -> None:
    from freshswitch_backend.persistence import database
    from freshswitch_backend.planning.store import store

    with database.begin() as db:
        plan_ids = list(db.scalars(select(QuantityPolicyRow.plan_id).distinct()))
    for plan_id in plan_ids:
        with database.begin() as db:
            plan_row = db.scalar(
                select(PlanRow).where(PlanRow.plan_id == plan_id).with_for_update(skip_locked=True)
            )
            if plan_row is None:
                continue
            for row in policy_rows(db, plan_id, lock=True):
                refresh_ledger(db, plan_row, row, load_ledger(row), store.clock())


def record_air_recommendations(
    plan_id: str, base_version: int, lot_ids: list[str], now: datetime
) -> None:
    from freshswitch_backend.persistence import database

    with database.begin() as db:
        db.get(PlanRow, plan_id, with_for_update=True)
        for row in policy_rows(db, plan_id, lock=True):
            ledger = load_ledger(row)
            for lot_id in lot_ids:
                if lot_id in ledger.reservations:
                    ledger.ai_air_recommendations[lot_id] = {
                        "base_version": base_version,
                        "at": now.isoformat(),
                        "source": "server_forecast",
                    }
            save_ledger(db, row, ledger, now)


def record_selected_recommendation(
    db: Session, plan_row: PlanRow, session, pending, alternative, now: datetime
) -> None:
    if pending.result.event.type == "air_switch" or pending.result.recommended_id != alternative.id:
        return
    before = {a.lot_id for a in session.plan.assignments if a.mode == "sea"}
    air = {a.lot_id for a in alternative.plan.assignments if a.mode == "air"} & before
    if not air:
        return
    for row in policy_rows(db, plan_row.plan_id, lock=True):
        ledger = load_ledger(row)
        for lot_id in air:
            if lot_id in ledger.reservations:
                ledger.ai_air_recommendations[lot_id] = {
                    "base_version": session.version,
                    "source": "selected_server_recommendation",
                    "at": now.isoformat(),
                    "replan_id": pending.replan_id,
                }
        save_ledger(db, row, ledger, now)


def guard_bound_lot(lot_id: str, org_id: str) -> None:
    from freshswitch_backend.persistence import database

    with database.begin() as db:
        found = db.scalar(
            select(QuantityPolicyRow.plan_id)
            .where(
                QuantityPolicyRow.payload["reservations"][lot_id]["shipper_org_id"].astext == org_id
            )
            .limit(1)
        )
        if found is not None:
            raise HTTPException(
                409, "항차 수량 예약에 연결된 출하는 수량 변경 견적으로 수정해 주세요"
            )


def guard_policy_deletion(db: Session, plan_id: str) -> None:
    from freshswitch_backend.planning.store import store

    for row in policy_rows(db, plan_id, lock=True):
        ledger = load_ledger(row)
        if (
            any(r.confirmed_quantity is not None or r.events for r in ledger.reservations.values())
            or store.clock() >= ledger.policy.confirms_at
            or any(r.get("status") == "queued" for r in ledger.requests.values())
        ):
            raise HTTPException(
                409, "확정 수량과 취소·충원 정산 이력이 있는 계획은 삭제할 수 없습니다"
            )
