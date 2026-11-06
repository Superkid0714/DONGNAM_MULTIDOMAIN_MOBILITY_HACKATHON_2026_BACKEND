import json
import re

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    SHIPPER_ORG_ID,
    seed_client,
)

from freshswitch_backend.api.saas_views import shipper_coldchain_view
from freshswitch_backend.coldchain.models import ColdchainAction
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.lots import ANY_ORG, lot_store
from freshswitch_backend.main import create_app
from freshswitch_backend.planning.store import store

OWN_LOT = "lot-jinju-sb-1"
OWN_SHIPPER = "sh-jinju"


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


@pytest.fixture
def plan_id(demo_tenants) -> str:
    scenario = get_scenario("default")
    assert scenario is not None
    lot = next(item for item in scenario.lots if item.id == OWN_LOT)
    lot_store.create(lambda _: (lot, scenario.id, None), org_id=SHIPPER_ORG_ID)
    session = store.create(
        scenario, registered_lot_ids=[OWN_LOT], org_id=FORWARDER_ORG_ID, lot_org_id=ANY_ORG
    )
    return session.plan_id


def _foreign_lots(plan_id: str) -> list[str]:
    session = store.get(plan_id, org_id=FORWARDER_ORG_ID)
    assert session is not None
    return [lot.id for lot in session.scenario.lots if lot.shipper_id != OWN_SHIPPER]


def _leaks(payload: object, raw_ids: list[str]) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False)
    return [
        lot_id
        for lot_id in raw_ids
        if re.search(f"(?<![A-Za-z0-9_]){re.escape(lot_id)}(?![A-Za-z0-9_])", text)
    ]


@pytest.fixture
def forwarder(saas_app, plan_id: str) -> TestClient:
    client = seed_client(FORWARDER_ORG_ID, saas_app)
    registered = client.post("/api/saas/devices", json={"device_id": "demo-sensor-01"})
    assert registered.status_code == 201, registered.text
    run = client.post(
        "/api/coldchain/simulate",
        json={
            "preset": "power_cut_restore",
            "plan_id": plan_id,
            "container_id": "CA-01",
            "device_id": "demo-sensor-01",
        },
    )
    assert run.status_code == 200, run.text
    return client


def _action_path(key: str, step: str) -> str:
    return f"/api/coldchain/containers/CA-01/actions/{key}/{step}"


def _handled(saas_app, plan_id: str, forwarder: TestClient) -> list[dict]:
    params = {"plan_id": plan_id}
    platform = seed_client(PLATFORM_ORG_ID, saas_app)
    actions = platform.get("/api/coldchain/containers/CA-01", params=params).json()["actions"]
    note = "lot-sancheong-sb-1 하차 확인"
    accepted = forwarder.post(
        _action_path(actions[0]["key"], "accept"), params=params, json={"note": note}
    )
    assert accepted.status_code == 200, accepted.text
    done = forwarder.post(_action_path(actions[1]["key"], "complete"), params=params)
    assert done.status_code == 200, done.text
    return platform.get("/api/coldchain/containers/CA-01", params=params).json()["actions"]


def _plan_detail_ids(shipper: TestClient, plan_id: str) -> dict[str, str]:
    detail = shipper.get(f"/api/saas/shipper/plans/{plan_id}")
    assert detail.status_code == 200, detail.text
    session = store.get(plan_id, org_id=FORWARDER_ORG_ID)
    assert session is not None
    raw = [lot.id for lot in session.scenario.lots]
    shown = [lot["id"] for lot in detail.json()["scenario"]["lots"]]
    return dict(zip(raw, shown, strict=True))


def _expected(raw_actions: list[dict], mapping: dict[str, str]) -> dict[str, list[str]]:
    return {a["key"]: [mapping[x] for x in a["lot_ids"]] for a in raw_actions}


def test_shipper_twin_actions_use_plan_detail_anon_ids(
    saas_app, plan_id: str, forwarder: TestClient
) -> None:
    raw_actions = _handled(saas_app, plan_id, forwarder)
    assert raw_actions[0]["accepted_by"]["org_id"] == FORWARDER_ORG_ID
    assert raw_actions[0]["accepted_by"]["user_id"]
    assert raw_actions[1]["done_by"]["org_id"] == FORWARDER_ORG_ID
    shipper = seed_client(SHIPPER_ORG_ID, saas_app)
    mapping = _plan_detail_ids(shipper, plan_id)
    assert mapping[OWN_LOT] == OWN_LOT
    assert mapping["lot-sancheong-sb-1"].startswith("anon-")
    state = shipper.get("/api/coldchain/containers/CA-01", params={"plan_id": plan_id})
    assert state.status_code == 200, state.text
    body = state.json()
    expected = _expected(raw_actions, mapping)
    assert {a["key"]: a["lot_ids"] for a in body["actions"]} == expected
    assert {a["action"]["key"]: a["action"]["lot_ids"] for a in body["alerts"] if a["action"]} == (
        expected
    )
    assert _leaks(body, _foreign_lots(plan_id)) == []
    first = next(a for a in body["actions"] if a["key"] == raw_actions[0]["key"])
    assert first["note"] == f"{mapping['lot-sancheong-sb-1']} 하차 확인"
    assert first["accepted_by"] == {"org_type": "forwarder", "org_id": None, "user_id": None}
    second = next(a for a in body["actions"] if a["key"] == raw_actions[1]["key"])
    assert second["done_by"] == {"org_type": "forwarder", "org_id": None, "user_id": None}
    text = json.dumps(body, ensure_ascii=False)
    assert PLATFORM_ORG_ID not in text and FORWARDER_ORG_ID not in text
    assert raw_actions[0]["accepted_by"]["user_id"] not in text


def test_shipper_alerts_and_stream_use_plan_detail_anon_ids(
    saas_app, plan_id: str, forwarder: TestClient
) -> None:
    raw_actions = _handled(saas_app, plan_id, forwarder)
    shipper = seed_client(SHIPPER_ORG_ID, saas_app)
    mapping = _plan_detail_ids(shipper, plan_id)
    expected = _expected(raw_actions, mapping)
    params = {"plan_id": plan_id}
    feed = shipper.get("/api/coldchain/alerts", params=params)
    assert feed.status_code == 200, feed.text
    alerts = feed.json()["alerts"]
    assert {a["action"]["key"]: a["action"]["lot_ids"] for a in alerts if a["action"]} == expected
    assert _leaks(alerts, _foreign_lots(plan_id)) == []
    stream = shipper.get("/api/coldchain/alerts/stream", params={**params, "timeout": 0})
    assert stream.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in stream.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events == alerts
    assert _leaks(events, _foreign_lots(plan_id)) == []
    handlers = [
        a["action"][name] for a in events if a["action"] for name in ("accepted_by", "done_by")
    ]
    assert [h for h in handlers if h is not None and (h["org_id"] or h["user_id"])] == []


def test_platform_and_forwarder_keep_raw_ids(saas_app, plan_id: str, forwarder: TestClient) -> None:
    raw_actions = _handled(saas_app, plan_id, forwarder)
    platform = seed_client(PLATFORM_ORG_ID, saas_app)
    params = {"plan_id": plan_id}
    state = forwarder.get("/api/coldchain/containers/CA-01", params=params).json()
    assert [a["lot_ids"] for a in state["actions"]] == [a["lot_ids"] for a in raw_actions]
    assert state["actions"][0]["accepted_by"]["org_id"] == FORWARDER_ORG_ID
    assert state["loss"]["cargo_value"] is None
    alerts = platform.get("/api/coldchain/alerts", params=params).json()["alerts"]
    assert "lot-sancheong-sb-1" in json.dumps(alerts, ensure_ascii=False)


def test_shipper_action_hides_lot_ids_missing_from_ledger(plan_id: str) -> None:
    session = store.get(plan_id, org_id=FORWARDER_ORG_ID)
    assert session is not None
    view = shipper_coldchain_view(session, [OWN_SHIPPER])
    fresh = "lot-newcomer-9"
    action = ColdchainAction(
        key="k",
        kind="genset_dispatch",
        severity="warning",
        title=f"{fresh} 점검",
        description=f"{OWN_LOT}, {fresh} 확인",
        container_id="CA-01",
        triggered_at="2026-11-04T00:00:00Z",
        lot_ids=[OWN_LOT, fresh, "lot-sancheong-sb-1"],
        note=f"{fresh} 하차",
    )
    shown = view.action(action).model_dump(mode="json")
    assert shown["lot_ids"][:2] == [OWN_LOT, "anon-x"]
    assert shown["lot_ids"][2].startswith("anon-") and shown["lot_ids"][2] != "anon-x"
    assert shown["title"] == "anon-x 점검"
    assert shown["description"] == f"{OWN_LOT}, anon-x 확인"
    assert shown["note"] == "anon-x 하차"
    assert _leaks(shown, [fresh, *_foreign_lots(plan_id)]) == []
