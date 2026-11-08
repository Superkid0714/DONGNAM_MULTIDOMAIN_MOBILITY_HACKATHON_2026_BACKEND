import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import SAAS_CSRF
from sqlalchemy import func, select, update

from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database, sandbox_reset
from freshswitch_backend.persistence import sandbox_world as sandbox_world_module
from freshswitch_backend.persistence.sandbox_world import (
    SEED_ID,
    create_visitor_world,
    lock_world,
    purge_expired_worlds,
)
from freshswitch_backend.persistence.seed import demo_orgs, seed_lots
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

CSRF = SAAS_CSRF
INTERVAL = 0.05


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _client(saas_app, body: dict | None = None) -> tuple[TestClient, dict]:
    client = TestClient(saas_app, base_url="https://testserver", headers=CSRF)
    response = client.post("/api/auth/demo-login", json=body or {"new_world": True})
    assert response.status_code == 200, response.text
    return client, response.json()


def _org(me: dict, kind: str) -> str:
    return next(item["org_id"] for item in me["memberships"] if item["org_type"] == kind)


def _plan(client: TestClient) -> dict:
    response = client.post("/api/saas/platform/plans", json={"include_registered": True})
    assert response.status_code == 201, response.text
    return response.json()


def test_two_visitors_are_isolated(saas_app) -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    client_a, me_a = _client(saas_app)
    client_b, me_b = _client(saas_app)
    assert me_a["world"]["world_id"] != me_b["world"]["world_id"]
    assert [item["org_type"] for item in me_a["memberships"]] == [org.type for org in demo_orgs()]
    assert len(me_a["memberships"]) == 8
    assert me_a["active_org"]["org_type"] == "platform"
    plan_a = _plan(client_a)
    lots_b = client_b.get("/api/saas/platform/lots").json()
    assert len(lots_b) == 6
    assert {row["status"] for row in lots_b} == {"registered"}
    plan_b = _plan(client_b)
    assert plan_b["plan_id"] != plan_a["plan_id"]
    missing = client_b.get(f"/api/plans/{plan_a['plan_id']}")
    assert missing.status_code == 404
    shock = client_b.post("/api/analysis/fuel-shock", json={"plan_id": plan_a["plan_id"]})
    assert shock.status_code == 404
    lot_id = plan_a["scenario"]["lots"][0]["id"]
    compared = client_b.post(
        "/api/compare/air-vs-sea", json={"plan_id": plan_a["plan_id"], "lot_id": lot_id}
    )
    assert compared.status_code == 404
    obs_a = client_a.get("/api/quality/observations").json()["observations"][0]["id"]
    assert client_b.get(f"/api/quality/observations/{obs_a}").status_code == 404
    learned = client_b.get("/api/quality/loss-rates").json()
    dumped = str(learned)
    assert obs_a not in dumped
    token_a = me_a["world"]["world_id"][1:]
    token_b = me_b["world"]["world_id"][1:]
    registered = client_a.post("/api/saas/devices", json={"device_id": "sensor-1"})
    assert registered.status_code == 201, registered.text
    assert registered.json()["device_id"] == f"d{token_a}-sensor-1"
    stolen = client_b.post("/api/saas/devices", json={"device_id": registered.json()["device_id"]})
    assert stolen.status_code == 422
    own = client_b.post("/api/saas/devices", json={"device_id": "sensor-1"})
    assert own.status_code == 201, own.text
    assert own.json()["device_id"] == f"d{token_b}-sensor-1"
    sim_a = client_a.post(
        "/api/coldchain/simulate",
        json={"device_id": "sim-01", "interval_seconds": INTERVAL, "container_id": "CA-A"},
    )
    sim_b = client_b.post(
        "/api/coldchain/simulate",
        json={"device_id": "sim-01", "interval_seconds": INTERVAL, "container_id": "CA-B"},
    )
    assert sim_a.status_code == 200, sim_a.text
    assert sim_b.status_code == 200, sim_b.text
    assert sim_a.json()["device_id"] == f"sim-{token_a}-01"
    assert sim_b.json()["device_id"] == f"sim-{token_b}-01"
    run_b = coldchain_store._runs[sim_b.json()["run_id"]]
    deadline = time.monotonic() + 5
    while run_b.info.sent < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    with sandbox_reset.demo_reset_lock():
        held = run_b.info.sent
        time.sleep(INTERVAL * 4)
        assert run_b.info.sent > held
    with lock_world(me_a["world"]["world_id"]):
        held = run_b.info.sent
        time.sleep(INTERVAL * 4)
        assert run_b.info.sent > held
    shipper = _org(me_a, "shipper")
    denied = client_a.get("/api/saas/platform/lots", headers={"X-Org-Id": shipper})
    assert denied.status_code == 403
    foreign = client_a.put(
        f"/api/saas/platform/plans/{plan_a['plan_id']}/forwarder",
        json={"forwarder_org_id": _org(me_b, "forwarder")},
    )
    assert foreign.status_code == 422
    other_lot = lots_b[0]
    added = client_a.post(
        f"/api/saas/platform/plans/{plan_a['plan_id']}/add-lot",
        json={"lot_id": other_lot["id"]},
    )
    assert added.status_code == 404
    assert added.json()["detail"] == "등록된 출하를 찾을 수 없습니다"
    owned = {lot["id"] for lot in client_a.get(f"/api/plans/{plan_a['plan_id']}").json()["scenario"]["lots"]}
    assert other_lot["id"] not in owned
    with database.begin() as db:
        foreign = db.get(LotRow, other_lot["id"])
        assert foreign is not None
        assert foreign.status == "planned"
        assert foreign.shipper_org_id.startswith(me_b["world"]["world_id"])
    catalog = client_a.get("/api/saas/catalog").json()
    bare = []
    for shipper_row in catalog["shippers"]:
        profile_id = shipper_row["id"]
        mark = profile_id.rfind("-sh-")
        bare.append(profile_id[mark + 1 :] if mark >= 0 else profile_id)
    assert bare == [shipper.id for shipper in scenario.shippers]
    benchmark = client_a.post("/api/analysis/benchmark", json={"plan_id": plan_a["plan_id"]})
    assert benchmark.status_code == 200, benchmark.text
    event_lot = benchmark.json()["replan"]["event"]["lot_id"]
    assert event_lot.endswith("-lot-sancheong-sb-1")
    again = client_a.post("/api/auth/demo-login", json={"new_world": True})
    assert again.status_code == 200, again.text
    assert again.json()["world"]["world_id"] != me_a["world"]["world_id"]
    with database.begin() as db:
        assert db.get(OrganizationRow, me_a["active_org"]["org_id"]) is None
    coldchain_store.stop_all()


def test_seed_plan_matches_visitor_totals(saas_app) -> None:
    with database.begin() as db:
        seed_lots(db)
    seed_client = TestClient(create_app(), base_url="https://testserver", headers=CSRF)
    seeded = seed_client.post(
        "/api/auth/demo-login", json={"org_type": "platform", "org_id": "freshswitch"}
    )
    assert seeded.status_code == 200, seeded.text
    seed_plan = _plan(seed_client)
    visitor, _ = _client(saas_app)
    visitor_plan = _plan(visitor)
    assert seed_plan["plan"]["total_savings"] == visitor_plan["plan"]["total_savings"]
    assert seed_plan["plan"]["total_fee"] == visitor_plan["plan"]["total_fee"]


def test_purge_removes_expired_world_only(saas_app) -> None:
    client, me = _client(saas_app)
    world_id = me["world"]["world_id"]
    with database.begin() as db:
        db.add(OrganizationRow(id="live-platform", type="platform", name="실조직", sandbox=False))
        world = db.get(SandboxWorldRow, world_id)
        assert world is not None
        world.expires_at = datetime.now(UTC) - timedelta(seconds=5)
    removed = purge_expired_worlds()
    assert removed >= 1
    with database.begin() as db:
        assert db.get(SandboxWorldRow, world_id) is None
        assert db.get(OrganizationRow, me["active_org"]["org_id"]) is None
        assert db.get(UserRow, me["user"]["id"]) is None
        assert db.scalar(select(func.count()).where(LotRow.shipper_org_id == me["active_org"]["org_id"])) == 0
        assert db.scalar(select(func.count()).where(ObservationRow.world_id == world_id)) == 0
        assert db.scalar(select(func.count()).where(AuthSessionRow.user_id == me["user"]["id"])) == 0
        assert db.get(OrganizationRow, DEMO_ORG) is not None
        assert db.get(OrganizationRow, "live-platform") is not None
        assert db.get(SandboxWorldRow, SEED_ID) is not None
    assert client.get("/api/me").status_code == 401
    opened = client.post("/api/auth/demo-login", json={"org_type": "platform"})
    assert opened.status_code == 200, opened.text
    assert opened.json()["world"]["world_id"] != world_id


def test_world_cap_drops_planless_then_rejects(saas_app) -> None:
    worlds = []
    for _ in range(40):
        _, me = _client(saas_app)
        worlds.append(me["world"]["world_id"])
    oldest = worlds[0]
    extra, me_extra = _client(saas_app)
    assert extra is not None
    assert me_extra["world"]["world_id"] != oldest
    with database.begin() as db:
        assert db.get(SandboxWorldRow, oldest) is None
        living = list(
            db.scalars(select(SandboxWorldRow.id).where(SandboxWorldRow.id != SEED_ID))
        )
    assert len(living) == 40
    now = datetime.now(UTC)
    with database.begin() as db:
        for world_id in living:
            org_id = db.scalar(
                select(OrganizationRow.id).where(
                    OrganizationRow.world_id == world_id, OrganizationRow.type == "platform"
                )
            )
            db.add(
                PlanRow(
                    plan_id=uuid.uuid4().hex,
                    org_id=org_id,
                    scenario_id="default",
                    version=1,
                    status="draft",
                    lot_count=0,
                    totals={},
                    created_at=now,
                    updated_at=now,
                    payload={},
                    committed={},
                )
            )
    blocked = TestClient(saas_app, base_url="https://testserver", headers=CSRF)
    response = blocked.post("/api/auth/demo-login", json={"new_world": True})
    assert response.status_code == 429, response.text
    assert response.headers["retry-after"] == "60"
    assert "가득" in response.json()["detail"]
    now = datetime.now(UTC)
    with database.begin() as db:
        ordered = list(
            db.scalars(
                select(SandboxWorldRow.id)
                .where(SandboxWorldRow.id != SEED_ID)
                .order_by(SandboxWorldRow.created_at, SandboxWorldRow.id)
            )
        )
        oldest = ordered[0]
        newest = ordered[-1]
        db.execute(update(AuthSessionRow).values(last_seen_at=now - timedelta(hours=2)))
        newest_users = select(UserRow.id).where(UserRow.world_id == newest)
        db.execute(
            update(AuthSessionRow)
            .where(AuthSessionRow.user_id.in_(newest_users))
            .values(last_seen_at=now)
        )
    opened = blocked.post("/api/auth/demo-login", json={"new_world": True})
    assert opened.status_code == 200, opened.text
    with database.begin() as db:
        assert db.get(SandboxWorldRow, oldest) is None
        assert db.get(SandboxWorldRow, newest) is not None
        living = list(db.scalars(select(SandboxWorldRow.id).where(SandboxWorldRow.id != SEED_ID)))
    assert len(living) == 40
    assert opened.json()["world"]["world_id"] in living


def test_role_switch_stays_in_the_world(saas_app) -> None:
    client, me = _client(saas_app)
    shipper = next(item["org_id"] for item in me["memberships"] if item["org_id"].endswith("shipper-jinju"))
    forwarder = next(
        item["org_id"] for item in me["memberships"] if item["org_id"].endswith("forwarder-dongnam")
    )
    switched = client.put("/api/auth/active-org", json={"org_id": shipper})
    assert switched.status_code == 200, switched.text
    assert switched.json()["active_org"]["org_type"] == "shipper"
    assert client.get("/api/saas/platform/lots").status_code == 403
    lots = client.get("/api/saas/shipper/lots")
    assert lots.status_code == 200, lots.text
    assert lots.json()
    forwarded = client.put("/api/auth/active-org", json={"org_id": forwarder})
    assert forwarded.status_code == 200
    assert client.get("/api/saas/forwarder/plans").json() == []
    peeked = client.get("/api/saas/platform/lots", headers={"X-Org-Id": me["active_org"]["org_id"]})
    assert peeked.status_code == 200, peeked.text
    assert client.get("/api/me").json()["active_org"]["org_id"] == forwarder
    other = create_visitor_world()
    denied = client.put("/api/auth/active-org", json={"org_id": other.orgs[0][0]})
    assert denied.status_code == 403
    assert client.get("/api/me").status_code == 200
    with database.begin() as db:
        session = db.scalar(select(AuthSessionRow).where(AuthSessionRow.user_id == me["user"]["id"]))
        assert session is not None
        session.active_org_id = None
    assert client.get("/api/me").json()["active_org"]["org_type"] == "platform"


def test_reset_is_per_world(saas_app, reset_admin_headers) -> None:
    client_a, me_a = _client(saas_app)
    client_b, me_b = _client(saas_app)
    plan_b = _plan(client_b)
    _plan(client_a)
    sim = client_b.post(
        "/api/coldchain/simulate",
        json={"device_id": "sim-01", "interval_seconds": INTERVAL, "container_id": "CA-B"},
    )
    assert sim.status_code == 200, sim.text
    run = coldchain_store._runs[sim.json()["run_id"]]
    deadline = time.monotonic() + 5
    while run.info.sent < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    reset = client_a.post("/api/saas/sandbox/reset")
    assert reset.status_code == 200, reset.text
    body = reset.json()
    assert body["orgs"] == 8
    assert body["world_id"] == me_a["world"]["world_id"]
    assert run.info.sent >= 2
    again = client_a.post("/api/saas/sandbox/reset")
    assert again.status_code == 429
    assert again.headers["retry-after"]
    assert client_a.get("/api/me").json()["world"]["world_id"] == me_a["world"]["world_id"]
    assert client_a.get("/api/saas/platform/plans").json() == []
    lots = client_a.get("/api/saas/platform/lots").json()
    assert len(lots) == 6
    assert {row["status"] for row in lots} == {"registered"}
    assert client_b.get("/api/me").status_code == 200
    visible = client_b.get("/api/saas/platform/plans").json()
    assert plan_b["plan_id"] in {row["plan_id"] for row in visible}
    admin = TestClient(saas_app, headers=reset_admin_headers)
    missing = admin.post("/api/demo/reset", params={"scope": "sandbox"})
    assert missing.status_code == 422
    wiped = admin.post("/api/demo/reset", params={"scope": "sandbox", "all": "true"})
    assert wiped.status_code == 200, wiped.text
    with database.begin() as db:
        assert db.get(SandboxWorldRow, SEED_ID) is not None
        assert db.get(OrganizationRow, DEMO_ORG) is not None
        assert db.get(SandboxWorldRow, me_b["world"]["world_id"]) is not None
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
        params={"scope": "sandbox", "world_id": me_b["world"]["world_id"]},
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

    _, me = _client(saas_app)
    token = me["world"]["world_id"][1:]
    create_real_platform()
    live = password_client(saas_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    kept = live.post("/api/saas/devices", json={"device_id": "arduino-01"})
    assert kept.status_code == 201, kept.text
    visitor = TestClient(saas_app, base_url="https://testserver", headers=CSRF)
    logged = visitor.post("/api/auth/demo-login", json={"new_world": True})
    assert logged.status_code == 200
    copied = visitor.post("/api/saas/devices", json={"device_id": "arduino-01"})
    assert copied.status_code == 201, copied.text
    assert copied.json()["device_id"] == f"d{logged.json()['world']['world_id'][1:]}-arduino-01"
    with database.begin() as db:
        assert db.get(DeviceRow, "arduino-01") is not None
    demo_name = live.post("/api/saas/devices", json={"device_id": "demo-x"})
    assert demo_name.status_code == 422
    world_name = live.post("/api/saas/devices", json={"device_id": f"d{token}-x"})
    assert world_name.status_code == 422
    borrowed = live.post(
        "/api/coldchain/bindings",
        json={"device_id": "sim-09", "container_id": "CA-01"},
    )
    assert borrowed.status_code == 422


def test_purge_continues_when_one_world_fails(saas_app, monkeypatch) -> None:
    ids = []
    for _ in range(2):
        _, me = _client(saas_app)
        ids.append(me["world"]["world_id"])
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


def test_create_skips_a_world_that_cannot_be_evicted(saas_app, monkeypatch) -> None:
    monkeypatch.setattr(sandbox_world_module, "MAX_WORLDS", 1)
    _, me = _client(saas_app)
    stuck = me["world"]["world_id"]
    real = sandbox_world_module.delete_world

    def flaky(world_id: str, *, allow_seed: bool = False) -> bool:
        if world_id == stuck:
            raise RuntimeError("boom")
        return real(world_id, allow_seed=allow_seed)

    monkeypatch.setattr(sandbox_world_module, "delete_world", flaky)
    blocked = TestClient(saas_app, base_url="https://testserver", headers=CSRF)
    response = blocked.post("/api/auth/demo-login", json={"new_world": True})
    assert response.status_code == 429, response.text
    with database.begin() as db:
        assert db.get(SandboxWorldRow, stuck) is not None


def test_visitor_registers_plans_assigns_and_simulates(saas_app) -> None:
    client, me = _client(saas_app)
    world_id = me["world"]["world_id"]
    assert world_id.startswith("w") and len(world_id) == 13
    platform = me["active_org"]["org_id"]
    shipper = next(item["org_id"] for item in me["memberships"] if item["org_id"].endswith("shipper-jinju"))
    forwarder = next(
        item["org_id"] for item in me["memberships"] if item["org_id"].endswith("forwarder-dongnam")
    )
    switched = client.put("/api/auth/active-org", json={"org_id": shipper})
    assert switched.status_code == 200, switched.text
    created = client.post(
        "/api/saas/shipper/lots",
        json={
            "product": "strawberry",
            "pallets": 8,
            "ready_date": "2026-11-16",
            "destination": "HKHKG",
            "sell_by_date": "2026-11-23",
            "lot_id": "client-should-ignore",
        },
    )
    assert created.status_code == 201, created.text
    lot_id = created.json()["registered"]["id"]
    assert lot_id.startswith(f"lot-{world_id}-")
    assert lot_id != "client-should-ignore"
    assert client.put("/api/auth/active-org", json={"org_id": platform}).status_code == 200
    planned = client.post(
        "/api/saas/platform/plans",
        json={"lot_ids": [lot_id], "include_registered": True},
    )
    assert planned.status_code == 201, planned.text
    body = planned.json()
    plan_id = body["plan_id"]
    assert lot_id in {lot["id"] for lot in body["scenario"]["lots"]}
    loaded = [item for item in body["plan"]["containers"] if item["used"]]
    assert loaded
    assert any(lot_id in item["lot_ids"] for item in loaded)
    container_id = next(item["container_id"] for item in loaded if lot_id in item["lot_ids"])
    assigned = client.put(
        f"/api/saas/platform/plans/{plan_id}/forwarder",
        json={"forwarder_org_id": forwarder},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["forwarder"]["org_id"] == forwarder
    assert client.put("/api/auth/active-org", json={"org_id": forwarder}).status_code == 200
    visible = client.get("/api/saas/forwarder/plans")
    assert visible.status_code == 200, visible.text
    assert plan_id in {item["plan_id"] for item in visible.json()}
    assert client.put("/api/auth/active-org", json={"org_id": platform}).status_code == 200
    simulated = client.post(
        "/api/coldchain/simulate",
        json={
            "preset": "power_cut_restore",
            "plan_id": plan_id,
            "container_id": container_id,
            "device_id": "sim-01",
        },
    )
    assert simulated.status_code == 200, simulated.text
    assert simulated.json()["device_id"] == f"sim-{world_id[1:]}-01"
    assert simulated.json()["done"] is True
    state = client.get(f"/api/coldchain/containers/{container_id}", params={"plan_id": plan_id})
    assert state.status_code == 200, state.text
    assert state.json()["reading_count"] > 0
    assert client.put("/api/auth/active-org", json={"org_id": forwarder}).status_code == 200
    forwarder_state = client.get(
        f"/api/coldchain/containers/{container_id}", params={"plan_id": plan_id}
    )
    assert forwarder_state.status_code == 200, forwarder_state.text
    assert forwarder_state.json()["reading_count"] > 0
