from fastapi.testclient import TestClient
from test_saas_api import REDUCE, _assign, _lot_body, _platform_plan, _scenario


def _demo_lot(client: TestClient, lot_id: str = "demo-extra-1") -> str:
    lot = _scenario().lots[0]
    body = _lot_body(lot, lot_id=lot_id, shipper=lot.shipper_id)
    response = client.post("/api/lots", json=body)
    assert response.status_code == 201, response.text
    return response.json()["registered"]["id"]


def test_shipper_cannot_unlock_planned_lot_through_generic_lots(
    platform_client: TestClient, saas_login, shipper_client: TestClient
) -> None:
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    path = "/api/lots/lot-jinju-sb-1"
    reopen = shipper_client.patch(path, json={"status": "registered", "pallets": 2})
    assert reopen.status_code == 409
    assert shipper_client.patch(path, json={"pallets": 2}).status_code == 409
    assert shipper_client.patch(path, json={"status": "shipped"}).status_code == 422
    assert shipper_client.delete(path).status_code == 409
    lot = shipper_client.get(path).json()
    assert lot["status"] == "planned"
    assert [p["plan_id"] for p in shipper_client.get("/api/saas/shipper/plans").json()] == [plan_id]


def test_shipper_generic_lot_routes_still_edit_registered_lots(
    shipper_client: TestClient,
) -> None:
    lot = _scenario().lots[0]
    created = shipper_client.post("/api/saas/shipper/lots", json=_lot_body(lot, lot_id=None))
    lot_id = created.json()["registered"]["id"]
    patched = shipper_client.patch(f"/api/lots/{lot_id}", json={"pallets": 4})
    assert patched.status_code == 200, patched.text
    assert patched.json()["pallets"] == 4
    assert (
        shipper_client.patch(f"/api/lots/{lot_id}", json={"status": "planned"}).status_code == 422
    )
    assert shipper_client.delete(f"/api/lots/{lot_id}").status_code == 204
    assert shipper_client.delete(f"/api/lots/{lot_id}").status_code == 404


def test_anonymous_lot_routes_unchanged(client: TestClient) -> None:
    lot_id = _demo_lot(client)
    planned = client.patch(f"/api/lots/{lot_id}", json={"status": "planned"})
    assert planned.status_code == 200
    assert client.delete(f"/api/lots/{lot_id}").status_code == 204


def test_platform_plan_rejects_demo_lots(
    client: TestClient, platform_client: TestClient, shipper_client: TestClient
) -> None:
    demo_id = _demo_lot(client)
    explicit = platform_client.post("/api/saas/platform/plans", json={"lot_ids": [demo_id]})
    assert explicit.status_code == 422
    generic = platform_client.post("/api/plans", json={"lot_ids": [demo_id]})
    assert generic.status_code == 422

    lot = _scenario().lots[1]
    own = shipper_client.post("/api/saas/shipper/lots", json=_lot_body(lot, lot_id="own-1"))
    assert own.status_code == 201, own.text
    created = platform_client.post(
        "/api/saas/platform/plans", json={"lot_ids": ["own-1"], "include_registered": True}
    )
    assert created.status_code == 201, created.text
    assert created.json()["registered_lot_ids"] == ["own-1"]
    assert client.get(f"/api/lots/{demo_id}").json()["status"] == "registered"

    only_demo = platform_client.post(
        "/api/saas/platform/plans", json={"lot_ids": [], "include_registered": True}
    )
    assert only_demo.status_code == 422


def test_forwarder_views_hide_shipper_money(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    _assign(plan_id)
    assert (
        platform_client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE}).status_code
        == 200
    )
    applied = platform_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"}
    )
    assert applied.status_code == 200, applied.text

    detail = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}").json()
    assert detail["plan"]["objective"] is None
    assert detail["history"]
    for entry in detail["history"]:
        assert entry["cost_before"] is None
        assert entry["cost_after"] is None
        assert entry["cost_delta"] is None

    orders = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    assert orders
    for order in orders:
        for confirmation in order["shipper_confirmations"]:
            for name in ("cost_before", "cost_after", "cost_delta", "net_cost_delta"):
                assert confirmation[name] is None
        if order["cancellation"] is not None:
            assert order["cancellation"]["fee"] is None
