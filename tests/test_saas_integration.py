from functools import partial

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    OTHER_FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    accept_plan,
    open_quantity_window,
    queue_quantity,
    seed_client,
)

from freshswitch_backend.api.auth import Principal
from freshswitch_backend.api.saas_access import PlanAccess, plan_for
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence.seed import shipper_org_id

REDUCE = {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}


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
        "shipper": "ignored",
        "lot_id": lot.id,
    }


def _forwarder_plan(saas_app) -> tuple[TestClient, str]:
    scenario = get_scenario("default")
    assert scenario is not None
    ids = []
    for lot in scenario.lots:
        shipper = seed_client(shipper_org_id(lot.shipper_id), saas_app)
        response = shipper.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    forwarder = seed_client(FORWARDER_ORG_ID, saas_app)
    created = forwarder.post("/api/saas/forwarder/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    return forwarder, created.json()["plan_id"]


def _forwarder_reads(client: TestClient, plan_id: str, container_id: str) -> dict[str, int]:
    params = {"plan_id": plan_id}
    return {
        "detail": client.get(f"/api/saas/forwarder/plans/{plan_id}").status_code,
        "orders": client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").status_code,
        "containers": client.get("/api/coldchain/containers", params=params).status_code,
        "state": client.get(f"/api/coldchain/containers/{container_id}", params=params).status_code,
        "alerts": client.get("/api/coldchain/alerts", params=params).status_code,
    }


def test_plan_for_signature_matches_coldchain_use(saas_app) -> None:
    _, plan_id = _forwarder_plan(saas_app)
    principal = Principal(
        user_id="user-freshswitch",
        org_id=PLATFORM_ORG_ID,
        org_type="platform",
        role="admin",
    )
    access = plan_for(plan_id, principal)
    assert isinstance(access, PlanAccess)
    assert access.owner_org_id == FORWARDER_ORG_ID
    assert access.session.plan_id == plan_id


def test_forwarder_owned_plan_access_across_routers(saas_app) -> None:
    forwarder, plan_id = _forwarder_plan(saas_app)
    shippers = partial(seed_client, client_app=saas_app)
    accept_plan(shippers, plan_id)
    open_quantity_window()
    queue_quantity(
        shippers(shipper_org_id("sh-sancheong")), plan_id, REDUCE["lot_id"], REDUCE["new_pallets"]
    )
    event = forwarder.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE})
    assert event.status_code == 200, event.text
    applied = forwarder.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["active_order_id"]
    plan = forwarder.get(f"/api/plans/{plan_id}").json()["plan"]
    container_id = next(c["container_id"] for c in plan["containers"] if c["used"])
    registered = forwarder.post("/api/saas/devices", json={"device_id": "demo-sensor-01"})
    assert registered.status_code == 201, registered.text
    run = forwarder.post(
        "/api/coldchain/simulate",
        json={
            "preset": "power_cut_restore",
            "plan_id": plan_id,
            "container_id": container_id,
            "device_id": "demo-sensor-01",
        },
    )
    assert run.status_code == 200, run.text

    platform = seed_client(PLATFORM_ORG_ID, saas_app)
    outsider = seed_client(OTHER_FORWARDER_ORG_ID, saas_app)
    assert set(_forwarder_reads(forwarder, plan_id, container_id).values()) == {200}
    assert [p["plan_id"] for p in forwarder.get("/api/saas/forwarder/plans").json()] == [plan_id]
    orders = forwarder.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    assert [o["order_id"] for o in orders] == [order_id]
    listed = platform.get("/api/saas/platform/plans").json()
    row = next(p for p in listed if p["plan_id"] == plan_id)
    assert row["forwarder"]["org_id"] == FORWARDER_ORG_ID
    assert set(_forwarder_reads(outsider, plan_id, container_id).values()) == {404}
    assert outsider.get("/api/saas/forwarder/plans").json() == []

    path = f"/api/saas/platform/plans/{plan_id}/forwarder"
    moved = platform.put(path, json={"forwarder_org_id": OTHER_FORWARDER_ORG_ID})
    assert moved.status_code == 403
    assert platform.delete(path).status_code == 403
    assert set(_forwarder_reads(forwarder, plan_id, container_id).values()) == {200}
    assert set(_forwarder_reads(outsider, plan_id, container_id).values()) == {404}
    confirm = outsider.post(
        f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm", json={"accept": True}
    )
    assert confirm.status_code == 404
