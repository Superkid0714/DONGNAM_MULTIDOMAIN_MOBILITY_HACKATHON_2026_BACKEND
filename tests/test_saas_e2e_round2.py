from fastapi.testclient import TestClient
from test_saas_api import _forwarder_plan


def test_shipper_plan_detail_holds_only_own_collection(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    full = platform_client.get(f"/api/plans/{plan_id}").json()["plan"]
    own_cost = next(c for c in full["shipper_costs"] if c["shipper_id"] == "sh-pohang")
    own_lots = [c for c in full["lot_collections"] if c["shipper_id"] == "sh-pohang"]
    assert own_lots
    assert len(own_lots) < len(full["lot_collections"])

    plan = saas_login("shipper-pohang").get(f"/api/saas/shipper/plans/{plan_id}").json()["plan"]
    assert plan["lot_collections"] == own_lots
    assert plan["total_collection_estimate"] == own_cost["operational"]["collection_estimate"]
    assert plan["total_collection_estimate"] < full["total_collection_estimate"]
    if full["total_collection_route"] is None:
        assert plan["total_collection_route"] is None
    else:
        assert plan["total_collection_route"] == own_cost["operational"]["collection"]
        assert plan["total_collection_route"] < full["total_collection_route"]


def test_alert_stream_disables_proxy_buffering(client: TestClient) -> None:
    stream = client.get("/api/coldchain/alerts/stream", params={"since": 0, "timeout": 0})
    assert stream.status_code == 200
    assert stream.headers["cache-control"] == "no-cache, no-transform"
    assert stream.headers["x-accel-buffering"] == "no"


def test_shipper_free_text_scenario_description_is_blank(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    jinju = saas_login("shipper-jinju")
    catalog = jinju.get("/api/saas/catalog").json()
    assert catalog["description"] == ""
    assert [s["id"] for s in catalog["shippers"]] == ["sh-jinju"]
    detail = jinju.get(f"/api/saas/shipper/plans/{plan_id}").json()
    assert detail["scenario"]["description"] == ""
    platform = platform_client.get("/api/saas/catalog").json()
    assert platform["description"] == ""
    assert len(platform["shippers"]) > 1
