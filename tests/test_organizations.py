import uuid
from collections.abc import Iterator

import psycopg
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.database import migration_config, normalize_url
from freshswitch_backend.persistence.orgs import ensure_demo_org
from freshswitch_backend.persistence.passwords import hash_password, verify_password
from freshswitch_backend.persistence.seed import demo_orgs, run, shipper_org_id
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    LotRow,
    MembershipRow,
    OrganizationRow,
    PackhouseRow,
    PlanRow,
    ShipperProfileRow,
    UserRow,
)

ORG_SCOPED = {
    "plan_sessions": "org_id",
    "lots": "shipper_org_id",
    "coldchain_containers": "org_id",
    "coldchain_alerts": "org_id",
    "coldchain_bindings": "org_id",
    "coldchain_sim_runs": "org_id",
}
NEW_TABLES = {
    "organizations",
    "users",
    "memberships",
    "auth_sessions",
    "packhouses",
    "shipper_profiles",
    "plan_forwarders",
    "devices",
}


@pytest.fixture
def scratch_engine(postgres: str) -> Iterator[Engine]:
    name = f"migtest_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(postgres, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{name}"')
    except psycopg.Error as exc:
        admin.close()
        pytest.skip(f"임시 데이터베이스를 만들 수 없습니다: {exc}")
    base, _, _ = normalize_url(postgres).rpartition("/")
    engine = create_engine(f"{base}/{name}")
    try:
        yield engine
    finally:
        engine.dispose()
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _migrate(engine: Engine, action: str, revision: str) -> None:
    config = migration_config()
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, action)(config, revision)


def _columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {c["name"] for c in inspect(conn).get_columns(table)}


def test_0002_upgrade_backfills_demo_and_downgrades(scratch_engine: Engine) -> None:
    _migrate(scratch_engine, "upgrade", "0001")
    with scratch_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO plan_sessions (plan_id, scenario_id, version, status, lot_count,"
                " totals, created_at, updated_at, payload, committed) VALUES"
                " ('p-old', 'default', 1, 'draft', 0, '{}', now(), now(), '{}', '{}')"
            )
        )

    _migrate(scratch_engine, "upgrade", "head")
    with scratch_engine.connect() as conn:
        assert NEW_TABLES <= set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT org_id FROM plan_sessions")) == DEMO_ORG
        assert conn.scalar(text("SELECT type FROM organizations WHERE id = 'demo'")) == "demo"
    for table, column in ORG_SCOPED.items():
        assert column in _columns(scratch_engine, table)

    _migrate(scratch_engine, "downgrade", "0001")
    with scratch_engine.connect() as conn:
        assert not NEW_TABLES & set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT plan_id FROM plan_sessions")) == "p-old"
    for table, column in ORG_SCOPED.items():
        assert column not in _columns(scratch_engine, table)

    _migrate(scratch_engine, "upgrade", "head")
    with scratch_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM organizations")) == 1


def _org_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(OrganizationRow))


def test_ensure_demo_org_is_idempotent() -> None:
    ensure_demo_org()
    ensure_demo_org()
    with database.begin() as db:
        assert _org_count(db) == 1
        demo = db.get(OrganizationRow, DEMO_ORG)
        assert demo is not None and demo.type == "demo"


def test_existing_inserts_default_to_demo_org(client: TestClient) -> None:
    lot = client.post(
        "/api/lots",
        json={
            "product": "딸기",
            "pallets": 2,
            "ready_date": "2026-11-17",
            "shipper": "산청",
            "destination": "홍콩",
        },
    ).json()["registered"]
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    with database.begin() as db:
        assert db.get(PlanRow, plan_id).org_id == DEMO_ORG
        assert db.get(LotRow, lot["id"]).shipper_org_id == DEMO_ORG


def test_seed_is_idempotent_and_mirrors_scenario() -> None:
    first = run(password="pw-1")
    with database.begin() as db:
        hashes = dict(db.execute(select(UserRow.id, UserRow.password_hash)).all())
    second = run(password="pw-2")

    assert first == second == {"demo": 1, "platform": 1, "shipper": 5, "forwarder": 2}
    scenario = get_scenario("default")
    packhouses = {p.id: p for p in scenario.packhouses}
    with database.begin() as db:
        assert dict(db.execute(select(UserRow.id, UserRow.password_hash)).all()) == hashes
        users = db.scalars(select(UserRow)).all()
        assert len(users) == len(demo_orgs()) == 8
        assert all(u.is_demo and verify_password("pw-1", u.password_hash) for u in users)
        assert db.scalar(select(func.count()).select_from(MembershipRow)) == 8

        profiles = {p.id: p for p in db.scalars(select(ShipperProfileRow))}
        assert set(profiles) == {s.id for s in scenario.shippers}
        for shipper in scenario.shippers:
            profile = profiles[shipper.id]
            assert profile.org_id == shipper_org_id(shipper.id)
            assert profile.name == shipper.name
            assert profile.packhouse_id == shipper.packhouse_id
            ph = db.get(PackhouseRow, shipper.packhouse_id)
            expected = packhouses[shipper.packhouse_id]
            assert (ph.org_id, ph.name, ph.lat, ph.lon) == (
                profile.org_id,
                expected.name,
                expected.lat,
                expected.lon,
            )
        products = [p.product_ids for p in profiles.values()]
        assert products.count(["strawberry"]) == 3
        assert products.count(["grape_so2"]) == 2


def test_password_hash_roundtrip() -> None:
    stored = hash_password("secret")
    assert stored.startswith("scrypt$")
    assert stored != hash_password("secret")
    assert verify_password("secret", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("secret", "garbage")


def test_seed_skips_demo_email_taken_by_other_user() -> None:
    with database.begin() as db:
        db.add(
            UserRow(
                id="u1",
                email="freshswitch@demo.freshswitch.ssalhub.xyz",
                password_hash=hash_password("mine"),
                name="가입자",
            )
        )

    counts = run(password="pw")

    assert counts["platform"] == 1
    with database.begin() as db:
        assert db.get(UserRow, "user-freshswitch") is None
        assert verify_password("mine", db.get(UserRow, "u1").password_hash)
        members = set(db.scalars(select(MembershipRow.user_id)))
        assert "u1" not in members
        assert len(members) == len(demo_orgs()) - 1


def test_device_ids_are_globally_unique() -> None:
    with database.engine.connect() as conn:
        devices = inspect(conn).get_pk_constraint("devices")["constrained_columns"]
        bindings = inspect(conn).get_pk_constraint("coldchain_bindings")["constrained_columns"]
    assert devices == bindings == ["device_id"]
