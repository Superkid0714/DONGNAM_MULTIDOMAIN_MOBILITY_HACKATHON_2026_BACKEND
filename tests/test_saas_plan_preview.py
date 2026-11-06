import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    SAAS_CSRF,
    SHIPPER_ORG_ID,
    login_client,
)

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence.seed import shipper_org_id

PREVIEW = "/api/saas/forwarder/plans/preview"
PLATFORM_PREVIEW = "/api/saas/platform/plans/preview"
RESOURCE = {
    "name": "실제 냉장 컨테이너",
    "kind": "40RH-CA",
    "capacity_pallets": 20,
    "temp_min_c": -1,
    "temp_max_c": 4,
    "vessel_name": "등록 선박",
    "terminal": "등록 터미널",
    "origin": "KRPUS",
    "destination": "HKHKG",
    "etd": "2026-11-18T18:00:00+09:00",
    "cy_cutoff": "2026-11-17T16:00:00+09:00",
    "transit_days": 3.5,
    "enabled": True,
}
TOTALS = ("total_baseline_cost", "total_shared_cost", "total_savings", "total_fee", "objective")


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
        "shipper": "무시",
        "lot_id": lot.id,
    }


def _real_forwarder(app) -> TestClient:
    client = TestClient(app, base_url="https://testserver", headers=SAAS_CSRF)
    signup = client.post(
        "/api/auth/signup",
        json={
            "email": "preview-forwarder@example.com",
            "password": "password-123",
            "name": "실제 포워더",
            "org_name": "실제 포워더 회사",
            "org_type": "forwarder",
        },
    )
    assert signup.status_code == 201, signup.text
    resource = client.post("/api/saas/forwarder/resources", json=RESOURCE)
    assert resource.status_code == 201, resource.text
    return client


def _register_all(app) -> list[str]:
    scenario = get_scenario("default")
    assert scenario is not None
    ids = []
    for lot in scenario.lots:
        client = login_client(shipper_org_id(lot.shipper_id), app)
        response = client.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    return ids


def _shape(plan: dict) -> dict:
    return {
        "totals": {name: plan[name] for name in TOTALS},
        "assignments": sorted(
            (a["lot_id"], a["mode"], a["container_id"], a["pallets"]) for a in plan["assignments"]
        ),
        "containers": sorted(
            (c["container_id"], c["used"], c["pallets"]) for c in plan["containers"]
        ),
    }


def test_preview_does_not_save_and_matches_create(saas_app) -> None:
    ids = _register_all(saas_app)
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    forwarder = login_client(FORWARDER_ORG_ID, saas_app)
    assert platform.post(PLATFORM_PREVIEW, json={"lot_ids": ids}).status_code == 403
    preview = forwarder.post(PREVIEW, json={"lot_ids": ids})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert set(body) == {"scenario", "plan", "routes"}
    assert {lot["id"] for lot in body["scenario"]["lots"]} == set(ids)
    assert body["routes"]
    assert platform.get("/api/saas/platform/plans").json() == []
    lots = platform.get("/api/saas/platform/lots").json()
    assert {lot["status"] for lot in lots} == {"registered"}

    again = forwarder.post(PREVIEW, json={"lot_ids": ids})
    assert again.status_code == 200
    assert _shape(again.json()["plan"]) == _shape(body["plan"])
    reordered = forwarder.post(PREVIEW, json={"lot_ids": list(reversed(ids))})
    assert reordered.status_code == 200
    assert _shape(reordered.json()["plan"])["totals"] == _shape(body["plan"])["totals"]

    created = forwarder.post("/api/saas/forwarder/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    assert _shape(created.json()["plan"]) == _shape(body["plan"])


def test_preview_validates_lots(saas_app) -> None:
    shipper = login_client(SHIPPER_ORG_ID, saas_app)
    lot = next(lot for lot in get_scenario("default").lots if lot.shipper_id == "sh-jinju")
    registered = shipper.post("/api/saas/shipper/lots", json=_lot_body(lot))
    assert registered.status_code == 201, registered.text
    lot_id = registered.json()["registered"]["id"]
    forwarder = login_client(FORWARDER_ORG_ID, saas_app)

    assert forwarder.post(PREVIEW, json={"lot_ids": []}).status_code == 422
    assert forwarder.post(PREVIEW, json={"lot_ids": ["lot-missing"]}).status_code in (404, 422)
    assert shipper.post(PREVIEW, json={"lot_ids": [lot_id]}).status_code == 403

    real = _real_forwarder(saas_app)
    foreign = real.post(PREVIEW, json={"lot_ids": [lot.id]})
    assert foreign.status_code == 422
    assert foreign.json()["detail"].startswith("등록되지 않은 로트입니다")

    created = forwarder.post("/api/saas/forwarder/plans", json={"lot_ids": [lot_id]})
    assert created.status_code == 201
    assert forwarder.post(PREVIEW, json={"lot_ids": [lot_id]}).status_code == 409


def test_preview_requires_login_in_saas_mode(saas_app) -> None:
    anonymous = TestClient(saas_app, base_url="https://testserver")
    response = anonymous.post(PREVIEW, json={"lot_ids": ["x"]})
    assert response.status_code == 401
