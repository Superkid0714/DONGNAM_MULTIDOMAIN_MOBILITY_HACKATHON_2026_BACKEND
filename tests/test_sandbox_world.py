import secrets
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import SAAS_CSRF
from sqlalchemy import func, select

from freshswitch_backend.api.plans import CreatePlanRequest, create_tenant_plan
from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.coldchain.devices import rewrite_simulator_id
from freshswitch_backend.coldchain.models import SimulationRequest
from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database, sandbox_reset
from freshswitch_backend.persistence import sandbox_world as sandbox_world_module
from freshswitch_backend.persistence.sandbox_world import (
    SEED_ID,
    VisitorWorld,
    create_visitor_world,
    lock_world,
    purge_expired_worlds,
)
from freshswitch_backend.persistence.seed import seed_lots
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    AuthSessionRow,
    DeviceRow,
    LotRow,
    ObservationRow,
    OrganizationRow,
    PlanRow,
    SandboxWorldRow,
    UserRow,
)
from freshswitch_backend.planning.store import PlanSession, store

CSRF = SAAS_CSRF
INTERVAL = 0.05


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


@pytest.fixture
def demo_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": False}))


def _member(world: VisitorWorld, slug: str) -> str:
    return f"{world.world_id}-{slug}"


def _session(world: VisitorWorld, slug: str = "freshswitch") -> str:
    token_hash = secrets.token_hex(32)
    now = datetime.now(UTC)
    with database.begin() as db:
        db.add(
            AuthSessionRow(
                token_hash=token_hash,
                user_id=world.user_id,
                active_org_id=_member(world, slug),
                expires_at=now + timedelta(hours=12),
                last_seen_at=now,
            )
        )
    return token_hash


def _plan(org_id: str) -> PlanSession:
    return create_tenant_plan(CreatePlanRequest(include_registered=True), org_id)


def _world_lots(world: VisitorWorld) -> dict[str, LotRow]:
    with database.begin() as db:
        rows = db.scalars(select(LotRow).where(LotRow.shipper_org_id.startswith(world.world_id)))
        return {row.id: row for row in rows}


def _simulate(world: VisitorWorld, org_id: str, device_id: str, container_id: str):
    body = SimulationRequest(
        device_id=rewrite_simulator_id(device_id, world.world_id),
        interval_seconds=INTERVAL,
        container_id=container_id,
    )
    return coldchain_store._runs[coldchain_store.simulate(body, org_id).run_id]


def test_world_plans_and_runs_stay_in_their_world(demo_tenants) -> None:
    world_a = create_visitor_world()
    world_b = create_visitor_world()
    assert world_a.world_id != world_b.world_id
    forwarder_a = _member(world_a, "forwarder-dongnam")
    forwarder_b = _member(world_b, "forwarder-dongnam")
    plan_a = _plan(forwarder_a)
    assert {lot.id for lot in plan_a.scenario.lots} == set(_world_lots(world_a))
    assert {row.status for row in _world_lots(world_b).values()} == {"registered"}
    plan_b = _plan(forwarder_b)
    assert plan_b.plan_id != plan_a.plan_id
    assert {lot.id for lot in plan_b.scenario.lots} == set(_world_lots(world_b))
    assert store.get(plan_a.plan_id, forwarder_b) is None
    assert store.get(plan_b.plan_id, forwarder_a) is None
    run_b = _simulate(world_b, forwarder_b, "sim-b01", "CA-B")
    deadline = time.monotonic() + 5
    while run_b.info.sent < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    with sandbox_reset.demo_reset_lock():
        held = run_b.info.sent
        time.sleep(INTERVAL * 4)
        assert run_b.info.sent > held
    with lock_world(world_a.world_id):
        held = run_b.info.sent
        time.sleep(INTERVAL * 4)
        assert run_b.info.sent > held
    coldchain_store.stop_all()


def test_seed_plan_matches_visitor_totals(demo_tenants) -> None:
    with database.begin() as db:
        seed_lots(db)
    seed_plan = _plan("forwarder-dongnam")
    world = create_visitor_world()
    visitor_plan = _plan(_member(world, "forwarder-dongnam"))
    assert seed_plan.plan.total_savings == visitor_plan.plan.total_savings
    assert seed_plan.plan.total_fee == visitor_plan.plan.total_fee


def test_purge_removes_expired_world_only(demo_tenants) -> None:
    world = create_visitor_world()
    platform_org = _member(world, "freshswitch")
    session = _session(world)
    with database.begin() as db:
        assert db.get(AuthSessionRow, session) is not None
        db.add(OrganizationRow(id="live-platform", type="platform", name="실조직", sandbox=False))
        row = db.get(SandboxWorldRow, world.world_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=5)
    removed = purge_expired_worlds()
    assert removed >= 1
    with database.begin() as db:
        assert db.get(SandboxWorldRow, world.world_id) is None
        assert db.get(OrganizationRow, platform_org) is None
        assert db.get(UserRow, world.user_id) is None
        shipper_lots = select(func.count()).where(LotRow.shipper_org_id.startswith(world.world_id))
        assert db.scalar(shipper_lots) == 0
        assert db.scalar(select(func.count()).where(ObservationRow.world_id == world.world_id)) == 0
        assert db.scalar(select(func.count()).where(AuthSessionRow.user_id == world.user_id)) == 0
        assert db.get(OrganizationRow, DEMO_ORG) is not None
        assert db.get(OrganizationRow, "live-platform") is not None
        assert db.get(SandboxWorldRow, SEED_ID) is not None
        assert db.get(AuthSessionRow, session) is None


def test_reset_is_per_world(demo_app, reset_admin_headers) -> None:
    world_a = create_visitor_world()
    world_b = create_visitor_world()
    session_b = _session(world_b)
    forwarder_b = _member(world_b, "forwarder-dongnam")
    plan_b = _plan(forwarder_b)
    plan_a = _plan(_member(world_a, "forwarder-dongnam"))
    run = _simulate(world_b, forwarder_b, "sim-b01", "CA-B")
    deadline = time.monotonic() + 5
    while run.info.sent < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    admin = TestClient(demo_app, headers=reset_admin_headers)
    reset = admin.post("/api/demo/reset", params={"scope": "sandbox", "world_id": world_a.world_id})
    assert reset.status_code == 200, reset.text
    body = reset.json()
    assert body["orgs"] == 8
    assert body["world_id"] == world_a.world_id
    assert run.info.sent >= 2
    with pytest.raises(sandbox_reset.SandboxResetCooldown) as again:
        sandbox_reset.reset_sandbox(
            cooldown=sandbox_reset.RESET_COOLDOWN, world_id=world_a.world_id
        )
    assert again.value.retry_after > 0
    with database.begin() as db:
        assert db.get(PlanRow, plan_a.plan_id) is None
        assert db.get(PlanRow, plan_b.plan_id) is not None
        assert db.get(AuthSessionRow, session_b) is not None
    lots = _world_lots(world_a)
    assert len(lots) == 6
    assert {row.status for row in lots.values()} == {"registered"}
    missing = admin.post("/api/demo/reset", params={"scope": "sandbox"})
    assert missing.status_code == 422
    wiped = admin.post("/api/demo/reset", params={"scope": "sandbox", "all": "true"})
    assert wiped.status_code == 200, wiped.text
    with database.begin() as db:
        assert db.get(SandboxWorldRow, SEED_ID) is not None
        assert db.get(OrganizationRow, DEMO_ORG) is not None
        assert db.get(SandboxWorldRow, world_b.world_id) is not None
    missing_world = admin.post(
        "/api/demo/reset", params={"scope": "sandbox", "world_id": "wdeadbeef0000"}
    )
    assert missing_world.status_code == 404
    bare = admin.post("/api/quality/reset", params={"scope": "sandbox"})
    assert bare.status_code == 422
    missing_quality = admin.post(
        "/api/quality/reset", params={"scope": "sandbox", "world_id": "wdeadbeef0000"}
    )
    assert missing_quality.status_code == 404
    quality = admin.post(
        "/api/quality/reset",
        params={"scope": "sandbox", "world_id": world_b.world_id},
    )
    assert quality.status_code == 200, quality.text
    assert quality.json()["seeded"] == 6
    coldchain_store.stop_all()


def test_live_device_ids_stay_apart(saas_app) -> None:
    from saas_fixtures import (
        REAL_PLATFORM_EMAIL,
        REAL_PLATFORM_PASSWORD,
        create_real_platform,
        password_client,
    )

    token = create_visitor_world().world_id[1:]
    create_real_platform()
    live = password_client(saas_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    kept = live.post("/api/saas/devices", json={"device_id": "arduino-01"})
    assert kept.status_code == 201, kept.text
    with database.begin() as db:
        assert db.get(DeviceRow, "arduino-01") is not None
    demo_name = live.post("/api/saas/devices", json={"device_id": "demo-x"})
    assert demo_name.status_code == 422
    world_name = live.post("/api/saas/devices", json={"device_id": f"d{token}-x"})
    assert world_name.status_code == 422
    forwarder = TestClient(saas_app, base_url="https://testserver", headers=CSRF)
    signed = forwarder.post(
        "/api/auth/signup",
        json={
            "email": "forwarder@live.example.com",
            "password": "live-forwarder-password",
            "name": "실제 포워더",
            "org_name": "실제 포워더",
            "org_type": "forwarder",
        },
    )
    assert signed.status_code == 201, signed.text
    borrowed = forwarder.post(
        "/api/coldchain/bindings",
        json={"device_id": "sim-09", "container_id": "CA-01"},
    )
    assert borrowed.status_code == 422


def test_purge_continues_when_one_world_fails(demo_tenants, monkeypatch) -> None:
    ids = [create_visitor_world().world_id for _ in range(2)]
    with database.begin() as db:
        for world_id in ids:
            world = db.get(SandboxWorldRow, world_id)
            assert world is not None
            world.expires_at = datetime.now(UTC) - timedelta(seconds=5)
    real = sandbox_world_module.delete_world

    def flaky(world_id: str, *, allow_seed: bool = False) -> bool:
        if world_id == ids[0]:
            raise RuntimeError("boom")
        return real(world_id, allow_seed=allow_seed)

    monkeypatch.setattr(sandbox_world_module, "delete_world", flaky)
    assert purge_expired_worlds() == 1
    with database.begin() as db:
        assert db.get(SandboxWorldRow, ids[0]) is not None
        assert db.get(SandboxWorldRow, ids[1]) is None
