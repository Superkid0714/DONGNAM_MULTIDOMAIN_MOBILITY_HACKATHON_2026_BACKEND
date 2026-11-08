from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert

from freshswitch_backend.domain.models import Voyage
from freshswitch_backend.forecast.history import (
    MAX_DELAY_HOURS,
    MIN_DELAY_HOURS,
    DepartureRecord,
    terminal_of,
    vessel_key,
)
from freshswitch_backend.integrations.mof_snapshot import load_demo_fixture
from freshswitch_backend.integrations.mof_vessel import (
    KST,
    MAX_GAP_HOURS,
    DepartureMatch,
    PlannedVoyage,
    VesselMovement,
    match_departures,
    normalize_call_sign,
)
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.tables import DelayObservationRow
from freshswitch_backend.quality import LearningScope, learning_scope
from freshswitch_backend.quality.store import ScopeRef, scope_ref

OWN_SOURCE = "자체 관측: schedule-check로 매칭된 실제 출항(계획 ETD 대비 해수부 출항일시)"
DEMO_SCOPE: LearningScope = "demo"


def planned_voyages(voyages: Iterable[Voyage]) -> list[PlannedVoyage]:
    return [
        PlannedVoyage(voyage_id=v.id, vessel_name=v.vessel_name, etd=v.etd, call_sign=v.call_sign)
        for v in voyages
    ]


def _movement_key(movement: VesselMovement) -> tuple[str, datetime] | None:
    if not movement.call_sign or movement.departure_at is None:
        return None
    return normalize_call_sign(movement.call_sign), movement.departure_at.astimezone(UTC)


def _synthetic_keys() -> set[tuple[str, datetime]]:
    fixture = load_demo_fixture()
    if fixture is None:
        return set()
    return {key for m in fixture.movements if (key := _movement_key(m)) is not None}


def real_movements(movements: Sequence[VesselMovement]) -> list[VesselMovement]:
    synthetic = _synthetic_keys()
    return [m for m in movements if _movement_key(m) not in synthetic]


def _row(match: DepartureMatch) -> dict[str, Any] | None:
    movement = match.movement
    if movement.departure_at is None:
        return None
    delay = (movement.departure_at - match.planned_etd).total_seconds() / 3600
    if not MIN_DELAY_HOURS <= delay <= MAX_DELAY_HOURS:
        return None
    call_sign = movement.call_sign or match.voyage.call_sign or None
    key = vessel_key(match.voyage.vessel_name, call_sign)
    if key is None:
        return None
    return {
        "voyage_id": match.voyage.voyage_id,
        "vessel_key": key,
        "vessel_name": match.voyage.vessel_name,
        "call_sign": call_sign,
        "matched_by": match.matched_by,
        "terminal": terminal_of(movement.berth),
        "vessel_kind": movement.vessel_kind,
        "planned_departure_at": match.planned_etd,
        "departure_at": movement.departure_at,
        "delay_hours": round(delay, 2),
    }


def _unique(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[Any, Any], dict[str, Any]] = {}
    for row in rows:
        key = (row["vessel_key"], row["planned_departure_at"])
        kept = best.get(key)
        if kept is None or abs(row["delay_hours"]) < abs(kept["delay_hours"]):
            best[key] = row
    return list(best.values())


def _ref(org_id: str) -> ScopeRef:
    return scope_ref(learning_scope(org_id))


def _delay_scope(scope: ScopeRef):
    kind = DelayObservationRow.scope == scope.kind
    if scope.world_id is None:
        return and_(kind, DelayObservationRow.world_id.is_(None))
    return and_(kind, DelayObservationRow.world_id == scope.world_id)


def record_matches(
    org_id: str,
    matches: Iterable[DepartureMatch],
    plan_id: str | None = None,
    target: Database | None = None,
) -> int:
    scope = _ref(org_id)
    if scope.kind == DEMO_SCOPE:
        return 0
    rows = _unique([r for m in matches if (r := _row(m)) is not None])
    if not rows:
        return 0
    values = [
        {**r, "org_id": org_id, "scope": scope.kind, "world_id": scope.world_id, "plan_id": plan_id}
        for r in rows
    ]
    statement = insert(DelayObservationRow).values(values)
    statement = statement.on_conflict_do_update(
        constraint="uq_delay_observations_org_vessel_planned",
        set_={
            name: statement.excluded[name]
            for name in (
                "plan_id",
                "voyage_id",
                "call_sign",
                "matched_by",
                "terminal",
                "vessel_kind",
                "departure_at",
                "delay_hours",
                "world_id",
            )
        }
        | {"recorded_at": func.now()},
    )
    with (target or database).begin() as db:
        db.execute(statement)
    return len(rows)


def record_schedule_check(
    org_id: str,
    voyages: Iterable[Voyage],
    movements: Sequence[VesselMovement],
    *,
    trusted: bool,
    plan_id: str | None = None,
    max_gap_hours: float = MAX_GAP_HOURS,
    target: Database | None = None,
) -> int:
    if not trusted or _ref(org_id).kind == DEMO_SCOPE:
        return 0
    matches = match_departures(planned_voyages(voyages), real_movements(movements), max_gap_hours)
    return record_matches(org_id, matches, plan_id, target)


def clear_observations(
    scope: LearningScope | ScopeRef,
    target: Database | None = None,
    world_id: str | None = None,
) -> int:
    ref = scope_ref(scope, world_id)
    if ref.kind == "sandbox" and ref.world_id is None:
        raise ValueError("world_id가 필요합니다")
    with (target or database).begin() as db:
        result = db.execute(delete(DelayObservationRow).where(_delay_scope(ref)))
    return result.rowcount or 0


def clear_org_observations(org_id: str, target: Database | None = None) -> int:
    with (target or database).begin() as db:
        result = db.execute(delete(DelayObservationRow).where(DelayObservationRow.org_id == org_id))
    return result.rowcount or 0


def own_records(org_id: str, target: Database | None = None) -> list[DepartureRecord]:
    scope = _ref(org_id)
    with (target or database).begin() as db:
        rows = db.scalars(
            select(DelayObservationRow).where(_delay_scope(scope)).order_by(DelayObservationRow.id)
        ).all()
    latest = {(r.vessel_key, r.planned_departure_at): r for r in rows}
    return [
        DepartureRecord(
            call_sign=r.call_sign or r.vessel_key,
            vessel_name=r.vessel_name,
            vessel_kind=r.vessel_kind,
            terminal=r.terminal,
            planned_departure_at=r.planned_departure_at.astimezone(KST),
            departure_at=r.departure_at.astimezone(KST),
            delay_hours=r.delay_hours,
        )
        for r in latest.values()
    ]
