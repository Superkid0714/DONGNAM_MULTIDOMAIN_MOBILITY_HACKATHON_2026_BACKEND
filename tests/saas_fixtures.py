import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.main import app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import create_platform, demo_orgs
from freshswitch_backend.persistence.tables import AuthSessionRow, SandboxWorldRow

SAAS_CSRF = {"X-Requested-With": "freshswitch"}
SAAS_DEMO_PASSWORD = "test-demo-password"
PLATFORM_ORG_ID = "freshswitch"
SHIPPER_ORG_ID = "shipper-jinju"
OTHER_SHIPPER_ORG_ID = "shipper-sancheong"
FORWARDER_ORG_ID = "forwarder-dongnam"
OTHER_FORWARDER_ORG_ID = "forwarder-sinhang"


def demo_org_type(org_id: str) -> str:
    return next(org.type for org in demo_orgs() if org.id == org_id)


def _world_alive(world_id: str | None, user_id: str | None) -> bool:
    if not world_id or not user_id:
        return False
    with database.begin() as db:
        world = db.get(SandboxWorldRow, world_id)
        return world is not None and world.expires_at > datetime.now(UTC)


def _mint_session(client: TestClient, client_app, user_id: str, org_id: str, world_id: str) -> None:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(UTC)
    with database.begin() as db:
        world = db.get(SandboxWorldRow, world_id)
        assert world is not None
        db.add(
            AuthSessionRow(
                token_hash=token_hash,
                user_id=user_id,
                active_org_id=org_id,
                expires_at=world.expires_at,
                last_seen_at=now,
            )
        )
    settings = client_app.state.settings
    client.cookies.clear()
    client.cookies.set(settings.session_cookie_name, token, path=settings.cookie_path)


def login_client(org_id: str, client_app=app) -> TestClient:
    client = TestClient(client_app, base_url="https://testserver", headers=SAAS_CSRF)
    settings = getattr(client_app.state, "settings", None)
    if settings is None or not settings.saas_mode:
        response = client.post(
            "/api/auth/demo-login", json={"org_type": demo_org_type(org_id), "org_id": org_id}
        )
        assert response.status_code == 200, response.text
        assert response.json()["active_org"]["org_id"] == org_id
        return client
    world_id = getattr(client_app.state, "sandbox_world_id", None)
    user_id = getattr(client_app.state, "sandbox_user_id", None)
    if not _world_alive(world_id, user_id):
        opened = client.post("/api/auth/demo-login", json={"new_world": True, "org_type": "platform"})
        assert opened.status_code == 200, opened.text
        body = opened.json()
        world_id = body["world"]["world_id"]
        user_id = body["user"]["id"]
        client_app.state.sandbox_world_id = world_id
        client_app.state.sandbox_user_id = user_id
    mapped = org_id if org_id.startswith(f"{world_id}-") else f"{world_id}-{org_id}"
    current = client.get("/api/me")
    if current.status_code == 200:
        if current.json()["active_org"]["org_id"] != mapped:
            switched = client.put("/api/auth/active-org", json={"org_id": mapped})
            assert switched.status_code == 200, switched.text
    else:
        _mint_session(client, client_app, user_id, mapped, world_id)
    me = client.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json()["active_org"]["org_id"] == mapped
    return client


def seed_client(org_id: str, client_app=app) -> TestClient:
    client = TestClient(client_app, base_url="https://testserver", headers=SAAS_CSRF)
    user_id = next(org.user_id for org in demo_orgs() if org.id == org_id)
    _mint_session(client, client_app, user_id, org_id, "seed")
    me = client.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json()["active_org"]["org_id"] == org_id
    return client


@pytest.fixture
def saas_login(demo_tenants) -> Callable[[str], TestClient]:
    return login_client


@pytest.fixture
def platform_client(saas_login) -> TestClient:
    return saas_login(PLATFORM_ORG_ID)


@pytest.fixture
def shipper_client(saas_login) -> TestClient:
    return saas_login(SHIPPER_ORG_ID)


@pytest.fixture
def other_shipper_client(saas_login) -> TestClient:
    return saas_login(OTHER_SHIPPER_ORG_ID)


@pytest.fixture
def forwarder_client(saas_login) -> TestClient:
    return saas_login(FORWARDER_ORG_ID)


@pytest.fixture
def other_forwarder_client(saas_login) -> TestClient:
    return saas_login(OTHER_FORWARDER_ORG_ID)


REAL_PLATFORM_EMAIL = "ops@real-platform.example.com"
REAL_PLATFORM_PASSWORD = "real-platform-password"


def create_real_platform(org_id: str = "platform-real") -> str:
    with database.begin() as db:
        create_platform(
            db,
            name="실제 플랫폼",
            email=REAL_PLATFORM_EMAIL,
            user_name="실제 운영",
            password=REAL_PLATFORM_PASSWORD,
            org_id=org_id,
        )
    return org_id


def password_client(client_app, email: str, password: str) -> TestClient:
    client = TestClient(client_app, base_url="https://testserver", headers=SAAS_CSRF)
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return client
