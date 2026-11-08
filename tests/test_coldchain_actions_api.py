from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import seed_client

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.lots import ANY_ORG, lot_store
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PlanForwarderRow
from freshswitch_backend.planning.store import store

CSRF = {"X-Requested-With": "freshswitch"}
PLATFORM = "freshswitch"
SHIPPER = "shipper-jinju"
FORWARDER = "forwarder-dongnam"
OUTSIDER_FORWARDER = "forwarder-sinhang"
LOT_ID = "lot-jinju-sb-1"


def _path(container_id: str, key: str, transition: str) -> str:
    return f"/api/coldchain/containers/{container_id}/actions/{quote(key, safe='')}/{transition}"


def _simulate(
    client: TestClient, plan_id: str, container_id: str = "CA-01", device_id: str = "sim-01"
) -> None:
    run = client.post(
        "/api/coldchain/simulate",
        json={
            "preset": "power_cut_restore",
            "plan_id": plan_id,
            "container_id": container_id,
            "device_id": device_id,
        },
    )
    assert run.status_code == 200, run.text


def _state(client: TestClient, plan_id: str, container_id: str = "CA-01") -> dict:
    response = client.get(f"/api/coldchain/containers/{container_id}", params={"plan_id": plan_id})
    assert response.status_code == 200, response.text
    return response.json()


def _open_count(client: TestClient, plan_id: str) -> int:
    rows = client.get("/api/coldchain/containers", params={"plan_id": plan_id}).json()
    return next(r["open_actions"] for r in rows if r["container_id"] == "CA-01")


def test_demo_action_accept_then_complete(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _simulate(client, plan_id)
    actions = _state(client, plan_id)["actions"]
    assert actions and all(a["status"] == "open" for a in actions)
    total = _open_count(client, plan_id)
    assert total == len(actions)
    key = actions[0]["key"]
    params = {"plan_id": plan_id}
    accepted = client.post(_path("CA-01", key, "accept"), params=params, json={"note": "접수"})
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["status"] == "accepted"
    assert body["accepted_by"]["org_type"] == "platform"
    assert body["accepted_at"] is not None
    assert body["note"] == "접수"
    assert _open_count(client, plan_id) == total
    again = client.post(_path("CA-01", key, "accept"), params=params)
    assert again.status_code == 200
    assert again.json()["accepted_at"] == body["accepted_at"]
    done = client.post(_path("CA-01", key, "complete"), params=params)
    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert done.json()["done_at"] is not None
    assert done.json()["note"] == "접수"
    assert _open_count(client, plan_id) == total - 1
    assert client.post(_path("CA-01", key, "complete"), params=params).status_code == 200
    assert client.post(_path("CA-01", key, "accept"), params=params).status_code == 409
    state = _state(client, plan_id)
    stored = next(a for a in state["actions"] if a["key"] == key)
    assert stored["status"] == "done"
    alert = next(a for a in state["alerts"] if a["key"] == key)
    assert alert["action"]["status"] == "done"
    missing = client.post(_path("CA-01", "missing", "accept"), params=params)
    assert missing.status_code == 404
    unknown = client.post(_path("CA-99", key, "accept"), params=params)
    assert unknown.status_code in (404, 422)


def test_complete_without_accept_fills_accept_fields(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _simulate(client, plan_id)
    key = _state(client, plan_id)["actions"][-1]["key"]
    done = client.post(_path("CA-01", key, "complete"), params={"plan_id": plan_id})
    assert done.status_code == 200
    assert done.json()["accepted_at"] == done.json()["done_at"]


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _login(app, org_id: str) -> TestClient:
    return seed_client(org_id, app)


@pytest.fixture
def saas_plan(saas_app) -> str:
    scenario = get_scenario("default")
    assert scenario is not None
    lot = next(item for item in scenario.lots if item.id == LOT_ID)
    lot_store.create(lambda _: (lot, scenario.id, None), org_id=SHIPPER)
    session = store.create(
        scenario, registered_lot_ids=[LOT_ID], org_id=PLATFORM, lot_org_id=ANY_ORG
    )
    with database.begin() as db:
        db.add(PlanForwarderRow(plan_id=session.plan_id, forwarder_org_id=FORWARDER))
    platform = _login(saas_app, PLATFORM)
    registered = platform.post("/api/saas/devices", json={"device_id": "demo-sensor-01"}, headers=CSRF)
    assert registered.status_code == 201, registered.text
    _simulate(platform, session.plan_id, device_id="demo-sensor-01")
    return session.plan_id


def test_saas_roles_for_action_updates(saas_app, saas_plan: str) -> None:
    params = {"plan_id": saas_plan}
    platform = _login(saas_app, PLATFORM)
    keys = [a["key"] for a in _state(platform, saas_plan)["actions"]]
    shipper = _login(saas_app, SHIPPER)
    denied = shipper.post(_path("CA-01", keys[0], "accept"), params=params)
    assert denied.status_code == 403
    outsider = _login(saas_app, OUTSIDER_FORWARDER)
    assert outsider.post(_path("CA-01", keys[0], "accept"), params=params).status_code == 404
    accepted = platform.post(_path("CA-01", keys[0], "accept"), params=params)
    assert accepted.status_code == 200
    assert accepted.json()["accepted_by"] == {
        "org_type": "platform",
        "org_id": PLATFORM,
        "user_id": accepted.json()["accepted_by"]["user_id"],
    }
    forwarder = _login(saas_app, FORWARDER)
    done = forwarder.post(_path("CA-01", keys[0], "complete"), params=params)
    assert done.status_code == 200, done.text
    assert done.json()["accepted_by"]["org_type"] == "platform"
    assert done.json()["done_by"]["org_type"] == "forwarder"
    assert done.json()["done_by"]["org_id"] == FORWARDER
    seen = next(a for a in _state(shipper, saas_plan)["actions"] if a["key"] == keys[0])
    assert seen["status"] == "done"
    rows = forwarder.get("/api/coldchain/containers", params=params).json()
    assert rows[0]["open_actions"] == len(keys) - 1
    no_csrf = TestClient(saas_app, base_url="https://testserver", cookies=forwarder.cookies)
    assert no_csrf.post(_path("CA-01", keys[1], "accept"), params=params).status_code == 403


def test_residual_risks_in_twin_state(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    empty = _state(client, plan_id)
    assert empty["residual_risks"] == []
    _simulate(client, plan_id)
    risks = {r["kind"]: r for r in _state(client, plan_id)["residual_risks"]}
    assert {"heat_accumulation", "temperature_swing", "sensor_gap"} <= set(risks)
    assert risks["temperature_swing"]["level"] == "warning"
    assert risks["heat_accumulation"]["threshold"]["unit"] == "°C·시간"
    assert all(r["threshold"]["source"].startswith("가정") for r in risks.values())


def test_saas_container_reset_scope(saas_app, saas_plan: str) -> None:
    params = {"plan_id": saas_plan}
    path = "/api/coldchain/containers/CA-01"
    shipper = _login(saas_app, SHIPPER)
    assert shipper.delete(path, params=params).status_code == 403
    outsider = _login(saas_app, OUTSIDER_FORWARDER)
    assert outsider.delete(path, params=params).status_code == 404
    other_shipper = _login(saas_app, "shipper-sancheong")
    assert other_shipper.delete(path, params=params).status_code == 404
    forwarder = _login(saas_app, FORWARDER)
    assert forwarder.delete(path, params=params).status_code == 404
    platform = _login(saas_app, PLATFORM)
    assert _state(platform, saas_plan)["reading_count"] > 0
    assert platform.delete(path, params=params).status_code == 204
    assert platform.delete(path, params=params).status_code == 404
    rows = platform.get("/api/coldchain/containers", params=params).json()
    assert all(row["container_id"] != "CA-01" for row in rows)
