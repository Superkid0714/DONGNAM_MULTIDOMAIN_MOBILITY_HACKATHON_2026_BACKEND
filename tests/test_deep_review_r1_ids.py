import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import seed_client

from freshswitch_backend.coldchain.devices import token_hash
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import load_scenarios
from freshswitch_backend.domain.ids import ID_MAX_LENGTH, id_error
from freshswitch_backend.intake.models import Resolved
from freshswitch_backend.intake.service import _lot_id
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import DeviceRow
from freshswitch_backend.planning.store import store

CSRF = {"X-Requested-With": "freshswitch"}
BAD_IDS = ["x/y", "demo-a/b", "demo a", " ", "-lead", ".lead", "a" * (ID_MAX_LENGTH + 1), "부산"]
FORM = {
    "product": "딸기",
    "pallets": 2,
    "ready_date": "2026-11-17",
    "shipper": "산청",
    "destination": "홍콩",
}
LITERAL = re.compile(
    r"""(?:["'](?:lot_id|device_id)["']\s*:\s*|\b(?:lot_id|device_id|LOT_ID|DEVICE_ID)\s*=\s*)"""
    r"""["']([^"'{}]+)["']"""
)
INTENTIONALLY_INVALID = {"=1+1"}


@pytest.fixture
def saas(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _platform(app) -> TestClient:
    return seed_client("freshswitch", app)


def test_existing_scenario_and_generated_ids_pass_rule() -> None:
    for scenario in load_scenarios().values():
        for lot in scenario.lots:
            assert id_error(lot.id) is None, lot.id
        for shipper in scenario.shippers:
            for product in scenario.products:
                resolved = Resolved(
                    product_id=product.id,
                    product_name=product.name,
                    shipper_id=shipper.id,
                    shipper_name=shipper.name,
                    packhouse_id="p",
                    packhouse_name="p",
                    destination_port_id="HKHKG",
                    destination_name="홍콩",
                )
                generated = _lot_id(set(), resolved)
                assert id_error(generated) is None, generated


def test_existing_fixture_ids_pass_rule() -> None:
    roots = [
        Path(__file__).parent,
        Path(__file__).parents[1] / "src",
        Path(__file__).parents[1] / "scripts",
    ]
    found: set[str] = set()
    for root in roots:
        for path in root.rglob("*.py"):
            found.update(LITERAL.findall(path.read_text(encoding="utf-8")))
    assert {"lot-jinju-sb-1", "sim-01", "demo-p1", "arduino-01"} <= found
    bad = {value for value in found - INTENTIONALLY_INVALID if id_error(value) is not None}
    assert bad == set()


@pytest.mark.parametrize("bad", BAD_IDS)
def test_intake4_lot_id_rule(client: TestClient, bad: str) -> None:
    response = client.post("/api/intake/lots", json={**FORM, "lot_id": bad})
    assert response.status_code == 422, response.text


def test_intake4_blank_lot_id_is_generated(client: TestClient) -> None:
    response = client.post("/api/intake/lots", json={**FORM, "lot_id": ""})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lot"]["id"] == "lot-sancheong-strawberry-1"
    assert any(item.startswith("lot_id 자동 생성") for item in body["defaults_applied"])
    explicit = client.post("/api/intake/lots", json={**FORM, "lot_id": "lot-sancheong.v2_1"})
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["lot"]["id"] == "lot-sancheong.v2_1"


@pytest.mark.parametrize("bad", ["demo-a/b", "demo-a b", "demo-" + "a" * ID_MAX_LENGTH])
def test_coldchain3_device_id_rule(saas, bad: str) -> None:
    platform = _platform(saas)
    created = platform.post("/api/saas/devices", json={"device_id": bad}, headers=CSRF)
    assert created.status_code == 422, created.text


@pytest.mark.parametrize("bad", ["x/y", "x y", "센서 1", "TK:0001", "a" * (ID_MAX_LENGTH + 1)])
def test_coldchain3_simulation_rejects_rule_breaking_device_ids(
    client: TestClient, bad: str
) -> None:
    simulated = client.post("/api/coldchain/simulate", json={"device_id": bad})
    assert simulated.status_code == 422, simulated.text


def test_coldchain3_simulation_default_device_id(client: TestClient) -> None:
    simulated = client.post("/api/coldchain/simulate", json={})
    assert simulated.status_code == 200, simulated.text
    assert simulated.json()["device_id"] == "sim-01"


def test_coldchain3_valid_device_can_be_deleted(saas) -> None:
    platform = _platform(saas)
    created = platform.post("/api/saas/devices", json={"device_id": "demo-a.b_c"}, headers=CSRF)
    assert created.status_code == 201, created.text
    deleted = platform.delete("/api/saas/devices/demo-a.b_c", headers=CSRF)
    assert deleted.status_code == 204, deleted.text


@pytest.mark.parametrize("bad", ["TK:0001", "x/y", "센서 1"])
def test_coldchain3_rule_breaking_device_id_is_rejected_everywhere(
    saas, client: TestClient, bad: str
) -> None:
    token = "legacy-token-tk-0001"
    with database.begin() as db:
        db.add(DeviceRow(device_id=bad, org_id="freshswitch", token_hash=token_hash(token)))
    scenario = load_scenarios()["default"]
    plan_id = store.create(scenario, org_id="freshswitch").plan_id
    platform = _platform(saas)
    bound = platform.post(
        "/api/coldchain/bindings",
        json={"device_id": bad, "container_id": "CA-01", "plan_id": plan_id},
        headers=CSRF,
    )
    assert bound.status_code == 422, bound.text
    device = TestClient(saas, base_url="https://testserver")
    reading = device.post(
        "/api/coldchain/readings",
        json={"device_id": bad, "temp_c": 1.0, "power_on": True},
        headers={"X-Device-Token": token},
    )
    assert reading.status_code == 422, reading.text
    simulated = platform.post(
        "/api/coldchain/simulate", json={"device_id": bad, "plan_id": plan_id}, headers=CSRF
    )
    assert simulated.status_code == 422, simulated.text
    demo_reading = client.post(
        "/api/coldchain/readings", json={"device_id": bad, "temp_c": 1.0, "power_on": True}
    )
    assert demo_reading.status_code == 422, demo_reading.text
    demo_bound = client.post(
        "/api/coldchain/bindings", json={"device_id": bad, "container_id": "CA-01"}
    )
    assert demo_bound.status_code == 422, demo_bound.text
