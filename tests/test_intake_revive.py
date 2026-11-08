from fastapi.testclient import TestClient
from sqlalchemy import update
from test_saas_e2e_round1 import _confirm_all, _pallets, _reduced_plan

from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import LotRow, PlanRow

JINJU = "lot-jinju-sb-1"
SHIPPER_LOTS = "/api/saas/shipper/lots"


def _apply(platform: TestClient, plan_id: str, event: dict) -> str:
    posted = platform.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert posted.status_code == 200, posted.text
    alternative = next(a["id"] for a in posted.json()["alternatives"] if a["feasible"])
    applied = platform.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative})
    assert applied.status_code == 200, applied.text
    return applied.json()["active_order_id"]


def _registered_ids(plan_id: str) -> list[str]:
    with database.begin() as db:
        return list(db.get(PlanRow, plan_id).payload["registered_lot_ids"])


def _cancelled_in_p1(platform: TestClient, saas_login, forwarder: TestClient) -> str:
    plan_id, order_id = _reduced_plan(platform, saas_login)
    _confirm_all(platform, saas_login, forwarder, plan_id, order_id)
    order_id = _apply(platform, plan_id, {"type": "cancel", "lot_id": JINJU})
    _confirm_all(platform, saas_login, forwarder, plan_id, order_id)
    return plan_id


def test_revived_lot_survives_old_plan_commit_and_release(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    p1 = _cancelled_in_p1(platform_client, saas_login, forwarder_client)
    jinju = saas_login("shipper-jinju")
    assert _pallets(jinju, SHIPPER_LOTS, JINJU) == (8, "cancelled")
    assert JINJU in _registered_ids(p1)

    revived = jinju.patch(f"{SHIPPER_LOTS}/{JINJU}", json={"status": "registered"})
    assert revived.status_code == 200, revived.text
    p2 = platform_client.post("/api/saas/platform/plans", json={"lot_ids": [JINJU]})
    assert p2.status_code == 201, p2.text
    p2_id = p2.json()["plan_id"]
    assert _pallets(jinju, SHIPPER_LOTS, JINJU) == (8, "planned")

    order_id = _apply(
        platform_client, p1, {"type": "reduce", "lot_id": "lot-sangju-gr-1", "new_pallets": 10}
    )
    _confirm_all(platform_client, saas_login, forwarder_client, p1, order_id)
    assert _pallets(jinju, SHIPPER_LOTS, JINJU) == (8, "planned")
    assert jinju.delete(f"{SHIPPER_LOTS}/{JINJU}").status_code == 409

    assert platform_client.delete(f"/api/plans/{p2_id}").status_code == 204
    assert _pallets(jinju, SHIPPER_LOTS, JINJU) == (8, "registered")
    edited = jinju.patch(f"{SHIPPER_LOTS}/{JINJU}", json={"pallets": 2})
    assert edited.status_code == 200, edited.text


def test_reviving_lot_still_referenced_by_plan_is_rejected(
    platform_client: TestClient, saas_login
) -> None:
    p1, _ = _reduced_plan(platform_client, saas_login)
    assert JINJU in _registered_ids(p1)
    with database.begin() as db:
        db.execute(update(LotRow).where(LotRow.id == JINJU).values(status="cancelled"))
    jinju = saas_login("shipper-jinju")
    revived = jinju.patch(f"{SHIPPER_LOTS}/{JINJU}", json={"status": "registered"})
    assert revived.status_code == 409, revived.text
    assert _pallets(jinju, SHIPPER_LOTS, JINJU) == (8, "cancelled")


def _demo_plan_with_jinju(client: TestClient) -> None:
    demo = client.post("/api/plans", json={"scenario_id": "default"})
    assert demo.status_code == 201, demo.text
    assert JINJU in {lot["id"] for lot in demo.json()["scenario"]["lots"]}


def test_demo_plan_with_seed_id_does_not_pin_tenant_lot(
    client: TestClient, platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    _demo_plan_with_jinju(client)
    _cancelled_in_p1(platform_client, saas_login, forwarder_client)
    jinju = saas_login("shipper-jinju")
    assert _pallets(jinju, SHIPPER_LOTS, JINJU) == (8, "cancelled")

    revived = jinju.patch(f"{SHIPPER_LOTS}/{JINJU}", json={"status": "registered"})
    assert revived.status_code == 200, revived.text
    p2 = platform_client.post("/api/saas/platform/plans", json={"lot_ids": [JINJU]})
    assert p2.status_code == 201, p2.text
    assert _pallets(jinju, SHIPPER_LOTS, JINJU) == (8, "planned")
    _demo_plan_with_jinju(client)
    assert platform_client.delete(f"/api/plans/{p2.json()['plan_id']}").status_code == 204
    assert _pallets(jinju, SHIPPER_LOTS, JINJU) == (8, "registered")
