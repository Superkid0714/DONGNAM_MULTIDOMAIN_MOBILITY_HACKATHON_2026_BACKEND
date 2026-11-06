import logging
import math
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import Literal

from pydantic import TypeAdapter
from sqlalchemy import delete, func, select, text, tuple_, update
from sqlalchemy.orm import Session

from freshswitch_backend.coldchain.models import (
    ActionHandler,
    Alert,
    AlertFeed,
    Binding,
    BindingRequest,
    ColdchainAction,
    IngestRejected,
    IngestResult,
    Reading,
    ReadingIn,
    ResidualRisk,
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
from freshswitch_backend.domain.models import Quantity
from freshswitch_backend.execution.quality import (
    ArrivalRequest,
    QualityError,
    arrival_settlement,
)
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.database import RESET_LOCK, WORLD_LOCK
from freshswitch_backend.persistence.tables import (
    ALERT_SEQ,
    DEMO_ORG,
    AlertRow,
    BindingRow,
    ContainerRow,
    DeviceRow,
    OrganizationRow,
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
RESET_POLL_SECONDS = 0.05
AMBIGUOUS = "동일 컨테이너의 계획이 여러 개입니다. plan_id를 지정하세요"
STALE_BINDING = "연결된 계획이 삭제되었습니다. 기기를 다시 바인딩하세요"
DEFAULT_STAGE: Stage = "terminal"
PREVIEW_RULE = (
    "트윈 예상 손실률을 도착 검수 손실률 대신 넣어 품질 사후정산을 미리 계산한다. "
    "저장하지 않는다. 실제 환급은 도착 검수(POST /settlement/arrival)로 확정한다"
)


class ColdchainError(ValueError):
    pass


class NotFound(LookupError):
    pass


class DeviceDenied(ColdchainError):
    pass


class ActionConflict(ColdchainError):
    pass


ActionTransition = Literal["accept", "complete"]
HEAT_MARGIN = Quantity(
    value=1.2,
    unit="배",
    source=(
        "가정: 적정 하한 위 적산 온도(°C·시간)가 적정 범위 중앙을 유지했을 때의 1.2배를 넘으면 "
        "도착 뒤 유통기한 단축 위험. 손실률 모델은 도착 시점 손실만 계산한다"
    ),
)
SWING_STD_C = Quantity(
    value=1.0,
    unit="°C",
    source="가정: 품온 표준편차가 1°C를 넘으면 결로·호흡 변동 위험. 손실률 모델은 출렁임을 보지 않는다",
)
DOOR_OPEN_COUNT = Quantity(
    value=1,
    unit="회",
    source="가정: 문 열림 측정이 한 번이라도 있으면 외기 유입 위험. 손실률 모델은 문 열림을 보지 않는다",
)


@dataclass
class OutageEpisode:
    start: datetime
    end: datetime | None
    minutes: float
    start_temp_c: float
    max_temp_c: float
    end_temp_c: float | None


def _point_gap(prev: TimelinePoint, cur: TimelinePoint) -> float:
    return max(0.0, (cur.ts - prev.ts).total_seconds() / 60)


def outage_episodes(points: list[TimelinePoint], max_gap: float) -> list[OutageEpisode]:
    episodes: list[OutageEpisode] = []
    current: OutageEpisode | None = None
    prev: TimelinePoint | None = None
    for point in points:
        if current is not None and prev is not None:
            current.minutes += min(_point_gap(prev, point), max_gap)
            current.max_temp_c = max(current.max_temp_c, point.temp_c)
        if not point.power_on and current is None:
            current = OutageEpisode(
                start=point.ts,
                end=None,
                minutes=0.0,
                start_temp_c=point.temp_c,
                max_temp_c=point.temp_c,
                end_temp_c=None,
            )
            episodes.append(current)
        elif point.power_on and current is not None:
            current.end = point.ts
            current.end_temp_c = point.temp_c
            current = None
        prev = point
    return episodes


def residual_risks(
    points: list[TimelinePoint], band_min_c: float, band_max_c: float, params: ColdchainParams
) -> list[ResidualRisk]:
    if not points:
        return []
    max_gap = params.max_gap_minutes.value
    heat = 0.0
    elapsed = 0.0
    gap = 0.0
    crossings = 0
    for prev, cur in pairwise(points):
        dt = _point_gap(prev, cur)
        gap += max(0.0, dt - max_gap)
        dt = min(dt, max_gap)
        elapsed += dt
        heat += (max(0.0, prev.temp_c - band_min_c) + max(0.0, cur.temp_c - band_min_c)) / 2 * dt
        if prev.temp_c <= band_max_c < cur.temp_c:
            crossings += 1
    heat_h = heat / 60
    middle = max((band_max_c - band_min_c) / 2, 0.5)
    heat_limit = middle * elapsed / 60 * HEAT_MARGIN.value
    temps = [p.temp_c for p in points]
    mean = sum(temps) / len(temps)
    std = math.sqrt(sum((t - mean) ** 2 for t in temps) / len(temps))
    risks = [
        ResidualRisk(
            kind="heat_accumulation",
            level="warning" if heat_h > heat_limit else "info",
            title="적산 온도",
            value=round(heat_h, 2),
            unit="°C·시간",
            threshold=Quantity(
                value=round(heat_limit, 2), unit="°C·시간", source=HEAT_MARGIN.source
            ),
            message=(
                f"적정 하한({band_min_c:g}°C) 위 적산 {heat_h:.1f}°C·시간, "
                f"범위 중앙 유지 시 {heat_limit / HEAT_MARGIN.value:.1f}°C·시간. "
                "도착 뒤 유통기한 단축은 손실률에 들어 있지 않다"
            ),
        ),
        ResidualRisk(
            kind="temperature_swing",
            level="warning" if std > SWING_STD_C.value else "info",
            title="온도 출렁임",
            value=round(std, 2),
            unit="°C",
            threshold=SWING_STD_C,
            message=(
                f"품온 표준편차 {std:.2f}°C, 적정 상한 위로 넘어간 횟수 {crossings}회. "
                "결로·호흡 변동은 손실률에 들어 있지 않다"
            ),
        ),
        ResidualRisk(
            kind="sensor_gap",
            level="warning" if gap > 0 else "info",
            title="측정 공백",
            value=round(gap, 1),
            unit="분",
            threshold=params.max_gap_minutes,
            message=(
                f"측정 간격이 {max_gap:g}분을 넘은 공백 {gap:.0f}분. 공백 동안 상태는 "
                f"{max_gap:g}분까지만 이어 센다"
            ),
        ),
    ]
    doors = [p.door_open for p in points if p.door_open is not None]
    if doors:
        opened = sum(1 for d in doors if d)
        risks.append(
            ResidualRisk(
                kind="door_open",
                level="warning" if opened >= DOOR_OPEN_COUNT.value else "info",
                title="문 열림",
                value=opened,
                unit="회",
                threshold=DOOR_OPEN_COUNT,
                message=f"문 열림 측정 {opened}회. 외기 유입은 손실률에 들어 있지 않다",
            )
        )
    return risks


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
    org_id: str = DEMO_ORG

    @property
    def key(self) -> str:
        return _record_key(self.plan_id, self.container_id, self.org_id)

    def sorted_readings(self) -> list[Reading]:
        return sorted(self.readings.values(), key=lambda r: (r.ts, r.device_id))


@dataclass
class _Run:
    info: SimulationRun
    readings: list[ReadingIn]
    stop: threading.Event
    thread: threading.Thread | None = None
    org_id: str = DEMO_ORG
    world_id: str | None = None
    watches_reset: bool = False
    cancelled: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _device_owner(db: Session, device_id: str) -> str | None:
    return db.scalar(select(DeviceRow.org_id).where(DeviceRow.device_id == device_id))


def _device_allowed(owner: str | None, org_id: str) -> bool:
    return owner is None or owner in (DEMO_ORG, org_id)


def _tick_lock(db: Session, run: _Run) -> bool:
    if run.world_id:
        locked = db.scalar(
            select(func.pg_try_advisory_xact_lock_shared(WORLD_LOCK, func.hashtext(run.world_id)))
        )
        return bool(locked)
    if run.watches_reset:
        return bool(db.scalar(select(func.pg_try_advisory_xact_lock_shared(RESET_LOCK))))
    return True


def _run_scope(db: Session, org_id: str) -> tuple[str | None, bool]:
    org = db.get(OrganizationRow, org_id)
    world_id = org.world_id if org is not None else None
    watches_reset = org_id == DEMO_ORG or (org is not None and org.type == "demo")
    return world_id, watches_reset


def _foreign_device(device_id: str) -> str:
    return f"다른 조직에 등록된 장치입니다: {device_id}"


def _binding_mismatch(item: ReadingIn, binding: Binding | None) -> str | None:
    if binding is None:
        return f"장치가 컨테이너에 연결되어 있지 않습니다: {item.device_id}"
    if item.container_id is not None and item.container_id != binding.container_id:
        return f"장치에 연결된 컨테이너({binding.container_id})와 다릅니다: {item.container_id}"
    if item.plan_id is not None and item.plan_id != binding.plan_id:
        return f"장치에 연결된 계획과 다릅니다: {item.plan_id}"
    return None


def _record_key(plan_id: str | None, container_id: str, org_id: str = DEMO_ORG) -> str:
    if plan_id:
        return f"{plan_id}:{container_id}"
    if org_id == DEMO_ORG:
        return f"-:{container_id}"
    return f"-{org_id}:{container_id}"


def _status_of(alert: Alert) -> str | None:
    if alert.kind != "status_change":
        return None
    return alert.key.split(":", 2)[1]


def _same_status_run(alert: Alert, evaluated: list[Alert], stored: dict[str, Alert]) -> bool:
    status = _status_of(alert)
    if status is None:
        return False
    changes = [item for item in [*evaluated, *stored.values()] if _status_of(item) is not None]
    until = min(
        (item.at for item in changes if _status_of(item) != status and item.at > alert.at),
        default=None,
    )
    return any(
        _status_of(item) == status and item.at >= alert.at and (until is None or item.at < until)
        for item in stored.values()
    )


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

    def _stop_runs(self, org_id: str) -> None:
        self.stop_orgs([org_id])

    def stop_orgs(self, org_ids: list[str]) -> None:
        wanted = set(org_ids)
        if not wanted:
            return
        with self._lock:
            runs = [run for run in self._runs.values() if run.org_id in wanted]
            for run in runs:
                run.cancelled = True
                run.stop.set()
                del self._runs[run.info.run_id]
        for run in runs:
            if run.thread is not None and run.thread is not threading.current_thread():
                run.thread.join(timeout=5)

    def clear(self, org_id: str = DEMO_ORG) -> int:
        self._stop_runs(org_id)
        with self._lock:
            with self._db.begin() as db:
                count = (
                    db.scalar(
                        select(func.count())
                        .select_from(ContainerRow)
                        .where(ContainerRow.org_id == org_id)
                    )
                    or 0
                )
                db.execute(delete(ContainerRow).where(ContainerRow.org_id == org_id))
                db.execute(delete(BindingRow).where(BindingRow.org_id == org_id))
                db.execute(delete(RunRow).where(RunRow.org_id == org_id))
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

    def _session(self, plan_id: str | None, org_id: str = DEMO_ORG) -> PlanSession | None:
        return plan_store.get(plan_id, org_id=org_id) if plan_id else None

    def require_plan(
        self, plan_id: str | None, container_id: str | None = None, org_id: str = DEMO_ORG
    ) -> None:
        if plan_id is None:
            return
        session = self._session(plan_id, org_id)
        if session is None:
            raise NotFound("계획을 찾을 수 없습니다")
        if container_id is not None and container_id not in {
            c.id for c in session.scenario.containers
        }:
            raise ColdchainError(f"계획에 없는 컨테이너입니다: {container_id}")

    def _matches(
        self, db: Session, container_id: str, plan_id: str | None = None, org_id: str = DEMO_ORG
    ) -> list[ContainerRow]:
        query = select(ContainerRow).where(
            ContainerRow.container_id == container_id, ContainerRow.org_id == org_id
        )
        if plan_id is not None:
            query = query.where(ContainerRow.plan_id == plan_id)
        return list(db.scalars(query.order_by(ContainerRow.seq)))

    def _resolve_plan(
        self, db: Session, container_id: str, plan_id: str | None, org_id: str = DEMO_ORG
    ) -> str | None:
        if plan_id is not None:
            return plan_id
        matches = self._matches(db, container_id, None, org_id)
        if len(matches) > 1:
            raise ColdchainError(AMBIGUOUS)
        return matches[0].plan_id if matches else None

    def _find(
        self, db: Session, container_id: str, plan_id: str | None = None, org_id: str = DEMO_ORG
    ) -> ContainerRecord | None:
        matches = self._matches(db, container_id, plan_id, org_id)
        if len(matches) > 1:
            raise ColdchainError(AMBIGUOUS)
        return self._load(db, matches[0]) if matches else None

    def _load(self, db: Session, row: ContainerRow) -> ContainerRecord:
        key = row.record_key
        readings = db.scalars(select(ReadingRow).where(ReadingRow.record_key == key))
        alerts = db.scalars(
            select(AlertRow)
            .where(AlertRow.record_key == key)
            .order_by(AlertRow.created_at, AlertRow.seq)
        )
        record = ContainerRecord(
            container_id=row.container_id,
            plan_id=row.plan_id,
            stage=row.stage,
            carry=_CARRY.validate_python(row.carry) if row.carry is not None else None,
            history=_HISTORY.validate_python(row.history),
            anchors=set(_ANCHORS.validate_python(row.anchors)),
            stored=True,
            org_id=row.org_id,
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
                    org_id=record.org_id,
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
                    org_id=record.org_id,
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
        org_id: str = DEMO_ORG,
    ) -> ContainerRecord:
        key = _record_key(plan_id, container_id, org_id)
        record = records.get(key)
        if record is None:
            row = db.get(ContainerRow, key)
            if row is not None and row.org_id != org_id:
                raise NotFound("계획을 찾을 수 없습니다")
            record = (
                self._load(db, row)
                if row is not None
                else ContainerRecord(container_id=container_id, plan_id=plan_id, org_id=org_id)
            )
            records[key] = record
        return record

    def _trim(self, db: Session, org_id: str = DEMO_ORG) -> None:
        scope = ContainerRow.org_id == org_id
        cutoff = db.scalar(
            select(ContainerRow.seq)
            .where(scope)
            .order_by(ContainerRow.seq.desc())
            .offset(MAX_CONTAINERS)
            .limit(1)
        )
        if cutoff is not None:
            db.execute(delete(ContainerRow).where(ContainerRow.seq <= cutoff, scope))

    def bind(
        self, body: BindingRequest, org_id: str = DEMO_ORG, *, plan_required: bool = False
    ) -> Binding:
        with self._lock, self._db.begin() as db:
            if not _device_allowed(_device_owner(db, body.device_id), org_id):
                raise DeviceDenied(_foreign_device(body.device_id))
            plan_id = self._resolve_plan(db, body.container_id, body.plan_id, org_id)
            if plan_id is None and plan_required and org_id != DEMO_ORG:
                raise ColdchainError("계획을 지정하세요")
            self.require_plan(plan_id, body.container_id, org_id)
            binding = Binding(
                device_id=body.device_id,
                container_id=body.container_id,
                plan_id=plan_id,
                stage=body.stage,
                bound_at=self.clock(),
            )
            row = db.get(BindingRow, body.device_id)
            if row is not None and row.org_id != org_id:
                raise ColdchainError(f"다른 조직에 연결된 장치입니다: {body.device_id}")
            payload = binding.model_dump(mode="json")
            if row is None:
                db.add(
                    BindingRow(
                        device_id=body.device_id,
                        org_id=org_id,
                        container_id=body.container_id,
                        plan_id=plan_id,
                        payload=payload,
                    )
                )
            else:
                row.container_id = body.container_id
                row.plan_id = plan_id
                row.payload = payload
            record = self._record(db, {}, body.container_id, plan_id, org_id)
            if body.stage:
                record.stage = body.stage
            self._save(db, record)
            self._trim(db, org_id)
            return binding

    def bindings(self, org_id: str = DEMO_ORG) -> list[Binding]:
        with self._db.begin() as db:
            rows = db.scalars(
                select(BindingRow).where(BindingRow.org_id == org_id).order_by(BindingRow.device_id)
            )
            return [Binding.model_validate(row.payload) for row in rows]

    def _binding(self, db: Session, device_id: str, org_id: str = DEMO_ORG) -> Binding | None:
        row = db.get(BindingRow, device_id)
        if row is None or row.org_id != org_id:
            return None
        return Binding.model_validate(row.payload)

    def _context(self, record: ContainerRecord, plan_id: str | None = None) -> TwinContext:
        plan_id = plan_id or record.plan_id
        session = self._session(plan_id, record.org_id)
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
            if _same_status_run(alert, evaluation.alerts, record.alerts):
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
            open_actions=sum(
                1
                for a in record.alerts.values()
                if a.action is not None and a.action.status != "done"
            ),
        )

    def ingest(
        self, items: list[ReadingIn], org_id: str = DEMO_ORG, bound_only: bool = False
    ) -> IngestResult:
        with self._lock, self._db.begin() as db:
            return self._ingest(db, items, org_id, bound_only)

    def _ingest(
        self, db: Session, items: list[ReadingIn], org_id: str, bound_only: bool = False
    ) -> IngestResult:
        accepted = 0
        duplicates = 0
        rejected: list[IngestRejected] = []
        touched: list[str] = []
        new_alerts: list[Alert] = []
        records: dict[str, ContainerRecord] = {}
        bindings: dict[str, Binding | None] = {}
        checked: dict[tuple[str | None, str], tuple[str | None, str | None]] = {}
        live: dict[str, bool] = {}
        owners: dict[str, str | None] = {}
        for index, item in enumerate(items):
            if item.device_id not in owners:
                owners[item.device_id] = _device_owner(db, item.device_id)
            if not _device_allowed(owners[item.device_id], org_id):
                rejected.append(
                    IngestRejected(
                        index=index,
                        device_id=item.device_id,
                        reason=_foreign_device(item.device_id),
                    )
                )
                continue
            if item.device_id not in bindings:
                bindings[item.device_id] = self._binding(db, item.device_id, org_id)
            binding = bindings[item.device_id]
            if bound_only:
                reason = _binding_mismatch(item, binding)
                if reason is not None:
                    rejected.append(
                        IngestRejected(index=index, device_id=item.device_id, reason=reason)
                    )
                    continue
                item = item.model_copy(update={"container_id": None, "plan_id": None})
                bound_plan = binding.plan_id if binding else None
                if bound_plan is not None and bound_plan not in live:
                    live[bound_plan] = self._session(bound_plan, org_id) is not None
                if bound_plan is None or not live[bound_plan]:
                    rejected.append(
                        IngestRejected(index=index, device_id=item.device_id, reason=STALE_BINDING)
                    )
                    continue
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
                        live[plan_id] = self._session(plan_id, org_id) is not None
                    if not live[plan_id]:
                        plan_id = None
            check = (plan_id, container_id)
            if check not in checked:
                try:
                    resolved = self._resolve_plan(db, container_id, plan_id, org_id)
                    self.require_plan(resolved, container_id, org_id)
                    checked[check] = (resolved, None)
                except (NotFound, ColdchainError) as exc:
                    checked[check] = (None, str(exc))
            plan_id, reason = checked[check]
            if reason is not None:
                rejected.append(
                    IngestRejected(index=index, device_id=item.device_id, reason=reason)
                )
                continue
            record = self._record(db, records, container_id, plan_id, org_id)
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
        self._trim(db, org_id)
        return IngestResult(
            accepted=accepted,
            duplicates=duplicates,
            rejected=rejected,
            containers=summaries,
            new_alerts=new_alerts,
        )

    def get_record(
        self, container_id: str, plan_id: str | None = None, org_id: str = DEMO_ORG
    ) -> ContainerRecord:
        with self._db.begin() as db:
            record = self._find(db, container_id, plan_id, org_id)
        if record is None:
            raise NotFound(f"센서 기록이 없는 컨테이너입니다: {container_id}")
        return record

    def state(
        self,
        container_id: str,
        plan_id: str | None = None,
        points: int = 720,
        org_id: str = DEMO_ORG,
    ) -> TwinState:
        self.require_plan(plan_id, container_id, org_id)
        with self._lock:
            with self._db.begin() as db:
                record = self._find(db, container_id, plan_id, org_id)
            has_data = record is not None
            record = record or ContainerRecord(
                container_id=container_id, plan_id=plan_id, org_id=org_id
            )
            ctx, ev = self._evaluate(record, plan_id)
            alerts = list(record.alerts.values())
            trail = [*record.history, *ev.timeline]
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
                residual_risks=residual_risks(trail, ctx.band_min_c, ctx.band_max_c, self.params),
            )

    def update_action(
        self,
        container_id: str,
        action_key: str,
        transition: ActionTransition,
        handler: ActionHandler,
        note: str | None = None,
        plan_id: str | None = None,
        org_id: str = DEMO_ORG,
    ) -> ColdchainAction:
        with self._lock, self._db.begin() as db:
            matches = self._matches(db, container_id, plan_id, org_id)
            if len(matches) > 1:
                raise ColdchainError(AMBIGUOUS)
            if not matches:
                raise NotFound(f"센서 기록이 없는 컨테이너입니다: {container_id}")
            row = db.scalar(
                select(AlertRow).where(
                    AlertRow.record_key == matches[0].record_key, AlertRow.key == action_key
                )
            )
            alert = Alert.model_validate(row.payload) if row is not None else None
            if alert is None or alert.action is None:
                raise NotFound(f"조치를 찾을 수 없습니다: {action_key}")
            action = alert.action
            target = "accepted" if transition == "accept" else "done"
            if action.status == target:
                return action
            if action.status == "done":
                raise ActionConflict("이미 완료된 조치입니다")
            now = self.clock()
            update: dict = {"status": target, "note": note if note is not None else action.note}
            if action.accepted_at is None:
                update |= {"accepted_at": now, "accepted_by": handler}
            if target == "done":
                update |= {"done_at": now, "done_by": handler}
            action = action.model_copy(update=update)
            seq = db.scalar(ALERT_SEQ.next_value())
            row.seq = seq
            row.payload = alert.model_copy(update={"seq": seq, "action": action}).model_dump(
                mode="json"
            )
            return action

    def export_points(
        self, container_id: str, plan_id: str | None = None, org_id: str = DEMO_ORG
    ) -> tuple[list[Reading], list[TimelinePoint], list[OutageEpisode]]:
        self.require_plan(plan_id, container_id, org_id)
        with self._lock:
            record = self.get_record(container_id, plan_id, org_id)
            _, ev = self._evaluate(record, plan_id)
        points = [*record.history, *ev.timeline]
        episodes = outage_episodes(points, self.params.max_gap_minutes.value)
        return record.sorted_readings(), record.history, episodes

    def summaries(self, plan_id: str | None = None, org_id: str = DEMO_ORG) -> list[TwinSummary]:
        with self._lock:
            with self._db.begin() as db:
                query = (
                    select(ContainerRow)
                    .where(ContainerRow.org_id == org_id)
                    .order_by(ContainerRow.seq)
                )
                if plan_id is not None:
                    query = query.where(ContainerRow.plan_id == plan_id)
                records = [self._load(db, row) for row in db.scalars(query).all()]
            out = []
            for record in records:
                ctx, ev = self._evaluate(record)
                out.append(self._summary(record, ctx, ev))
            return out

    def reset_container(
        self, container_id: str, plan_id: str | None = None, org_id: str = DEMO_ORG
    ) -> bool:
        with self._lock:
            with self._db.begin() as db:
                matches = self._matches(db, container_id, plan_id, org_id)
                if len(matches) > 1:
                    raise ColdchainError(AMBIGUOUS)
                resolved = matches[0].plan_id if matches else plan_id
                deleted = db.execute(
                    delete(ContainerRow).where(
                        ContainerRow.record_key == _record_key(resolved, container_id, org_id),
                        ContainerRow.org_id == org_id,
                    )
                ).rowcount
            for run in self._runs.values():
                if (
                    run.org_id == org_id
                    and run.info.container_id == container_id
                    and run.info.plan_id == resolved
                ):
                    run.stop.set()
            return bool(deleted)

    def _last_seq(self, db: Session) -> int:
        row = db.execute(text(f"SELECT last_value, is_called FROM {ALERT_SEQ.name}")).one()
        return row.last_value if row.is_called else 0

    def alerts(
        self,
        since: int = 0,
        container_id: str | None = None,
        limit: int = 100,
        plan_id: str | None = None,
        org_id: str = DEMO_ORG,
    ) -> AlertFeed:
        with self._db.begin() as db:
            query = (
                select(AlertRow)
                .where(AlertRow.seq > since, AlertRow.org_id == org_id)
                .order_by(AlertRow.seq)
            )
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
        self, container_id: str, plan_id: str | None = None, org_id: str = DEMO_ORG
    ) -> SettlementPreview:
        self.require_plan(plan_id, container_id, org_id)
        with self._lock:
            record = self.get_record(container_id, plan_id, org_id)
            plan_id = plan_id or record.plan_id
            session = self._session(plan_id, org_id)
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

    def _make_run_room(self, db: Session, org_id: str = DEMO_ORG) -> None:
        scope = RunRow.org_id == org_id
        total = db.scalar(select(func.count()).select_from(RunRow).where(scope)) or 0
        excess = total - MAX_RUNS + 1
        if excess <= 0:
            return
        finished = db.scalars(
            select(RunRow.run_id)
            .where(RunRow.done.is_(True), scope)
            .order_by(RunRow.seq)
            .limit(excess)
        ).all()
        if len(finished) < excess:
            raise ColdchainError("동시에 실행할 수 있는 시뮬레이션 수를 초과했습니다")
        db.execute(delete(RunRow).where(RunRow.run_id.in_(finished)))

    def _insert_run(self, info: SimulationRun, org_id: str = DEMO_ORG) -> None:
        with self._db.begin() as db:
            db.add(
                RunRow(
                    run_id=info.run_id,
                    org_id=org_id,
                    container_id=info.container_id,
                    done=info.done,
                    payload=info.model_dump(mode="json"),
                )
            )

    def _update_run(self, db: Session, info: SimulationRun) -> None:
        db.execute(
            update(RunRow)
            .where(RunRow.run_id == info.run_id)
            .values(done=info.done, payload=info.model_dump(mode="json"))
        )

    def _persist_run(self, info: SimulationRun) -> None:
        with self._db.begin() as db:
            self._update_run(db, info)

    def simulate(self, body: SimulationRequest, org_id: str = DEMO_ORG) -> SimulationRun:
        preset = PRESETS[body.preset]
        with self._lock:
            with self._db.begin() as db:
                if not _device_allowed(_device_owner(db, body.device_id), org_id):
                    raise DeviceDenied(_foreign_device(body.device_id))
                world_id, watches_reset = _run_scope(db, org_id)
                self._make_run_room(db, org_id)
                plan_id = self._resolve_plan(db, body.container_id, body.plan_id, org_id)
                self.require_plan(plan_id, body.container_id, org_id)
            if body.reset:
                self.reset_container(body.container_id, plan_id, org_id)
            with self._db.begin() as db:
                record = self._record(db, {}, body.container_id, plan_id, org_id)
                record.stage = body.stage
                ctx = self._context(record)
                self._save(db, record)
                self._trim(db, org_id)
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
                    self._make_run_room(db, org_id)
                result = self.ingest(readings, org_id)
                info.sent = len(readings)
                info.done = True
                info.state = result.containers[0] if result.containers else None
                self._insert_run(info, org_id)
            return info
        run = _Run(
            info=info,
            readings=readings,
            stop=threading.Event(),
            org_id=org_id,
            world_id=world_id,
            watches_reset=watches_reset,
        )
        run.thread = threading.Thread(target=self._stream, args=(run,), daemon=True)
        with self._lock:
            with self._db.begin() as db:
                self._make_run_room(db, org_id)
            self._runs[info.run_id] = run
            self._insert_run(info, org_id)
        run.thread.start()
        return info.model_copy()

    def _stream(self, run: _Run) -> None:
        try:
            for reading in run.readings:
                if not self._tick(run, reading):
                    break
                if run.stop.wait(run.info.interval_seconds):
                    break
        except Exception as exc:
            logger.exception("시뮬레이션 스트림 실패: %s", run.info.run_id)
            run.info.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._finish(run)

    def _tick(self, run: _Run, reading: ReadingIn) -> bool:
        while True:
            with self._lock:
                if run.stop.is_set():
                    return False
                self._db.init()
                with Session(self._db.engine, expire_on_commit=False) as db:
                    if _tick_lock(db, run):
                        if db.get(RunRow, run.info.run_id) is None:
                            run.cancelled = True
                            return False
                        result = self._ingest(db, [reading], run.org_id)
                        run.info.sent += 1
                        if result.containers:
                            run.info.state = result.containers[0]
                        self._update_run(db, run.info)
                        db.commit()
                        return True
                    db.rollback()
            if run.stop.wait(RESET_POLL_SECONDS):
                return False

    def _finish(self, run: _Run) -> None:
        with self._lock:
            run.info.done = True
            if self._runs.get(run.info.run_id) is not run:
                return
            del self._runs[run.info.run_id]
            if run.cancelled:
                return
            try:
                self._persist_run(run.info)
            except Exception:
                logger.exception("시뮬레이션 종료 상태 저장 실패: %s", run.info.run_id)

    def _stored_run(self, run_id: str, org_id: str = DEMO_ORG) -> SimulationRun:
        with self._db.begin() as db:
            row = db.get(RunRow, run_id)
            if row is None or row.org_id != org_id:
                raise NotFound("시뮬레이션 실행을 찾을 수 없습니다")
            info = SimulationRun.model_validate(row.payload)
        if not info.done:
            info.done = True
        return info

    def _live_run(self, run_id: str, org_id: str) -> _Run | None:
        run = self._runs.get(run_id)
        if run is None or run.org_id != org_id:
            return None
        return run

    def run(self, run_id: str, org_id: str = DEMO_ORG) -> SimulationRun:
        with self._lock:
            run = self._live_run(run_id, org_id)
            if run is None:
                return self._stored_run(run_id, org_id)
            return run.info.model_copy()

    def stop(self, run_id: str, org_id: str = DEMO_ORG) -> SimulationRun:
        with self._lock:
            run = self._live_run(run_id, org_id)
            if run is None:
                return self._stored_run(run_id, org_id)
            run.stop.set()
        if run.thread is not None:
            run.thread.join(timeout=5)
        return self.run(run_id, org_id)

    def wait(self, run_id: str, timeout: float = 30, org_id: str = DEMO_ORG) -> SimulationRun:
        with self._lock:
            run = self._live_run(run_id, org_id)
        if run is None:
            return self._stored_run(run_id, org_id)
        if run.thread is not None:
            run.thread.join(timeout=timeout)
        return self.run(run_id, org_id)


coldchain_store = ColdchainStore()
