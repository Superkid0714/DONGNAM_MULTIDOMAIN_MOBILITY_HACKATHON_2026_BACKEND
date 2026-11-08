import uuid
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from freshswitch_backend.domain.models import Lot, Packhouse, Scenario, Shipper
from freshswitch_backend.execution.quantity_policy import QuantityPolicy
from freshswitch_backend.lots.models import RegisteredLot
from freshswitch_backend.persistence.orgs import same_sandbox
from freshswitch_backend.persistence.tables import (
    LotRow,
    OrganizationRow,
    PackhouseRow,
    PendingRow,
    PlanRow,
    ShipperProfileRow,
)
from freshswitch_backend.planning.service import solve_with_routes
from freshswitch_backend.planning.store import (
    REGISTERED_ADD,
    PendingReplan,
    PlanSession,
    SessionConflict,
    _booked,
    _dump_pending,
    _lock_bookings,
    store,
)
from freshswitch_backend.replan.engine import diff_plans, executed_dispatches, fixed_routes
from freshswitch_backend.replan.models import AddLotEvent, Alternative, IncreaseEvent, ReplanResult
from freshswitch_backend.routing import locked_routes
from freshswitch_backend.routing.models import TruckRoute


class RefillVacancy(BaseModel):
    vacancy_id: str
    reservation_id: str
    container_id: str
    voyage_id: str
    product_id: str
    destination: str
    quantity: int = Field(gt=0)
    refilled_quantity: int = Field(default=0, ge=0)

    @property
    def available_quantity(self) -> int:
        return max(0, self.quantity - self.refilled_quantity)


class WaitlistEntry(BaseModel):
    entry_id: str
    lot_id: str
    shipper_org_id: str
    voyage_id: str
    destination: str
    product_id: str
    departure_date: date
    quantity: int = Field(gt=0)
    created_at: datetime
    fulfilled_quantity: int = Field(default=0, ge=0)
    status: Literal["waiting", "withdrawn"] = "waiting"


class RefillOffer(BaseModel):
    offer_id: str
    vacancy_id: str
    container_id: str
    voyage_id: str
    lot_id: str
    shipper_org_id: str
    source: Literal["waitlist", "registered"]
    waitlist_entry_id: str | None = None
    offered_quantity: int = Field(gt=0)
    created_at: datetime
    expires_at: datetime
    status: Literal[
        "pending", "pending_execution", "committed", "rejected", "expired", "failed"
    ] = "pending"
    detour_minutes: float | None = None
    destination: str | None = None
    departure_at: datetime | None = None
    pickup_at: datetime | None = None
    port_eta: datetime | None = None
    route_description: str | None = None
    accepted_quantity: int = 0
    accepted_lot_id: str | None = None
    split_parent_lot_id: str | None = None
    order_id: str | None = None
    decided_at: datetime | None = None
    reason: str | None = None


class RefillState(BaseModel):
    waitlist: list[WaitlistEntry] = Field(default_factory=list)
    offers: list[RefillOffer] = Field(default_factory=list)


class CommittedRefill(BaseModel):
    vacancy_id: str
    offer_id: str
    lot_id: str
    container_id: str
    quantity: int


def join_waitlist(
    db: Session,
    row: PlanRow,
    state: RefillState,
    policy: QuantityPolicy,
    voyage_id: str,
    lot_id: str,
    shipper_org_id: str,
    quantity: int,
    now: datetime,
) -> RefillState:
    if now >= policy.cutoff:
        raise SessionConflict("충원 대기 등록 마감이 지났습니다")
    lot = db.get(LotRow, lot_id, with_for_update=True)
    if lot is None or lot.shipper_org_id != shipper_org_id:
        raise KeyError(lot_id)
    if lot.status != "registered" or not 0 < quantity <= lot.pallets:
        raise SessionConflict("등록 상태의 남은 수량만 충원 대기에 넣을 수 있습니다")
    session = PlanSession.model_validate(row.payload)
    voyage = next((v for v in session.scenario.voyages if v.id == voyage_id), None)
    if voyage is None or voyage.destination != lot.destination:
        raise SessionConflict("출항지와 목적지가 맞는 항차가 필요합니다")
    eligible = {candidate.id for candidate in _registered(db, row)}
    if lot_id not in eligible:
        raise SessionConflict("이 계획에 연결할 수 없는 등록 출하입니다")
    updated = state.model_copy(deep=True)
    if any(e.lot_id == lot_id and e.status == "waiting" for e in updated.waitlist):
        raise SessionConflict("이미 충원 대기 중인 출하입니다")
    updated.waitlist.append(
        WaitlistEntry(
            entry_id=str(uuid.uuid4()),
            lot_id=lot_id,
            shipper_org_id=shipper_org_id,
            voyage_id=voyage_id,
            destination=lot.destination,
            product_id=lot.product_id,
            departure_date=policy.departure.date(),
            quantity=quantity,
            created_at=now,
        )
    )
    return updated


def _registered(db: Session, row: PlanRow) -> list[LotRow]:
    return list(
        db.scalars(
            select(LotRow)
            .join(OrganizationRow, OrganizationRow.id == LotRow.shipper_org_id)
            .where(
                LotRow.status == "registered",
                LotRow.scenario_id == row.scenario_id,
                OrganizationRow.type == "shipper",
                same_sandbox(OrganizationRow.sandbox, row.org_id),
            )
            .order_by(LotRow.created_at, LotRow.id)
        )
    )


def _matches(lot: LotRow, vacancy: RefillVacancy, session: PlanSession, now: datetime) -> bool:
    voyage = next((v for v in session.scenario.voyages if v.id == vacancy.voyage_id), None)
    return bool(
        voyage
        and now < voyage.cy_cutoff
        and lot.destination == vacancy.destination == voyage.destination
        and lot.product_id == vacancy.product_id
        and lot.ready_date <= voyage.cy_cutoff.date()
        and lot.product_id in {p.id for p in session.scenario.products}
        and lot.id not in {item.id for item in session.scenario.lots}
    )


def _route_lots(route: TruckRoute) -> set[str]:
    return {lid for stop in route.stops for lid in stop.lot_ids}


def _candidate_scenario(db: Session, row: PlanRow, scenario: Scenario, lot: Lot) -> Scenario:
    shippers = {shipper.id: shipper for shipper in scenario.shippers}
    packhouses = {packhouse.id: packhouse for packhouse in scenario.packhouses}
    source = db.get(LotRow, lot.id)
    if source is None:
        raise SessionConflict("등록 출하의 소유 조직을 확인할 수 없습니다")
    if lot.shipper_id not in shippers:
        profile = db.scalar(
            select(ShipperProfileRow)
            .join(OrganizationRow, OrganizationRow.id == ShipperProfileRow.org_id)
            .where(
                ShipperProfileRow.id == lot.shipper_id,
                ShipperProfileRow.org_id == source.shipper_org_id,
                OrganizationRow.type == "shipper",
                same_sandbox(OrganizationRow.sandbox, row.org_id),
            )
        )
        if profile is None:
            raise SessionConflict("새 화주의 등록 정보와 집하 위치를 확인할 수 없습니다")
        shippers[profile.id] = Shipper.model_validate(profile, from_attributes=True)
    needed = {shippers[lot.shipper_id].packhouse_id}
    if lot.packhouse_id:
        needed.add(lot.packhouse_id)
    for packhouse_id in needed - packhouses.keys():
        registered = db.get(PackhouseRow, packhouse_id)
        if registered is None or registered.org_id != source.shipper_org_id:
            raise SessionConflict("등록된 집하 위치의 좌표를 확인할 수 없습니다")
        packhouses[packhouse_id] = Packhouse.model_validate(registered, from_attributes=True)
    return scenario.model_copy(
        update={"shippers": list(shippers.values()), "packhouses": list(packhouses.values())}
    )


def _detour(before: list[TruckRoute], after: TruckRoute, lot_id: str) -> float | None:
    originals = [
        route
        for route in before
        if route.truck_id == after.truck_id
        and route.trip == after.trip
        and _route_lots(route) & (_route_lots(after) - {lot_id})
    ]
    if len(originals) != 1:
        return None
    original = originals[0]
    if not _route_lots(original).issubset(_route_lots(after)):
        return None
    old_minutes = (original.port_eta - original.earliest_departure).total_seconds() / 60
    new_minutes = (after.port_eta - after.earliest_departure).total_seconds() / 60
    return max(0.0, new_minutes - old_minutes)


def preview_refill(
    db: Session,
    row: PlanRow,
    lot: Lot,
    vacancy: RefillVacancy,
    policy: QuantityPolicy,
    now: datetime,
    *,
    require_detour: bool,
) -> tuple[ReplanResult, float | None]:
    state = store._require(db, row.plan_id, row.org_id)
    session = state.session
    if session.status != "committed" or state.unconfirmed:
        raise SessionConflict("현재 계획의 확인이 끝나야 충원을 진행할 수 있습니다")
    voyage = next(v for v in session.scenario.voyages if v.id == vacancy.voyage_id)
    if now >= min(policy.cutoff, voyage.cy_cutoff):
        raise SessionConflict("충원 마감이 지났습니다")
    port = next(p for p in session.scenario.ports if p.id == voyage.origin)
    if port.unlocode.upper() != "KRPUS":
        raise SessionConflict("부산항 도착을 검증할 수 없는 항차입니다")
    container = next((c for c in session.scenario.containers if c.id == vacancy.container_id), None)
    if container is None or container.voyage_id != vacancy.voyage_id:
        raise SessionConflict("충원 대상 컨테이너가 항차에 없습니다")
    filled = sum(a.pallets for a in session.plan.assignments if a.container_id == container.id)
    if lot.pallets > min(vacancy.available_quantity, container.capacity_pallets - filled):
        raise SessionConflict("취소로 생긴 남은 공석보다 수락 물량이 큽니다")
    if lot.destination != vacancy.destination or lot.product_id != vacancy.product_id:
        raise SessionConflict("충원 공석의 목적지·품목이 일치하지 않습니다")
    prepared = _candidate_scenario(db, row, session.scenario, lot)
    after = prepared.model_copy(update={"lots": [*prepared.lots, lot]})
    dispatched = executed_dispatches(state.orders.values())
    held, reasons = fixed_routes(session.routes, now, dispatched)
    fixed = {
        a.lot_id: a.container_id if a.mode == "sea" else "AIR" for a in session.plan.assignments
    }
    fixed[lot.id] = vacancy.container_id
    booked = _booked(db, row.org_id, row.scenario_id, row.plan_id)
    forbidden = [(item.id, cid) for item in after.lots for cid in booked]
    with locked_routes(held, not_before=now):
        built = solve_with_routes(after, fixed=fixed, forbidden=forbidden, time_limit_s=2.0)
    assignment = next((a for a in built.plan.assignments if a.lot_id == lot.id), None)
    if assignment is None or assignment.container_id != vacancy.container_id:
        raise SessionConflict("충원 물량의 컨테이너·집하 경로를 배정할 수 없습니다")
    positions = {
        a.lot_id: a.container_id if a.mode == "sea" else "AIR" for a in built.plan.assignments
    }
    if any(positions.get(lid) != location for lid, location in fixed.items()):
        raise SessionConflict("기존 확정 출하의 배정을 유지할 수 없습니다")
    new_routes = {
        (vp.voyage_id, route.truck_id, route.trip): route
        for vp in built.routes
        for route in vp.pickup.routes
    }
    for voyage_id, routes in held.items():
        if any(
            new_routes.get((voyage_id, route.truck_id, route.trip)) != route for route in routes
        ):
            raise SessionConflict("이미 출발했거나 실행된 집하 경로를 바꿀 수 없습니다")
    candidate_routes = [
        route
        for vp in built.routes
        if vp.voyage_id == vacancy.voyage_id
        for route in vp.pickup.routes
        if lot.id in _route_lots(route)
    ]
    if len(candidate_routes) != 1 or candidate_routes[0].port_eta > min(
        policy.cutoff, voyage.cy_cutoff
    ):
        raise SessionConflict("부산항 마감 전 도착하는 집하 경로가 없습니다")
    originals = [
        route
        for vp in session.routes
        if vp.voyage_id == vacancy.voyage_id
        for route in vp.pickup.routes
    ]
    detour = _detour(originals, candidate_routes[0], lot.id)
    if require_detour and (detour is None or detour > policy.detour_max_minutes):
        raise SessionConflict("기존 집하 경로 대비 30분 이내 우회를 검증할 수 없습니다")
    diff = diff_plans(
        after, session.plan, built.plan, routes_before=session.routes, routes_after=built.routes
    )
    cost = built.plan.total_operational_cost
    alternative = Alternative(
        id="pull_forward",
        label="취소 공석 충원",
        summary="확인 후 충원 물량 확정",
        feasible=True,
        infeasible_reasons=[],
        total_cost=cost,
        cost_delta=cost - session.plan.total_operational_cost,
        plan=built.plan,
        routes=built.routes,
        diff=diff,
        solve_ms=built.plan.solve_ms,
    )
    return ReplanResult(
        event=AddLotEvent(lot=lot, occurred_at=now),
        scope="container",
        free_lot_ids=[lot.id],
        fixed_lot_ids=list(fixed.keys() - {lot.id}),
        fixed_reasons=reasons,
        occurred_at=now,
        alternatives=[alternative],
        recommended_id=alternative.id,
        diff=diff,
        solve_ms=alternative.solve_ms,
        scenario_after=after,
    ), detour


def advance_offers(
    db: Session,
    row: PlanRow,
    state: RefillState,
    vacancies: list[RefillVacancy],
    policy: QuantityPolicy,
    now: datetime,
) -> RefillState:
    updated = state.model_copy(deep=True)
    for offer in updated.offers:
        if offer.status == "pending" and now >= min(offer.expires_at, policy.cutoff):
            offer.status = "expired"
            offer.decided_at = now
    if now >= policy.cutoff or row.status != "committed":
        return updated
    session = PlanSession.model_validate(row.payload)
    registered = _registered(db, row)
    rows = {lot.id: lot for lot in registered}
    occupied = {o.lot_id for o in updated.offers if o.status in ("pending", "pending_execution")}
    for vacancy in vacancies:
        if vacancy.available_quantity <= 0 or any(
            offer.container_id == vacancy.container_id
            and offer.voyage_id == vacancy.voyage_id
            and offer.status in ("pending", "pending_execution")
            for offer in updated.offers
        ):
            continue
        container = next(
            (c for c in session.scenario.containers if c.id == vacancy.container_id), None
        )
        if container is None:
            continue
        available = container.capacity_pallets - sum(
            a.pallets for a in session.plan.assignments if a.container_id == container.id
        )
        if available <= 0:
            continue
        tried = {o.lot_id for o in updated.offers if o.vacancy_id == vacancy.vacancy_id}
        waiting = sorted(
            [
                e
                for e in updated.waitlist
                if e.status == "waiting"
                and e.quantity > e.fulfilled_quantity
                and e.voyage_id == vacancy.voyage_id
                and e.destination == vacancy.destination
                and e.product_id == vacancy.product_id
                and e.departure_date == policy.departure.date()
            ],
            key=lambda e: e.created_at,
        )
        candidates = [
            (rows[e.lot_id], e.quantity - e.fulfilled_quantity, "waitlist", e.entry_id)
            for e in waiting
            if e.lot_id in rows
        ]
        waitlisted = {e.lot_id for e in waiting}
        if now >= policy.confirms_at:
            candidates += [
                (lot, lot.pallets, "registered", None)
                for lot in registered
                if lot.id not in waitlisted
            ]
        chosen = None
        best_registered_key = None
        for lot, requested, source, entry_id in candidates:
            if lot.id in tried or lot.id in occupied or not _matches(lot, vacancy, session, now):
                continue
            quantity = min(requested, lot.pallets, vacancy.available_quantity, available)
            candidate = RegisteredLot.model_validate(lot, from_attributes=True).to_lot()
            candidate = candidate.model_copy(update={"pallets": quantity})
            try:
                result, detour = preview_refill(
                    db,
                    row,
                    candidate,
                    vacancy,
                    policy,
                    now,
                    require_detour=source == "registered",
                )
            except (SessionConflict, ValueError):
                continue
            if source == "registered":
                if detour is None:
                    continue
                registered_key = (detour, lot.created_at, lot.id)
                if best_registered_key is not None and registered_key >= best_registered_key:
                    continue
                best_registered_key = registered_key
            chosen = RefillOffer(
                offer_id=str(uuid.uuid4()),
                vacancy_id=vacancy.vacancy_id,
                container_id=vacancy.container_id,
                voyage_id=vacancy.voyage_id,
                lot_id=lot.id,
                shipper_org_id=lot.shipper_org_id,
                source=source,
                waitlist_entry_id=entry_id,
                offered_quantity=quantity,
                created_at=now,
                expires_at=min(
                    now + timedelta(minutes=policy.offer_timeout_minutes), policy.cutoff
                ),
                detour_minutes=detour,
                **_offer_route_details(result, candidate.id, vacancy),
            )
            if source == "waitlist":
                break
        if chosen is not None:
            updated.offers.append(chosen)
            occupied.add(chosen.lot_id)
    return updated


def _offer_route_details(result: ReplanResult, lot_id: str, vacancy: RefillVacancy) -> dict:
    scenario = result.scenario_after
    voyage = next(v for v in scenario.voyages if v.id == vacancy.voyage_id)
    for pickup in result.alternatives[0].routes:
        for route in pickup.pickup.routes:
            for stop in route.stops:
                if lot_id not in stop.lot_ids:
                    continue
                packhouse = next(p for p in scenario.packhouses if p.id == stop.packhouse_id)
                port = next(p for p in scenario.ports if p.id == voyage.origin)
                return {
                    "destination": vacancy.destination,
                    "departure_at": voyage.etd,
                    "pickup_at": stop.eta,
                    "port_eta": route.port_eta,
                    "route_description": f"{packhouse.name} → {port.name}",
                }
    raise SessionConflict("실제 집하 시각을 확인할 수 없는 충원 제안입니다")


def _split(db: Session, original: LotRow, quantity: int, now: datetime) -> LotRow:
    if quantity == original.pallets:
        return original
    child = LotRow(
        id=f"refill-{uuid.uuid4().hex}",
        scenario_id=original.scenario_id,
        shipper_id=original.shipper_id,
        shipper_org_id=original.shipper_org_id,
        product_id=original.product_id,
        pallets=quantity,
        ready_date=original.ready_date,
        destination=original.destination,
        sell_by_date=original.sell_by_date,
        sell_window=original.sell_window,
        packhouse=original.packhouse,
        source=original.source,
        status="registered",
        created_at=now,
        updated_at=now,
    )
    original.pallets -= quantity
    original.updated_at = now
    db.add(child)
    db.flush()
    return child


def respond_offer(
    db: Session,
    row: PlanRow,
    state: RefillState,
    vacancies: list[RefillVacancy],
    policy: QuantityPolicy,
    offer_id: str,
    shipper_org_id: str,
    accept: bool,
    quantity: int | None,
    now: datetime,
) -> RefillState:
    updated = state.model_copy(deep=True)
    offer = next((o for o in updated.offers if o.offer_id == offer_id), None)
    if offer is None or offer.shipper_org_id != shipper_org_id:
        raise KeyError(offer_id)
    if offer.status != "pending":
        if (
            accept
            and offer.status in ("pending_execution", "committed")
            and quantity in (None, offer.accepted_quantity)
        ):
            return updated
        raise SessionConflict("이미 회신되거나 만료된 충원 제안입니다")
    if now >= min(offer.expires_at, policy.cutoff):
        raise SessionConflict("충원 제안의 회신 기한이 지났습니다")
    if not accept:
        offer.status, offer.decided_at = "rejected", now
        return advance_offers(db, row, updated, vacancies, policy, now)
    count = offer.offered_quantity if quantity is None else quantity
    if not 0 < count <= offer.offered_quantity:
        raise ValueError("수락 수량은 제안 수량 이하여야 합니다")
    vacancy = next((v for v in vacancies if v.vacancy_id == offer.vacancy_id), None)
    if vacancy is None or count > vacancy.available_quantity:
        raise SessionConflict("취소 공석이 이미 충원되었습니다")
    _lock_bookings(db, row.org_id)
    original = db.get(LotRow, offer.lot_id, with_for_update=True)
    if original is None or original.status != "registered" or original.pallets < count:
        raise SessionConflict("다른 계획이 이미 출하 물량을 사용했습니다")
    if original.shipper_org_id != shipper_org_id:
        raise KeyError(offer_id)
    candidate = _split(db, original, count, now)
    lot = RegisteredLot.model_validate(candidate, from_attributes=True).to_lot()
    result, detour = preview_refill(
        db,
        row,
        lot,
        vacancy,
        policy,
        now,
        require_detour=offer.source == "registered",
    )
    pending = PendingReplan(replan_id=str(uuid.uuid4()), base_version=row.version, result=result)
    db.add(
        PendingRow(
            replan_id=pending.replan_id,
            plan_id=row.plan_id,
            base_version=row.version,
            payload={**_dump_pending(pending), REGISTERED_ADD: candidate.id},
        )
    )
    db.flush()
    applied, _ = store._apply(
        db,
        row.plan_id,
        "pull_forward",
        pending.replan_id,
        None,
        row.org_id,
        refill_invitation=True,
    )
    offer.status = "pending_execution"
    offer.accepted_quantity = count
    offer.accepted_lot_id = candidate.id
    offer.split_parent_lot_id = original.id if original.id != candidate.id else None
    offer.order_id = applied.active_order_id
    offer.decided_at = now
    offer.detour_minutes = detour
    for name, value in _offer_route_details(result, lot.id, vacancy).items():
        setattr(offer, name, value)
    return updated


def reconcile_refills(
    db: Session,
    row: PlanRow,
    state: RefillState,
    now: datetime,
) -> tuple[RefillState, list[CommittedRefill]]:
    updated = state.model_copy(deep=True)
    plan = store._require(db, row.plan_id, row.org_id)
    committed = {a.lot_id: a for a in plan.committed.plan.assignments}
    additions = []
    for offer in updated.offers:
        if offer.status != "pending_execution":
            continue
        order = plan.orders.get(offer.order_id)
        assignment = committed.get(offer.accepted_lot_id)
        if (
            order is not None
            and order.status in ("forwarder_confirmed", "executed")
            and assignment is not None
            and assignment.container_id == offer.container_id
            and assignment.voyage_id == offer.voyage_id
            and not plan.unconfirmed
        ):
            offer.status = "committed"
            for entry in updated.waitlist:
                if entry.entry_id == offer.waitlist_entry_id:
                    entry.fulfilled_quantity += offer.accepted_quantity
            additions.append(
                CommittedRefill(
                    vacancy_id=offer.vacancy_id,
                    offer_id=offer.offer_id,
                    lot_id=offer.accepted_lot_id,
                    container_id=offer.container_id,
                    quantity=min(offer.accepted_quantity, assignment.pallets),
                )
            )
        elif order is None or order.status in ("rejected", "expired"):
            offer.status = "failed"
            offer.reason = "실행 지시가 거절되거나 만료되었습니다"
            offer.decided_at = now
    return updated, additions


def build_quantity_increase(
    db: Session,
    row: PlanRow,
    lot_id: str,
    quantity: int,
    now: datetime,
) -> ReplanResult:
    state = store._require(db, row.plan_id, row.org_id)
    session = state.session
    if session.status != "committed" or state.unconfirmed:
        raise SessionConflict("현재 계획의 확인이 끝나야 수량을 늘릴 수 있습니다")
    original = next((lot for lot in session.scenario.lots if lot.id == lot_id), None)
    assignment = next((a for a in session.plan.assignments if a.lot_id == lot_id), None)
    if original is None or assignment is None or assignment.mode != "sea":
        raise SessionConflict("해상 배정된 출하의 수량만 늘릴 수 있습니다")
    if quantity <= original.pallets:
        raise ValueError("증량할 수량은 현재 수량보다 커야 합니다")
    voyage = next(v for v in session.scenario.voyages if v.id == assignment.voyage_id)
    if now >= voyage.cy_cutoff:
        raise SessionConflict("수량 변경 마감이 지났습니다")
    container = next(c for c in session.scenario.containers if c.id == assignment.container_id)
    fill = sum(a.pallets for a in session.plan.assignments if a.container_id == container.id)
    if fill + quantity - original.pallets > container.capacity_pallets:
        raise SessionConflict("남은 컨테이너 용량보다 증량 수량이 큽니다")
    after = session.scenario.model_copy(
        update={
            "lots": [
                lot.model_copy(update={"pallets": quantity}) if lot.id == lot_id else lot
                for lot in session.scenario.lots
            ]
        }
    )
    held, reasons = fixed_routes(session.routes, now, executed_dispatches(state.orders.values()))
    if any(reason.lot_id == lot_id for reason in reasons):
        raise SessionConflict("이미 출발했거나 실행된 집하 물량을 늘릴 수 없습니다")
    fixed = {
        a.lot_id: a.container_id if a.mode == "sea" else "AIR" for a in session.plan.assignments
    }
    booked = _booked(db, row.org_id, row.scenario_id, row.plan_id)
    with locked_routes(held, not_before=now):
        built = solve_with_routes(
            after,
            fixed=fixed,
            forbidden=[(lot.id, cid) for lot in after.lots for cid in booked],
            time_limit_s=2.0,
        )
    positions = {
        a.lot_id: a.container_id if a.mode == "sea" else "AIR" for a in built.plan.assignments
    }
    if any(positions.get(lid) != location for lid, location in fixed.items()):
        raise SessionConflict("증량 후 기존 확정 출하의 배정을 유지할 수 없습니다")
    new_routes = {
        (vp.voyage_id, route.truck_id, route.trip): route
        for vp in built.routes
        for route in vp.pickup.routes
    }
    if any(
        new_routes.get((voyage_id, route.truck_id, route.trip)) != route
        for voyage_id, routes in held.items()
        for route in routes
    ):
        raise SessionConflict("이미 출발했거나 실행된 집하 경로를 바꿀 수 없습니다")
    carrying = [
        route for vp in built.routes for route in vp.pickup.routes if lot_id in _route_lots(route)
    ]
    if len(carrying) != 1 or carrying[0].port_eta > voyage.cy_cutoff:
        raise SessionConflict("마감 전 도착하는 증량 집하 경로가 없습니다")
    diff = diff_plans(
        after, session.plan, built.plan, routes_before=session.routes, routes_after=built.routes
    )
    alternative = Alternative(
        id="pull_forward",
        label="확정 수량 증량",
        summary="여유 용량 내 증량 후 실행 확인",
        feasible=True,
        infeasible_reasons=[],
        total_cost=built.plan.total_operational_cost,
        cost_delta=built.plan.total_operational_cost - session.plan.total_operational_cost,
        plan=built.plan,
        routes=built.routes,
        diff=diff,
        solve_ms=built.plan.solve_ms,
    )
    return ReplanResult(
        event=IncreaseEvent(lot_id=lot_id, new_pallets=quantity, occurred_at=now),
        scope="container",
        free_lot_ids=[],
        fixed_lot_ids=list(fixed),
        fixed_reasons=reasons,
        occurred_at=now,
        alternatives=[alternative],
        recommended_id=alternative.id,
        diff=diff,
        solve_ms=alternative.solve_ms,
        scenario_after=after,
    )
