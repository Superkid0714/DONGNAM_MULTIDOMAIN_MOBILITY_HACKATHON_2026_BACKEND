from fastapi.testclient import TestClient
from saas_fixtures import accept_plan, open_quantity_window, queue_quantity
from sqlalchemy import update
from test_saas_api import _forwarder_plan
from test_saas_e2e_round1 import _confirm_all, _pallets, _reduced_plan

from freshswitch_backend.api.lots import LOT_STILL_REFERENCED
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import LotRow, PlanRow

AIR_LOT = "lot-miryang-sb-2"
MIRYANG = "shipper-miryang"
SHIPPER_LOTS = "/api/saas/shipper/lots"


def _apply(forwarder: TestClient, plan_id: str, event: dict) -> str:
    posted = forwarder.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert posted.status_code == 200, posted.text
    alternative = next(a["id"] for a in posted.json()["alternatives"] if a["feasible"])
    applied = forwarder.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative})
    assert applied.status_code == 200, applied.text
    return applied.json()["active_order_id"]


def _registered_ids(plan_id: str) -> list[str]:
    with database.begin() as db:
        return list(db.get(PlanRow, plan_id).payload["registered_lot_ids"])


def _cancelled_in_p1(saas_login, forwarder: TestClient) -> str:
    plan = _forwarder_plan(saas_login)
    plan_id = plan["plan_id"]
    air = {a["lot_id"] for a in plan["plan"]["assignments"] if a["mode"] == "air"}
    assert AIR_LOT in air
    accept_plan(saas_login, plan_id)
    open_quantity_window()
    order_id = _apply(forwarder, plan_id, {"type": "cancel", "lot_id": AIR_LOT})
    _confirm_all(forwarder, saas_login, forwarder, plan_id, order_id)
    return plan_id


def test_revived_lot_survives_old_plan_commit_and_release(
    saas_login, forwarder_client: TestClient
) -> None:
    p1 = _cancelled_in_p1(saas_login, forwarder_client)
    miryang = saas_login(MIRYANG)
    assert _pallets(miryang, SHIPPER_LOTS, AIR_LOT) == (2, "cancelled")
    assert AIR_LOT in _registered_ids(p1)

    revived = miryang.patch(f"{SHIPPER_LOTS}/{AIR_LOT}", json={"status": "registered"})
    assert revived.status_code == 200, revived.text
    p2 = forwarder_client.post("/api/saas/forwarder/plans", json={"lot_ids": [AIR_LOT]})
    assert p2.status_code == 201, p2.text
    p2_id = p2.json()["plan_id"]
    assert _pallets(miryang, SHIPPER_LOTS, AIR_LOT) == (2, "planned")

    queue_quantity(saas_login("shipper-sangju"), p1, "lot-sangju-gr-1", 10)
    order_id = _apply(
        forwarder_client, p1, {"type": "reduce", "lot_id": "lot-sangju-gr-1", "new_pallets": 10}
    )
    _confirm_all(forwarder_client, saas_login, forwarder_client, p1, order_id)
    assert _pallets(miryang, SHIPPER_LOTS, AIR_LOT) == (2, "planned")
    assert miryang.delete(f"{SHIPPER_LOTS}/{AIR_LOT}").status_code == 409

    assert forwarder_client.delete(f"/api/plans/{p2_id}").status_code == 204
    assert _pallets(miryang, SHIPPER_LOTS, AIR_LOT) == (2, "registered")
    edited = miryang.patch(f"{SHIPPER_LOTS}/{AIR_LOT}", json={"pallets": 1})
    assert edited.status_code == 200, edited.text


def test_reviving_lot_still_referenced_by_plan_is_rejected(
    forwarder_client: TestClient, saas_login
) -> None:
    p1, _ = _reduced_plan(forwarder_client, saas_login)
    assert AIR_LOT in _registered_ids(p1)
    with database.begin() as db:
        db.execute(update(LotRow).where(LotRow.id == AIR_LOT).values(status="cancelled"))
    miryang = saas_login(MIRYANG)
    revived = miryang.patch(f"{SHIPPER_LOTS}/{AIR_LOT}", json={"status": "registered"})
    assert revived.status_code == 409, revived.text
    assert revived.json()["detail"] == LOT_STILL_REFERENCED
    assert _pallets(miryang, SHIPPER_LOTS, AIR_LOT) == (2, "cancelled")


def _demo_plan_with_air_lot(client: TestClient) -> None:
    demo = client.post("/api/plans", json={"scenario_id": "default"})
    assert demo.status_code == 201, demo.text
    assert AIR_LOT in {lot["id"] for lot in demo.json()["scenario"]["lots"]}


def test_demo_plan_with_seed_id_does_not_pin_tenant_lot(
    client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    _demo_plan_with_air_lot(client)
    _cancelled_in_p1(saas_login, forwarder_client)
    miryang = saas_login(MIRYANG)
    assert _pallets(miryang, SHIPPER_LOTS, AIR_LOT) == (2, "cancelled")

    revived = miryang.patch(f"{SHIPPER_LOTS}/{AIR_LOT}", json={"status": "registered"})
    assert revived.status_code == 200, revived.text
    p2 = forwarder_client.post("/api/saas/forwarder/plans", json={"lot_ids": [AIR_LOT]})
    assert p2.status_code == 201, p2.text
    assert _pallets(miryang, SHIPPER_LOTS, AIR_LOT) == (2, "planned")
    _demo_plan_with_air_lot(client)
    assert forwarder_client.delete(f"/api/plans/{p2.json()['plan_id']}").status_code == 204
    assert _pallets(miryang, SHIPPER_LOTS, AIR_LOT) == (2, "registered")
