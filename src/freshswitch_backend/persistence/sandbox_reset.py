import math
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.persistence.database import RESET_LOCK, database
from freshswitch_backend.persistence.seed import SCENARIO_ID

SANDBOX_SCOPE = "sandbox"
RESET_COOLDOWN = timedelta(seconds=60)


class SandboxResetCooldown(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"{retry_after}초 뒤에 다시 초기화할 수 있습니다")
        self.retry_after = retry_after


@dataclass(frozen=True)
class SandboxResetResult:
    orgs: int
    reset_at: datetime
    world_id: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


clock: Callable[[], datetime] = _now
_demo_lock = threading.Lock()


@contextmanager
def demo_reset_lock() -> Iterator[None]:
    database.init()
    with _demo_lock:
        conn = database.engine.connect()
        conn.execute(select(func.pg_advisory_lock(RESET_LOCK)))
        conn.commit()
        try:
            yield
        finally:
            try:
                conn.execute(select(func.pg_advisory_unlock(RESET_LOCK)))
                conn.commit()
            finally:
                conn.close()


def _world_cooldown(reset_at: datetime | None, now: datetime, cooldown: timedelta | None) -> None:
    if cooldown is None or reset_at is None:
        return
    remaining = cooldown - (now - reset_at)
    if remaining > timedelta(0):
        raise SandboxResetCooldown(max(1, math.ceil(remaining.total_seconds())))


def _reset_one(
    world_id: str,
    keep_token_hash: str | None,
    cooldown: timedelta | None,
    scenario_id: str,
) -> SandboxResetResult:
    from freshswitch_backend.persistence.sandbox_world import (
        SEED_ID,
        WorldNotFound,
        drop_world_sessions,
        erase_world,
        lock_world,
        reseed_world,
    )
    from freshswitch_backend.persistence.sandbox_world import _org_ids as world_org_ids
    from freshswitch_backend.persistence.tables import SandboxWorldRow

    now = clock()
    with database.begin() as db:
        world = db.get(SandboxWorldRow, world_id)
        if world is None:
            if world_id != SEED_ID:
                raise WorldNotFound(world_id)
            from freshswitch_backend.persistence.seed import seed_demo_tenants, seed_lots

            seed_demo_tenants(db)
            seed_lots(db, scenario_id, now)
            world = db.get(SandboxWorldRow, world_id)
        if world is None:
            raise WorldNotFound(world_id)
        _world_cooldown(world.reset_at, now, cooldown)
        org_ids = world_org_ids(db, world_id)
    with lock_world(world_id):
        coldchain_store.stop_orgs(org_ids)
        with database.begin() as db:
            world = db.get(SandboxWorldRow, world_id)
            if world is None:
                raise WorldNotFound(world_id)
            _world_cooldown(world.reset_at, now, cooldown)
            drop_world_sessions(db, world_id, keep_token_hash)
        with database.begin() as db:
            if db.get(SandboxWorldRow, world_id) is None:
                raise WorldNotFound(world_id)
            erase_world(db, world_id, world_org_ids(db, world_id), keep_structure=True)
        with database.begin() as db:
            if db.get(SandboxWorldRow, world_id) is None:
                raise WorldNotFound(world_id)
            erase_world(db, world_id, world_org_ids(db, world_id), keep_structure=True)
            orgs = reseed_world(db, world_id, now, scenario_id)
    return SandboxResetResult(orgs=orgs, reset_at=now, world_id=world_id)


def _reset_all(scenario_id: str) -> SandboxResetResult:
    from freshswitch_backend.persistence.sandbox_world import SEED_ID
    from freshswitch_backend.persistence.tables import SandboxWorldRow

    now = clock()
    with database.begin() as db:
        ids = list(db.scalars(select(SandboxWorldRow.id).where(SandboxWorldRow.id != SEED_ID)))
    total = 0
    for world_id in ids:
        total += _reset_one(world_id, None, None, scenario_id).orgs
    return SandboxResetResult(orgs=total, reset_at=now, world_id=None)


def reset_sandbox(
    keep_token_hash: str | None = None,
    cooldown: timedelta | None = None,
    scenario_id: str = SCENARIO_ID,
    *,
    world_id: str | None = None,
    all_worlds: bool = False,
) -> SandboxResetResult:
    if all_worlds:
        return _reset_all(scenario_id)
    from freshswitch_backend.persistence.sandbox_world import SEED_ID

    return _reset_one(world_id or SEED_ID, keep_token_hash, cooldown, scenario_id)
