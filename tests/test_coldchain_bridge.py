import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import seed_client

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.planning.store import store

BRIDGE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "arduino" / "bridge.py"
CSRF = {"X-Requested-With": "freshswitch"}
FAKE_RAW = {"device_id": "fake-01", "temp_c": 1.2, "power_on": True}


def _bridge():
    spec = importlib.util.spec_from_file_location("freshswitch_bridge", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = _bridge()


def _send(client: TestClient, request) -> object:
    return client.post(
        request.full_url,
        content=request.data,
        headers=dict(request.header_items()),
    )


def test_token_from_env_goes_to_header_and_binding_decides_container(monkeypatch) -> None:
    monkeypatch.setenv(bridge.TOKEN_ENV, "env-token")
    args = bridge.parse_args(["--fake", "--device-id", "demo-b1"])
    assert args.device_token == "env-token"
    payload = bridge.to_payload(FAKE_RAW, args, bridge.Clock(60))
    assert payload["device_id"] == "demo-b1"
    assert "container_id" not in payload
    request = bridge.build_request("https://example.com/test/api/", payload, args.device_token)
    assert request.full_url == "https://example.com/test/api/coldchain/readings"
    assert request.get_header("X-device-token") == "env-token"
    assert json.loads(request.data)["temp_c"] == 1.2


def test_demo_mode_keeps_default_container_without_header(monkeypatch) -> None:
    monkeypatch.delenv(bridge.TOKEN_ENV, raising=False)
    args = bridge.parse_args(["--fake"])
    payload = bridge.to_payload(FAKE_RAW, args, bridge.Clock(60))
    assert payload["container_id"] == "CA-01"
    request = bridge.build_request(args.api, payload, args.device_token)
    assert request.get_header("X-device-token") is None


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _login(app, org_id: str) -> TestClient:
    return seed_client(org_id, app)


def test_fake_reading_with_device_token_reaches_platform_monitor(saas_app) -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    plan_id = store.create(scenario, org_id="forwarder-dongnam").plan_id
    forwarder = _login(saas_app, "forwarder-dongnam")
    created = forwarder.post("/api/saas/devices", json={"device_id": "demo-bridge-01"})
    assert created.status_code == 201, created.text
    token = created.json()["device_token"]
    bound = forwarder.post(
        "/api/coldchain/bindings",
        json={"device_id": "demo-bridge-01", "container_id": "CA-01", "plan_id": plan_id},
    )
    assert bound.status_code == 201, bound.text
    args = bridge.parse_args(
        [
            "--fake",
            "--api",
            "https://testserver/api",
            "--device-id",
            "demo-bridge-01",
            "--device-token",
            token,
        ]
    )
    payload = bridge.to_payload(FAKE_RAW, args, bridge.Clock(60))
    device = TestClient(saas_app, base_url="https://testserver")
    no_token = bridge.build_request(args.api, payload, None)
    assert _send(device, no_token).status_code == 401
    sent = _send(device, bridge.build_request(args.api, payload, args.device_token))
    assert sent.status_code == 200, sent.text
    assert sent.json()["accepted"] == 1
    platform = _login(saas_app, "freshswitch")
    monitor = platform.get("/api/coldchain/containers", params={"plan_id": plan_id})
    assert [(c["container_id"], c["reading_count"]) for c in monitor.json()] == [("CA-01", 1)]
