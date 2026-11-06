from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from saas_fixtures import (
    PLATFORM_ORG_ID,
    SHIPPER_ORG_ID,
    login_client,
    saas_app,
    saas_settings,
)
from sqlalchemy import select, update

from freshswitch_backend.api import auth
from freshswitch_backend.config import Settings, get_settings
from freshswitch_backend.main import app, create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    AuthSessionRow,
    MembershipRow,
    OrganizationRow,
)

CSRF = {"X-Requested-With": "freshswitch"}
PASSWORD = "correct-horse-battery"


def _signup(client: TestClient, email: str = "Owner@Example.com", **extra) -> dict:
    body = {
        "email": email,
        "password": PASSWORD,
        "name": "김화주",
        "org_name": "진주 딸기 영농조합",
        "org_type": "shipper",
        **extra,
    }
    return client.post("/api/auth/signup", json=body).json()


def _cookie_attrs(header: str) -> dict[str, str]:
    parts = [p.strip() for p in header.split(";")]
    name, _, value = parts[0].partition("=")
    attrs = {"name": name, "value": value}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        attrs[key.lower()] = val or "true"
    return attrs


@pytest.fixture
def https_client() -> TestClient:
    return TestClient(saas_app, base_url="https://testserver")


def test_signup_sets_session_and_me(https_client: TestClient) -> None:
    response = https_client.post(
        "/api/auth/signup",
        json={
            "email": "Owner@Example.com",
            "password": PASSWORD,
            "name": "김화주",
            "org_name": "진주 딸기 영농조합",
            "org_type": "shipper",
        },
    )
    assert response.status_code == 201
    me = response.json()
    assert me["user"]["email"] == "owner@example.com"
    assert me["user"]["name"] == "김화주"
    assert me["active_org"]["org_type"] == "shipper"
    assert me["active_org"]["org_name"] == "진주 딸기 영농조합"
    assert me["active_org"]["role"] == "admin"
    assert me["memberships"] == [me["active_org"]]

    cookie = _cookie_attrs(response.headers["set-cookie"])
    assert cookie["name"] == "fs_session"
    assert cookie["httponly"] == "true"
    assert cookie["secure"] == "true"
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"
    assert int(cookie["max-age"]) == get_settings().session_ttl

    with database.begin() as db:
        stored = db.scalars(select(AuthSessionRow)).all()
    assert len(stored) == 1
    assert stored[0].token_hash != cookie["value"]
    assert len(stored[0].token_hash) == 64

    current = https_client.get("/api/me")
    assert current.status_code == 200
    assert current.json() == me


def test_signup_rejections(https_client: TestClient) -> None:
    _signup(https_client)
    fresh = TestClient(saas_app, base_url="https://testserver")
    duplicate = fresh.post(
        "/api/auth/signup",
        json={
            "email": "OWNER@example.com",
            "password": PASSWORD,
            "name": "다른 사람",
            "org_name": "다른 조직",
            "org_type": "forwarder",
        },
    )
    assert duplicate.status_code == 409

    base = {"password": PASSWORD, "name": "가", "org_name": "나"}
    platform = fresh.post(
        "/api/auth/signup", json={**base, "email": "p@example.com", "org_type": "platform"}
    )
    assert platform.status_code == 422
    demo = fresh.post(
        "/api/auth/signup",
        json={**base, "email": "x@demo.freshswitch.ssalhub.xyz", "org_type": "shipper"},
    )
    assert demo.status_code == 422
    short = fresh.post(
        "/api/auth/signup",
        json={**base, "email": "s@example.com", "org_type": "shipper", "password": "short"},
    )
    assert short.status_code == 422
    bad_email = fresh.post(
        "/api/auth/signup", json={**base, "email": "not-an-email", "org_type": "shipper"}
    )
    assert bad_email.status_code == 422
    assert "set-cookie" not in platform.headers


def test_login_logout_flow(https_client: TestClient) -> None:
    _signup(https_client)
    https_client.cookies.clear()
    assert https_client.get("/api/me").status_code == 401

    wrong = https_client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": "wrong-password"}
    )
    assert wrong.status_code == 401
    unknown = https_client.post(
        "/api/auth/login", json={"email": "ghost@example.com", "password": PASSWORD}
    )
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]

    ok = https_client.post(
        "/api/auth/login", json={"email": " OWNER@example.com ", "password": PASSWORD}
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["email"] == "owner@example.com"
    assert https_client.get("/api/me").status_code == 200
    with database.begin() as db:
        assert len(db.scalars(select(AuthSessionRow)).all()) == 2

    out = https_client.post("/api/auth/logout", headers=CSRF)
    assert out.status_code == 204
    cleared = _cookie_attrs(out.headers["set-cookie"])
    assert cleared["name"] == "fs_session"
    assert cleared["max-age"] == "0"
    assert cleared["path"] == "/"
    with database.begin() as db:
        assert len(db.scalars(select(AuthSessionRow)).all()) == 1
    assert https_client.get("/api/me").status_code == 401


def test_login_hashes_even_for_unknown_user(https_client: TestClient, monkeypatch) -> None:
    calls: list[str] = []
    original = auth.verify_password

    def spy(password: str, stored: str) -> bool:
        calls.append(stored)
        return original(password, stored)

    monkeypatch.setattr(auth, "verify_password", spy)
    https_client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "x"})
    assert len(calls) == 1
    assert calls[0].startswith("scrypt$")


def test_relogin_replaces_previous_session(https_client: TestClient) -> None:
    _signup(https_client)
    https_client.post(
        "/api/auth/login",
        json={"email": "owner@example.com", "password": PASSWORD},
        headers=CSRF,
    )
    with database.begin() as db:
        assert len(db.scalars(select(AuthSessionRow)).all()) == 1


def test_expired_session_requires_login(https_client: TestClient) -> None:
    _signup(https_client)
    with database.begin() as db:
        db.execute(
            update(AuthSessionRow).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    response = https_client.get("/api/me")
    assert response.status_code == 401
    cleared = _cookie_attrs(response.headers["set-cookie"])
    assert cleared["name"] == "fs_session"
    assert cleared["max-age"] == "0"
    assert cleared["path"] == "/"


def test_login_succeeds_with_expired_session_cookie(https_client: TestClient) -> None:
    _signup(https_client)
    with database.begin() as db:
        db.execute(
            update(AuthSessionRow).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    response = https_client.post(
        "/api/auth/login",
        json={"email": "owner@example.com", "password": PASSWORD},
        headers=CSRF,
    )
    assert response.status_code == 200


@pytest.mark.parametrize("target", [app, saas_app], ids=["main", "saas"])
def test_demo_login_is_not_routed(target, demo_tenants) -> None:
    http = TestClient(target, base_url="https://testserver", headers=CSRF)
    for org_type in ("platform", "shipper", "forwarder"):
        response = http.post("/api/auth/demo-login", json={"org_type": org_type})
        assert response.status_code == 404
        assert "set-cookie" not in response.headers
    with database.begin() as db:
        assert db.scalars(select(AuthSessionRow)).all() == []


def test_csrf_required_for_cookie_session(https_client: TestClient) -> None:
    _signup(https_client, org_type="forwarder")
    blocked = https_client.post("/api/plans", json={"scenario_id": "nope"})
    assert blocked.status_code == 403
    allowed = https_client.post("/api/plans", json={"scenario_id": "nope"}, headers=CSRF)
    assert allowed.status_code == 404
    wrong = https_client.post(
        "/api/plans", json={"scenario_id": "nope"}, headers={"X-Requested-With": "XMLHttpRequest"}
    )
    assert wrong.status_code == 403
    assert https_client.post("/api/auth/logout").status_code == 403
    assert https_client.get("/api/me").status_code == 200


def test_anonymous_requests_skip_csrf(client: TestClient) -> None:
    assert client.post("/api/plans", json={"scenario_id": "nope"}).status_code == 404
    assert client.get("/api/scenarios").status_code == 200
    anonymous = TestClient(saas_app, base_url="https://testserver")
    assert anonymous.post("/api/auth/logout").status_code == 204
    assert anonymous.get("/api/me").status_code == 401


def _add_membership(user_id: str, org_id: str, org_type: str, name: str) -> None:
    with database.begin() as db:
        db.add(OrganizationRow(id=org_id, type=org_type, name=name))
        db.flush()
        db.add(MembershipRow(user_id=user_id, org_id=org_id, role="member"))


def test_org_header_switches_and_rejects_outsiders(https_client: TestClient) -> None:
    me = _signup(https_client)
    home = me["active_org"]["org_id"]
    _add_membership(me["user"]["id"], "forwarder-extra", "forwarder", "추가 포워더")
    with database.begin() as db:
        db.add(OrganizationRow(id="shipper-outside", type="shipper", name="남의 조직"))

    listed = https_client.get("/api/me").json()
    assert [m["org_id"] for m in listed["memberships"]] == [home, "forwarder-extra"]
    assert listed["active_org"]["org_id"] == home

    switched = https_client.get("/api/me", headers={"X-Org-Id": "forwarder-extra"})
    assert switched.status_code == 200
    assert switched.json()["active_org"] == {
        "org_id": "forwarder-extra",
        "org_type": "forwarder",
        "org_name": "추가 포워더",
        "role": "member",
        "sandbox": False,
        "world_id": None,
    }
    assert https_client.get("/api/me").json()["active_org"]["org_id"] == home

    outside = https_client.get("/api/me", headers={"X-Org-Id": "shipper-outside"})
    assert outside.status_code == 403
    missing = https_client.get("/api/me", headers={"X-Org-Id": "no-such-org"})
    assert missing.status_code == 403
    demo = https_client.get("/api/me", headers={"X-Org-Id": DEMO_ORG})
    assert demo.status_code == 403


PlatformOnly = Annotated[auth.Principal, Depends(auth.require_role("platform"))]
Carrier = Annotated[auth.Principal, Depends(auth.require_role("shipper", "forwarder"))]
OrgScope = Annotated[str, Depends(auth.current_org_id)]


def _probe_app() -> FastAPI:
    probe = APIRouter()

    @probe.get("/probe/org")
    def org(org_id: OrgScope) -> dict:
        return {"org_id": org_id}

    @probe.get("/probe/principal")
    def principal(p: auth.AuthPrincipal) -> dict:
        return {"user_id": p.user_id, "org_id": p.org_id, "type": p.org_type, "role": p.role}

    @probe.get("/probe/platform")
    def platform_only(p: PlatformOnly) -> dict:
        return {"org_id": p.org_id}

    @probe.get("/probe/carriers")
    def carriers(p: Carrier) -> dict:
        return {"org_id": p.org_id}

    built = create_app(saas_settings())
    built.include_router(probe, prefix=get_settings().api_prefix)
    return built


def test_principal_dependencies(demo_tenants) -> None:
    built = _probe_app()
    anonymous = TestClient(built, base_url="https://testserver")
    assert anonymous.get("/api/probe/org").json() == {"org_id": DEMO_ORG}
    assert anonymous.get("/api/probe/principal").status_code == 401
    assert anonymous.get("/api/probe/platform").status_code == 401

    probe = login_client(SHIPPER_ORG_ID, built)
    me = probe.get("/api/me").json()
    assert probe.get("/api/probe/org").json() == {"org_id": SHIPPER_ORG_ID}
    assert probe.get("/api/probe/principal").json() == {
        "user_id": me["user"]["id"],
        "org_id": SHIPPER_ORG_ID,
        "type": "shipper",
        "role": "admin",
    }
    assert probe.get("/api/probe/platform").status_code == 403
    assert probe.get("/api/probe/carriers").status_code == 200

    platform = login_client(PLATFORM_ORG_ID, built)
    assert platform.get("/api/probe/platform").status_code == 200
    assert platform.get("/api/probe/carriers").status_code == 403


def _write_probe_app(calls: list[str]) -> FastAPI:
    probe = APIRouter()

    @probe.post("/probe/write")
    def write(org_id: OrgScope) -> dict:
        calls.append(org_id)
        return {"org_id": org_id}

    built = create_app(saas_settings())
    built.include_router(probe, prefix=get_settings().api_prefix)
    return built


def test_stale_session_never_falls_back_to_demo() -> None:
    calls: list[str] = []
    probe = TestClient(_write_probe_app(calls), base_url="https://testserver")
    me = _signup(probe)
    org_id = me["active_org"]["org_id"]
    saas_headers = {**CSRF, "X-Org-Id": org_id}
    assert probe.post("/api/probe/write", headers=saas_headers).json() == {"org_id": org_id}

    with database.begin() as db:
        db.execute(
            update(AuthSessionRow).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    expired = probe.post("/api/probe/write", headers=saas_headers)
    assert expired.status_code == 401
    assert _cookie_attrs(expired.headers["set-cookie"])["max-age"] == "0"
    assert calls == [org_id]


def test_logged_out_cookie_and_orphan_org_header_are_rejected() -> None:
    calls: list[str] = []
    probe = TestClient(_write_probe_app(calls), base_url="https://testserver")
    _signup(probe)
    token = probe.cookies.get("fs_session")
    other_tab = TestClient(probe.app, base_url="https://testserver")
    other_tab.cookies.set("fs_session", token)
    assert other_tab.post("/api/auth/logout", headers=CSRF).status_code == 204

    assert probe.post("/api/probe/write", headers=CSRF).status_code == 401
    assert probe.get("/api/me").status_code == 401

    anonymous = TestClient(probe.app, base_url="https://testserver")
    orphan = anonymous.post("/api/probe/write", headers={**CSRF, "X-Org-Id": "shipper-x"})
    assert orphan.status_code == 401
    assert "set-cookie" not in orphan.headers
    assert anonymous.post("/api/probe/write").json() == {"org_id": DEMO_ORG}
    assert calls == [DEMO_ORG]


def test_settings_normalize_paths() -> None:
    settings = Settings(api_prefix="test/api/", session_cookie_path="/test/")
    assert settings.api_prefix == "/test/api"
    assert settings.session_cookie_path == "/test"
    assert Settings(session_cookie_path="/").cookie_path == "/"


@pytest.fixture
def saas_client() -> TestClient:
    settings = saas_settings(api_prefix="/test/api", session_cookie_path="/test")
    return TestClient(create_app(settings), base_url="https://testserver")


def test_saas_prefix_routes(saas_client: TestClient) -> None:
    assert saas_client.get("/test/api/health").status_code == 200
    assert saas_client.get("/test/api/scenarios").status_code == 401
    assert saas_client.get("/api/health").status_code == 404
    assert saas_client.get("/test/api/docs").status_code == 200
    assert saas_client.get("/test/api/redoc").status_code == 200
    assert saas_client.get("/api/docs").status_code == 404
    spec = saas_client.get("/test/api/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert "/test/api/me" in paths
    assert "/test/api/auth/login" in paths
    assert all(path.startswith("/test/api/") for path in paths)
    assert "/test/api/openapi.json" in saas_client.get("/test/api/docs").text


def test_saas_prefix_session_cookie(saas_client: TestClient) -> None:
    response = saas_client.post(
        "/test/api/auth/signup",
        json={
            "email": "saas@example.com",
            "password": PASSWORD,
            "name": "가",
            "org_name": "나",
            "org_type": "forwarder",
        },
    )
    assert response.status_code == 201
    assert _cookie_attrs(response.headers["set-cookie"])["path"] == "/test"
    assert saas_client.get("/test/api/me").status_code == 200
    out = saas_client.post("/test/api/auth/logout", headers=CSRF)
    assert out.status_code == 204
    assert _cookie_attrs(out.headers["set-cookie"])["path"] == "/test"
    assert saas_client.get("/test/api/me").status_code == 401


def test_default_prefix_paths_unchanged() -> None:
    paths = app.openapi()["paths"]
    assert all(path.startswith("/api/") for path in paths)
    assert {"/api/health", "/api/plans", "/api/intake/lots"} <= set(paths)
    assert not {"/api/me", "/api/auth/login", "/api/auth/signup"} & set(paths)
    assert not any(path.startswith("/api/saas/") for path in paths)
    assert app.docs_url == "/api/docs"
    assert app.openapi_url == "/api/openapi.json"
