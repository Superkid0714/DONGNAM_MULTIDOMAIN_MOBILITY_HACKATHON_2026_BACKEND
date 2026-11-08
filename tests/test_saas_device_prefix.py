import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    PLATFORM_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    SAAS_CSRF,
    create_real_platform,
    login_client,
    password_client,
)

from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _register(client: TestClient, device_id: str):
    return client.post("/api/saas/devices", json={"device_id": device_id})


@pytest.mark.parametrize("device_id", ["arduino-01", "dev-1", "demo-sensor-1"])
def test_visitor_world_rewrites_device_ids(saas_app, device_id: str) -> None:
    sandbox = login_client(PLATFORM_ORG_ID, saas_app)
    token = sandbox.get("/api/me").json()["world"]["world_id"][1:]
    created = _register(sandbox, device_id)
    assert created.status_code == 201, created.text
    slug = device_id[5:] if device_id.lower().startswith("demo-") else device_id
    assert created.json()["device_id"] == f"d{token}-{slug}"
    assert created.json()["device_token"]


def test_visitor_register_rejects_simulator_ids(saas_app) -> None:
    sandbox = login_client(PLATFORM_ORG_ID, saas_app)
    rejected = _register(sandbox, "sim-01")
    assert rejected.status_code == 422
    assert sandbox.get("/api/saas/devices").json() == []


@pytest.mark.parametrize(
    ("device_id", "fragment"),
    [("demo-sensor-1", "'demo-'"), ("DEMO-x", "'demo-'"), ("sim-01", "'sim-'")],
)
def test_real_org_cannot_take_reserved_prefix(saas_app, device_id: str, fragment: str) -> None:
    create_real_platform()
    real = password_client(saas_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    rejected = _register(real, device_id)
    assert rejected.status_code == 422
    assert fragment in str(rejected.json()["detail"])
    assert real.get("/api/saas/devices").json() == []


def test_real_org_registers_plain_device(saas_app) -> None:
    create_real_platform()
    real = password_client(saas_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    assert _register(real, "arduino-01").status_code == 201


def test_demo_mode_keeps_device_ids_open(demo_tenants) -> None:
    client = TestClient(create_app(), base_url="https://testserver", headers=SAAS_CSRF)
    login = client.post("/api/auth/demo-login", json={"org_type": "platform"})
    assert login.status_code == 200
    assert _register(client, "dev-open").status_code == 201
