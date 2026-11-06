import secrets
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    REAL_PLATFORM_EMAIL,
    create_real_platform,
)
from sqlalchemy import func, select

from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence import sandbox_reset as sandbox_reset_module
from freshswitch_backend.persistence.sandbox_reset import (
    RESET_COOLDOWN,
    SandboxResetCooldown,
    reset_sandbox,
)
from freshswitch_backend.persistence.sandbox_world import (
    VisitorWorld,
    create_visitor_world,
)
from freshswitch_backend.persistence.seed import main, run_lots
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    AlertRow,
    AuthSessionRow,
    BindingRow,
    ContainerRow,
    DeviceRow,
    IdempotencyRow,
    LotRow,
    ObservationRow,
    OrderRow,
    OrganizationRow,
    PackhouseRow,
    PendingRow,
    PlanForwarderRow,
    PlanRow,
    ReadingRow,
    RunRow,
    ShipperProfileRow,
    UserRow,
)
from freshswitch_backend.quality import quality_history
from freshswitch_backend.quality.models import ObservationIn

REAL_SHIPPER_ORG = "shipper-real"
REAL_LOT = "lot-real-1"
REAL_PLAN = "plan-real-1"
DEMO_LOT = "lot-demo-keep"
REAL_DEVICES = ("real-sensor-1", "demo-real-1")
STAMP = datetime(2026, 11, 10, tzinfo=UTC)
BASE_NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


@pytest.fixture
def demo_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": False}))


def _session(user_id: str, org_id: str) -> str:
    token_hash = secrets.token_hex(32)
    now = datetime.now(UTC)
    with database.begin() as db:
        db.add(
            AuthSessionRow(
                token_hash=token_hash,
                user_id=user_id,
                active_org_id=org_id,
                expires_at=now + timedelta(hours=12),
                last_seen_at=now,
            )
        )
    return token_hash


def _visitor(world: VisitorWorld, slug: str = "freshswitch") -> str:
    return _session(world.user_id, f"{world.world_id}-{slug}")


def _real_session() -> str:
    with database.begin() as db:
        user_id = db.scalar(select(UserRow.id).where(UserRow.email == REAL_PLATFORM_EMAIL))
    return _session(user_id, "platform-real")


def _alive(token_hash: str) -> bool:
    with database.begin() as db:
        return db.get(AuthSessionRow, token_hash) is not None


def _session_reset(world: VisitorWorld, caller: str):
    return reset_sandbox(keep_token_hash=caller, cooldown=RESET_COOLDOWN, world_id=world.world_id)


@pytest.fixture
def reset_clock(monkeypatch):
    now = [BASE_NOW]
    monkeypatch.setattr(sandbox_reset_module, "clock", lambda: now[0])
    return now


def _container(record_key: str, org_id: str, plan_id: str | None) -> ContainerRow:
    return ContainerRow(
        record_key=record_key,
        org_id=org_id,
        container_id=record_key,
        plan_id=plan_id,
        stage="loading",
        carry=None,
        history=[],
        anchors=[],
    )


def _coldchain(db, prefix: str, org_id: str, plan_id: str | None, device_id: str) -> None:
    db.add(_container(f"{prefix}-c", org_id, plan_id))
    db.flush()
    db.add(ReadingRow(record_key=f"{prefix}-c", device_id=device_id, ts=STAMP, payload={}))
    db.add(
        AlertRow(
            seq=db.scalar(select(func.nextval("coldchain_alert_seq"))),
            org_id=org_id,
            record_key=f"{prefix}-c",
            container_id=f"{prefix}-c",
            plan_id=plan_id,
            key=f"{prefix}-alert",
            created_at=STAMP,
            payload={},
        )
    )
    db.add(
        BindingRow(
            device_id=device_id,
            org_id=org_id,
            container_id=f"{prefix}-c",
            plan_id=plan_id,
            payload={},
        )
    )
    db.add(
        RunRow(
            run_id=f"{prefix}-run",
            org_id=org_id,
            container_id=f"{prefix}-c",
            done=True,
            payload={},
        )
    )


def _real_world() -> None:
    create_real_platform()
    with database.begin() as db:
        db.add(OrganizationRow(id=REAL_SHIPPER_ORG, type="shipper", name="실제 화주"))
        db.flush()
        db.add(
            PackhouseRow(
                id="ph-real",
                org_id=REAL_SHIPPER_ORG,
                name="실제 선과장",
                region="x",
                lat=35,
                lon=128,
            )
        )
        db.flush()
        db.add(
            ShipperProfileRow(
                id="sh-real", org_id=REAL_SHIPPER_ORG, name="실제 화주", packhouse_id="ph-real"
            )
        )
        db.add(
            LotRow(
                id=REAL_LOT,
                scenario_id="default",
                shipper_id="sh-real",
                shipper_org_id=REAL_SHIPPER_ORG,
                product_id="strawberry",
                pallets=3,
                ready_date=date(2026, 11, 16),
                destination="HKHKG",
                sell_by_date=date(2026, 11, 23),
                source="등록",
                status="planned",
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        db.add(
            PlanRow(
                plan_id=REAL_PLAN,
                org_id="platform-real",
                scenario_id="default",
                version=1,
                status="planned",
                lot_count=1,
                totals={},
                created_at=STAMP,
                updated_at=STAMP,
                payload={"registered_lot_ids": [REAL_LOT]},
                committed={},
            )
        )
        for device_id in REAL_DEVICES:
            db.add(
                DeviceRow(device_id=device_id, org_id="platform-real", token_hash=f"h-{device_id}")
            )
        db.add(
            LotRow(
                id=DEMO_LOT,
                scenario_id="default",
                shipper_id="sh-jinju",
                shipper_org_id=DEMO_ORG,
                product_id="strawberry",
                pallets=2,
                ready_date=date(2026, 11, 16),
                destination="HKHKG",
                sell_by_date=date(2026, 11, 23),
                source="등록",
                status="registered",
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        db.flush()
        _coldchain(db, "real", "platform-real", REAL_PLAN, "real-sensor-1")
        db.add(_container("demo-org-c", DEMO_ORG, None))
    quality_history.add(
        ObservationIn(product_id="strawberry", pallets=2, actual_loss_pct=4.0), scope="live"
    )
    quality_history.add(
        ObservationIn(product_id="strawberry", pallets=2, actual_loss_pct=5.0), scope="demo"
    )


def _real_snapshot() -> dict:
    with database.begin() as db:
        return {
            "lot": db.get(LotRow, REAL_LOT).status,
            "plan": db.get(PlanRow, REAL_PLAN).version,
            "devices": sorted(
                db.scalars(select(DeviceRow.device_id).where(DeviceRow.org_id == "platform-real"))
            ),
            "containers": db.scalar(
                select(func.count())
                .select_from(ContainerRow)
                .where(ContainerRow.org_id == "platform-real")
            ),
            "readings": db.scalar(
                select(func.count())
                .select_from(ReadingRow)
                .where(ReadingRow.record_key == "real-c")
            ),
            "alerts": db.scalar(
                select(func.count()).select_from(AlertRow).where(AlertRow.org_id == "platform-real")
            ),
            "bindings": db.scalar(
                select(func.count())
                .select_from(BindingRow)
                .where(BindingRow.org_id == "platform-real")
            ),
            "runs": db.scalar(
                select(func.count()).select_from(RunRow).where(RunRow.org_id == "platform-real")
            ),
            "live_obs": db.scalar(
                select(func.count())
                .select_from(ObservationRow)
                .where(ObservationRow.scope == "live")
            ),
            "demo_plans": db.scalar(
                select(func.count()).select_from(PlanRow).where(PlanRow.org_id == DEMO_ORG)
            ),
            "demo_lot": db.get(LotRow, DEMO_LOT).status,
            "demo_containers": db.scalar(
                select(func.count())
                .select_from(ContainerRow)
                .where(ContainerRow.org_id == DEMO_ORG)
            ),
            "demo_obs": sorted(
                db.scalars(select(ObservationRow.id).where(ObservationRow.scope == "demo"))
            ),
        }


def _plan_children(db, plan_id: str) -> None:
    db.add(PendingRow(replan_id=f"{plan_id}-r", plan_id=plan_id, base_version=1, payload={}))
    db.add(
        OrderRow(
            order_id=f"{plan_id}-o",
            plan_id=plan_id,
            status="requested",
            created_at=STAMP,
            updated_at=STAMP,
            payload={},
        )
    )
    db.add(
        IdempotencyRow(
            plan_id=plan_id, key=f"{plan_id}-k", kind="commit", fingerprint="f", payload={}
        )
    )


def _count(db, row, real_plans) -> int:
    return db.scalar(select(func.count()).select_from(row).where(~row.plan_id.in_(real_plans)))


def _sandbox_activity(world: VisitorWorld) -> tuple[str, str, str]:
    world_id = world.world_id
    platform_org = f"{world_id}-freshswitch"
    forwarder = f"{world_id}-{FORWARDER_ORG_ID}"
    plan_id = f"plan-{world_id}-sbx"
    device_id = "demo-sensor-9"
    planned = [lot_id for lot_id, *_ in _lots_for(world_id)]
    _late_plan(plan_id, planned, forwarder)
    with database.begin() as db:
        db.add(PlanForwarderRow(plan_id=plan_id, forwarder_org_id=forwarder))
        db.add(DeviceRow(device_id=device_id, org_id=platform_org, token_hash=f"h-{plan_id}"))
        for lot_id in planned:
            db.get(LotRow, lot_id).status = "planned"
        db.add(
            LotRow(
                id=f"lot-{world_id}-sbx",
                scenario_id="default",
                shipper_id=f"{world_id}-sh-jinju",
                shipper_org_id=f"{world_id}-shipper-jinju",
                product_id="strawberry",
                pallets=2,
                ready_date=date(2026, 11, 16),
                destination="HKHKG",
                sell_by_date=date(2026, 11, 23),
                source="등록",
                status="registered",
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        db.flush()
        _plan_children(db, plan_id)
        _coldchain(db, "sbx", platform_org, plan_id, device_id)
        db.add(
            ObservationRow(
                id="sbx-manual",
                scope="sandbox",
                world_id=world_id,
                origin="manual",
                product_id="strawberry",
                recorded_at=STAMP,
                payload={},
            )
        )
    return plan_id, device_id, world_id


def _lots_for(world_id: str) -> list:
    prefix = f"{world_id}-"
    return [row for row in _sandbox_snapshot()["lots"] if row[1].startswith(prefix)]


def _lots_except(world_id: str) -> list:
    prefix = f"{world_id}-"
    return [row for row in _sandbox_snapshot()["lots"] if not row[1].startswith(prefix)]


def _world_observations(world_id: str) -> list[str]:
    with database.begin() as db:
        return sorted(
            db.scalars(select(ObservationRow.id).where(ObservationRow.world_id == world_id))
        )


def _sandbox_snapshot() -> dict:
    with database.begin() as db:
        sandbox_orgs = select(OrganizationRow.id).where(OrganizationRow.sandbox.is_(True))
        real_plans = select(PlanRow.plan_id).where(~PlanRow.org_id.in_(sandbox_orgs))
        return {
            "orgs": sorted(db.scalars(sandbox_orgs)),
            "lots": sorted(
                db.execute(
                    select(
                        LotRow.id,
                        LotRow.shipper_org_id,
                        LotRow.status,
                        LotRow.pallets,
                        LotRow.scenario_id,
                    ).where(LotRow.shipper_org_id.in_(sandbox_orgs))
                ).all()
            ),
            "plans": db.scalar(
                select(func.count()).select_from(PlanRow).where(PlanRow.org_id.in_(sandbox_orgs))
            ),
            "pending": _count(db, PendingRow, real_plans),
            "orders": _count(db, OrderRow, real_plans),
            "idempotency": _count(db, IdempotencyRow, real_plans),
            "forwarders": db.scalar(
                select(func.count())
                .select_from(PlanForwarderRow)
                .where(PlanForwarderRow.forwarder_org_id.in_(sandbox_orgs))
            ),
            "devices": sorted(
                db.scalars(select(DeviceRow.device_id).where(DeviceRow.org_id.in_(sandbox_orgs)))
            ),
            "containers": db.scalar(
                select(func.count())
                .select_from(ContainerRow)
                .where(ContainerRow.org_id.in_(sandbox_orgs))
            ),
            "readings": db.scalar(
                select(func.count()).select_from(ReadingRow).where(ReadingRow.record_key == "sbx-c")
            ),
            "alerts": db.scalar(
                select(func.count()).select_from(AlertRow).where(AlertRow.org_id.in_(sandbox_orgs))
            ),
            "bindings": db.scalar(
                select(func.count())
                .select_from(BindingRow)
                .where(BindingRow.org_id.in_(sandbox_orgs))
            ),
            "runs": db.scalar(
                select(func.count()).select_from(RunRow).where(RunRow.org_id.in_(sandbox_orgs))
            ),
            "observations": sorted(
                db.scalars(select(ObservationRow.id).where(ObservationRow.scope == "sandbox"))
            ),
        }


def test_admin_reset_restores_baseline_and_keeps_real_data(
    demo_app, client, reset_admin_headers, reset_clock
) -> None:
    run_lots()
    seed_lots = _sandbox_snapshot()["lots"]
    assert len(seed_lots) == 6
    _real_world()
    assert client.post("/api/plans", json={"scenario_id": "default"}).status_code == 201
    real_session = _real_session()
    world = create_visitor_world()
    platform = _visitor(world)
    shipper = _visitor(world, "shipper-jinju")
    world_id = world.world_id
    visitor_before = _lots_for(world_id)
    rest_before = _lots_except(world_id)
    observations_before = _world_observations(world_id)
    assert len(visitor_before) == 6
    assert rest_before == seed_lots
    assert len(observations_before) == 6
    assert all(status == "registered" for _, _, status, _, _ in visitor_before)
    _, device_id, activity_world = _sandbox_activity(world)
    assert activity_world == world_id
    real_before = _real_snapshot()
    dirty = _sandbox_snapshot()
    assert {status for _, _, status, _, _ in _lots_for(world_id)} == {"planned", "registered"}
    assert dirty["plans"] == 1 and dirty["forwarders"] == 1
    assert dirty["pending"] == dirty["orders"] == dirty["idempotency"] == 1
    assert device_id in dirty["devices"]
    assert all(d == device_id or d.startswith("RCM-") for d in dirty["devices"])
    assert len(dirty["lots"]) == 13
    assert "sbx-manual" in dirty["observations"]

    response = TestClient(demo_app, base_url="https://testserver").post(
        "/api/demo/reset",
        params={"scope": "sandbox", "world_id": world_id},
        headers=reset_admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"scope", "orgs", "reset_at", "world_id"}
    assert body["scope"] == "sandbox"
    assert body["orgs"] == 8
    assert body["world_id"] == world_id
    assert datetime.fromisoformat(body["reset_at"]) == BASE_NOW

    after = _sandbox_snapshot()
    assert _lots_for(world_id) == visitor_before
    assert _lots_except(world_id) == rest_before
    assert _world_observations(world_id) == observations_before
    wiped = ("plans", "forwarders", "pending", "orders", "idempotency", "containers")
    for key in (*wiped, "readings", "alerts", "bindings", "runs"):
        assert after[key] == 0, key
    assert after["devices"] == []
    assert "sbx-manual" not in after["observations"]
    assert _real_snapshot() == real_before
    assert _alive(real_session)
    assert not _alive(platform)
    assert not _alive(shipper)

    reset_clock[0] = BASE_NOW + timedelta(seconds=5)
    again = client.post(
        "/api/demo/reset",
        params={"scope": "sandbox", "world_id": world_id},
        headers=reset_admin_headers,
    )
    assert again.status_code == 200
    assert _sandbox_snapshot() == after
    assert _real_snapshot() == real_before


def test_session_reset_keeps_caller_and_logs_out_other_demo_users(
    demo_tenants, reset_clock
) -> None:
    run_lots()
    world = create_visitor_world()
    platform = _visitor(world)
    other_platform = _visitor(world)
    shipper = _visitor(world, "shipper-jinju")
    _sandbox_activity(world)

    result = _session_reset(world, platform)
    assert result.orgs == 8
    assert result.world_id == world.world_id
    assert _alive(platform)
    assert not _alive(other_platform)
    assert not _alive(shipper)
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(AuthSessionRow)) == 1
    lots = _lots_for(world.world_id)
    assert len(lots) == 6
    assert {status for _, _, status, _, _ in lots} == {"registered"}
    assert _sandbox_snapshot()["plans"] == 0


def test_session_reset_cooldown(demo_tenants, reset_clock) -> None:
    world = create_visitor_world()
    platform = _visitor(world)
    _session_reset(world, platform)

    reset_clock[0] = BASE_NOW + timedelta(seconds=20)
    with pytest.raises(SandboxResetCooldown) as blocked:
        _session_reset(world, platform)
    assert blocked.value.retry_after == 40

    reset_clock[0] = BASE_NOW + timedelta(seconds=60)
    assert _session_reset(world, platform).world_id == world.world_id


def test_admin_reset_ignores_cooldown_but_starts_it(
    demo_app, client, reset_admin_headers, reset_clock
) -> None:
    world = create_visitor_world()
    platform = _visitor(world)
    params = {"scope": "sandbox", "world_id": world.world_id}
    for _ in range(2):
        response = client.post("/api/demo/reset", params=params, headers=reset_admin_headers)
        assert response.status_code == 200, response.text
    assert not _alive(platform)
    platform = _visitor(world)
    with pytest.raises(SandboxResetCooldown):
        _session_reset(world, platform)


def test_admin_sandbox_reset_needs_token(demo_app, client, reset_admin_headers) -> None:
    demo = TestClient(demo_app, base_url="https://testserver")
    assert demo.post("/api/demo/reset", params={"scope": "sandbox"}).status_code == 401
    assert (
        demo.post(
            "/api/demo/reset", params={"scope": "sandbox"}, headers={"Authorization": "Bearer x"}
        ).status_code
        == 401
    )
    assert (
        client.post("/api/demo/reset", params={"scope": "world"}, headers=reset_admin_headers)
    ).status_code == 422


def test_demo_scope_reset_unchanged(client, reset_admin_headers, demo_tenants) -> None:
    run_lots()
    assert client.post("/api/plans", json={"scenario_id": "default"}).status_code == 201
    body = client.post("/api/demo/reset", headers=reset_admin_headers).json()
    assert set(body) == {
        "cleared_sessions",
        "cleared_coldchain_containers",
        "cleared_quality_observations",
        "cleared_lots",
    }
    assert body["cleared_sessions"] == 1
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(LotRow)) == 6


def test_reset_cli(demo_tenants, capsys, reset_clock) -> None:
    with pytest.raises(SystemExit) as bare:
        main(["reset-sandbox"])
    assert bare.value.code == 2
    main(["reset-sandbox", "--world", "seed"])
    printed = capsys.readouterr().out.split()
    assert printed[:2] == ["scope=sandbox", "orgs=8"]
    assert printed[2] == f"reset_at={BASE_NOW.isoformat()}"
    main(["reset-sandbox", "--world", "seed"])
    capsys.readouterr()
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(LotRow)) == 6


def test_reset_seeds_fresh_database(reset_clock) -> None:
    result = sandbox_reset_module.reset_sandbox()
    assert result.orgs == 8
    assert result.reset_at == BASE_NOW
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(LotRow)) == 6


def _late_plan(plan_id: str, lot_ids: list[str], org_id: str) -> None:
    with database.begin() as db:
        db.add(
            PlanRow(
                plan_id=plan_id,
                org_id=org_id,
                scenario_id="default",
                version=1,
                status="planned",
                lot_count=len(lot_ids),
                totals={},
                created_at=STAMP,
                updated_at=STAMP,
                payload={"registered_lot_ids": lot_ids},
                committed={},
            )
        )


def test_reset_logs_out_first_and_drops_plans_written_mid_reset(
    demo_tenants, monkeypatch, reset_clock
) -> None:
    from freshswitch_backend.api.plans import CreatePlanRequest, create_tenant_plan
    from freshswitch_backend.persistence import sandbox_world

    world = create_visitor_world()
    platform = _visitor(world)
    other = _visitor(world)
    org_id = f"{world.world_id}-freshswitch"
    seen: list[bool] = []
    real_erase = sandbox_world.erase_world

    def erase(db, world_id: str, org_ids: list[str], *, keep_structure: bool) -> None:
        if not seen:
            seen.append(_alive(other))
            _late_plan("plan-mid-early", [], org_id)
            real_erase(db, world_id, org_ids, keep_structure=keep_structure)
            _late_plan("plan-mid-late", [], org_id)
            return
        real_erase(db, world_id, org_ids, keep_structure=keep_structure)

    monkeypatch.setattr(sandbox_world, "erase_world", erase)
    _session_reset(world, platform)
    assert seen == [False]
    assert _alive(platform)
    with database.begin() as db:
        assert db.get(PlanRow, "plan-mid-early") is None
        assert db.get(PlanRow, "plan-mid-late") is None
    after = _sandbox_snapshot()
    assert after["plans"] == 0
    assert len(after["lots"]) == 6
    assert all(status == "registered" for _, _, status, _, _ in after["lots"])
    created = create_tenant_plan(
        CreatePlanRequest(include_registered=True), f"{world.world_id}-{FORWARDER_ORG_ID}"
    )
    with database.begin() as db:
        assert db.get(PlanRow, created.plan_id).lot_count == 6


def test_failed_reset_does_not_start_cooldown(demo_tenants, monkeypatch, reset_clock) -> None:
    world = create_visitor_world()
    platform = _visitor(world)

    def broken(*args, **kwargs) -> int:
        raise RuntimeError("seed failed")

    monkeypatch.setattr("freshswitch_backend.persistence.sandbox_world.reseed_world", broken)
    with pytest.raises(RuntimeError):
        _session_reset(world, platform)
    monkeypatch.undo()
    monkeypatch.setattr(sandbox_reset_module, "clock", lambda: BASE_NOW)
    assert _session_reset(world, platform).world_id == world.world_id
    with pytest.raises(SandboxResetCooldown):
        _session_reset(world, platform)


def test_reset_with_fuel_shock_scenario(demo_tenants, reset_clock) -> None:
    run_lots()
    main(["reset-sandbox", "--world", "seed", "--scenario", "fuel-shock"])
    lots = _sandbox_snapshot()["lots"]
    assert len(lots) == 8
    assert {scenario for *_, scenario in lots} == {"fuel-shock"}
