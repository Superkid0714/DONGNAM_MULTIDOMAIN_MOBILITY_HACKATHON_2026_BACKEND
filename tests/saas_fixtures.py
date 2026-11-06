import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import create_platform, demo_orgs, shipper_org_id
from freshswitch_backend.persistence.tables import AuthSessionRow, MembershipRow, PlanRow, UserRow

SAAS_CSRF = {"X-Requested-With": "freshswitch"}
SAAS_DEMO_PASSWORD = "test-demo-password"
PLATFORM_ORG_ID = "freshswitch"
SHIPPER_ORG_ID = "shipper-jinju"
OTHER_SHIPPER_ORG_ID = "shipper-sancheong"
FORWARDER_ORG_ID = "forwarder-dongnam"
OTHER_FORWARDER_ORG_ID = "forwarder-sinhang"
DEMO_ORG_BY_TYPE = {
    "platform": PLATFORM_ORG_ID,
    "shipper": SHIPPER_ORG_ID,
    "forwarder": FORWARDER_ORG_ID,
}


def saas_settings(**update):
    return get_settings().model_copy(update={"saas_mode": True, **update})


saas_app = create_app(saas_settings())


def demo_org_type(org_id: str) -> str:
    return next(org.type for org in demo_orgs() if org.id == org_id)


def _operator(org_id: str) -> str:
    user_id = f"operator-{org_id}"[:64]
    with database.begin() as db:
        if db.get(UserRow, user_id) is None:
            db.add(
                UserRow(
                    id=user_id,
                    email=f"{org_id}@operator.example.com",
                    name="운영 담당",
                    password_hash="!",
                    is_demo=False,
                )
            )
            db.flush()
            db.add(MembershipRow(user_id=user_id, org_id=org_id, role="admin"))
    return user_id


def _mint_session(client: TestClient, client_app, user_id: str, org_id: str) -> None:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
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
    settings = client_app.state.settings
    client.cookies.clear()
    client.cookies.set(settings.session_cookie_name, token, path=settings.cookie_path)


def _saas(client_app) -> bool:
    settings = getattr(client_app.state, "settings", None)
    return settings is not None and settings.saas_mode


def sign_in(client: TestClient, org_id: str) -> None:
    _mint_session(client, client.app, _operator(org_id), org_id)


def sign_in_as(client: TestClient, org_type: str) -> str:
    org_id = DEMO_ORG_BY_TYPE[org_type]
    sign_in(client, org_id)
    return org_id


def login_client(org_id: str, client_app=saas_app) -> TestClient:
    client = TestClient(client_app, base_url="https://testserver", headers=SAAS_CSRF)
    if _saas(client_app):
        sign_in(client, org_id)
    else:
        response = client.post(
            "/api/auth/demo-login", json={"org_type": demo_org_type(org_id), "org_id": org_id}
        )
        assert response.status_code == 200, response.text
    me = client.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json()["active_org"]["org_id"] == org_id
    return client


def seed_client(org_id: str, client_app=saas_app) -> TestClient:
    client = TestClient(client_app, base_url="https://testserver", headers=SAAS_CSRF)
    if _saas(client_app):
        user_id = _operator(org_id)
    else:
        user_id = next(org.user_id for org in demo_orgs() if org.id == org_id)
    _mint_session(client, client_app, user_id, org_id)
    me = client.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json()["active_org"]["org_id"] == org_id
    return client


@pytest.fixture
def anonymous_saas() -> TestClient:
    return TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF)


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


def queue_quantity(shipper: TestClient, plan_id: str, lot_id: str, quantity: int) -> dict:
    path = f"/api/saas/shipper/plans/{plan_id}/lots/{lot_id}/quantity"
    quote = shipper.post(f"{path}/quote", json={"quantity": quantity})
    assert quote.status_code == 200, quote.text
    key = f"queue-{lot_id}-{quantity}-{secrets.token_hex(4)}"
    submitted = shipper.post(
        path, json={"quote_id": quote.json()["quote_id"], "idempotency_key": key}
    )
    assert submitted.status_code in (200, 201), submitted.text
    return submitted.json()


def accept_plan(login: Callable[[str], TestClient], plan_id: str) -> None:
    with database.begin() as db:
        row = db.get(PlanRow, plan_id)
        assert row is not None
        shipper_ids = sorted({lot["shipper_id"] for lot in row.payload["scenario"]["lots"]})
    for shipper_id in shipper_ids:
        response = login(shipper_org_id(shipper_id)).post(
            f"/api/saas/shipper/plans/{plan_id}/confirmation", json={"accept": True}
        )
        assert response.status_code == 200, response.text


SCENARIO_DEVICE_ID = "demo-sensor-01"


def forwarder_scenario_plan() -> tuple[TestClient, str]:
    from freshswitch_backend.data.loader import get_scenario
    from freshswitch_backend.planning.store import store

    scenario = get_scenario("default")
    assert scenario is not None
    session = store.create(scenario, org_id=FORWARDER_ORG_ID)
    forwarder = seed_client(FORWARDER_ORG_ID)
    registered = forwarder.post("/api/saas/devices", json={"device_id": SCENARIO_DEVICE_ID})
    assert registered.status_code == 201, registered.text
    return forwarder, session.plan_id


QUANTITY_WINDOW_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def open_quantity_window(now: datetime = QUANTITY_WINDOW_NOW) -> None:
    from freshswitch_backend.lots import lot_store
    from freshswitch_backend.planning.store import store

    store.clock = lambda: now
    lot_store.clock = lambda: now
