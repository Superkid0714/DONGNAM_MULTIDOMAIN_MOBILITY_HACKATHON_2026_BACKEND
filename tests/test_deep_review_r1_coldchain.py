from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import seed_client

from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST
from freshswitch_backend.lots import ANY_ORG, lot_store
from freshswitch_backend.main import create_app
from freshswitch_backend.planning.store import store

CSRF = {"X-Requested-With": "freshswitch"}
PLATFORM = "freshswitch"
FORWARDER = "forwarder-dongnam"
SHIPPER = "shipper-jinju"
LOT_ID = "lot-jinju-sb-1"


@pytest.fixture
def saas(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _login(app, org_id: str) -> TestClient:
    return seed_client(org_id, app)


def _two_plans() -> tuple[str, str]:
    scenario = get_scenario("default")
    assert scenario is not None
    lot = next(item for item in scenario.lots if item.id == LOT_ID)
    lot_store.create(lambda _: (lot, scenario.id, None), org_id=SHIPPER)
    first = store.create(
        scenario, registered_lot_ids=[LOT_ID], org_id=FORWARDER, lot_org_id=ANY_ORG
    )
    second = store.create(scenario, org_id=FORWARDER)
    return first.plan_id, second.plan_id


def _count(client: TestClient, plan_id: str) -> int:
    response = client.get("/api/coldchain/containers/CA-01", params={"plan_id": plan_id})
    assert response.status_code == 200, response.text
    return response.json()["reading_count"]


def test_coldchain1_token_reading_of_deleted_plan_binding_is_rejected(saas, monkeypatch) -> None:
    monkeypatch.setattr(coldchain_store, "clock", lambda: datetime(2026, 11, 17, 12, tzinfo=KST))
    first, second = _two_plans()
    platform = _login(saas, PLATFORM)
    forwarder = _login(saas, FORWARDER)
    registered = forwarder.post(
        "/api/saas/devices", json={"device_id": "demo-sensor-01"}, headers=CSRF
    )
    assert registered.status_code == 201, registered.text
    run = forwarder.post(
        "/api/coldchain/simulate",
        json={
            "preset": "power_cut_restore",
            "plan_id": second,
            "container_id": "CA-01",
            "device_id": "demo-sensor-01",
        },
        headers=CSRF,
    )
    assert run.status_code == 200, run.text
    assert run.json()["done"] is True
    assert run.json()["sent"] > 0
    before = _count(platform, second)
    assert before == run.json()["sent"]
    token = forwarder.post("/api/saas/devices", json={"device_id": "demo-x1"}, headers=CSRF).json()[
        "device_token"
    ]
    bound = forwarder.post(
        "/api/coldchain/bindings",
        json={"device_id": "demo-x1", "container_id": "CA-01", "plan_id": first},
        headers=CSRF,
    )
    assert bound.status_code == 201, bound.text
    device = TestClient(saas, base_url="https://testserver")
    headers = {"X-Device-Token": token}
    reading = {"device_id": "demo-x1", "temp_c": 30.0, "power_on": False}
    live = device.post("/api/coldchain/readings", json=reading, headers=headers)
    assert live.status_code == 200, live.text
    assert live.json()["containers"][0]["plan_id"] == first
    assert store.delete(first, org_id=FORWARDER)
    stale = device.post(
        "/api/coldchain/readings",
        json={**reading, "ts": "2026-11-17T12:00:00+09:00"},
        headers=headers,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == "연결된 계획이 삭제되었습니다. 기기를 다시 바인딩하세요"
    assert _count(platform, second) == before
    rebound = forwarder.post(
        "/api/coldchain/bindings",
        json={"device_id": "demo-x1", "container_id": "CA-01", "plan_id": second},
        headers=CSRF,
    )
    assert rebound.status_code == 201, rebound.text
    again = device.post(
        "/api/coldchain/readings",
        json={**reading, "ts": "2026-11-17T12:00:00+09:00"},
        headers=headers,
    )
    assert again.status_code == 200, again.text
    assert _count(platform, second) == before + 1


def test_coldchain_saas_binding_without_plan_is_rejected(saas) -> None:
    forwarder = _login(saas, FORWARDER)
    created = forwarder.post("/api/saas/devices", json={"device_id": "demo-np1"}, headers=CSRF)
    assert created.status_code == 201, created.text
    bound = forwarder.post(
        "/api/coldchain/bindings",
        json={"device_id": "demo-np1", "container_id": "CA-77"},
        headers=CSRF,
    )
    assert bound.status_code == 422, bound.text
    assert bound.json()["detail"] == "계획을 지정하세요"
    listed = forwarder.get("/api/coldchain/bindings")
    assert listed.status_code == 200, listed.text
    assert all(item["device_id"] != "demo-np1" for item in listed.json())
