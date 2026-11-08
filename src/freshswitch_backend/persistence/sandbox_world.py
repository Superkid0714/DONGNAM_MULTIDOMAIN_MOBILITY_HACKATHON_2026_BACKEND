import logging
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.orm import Session

from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.persistence.database import WORLD_CREATE_LOCK, WORLD_LOCK, database
from freshswitch_backend.persistence.seed import DEMO_EMAIL_DOMAIN, SCENARIO_ID, demo_orgs
from freshswitch_backend.persistence.tables import (
    AlertRow,
    AuthSessionRow,
    BindingRow,
    ContainerRow,
    DelayObservationRow,
    DeviceRow,
    ForwarderResourceRow,
    LotRow,
    MembershipRow,
    MetaRow,
    ObservationRow,
    OrganizationRow,
    PackhouseRow,
    PlanRow,
    RunRow,
    SandboxWorldRow,
    ShipperProfileRow,
    UserRow,
)
from freshswitch_backend.quality.store import quality_history

logger = logging.getLogger(__name__)

SEED_ID = "seed"
SEED_EXPIRES = datetime(2099, 1, 1, tzinfo=UTC)
MAX_WORLDS = 40
INITIAL_TTL = timedelta(hours=2)
PLAN_TTL = timedelta(hours=12)
IDLE_EVICT_AFTER = timedelta(hours=1)
_create_thread_lock = threading.Lock()
_world_thread_guard = threading.Lock()
_world_thread_locks: dict[str, threading.Lock] = {}


class WorldCapacityError(Exception):
    retry_after = 60


class WorldNotFound(Exception):
    pass


@dataclass(frozen=True)
class VisitorWorld:
    world_id: str
    user_id: str
    orgs: tuple[tuple[str, str], ...]
    expires_at: datetime


def org_world_id(org_id: str | None) -> str | None:
    if not org_id:
        return None
    with database.begin() as db:
        return db.scalar(select(OrganizationRow.world_id).where(OrganizationRow.id == org_id))


_VISITOR_WORLD = re.compile(r"^w[0-9a-f]{12}$")


def ignores_client_lot_id(org_id: str | None) -> bool:
    world_id = org_world_id(org_id)
    return world_id is not None and _VISITOR_WORLD.fullmatch(world_id) is not None


def _now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _session_lock(statement: str, params: dict[str, object]) -> Iterator[None]:
    database.init()
    conn = database.engine.connect()
    conn.execute(text(statement), params)
    conn.commit()
    try:
        yield
    finally:
        unlock = statement.replace("pg_advisory_lock", "pg_advisory_unlock", 1)
        try:
            conn.execute(text(unlock), params)
            conn.commit()
        finally:
            conn.close()


def _world_thread_lock(world_id: str) -> threading.Lock:
    with _world_thread_guard:
        lock = _world_thread_locks.get(world_id)
        if lock is None:
            lock = threading.Lock()
            _world_thread_locks[world_id] = lock
        return lock


@contextmanager
def lock_world(world_id: str) -> Iterator[None]:
    with (
        _world_thread_lock(world_id),
        _session_lock(
            "SELECT pg_advisory_lock(:key, hashtext(:world))",
            {"key": WORLD_LOCK, "world": world_id},
        ),
    ):
        yield


def _lock_create(db: Session) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": WORLD_CREATE_LOCK})


def _org_ids(db: Session, world_id: str) -> list[str]:
    return list(
        db.scalars(
            select(OrganizationRow.id)
            .where(OrganizationRow.world_id == world_id)
            .order_by(OrganizationRow.id)
        )
    )


def _live_count(db: Session, now: datetime) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(SandboxWorldRow)
            .where(SandboxWorldRow.expires_at >= now, SandboxWorldRow.id != SEED_ID)
        )
        or 0
    )


def _has_plan():
    return exists(
        select(PlanRow.plan_id)
        .join(OrganizationRow, OrganizationRow.id == PlanRow.org_id)
        .where(OrganizationRow.world_id == SandboxWorldRow.id)
    )


def _last_seen():
    return (
        select(func.max(AuthSessionRow.last_seen_at))
        .select_from(AuthSessionRow)
        .join(UserRow, UserRow.id == AuthSessionRow.user_id)
        .where(UserRow.world_id == SandboxWorldRow.id)
        .correlate(SandboxWorldRow)
        .scalar_subquery()
    )


def _planless_ids(db: Session, now: datetime) -> list[str]:
    return list(
        db.scalars(
            select(SandboxWorldRow.id)
            .where(
                SandboxWorldRow.expires_at >= now,
                SandboxWorldRow.id != SEED_ID,
                ~_has_plan(),
            )
            .order_by(SandboxWorldRow.created_at, SandboxWorldRow.id)
        )
    )


def _idle_planned_ids(db: Session, now: datetime) -> list[str]:
    seen = _last_seen()
    cutoff = now - IDLE_EVICT_AFTER
    return list(
        db.scalars(
            select(SandboxWorldRow.id)
            .where(
                SandboxWorldRow.expires_at >= now,
                SandboxWorldRow.id != SEED_ID,
                _has_plan(),
                or_(seen.is_(None), seen < cutoff),
            )
            .order_by(seen.asc().nulls_first(), SandboxWorldRow.created_at, SandboxWorldRow.id)
        )
    )


def _prefix(world_id: str) -> str:
    return "" if world_id == SEED_ID else f"{world_id}-"


def _lot_rows(
    db: Session, world_id: str, now: datetime, scenario_id: str = SCENARIO_ID
) -> list[dict[str, object]]:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        return []
    prefix = _prefix(world_id)
    profiles = db.execute(
        select(ShipperProfileRow.id, ShipperProfileRow.org_id, ShipperProfileRow.packhouse_id)
        .join(OrganizationRow, OrganizationRow.id == ShipperProfileRow.org_id)
        .where(OrganizationRow.world_id == world_id)
    ).all()
    owners: dict[str, tuple[str, str]] = {}
    for profile_id, org_id, packhouse_id in profiles:
        bare = profile_id[len(prefix) :] if prefix and profile_id.startswith(prefix) else profile_id
        owners[bare] = (org_id, packhouse_id)
    rows = []
    for lot in scenario.lots:
        owner = owners.get(lot.shipper_id)
        if owner is None:
            continue
        org_id, packhouse_id = owner
        rows.append(
            {
                "id": f"{prefix}{lot.id}",
                "scenario_id": scenario.id,
                "shipper_id": f"{prefix}{lot.shipper_id}",
                "shipper_org_id": org_id,
                "product_id": lot.product_id,
                "pallets": lot.pallets,
                "ready_date": lot.ready_date,
                "destination": lot.destination,
                "sell_by_date": lot.sell_by_date,
                "sell_window": lot.sell_window,
                "packhouse": packhouse_id,
                "source": lot.source,
                "status": "registered",
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def expected_world_lots(db: Session, now: datetime | None = None) -> list[dict[str, object]]:
    stamp = now or _now()
    worlds = list(db.scalars(select(SandboxWorldRow.id)))
    rows: list[dict[str, object]] = []
    for world_id in worlds:
        rows.extend(_lot_rows(db, world_id, stamp))
    return rows


def _insert_lots(db: Session, world_id: str, now: datetime, scenario_id: str = SCENARIO_ID) -> None:
    rows = _lot_rows(db, world_id, now, scenario_id)
    if rows:
        db.add_all([LotRow(**row) for row in rows])


def _clone_catalog(db: Session, world_id: str, orgs: list) -> None:
    scenario = get_scenario(SCENARIO_ID)
    if scenario is None:
        raise RuntimeError(f"시나리오 {SCENARIO_ID}를 찾을 수 없습니다")
    prefix = f"{world_id}-"
    packhouses = {p.id: p for p in scenario.packhouses}
    products: dict[str, list[str]] = {}
    for lot in scenario.lots:
        bucket = products.setdefault(lot.shipper_id, [])
        if lot.product_id not in bucket:
            bucket.append(lot.product_id)
    shippers = {s.id: s for s in scenario.shippers}
    owned = [(f"{prefix}{org.id}", shippers[sid]) for org in orgs for sid in org.shipper_ids]
    for org_id, shipper in owned:
        packhouse = packhouses[shipper.packhouse_id]
        db.add(
            PackhouseRow(
                id=f"{prefix}{packhouse.id}",
                org_id=org_id,
                name=packhouse.name,
                region=packhouse.region,
                lat=packhouse.lat,
                lon=packhouse.lon,
                source=packhouse.source,
            )
        )
        db.add(
            ShipperProfileRow(
                id=f"{prefix}{shipper.id}",
                org_id=org_id,
                name=shipper.name,
                packhouse_id=f"{prefix}{shipper.packhouse_id}",
                product_ids=products.get(shipper.id, []),
                source=shipper.source,
            )
        )
    from freshswitch_backend.persistence.resources import ensure_sandbox_resources

    ensure_sandbox_resources(db, db.get(OrganizationRow, f"{prefix}{orgs[0].id}"))


def _password_hash() -> str:
    from freshswitch_backend.api.auth import _dummy_hash

    return _dummy_hash()


def _insert_world(db: Session, now: datetime) -> VisitorWorld:
    world_id = "w" + secrets.token_hex(6)
    while db.get(SandboxWorldRow, world_id) is not None:
        world_id = "w" + secrets.token_hex(6)
    expires = now + INITIAL_TTL
    db.add(
        SandboxWorldRow(
            id=world_id,
            created_at=now,
            expires_at=expires,
            quality_seeded=False,
        )
    )
    db.flush()
    orgs = demo_orgs()
    prefix = f"{world_id}-"
    ordered: list[tuple[str, str]] = []
    for org in orgs:
        org_id = f"{prefix}{org.id}"
        ordered.append((org_id, org.type))
        db.add(
            OrganizationRow(
                id=org_id, type=org.type, name=org.name, sandbox=True, world_id=world_id
            )
        )
    user_id = f"user-{world_id}"
    db.add(
        UserRow(
            id=user_id,
            email=f"{world_id}@{DEMO_EMAIL_DOMAIN}",
            password_hash=_password_hash(),
            name="데모",
            is_demo=True,
            world_id=world_id,
        )
    )
    db.flush()
    for index, (org_id, _) in enumerate(ordered):
        db.add(
            MembershipRow(
                user_id=user_id,
                org_id=org_id,
                role="admin",
                created_at=now + timedelta(microseconds=index),
            )
        )
    _clone_catalog(db, world_id, orgs)
    db.flush()
    _insert_lots(db, world_id, now)
    quality_history.seed_world(db, world_id, prefixed=True)
    db.flush()
    return VisitorWorld(world_id, user_id, tuple(ordered), expires)


def erase_world(db: Session, world_id: str, org_ids: list[str], *, keep_structure: bool) -> None:
    db.execute(delete(ObservationRow).where(ObservationRow.world_id == world_id))
    db.execute(delete(DelayObservationRow).where(DelayObservationRow.world_id == world_id))
    if org_ids:
        plan_ids = list(db.scalars(select(PlanRow.plan_id).where(PlanRow.org_id.in_(org_ids))))
        if plan_ids:
            db.execute(
                delete(ObservationRow).where(
                    ObservationRow.origin == "arrival", ObservationRow.plan_id.in_(plan_ids)
                )
            )
            db.execute(delete(PlanRow).where(PlanRow.plan_id.in_(plan_ids)))
        db.execute(delete(AlertRow).where(AlertRow.org_id.in_(org_ids)))
        db.execute(delete(ContainerRow).where(ContainerRow.org_id.in_(org_ids)))
        db.execute(delete(BindingRow).where(BindingRow.org_id.in_(org_ids)))
        db.execute(delete(RunRow).where(RunRow.org_id.in_(org_ids)))
        db.execute(delete(DeviceRow).where(DeviceRow.org_id.in_(org_ids)))
        db.execute(delete(LotRow).where(LotRow.shipper_org_id.in_(org_ids)))
    db.execute(delete(MetaRow).where(MetaRow.key == f"forwarder_resources_seeded:{world_id}"))
    if org_ids:
        db.execute(delete(ForwarderResourceRow).where(ForwarderResourceRow.org_id.in_(org_ids)))
    if keep_structure:
        return
    if org_ids:
        db.execute(delete(ShipperProfileRow).where(ShipperProfileRow.org_id.in_(org_ids)))
        db.execute(delete(PackhouseRow).where(PackhouseRow.org_id.in_(org_ids)))
        db.execute(delete(OrganizationRow).where(OrganizationRow.id.in_(org_ids)))
    db.execute(delete(UserRow).where(UserRow.world_id == world_id))
    db.execute(delete(SandboxWorldRow).where(SandboxWorldRow.id == world_id))


def drop_world_sessions(db: Session, world_id: str, keep_token_hash: str | None) -> None:
    users = select(UserRow.id).where(UserRow.world_id == world_id)
    stmt = delete(AuthSessionRow).where(AuthSessionRow.user_id.in_(users))
    if keep_token_hash is not None:
        stmt = stmt.where(AuthSessionRow.token_hash != keep_token_hash)
    db.execute(stmt)


def reseed_world(db: Session, world_id: str, now: datetime, scenario_id: str = SCENARIO_ID) -> int:
    from freshswitch_backend.persistence.resources import ensure_sandbox_resources

    org = db.scalar(select(OrganizationRow).where(OrganizationRow.world_id == world_id).limit(1))
    ensure_sandbox_resources(db, org)
    _insert_lots(db, world_id, now, scenario_id)
    quality_history.seed_world(db, world_id, prefixed=world_id != SEED_ID)
    world = db.get(SandboxWorldRow, world_id)
    if world is not None:
        world.quality_seeded = True
        world.reset_at = now
    return len(_org_ids(db, world_id))


def delete_world(world_id: str, *, allow_seed: bool = False) -> bool:
    if world_id == SEED_ID and not allow_seed:
        return False
    with database.begin() as db:
        if db.get(SandboxWorldRow, world_id) is None:
            return False
        org_ids = _org_ids(db, world_id)
    with lock_world(world_id):
        coldchain_store.stop_orgs(org_ids)
        with database.begin() as db:
            if db.get(SandboxWorldRow, world_id) is None:
                return False
            erase_world(db, world_id, _org_ids(db, world_id), keep_structure=False)
    return True


def _forget(world_id: str, failed: set[str]) -> None:
    try:
        delete_world(world_id, allow_seed=True)
    except Exception:
        logger.exception("세계 %s 삭제에 실패했습니다", world_id)
        failed.add(world_id)


def purge_expired_worlds(now: datetime | None = None) -> int:
    moment = now or _now()
    with database.begin() as db:
        ids = list(
            db.scalars(select(SandboxWorldRow.id).where(SandboxWorldRow.expires_at < moment))
        )
    removed = 0
    for world_id in ids:
        try:
            if delete_world(world_id, allow_seed=True):
                removed += 1
        except Exception:
            logger.exception("세계 %s 삭제에 실패했습니다", world_id)
    return removed


def _victims(db: Session, now: datetime, failed: set[str]) -> list[str]:
    projected = _live_count(db, now)
    chosen: list[str] = []
    for world_id in [*_planless_ids(db, now), *_idle_planned_ids(db, now)]:
        if world_id in failed:
            continue
        if projected < MAX_WORLDS:
            break
        chosen.append(world_id)
        projected -= 1
    return chosen


def _expired_ids(db: Session, now: datetime) -> list[str]:
    return list(db.scalars(select(SandboxWorldRow.id).where(SandboxWorldRow.expires_at < now)))


def create_visitor_world(now: datetime | None = None) -> VisitorWorld:
    moment = now or _now()
    failed: set[str] = set()
    with _create_thread_lock:
        for _ in range(MAX_WORLDS + 1):
            with database.begin() as db:
                _lock_create(db)
                pending = [
                    world_id for world_id in _expired_ids(db, moment) if world_id not in failed
                ]
                pending.extend(_victims(db, moment, failed))
                if not pending:
                    if _live_count(db, moment) >= MAX_WORLDS:
                        raise WorldCapacityError
                    return _insert_world(db, moment)
            for world_id in pending:
                _forget(world_id, failed)
        raise WorldCapacityError


def extend_first_plan(db: Session, org_id: str) -> None:
    org = db.get(OrganizationRow, org_id)
    if org is None or not org.world_id:
        return
    world = db.get(SandboxWorldRow, org.world_id)
    if world is None:
        return
    target = world.created_at + PLAN_TTL
    if world.expires_at >= target:
        return
    world.expires_at = target
    users = select(UserRow.id).where(UserRow.world_id == world.id)
    db.execute(
        update(AuthSessionRow)
        .where(AuthSessionRow.user_id.in_(users), AuthSessionRow.expires_at < target)
        .values(expires_at=target)
    )


def seed_visitor_telemetry(plan_id: str, org_id: str) -> int:
    world_id = org_world_id(org_id)
    if world_id is None or _VISITOR_WORLD.fullmatch(world_id) is None:
        return 0
    from freshswitch_backend.coldchain.telemetry import seed_container
    from freshswitch_backend.planning.store import store as plan_store

    session = plan_store.get(plan_id, org_id=org_id)
    if session is None:
        return 0
    containers = sorted(
        {a.container_id for a in session.plan.assignments if a.mode == "sea" and a.container_id}
    )
    end = coldchain_store.clock()
    seeded = 0
    for container_id in containers:
        try:
            done = seed_container(
                coldchain_store,
                org_id=org_id,
                plan_id=plan_id,
                container_id=container_id,
                seed=f"{world_id}:{container_id}",
                end=end,
            )
        except Exception:
            logger.exception(
                "세계 %s 컨테이너 %s 측정값을 만들지 못했습니다", world_id, container_id
            )
            continue
        seeded += done is not None
    return seeded


def ensure_seed_world(db: Session, now: datetime | None = None) -> None:
    moment = now or _now()
    if db.get(SandboxWorldRow, SEED_ID) is None:
        db.add(
            SandboxWorldRow(
                id=SEED_ID,
                created_at=moment,
                expires_at=SEED_EXPIRES,
                quality_seeded=False,
            )
        )
        db.flush()


def mark_seed_members(db: Session, org_ids: list[str], user_ids: list[str]) -> None:
    if org_ids:
        db.execute(
            update(OrganizationRow)
            .where(OrganizationRow.id.in_(org_ids))
            .values(world_id=SEED_ID, sandbox=True)
        )
    if user_ids:
        db.execute(update(UserRow).where(UserRow.id.in_(user_ids)).values(world_id=SEED_ID))


def ensure_seed_quality(db: Session) -> None:
    world = db.get(SandboxWorldRow, SEED_ID)
    if world is None or world.quality_seeded:
        return
    quality_history.seed_world(db, SEED_ID, prefixed=False)
    world.quality_seeded = True
