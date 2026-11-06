import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import SCENARIO_DEVICE_ID, forwarder_scenario_plan, seed_client

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.lots import ANY_ORG, lot_store
from freshswitch_backend.main import create_app
from freshswitch_backend.planning.store import store

CSRF = {"X-Requested-With": "freshswitch"}
PLATFORM = "freshswitch"
SHIPPER = "shipper-jinju"
FORWARDER = "forwarder-dongnam"
OUTSIDER_FORWARDER = "forwarder-sinhang"
OUTSIDER_SHIPPER = "shipper-sancheong"
LOT_ID = "lot-jinju-sb-1"


def _path(key: str, transition: str) -> str:
    return f"/api/coldchain/containers/CA-01/actions/{quote(key, safe='')}/{transition}"


def _simulate(client: TestClient, plan_id: str, device_id: str = "sim-01") -> None:
    run = client.post(
        "/api/coldchain/simulate",
        json={
            "preset": "power_cut_restore",
            "plan_id": plan_id,
            "container_id": "CA-01",
            "device_id": device_id,
        },
    )
    assert run.status_code == 200, run.text


def _feed(client: TestClient, plan_id: str, since: int = 0) -> dict:
    response = client.get("/api/coldchain/alerts", params={"plan_id": plan_id, "since": since})
    assert response.status_code == 200, response.text
    return response.json()


def _stream(client: TestClient, plan_id: str, since: int) -> list[dict]:
    response = client.get(
        "/api/coldchain/alerts/stream", params={"plan_id": plan_id, "since": since, "timeout": 0}
    )
    assert response.status_code == 200, response.text
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _keys(feed: dict) -> list[str]:
    return [f"{a['container_id']}:{a['key']}" for a in feed["alerts"]]


def test_forwarder_action_updates_reappear_in_feed_and_stream(demo_tenants) -> None:
    client, plan_id = forwarder_scenario_plan()
    _simulate(client, plan_id, device_id=SCENARIO_DEVICE_ID)
    before = _feed(client, plan_id)
    keys = _keys(before)
    assert keys and len(set(keys)) == len(keys)
    state_keys = [
        a["key"]
        for a in client.get("/api/coldchain/containers/CA-01", params={"plan_id": plan_id}).json()[
            "alerts"
        ]
    ]
    cursor = before["next_since"]
    assert _feed(client, plan_id, cursor)["alerts"] == []
    key = next(a["key"] for a in before["alerts"] if a["action"] is not None)
    params = {"plan_id": plan_id}

    assert client.post(_path(key, "accept"), params=params).status_code == 200
    accepted = _feed(client, plan_id, cursor)
    assert [a["key"] for a in accepted["alerts"]] == [key]
    assert accepted["alerts"][0]["action"]["status"] == "accepted"
    assert accepted["alerts"][0]["seq"] > cursor
    assert accepted["next_since"] == accepted["alerts"][0]["seq"]
    streamed = _stream(client, plan_id, cursor)
    assert [(a["key"], a["action"]["status"]) for a in streamed] == [(key, "accepted")]

    again = client.post(_path(key, "accept"), params=params)
    assert again.status_code == 200
    assert _feed(client, plan_id, accepted["next_since"])["alerts"] == []

    assert client.post(_path(key, "complete"), params=params).status_code == 200
    done = _feed(client, plan_id, accepted["next_since"])
    assert [(a["key"], a["action"]["status"]) for a in done["alerts"]] == [(key, "done")]
    streamed = _stream(client, plan_id, accepted["next_since"])
    assert [(a["key"], a["action"]["status"]) for a in streamed] == [(key, "done")]

    after = _feed(client, plan_id)
    assert sorted(_keys(after)) == sorted(keys)
    assert len(after["alerts"]) == len(before["alerts"])
    state = client.get("/api/coldchain/containers/CA-01", params=params).json()
    assert [a["key"] for a in state["alerts"]] == state_keys
    assert next(a for a in state["alerts"] if a["key"] == key)["action"]["status"] == "done"


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
        scenario, registered_lot_ids=[LOT_ID], org_id=FORWARDER, lot_org_id=ANY_ORG
    )
    forwarder = _login(saas_app, FORWARDER)
    registered = forwarder.post(
        "/api/saas/devices", json={"device_id": "demo-sensor-01"}, headers=CSRF
    )
    assert registered.status_code == 201, registered.text
    _simulate(forwarder, session.plan_id, "demo-sensor-01")
    return session.plan_id


def test_saas_action_update_stream_keeps_role_scope(saas_app, saas_plan: str) -> None:
    platform = _login(saas_app, PLATFORM)
    shipper = _login(saas_app, SHIPPER)
    forwarder = _login(saas_app, FORWARDER)
    initial = {
        name: _feed(client, saas_plan)
        for name, client in (("platform", platform), ("shipper", shipper), ("forwarder", forwarder))
    }
    cursor = initial["platform"]["next_since"]
    key = next(a["key"] for a in initial["platform"]["alerts"] if a["action"] is not None)

    done = forwarder.post(_path(key, "complete"), params={"plan_id": saas_plan})
    assert done.status_code == 200, done.text

    for name, client in (("platform", platform), ("shipper", shipper), ("forwarder", forwarder)):
        feed = _feed(client, saas_plan, cursor)
        assert [(a["key"], a["action"]["status"]) for a in feed["alerts"]] == [(key, "done")]
        streamed = _stream(client, saas_plan, cursor)
        assert streamed == feed["alerts"]
        full = _feed(client, saas_plan)
        assert sorted(_keys(full)) == sorted(_keys(initial[name]))

    shipper_alert = _feed(shipper, saas_plan, cursor)["alerts"][0]
    original = next(a for a in initial["shipper"]["alerts"] if a["key"] == key)
    assert shipper_alert["title"] == original["title"]
    assert shipper_alert["message"] == original["message"]

    for org_id in (OUTSIDER_FORWARDER, OUTSIDER_SHIPPER):
        outsider = _login(saas_app, org_id)
        params = {"plan_id": saas_plan, "since": cursor, "timeout": 0}
        assert outsider.get("/api/coldchain/alerts", params=params).status_code == 404
        assert outsider.get("/api/coldchain/alerts/stream", params=params).status_code == 404
        assert outsider.get("/api/coldchain/alerts", params={"since": 0}).json()["alerts"] == []
