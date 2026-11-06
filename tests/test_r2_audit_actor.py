import itertools
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from fastapi import Request
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    SAAS_CSRF,
    SHIPPER_ORG_ID,
    accept_plan,
    login_client,
    open_quantity_window,
    queue_quantity,
)
from sqlalchemy import select, text
from test_saas_api import _forwarder_plan, _lot_body, _scenario

from freshswitch_backend.api import auth
from freshswitch_backend.api.auth import RateRule, SlidingWindowLimiter
from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database, set_meta
from freshswitch_backend.persistence.database import migration_config
from freshswitch_backend.persistence.tables import (
    AuthSessionRow,
    MembershipRow,
    ObservationRow,
    OrganizationRow,
)

REDUCE = {"type": "reduce", "lot_id": "lot-jinju-sb-1", "new_pallets": 2}
PROXY_PEER = ("127.0.0.1", 50000)
SIGNUPS = itertools.count()


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _apply(planner: TestClient, plan_id: str, event: dict) -> dict:
    r = planner.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert r.status_code == 200, r.text
    alt = next(a["id"] for a in r.json()["alternatives"] if a["feasible"])
    body = {"alternative_id": alt, "replan_id": r.json()["replan_id"]}
    applied = planner.post(f"/api/plans/{plan_id}/apply", json=body)
    assert applied.status_code == 200, applied.text
    return applied.json()


def test_platform_cannot_confirm_for_shipper_or_forwarder(
    platform_client, saas_login, forwarder_client
) -> None:
    pid = _forwarder_plan(saas_login)["plan_id"]
    accept_plan(saas_login, pid)
    open_quantity_window()
    queue_quantity(saas_login(SHIPPER_ORG_ID), pid, REDUCE["lot_id"], REDUCE["new_pallets"])
    oid = _apply(forwarder_client, pid, REDUCE)["active_order_id"]
    path = f"/api/plans/{pid}/orders/{oid}/confirm"
    order = platform_client.get(f"/api/plans/{pid}/orders/{oid}").json()
    sids = [c["shipper_id"] for c in order["shipper_confirmations"]]
    assert sids

    for sid in sids:
        body = {"actor": "shipper", "shipper_id": sid, "accept": True}
        assert platform_client.post(path, json=body).status_code == 403
    forwarder_body = {"actor": "forwarder", "accept": True}
    assert platform_client.post(path, json=forwarder_body).status_code == 403
    unchanged = platform_client.get(f"/api/plans/{pid}/orders/{oid}").json()
    assert unchanged["status"] == order["status"]
    assert {c["status"] for c in unchanged["shipper_confirmations"]} == {"pending"}

    assert forwarder_client.post(path, json=forwarder_body).status_code == 403
    shipper = saas_login(SHIPPER_ORG_ID)
    shipper_body = {"actor": "shipper", "shipper_id": "sh-jinju", "accept": True}
    assert shipper.post(path, json=shipper_body).status_code in (403, 404)

    for sid in sids:
        own = saas_login("shipper-" + sid.removeprefix("sh-"))
        confirmed = own.post(
            f"/api/saas/shipper/plans/{pid}/orders/{oid}/confirm", json={"accept": True}
        )
        assert confirmed.status_code == 200, confirmed.text

    saas_path = f"/api/saas/forwarder/plans/{pid}/orders/{oid}/confirm"
    outsider = saas_login("forwarder-sinhang")
    assert outsider.post(saas_path, json={"accept": True}).status_code == 404
    done = forwarder_client.post(saas_path, json={"accept": True})
    assert done.status_code == 200, done.text
    assert done.json()["status"] in ("forwarder_confirmed", "executed")


def test_anonymous_demo_confirm_still_works(client: TestClient) -> None:
    created = client.post("/api/plans", json={"scenario_id": "default"})
    assert created.status_code == 201, created.text
    pid = created.json()["plan_id"]
    oid = _apply(client, pid, REDUCE)["active_order_id"]
    path = f"/api/plans/{pid}/orders/{oid}/confirm"
    order = client.get(f"/api/plans/{pid}/orders/{oid}").json()
    for c in order["shipper_confirmations"]:
        body = {"actor": "shipper", "shipper_id": c["shipper_id"], "accept": True}
        assert client.post(path, json=body).status_code == 200
    done = client.post(path, json={"actor": "forwarder", "accept": True})
    assert done.status_code == 200, done.text
    assert done.json()["status"] in ("forwarder_confirmed", "executed")


def test_simulator_is_sandbox_only(saas_app) -> None:
    real = TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF)
    signed = real.post(
        "/api/auth/signup",
        json={
            "email": "simulator-forwarder@example.com",
            "password": "simulator-forwarder-pass",
            "name": "실제 포워더",
            "org_name": "실제 포워더",
            "org_type": "forwarder",
        },
    )
    assert signed.status_code == 201, signed.text
    body = {"device_id": "sim-x", "reset": True, "preset": "normal"}
    blocked = real.post("/api/coldchain/simulate", json=body)
    assert blocked.status_code == 403
    assert "sandbox" in blocked.json()["detail"]

    sandbox = login_client(FORWARDER_ORG_ID, saas_app)
    device = sandbox.post("/api/saas/devices", json={"device_id": "demo-sim-1"})
    assert device.status_code == 201, device.text
    allowed = sandbox.post(
        "/api/coldchain/simulate", json={"preset": "normal", "device_id": "demo-sim-1"}
    )
    assert allowed.status_code == 200, allowed.text


def test_anonymous_demo_simulator_unchanged(client: TestClient) -> None:
    run = client.post("/api/coldchain/simulate", json={"preset": "normal"})
    assert run.status_code == 200, run.text


def _small_limiter(app, rules: dict[str, RateRule]) -> FakeClock:
    clock = FakeClock()
    app.state.auth_rate_limiter = SlidingWindowLimiter(rules, clock=clock)
    return clock


def _demo_login(app, ip: str | None = None) -> int:
    headers = {**SAAS_CSRF, **({"X-Forwarded-For": f"10.9.9.9, {ip}"} if ip else {})}
    client = TestClient(app, base_url="https://testserver", headers=headers, client=PROXY_PEER)
    body = {"org_type": "platform", "org_id": PLATFORM_ORG_ID}
    return client.post("/api/auth/demo-login", json=body).status_code


def _signup_from(app, ip: str | None = None, peer=PROXY_PEER) -> int:
    forwarded = {"X-Forwarded-For": f"10.9.9.9, {ip}"} if ip else {}
    client = TestClient(
        app, base_url="https://testserver", headers={**SAAS_CSRF, **forwarded}, client=peer
    )
    n = next(SIGNUPS)
    body = {
        "email": f"limited-{n}@example.com",
        "password": "rate-limit-pass",
        "name": "속도 제한",
        "org_name": f"제한 업체 {n}",
        "org_type": "shipper",
    }
    return client.post("/api/auth/signup", json=body).status_code


def test_auth_rate_limit_is_per_client(saas_app) -> None:
    rule = RateRule(per_client=2, window_seconds=60)
    clock = _small_limiter(saas_app, {"demo-login": rule, "signup": rule})
    assert [_signup_from(saas_app, "1.1.1.1") for _ in range(3)] == [201, 201, 429]
    client = TestClient(
        saas_app, base_url="https://testserver", headers=SAAS_CSRF, client=PROXY_PEER
    )
    limited = client.post(
        "/api/auth/signup",
        json={
            "email": "limited-direct@example.com",
            "password": "rate-limit-pass",
            "name": "속도 제한",
            "org_name": "제한 업체",
            "org_type": "shipper",
        },
        headers={"X-Forwarded-For": "1.1.1.1"},
    )
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert _signup_from(saas_app, "2.2.2.2") == 201
    clock.now += 61
    assert _signup_from(saas_app, "1.1.1.1") == 201


def test_full_clients_do_not_block_other_clients(saas_app) -> None:
    rule = RateRule(per_client=1, window_seconds=60)
    _small_limiter(saas_app, {"demo-login": rule, "signup": rule})
    first = [_signup_from(saas_app, f"3.3.3.{n}") for n in range(20)]
    again = [_signup_from(saas_app, f"3.3.3.{n}") for n in range(20)]
    assert first == [201] * 20
    assert again == [429] * 20
    assert _signup_from(saas_app, "3.3.4.1") == 201


def test_forwarded_headers_are_ignored_from_untrusted_peers(saas_app) -> None:
    rule = RateRule(per_client=1, window_seconds=60)
    _small_limiter(saas_app, {"demo-login": rule, "signup": rule})
    codes = []
    for n in range(2):
        client = TestClient(
            saas_app,
            base_url="https://testserver",
            headers={**SAAS_CSRF, "X-Forwarded-For": f"6.6.6.{n}", "X-Real-IP": f"7.7.7.{n}"},
            client=("203.0.113.9", 40000),
        )
        body = {
            "email": f"untrusted-{n}@example.com",
            "password": "rate-limit-pass",
            "name": "속도 제한",
            "org_name": f"제한 업체 {n}",
            "org_type": "shipper",
        }
        codes.append(client.post("/api/auth/signup", json=body).status_code)
    assert codes == [201, 429]


def test_client_address_trusts_configured_proxies_only(monkeypatch) -> None:
    def request(peer: str, headers: dict[str, str]):
        scope = {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 1),
        }
        return Request(scope)

    forwarded = {"X-Forwarded-For": "6.6.6.6, 8.8.8.8"}
    assert auth.client_address(request("127.0.0.1", forwarded)) == "8.8.8.8"
    assert auth.client_address(request("172.18.0.1", {"X-Real-IP": "9.9.9.9"})) == "9.9.9.9"
    assert auth.client_address(request("192.168.240.1", forwarded)) == "8.8.8.8"
    assert auth.client_address(request("10.0.3.1", forwarded)) == "8.8.8.8"
    assert auth.client_address(request("203.0.113.9", forwarded)) == "203.0.113.9"
    monkeypatch.setenv(auth.TRUSTED_PROXIES_ENV, "203.0.113.0/24")
    assert auth.client_address(request("203.0.113.9", forwarded)) == "8.8.8.8"
    assert auth.client_address(request("127.0.0.1", forwarded)) == "127.0.0.1"


def _signup(app, n: int, ip: str = "4.4.4.4") -> int:
    client = TestClient(
        app,
        base_url="https://testserver",
        headers={**SAAS_CSRF, "X-Forwarded-For": ip},
        client=PROXY_PEER,
    )
    body = {
        "email": f"rate-{n}@example.com",
        "password": "rate-limit-pass",
        "name": "속도 제한",
        "org_name": f"속도 제한 {n}",
        "org_type": "shipper",
    }
    return client.post("/api/auth/signup", json=body).status_code


def test_signup_is_rate_limited(saas_app) -> None:
    rule = RateRule(per_client=2, window_seconds=600)
    _small_limiter(saas_app, {"demo-login": rule, "signup": rule})
    assert [_signup(saas_app, n) for n in range(3)] == [201, 201, 429]
    assert _signup(saas_app, 3, ip="5.5.5.5") == 201


def test_default_rules_are_installed_lazily(saas_app) -> None:
    assert _signup_from(saas_app) == 201
    limiter = saas_app.state.auth_rate_limiter
    assert isinstance(limiter, SlidingWindowLimiter)
    assert auth.AUTH_RATE_RULES["signup"].per_client >= 30


def _add_session(user_id: str, expires_at: datetime) -> str:
    token_hash = secrets.token_hex(32)
    with database.begin() as db:
        db.add(AuthSessionRow(token_hash=token_hash, user_id=user_id, expires_at=expires_at))
    return token_hash


def test_purge_expired_sessions(saas_app, capsys) -> None:
    assert _signup_from(saas_app) == 201
    with database.begin() as db:
        user_id = db.scalar(select(AuthSessionRow.user_id))
    now = datetime.now(UTC)
    expired = [_add_session(user_id, now - timedelta(minutes=n + 1)) for n in range(3)]
    alive = _add_session(user_id, now + timedelta(days=1))

    assert auth.purge_expired_sessions() == 3
    with database.begin() as db:
        left = set(db.scalars(select(AuthSessionRow.token_hash)))
    assert alive in left
    assert not left & set(expired)

    _add_session(user_id, now - timedelta(minutes=5))
    auth.main(["purge-sessions"])
    assert "purged=1" in capsys.readouterr().out


def test_org_header_does_not_change_stored_active_org(saas_app) -> None:
    client = TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF)
    signed = client.post(
        "/api/auth/signup",
        json={
            "email": "multi-org@example.com",
            "password": "multi-org-pass",
            "name": "여러 조직",
            "org_name": "기본 화주",
            "org_type": "shipper",
        },
    )
    assert signed.status_code == 201, signed.text
    home = signed.json()["active_org"]["org_id"]
    user_id = signed.json()["user"]["id"]
    with database.begin() as db:
        db.add(OrganizationRow(id="forwarder-extra", type="forwarder", name="추가 포워더"))
        db.flush()
        db.add(MembershipRow(user_id=user_id, org_id="forwarder-extra", role="member"))

    switched = client.get("/api/me", headers={"X-Org-Id": "forwarder-extra"})
    assert switched.status_code == 200
    assert switched.json()["active_org"]["org_id"] == "forwarder-extra"
    assert client.get("/api/me").json()["active_org"]["org_id"] == home
    with database.begin() as db:
        stored = db.scalars(
            select(AuthSessionRow.active_org_id).where(AuthSessionRow.user_id == user_id)
        ).all()
    assert stored == [home]

    bad = client.put("/api/auth/active-org", json={"org_id": "shipper-not-mine"})
    assert bad.status_code == 403
    moved = client.put("/api/auth/active-org", json={"org_id": "forwarder-extra"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["active_org"]["org_id"] == "forwarder-extra"
    assert client.get("/api/me").json()["active_org"]["org_id"] == "forwarder-extra"
    with database.begin() as db:
        stored = db.scalars(
            select(AuthSessionRow.active_org_id).where(AuthSessionRow.user_id == user_id)
        ).all()
    assert stored == ["forwarder-extra"]


def test_active_org_switch_requires_login(saas_app) -> None:
    client = TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF)
    response = client.put("/api/auth/active-org", json={"org_id": PLATFORM_ORG_ID})
    assert response.status_code == 401


def test_0004_downgrade_drops_non_demo_observations() -> None:
    from freshswitch_backend.persistence.sandbox_world import ensure_seed_world

    with database.begin() as db:
        ensure_seed_world(db)
        for scope in ("demo", "sandbox", "live"):
            db.add(
                ObservationRow(
                    id=f"obs-{scope}",
                    scope=scope,
                    world_id="seed" if scope == "sandbox" else None,
                    origin="inspection",
                    product_id="strawberry",
                    recorded_at=datetime.now(UTC),
                    payload={},
                )
            )
        set_meta(db, "quality_seeded", True)
        set_meta(db, "quality_seeded:sandbox", True)
        set_meta(db, "quality_seeded:live", True)
    revision = ScriptDirectory.from_config(migration_config()).get_revision("0004")
    assert revision is not None
    with database.engine.connect() as conn:
        trans = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                revision.module.downgrade()
            ids = conn.execute(text("SELECT id FROM quality_observations")).scalars().all()
            keys = conn.execute(text("SELECT key FROM app_meta")).scalars().all()
        finally:
            trans.rollback()
    assert ids == ["obs-demo"]
    assert "quality_seeded" in keys
    assert not {"quality_seeded:sandbox", "quality_seeded:live"} & set(keys)


def test_explicit_sell_by_in_patch_is_kept(saas_login) -> None:
    jinju = saas_login(SHIPPER_ORG_ID)
    lot = next(item for item in _scenario().lots if item.shipper_id == "sh-jinju")
    created = jinju.post("/api/saas/shipper/lots", json=_lot_body(lot))
    assert created.status_code == 201, created.text
    lot_id = created.json()["registered"]["id"]
    ready = lot.ready_date + timedelta(days=1)
    sell_by = ready + timedelta(days=2)

    response = jinju.patch(
        f"/api/saas/shipper/lots/{lot_id}",
        json={"ready_date": ready.isoformat(), "sell_by_date": sell_by.isoformat()},
    )

    assert response.status_code == 200, response.text
    assert response.json()["ready_date"] == ready.isoformat()
    assert response.json()["sell_by_date"] == sell_by.isoformat()
    again = jinju.patch(
        f"/api/saas/shipper/lots/{lot_id}",
        json={"product": lot.product_id, "sell_by_date": sell_by.isoformat()},
    )
    assert again.status_code == 200, again.text
    assert again.json()["sell_by_date"] == sell_by.isoformat()
