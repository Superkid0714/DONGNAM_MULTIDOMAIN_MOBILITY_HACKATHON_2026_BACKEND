from datetime import date

from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID
from sqlalchemy import update

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import LotRow, PlanForwarderRow

PLATFORM = "/api/saas/platform/plans"
EXTRA = "lot-jinju-sb-extra"


def _lot_body(lot, lot_id: str | None = None) -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "sell_window": lot.sell_window,
        "lot_id": lot_id or lot.id,
    }


def _register(saas_login, lot, lot_id: str | None = None) -> str:
    response = saas_login(shipper_org_id(lot.shipper_id)).post(
        "/api/saas/shipper/lots", json=_lot_body(lot, lot_id)
    )
    assert response.status_code == 201, response.text
    return response.json()["registered"]["id"]


def _plan_with_spare(
    platform_client: TestClient, saas_login, skip: tuple[str, ...] = ()
) -> tuple[str, str]:
    scenario = get_scenario("default")
    assert scenario is not None
    ids = [_register(saas_login, lot) for lot in scenario.lots if lot.shipper_id not in skip]
    jinju = next(lot for lot in scenario.lots if lot.shipper_id == "sh-jinju")
    spare = jinju.model_copy(update={"pallets": 2})
    extra = _register(saas_login, spare, EXTRA)
    created = platform_client.post(PLATFORM, json={"lot_ids": ids, "auto_accept": True})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    with database.begin() as db:
        db.add(PlanForwarderRow(plan_id=plan_id, forwarder_org_id=FORWARDER_ORG_ID))
    return plan_id, extra


def _status(saas_login, lot_id: str, org_id: str = "shipper-jinju") -> str:
    lots = saas_login(org_id).get("/api/saas/shipper/lots").json()
    return next(lot["status"] for lot in lots if lot["id"] == lot_id)


def _apply_add(platform_client: TestClient, plan_id: str, extra: str) -> dict:
    computed = platform_client.post(f"{PLATFORM}/{plan_id}/add-lot", json={"lot_id": extra})
    assert computed.status_code == 200, computed.text
    feasible = next(a["id"] for a in computed.json()["alternatives"] if a["feasible"])
    applied = platform_client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": feasible})
    assert applied.status_code == 200, applied.text
    return applied.json()


def _reject_order(platform_client: TestClient, saas_login, plan_id: str, order_id: str) -> None:
    order = platform_client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    if order["shipper_confirmations"]:
        target = order["shipper_confirmations"][0]["shipper_id"]
        rejected = saas_login(shipper_org_id(target)).post(
            f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": False, "shipper_id": target, "note": "안 됩니다"},
        )
    else:
        rejected = saas_login("forwarder-dongnam").post(
            f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": False, "note": "안 됩니다"},
        )
    assert rejected.status_code == 200, rejected.text


def test_add_registered_lot_claims_only_on_apply(platform_client: TestClient, saas_login) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login)

    computed = platform_client.post(f"{PLATFORM}/{plan_id}/add-lot", json={"lot_id": extra})
    assert computed.status_code == 200, computed.text
    result = computed.json()
    assert result["event"]["type"] == "add_lot"
    assert result["event"]["lot"]["id"] == extra
    assert _status(saas_login, extra) == "registered"
    session = platform_client.get(f"/api/plans/{plan_id}").json()
    assert extra not in session["registered_lot_ids"]

    feasible = next(a["id"] for a in result["alternatives"] if a["feasible"])
    applied = platform_client.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": feasible, "replan_id": result["replan_id"]},
    )
    assert applied.status_code == 200, applied.text
    assert extra in applied.json()["registered_lot_ids"]
    assert extra in {lot["id"] for lot in applied.json()["scenario"]["lots"]}
    assert _status(saas_login, extra) == "planned"

    twice = platform_client.post(f"{PLATFORM}/{plan_id}/add-lot", json={"lot_id": extra})
    assert twice.status_code == 409


def test_add_lot_rejects_taken_unknown_and_foreign_lots(
    platform_client: TestClient, saas_login
) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login)
    unknown = platform_client.post(f"{PLATFORM}/{plan_id}/add-lot", json={"lot_id": "nope"})
    assert unknown.status_code == 404
    inside = platform_client.post(
        f"{PLATFORM}/{plan_id}/add-lot", json={"lot_id": "lot-jinju-sb-1"}
    )
    assert inside.status_code == 409
    with database.begin() as db:
        db.execute(update(LotRow).where(LotRow.id == extra).values(status="planned"))
    taken = platform_client.post(f"{PLATFORM}/{plan_id}/add-lot", json={"lot_id": extra})
    assert taken.status_code == 409
    missing_plan = platform_client.post(f"{PLATFORM}/missing/add-lot", json={"lot_id": extra})
    assert missing_plan.status_code == 404


def test_apply_conflicts_when_lot_was_taken_meanwhile(
    platform_client: TestClient, saas_login
) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login)
    computed = platform_client.post(f"{PLATFORM}/{plan_id}/add-lot", json={"lot_id": extra})
    assert computed.status_code == 200, computed.text
    with database.begin() as db:
        db.execute(update(LotRow).where(LotRow.id == extra).values(status="planned"))
    feasible = next(a["id"] for a in computed.json()["alternatives"] if a["feasible"])
    applied = platform_client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": feasible})
    assert applied.status_code == 409
    session = platform_client.get(f"/api/plans/{plan_id}").json()
    assert session["version"] == 1
    assert extra not in session["registered_lot_ids"]


def test_rejected_add_lot_order_releases_the_lot(platform_client: TestClient, saas_login) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login)
    applied = _apply_add(platform_client, plan_id, extra)
    _reject_order(platform_client, saas_login, plan_id, applied["active_order_id"])
    session = platform_client.get(f"/api/plans/{plan_id}").json()
    assert session["status"] == "committed"
    assert extra not in session["registered_lot_ids"]
    assert extra not in {lot["id"] for lot in session["scenario"]["lots"]}
    assert _status(saas_login, extra) == "registered"


def test_demo_inline_add_lot_unchanged(client: TestClient) -> None:
    created = client.post("/api/plans", json={"scenario_id": "default"})
    plan_id = created.json()["plan_id"]
    lot = {
        "id": "lot-demo-extra",
        "shipper_id": "sh-jinju",
        "product_id": "strawberry",
        "pallets": 2,
        "ready_date": date(2026, 11, 16).isoformat(),
        "destination": "HKG",
        "sell_by_date": date(2026, 11, 30).isoformat(),
    }
    scenario = get_scenario("default")
    assert scenario is not None
    lot["destination"] = scenario.lots[0].destination
    computed = client.post(
        f"/api/plans/{plan_id}/events", json={"event": {"type": "add_lot", "lot": lot}}
    )
    assert computed.status_code == 200, computed.text
    feasible = next(a["id"] for a in computed.json()["alternatives"] if a["feasible"])
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": feasible})
    assert applied.status_code == 200, applied.text
    assert applied.json()["registered_lot_ids"] == []


def test_rollback_after_superseded_add_lot_releases_the_lot(
    platform_client: TestClient, saas_login
) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login)
    _apply_add(platform_client, plan_id, extra)
    assert _status(saas_login, extra) == "planned"
    event = platform_client.post(
        f"/api/plans/{plan_id}/events",
        json={"event": {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}},
    )
    assert event.status_code == 200, event.text
    feasible = next(a["id"] for a in event.json()["alternatives"] if a["feasible"])
    applied = platform_client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": feasible})
    assert applied.status_code == 200, applied.text
    _reject_order(platform_client, saas_login, plan_id, applied.json()["active_order_id"])
    session = platform_client.get(f"/api/plans/{plan_id}").json()
    assert session["version"] == 4
    assert extra not in {lot["id"] for lot in session["scenario"]["lots"]}
    assert extra not in session["registered_lot_ids"]
    assert _status(saas_login, extra) == "registered"


def test_add_lot_asks_a_new_shipper_to_confirm(platform_client: TestClient, saas_login) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login, skip=("sh-jinju",))
    _apply_add(platform_client, plan_id, extra)
    state = platform_client.get(f"{PLATFORM}/{plan_id}/confirmation").json()
    jinju = next(c for c in state["confirmations"] if c["shipper_id"] == "sh-jinju")
    assert jinju["status"] == "pending"
    assert jinju["lot_ids"] == [extra]
    assert state["status"] == "pending_confirmation"
    sent = saas_login("shipper-jinju").get(f"/api/saas/shipper/notifications?plan_id={plan_id}")
    assert "plan_confirmation_requested" in [n["kind"] for n in sent.json()]
    attention = platform_client.get("/api/saas/platform/plan-confirmations").json()
    assert plan_id in [s["plan_id"] for s in attention]
