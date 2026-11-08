import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import TypeAdapter
from sqlalchemy import delete, func, select, text, tuple_
from sqlalchemy.orm import Session

from freshswitch_backend.coldchain.models import (
    Alert,
    AlertFeed,
    Binding,
    BindingRequest,
    IngestRejected,
    IngestResult,
    Reading,
    ReadingIn,
    SettlementPreview,
    SettlementPreviewLot,
    SimulationRequest,
    SimulationRun,
    Stage,
    TimelinePoint,
    TwinState,
    TwinSummary,
)
from freshswitch_backend.coldchain.params import ColdchainParams
from freshswitch_backend.coldchain.simulator import PRESETS, preset_readings
from freshswitch_backend.coldchain.twin import (
    Carry,
    Evaluation,
    TwinContext,
    build_context,
    downsample,
    evaluate,
    loss_summary,
)
from freshswitch_backend.execution.quality import (
    ArrivalRequest,
    QualityError,
    arrival_settlement,
)
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.tables import (
    ALERT_SEQ,
    AlertRow,
    BindingRow,
    ContainerRow,
    ReadingRow,
    RunRow,
)
from freshswitch_backend.planning.store import PlanSession
from freshswitch_backend.planning.store import store as plan_store

logger = logging.getLogger(__name__)

MAX_READINGS = 5000
MAX_HISTORY = 1000
MAX_CONTAINERS = 200
MAX_RUNS = 200
AMBIGUOUS = "동일 컨테이너의 계획이 여러 개입니다. plan_id를 지정하세요"
DEFAULT_STAGE: Stage = "terminal"
PREVIEW_RULE = (
    "트윈 예상 손실률을 도착 검수 손실률 대신 넣어 품질 사후정산을 미리 계산한다. "
    "저장하지 않는다. 실제 환급은 도착 검수(POST /settlement/arrival)로 확정한다"
)


class ColdchainError(ValueError):
    pass


class NotFound(LookupError):
    pass


@dataclass
class ContainerRecord:
    container_id: str
    plan_id: str | None = None
    stage: Stage = DEFAULT_STAGE
    readings: dict[tuple[str, datetime], Reading] = field(default_factory=dict)
    alerts: OrderedDict[str, Alert] = field(default_factory=OrderedDict)
    carry: Carry | None = None
    history: list[TimelinePoint] = field(default_factory=list)
    anchors: set[datetime] = field(default_factory=set)
    stored_readings: set[tuple[str, datetime]] = field(default_factory=set)
    stored_alerts: set[str] = field(default_factory=set)
    stored: bool = False

    @property
    def key(self) -> str:
        return _record_key(self.plan_id, self.container_id)

    def sorted_readings(self) -> list[Reading]:
        return sorted(self.readings.values(), key=lambda r: (r.ts, r.device_id))


@dataclass
class _Run:
    info: SimulationRun
    readings: list[ReadingIn]
    stop: threading.Event
    thread: threading.Thread | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _record_key(plan_id: str | None, container_id: str) -> str:
    return f"{plan_id or '-'}:{container_id}"


_CARRY = TypeAdapter(Carry)
_HISTORY = TypeAdapter(list[TimelinePoint])
_ANCHORS = TypeAdapter(list[datetime])


class ColdchainStore:
    def __init__(self, params: ColdchainParams | None = None, db: Database | None = None) -> None:
        self.params = params or ColdchainParams()
        self._db = db or database
        self._runs: dict[str, _Run] = {}
        self._lock = threading.RLock()
        self.clock: Callable[[], datetime] = _now

    def stop_all(self) -> None:
        with self._lock:
            runs = list(self._runs.values())
            for run in runs:
                run.stop.set()
            self._runs.clear()
        for run in runs:
            if run.thread is not None and run.thread is not threading.current_thread():
                run.thread.join(timeout=5)

    def clear(self) -> int:
        self.stop_all()
        with self._lock:
            with self._db.begin() as db:
                count = db.scalar(select(func.count()).select_from(ContainerRow)) or 0
                db.execute(delete(ContainerRow))
                db.execute(delete(BindingRow))
                db.execute(delete(RunRow))
            return count

    def finish_orphan_runs(self) -> int:
        with self._lock, self._db.begin() as db:
            rows = db.scalars(select(RunRow).where(RunRow.done.is_(False))).all()
            count = 0
            for row in rows:
                if row.run_id in self._runs:
                    continue
                row.done = True
                row.payload = {**row.payload, "done": True}
                count += 1
            return count

    def _session(self, plan_id: str | None) -> PlanSession | None:
        return plan_store.get(plan_id) if plan_id else None

    def require_plan(self, plan_id: str | None, container_id: str | None = None) -> None:
        if plan_id is None:
            return
        session = plan_store.get(plan_id)
        if session is None:
            raise NotFound("계획을 찾을 수 없습니다")
        if container_id is not None and container_id not in {
            c.id for c in session.scenario.containers
        }:
            raise ColdchainError(f"계획에 없는 컨테이너입니다: {container_id}")

    def _matches(
        self, db: Session, container_id: str, plan_id: str | None = None
    ) -> list[ContainerRow]:
        query = select(ContainerRow).where(ContainerRow.container_id == container_id)
        if plan_id is not None:
            query = query.where(ContainerRow.plan_id == plan_id)
        return list(db.scalars(query.order_by(ContainerRow.seq)))

    def _resolve_plan(self, db: Session, container_id: str, plan_id: str | None) -> str | None:
        if plan_id is not None:
            return plan_id
        matches = self._matches(db, container_id)
        if len(matches) > 1:
            raise ColdchainError(AMBIGUOUS)
        return matches[0].plan_id if matches else None

    def _find(
        self, db: Session, container_id: str, plan_id: str | None = None
    ) -> ContainerRecord | None:
        matches = self._matches(db, container_id, plan_id)
        if len(matches) > 1:
            raise ColdchainError(AMBIGUOUS)
        return self._load(db, matches[0]) if matches else None

    def _load(self, db: Session, row: ContainerRow) -> ContainerRecord:
        key = row.record_key
        readings = db.scalars(select(ReadingRow).where(ReadingRow.record_key == key))
        alerts = db.scalars(
            select(AlertRow).where(AlertRow.record_key == key).order_by(AlertRow.seq)
        )
        record = ContainerRecord(
            container_id=row.container_id,
            plan_id=row.plan_id,
            stage=row.stage,
            carry=_CARRY.validate_python(row.carry) if row.carry is not None else None,
            history=_HISTORY.validate_python(row.history),
            anchors=set(_ANCHORS.validate_python(row.anchors)),
            stored=True,
        )
        for item in readings:
            reading = Reading.model_validate(item.payload)
            record.readings[(reading.device_id, reading.ts)] = reading
        for item in alerts:
            alert = Alert.model_validate(item.payload)
            record.alerts[alert.key] = alert
        record.stored_readings = set(record.readings)
        record.stored_alerts = set(record.alerts)
        return record

    def _save(self, db: Session, record: ContainerRecord) -> None:
        carry = _CARRY.dump_python(record.carry, mode="json") if record.carry else None
        history = _HISTORY.dump_python(record.history, mode="json")
        anchors = _ANCHORS.dump_python(sorted(record.anchors), mode="json")
        if not record.stored:
            db.add(
                ContainerRow(
                    record_key=record.key,
                    container_id=record.container_id,
                    plan_id=record.plan_id,
                    stage=record.stage,
                    carry=carry,
                    history=history,
                    anchors=anchors,
                )
            )
            db.flush()
            record.stored = True
        else:
            row = db.get(ContainerRow, record.key)
            if row is None:
                return
            row.stage = record.stage
            row.carry = carry
            row.history = history
            row.anchors = anchors
        removed = list(record.stored_readings - set(record.readings))
        for start in range(0, len(removed), 1000):
            db.execute(
                delete(ReadingRow).where(
                    ReadingRow.record_key == record.key,
                    tuple_(ReadingRow.device_id, ReadingRow.ts).in_(removed[start : start + 1000]),
                )
            )
        for key in set(record.readings) - record.stored_readings:
            reading = record.readings[key]
            db.add(
                ReadingRow(
                    record_key=record.key,
                    device_id=reading.device_id,
                    ts=reading.ts,
                    payload=reading.model_dump(mode="json"),
                )
            )
        for key in [k for k in record.alerts if k not in record.stored_alerts]:
            alert = record.alerts[key]
            db.add(
                AlertRow(
                    seq=alert.seq,
                    record_key=record.key,
                    container_id=record.container_id,
                    plan_id=record.plan_id,
                    key=key,
                    created_at=alert.created_at,
                    payload=alert.model_dump(mode="json"),
                )
            )
        db.flush()
        record.stored_readings = set(record.readings)
        record.stored_alerts = set(record.alerts)

    def _record(
        self,
        db: Session,
        records: dict[str, ContainerRecord],
        container_id: str,
        plan_id: str | None,
    ) -> ContainerRecord:
        key = _record_key(plan_id, container_id)
        record = records.get(key)
        if record is None:
            row = db.get(ContainerRow, key)
            record = (
                self._load(db, row)
                if row is not None
                else ContainerRecord(container_id=container_id, plan_id=plan_id)
            )
            records[key] = record
        return record

    def _trim(self, db: Session) -> None:
        cutoff = db.scalar(
            select(ContainerRow.seq)
            .order_by(ContainerRow.seq.desc())
            .offset(MAX_CONTAINERS)
            .limit(1)
        )
        if cutoff is not None:
            db.execute(delete(ContainerRow).where(ContainerRow.seq <= cutoff))

    def bind(self, body: BindingRequest) -> Binding:
        with self._lock, self._db.begin() as db:
            plan_id = self._resolve_plan(db, body.container_id, body.plan_id)
            self.require_plan(plan_id, body.container_id)
            binding = Binding(
                device_id=body.device_id,
                container_id=body.container_id,
                plan_id=plan_id,
                stage=body.stage,
                bound_at=self.clock(),
            )
            row = db.get(BindingRow, body.device_id)
            payload = binding.model_dump(mode="json")
            if row is None:
                db.add(
                    BindingRow(
                        device_id=body.device_id,
                        container_id=body.container_id,
                        plan_id=plan_id,
                        payload=payload,
                    )
                )
            else:
                row.container_id = body.container_id
                row.plan_id = plan_id
                row.payload = payload
            record = self._record(db, {}, body.container_id, plan_id)
            if body.stage:
                record.stage = body.stage
            self._save(db, record)
            self._trim(db)
            return binding

    def bindings(self) -> list[Binding]:
        with self._db.begin() as db:
            rows = db.scalars(select(BindingRow).order_by(BindingRow.device_id))
            return [Binding.model_validate(row.payload) for row in rows]

    def _binding(self, db: Session, device_id: str) -> Binding | None:
        row = db.get(BindingRow, device_id)
        return Binding.model_validate(row.payload) if row is not None else None

    def _context(self, record: ContainerRecord, plan_id: str | None = None) -> TwinContext:
        plan_id = plan_id or record.plan_id
        session = self._session(plan_id)
        return build_context(
            record.container_id,
            record.stage,
            self.params,
            plan_id=plan_id if session else None,
            scenario=session.scenario if session else None,
            plan=session.plan if session else None,
        )

    def _evaluate(
        self, record: ContainerRecord, plan_id: str | None = None
    ) -> tuple[TwinContext, Evaluation]:
        ctx = self._context(record, plan_id)
        return ctx, evaluate(
            record.sorted_readings(), ctx, self.params, record.carry, record.anchors
        )

    def _compact(self, db: Session, record: ContainerRecord) -> list[Alert]:
        excess = len(record.readings) - MAX_READINGS
        if excess <= 0:
            return []
        old = record.sorted_readings()[:excess]
        ev = evaluate(old, self._context(record), self.params, record.carry, record.anchors)
        new = self._merge(db, record, ev)
        record.carry = ev.carry
        record.history = downsample([*record.history, *ev.timeline], MAX_HISTORY)
        for reading in old:
            del record.readings[(reading.device_id, reading.ts)]
        return new

    def _merge(self, db: Session, record: ContainerRecord, evaluation: Evaluation) -> list[Alert]:
        record.anchors.update(evaluation.anchors)
        new: list[Alert] = []
        for alert in evaluation.alerts:
            if alert.key in record.alerts:
                continue
            seq = db.scalar(ALERT_SEQ.next_value())
            now = self.clock()
            action = alert.action.model_copy(update={"created_at": now}) if alert.action else None
            stored = alert.model_copy(
                update={
                    "seq": seq,
                    "created_at": now,
                    "action": action,
                    "plan_id": record.plan_id,
                }
            )
            record.alerts[alert.key] = stored
            new.append(stored)
        return new

    def _summary(self, record: ContainerRecord, ctx: TwinContext, ev: Evaluation) -> TwinSummary:
        loss = loss_summary(ctx, ev.exposure, self.params)
        current = ev.current
        return TwinSummary(
            container_id=record.container_id,
            plan_id=ctx.plan_id,
            status=ev.status,
            reading_count=len(record.readings),
            last_ts=current.ts if current else None,
            temp_c=current.temp_c if current else None,
            power_on=current.power_on if current else None,
            cumulative_outage_minutes=ev.outage.cumulative_minutes,
            sensor_loss_rate=loss.sensor_loss_rate,
            predicted_loss_rate=loss.predicted_loss_rate,
            delta_krw=loss.delta_krw,
            open_actions=sum(1 for a in record.alerts.values() if a.action is not None),
        )

    def ingest(self, items: list[ReadingIn]) -> IngestResult:
        with self._lock, self._db.begin() as db:
            return self._ingest(db, items)

    def _ingest(self, db: Session, items: list[ReadingIn]) -> IngestResult:
        accepted = 0
        duplicates = 0
        rejected: list[IngestRejected] = []
        touched: list[str] = []
        new_alerts: list[Alert] = []
        records: dict[str, ContainerRecord] = {}
        bindings: dict[str, Binding | None] = {}
        checked: dict[tuple[str | None, str], tuple[str | None, str | None]] = {}
        live: dict[str, bool] = {}
        for index, item in enumerate(items):
            if item.device_id not in bindings:
                bindings[item.device_id] = self._binding(db, item.device_id)
            binding = bindings[item.device_id]
            container_id = item.container_id or (binding.container_id if binding else None)
            if container_id is None:
                rejected.append(
                    IngestRejected(
                        index=index,
                        device_id=item.device_id,
                        reason="container_id가 없고 장치 연결도 없습니다",
                    )
                )
                continue
            plan_id = item.plan_id
            if plan_id is None and binding and binding.container_id == container_id:
                plan_id = binding.plan_id
                if plan_id is not None:
                    if plan_id not in live:
                        live[plan_id] = plan_store.get(plan_id) is not None
                    if not live[plan_id]:
                        plan_id = None
            check = (plan_id, container_id)
            if check not in checked:
                try:
                    resolved = self._resolve_plan(db, container_id, plan_id)
                    self.require_plan(resolved, container_id)
                    checked[check] = (resolved, None)
                except (NotFound, ColdchainError) as exc:
                    checked[check] = (None, str(exc))
            plan_id, reason = checked[check]
            if reason is not None:
                rejected.append(
                    IngestRejected(index=index, device_id=item.device_id, reason=reason)
                )
                continue
            record = self._record(db, records, container_id, plan_id)
            stage = item.stage or (binding.stage if binding else None)
            if stage:
                record.stage = stage
            reading = Reading(
                device_id=item.device_id,
                container_id=container_id,
                ts=item.ts or self.clock(),
                temp_c=item.temp_c,
                power_on=item.power_on,
                humidity_pct=item.humidity_pct,
                door_open=item.door_open,
            )
            key = (reading.device_id, reading.ts)
            if key in record.readings:
                duplicates += 1
                continue
            compacted = record.carry.prev if record.carry else None
            if compacted is not None and reading.ts <= compacted.ts:
                rejected.append(
                    IngestRejected(
                        index=index,
                        device_id=item.device_id,
                        reason=f"이미 요약된 구간({compacted.ts.isoformat()}) 이전 측정값입니다",
                    )
                )
                continue
            record.readings[key] = reading
            new_alerts.extend(self._compact(db, record))
            accepted += 1
            if record.key not in touched:
                touched.append(record.key)
        summaries: list[TwinSummary] = []
        for record_key in touched:
            record = records[record_key]
            ctx, ev = self._evaluate(record)
            new_alerts.extend(self._merge(db, record, ev))
            summaries.append(self._summary(record, ctx, ev))
        for record in records.values():
            self._save(db, record)
        self._trim(db)
        return IngestResult(
            accepted=accepted,
            duplicates=duplicates,
            rejected=rejected,
            containers=summaries,
            new_alerts=new_alerts,
        )

    def get_record(self, container_id: str, plan_id: str | None = None) -> ContainerRecord:
        with self._db.begin() as db:
            record = self._find(db, container_id, plan_id)
        if record is None:
            raise NotFound(f"센서 기록이 없는 컨테이너입니다: {container_id}")
        return record

    def state(self, container_id: str, plan_id: str | None = None, points: int = 720) -> TwinState:
        self.require_plan(plan_id, container_id)
        with self._lock:
            with self._db.begin() as db:
                record = self._find(db, container_id, plan_id)
            has_data = record is not None
            record = record or ContainerRecord(container_id=container_id, plan_id=plan_id)
            ctx, ev = self._evaluate(record, plan_id)
            alerts = list(record.alerts.values())
            return TwinState(
                container_id=container_id,
                plan_id=ctx.plan_id,
                scenario_id=ctx.scenario_id,
                voyage_id=ctx.voyage.id if ctx.voyage else None,
                stage=record.stage,
                device_ids=sorted({r.device_id for r in record.readings.values()}),
                products=ctx.products,
                status=ev.status,
                current=ev.current,
                outage=ev.outage,
                excursion=ev.excursion,
                loss=loss_summary(ctx, ev.exposure, self.params),
                reading_count=len(record.readings),
                timeline=downsample([*record.history, *ev.timeline], points),
                actions=[a.action for a in alerts if a.action is not None],
                alerts=alerts,
                params=dict(self.params),
                has_data=has_data,
            )

    def summaries(self, plan_id: str | None = None) -> list[TwinSummary]:
        with self._lock:
            with self._db.begin() as db:
                query = select(ContainerRow).order_by(ContainerRow.seq)
                if plan_id is not None:
                    query = query.where(ContainerRow.plan_id == plan_id)
                records = [self._load(db, row) for row in db.scalars(query).all()]
            out = []
            for record in records:
                ctx, ev = self._evaluate(record)
                out.append(self._summary(record, ctx, ev))
            return out

    def reset_container(self, container_id: str, plan_id: str | None = None) -> None:
        with self._lock:
            with self._db.begin() as db:
                matches = self._matches(db, container_id, plan_id)
                if len(matches) > 1:
                    raise ColdchainError(AMBIGUOUS)
                resolved = matches[0].plan_id if matches else plan_id
                db.execute(
                    delete(ContainerRow).where(
                        ContainerRow.record_key == _record_key(resolved, container_id)
                    )
                )
            for run in self._runs.values():
                if run.info.container_id == container_id and run.info.plan_id == resolved:
                    run.stop.set()

    def _last_seq(self, db: Session) -> int:
        row = db.execute(text(f"SELECT last_value, is_called FROM {ALERT_SEQ.name}")).one()
        return row.last_value if row.is_called else 0

    def alerts(
        self,
        since: int = 0,
        container_id: str | None = None,
        limit: int = 100,
        plan_id: str | None = None,
    ) -> AlertFeed:
        with self._db.begin() as db:
            query = select(AlertRow).where(AlertRow.seq > since).order_by(AlertRow.seq)
            if container_id is not None:
                query = query.where(AlertRow.container_id == container_id)
            if plan_id is not None:
                query = query.where(AlertRow.plan_id == plan_id)
            items = [Alert.model_validate(r.payload) for r in db.scalars(query.limit(limit))]
            next_since = items[-1].seq if items else max(since, 0)
            if not items:
                last = self._last_seq(db)
                if since > last:
                    next_since = last
            return AlertFeed(alerts=items, next_since=next_since)

    def settlement_preview(
        self, container_id: str, plan_id: str | None = None
    ) -> SettlementPreview:
        self.require_plan(plan_id, container_id)
        with self._lock:
            record = self.get_record(container_id, plan_id)
            plan_id = plan_id or record.plan_id
            session = self._session(plan_id)
            if session is None or plan_id is None:
                raise ColdchainError("계획과 연결되지 않은 컨테이너입니다. plan_id를 지정하세요")
            ctx, ev = self._evaluate(record, plan_id)
            loss = loss_summary(ctx, ev.exposure, self.params)
        inspections = list(session.arrival_inspections)
        lots: list[SettlementPreviewLot] = []
        for item in loss.lots:
            try:
                result, inspections = arrival_settlement(
                    plan_id,
                    session.scenario,
                    session.plan,
                    inspections,
                    ArrivalRequest(
                        lot_id=item.lot_id,
                        inspected_loss_pct=min(100.0, item.predicted_loss_rate * 100),
                        note="손실 트윈 예상치",
                    ),
                    self.clock(),
                )
            except QualityError:
                continue
            lots.append(
                SettlementPreviewLot(
                    lot_id=item.lot_id,
                    shipper_id=item.shipper_id,
                    planned_loss_rate=result.planned_loss_rate,
                    predicted_loss_rate=result.inspected_loss_rate,
                    excess_loss_value=result.excess_loss_value,
                    refund_before=result.refund_before,
                    refund_after=result.refund_after,
                    refund_change=result.refund_change,
                    capped=result.capped,
                )
            )
        return SettlementPreview(
            plan_id=plan_id,
            container_id=container_id,
            lots=lots,
            total_refund_change=sum(x.refund_change for x in lots),
            rule=PREVIEW_RULE,
            note="도착 검수 전 예상치. 계획 손실 가정을 넘는 손실액만큼 성과보수에서 뺀다(하한 0)",
        )

    def _make_run_room(self, db: Session) -> None:
        total = db.scalar(select(func.count()).select_from(RunRow)) or 0
        excess = total - MAX_RUNS + 1
        if excess <= 0:
            return
        finished = db.scalars(
            select(RunRow.run_id).where(RunRow.done.is_(True)).order_by(RunRow.seq).limit(excess)
        ).all()
        if len(finished) < excess:
            raise ColdchainError("동시에 실행할 수 있는 시뮬레이션 수를 초과했습니다")
        db.execute(delete(RunRow).where(RunRow.run_id.in_(finished)))

    def _persist_run(self, info: SimulationRun) -> None:
        with self._db.begin() as db:
            row = db.get(RunRow, info.run_id)
            payload = info.model_dump(mode="json")
            if row is None:
                db.add(
                    RunRow(
                        run_id=info.run_id,
                        container_id=info.container_id,
                        done=info.done,
                        payload=payload,
                    )
                )
            else:
                row.done = info.done
                row.payload = payload

    def simulate(self, body: SimulationRequest) -> SimulationRun:
        preset = PRESETS[body.preset]
        with self._lock:
            with self._db.begin() as db:
                self._make_run_room(db)
                plan_id = self._resolve_plan(db, body.container_id, body.plan_id)
                self.require_plan(plan_id, body.container_id)
            if body.reset:
                self.reset_container(body.container_id, plan_id)
            with self._db.begin() as db:
                record = self._record(db, {}, body.container_id, plan_id)
                record.stage = body.stage
                ctx = self._context(record)
                self._save(db, record)
                self._trim(db)
        readings = preset_readings(
            preset,
            device_id=body.device_id,
            container_id=body.container_id,
            plan_id=plan_id,
            band_min_c=ctx.band_min_c,
            band_max_c=ctx.band_max_c,
            start=body.start,
        )
        info = SimulationRun(
            run_id=uuid.uuid4().hex[:12],
            preset=body.preset,
            container_id=body.container_id,
            plan_id=plan_id,
            device_id=body.device_id,
            mode="stream" if body.interval_seconds > 0 else "instant",
            total=len(readings),
            sent=0,
            interval_seconds=body.interval_seconds,
            done=False,
            start=readings[0].ts,
            end=readings[-1].ts,
        )
        if body.interval_seconds <= 0:
            with self._lock:
                with self._db.begin() as db:
                    self._make_run_room(db)
                result = self.ingest(readings)
                info.sent = len(readings)
                info.done = True
                info.state = result.containers[0] if result.containers else None
                self._persist_run(info)
            return info
        run = _Run(info=info, readings=readings, stop=threading.Event())
        run.thread = threading.Thread(target=self._stream, args=(run,), daemon=True)
        with self._lock:
            with self._db.begin() as db:
                self._make_run_room(db)
            self._runs[info.run_id] = run
            self._persist_run(info)
        run.thread.start()
        return info.model_copy()

    def _stream(self, run: _Run) -> None:
        try:
            for reading in run.readings:
                with self._lock:
                    if run.stop.is_set():
                        break
                    result = self.ingest([reading])
                    run.info.sent += 1
                    if result.containers:
                        run.info.state = result.containers[0]
                    self._persist_run(run.info)
                if run.stop.wait(run.info.interval_seconds):
                    break
        except Exception as exc:
            logger.exception("시뮬레이션 스트림 실패: %s", run.info.run_id)
            run.info.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._finish(run)

    def _finish(self, run: _Run) -> None:
        with self._lock:
            run.info.done = True
            if self._runs.get(run.info.run_id) is not run:
                return
            del self._runs[run.info.run_id]
            try:
                self._persist_run(run.info)
            except Exception:
                logger.exception("시뮬레이션 종료 상태 저장 실패: %s", run.info.run_id)

    def _stored_run(self, run_id: str) -> SimulationRun:
        with self._db.begin() as db:
            row = db.get(RunRow, run_id)
            if row is None:
                raise NotFound("시뮬레이션 실행을 찾을 수 없습니다")
            info = SimulationRun.model_validate(row.payload)
        if not info.done:
            info.done = True
        return info

    def run(self, run_id: str) -> SimulationRun:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return self._stored_run(run_id)
            return run.info.model_copy()

    def stop(self, run_id: str) -> SimulationRun:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return self._stored_run(run_id)
            run.stop.set()
        if run.thread is not None:
            run.thread.join(timeout=5)
        return self.run(run_id)

    def wait(self, run_id: str, timeout: float = 30) -> SimulationRun:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            return self._stored_run(run_id)
        if run.thread is not None:
            run.thread.join(timeout=timeout)
        return self.run(run_id)


coldchain_store = ColdchainStore()
