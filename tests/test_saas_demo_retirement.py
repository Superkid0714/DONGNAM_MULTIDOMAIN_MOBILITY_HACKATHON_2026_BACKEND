from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from freshswitch_backend.api.auth import _token_hash
from freshswitch_backend.config import Settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.passwords import hash_password
from freshswitch_backend.persistence.tables import (
    AuthSessionRow,
    LotRow,
    MembershipRow,
    OrganizationRow,
    PlanRow,
    SandboxWorldRow,
    UserRow,
)

PREFIX = "/test/api"
CSRF = {"X-Requested-With": "freshswitch"}
PASSWORD = "existing-account-password"
RETIRED = (
    ("POST", "/auth/demo-login"),
    ("POST", "/saas/sandbox/reset"),
    ("POST", "/demo/reset?scope=sandbox&all=true"),
    ("POST", "/demo/reset"),
    ("GET", "/demo/status"),
    ("GET", "/demo/script"),
    ("POST", "/saas/platform/plans/missing/confirmation/auto-accept"),
    ("POST", "/saas/forwarder/plans/missing/confirmation/auto-accept"),
)


@pytest.fixture
def saas():
    return create_app(Settings(saas_mode=True, api_prefix=PREFIX, session_cookie_path="/test"))


def client(saas):
    return TestClient(saas, base_url="https://testserver", headers=CSRF)


def counts():
    with database.begin() as db:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (
                UserRow,
                OrganizationRow,
                MembershipRow,
                AuthSessionRow,
                SandboxWorldRow,
                LotRow,
                PlanRow,
            )
        }


def signup(http, email="customer@example.com", org_type="shipper"):
    response = http.post(
        PREFIX + "/auth/signup",
        json={
            "email": email,
            "password": PASSWORD,
            "name": "기존 계정",
            "org_name": "일반 업체",
            "org_type": org_type,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def synthetic_session(saas, *, seed=False):
    world_id = "seed" if seed else "w-retired"
    now = datetime.now(UTC)
    with database.begin() as db:
        db.add(SandboxWorldRow(id=world_id, created_at=now, expires_at=now + timedelta(hours=12)))
        db.flush()
        db.add(
            OrganizationRow(
                id="synthetic", name="과거 데모", type="platform", sandbox=True, world_id=world_id
            )
        )
        db.add(
            UserRow(
                id="synthetic",
                email="retired@demo.freshswitch.local",
                name="데모",
                password_hash=hash_password(PASSWORD),
                is_demo=True,
                world_id=world_id,
            )
        )
        db.flush()
        db.add(MembershipRow(user_id="synthetic", org_id="synthetic", role="admin"))
        db.add(
            AuthSessionRow(
                token_hash=_token_hash("retired-session"),
                user_id="synthetic",
                active_org_id="synthetic",
                expires_at=now + timedelta(hours=12),
                last_seen_at=now - timedelta(hours=1),
            )
        )
    http = client(saas)
    http.cookies.set(saas.state.settings.session_cookie_name, "retired-session", path="/test")
    return http


@pytest.mark.parametrize("authenticated", [False, True])
def test_retired_endpoints_do_not_create_reset_or_switch_any_data(saas, authenticated):
    http = client(saas)
    if authenticated:
        signup(http)
    before = counts()
    for method, path in RETIRED:
        response = http.request(
            method, PREFIX + path, json={"new_world": True, "org_type": "platform"}
        )
        assert response.status_code == 404, (path, response.text)
        assert "set-cookie" not in response.headers
    assert counts() == before


def test_saas_schema_omits_demo_routes_without_changing_main_routes(saas):
    paths = saas.openapi()["paths"]
    assert not any(
        "demo-login" in p or "/demo/" in p or "/sandbox/" in p or "auto-accept" in p for p in paths
    )
    assert PREFIX + "/auth/active-org" in paths
    assert PREFIX + "/auth/login" in paths
    assert PREFIX + "/auth/signup" in paths
    original = create_app(Settings(saas_mode=False)).openapi()["paths"]
    assert "/api/demo/reset" in original
    assert "/api/intake/lots" in original


@pytest.mark.parametrize("seed", [False, True])
@pytest.mark.parametrize("path", ["/me", "/saas/admin/overview", "/scenarios"])
def test_old_synthetic_sessions_require_login_and_preserve_rows(saas, seed, path):
    http = synthetic_session(saas, seed=seed)
    before = counts()
    response = http.get(PREFIX + path)
    assert response.status_code == 401, response.text
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "Path=/test" in response.headers["set-cookie"]
    assert counts() == before
    with database.begin() as db:
        session = db.get(AuthSessionRow, _token_hash("retired-session"))
        assert session.last_seen_at < datetime.now(UTC) - timedelta(minutes=50)


def test_visitor_cannot_switch_into_real_company(saas):
    http = synthetic_session(saas)
    with database.begin() as db:
        db.add(OrganizationRow(id="real-company", name="실제 회사", type="shipper"))
        db.flush()
        db.add(MembershipRow(user_id="synthetic", org_id="real-company", role="admin"))
    response = http.put(
        PREFIX + "/auth/active-org",
        json={"org_id": "real-company"},
        headers={"X-Org-Id": "real-company"},
    )
    assert response.status_code == 401
    with database.begin() as db:
        assert db.get(AuthSessionRow, _token_hash("retired-session")).active_org_id == "synthetic"


@pytest.mark.parametrize("org_type", ["shipper", "forwarder"])
def test_password_accounts_signup_login_and_company_switch_keep_working(saas, org_type):
    http = client(saas)
    me = signup(http, email="public-demo-account@example.com", org_type=org_type)
    other = signup(client(saas), email="other@example.com")
    user_id = me["user"]["id"]
    with database.begin() as db:
        db.add(OrganizationRow(id="second-company", name="두번째 업체", type=org_type))
        db.flush()
        db.add(MembershipRow(user_id=user_id, org_id="second-company", role="member"))
    assert http.get(PREFIX + "/me").status_code == 200
    denied = http.put(PREFIX + "/auth/active-org", json={"org_id": other["active_org"]["org_id"]})
    assert denied.status_code == 403
    denied = http.get(PREFIX + "/me", headers={"X-Org-Id": other["active_org"]["org_id"]})
    assert denied.status_code == 403
    switched = http.put(PREFIX + "/auth/active-org", json={"org_id": "second-company"})
    assert switched.status_code == 200
    assert switched.json()["active_org"]["role"] == "member"
    csrf = http.put(
        PREFIX + "/auth/active-org",
        json={"org_id": me["active_org"]["org_id"]},
        headers={"X-Requested-With": ""},
    )
    assert csrf.status_code == 403
    assert http.post(PREFIX + "/auth/logout").status_code == 204
    response = http.post(
        PREFIX + "/auth/login", json={"email": me["user"]["email"], "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["user"]["id"] == user_id
    assert http.get(PREFIX + "/me").status_code == 200


@pytest.mark.parametrize(
    "path", ["/saas/forwarder/plans", "/saas/forwarder/plans/missing/confirmation/recompute"]
)
def test_auto_accept_cannot_bypass_real_confirmation(saas, path):
    http = client(saas)
    signup(http, org_type="forwarder")
    before = counts()
    response = http.post(PREFIX + path, json={"auto_accept": True})
    assert response.status_code == 422, response.text
    assert counts() == before


def test_saas_startup_does_not_purge_historical_worlds(saas, monkeypatch):
    from freshswitch_backend.persistence import sandbox_world

    def unexpected_purge():
        pytest.fail("SaaS startup must preserve historical demo records")

    monkeypatch.setattr(sandbox_world, "purge_expired_worlds", unexpected_purge)
    with client(saas) as http:
        assert http.get(PREFIX + "/health").status_code == 200


def test_existing_platform_credentials_keep_admin_reads_and_session(saas):
    from freshswitch_backend.persistence.seed import create_platform

    with database.begin() as db:
        create_platform(
            db,
            name="운영 플랫폼",
            email="ops@example.com",
            user_name="운영자",
            password=PASSWORD,
            org_id="normal-platform",
        )
    http = client(saas)
    response = http.post(
        PREFIX + "/auth/login", json={"email": "ops@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    token = http.cookies.get(saas.state.settings.session_cookie_name)
    before = counts()
    assert http.get(PREFIX + "/saas/admin/overview").status_code == 200
    assert http.get(PREFIX + "/me").json()["active_org"]["org_id"] == "normal-platform"
    assert http.cookies.get(saas.state.settings.session_cookie_name) == token
    assert counts() == before
