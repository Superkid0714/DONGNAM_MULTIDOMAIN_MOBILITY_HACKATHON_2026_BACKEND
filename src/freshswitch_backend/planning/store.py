import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, true
from sqlalchemy.orm import Session

from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.execution.models import (
    TERMINAL_STATUSES,
    ConfirmRequest,
    ExecutionOrder,
    OrderLogEntry,
    OrderStatus,
    RequestResultBody,
)
from freshswitch_backend.execution.orders import build_order
from freshswitch_backend.execution.quality import (
    ArrivalInspection,
    ArrivalRequest,
    ArrivalSettlementResult,
    arrival_settlement,
    current_inspections,
    planned_loss_rate,
)
from freshswitch_backend.explain.models import Explanation
from freshswitch_backend.lots import mark_planned, release_lots
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.tables import (
    BindingRow,
    ContainerRow,
    IdempotencyRow,
    OrderRow,
    PendingRow,
    PlanRow,
)
from freshswitch_backend.planning.service import VoyagePickup, build_plan
from freshswitch_backend.replan import (
    Alternative,
    CancelEvent,
    Event,
    ReplanResult,
    diff_plans,
    locks_air,
    plan_score,
)
from freshswitch_backend.replan.models import AlternativeId, CancellationCharge

MAX_SESSIONS = 200
MAX_PENDING = 20
MAX_ORDERS = 50
MAX_IDEMPOTENCY_KEYS = 100

SessionStatus = Literal["committed", "pending_confirmation"]


class SessionConflict(Exception):
    pass


class OrderInputError(ValueError):
    pass


class IdempotencyMismatch(ValueError):
    pass


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


@dataclass
class _State:
    session: PlanSession
    committed: _Snapshot
    orders: OrderedDict[str, ExecutionOrder]


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


def _snapshot(session: PlanSession) -> _Snapshot:
    return _Snapshot(
        version=session.version,
        scenario=session.scenario,
        plan=session.plan,
        routes=session.routes,
        air_locked_lot_ids=session.air_locked_lot_ids,
        arrival_inspections=session.arrival_inspections,
    )


def _cumulative(
    committed: _Snapshot,
    scenario_after: Scenario,
    alternative: Alternative,
    superseded: list[ExecutionOrder],
) -> Alternative:
    diff = diff_plans(scenario_after, committed.plan, alternative.plan)
    charges: list[CancellationCharge] = [
        o.cancellation for o in superseded if o.cancellation and o.cancellation.fee
    ]
    if alternative.diff.cancellation and alternative.diff.cancellation.fee:
        charges.append(alternative.diff.cancellation)
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


class SessionStore:
    def __init__(self, max_sessions: int = MAX_SESSIONS, db: Database | None = None) -> None:
        self._db = db or database
        self._lock = threading.RLock()
        self._max = max_sessions
        self.clock: Callable[[], datetime] = _now

    def _load(self, db: Session, plan_id: str) -> _State | None:
        row = db.get(PlanRow, plan_id)
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
        )

    def _save(self, db: Session, state: _State) -> None:
        session = state.session
        row = db.get(PlanRow, session.plan_id)
        if row is None:
            return
        row.version = session.version
        row.status = session.status
        row.updated_at = session.updated_at
        row.lot_count = len(session.scenario.lots)
        row.totals = _totals(session.plan)
        row.payload = session.model_dump(mode="json")
        row.committed = state.committed.model_dump(mode="json")
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

    def _has_due(self, db: Session, plan_id: str) -> bool:
        now = self.clock()
        rows = db.scalars(
            select(OrderRow.payload).where(
                OrderRow.plan_id == plan_id,
                OrderRow.status.in_(("proposed", "shipper_confirmed")),
            )
        )
        for payload in rows:
            order = ExecutionOrder.model_validate(payload)
            deadline = (
                order.deadlines.shipper if order.status == "proposed" else order.deadlines.forwarder
            )
            if now > deadline:
                return True
        return False

    def _expire(self, plan_id: str) -> None:
        with self._db.begin() as db:
            if not self._has_due(db, plan_id):
                return
            state = self._load(db, plan_id)
            if state is not None and self._expire_due(state):
                self._save(db, state)

    def _require(self, db: Session, plan_id: str) -> _State:
        state = self._load(db, plan_id)
        if state is None:
            raise KeyError(plan_id)
        return state

    def create(
        self, scenario: Scenario, registered_lot_ids: list[str] | None = None
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
        with self._lock, self._db.begin() as db:
            db.add(
                PlanRow(
                    plan_id=session.plan_id,
                    scenario_id=session.scenario_id,
                    version=session.version,
                    status=session.status,
                    lot_count=len(working.lots),
                    totals=_totals(session.plan),
                    created_at=now,
                    updated_at=now,
                    payload=session.model_dump(mode="json"),
                    committed=_snapshot(session).model_dump(mode="json"),
                )
            )
            db.flush()
            mark_planned(db, session.registered_lot_ids, now)
            cutoff = _cutoff(db, PlanRow, None, self._max)
            if cutoff is not None:
                self._drop(db, PlanRow.seq <= cutoff)
        return session

    def get(self, plan_id: str) -> PlanSession | None:
        with self._lock:
            self._expire(plan_id)
            with self._db.begin() as db:
                row = db.get(PlanRow, plan_id)
                return PlanSession.model_validate(row.payload) if row is not None else None

    def summaries(self, limit: int = 100, offset: int = 0) -> list[PlanSummary]:
        with self._db.begin() as db:
            rows = db.scalars(
                select(PlanRow)
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
        release_lots(db, {lot_id for row in rows for lot_id in row[1] or []}, self.clock())
        return len(plan_ids)

    def delete(self, plan_id: str) -> bool:
        with self._lock, self._db.begin() as db:
            return self._drop(db, PlanRow.plan_id == plan_id) > 0

    def count(self) -> int:
        with self._db.begin() as db:
            return db.scalar(select(func.count()).select_from(PlanRow)) or 0

    def clear(self) -> int:
        with self._lock, self._db.begin() as db:
            count = self._drop(db, true())
            release_lots(db, None, self.clock())
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
    ) -> Any | None:
        with self._lock, self._db.begin() as db:
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
        self, plan_id: str, key: str, kind: str, fingerprint: str, value: BaseModel
    ) -> Any:
        with self._lock, self._db.begin() as db:
            if db.get(PlanRow, plan_id) is None:
                return value
            return self._remember(db, plan_id, key, kind, fingerprint, value)

    def add_pending(self, session: PlanSession, result: ReplanResult) -> PendingReplan:
        pending = PendingReplan(
            replan_id=str(uuid.uuid4()), base_version=session.version, result=result
        )
        with self._lock, self._db.begin() as db:
            if db.get(PlanRow, session.plan_id) is None:
                return pending
            db.add(
                PendingRow(
                    replan_id=pending.replan_id,
                    plan_id=session.plan_id,
                    base_version=pending.base_version,
                    payload=_dump_pending(pending),
                )
            )
            db.flush()
            _trim(db, PendingRow, PendingRow.plan_id == session.plan_id, MAX_PENDING)
        return pending

    def _pending(self, db: Session, plan_id: str, replan_id: str | None) -> PendingReplan:
        query = select(PendingRow).where(PendingRow.plan_id == plan_id)
        if replan_id is None:
            row = db.scalars(query.order_by(PendingRow.seq.desc()).limit(1)).first()
            if row is None:
                raise SessionConflict("적용할 재최적화 결과가 없습니다. 먼저 이벤트를 보내세요")
        else:
            row = db.scalars(query.where(PendingRow.replan_id == replan_id)).first()
            if row is None:
                raise KeyError(replan_id)
        return _load_pending(row.payload)

    def apply(
        self,
        plan_id: str,
        alternative_id: str,
        replan_id: str | None = None,
        idempotency: tuple[str, str] | None = None,
    ) -> PlanSession:
        with self._lock:
            if idempotency is not None:
                replayed = self.idempotent(
                    plan_id, idempotency[0], "apply", idempotency[1], PlanSession
                )
                if replayed is not None:
                    return replayed
            self._expire(plan_id)
            with self._db.begin() as db:
                return self._apply(db, plan_id, alternative_id, replan_id, idempotency)

    def _apply(
        self,
        db: Session,
        plan_id: str,
        alternative_id: str,
        replan_id: str | None,
        idempotency: tuple[str, str] | None,
    ) -> PlanSession:
        state = self._require(db, plan_id)
        session = state.session
        pending = self._pending(db, plan_id, replan_id)
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
        event = pending.result.event
        scenario = pending.result.scenario_after
        locked = set(session.air_locked_lot_ids)
        if locks_air(scenario, event):
            locked.add(event.lot_id)
        if isinstance(event, CancelEvent):
            locked.discard(event.lot_id)
        now = self.clock()
        superseded = [
            o for o in state.orders.values() if o.status in ("proposed", "shipper_confirmed")
        ]
        for previous in superseded:
            self._transition(previous, "expired", now, "system", "새 대안 적용으로 대체됨")
            previous.expired_reason = "superseded"
        committed = state.committed
        before = session
        ordered = alternative
        if committed.version != session.version:
            before = committed
            ordered = _cumulative(committed, scenario, alternative, superseded)
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
            }
        )
        state.session = updated
        state.orders[order.order_id] = order
        self._save(db, state)
        if idempotency is not None:
            self._remember(db, plan_id, idempotency[0], "apply", idempotency[1], updated)
        return updated

    def orders(self, plan_id: str) -> list[ExecutionOrder]:
        with self._lock:
            self._expire(plan_id)
            with self._db.begin() as db:
                rows = db.scalars(
                    select(OrderRow.payload)
                    .where(OrderRow.plan_id == plan_id)
                    .order_by(OrderRow.seq)
                )
                return [ExecutionOrder.model_validate(payload) for payload in rows]

    def order(self, plan_id: str, order_id: str) -> ExecutionOrder:
        with self._lock:
            self._expire(plan_id)
        with self._db.begin() as db:
            return self._require(db, plan_id).orders[order_id]

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
            }
        )
        state.committed = snapshot.model_copy(update={"version": version})
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
        state.committed = _snapshot(updated)

    def confirm(self, plan_id: str, order_id: str, body: ConfirmRequest) -> ExecutionOrder:
        with self._lock:
            self._expire(plan_id)
            return self._confirm(plan_id, order_id, body)

    def _confirm(self, plan_id: str, order_id: str, body: ConfirmRequest) -> ExecutionOrder:
        with self._db.begin() as db:
            state = self._require(db, plan_id)
            order = state.orders[order_id]
            now = self.clock()
            if body.actor == "shipper":
                result = self._confirm_shipper(state, order, body, now)
            else:
                result = self._confirm_forwarder(state, order, body, now)
            self._save(db, state)
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
        self, plan_id: str, order_id: str, request_id: str, body: RequestResultBody
    ) -> ExecutionOrder:
        with self._lock:
            self._expire(plan_id)
            return self._answer_request(plan_id, order_id, request_id, body)

    def _answer_request(
        self, plan_id: str, order_id: str, request_id: str, body: RequestResultBody
    ) -> ExecutionOrder:
        with self._db.begin() as db:
            state = self._require(db, plan_id)
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
            return order

    def record_arrival(self, plan_id: str, body: ArrivalRequest) -> ArrivalSettlementResult:
        return self.record_arrival_snapshot(plan_id, body)[0]

    def record_arrival_snapshot(
        self, plan_id: str, body: ArrivalRequest
    ) -> tuple[ArrivalSettlementResult, PlanSession]:
        with self._lock:
            self._expire(plan_id)
            return self._record_arrival(plan_id, body)

    def _record_arrival(
        self, plan_id: str, body: ArrivalRequest
    ) -> tuple[ArrivalSettlementResult, PlanSession]:
        with self._db.begin() as db:
            state = self._require(db, plan_id)
            session = state.session
            now = self.clock()
            result, inspections = arrival_settlement(
                plan_id, session.scenario, session.plan, session.arrival_inspections, body, now
            )
            updated = session.model_copy(
                update={
                    "arrival_inspections": inspections,
                    "updated_at": now,
                    "inspection_revision": session.inspection_revision + 1,
                }
            )
            state.session = updated
            if updated.status == "committed":
                state.committed = _snapshot(updated)
            self._save(db, state)
            return result, updated.model_copy(deep=True)

    @staticmethod
    def _carry_inspections(session: PlanSession, scenario: Scenario, plan: Plan):
        before = {a.lot_id: a for a in session.plan.assignments}
        after = {a.lot_id: a for a in plan.assignments}
        lots_before = {lot.id: lot for lot in session.scenario.lots}
        lots_after = {lot.id: lot for lot in scenario.lots}
        kept = []
        for inspection in current_inspections(plan, session.arrival_inspections):
            lid = inspection.lot_id
            if before.get(lid) != after.get(lid) or lots_before.get(lid) != lots_after.get(lid):
                continue
            if planned_loss_rate(session.scenario, session.plan, lid) != planned_loss_rate(
                scenario, plan, lid
            ):
                continue
            kept.append(inspection)
        return kept


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
