import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text, true
from sqlalchemy.orm import Session

from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.execution.models import (
    TERMINAL_STATUSES,
    ConfirmRequest,
    ExecutionOrder,
    OrderLogEntry,
    OrderStatus,
    ReplanCandidate,
    RequestResultBody,
    ShipperConfirmation,
)
from freshswitch_backend.execution.orders import (
    CONSOLIDATOR_LABEL,
    REASON_TEXT,
    build_order,
    fmt,
    short_name,
)
from freshswitch_backend.execution.params import ExecutionParams
from freshswitch_backend.execution.quality import (
    ArrivalInspection,
    ArrivalRequest,
    ArrivalSettlementResult,
    QualityError,
    arrival_settlement,
    current_inspections,
    planned_loss_rate,
)
from freshswitch_backend.explain import render_template
from freshswitch_backend.explain.models import Explanation
from freshswitch_backend.lots import (
    ANY_ORG,
    LotUnavailable,
    claim_lots,
    lot_owners,
    mark_planned,
    release_lots,
    sync_committed_lots,
)
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.resources import ResourceUnavailable, sync_allocations
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    BindingRow,
    ChangeRequestRow,
    ContainerRow,
    ForwarderResourceRow,
    IdempotencyRow,
    LotRow,
    ObservationRow,
    OrderRow,
    OrganizationRow,
    PendingRow,
    PlanConfirmationRow,
    PlanForwarderRow,
    PlanRow,
)
from freshswitch_backend.planning.service import VoyagePickup, build_plan
from freshswitch_backend.replan import (
    AddLotEvent,
    AirSwitchEvent,
    Alternative,
    CancelEvent,
    Event,
    ReplanResult,
    diff_plans,
    locks_air,
    plan_score,
)
from freshswitch_backend.replan.engine import PREFERENCE
from freshswitch_backend.replan.explanation import build_explain_context
from freshswitch_backend.replan.models import AlternativeId, CancellationCharge, PlanDiff

MAX_SESSIONS = 200
MAX_PENDING = 20
MAX_ORDERS = 50
MAX_IDEMPOTENCY_KEYS = 100
REGISTERED_ADD = "registered_lot_id"
RECOMPUTE_AFTER_ORDER = (
    "포워더가 확정한 실행 지시가 있는 계획은 처음부터 다시 계산할 수 없습니다. "
    "재계획 후보의 출하 취소로 거절한 화주를 빼세요(취소 부담금 없음)"
)
APPLY_BEFORE_CONFIRMATION = (
    "화주 확인을 기다리는 계획은 바꿀 수 없습니다. 전원 수락을 기다리거나 "
    "거절한 화주를 빼고 다시 계산하세요"
)
ADD_BEFORE_CONFIRMATION = (
    "포워더가 배정된 계획에는 아직 계획을 수락하지 않은 화주의 출하를 넣을 수 없습니다. "
    "포워더 배정을 해제한 뒤 넣고, 그 화주가 수락하면 다시 배정하세요"
)

SessionStatus = Literal["committed", "pending_confirmation", "pending_shipper_confirmation"]
ConfirmationStatus = Literal["pending", "accepted", "rejected"]
SEA_ROLES = ("carrier", "terminal")
BOOKED_CONTAINERS = text(
    "SELECT a->>'container_id' FROM plan_sessions, "
    "jsonb_array_elements(payload->'plan'->'assignments') AS a "
    "WHERE org_id = :org AND scenario_id = :scenario AND plan_id <> :plan AND a->>'mode' = 'sea' "
    "UNION SELECT a->>'container_id' FROM plan_sessions, "
    "jsonb_array_elements(committed->'plan'->'assignments') AS a "
    "WHERE org_id = :org AND scenario_id = :scenario AND plan_id <> :plan AND a->>'mode' = 'sea'"
)


class SessionConflict(Exception):
    pass


class ContainerBooked(SessionConflict):
    def __init__(self, container_ids: list[str]) -> None:
        super().__init__(
            f"다른 계획에 이미 배정된 컨테이너입니다: {', '.join(container_ids)}. "
            "이벤트를 다시 보내 재계산하세요"
        )
        self.container_ids = container_ids


class OrderInputError(ValueError):
    pass


class LotTaken(SessionConflict):
    def __init__(self, lot_ids: list[str]) -> None:
        super().__init__(
            f"이미 다른 계획에 들어갔거나 그사이 바뀐 출하입니다: {', '.join(lot_ids)}. "
            "출하 목록을 새로 고친 뒤 다시 계산하세요"
        )
        self.lot_ids = lot_ids


class IdempotencyMismatch(ValueError):
    pass


class InspectionPending(SessionConflict, QualityError):
    def __init__(self, lot_id: str) -> None:
        super().__init__(
            f"확정 대기 중인 실행 지시가 {lot_id}의 배정·수량·손실률을 바꿉니다. "
            "실행 지시가 확정되거나 거절·만료된 뒤 도착 검수를 기록하세요"
        )
        self.lot_id = lot_id


class HistoryEntry(BaseModel):
    seq: int
    replan_id: str
    event: Event
    alternative_id: AlternativeId
    applied_at: datetime
    cost_before: int
    cost_after: int
    cost_delta: int
    solve_ms: int
    explanation: Explanation | None = None
    kind: Literal["apply", "rollback"] = "apply"
    order_id: str | None = None


class PlanSession(BaseModel):
    plan_id: str
    scenario_id: str
    version: int
    created_at: datetime
    updated_at: datetime
    scenario: Scenario
    plan: Plan
    routes: list[VoyagePickup]
    air_locked_lot_ids: list[str]
    history: list[HistoryEntry]
    status: SessionStatus = "committed"
    committed_version: int = 1
    active_order_id: str | None = None
    arrival_inspections: list[ArrivalInspection] = Field(default_factory=list)
    inspection_revision: int = 0
    registered_lot_ids: list[str] = Field(default_factory=list)


class PlanSummary(BaseModel):
    plan_id: str
    scenario_id: str
    version: int
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    lot_count: int
    totals: dict[str, int]


class PlanConfirmation(BaseModel):
    plan_id: str
    round: int
    shipper_id: str
    shipper_org_id: str
    lot_ids: list[str]
    status: ConfirmationStatus
    reason: str | None = None
    auto_accepted: bool = False
    created_at: datetime
    decided_at: datetime | None = None


class PlanConfirmationState(BaseModel):
    plan_id: str
    status: SessionStatus
    version: int
    round: int
    confirmations: list[PlanConfirmation]
    needs_replan: bool
    replan_candidates: list[ReplanCandidate]


class PendingReplan(BaseModel):
    replan_id: str
    base_version: int
    result: ReplanResult


class _Snapshot(BaseModel):
    version: int
    scenario: Scenario
    plan: Plan
    routes: list[VoyagePickup]
    air_locked_lot_ids: list[str]
    arrival_inspections: list[ArrivalInspection] = Field(default_factory=list)
    applied_charges: list[CancellationCharge] | None = None


@dataclass
class _State:
    session: PlanSession
    committed: _Snapshot
    orders: OrderedDict[str, ExecutionOrder]
    unconfirmed: set[str] = field(default_factory=set)
    release: set[str] = field(default_factory=set)
    withdraw: set[str] = field(default_factory=set)
    advanced: bool = False

    def settled_status(self) -> SessionStatus:
        shippers = {lot.shipper_id for lot in self.session.scenario.lots}
        if self.unconfirmed & shippers:
            return "pending_shipper_confirmation"
        return "committed"


TOTAL_FIELDS = (
    "total_baseline_cost",
    "total_shared_cost",
    "total_savings",
    "total_fee",
    "total_operational_cost",
    "total_operational_savings",
)


def _totals(plan: Plan) -> dict[str, int]:
    totals = {name: getattr(plan, name) for name in TOTAL_FIELDS}
    totals["assigned_lots"] = len(plan.assignments)
    totals["unassigned_lots"] = len(plan.unassigned)
    return totals


def _summary(row: PlanRow) -> PlanSummary:
    return PlanSummary(
        plan_id=row.plan_id,
        scenario_id=row.scenario_id,
        version=row.version,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        lot_count=row.lot_count,
        totals=row.totals,
    )


def _dump_pending(pending: PendingReplan) -> dict[str, Any]:
    after = pending.result.scenario_after
    return {
        "pending": pending.model_dump(mode="json"),
        "scenario_after": after.model_dump(mode="json") if after is not None else None,
    }


def _load_pending(payload: dict[str, Any]) -> PendingReplan:
    pending = PendingReplan.model_validate(payload["pending"])
    if payload.get("scenario_after") is not None:
        pending.result.scenario_after = Scenario.model_validate(payload["scenario_after"])
    return pending


def _now() -> datetime:
    return datetime.now(UTC)


def sea_containers(plan: Plan) -> set[str]:
    return {a.container_id for a in plan.assignments if a.mode == "sea" and a.container_id}


def _booked(db: Session, org_id: str, scenario_id: str, plan_id: str = "") -> set[str]:
    if org_id == DEMO_ORG:
        return set()
    params = {"org": org_id, "scenario": scenario_id, "plan": plan_id}
    reserved = set(
        db.scalars(
            select(ForwarderResourceRow.id).where(
                ForwarderResourceRow.allocated_plan_id.is_not(None),
                ForwarderResourceRow.allocated_plan_id != plan_id,
            )
        )
    )
    return {c for c in db.scalars(BOOKED_CONTAINERS, params) if c} | reserved


def _sync_resources(db: Session, session: PlanSession, committed: _Snapshot, org_id: str) -> None:
    try:
        sync_allocations(
            db,
            session.plan_id,
            org_id,
            [(session.scenario, session.plan), (committed.scenario, committed.plan)],
            session.updated_at,
        )
    except ResourceUnavailable as exc:
        raise ContainerBooked(exc.resource_ids) from exc


def _lock_bookings(db: Session, org_id: str) -> None:
    db.execute(select(OrganizationRow.id).where(OrganizationRow.id == org_id).with_for_update())


def _payload(
    db: Session,
    session: PlanSession,
    owners: dict[str, str | None],
    created_by: datetime | None = None,
) -> dict[str, Any]:
    registered = set(session.registered_lot_ids)
    owners = {i: org for i, org in owners.items() if i in registered}
    missing = [i for i in session.registered_lot_ids if i not in owners]
    if missing:
        owners = {**owners, **lot_owners(db, missing, created_by)}
    return {
        **session.model_dump(mode="json"),
        "lot_owner_orgs": owners,
        "shipper_org_ids": sorted({org for org in owners.values() if org}),
    }


def _stored_owners(row: PlanRow) -> tuple[dict[str, str | None], datetime | None]:
    if "lot_owner_orgs" in row.payload:
        return dict(row.payload["lot_owner_orgs"]), None
    return {}, row.created_at


def _mark_booked(session: PlanSession, result: ReplanResult, booked: set[str]) -> None:
    changed = False
    for alternative in result.alternatives:
        clash = sorted(sea_containers(alternative.plan) & booked)
        if alternative.feasible and clash:
            alternative.feasible = False
            alternative.infeasible_reasons.append(
                f"다른 계획에 이미 배정된 컨테이너를 씁니다: {', '.join(clash)}"
            )
            changed = True
    if not changed:
        return
    feasible = [a for a in result.alternatives if a.feasible]
    if result.recommended_id not in {a.id for a in feasible}:
        best = min(feasible, key=lambda a: (a.total_cost, PREFERENCE[a.id]), default=None)
        result.recommended_id = best.id if best else None
        result.diff = best.diff if best else None
    if result.explanation is not None:
        result.explanation = render_template(
            build_explain_context(session.scenario, session.plan, result)
        )


def _confirmation(row: PlanConfirmationRow) -> PlanConfirmation:
    return PlanConfirmation.model_validate(row, from_attributes=True)


def _round_rows(db: Session, plan_id: str, round_no: int) -> list[PlanConfirmationRow]:
    return list(
        db.scalars(
            select(PlanConfirmationRow)
            .where(PlanConfirmationRow.plan_id == plan_id, PlanConfirmationRow.round == round_no)
            .order_by(PlanConfirmationRow.shipper_id)
        )
    )


def _unconfirmed(db: Session, row: PlanRow) -> set[str]:
    if not row.confirmation_round:
        return set()
    return {
        c.shipper_id
        for c in _round_rows(db, row.plan_id, row.confirmation_round)
        if c.status != "accepted"
    }


def _present_shippers(row: PlanRow) -> set[str]:
    lots = (row.payload.get("scenario") or {}).get("lots") or []
    return {lot["shipper_id"] for lot in lots}


def awaiting_shippers(db: Session, row: PlanRow) -> set[str]:
    return _unconfirmed(db, row) & _present_shippers(row)


def _accepted(db: Session, row: PlanRow) -> set[str]:
    if not row.confirmation_round:
        return set()
    return {
        c.shipper_id
        for c in _round_rows(db, row.plan_id, row.confirmation_round)
        if c.status == "accepted"
    }


def _sandbox(db: Session, org_id: str) -> bool:
    return bool(db.scalar(select(OrganizationRow.sandbox).where(OrganizationRow.id == org_id)))


def _has_forwarder(db: Session, plan_id: str) -> bool:
    found = db.scalar(select(PlanForwarderRow.plan_id).where(PlanForwarderRow.plan_id == plan_id))
    return found is not None


def _current_owners(db: Session, row: PlanRow, session: PlanSession) -> dict[str, str | None]:
    owners, created_by = _stored_owners(row)
    missing = [i for i in session.registered_lot_ids if i not in owners]
    return {**owners, **lot_owners(db, missing, created_by)}


def _cleared(db: Session, row: PlanRow, session: PlanSession) -> set[str]:
    if row.confirmation_round:
        return _accepted(db, row)
    return set(_participants(session, _current_owners(db, row, session)))


def _guard_real_apply(db: Session, row: PlanRow, session: PlanSession, event: Event,
                      *, refill_invitation: bool = False) -> None:
    if row.org_id == DEMO_ORG or _sandbox(db, row.org_id):
        return
    if (
        session.status == "pending_shipper_confirmation"
        and _rejecting_shipper(db, row, session, event) is None
    ):
        raise SessionConflict(APPLY_BEFORE_CONFIRMATION)
    owner = db.get(OrganizationRow, row.org_id)
    if (
        isinstance(event, AddLotEvent)
        and db.get(LotRow, event.lot.id) is not None
        and event.lot.shipper_id not in _cleared(db, row, session)
        and _has_forwarder(db, row.plan_id)
        and (owner is None or owner.type != "forwarder")
        and not refill_invitation
    ):
        raise SessionConflict(ADD_BEFORE_CONFIRMATION)


def _withdraw(db: Session, row: PlanRow, shipper_ids: set[str]) -> None:
    if not shipper_ids or not row.confirmation_round:
        return
    db.execute(
        delete(PlanConfirmationRow).where(
            PlanConfirmationRow.plan_id == row.plan_id,
            PlanConfirmationRow.round == row.confirmation_round,
            PlanConfirmationRow.shipper_id.in_(shipper_ids),
        )
    )
    kept = [
        c
        for c in _candidates(row)
        if not (c.source == "shipper_rejected" and c.shipper_id in shipper_ids)
    ]
    if len(kept) != len(row.replan_candidates or []):
        _set_candidates(row, kept)


def _shippers_of(scenario: Scenario) -> set[str]:
    return {lot.shipper_id for lot in scenario.lots}


def _require_execution_consent(state: _State, order: ExecutionOrder | None = None) -> None:
    if state.unconfirmed & _shippers_of(state.session.scenario):
        raise SessionConflict(
            "계획에 포함된 모든 화주가 배정을 수락해야 실행·회신·검수를 진행할 수 있습니다"
        )
    if order is not None and any(c.status != "accepted" for c in order.shipper_confirmations):
        raise SessionConflict("변경 영향을 받는 화주의 확인이 끝나야 실행할 수 있습니다")


def _departed(db: Session, row: PlanRow, state: _State) -> set[str]:
    if not row.confirmation_round:
        return set()
    listed = {c.shipper_id for c in _round_rows(db, row.plan_id, row.confirmation_round)}
    return listed - _shippers_of(state.session.scenario) - _shippers_of(state.committed.scenario)


def _prune_candidates(row: PlanRow, state: _State) -> None:
    if not state.advanced:
        return
    state.advanced = False
    if row.org_id == DEMO_ORG or not row.needs_replan:
        return
    candidates = _candidates(row)
    committed = state.committed
    kept = _still_relevant(candidates, committed.scenario, committed.plan)
    if len(kept) != len(candidates):
        _set_candidates(row, kept)


def _committed_orders(state: _State) -> bool:
    if any(o.status in ("forwarder_confirmed", "executed") for o in state.orders.values()):
        return True
    history = state.session.history
    rolled_back = {e.order_id for e in history if e.kind == "rollback"}
    return any(
        e.kind == "apply" and e.order_id not in state.orders and e.order_id not in rolled_back
        for e in history
    )


def _rejecting_shipper(db: Session, row: PlanRow, session: PlanSession, event: Event) -> str | None:
    if row.org_id == DEMO_ORG or not row.confirmation_round or not isinstance(event, CancelEvent):
        return None
    lot = next((lot for lot in session.scenario.lots if lot.id == event.lot_id), None)
    if lot is None:
        return None
    rejected = any(
        c.shipper_id == lot.shipper_id and c.status == "rejected"
        for c in _round_rows(db, row.plan_id, row.confirmation_round)
    )
    return lot.shipper_id if rejected else None


def _waive_diff(diff: PlanDiff | None, payer: str) -> bool:
    if diff is None or diff.cancellation is None or diff.cancellation.payer_shipper_id != payer:
        return False
    diff.cancellation = None
    for delta in diff.shippers:
        delta.cancellation_adjustment = 0
    return True


def _waive_fee(session: PlanSession, result: ReplanResult, payer: str) -> None:
    changed = [_waive_diff(a.diff, payer) for a in result.alternatives]
    changed.append(_waive_diff(result.diff, payer))
    if any(changed) and result.explanation is not None:
        result.explanation = render_template(
            build_explain_context(session.scenario, session.plan, result)
        )


def _excused_lots(
    db: Session, row: PlanRow, session: PlanSession, order: ExecutionOrder
) -> set[str]:
    if not row.confirmation_round:
        return set()
    rejected = {
        c.shipper_id
        for c in _round_rows(db, row.plan_id, row.confirmation_round)
        if c.status == "rejected"
    }
    excused = set(order.notified_shipper_ids) & rejected - _shippers_of(session.scenario)
    planned = {lot.id for lot in session.scenario.lots}
    gone = [i for i in session.registered_lot_ids if i not in planned]
    if not excused or not gone:
        return set()
    return set(
        db.scalars(select(LotRow.id).where(LotRow.id.in_(gone), LotRow.shipper_id.in_(excused)))
    )


def _excuse(order: ExecutionOrder, shipper_id: str, now: datetime) -> None:
    kept = [c for c in order.shipper_confirmations if c.shipper_id != shipper_id]
    if len(kept) == len(order.shipper_confirmations):
        return
    order.shipper_confirmations = kept
    order.notified_shipper_ids = sorted({*order.notified_shipper_ids, shipper_id})
    if kept:
        order.deadlines.shipper = min(c.deadline for c in kept)
        return
    if order.status == "proposed":
        order.status = "shipper_confirmed"
        order.log.append(
            OrderLogEntry(
                at=now,
                actor="system",
                action="확인이 필요한 화주 없음 → 포워더 확정 대기",
                status_after="shipper_confirmed",
            )
        )


def _participants(
    session: PlanSession, owners: dict[str, str | None]
) -> dict[str, tuple[str, list[str]]]:
    registered = set(session.registered_lot_ids)
    found: dict[str, tuple[str, list[str]]] = {}
    for lot in session.scenario.lots:
        org = owners.get(lot.id)
        if lot.id not in registered or not org:
            continue
        found.setdefault(lot.shipper_id, (org, []))[1].append(lot.id)
    return found


def _open_round(
    db: Session,
    row: PlanRow,
    session: PlanSession,
    owners: dict[str, str | None],
    now: datetime,
    auto: bool = False,
) -> bool:
    participants = _participants(session, owners)
    if not participants:
        return False
    row.confirmation_round = (row.confirmation_round or 0) + 1
    for shipper_id, (org, lot_ids) in sorted(participants.items()):
        db.add(
            PlanConfirmationRow(
                plan_id=row.plan_id,
                round=row.confirmation_round,
                shipper_id=shipper_id,
                shipper_org_id=org,
                lot_ids=lot_ids,
                status="accepted" if auto else "pending",
                auto_accepted=auto,
                created_at=now,
                decided_at=now if auto else None,
            )
        )
    db.flush()
    return not auto


def _candidates(row: PlanRow) -> list[ReplanCandidate]:
    return [ReplanCandidate.model_validate(c) for c in row.replan_candidates or []]


def _set_candidates(row: PlanRow, candidates: list[ReplanCandidate]) -> None:
    row.replan_candidates = [c.model_dump(mode="json") for c in candidates]
    row.needs_replan = bool(candidates)


def _still_declined(candidate: ReplanCandidate, plan: Plan) -> bool:
    placed = {a.lot_id: a for a in plan.assignments}
    for lot_id in candidate.lot_ids:
        a = placed.get(lot_id)
        if a is None:
            continue
        if candidate.container_id and a.container_id == candidate.container_id:
            return True
        if (
            not candidate.container_id
            and candidate.voyage_id
            and a.voyage_id == candidate.voyage_id
        ):
            return True
    return False


def _still_relevant(
    candidates: list[ReplanCandidate], scenario: Scenario, plan: Plan
) -> list[ReplanCandidate]:
    shippers = {lot.shipper_id for lot in scenario.lots}
    return [
        c
        for c in candidates
        if (c.source == "shipper_rejected" and c.shipper_id in shippers)
        or (c.source == "request_declined" and _still_declined(c, plan))
    ]


def _state_of(db: Session, row: PlanRow, session: PlanSession) -> PlanConfirmationState:
    rows = _round_rows(db, row.plan_id, row.confirmation_round) if row.confirmation_round else []
    return PlanConfirmationState(
        plan_id=row.plan_id,
        status=session.status,
        version=session.version,
        round=row.confirmation_round,
        confirmations=[_confirmation(c) for c in rows],
        needs_replan=row.needs_replan,
        replan_candidates=_candidates(row),
    )


def _registered_add(db: Session, row: PlanRow, event: Event) -> str | None:
    if row.org_id == DEMO_ORG or not isinstance(event, AddLotEvent):
        return None
    if db.get(LotRow, event.lot.id) is None:
        return None
    return event.lot.id


def _trusted_request_initiator(
    db: Session,
    row: PlanRow,
    replan_id: str,
    event: Event,
    scenario: Scenario,
) -> str | None:
    if not isinstance(event, CancelEvent | AirSwitchEvent) and event.type not in {"reduce", "increase"}:
        return None
    request = db.scalar(
        select(ChangeRequestRow)
        .where(
            ChangeRequestRow.plan_id == row.plan_id,
            ChangeRequestRow.org_id == row.org_id,
            ChangeRequestRow.replan_id == replan_id,
            ChangeRequestRow.status == "open",
            ChangeRequestRow.requested_by.is_not(None),
            ChangeRequestRow.lot_id == event.lot_id,
            ChangeRequestRow.type == event.type,
        )
        .limit(1)
    )
    if request is None:
        return None
    if event.type in {"reduce", "increase"} and request.new_pallets != event.new_pallets:
        return None
    lot = next((lot for lot in scenario.lots if lot.id == request.lot_id), None)
    if lot is None or lot.shipper_id != request.shipper_id:
        return None
    return request.shipper_id


def _same_claim_world(owner: Any, shipper: Any) -> bool:
    if owner is None or shipper is None:
        return False
    if owner.world_id is not None:
        return owner.world_id == shipper.world_id
    return (
        shipper.world_id is None
        and not owner.sandbox
        and not shipper.sandbox
        and owner.type != "demo"
        and shipper.type != "demo"
    )


def _claim_added(
    db: Session, owner_org: str, event: AddLotEvent, now: datetime, required: bool = False
) -> str | None:
    lot_id = event.lot.id
    lot = db.get(LotRow, lot_id, with_for_update=True)
    if lot is None:
        if required:
            raise LotTaken([lot_id])
        return None
    rows = db.execute(
        select(
            OrganizationRow.id,
            OrganizationRow.world_id,
            OrganizationRow.sandbox,
            OrganizationRow.type,
        ).where(OrganizationRow.id.in_((owner_org, lot.shipper_org_id)))
    ).all()
    found = {row.id: row for row in rows}
    if not _same_claim_world(found.get(owner_org), found.get(lot.shipper_org_id)):
        raise LotTaken([lot_id])
    try:
        claim_lots(db, [event.lot], now)
    except LotUnavailable as exc:
        raise LotTaken(exc.lot_ids) from exc
    return lot.shipper_org_id


def _declined(order: ExecutionOrder, request: Any, now: datetime) -> ReplanCandidate:
    lot_ids = list(request.lot_ids)
    rebook = request.counterparty_role in SEA_ROLES and request.kind != "booking_cancel"
    return ReplanCandidate(
        id=str(uuid.uuid4()),
        source="request_declined",
        created_at=now,
        lot_ids=lot_ids,
        voyage_id=request.voyage_id,
        container_id=request.container_id,
        order_id=order.order_id,
        request_id=request.request_id,
        request_kind=request.kind,
        counterparty=request.counterparty,
        note=request.note,
        events=[AirSwitchEvent(lot_id=i) for i in lot_ids] if rebook else [],
    )


def _rejected(row: PlanConfirmationRow, scenario: Scenario, now: datetime) -> ReplanCandidate:
    present = {lot.id for lot in scenario.lots}
    lot_ids = [i for i in row.lot_ids if i in present]
    return ReplanCandidate(
        id=str(uuid.uuid4()),
        source="shipper_rejected",
        created_at=now,
        lot_ids=lot_ids,
        shipper_id=row.shipper_id,
        note=row.reason,
        events=[CancelEvent(lot_id=i) for i in lot_ids],
    )


def _invite_added(
    db: Session,
    row: PlanRow,
    session: PlanSession,
    event: AddLotEvent,
    shipper_org: str,
    now: datetime,
) -> str | None:
    if not row.confirmation_round:
        _open_round(db, row, session, _current_owners(db, row, session), now, auto=True)
        row.confirmation_round = row.confirmation_round or 1
    shipper_id = event.lot.shipper_id
    if any(
        c.shipper_id == shipper_id for c in _round_rows(db, row.plan_id, row.confirmation_round)
    ):
        return None
    db.add(
        PlanConfirmationRow(
            plan_id=row.plan_id,
            round=row.confirmation_round,
            shipper_id=shipper_id,
            shipper_org_id=shipper_org,
            lot_ids=[event.lot.id],
            status="pending",
            auto_accepted=False,
            created_at=now,
        )
    )
    db.flush()
    return shipper_id


def _notify_confirmation(session: PlanSession, owner_org_id: str, shipper_ids: set[str]) -> None:
    if not shipper_ids:
        return
    from freshswitch_backend.notifications import notify_plan_confirmation

    notify_plan_confirmation(session, owner_org_id, session.updated_at, shipper_ids=shipper_ids)


def _snapshot(session: PlanSession) -> _Snapshot:
    return _Snapshot(
        version=session.version,
        scenario=session.scenario,
        plan=session.plan,
        routes=session.routes,
        air_locked_lot_ids=session.air_locked_lot_ids,
        arrival_inspections=session.arrival_inspections,
        applied_charges=[],
    )


def _unregister(state: _State, gone: set[str]) -> list[str]:
    registered = state.session.registered_lot_ids
    state.release |= gone & set(registered)
    committed = state.committed
    if any(lot.id in gone for lot in committed.scenario.lots):
        lots = [lot for lot in committed.scenario.lots if lot.id not in gone]
        scenario = committed.scenario.model_copy(update={"lots": lots})
        state.committed = committed.model_copy(update={"scenario": scenario})
    return [i for i in registered if i not in gone]


def _charged(charge: CancellationCharge | None) -> list[CancellationCharge]:
    return [charge] if charge is not None and charge.fee else []


def _earlier_charges(
    committed: _Snapshot, superseded: list[ExecutionOrder]
) -> list[CancellationCharge]:
    if committed.applied_charges is not None:
        return list(committed.applied_charges)
    return [c for o in superseded for c in _charged(o.cancellation)]


def _cumulative(
    committed: _Snapshot,
    scenario_after: Scenario,
    alternative: Alternative,
    charges: list[CancellationCharge],
) -> Alternative:
    diff = diff_plans(
        scenario_after,
        committed.plan,
        alternative.plan,
        routes_before=committed.routes,
        routes_after=alternative.routes,
    )
    adjustments: dict[str, int] = {}
    for charge in charges:
        payer = charge.payer_shipper_id
        adjustments[payer] = adjustments.get(payer, 0) + charge.fee
        for transfer in charge.transfers:
            sid = transfer.shipper_id
            adjustments[sid] = adjustments.get(sid, 0) - transfer.amount
    for delta in diff.shippers:
        delta.cancellation_adjustment = adjustments.get(delta.shipper_id, 0)
    diff.cancellation = charges[-1] if charges else alternative.diff.cancellation
    return alternative.model_copy(update={"diff": diff})


def _charge_text(charge: CancellationCharge, names: dict[str, str]) -> str:
    recipients = [
        f"{short_name(names.get(t.shipper_id, t.shipper_id))} {t.amount:,}원"
        for t in charge.transfers
    ]
    if charge.consolidator_amount > 0:
        recipients.append(
            f"{CONSOLIDATOR_LABEL[charge.consolidator]} {charge.consolidator_amount:,}원"
        )
    payer = short_name(names.get(charge.payer_shipper_id, charge.payer_shipper_id))
    return f"{payer} {charge.fee:,}원 → {', '.join(recipients)}"


def _charge_payers(
    order: ExecutionOrder,
    charges: list[CancellationCharge],
    alternative: Alternative,
    scenarios: tuple[Scenario, Scenario],
) -> None:
    names = {s.id: s.name for scenario in reversed(scenarios) for s in scenario.shippers}
    deltas = {d.shipper_id: d for d in alternative.diff.shippers}
    default_deadline = order.deadlines.forwarder - timedelta(
        hours=ExecutionParams().forwarder_processing_hours.value
    )
    confirmations = {c.shipper_id: c for c in order.shipper_confirmations}
    added = False
    for sid in sorted({c.payer_shipper_id for c in charges}):
        item = confirmations.get(sid)
        if item is None:
            delta = deltas.get(sid)
            cost_delta = delta.cost_delta if delta else 0
            adjustment = delta.cancellation_adjustment if delta else 0
            item = ShipperConfirmation(
                shipper_id=sid,
                shipper_name=names.get(sid, sid),
                lot_ids=sorted({c.lot_id for c in charges if c.payer_shipper_id == sid}),
                reasons=[],
                reason_text=[],
                cost_before=delta.cost_before if delta else 0,
                cost_after=delta.cost_after if delta else 0,
                cost_delta=cost_delta,
                cancellation_adjustment=adjustment,
                net_cost_delta=cost_delta + adjustment,
                deadline=default_deadline,
            )
            confirmations[sid] = item
            added = True
        if "cancellation_fee" not in item.reasons:
            item.reasons.append("cancellation_fee")
            item.reason_text.append(REASON_TEXT["cancellation_fee"])
    order.shipper_confirmations = [confirmations[sid] for sid in sorted(confirmations)]
    order.notified_shipper_ids = [s for s in order.notified_shipper_ids if s not in confirmations]
    if added:
        order.deadlines.shipper = min(c.deadline for c in order.shipper_confirmations)
    if added and order.status == "shipper_confirmed":
        order.status = "proposed"
        order.log = [entry for entry in order.log if entry.status_after == "proposed"]
    if len(charges) > 1:
        for action in order.automatic:
            if action.kind == "settlement_recompute":
                head = action.description.split(". 취소 부담금 ", 1)[0]
                listed = ", ".join(_charge_text(c, names) for c in charges)
                action.description = f"{head}. 취소 부담금 {listed}"


def _deadline_passed(order: ExecutionOrder, now: datetime) -> str | None:
    if order.status == "proposed" and order.deadlines.shipper <= now:
        return f"화주 확인 기한({fmt(order.deadlines.shipper)})"
    if order.deadlines.forwarder <= now:
        return f"포워더 확정 기한({fmt(order.deadlines.forwarder)})"
    return None


class SessionStore:
    def __init__(self, max_sessions: int = MAX_SESSIONS, db: Database | None = None) -> None:
        self._db = db or database
        self._lock = threading.RLock()
        self._max = max_sessions
        self.clock: Callable[[], datetime] = _now

    def _load(self, db: Session, plan_id: str, org_id: str = DEMO_ORG) -> _State | None:
        row = _plan_row(db, plan_id, org_id)
        if row is None:
            return None
        orders = db.scalars(
            select(OrderRow).where(OrderRow.plan_id == plan_id).order_by(OrderRow.seq)
        )
        return _State(
            session=PlanSession.model_validate(row.payload),
            committed=_Snapshot.model_validate(row.committed),
            orders=OrderedDict(
                (o.order_id, ExecutionOrder.model_validate(o.payload)) for o in orders
            ),
            unconfirmed=_unconfirmed(db, row),
        )

    def _save(self, db: Session, state: _State) -> None:
        session = state.session
        row = db.get(PlanRow, session.plan_id)
        if row is None:
            return
        if state.committed.version != row.committed.get("version"):
            from freshswitch_backend.planning.quantity_policy import sync_committed

            sync_committed(db, row, state.session, state.session.updated_at)
        row.version = session.version
        row.status = session.status
        row.updated_at = session.updated_at
        row.lot_count = len(session.scenario.lots)
        row.totals = _totals(session.plan)
        row.payload = _payload(db, session, *_stored_owners(row))
        row.committed = state.committed.model_dump(mode="json")
        _sync_resources(db, session, state.committed, row.org_id)
        for order in state.orders.values():
            stored = db.get(OrderRow, order.order_id)
            payload = order.model_dump(mode="json")
            if stored is None:
                db.add(
                    OrderRow(
                        order_id=order.order_id,
                        plan_id=session.plan_id,
                        status=order.status,
                        created_at=order.created_at,
                        updated_at=order.updated_at,
                        payload=payload,
                    )
                )
            else:
                stored.status = order.status
                stored.updated_at = order.updated_at
                stored.payload = payload
        db.flush()
        _trim(db, OrderRow, OrderRow.plan_id == session.plan_id, MAX_ORDERS)
        if state.release:
            release_lots(db, state.release, session.updated_at)
            state.release = set()
        gone = state.withdraw | _departed(db, row, state)
        if gone:
            _withdraw(db, row, gone)
            state.unconfirmed -= gone
            state.withdraw = set()
        _prune_candidates(row, state)

    def _has_due(self, db: Session, plan_id: str) -> bool:
        return bool(self._due_plans(db, OrderRow.plan_id == plan_id))

    def _due_plans(self, db: Session, where: Any) -> list[str]:
        now = self.clock()
        rows = db.execute(
            select(OrderRow.plan_id, OrderRow.payload)
            .join(PlanRow, PlanRow.plan_id == OrderRow.plan_id)
            .where(OrderRow.status.in_(("proposed", "shipper_confirmed")), where)
            .order_by(OrderRow.seq)
        )
        due: list[str] = []
        for plan_id, payload in rows:
            order = ExecutionOrder.model_validate(payload)
            deadline = (
                order.deadlines.shipper if order.status == "proposed" else order.deadlines.forwarder
            )
            if now > deadline and plan_id not in due:
                due.append(plan_id)
        return due

    def expire_due(self, where: Any) -> int:
        if where is None:
            return 0
        with self._lock:
            with self._db.begin() as db:
                due = self._due_plans(db, where)
            for plan_id in due:
                self._expire(plan_id, ANY_ORG)
            return len(due)

    def _expire(self, plan_id: str, org_id: str = DEMO_ORG) -> None:
        with self._db.begin() as db:
            if _plan_row(db, plan_id, org_id) is None or not self._has_due(db, plan_id):
                return
            state = self._load(db, plan_id, org_id)
            if state is not None and self._expire_due(state):
                self._save(db, state)

    def _require(self, db: Session, plan_id: str, org_id: str = DEMO_ORG) -> _State:
        state = self._load(db, plan_id, org_id)
        if state is None:
            raise KeyError(plan_id)
        return state

    def create(
        self,
        scenario: Scenario,
        registered_lot_ids: list[str] | None = None,
        org_id: str = DEMO_ORG,
        lot_org_id: str | None = None,
        exclusive: bool = False,
        auto_accept: bool = False,
        quantity_policy_v1: bool = False,
    ) -> PlanSession:
        working = scenario.model_copy(deep=True)
        built = build_plan(working)
        now = self.clock()
        session = PlanSession(
            plan_id=str(uuid.uuid4()),
            scenario_id=scenario.id,
            version=1,
            created_at=now,
            updated_at=now,
            scenario=working,
            plan=built.plan,
            routes=built.routes,
            air_locked_lot_ids=[],
            history=[],
            registered_lot_ids=registered_lot_ids or [],
        )
        invited: set[str] = set()
        with self._lock, self._db.begin() as db:
            if exclusive:
                _lock_bookings(db, org_id)
                chosen = set(session.registered_lot_ids)
                owners = claim_lots(db, [lot for lot in scenario.lots if lot.id in chosen], now)
                clash = sea_containers(session.plan) & _booked(db, org_id, scenario.id)
                if clash:
                    raise ContainerBooked(sorted(clash))
            else:
                mark_planned(db, session.registered_lot_ids, now, lot_org_id or org_id)
                owners = {}
            row = PlanRow(
                plan_id=session.plan_id,
                org_id=org_id,
                scenario_id=session.scenario_id,
                version=session.version,
                status=session.status,
                lot_count=len(working.lots),
                totals=_totals(session.plan),
                created_at=now,
                updated_at=now,
                payload=_payload(db, session, owners),
                committed=_snapshot(session).model_dump(mode="json"),
                confirmation_round=0,
                needs_replan=False,
                replan_candidates=[],
            )
            db.add(row)
            db.flush()
            if quantity_policy_v1:
                from freshswitch_backend.planning.quantity_policy import initialize_policies

                initialize_policies(db, row, session, now)
            _sync_resources(db, session, _snapshot(session), org_id)
            owner_org = db.get(OrganizationRow, org_id)
            if owner_org is not None and owner_org.type == "forwarder":
                db.add(PlanForwarderRow(plan_id=session.plan_id, forwarder_org_id=org_id))
            from freshswitch_backend.persistence.sandbox_world import extend_first_plan

            extend_first_plan(db, org_id)
            if (
                exclusive
                and org_id != DEMO_ORG
                and _open_round(db, row, session, owners, now, auto=auto_accept)
            ):
                session.status = "pending_shipper_confirmation"
                row.status = session.status
                row.payload = _payload(db, session, owners)
                invited = set(_participants(session, owners))
            scope = PlanRow.org_id == org_id
            cutoff = _cutoff(db, PlanRow, scope, self._max)
            if cutoff is not None:
                self._drop(db, (PlanRow.seq <= cutoff) & scope)
        _notify_confirmation(session, org_id, invited)
        if org_id != DEMO_ORG:
            from freshswitch_backend.persistence.sandbox_world import seed_visitor_telemetry

            seed_visitor_telemetry(session.plan_id, org_id)
        return session

    def booked_containers(self, org_id: str, scenario_id: str, plan_id: str = "") -> set[str]:
        with self._db.begin() as db:
            return _booked(db, org_id, scenario_id, plan_id)

    def get(self, plan_id: str, org_id: str = DEMO_ORG) -> PlanSession | None:
        with self._lock:
            self._expire(plan_id, org_id)
            with self._db.begin() as db:
                row = _plan_row(db, plan_id, org_id)
                return PlanSession.model_validate(row.payload) if row is not None else None

    def org_of(self, plan_id: str) -> str | None:
        with self._db.begin() as db:
            return db.scalar(select(PlanRow.org_id).where(PlanRow.plan_id == plan_id))

    def summaries(
        self, limit: int = 100, offset: int = 0, org_id: str = DEMO_ORG
    ) -> list[PlanSummary]:
        self.expire_due(_plan_scope(org_id))
        with self._db.begin() as db:
            rows = db.scalars(
                select(PlanRow)
                .where(_plan_scope(org_id))
                .order_by(PlanRow.created_at.desc(), PlanRow.seq.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_summary(row) for row in rows]

    def _drop(self, db: Session, condition: Any) -> int:
        rows = db.execute(
            delete(PlanRow)
            .where(condition)
            .returning(PlanRow.plan_id, PlanRow.payload["registered_lot_ids"])
        ).all()
        plan_ids = [row[0] for row in rows]
        if not plan_ids:
            return 0
        for binding in db.scalars(select(BindingRow).where(BindingRow.plan_id.in_(plan_ids))):
            binding.plan_id = None
            binding.payload = {**binding.payload, "plan_id": None}
        db.execute(delete(ContainerRow).where(ContainerRow.plan_id.in_(plan_ids)))
        db.execute(
            delete(ObservationRow).where(
                ObservationRow.origin == "arrival", ObservationRow.plan_id.in_(plan_ids)
            )
        )
        release_lots(db, {lot_id for row in rows for lot_id in row[1] or []}, self.clock())
        return len(plan_ids)

    def delete(self, plan_id: str, org_id: str = DEMO_ORG) -> bool:
        with self._lock, self._db.begin() as db:
            from freshswitch_backend.planning.quantity_policy import guard_policy_deletion

            if _plan_row(db, plan_id, org_id) is not None:
                guard_policy_deletion(db, plan_id)
            return self._drop(db, (PlanRow.plan_id == plan_id) & _plan_scope(org_id)) > 0

    def count(self, org_id: str = DEMO_ORG) -> int:
        with self._db.begin() as db:
            return (
                db.scalar(select(func.count()).select_from(PlanRow).where(_plan_scope(org_id))) or 0
            )

    def clear(self, org_id: str = DEMO_ORG) -> int:
        with self._lock, self._db.begin() as db:
            count = self._drop(db, PlanRow.org_id == org_id)
            release_lots(db, None, self.clock(), org_id=org_id)
            return count

    def _idempotent(
        self,
        db: Session,
        plan_id: str,
        key: str,
        kind: str,
        fingerprint: str,
        model: type[BaseModel] | None,
    ) -> Any | None:
        stored = db.get(IdempotencyRow, (plan_id, key))
        if stored is None:
            return None
        if stored.kind != kind or stored.fingerprint != fingerprint:
            raise IdempotencyMismatch(
                "같은 idempotency_key로 다른 요청이 이미 처리됐습니다. 새 키를 쓰세요"
            )
        return model.model_validate(stored.payload) if model is not None else stored.payload

    def idempotent(
        self,
        plan_id: str,
        key: str,
        kind: str,
        fingerprint: str,
        model: type[BaseModel] | None = None,
        org_id: str = DEMO_ORG,
    ) -> Any | None:
        with self._lock, self._db.begin() as db:
            if _plan_row(db, plan_id, org_id) is None:
                return None
            return self._idempotent(db, plan_id, key, kind, fingerprint, model)

    def _remember(
        self, db: Session, plan_id: str, key: str, kind: str, fingerprint: str, value: BaseModel
    ) -> Any:
        replayed = self._idempotent(db, plan_id, key, kind, fingerprint, type(value))
        if replayed is not None:
            return replayed
        db.add(
            IdempotencyRow(
                plan_id=plan_id,
                key=key,
                kind=kind,
                fingerprint=fingerprint,
                payload=value.model_dump(mode="json"),
            )
        )
        db.flush()
        _trim(db, IdempotencyRow, IdempotencyRow.plan_id == plan_id, MAX_IDEMPOTENCY_KEYS)
        return value

    def remember(
        self,
        plan_id: str,
        key: str,
        kind: str,
        fingerprint: str,
        value: BaseModel,
        org_id: str = DEMO_ORG,
    ) -> Any:
        with self._lock, self._db.begin() as db:
            if _plan_row(db, plan_id, org_id) is None:
                return value
            return self._remember(db, plan_id, key, kind, fingerprint, value)

    def add_pending(
        self, session: PlanSession, result: ReplanResult, org_id: str = DEMO_ORG
    ) -> PendingReplan:
        pending = PendingReplan(
            replan_id=str(uuid.uuid4()), base_version=session.version, result=result
        )
        with self._lock, self._db.begin() as db:
            row = _plan_row(db, session.plan_id, org_id)
            if row is None:
                return pending
            _mark_booked(session, result, _booked(db, row.org_id, row.scenario_id, row.plan_id))
            rejecting = _rejecting_shipper(db, row, session, result.event)
            if rejecting:
                _waive_fee(session, result, rejecting)
            payload = _dump_pending(pending)
            added = _registered_add(db, row, result.event)
            if added:
                payload[REGISTERED_ADD] = added
            db.add(
                PendingRow(
                    replan_id=pending.replan_id,
                    plan_id=session.plan_id,
                    base_version=pending.base_version,
                    payload=payload,
                )
            )
            db.flush()
            _trim(db, PendingRow, PendingRow.plan_id == session.plan_id, MAX_PENDING)
        return pending

    def mark_registered_add(
        self, plan_id: str, replan_id: str, lot_id: str, org_id: str = DEMO_ORG
    ) -> None:
        with self._lock, self._db.begin() as db:
            if _plan_row(db, plan_id, org_id) is None:
                return
            row = db.get(PendingRow, replan_id, with_for_update=True)
            if row is None or row.plan_id != plan_id:
                return
            row.payload = {**row.payload, REGISTERED_ADD: lot_id}

    def _pending(
        self, db: Session, plan_id: str, replan_id: str | None
    ) -> tuple[PendingReplan, str | None]:
        query = select(PendingRow).where(PendingRow.plan_id == plan_id)
        if replan_id is None:
            row = db.scalars(query.order_by(PendingRow.seq.desc()).limit(1)).first()
            if row is None:
                raise SessionConflict("적용할 재최적화 결과가 없습니다. 먼저 이벤트를 보내세요")
        else:
            row = db.scalars(query.where(PendingRow.replan_id == replan_id)).first()
            if row is None:
                raise KeyError(replan_id)
        return _load_pending(row.payload), row.payload.get(REGISTERED_ADD)

    def apply(
        self,
        plan_id: str,
        alternative_id: str,
        replan_id: str | None = None,
        idempotency: tuple[str, str] | None = None,
        org_id: str = DEMO_ORG,
    ) -> PlanSession:
        with self._lock:
            if idempotency is not None:
                replayed = self.idempotent(
                    plan_id, idempotency[0], "apply", idempotency[1], PlanSession, org_id
                )
                if replayed is not None:
                    return replayed
            self._expire(plan_id, org_id)
            with self._db.begin() as db:
                updated, invited = self._apply(
                    db, plan_id, alternative_id, replan_id, idempotency, org_id
                )
            _notify_confirmation(updated, org_id, invited)
            return updated

    def _apply(
        self,
        db: Session,
        plan_id: str,
        alternative_id: str,
        replan_id: str | None,
        idempotency: tuple[str, str] | None,
        org_id: str = DEMO_ORG,
        *,
        refill_invitation: bool = False,
    ) -> tuple[PlanSession, set[str]]:
        locked = _plan_row(db, plan_id, org_id)
        if locked is not None and locked.org_id != DEMO_ORG:
            _lock_bookings(db, locked.org_id)
            db.refresh(locked, with_for_update=True)
        state = self._require(db, plan_id, org_id)
        session = state.session
        pending, registered_add = self._pending(db, plan_id, replan_id)
        if pending.base_version != session.version:
            raise SessionConflict(
                "이미 다른 대안이 적용된 계획입니다. 이벤트를 다시 보내 재최적화하세요"
            )
        alternative = next((a for a in pending.result.alternatives if a.id == alternative_id), None)
        if alternative is None:
            raise KeyError(alternative_id)
        if not alternative.feasible:
            raise SessionConflict(
                f"실행 불가한 대안입니다: {'; '.join(alternative.infeasible_reasons)}"
            )
        row = _plan_row(db, plan_id, ANY_ORG)
        event = pending.result.event
        _guard_real_apply(db, row, session, event, refill_invitation=refill_invitation)
        rejecting = _rejecting_shipper(db, row, session, event)
        if rejecting:
            _waive_fee(session, pending.result, rejecting)
        from freshswitch_backend.planning.quantity_policy import guard_quantity_apply, has_policy

        policy_v1 = has_policy(db, plan_id)
        if policy_v1:
            guard_quantity_apply(db, row, event, rejecting)
            alternative.diff.cancellation = None
            for delta in alternative.diff.shippers:
                delta.cancellation_adjustment = 0
        if row.org_id != DEMO_ORG:
            clash = sea_containers(alternative.plan) & _booked(
                db, row.org_id, row.scenario_id, plan_id
            )
            if clash:
                raise ContainerBooked(sorted(clash))
        scenario = pending.result.scenario_after
        locked = set(session.air_locked_lot_ids)
        if locks_air(scenario, event):
            locked.add(event.lot_id)
        if isinstance(event, CancelEvent):
            locked.discard(event.lot_id)
        now = self.clock()
        registered_ids = session.registered_lot_ids
        if policy_v1:
            from freshswitch_backend.planning.quantity_policy import record_selected_recommendation

            record_selected_recommendation(db, row, session, pending, alternative, now)
        invited: set[str] = set()
        if row.org_id != DEMO_ORG and isinstance(event, AddLotEvent):
            shipper_org = _claim_added(
                db, row.org_id, event, now, required=registered_add == event.lot.id
            )
            if shipper_org:
                registered_ids = [*registered_ids, event.lot.id]
                invite = _invite_added(db, row, session, event, shipper_org, now)
                if invite:
                    invited.add(invite)
                    state.unconfirmed.add(invite)
        superseded = [
            o for o in state.orders.values() if o.status in ("proposed", "shipper_confirmed")
        ]
        for previous in superseded:
            self._transition(previous, "expired", now, "system", "새 대안 적용으로 대체됨")
            previous.expired_reason = "superseded"
        committed = state.committed
        before = session
        ordered = alternative
        charges = _charged(alternative.diff.cancellation)
        if committed.version != session.version:
            before = committed
            charges = [*_earlier_charges(committed, superseded), *charges]
            ordered = _cumulative(committed, scenario, alternative, charges)
        if policy_v1:
            charges = []
            ordered.diff.cancellation = None
            for delta in ordered.diff.shippers:
                delta.cancellation_adjustment = 0
        order = build_order(
            plan_id=plan_id,
            replan_id=pending.replan_id,
            base_version=session.version,
            target_version=session.version + 1,
            rollback_version=committed.version,
            scenario_before=before.scenario,
            plan_before=before.plan,
            routes_before=before.routes,
            scenario_after=scenario,
            alternative=ordered,
            event=event,
            now=now,
            initiator_shipper_id=_trusted_request_initiator(
                db,
                row,
                pending.replan_id,
                event,
                before.scenario,
            ),
            infer_event_initiator=row.org_id == DEMO_ORG,
        )
        order.cancellation_charges = list(charges)
        if rejecting:
            _excuse(order, rejecting, now)
        if charges:
            _charge_payers(order, charges, ordered, (before.scenario, scenario))
        if policy_v1 and (event.type in {"cancel", "reduce", "increase", "air_switch"} or refill_invitation):
            from freshswitch_backend.planning.quantity_policy import policy_deadlines

            policy_deadlines(db, plan_id, order, ordered, now)
        passed = _deadline_passed(order, now)
        if passed:
            raise SessionConflict(
                f"이 대안의 실행 지시는 만드는 순간 {passed}이 이미 지나 적용할 수 없습니다. "
                "다른 대안을 고르거나 이벤트를 다시 보내세요"
            )
        entry = HistoryEntry(
            seq=len(session.history) + 1,
            replan_id=pending.replan_id,
            event=event,
            alternative_id=alternative.id,
            applied_at=now,
            cost_before=plan_score(session.plan),
            cost_after=alternative.total_cost,
            cost_delta=alternative.cost_delta,
            solve_ms=pending.result.solve_ms,
            explanation=pending.result.explanation,
            order_id=order.order_id,
        )
        updated = session.model_copy(
            update={
                "version": session.version + 1,
                "updated_at": now,
                "scenario": scenario,
                "plan": alternative.plan,
                "routes": alternative.routes,
                "air_locked_lot_ids": sorted(locked),
                "history": [*session.history, entry],
                "status": "pending_confirmation",
                "active_order_id": order.order_id,
                "arrival_inspections": self._carry_inspections(session, scenario, alternative.plan),
                "registered_lot_ids": registered_ids,
            }
        )
        state.session = updated
        state.committed = committed.model_copy(update={"applied_charges": charges})
        state.orders[order.order_id] = order
        self._save(db, state)
        if idempotency is not None:
            self._remember(db, plan_id, idempotency[0], "apply", idempotency[1], updated)
        return updated, invited

    def orders(self, plan_id: str, org_id: str = DEMO_ORG) -> list[ExecutionOrder]:
        with self._lock:
            self._expire(plan_id, org_id)
            with self._db.begin() as db:
                if _plan_row(db, plan_id, org_id) is None:
                    return []
                rows = db.scalars(
                    select(OrderRow.payload)
                    .where(OrderRow.plan_id == plan_id)
                    .order_by(OrderRow.seq)
                )
                return [ExecutionOrder.model_validate(payload) for payload in rows]

    def order(self, plan_id: str, order_id: str, org_id: str = DEMO_ORG) -> ExecutionOrder:
        with self._lock:
            self._expire(plan_id, org_id)
        with self._db.begin() as db:
            return self._require(db, plan_id, org_id).orders[order_id]

    def _transition(
        self,
        order: ExecutionOrder,
        status: OrderStatus,
        now: datetime,
        actor: str,
        action: str,
        shipper_id: str | None = None,
        note: str | None = None,
    ) -> None:
        order.status = status
        order.updated_at = now
        order.log.append(
            OrderLogEntry(
                at=now,
                actor=actor,
                action=action,
                status_after=status,
                shipper_id=shipper_id,
                note=note,
            )
        )

    def _expire_due(self, state: _State) -> bool:
        now = self.clock()
        changed = False
        for order in state.orders.values():
            if order.status == "proposed" and now > order.deadlines.shipper:
                deadline = order.deadlines.shipper
            elif order.status == "shipper_confirmed" and now > order.deadlines.forwarder:
                deadline = order.deadlines.forwarder
            else:
                continue
            self._transition(order, "expired", now, "system", "확인 기한 초과로 만료")
            order.expired_reason = f"deadline {deadline.isoformat()}"
            self._rollback(state, order, now)
            changed = True
        return changed

    def _rollback(self, state: _State, order: ExecutionOrder, now: datetime) -> None:
        session = state.session
        if session.version != order.target_version:
            return
        snapshot = state.committed
        version = session.version + 1
        kept = {lot.id for lot in snapshot.scenario.lots}
        added = {
            o.event.lot.id: o.event.lot.shipper_id
            for o in [*state.orders.values(), order]
            if isinstance(o.event, AddLotEvent)
            and o.status not in ("forwarder_confirmed", "executed")
        }
        dropped = {i for i in session.registered_lot_ids if i in added and i not in kept}
        registered_ids = _unregister(state, dropped)
        remaining = {lot.shipper_id for lot in state.committed.scenario.lots}
        state.withdraw |= {added[i] for i in dropped} - remaining
        snapshot = state.committed
        entry = HistoryEntry(
            seq=len(session.history) + 1,
            replan_id=order.replan_id,
            event=order.event,
            alternative_id=order.alternative_id,
            applied_at=now,
            cost_before=plan_score(session.plan),
            cost_after=plan_score(snapshot.plan),
            cost_delta=plan_score(snapshot.plan) - plan_score(session.plan),
            solve_ms=0,
            kind="rollback",
            order_id=order.order_id,
        )
        state.session = session.model_copy(
            update={
                "version": version,
                "updated_at": now,
                "scenario": snapshot.scenario,
                "plan": snapshot.plan,
                "routes": snapshot.routes,
                "air_locked_lot_ids": snapshot.air_locked_lot_ids,
                "history": [*session.history, entry],
                "status": "committed",
                "committed_version": version,
                "active_order_id": None,
                "arrival_inspections": snapshot.arrival_inspections,
                "registered_lot_ids": registered_ids,
            }
        )
        state.session.status = state.settled_status()
        state.committed = snapshot.model_copy(update={"version": version, "applied_charges": []})
        order.rolled_back_to_version = snapshot.version

    def _commit(self, state: _State, order: ExecutionOrder, now: datetime) -> None:
        session = state.session
        if session.version != order.target_version:
            return
        updated = session.model_copy(
            update={
                "updated_at": now,
                "status": "committed",
                "committed_version": session.version,
            }
        )
        state.session = updated
        updated.status = state.settled_status()
        state.committed = _snapshot(updated)
        state.advanced = True

    def confirm(
        self,
        plan_id: str,
        order_id: str,
        body: ConfirmRequest,
        org_id: str = DEMO_ORG,
    ) -> ExecutionOrder:
        with self._lock:
            self._expire(plan_id, org_id)
            return self._confirm(plan_id, order_id, body, org_id)

    def _confirm(
        self, plan_id: str, order_id: str, body: ConfirmRequest, org_id: str
    ) -> ExecutionOrder:
        with self._db.begin() as db:
            state = self._require(db, plan_id, org_id)
            order = state.orders[order_id]
            now = self.clock()
            committed = state.session.committed_version
            if body.actor == "shipper":
                result = self._confirm_shipper(state, order, body, now)
            else:
                result = self._confirm_forwarder(state, order, body, now)
            session = state.session
            registered = list(session.registered_lot_ids)
            row = _plan_row(db, plan_id, ANY_ORG)
            settled = session.committed_version != committed and row.org_id != DEMO_ORG
            if settled:
                excused = _excused_lots(db, row, session, order)
                if excused:
                    session.registered_lot_ids = [i for i in registered if i not in excused]
            self._save(db, state)
            if settled:
                sync_committed_lots(db, registered, session.scenario.lots, now)
            return result

    def _confirm_shipper(
        self, state: _State, order: ExecutionOrder, body: ConfirmRequest, now: datetime
    ) -> ExecutionOrder:
        if not body.shipper_id:
            raise OrderInputError("화주 확인에는 shipper_id가 필요합니다")
        item = next(
            (c for c in order.shipper_confirmations if c.shipper_id == body.shipper_id), None
        )
        if item is None:
            raise OrderInputError(f"이 실행 지시에서 확인할 화주가 아닙니다: {body.shipper_id}")
        decision = "accepted" if body.accept else "rejected"
        if item.status != "pending":
            if item.status == decision:
                return order
            raise SessionConflict(f"이미 {item.status} 처리된 화주 확인입니다")
        if order.status != "proposed":
            raise SessionConflict(f"화주 확인을 받을 수 없는 상태입니다: {order.status}")
        item.status = decision
        item.note = body.note
        item.decided_at = now
        if not body.accept:
            self._transition(
                order,
                "rejected",
                now,
                "shipper",
                "화주 거절 → 이전 확정 계획으로 복귀",
                body.shipper_id,
                body.note,
            )
            self._rollback(state, order, now)
            return order
        if all(c.status == "accepted" for c in order.shipper_confirmations):
            self._transition(
                order,
                "shipper_confirmed",
                now,
                "shipper",
                "모든 화주 확인 완료",
                body.shipper_id,
                body.note,
            )
        else:
            order.updated_at = now
            order.log.append(
                OrderLogEntry(
                    at=now,
                    actor="shipper",
                    action="화주 수락",
                    status_after=order.status,
                    shipper_id=body.shipper_id,
                    note=body.note,
                )
            )
        return order

    def _confirm_forwarder(
        self, state: _State, order: ExecutionOrder, body: ConfirmRequest, now: datetime
    ) -> ExecutionOrder:
        if order.status in ("forwarder_confirmed", "executed") and body.accept:
            return order
        if order.status in TERMINAL_STATUSES:
            raise SessionConflict(f"이미 종료된 실행 지시입니다: {order.status}")
        if order.status != "shipper_confirmed":
            raise SessionConflict("화주 확인이 끝나지 않아 포워더가 확정할 수 없습니다")
        if body.accept:
            _require_execution_consent(state, order)
        if not body.accept:
            self._transition(
                order,
                "rejected",
                now,
                "forwarder",
                "포워더 거절 → 이전 확정 계획으로 복귀",
                note=body.note,
            )
            self._rollback(state, order, now)
            return order
        self._transition(
            order,
            "forwarder_confirmed",
            now,
            "forwarder",
            "포워더 확정 → 계획 확정, 자동 조치 실행, 제3자 요청 발송",
            note=body.note,
        )
        for action in order.automatic:
            action.done = True
        self._commit(state, order, now)
        self._finish_if_done(state, order, now)
        return order

    def _finish_if_done(self, state: _State, order: ExecutionOrder, now: datetime) -> None:
        if order.status != "forwarder_confirmed":
            return
        if all(r.status == "accepted" for r in order.requests):
            self._transition(order, "executed", now, "system", "모든 요청 회신 완료 → 실행 완료")
            if state.session.active_order_id == order.order_id:
                state.session = state.session.model_copy(update={"active_order_id": None})

    def answer_request(
        self,
        plan_id: str,
        order_id: str,
        request_id: str,
        body: RequestResultBody,
        org_id: str = DEMO_ORG,
    ) -> ExecutionOrder:
        with self._lock:
            self._expire(plan_id, org_id)
            return self._answer_request(plan_id, order_id, request_id, body, org_id)

    def _answer_request(
        self,
        plan_id: str,
        order_id: str,
        request_id: str,
        body: RequestResultBody,
        org_id: str,
    ) -> ExecutionOrder:
        with self._db.begin() as db:
            state = self._require(db, plan_id, org_id)
            order = state.orders[order_id]
            request = next((r for r in order.requests if r.request_id == request_id), None)
            if request is None:
                raise KeyError(request_id)
            decision = "accepted" if body.accepted else "declined"
            if request.status != "pending":
                if request.status == decision:
                    return order
                raise SessionConflict(f"이미 {request.status} 처리된 요청입니다")
            if order.status != "forwarder_confirmed":
                raise SessionConflict("포워더 확정 뒤에만 요청 회신을 기록할 수 있습니다")
            _require_execution_consent(state, order)
            now = self.clock()
            request.status = decision
            request.note = body.note
            request.answered_at = now
            order.updated_at = now
            order.log.append(
                OrderLogEntry(
                    at=now,
                    actor="forwarder",
                    action=(
                        f"{request.request_id} {request.counterparty} 수락"
                        if body.accepted
                        else f"{request.request_id} {request.counterparty} 거절 → 재계획 필요"
                    ),
                    status_after=order.status,
                    note=body.note,
                )
            )
            self._finish_if_done(state, order, now)
            self._save(db, state)
            row = _plan_row(db, plan_id, ANY_ORG)
            if not body.accepted and row.org_id != DEMO_ORG:
                _set_candidates(row, [*_candidates(row), _declined(order, request, now)])
            return order

    def confirmation_state(self, plan_id: str, org_id: str = DEMO_ORG) -> PlanConfirmationState:
        with self._lock:
            self._expire(plan_id, org_id)
            with self._db.begin() as db:
                row = _plan_row(db, plan_id, org_id)
                if row is None:
                    raise KeyError(plan_id)
                return _state_of(db, row, PlanSession.model_validate(row.payload))

    def confirmation_states(self, plan_ids: list[str]) -> dict[str, PlanConfirmationState]:
        if not plan_ids:
            return {}
        with self._db.begin() as db:
            rows = db.scalars(select(PlanRow).where(PlanRow.plan_id.in_(plan_ids)))
            return {
                row.plan_id: _state_of(db, row, PlanSession.model_validate(row.payload))
                for row in rows
            }

    def needing_attention(self, org_id: str, limit: int = 100) -> list[PlanConfirmationState]:
        open_round = (
            select(PlanConfirmationRow.plan_id)
            .where(
                PlanConfirmationRow.plan_id == PlanRow.plan_id,
                PlanConfirmationRow.round == PlanRow.confirmation_round,
                PlanConfirmationRow.status != "accepted",
            )
            .exists()
        )
        self.expire_due(PlanRow.org_id == org_id)
        with self._db.begin() as db:
            rows = db.scalars(
                select(PlanRow)
                .where(
                    PlanRow.org_id == org_id,
                    (PlanRow.status == "pending_shipper_confirmation")
                    | PlanRow.needs_replan
                    | open_round,
                )
                .order_by(PlanRow.created_at.desc(), PlanRow.seq.desc())
                .limit(limit)
            )
            return [_state_of(db, row, PlanSession.model_validate(row.payload)) for row in rows]

    def shipper_confirmations(
        self,
        shipper_org_id: str,
        status: ConfirmationStatus | None = None,
        plan_id: str | None = None,
    ) -> list[PlanConfirmation]:
        query = (
            select(PlanConfirmationRow)
            .join(PlanRow, PlanRow.plan_id == PlanConfirmationRow.plan_id)
            .where(
                PlanConfirmationRow.shipper_org_id == shipper_org_id,
                PlanConfirmationRow.round == PlanRow.confirmation_round,
            )
            .order_by(PlanConfirmationRow.created_at.desc(), PlanConfirmationRow.shipper_id)
        )
        if status is not None:
            query = query.where(PlanConfirmationRow.status == status)
        if plan_id is not None:
            query = query.where(PlanConfirmationRow.plan_id == plan_id)
        with self._db.begin() as db:
            return [_confirmation(row) for row in db.scalars(query)]

    def _settle(self, db: Session, row: PlanRow, state: _State, now: datetime) -> None:
        state.unconfirmed = _unconfirmed(db, row)
        session = state.session
        if (
            session.status == "pending_shipper_confirmation"
            and state.settled_status() == "committed"
        ):
            state.session = session.model_copy(update={"status": "committed", "updated_at": now})
            self._save(db, state)

    def decide_confirmation(
        self,
        plan_id: str,
        shipper_org_id: str,
        accept: bool,
        reason: str | None = None,
        shipper_id: str | None = None,
        user_id: str | None = None,
    ) -> PlanConfirmationState:
        with self._lock:
            self._expire(plan_id, ANY_ORG)
            with self._db.begin() as db:
                row = db.get(PlanRow, plan_id, with_for_update=True)
                if row is None or not row.confirmation_round:
                    raise KeyError(plan_id)
                mine = [
                    c
                    for c in _round_rows(db, plan_id, row.confirmation_round)
                    if c.shipper_org_id == shipper_org_id
                    and (shipper_id is None or c.shipper_id == shipper_id)
                ]
                if not mine:
                    raise KeyError(plan_id)
                target = next((c for c in mine if c.status == "pending"), mine[0])
                if target.status != "pending":
                    word = "수락" if target.status == "accepted" else "거절"
                    raise SessionConflict(f"이미 {word}한 계획입니다")
                state = self._require(db, plan_id, ANY_ORG)
                now = self.clock()
                target.status = "accepted" if accept else "rejected"
                target.reason = reason
                target.decided_by = user_id
                target.decided_at = now
                db.flush()
                if not accept:
                    candidate = _rejected(target, state.session.scenario, now)
                    if candidate.lot_ids:
                        _set_candidates(row, [*_candidates(row), candidate])
                self._settle(db, row, state, now)
                return _state_of(db, row, state.session)

    def accept_all(self, plan_id: str, org_id: str) -> PlanConfirmationState:
        with self._lock:
            self._expire(plan_id, org_id)
            with self._db.begin() as db:
                row = _plan_row(db, plan_id, org_id)
                if row is None:
                    raise KeyError(plan_id)
                db.refresh(row, with_for_update=True)
                state = self._require(db, plan_id, org_id)
                now = self.clock()
                if row.confirmation_round:
                    for c in _round_rows(db, plan_id, row.confirmation_round):
                        if c.status == "pending":
                            c.status = "accepted"
                            c.auto_accepted = True
                            c.decided_at = now
                    db.flush()
                self._settle(db, row, state, now)
                return _state_of(db, row, state.session)

    def recompute_without(
        self,
        plan_id: str,
        org_id: str,
        shipper_ids: list[str] | None = None,
        auto_accept: bool = False,
    ) -> tuple[PlanSession, bool]:
        with self._lock:
            self._expire(plan_id, org_id)
            with self._db.begin() as db:
                row = _plan_row(db, plan_id, org_id)
                if row is None:
                    raise KeyError(plan_id)
                loaded = self._require(db, plan_id, org_id)
                session = loaded.session
                if session.status != "pending_shipper_confirmation":
                    raise SessionConflict("화주 확인을 기다리는 계획만 화주를 빼고 다시 계산합니다")
                if _committed_orders(loaded):
                    raise SessionConflict(RECOMPUTE_AFTER_ORDER)
                listed = {
                    c.shipper_id: c.status for c in _round_rows(db, plan_id, row.confirmation_round)
                }
                booked = _booked(db, row.org_id, row.scenario_id, plan_id)
        excluded = set(shipper_ids or [s for s, status in listed.items() if status == "rejected"])
        if not excluded:
            raise OrderInputError("뺄 화주가 없습니다. 거절한 화주가 없으면 shipper_ids를 주세요")
        unknown = sorted(excluded - set(listed))
        if unknown:
            raise OrderInputError(f"이 계획의 확인 대상 화주가 아닙니다: {', '.join(unknown)}")
        dropped = [lot.id for lot in session.scenario.lots if lot.shipper_id in excluded]
        lots = [lot for lot in session.scenario.lots if lot.shipper_id not in excluded]
        if not lots:
            raise OrderInputError("화주를 빼면 남는 출하가 없습니다. 계획을 지우세요")
        working = session.scenario.model_copy(
            update={
                "lots": lots,
                "containers": [c for c in session.scenario.containers if c.id not in booked],
            },
            deep=True,
        )
        built = build_plan(working)
        with self._lock, self._db.begin() as db:
            _lock_bookings(db, org_id)
            row = _plan_row(db, plan_id, org_id)
            if row is None:
                raise KeyError(plan_id)
            state = self._require(db, plan_id, org_id)
            if state.session.version != session.version:
                raise SessionConflict(
                    "그사이 계획이 바뀌었습니다. 확인 현황을 새로 고친 뒤 다시 하세요"
                )
            if _committed_orders(state):
                raise SessionConflict(RECOMPUTE_AFTER_ORDER)
            clash = sea_containers(built.plan) & _booked(db, row.org_id, row.scenario_id, plan_id)
            if clash:
                raise ContainerBooked(sorted(clash))
            now = self.clock()
            gone = set(dropped)
            version = session.version + 1
            updated = state.session.model_copy(
                update={
                    "version": version,
                    "updated_at": now,
                    "scenario": working,
                    "plan": built.plan,
                    "routes": built.routes,
                    "air_locked_lot_ids": [i for i in session.air_locked_lot_ids if i not in gone],
                    "committed_version": version,
                    "active_order_id": None,
                    "arrival_inspections": [],
                }
            )
            state.committed = _snapshot(updated)
            updated.registered_lot_ids = _unregister(state, gone)
            state.session = updated
            owners, created_by = _stored_owners(row)
            missing = [i for i in updated.registered_lot_ids if i not in owners]
            owners = {**owners, **lot_owners(db, missing, created_by)}
            pending = _open_round(db, row, updated, owners, now, auto=auto_accept)
            state.unconfirmed = _unconfirmed(db, row)
            updated.status = state.settled_status()
            self._save(db, state)
            kept = [
                c
                for c in _candidates(row)
                if not (c.source == "shipper_rejected" and c.shipper_id in excluded)
            ]
            _set_candidates(row, kept)
            return updated.model_copy(deep=True), pending

    def record_arrival(
        self, plan_id: str, body: ArrivalRequest, org_id: str = DEMO_ORG
    ) -> ArrivalSettlementResult:
        return self.record_arrival_snapshot(plan_id, body, org_id)[0]

    def record_arrival_snapshot(
        self, plan_id: str, body: ArrivalRequest, org_id: str = DEMO_ORG
    ) -> tuple[ArrivalSettlementResult, PlanSession]:
        with self._lock:
            self._expire(plan_id, org_id)
            return self._record_arrival(plan_id, body, org_id)

    def _record_arrival(
        self, plan_id: str, body: ArrivalRequest, org_id: str
    ) -> tuple[ArrivalSettlementResult, PlanSession]:
        with self._db.begin() as db:
            state = self._require(db, plan_id, org_id)
            _require_execution_consent(state)
            session = state.session
            now = self.clock()
            result, inspections = arrival_settlement(
                plan_id, session.scenario, session.plan, session.arrival_inspections, body, now
            )
            committed = state.committed
            pending = session.status == "pending_confirmation"
            if pending and not _unchanged(
                committed.scenario, committed.plan, session.scenario, session.plan, body.lot_id
            ):
                raise InspectionPending(body.lot_id)
            updated = session.model_copy(
                update={
                    "arrival_inspections": inspections,
                    "updated_at": now,
                    "inspection_revision": session.inspection_revision + 1,
                }
            )
            state.session = updated
            if pending:
                record = next(i for i in inspections if i.lot_id == body.lot_id)
                kept = [i for i in committed.arrival_inspections if i.lot_id != body.lot_id]
                state.committed = committed.model_copy(
                    update={"arrival_inspections": [*kept, record]}
                )
            else:
                state.committed = _snapshot(updated)
            self._save(db, state)
            return result, updated.model_copy(deep=True)

    @staticmethod
    def _carry_inspections(session: PlanSession, scenario: Scenario, plan: Plan):
        return [
            inspection
            for inspection in current_inspections(plan, session.arrival_inspections)
            if _unchanged(session.scenario, session.plan, scenario, plan, inspection.lot_id)
        ]


def _unchanged(
    scenario_before: Scenario, plan_before: Plan, scenario: Scenario, plan: Plan, lot_id: str
) -> bool:
    before = next((a for a in plan_before.assignments if a.lot_id == lot_id), None)
    after = next((a for a in plan.assignments if a.lot_id == lot_id), None)
    lot_before = next((lot for lot in scenario_before.lots if lot.id == lot_id), None)
    lot_after = next((lot for lot in scenario.lots if lot.id == lot_id), None)
    if before is None or before != after or lot_before != lot_after:
        return False
    return planned_loss_rate(scenario_before, plan_before, lot_id) == planned_loss_rate(
        scenario, plan, lot_id
    )


def _plan_scope(org_id: str) -> Any:
    return true() if org_id == ANY_ORG else PlanRow.org_id == org_id


def _plan_row(db: Session, plan_id: str, org_id: str) -> PlanRow | None:
    row = db.get(PlanRow, plan_id)
    if row is None or (org_id != ANY_ORG and row.org_id != org_id):
        return None
    return row


def _cutoff(db: Session, table: type, where: Any, keep: int) -> int | None:
    query = select(table.seq).order_by(table.seq.desc()).offset(keep).limit(1)
    if where is not None:
        query = query.where(where)
    return db.scalar(query)


def _trim(db: Session, table: type, where: Any, keep: int) -> None:
    cutoff = _cutoff(db, table, where, keep)
    if cutoff is None:
        return
    condition = table.seq <= cutoff
    if where is not None:
        condition = condition & where
    db.execute(delete(table).where(condition))


store = SessionStore()
