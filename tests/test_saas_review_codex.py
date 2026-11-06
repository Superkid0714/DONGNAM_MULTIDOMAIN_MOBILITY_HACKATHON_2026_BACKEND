import time

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    SHIPPER_ORG_ID,
    accept_plan,
    seed_client,
)
from sqlalchemy import delete

from freshswitch_backend.api import coldchain as coldchain_api
from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import AuthSessionRow
from freshswitch_backend.planning.store import store
from freshswitch_backend.quality import quality_history
from freshswitch_backend.quality.store import ScopeRef

OTHER_EXCESS_NOTE = "비공개 메모 1234567원"
OWN_NOTE = "자기 검수 메모"
OBSERVATION = {"product_id": "strawberry", "pallets": 1000, "actual_loss_pct": 100}


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _lot_body(lot) -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "sell_window": lot.sell_window,
        "shipper": lot.shipper_id,
        "lot_id": lot.id,
    }


def _world_plan(client_app) -> tuple[TestClient, TestClient, str]:
    scenario = get_scenario("default")
    assert scenario is not None
    ids = []
    for lot in scenario.lots:
        shipper = seed_client(shipper_org_id(lot.shipper_id), client_app)
        response = shipper.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    forwarder = seed_client(FORWARDER_ORG_ID, client_app)
    created = forwarder.post("/api/saas/forwarder/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    platform = seed_client(PLATFORM_ORG_ID, client_app)
    return platform, forwarder, plan_id


def _inspect(client_app, forwarder: TestClient, plan_id: str) -> None:
    accept_plan(lambda org_id: seed_client(org_id, client_app), plan_id)
    for lot_id, note in (("lot-sancheong-sb-1", OTHER_EXCESS_NOTE), ("lot-jinju-sb-1", OWN_NOTE)):
        response = forwarder.post(
            f"/api/plans/{plan_id}/settlement/arrival",
            json={"lot_id": lot_id, "inspected_loss_pct": 40, "note": note},
        )
        assert response.status_code == 200, response.text


def test_codex2_anonymous_cannot_read_raw_observations(saas_app) -> None:
    anonymous = TestClient(saas_app, base_url="https://testserver")
    target = quality_history.observations()[0].id
    assert anonymous.get("/api/quality/observations").status_code == 401
    assert anonymous.get(f"/api/quality/observations/{target}").status_code == 401


@pytest.mark.parametrize("org_id", [SHIPPER_ORG_ID, FORWARDER_ORG_ID])
def test_codex2_only_platform_reads_raw_observations(saas_app, org_id: str) -> None:
    client = seed_client(org_id, saas_app)
    target = quality_history.observations(ScopeRef("sandbox", "seed"))[0].id
    assert client.get("/api/quality/observations").status_code == 403
    assert client.get(f"/api/quality/observations/{target}").status_code == 403
    platform = seed_client(PLATFORM_ORG_ID, saas_app)
    assert platform.get("/api/quality/observations").status_code == 200
    assert platform.get(f"/api/quality/observations/{target}").status_code == 200


def test_codex3_anonymous_and_non_platform_cannot_add_observation(saas_app) -> None:
    before = len(quality_history.observations())
    anonymous = TestClient(saas_app, base_url="https://testserver")
    assert anonymous.post("/api/quality/observations", json=OBSERVATION).status_code == 401
    for org_id in (SHIPPER_ORG_ID, FORWARDER_ORG_ID, PLATFORM_ORG_ID):
        client = seed_client(org_id, saas_app)
        assert client.post("/api/quality/observations", json=OBSERVATION).status_code == 403
    assert len(quality_history.observations()) == before


def test_demo_mode_keeps_anonymous_observation_access(client: TestClient) -> None:
    assert client.get("/api/quality/observations").status_code == 200
    created = client.post("/api/quality/observations", json=OBSERVATION)
    assert created.status_code == 201
    assert client.get(f"/api/quality/observations/{created.json()['id']}").status_code == 200


def test_codex4_shipper_plan_hides_other_inspections(saas_app) -> None:
    platform, forwarder, plan_id = _world_plan(saas_app)
    _inspect(saas_app, forwarder, plan_id)
    full = platform.get(f"/api/plans/{plan_id}").json()
    shipper = seed_client(SHIPPER_ORG_ID, saas_app)
    response = shipper.get(f"/api/saas/shipper/plans/{plan_id}")
    assert response.status_code == 200
    body = response.json()
    inspections = body["arrival_inspections"]
    assert [row["shipper_id"] for row in inspections] == ["sh-jinju"]
    assert inspections[0]["note"] == OWN_NOTE
    assert {c["shipper_id"] for c in body["plan"]["lot_collections"]} == {"sh-jinju"}
    other_excess = next(
        row["excess_loss_value"]
        for row in full["arrival_inspections"]
        if row["shipper_id"] == "sh-sancheong"
    )
    assert other_excess > 0
    assert OTHER_EXCESS_NOTE not in response.text
    assert f'"excess_loss_value":{other_excess}' not in response.text


def test_codex4_shipper_plan_keeps_only_own_product_tariffs(saas_app) -> None:
    platform, _, plan_id = _world_plan(saas_app)
    full = platform.get(f"/api/plans/{plan_id}").json()
    assert {t["product_id"] for t in full["plan"]["settlement_tariffs"]} == {"strawberry"}
    strawberry = seed_client(SHIPPER_ORG_ID, saas_app)
    own = strawberry.get(f"/api/saas/shipper/plans/{plan_id}").json()
    assert {t["product_id"] for t in own["plan"]["settlement_tariffs"]} == {"strawberry"}
    grape = seed_client(shipper_org_id("sh-sangju"), saas_app)
    other = grape.get(f"/api/saas/shipper/plans/{plan_id}").json()
    assert other["plan"]["settlement_tariffs"] == []


def test_codex5_forwarder_plan_hides_inspection_money_and_notes(saas_app) -> None:
    _, forwarder, plan_id = _world_plan(saas_app)
    _inspect(saas_app, forwarder, plan_id)
    response = forwarder.get(f"/api/saas/forwarder/plans/{plan_id}")
    assert response.status_code == 200
    inspections = response.json()["arrival_inspections"]
    assert {row["lot_id"] for row in inspections} == {"lot-sancheong-sb-1", "lot-jinju-sb-1"}
    for row in inspections:
        assert row["excess_loss_value"] is None
        assert row["note"] is None
        assert row["inspected_loss_rate"] == pytest.approx(0.4)
    assert OTHER_EXCESS_NOTE not in response.text
    assert OWN_NOTE not in response.text


def _registered_device(forwarder: TestClient, plan_id: str, container_id: str) -> str:
    created = forwarder.post("/api/saas/devices", json={"device_id": "demo-codex"})
    assert created.status_code == 201, created.text
    bound = forwarder.post(
        "/api/coldchain/bindings",
        json={"device_id": "demo-codex", "container_id": container_id, "plan_id": plan_id},
    )
    assert bound.status_code == 201, bound.text
    return created.json()["device_token"]


def _used_containers(platform: TestClient, plan_id: str) -> list[str]:
    plan = platform.get(f"/api/plans/{plan_id}").json()["plan"]
    return [c["container_id"] for c in plan["containers"] if c["used"]]


def test_codex6_device_token_cannot_target_other_container(saas_app) -> None:
    platform, forwarder, plan_id = _world_plan(saas_app)
    bound, other = _used_containers(platform, plan_id)[:2]
    token = _registered_device(forwarder, plan_id, bound)
    device = TestClient(saas_app, base_url="https://testserver")
    headers = {"X-Device-Token": token}
    reading = {"device_id": "demo-codex", "temp_c": 40, "power_on": False}
    forged = device.post(
        "/api/coldchain/readings",
        json={**reading, "plan_id": plan_id, "container_id": other},
        headers=headers,
    )
    assert forged.status_code == 422
    untouched = platform.get(f"/api/coldchain/containers/{other}", params={"plan_id": plan_id})
    assert untouched.status_code == 200
    assert untouched.json()["reading_count"] == 0
    wrong_plan = device.post(
        "/api/coldchain/readings",
        json={**reading, "plan_id": "plan-elsewhere", "container_id": bound},
        headers=headers,
    )
    assert wrong_plan.status_code == 422
    explicit = device.post(
        "/api/coldchain/readings",
        json={**reading, "plan_id": plan_id, "container_id": bound},
        headers=headers,
    )
    assert explicit.status_code == 200, explicit.text
    implicit = device.post(
        "/api/coldchain/readings", json={**reading, "temp_c": 2}, headers=headers
    )
    assert implicit.status_code == 200, implicit.text
    assert [c["container_id"] for c in implicit.json()["containers"]] == [bound]
    assert implicit.json()["containers"][0]["plan_id"] == plan_id


def test_codex6_unbound_registered_device_is_rejected(saas_app) -> None:
    platform, forwarder, plan_id = _world_plan(saas_app)
    container = _used_containers(platform, plan_id)[0]
    created = forwarder.post("/api/saas/devices", json={"device_id": "demo-loose"})
    token = created.json()["device_token"]
    device = TestClient(saas_app, base_url="https://testserver")
    sent = device.post(
        "/api/coldchain/readings",
        json={
            "device_id": "demo-loose",
            "plan_id": plan_id,
            "container_id": container,
            "temp_c": 3,
            "power_on": True,
        },
        headers={"X-Device-Token": token},
    )
    assert sent.status_code == 422


def _stream_until_revoked(
    monkeypatch, client: TestClient, plan_id: str, revoke, timeout: float = 20
) -> float:
    monkeypatch.setattr(coldchain_api, "STREAM_RECHECK_SECONDS", 0.0, raising=False)
    original = coldchain_store.alerts
    calls = {"n": 0}

    def alerts(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            revoke()
        return original(*args, **kwargs)

    monkeypatch.setattr(coldchain_store, "alerts", alerts)
    started = time.monotonic()
    response = client.get(
        "/api/coldchain/alerts/stream", params={"plan_id": plan_id, "timeout": timeout}
    )
    assert response.status_code == 200
    return time.monotonic() - started


def test_codex7_stream_closes_after_plan_deleted(saas_app, monkeypatch) -> None:
    _, forwarder, plan_id = _world_plan(saas_app)

    def drop() -> None:
        assert store.delete(plan_id, org_id=FORWARDER_ORG_ID)

    assert _stream_until_revoked(monkeypatch, forwarder, plan_id, drop) < 10


def test_codex7_stream_closes_after_logout(saas_app, monkeypatch) -> None:
    _, forwarder, plan_id = _world_plan(saas_app)

    def logout() -> None:
        with database.begin() as db:
            db.execute(delete(AuthSessionRow))

    assert _stream_until_revoked(monkeypatch, forwarder, plan_id, logout) < 10


def test_stream_keeps_running_while_allowed(saas_app, monkeypatch) -> None:
    _, forwarder, plan_id = _world_plan(saas_app)
    elapsed = _stream_until_revoked(monkeypatch, forwarder, plan_id, lambda: None, timeout=2)
    assert elapsed >= 2
