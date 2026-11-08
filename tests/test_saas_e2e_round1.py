import json
import re

from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import PlanForwarderRow

POHANG_REDUCE = {"type": "reduce", "lot_id": "lot-pohang-gr-1", "new_pallets": 2}
MONEY = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:만\s*)?원")


def _lot_body(lot) -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "sell_window": lot.sell_window,
        "lot_id": lot.id,
    }


def _reduced_plan(platform_client: TestClient, saas_login) -> tuple[str, str]:
    scenario = get_scenario("default")
    assert scenario is not None
    ids = []
    for lot in scenario.lots:
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    created = platform_client.post("/api/saas/platform/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    with database.begin() as db:
        db.add(PlanForwarderRow(plan_id=plan_id, forwarder_org_id=FORWARDER_ORG_ID))
    event = platform_client.post(f"/api/plans/{plan_id}/events", json={"event": POHANG_REDUCE})
    assert event.status_code == 200, event.text
    applied = platform_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"}
    )
    assert applied.status_code == 200, applied.text
    return plan_id, applied.json()["active_order_id"]


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    return []


def _money_texts(payload) -> list[str]:
    return [s for s in _strings(payload) if MONEY.search(s)]


def _settlement_texts(orders: list[dict]) -> list[str]:
    return [
        action["description"]
        for order in orders
        for action in order["automatic"]
        if action["kind"] == "settlement_recompute"
    ]


def test_forwarder_free_text_has_no_settlement_amounts(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, _ = _reduced_plan(platform_client, saas_login)
    platform_orders = platform_client.get(f"/api/plans/{plan_id}/orders").json()
    assert any(MONEY.search(t) for t in _settlement_texts(platform_orders))

    orders = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    texts = _settlement_texts(orders)
    assert texts and all(not MONEY.search(t) for t in texts)
    assert _money_texts([o["automatic"] for o in orders]) == []

    plan = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}").json()
    explanation = plan["history"][0]["explanation"]
    assert explanation["shipper_messages"] == []
    assert not MONEY.search(explanation["operator_summary"])
    assert "감량" in explanation["operator_summary"]

    platform_plan = platform_client.get(f"/api/plans/{plan_id}").json()
    assert platform_plan["history"][0]["explanation"]["shipper_messages"]


def test_shipper_free_text_has_only_own_amounts(platform_client: TestClient, saas_login) -> None:
    plan_id, _ = _reduced_plan(platform_client, saas_login)
    platform_plan = platform_client.get(f"/api/plans/{plan_id}").json()
    platform_messages = platform_plan["history"][0]["explanation"]["shipper_messages"]
    sangju_message = next(m for m in platform_messages if m["shipper_id"] == "sh-sangju")
    sangju_amounts = MONEY.findall(sangju_message["message"])
    assert sangju_amounts

    pohang = saas_login("shipper-pohang")
    plan = pohang.get(f"/api/saas/shipper/plans/{plan_id}").json()
    explanation = plan["history"][0]["explanation"]
    assert [m["shipper_id"] for m in explanation["shipper_messages"]] == ["sh-pohang"]
    assert not MONEY.search(explanation["operator_summary"])
    text = json.dumps(plan, ensure_ascii=False)
    own = set(MONEY.findall(explanation["shipper_messages"][0]["message"]))
    for amount in set(sangju_amounts) - own:
        assert amount not in text

    orders = pohang.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    assert orders
    for description in _settlement_texts(orders):
        assert "플랫폼 성과보수" not in description
        assert "화주 총비용" not in description
    for order in orders:
        cancellation = order["cancellation"]
        if cancellation is None:
            continue
        assert all(t["shipper_id"] == "sh-pohang" for t in cancellation["transfers"])
        receivers = [t["amount"] for t in cancellation["transfers"]]
        for description in _settlement_texts([order]):
            amounts = {int(a.replace(",", "")) for a in re.findall(r"([\d,]+)원", description)}
            assert amounts <= {cancellation["fee"], *receivers}

    sangju = saas_login("shipper-sangju")
    for order in sangju.get(f"/api/saas/shipper/plans/{plan_id}/orders").json():
        cancellation = order["cancellation"]
        if cancellation is not None:
            assert cancellation["fee"] is None
            assert all(t["shipper_id"] == "sh-sangju" for t in cancellation["transfers"])


def test_shipper_plan_detail_holds_only_own_settlement(
    platform_client: TestClient, saas_login
) -> None:
    plan_id, _ = _reduced_plan(platform_client, saas_login)
    full = platform_client.get(f"/api/plans/{plan_id}").json()
    own_row = next(s for s in full["plan"]["settlements"] if s["shipper_id"] == "sh-pohang")
    own_cost = next(c for c in full["plan"]["shipper_costs"] if c["shipper_id"] == "sh-pohang")

    pohang = saas_login("shipper-pohang")
    body = pohang.get(f"/api/saas/shipper/plans/{plan_id}").json()
    plan = body["plan"]
    assert plan["settlements"] == [own_row]
    assert [c["shipper_id"] for c in plan["shipper_costs"]] == ["sh-pohang"]
    assert plan["total_baseline_cost"] == own_row["baseline_cost"]
    assert plan["total_shared_cost"] == own_row["shared_cost"]
    assert plan["total_savings"] == own_row["savings"]
    assert plan["total_fee"] == own_row["fee"]
    assert plan["total_operational_cost"] == own_cost["operational"]["total"]
    assert plan["objective"] is None
    assert plan["total_savings"] < full["plan"]["total_savings"]
    for container in plan["container_settlements"]:
        assert [r["shipper_id"] for r in container["shippers"]] == ["sh-pohang"]
        assert container["total_savings"] == container["shippers"][0]["savings"]
        assert container["total_fee"] == container["shippers"][0]["fee"]
    assert plan["container_settlements"]
    assert all(
        entry[name] is None
        for entry in body["history"]
        for name in ("cost_before", "cost_after", "cost_delta")
    )
    others = [s for s in full["plan"]["settlements"] if s["shipper_id"] != "sh-pohang"]
    text = json.dumps(plan["settlements"] + plan["container_settlements"])
    for row in others:
        assert str(row["shared_cost"]) not in text
    assert sum(c["pallets"] for c in plan["containers"]) == sum(
        c["pallets"] for c in full["plan"]["containers"]
    )


def _confirm_all(
    platform_client: TestClient,
    saas_login,
    forwarder_client: TestClient,
    plan_id: str,
    order_id: str,
) -> None:
    order = platform_client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    for confirmation in order["shipper_confirmations"]:
        shipper = saas_login(shipper_org_id(confirmation["shipper_id"]))
        answered = shipper.post(
            f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": True, "shipper_id": confirmation["shipper_id"]},
        )
        assert answered.status_code == 200, answered.text
    done = forwarder_client.post(
        f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm", json={"accept": True}
    )
    assert done.status_code == 200, done.text


def _pallets(client: TestClient, path: str, lot_id: str) -> tuple[int, str]:
    lot = next(x for x in client.get(path).json() if x["id"] == lot_id)
    return lot["pallets"], lot["status"]


def test_confirmed_reduce_updates_lot_record(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, order_id = _reduced_plan(platform_client, saas_login)
    pohang = saas_login("shipper-pohang")
    lot_id = POHANG_REDUCE["lot_id"]
    assert _pallets(pohang, "/api/saas/shipper/lots", lot_id) == (5, "planned")

    _confirm_all(platform_client, saas_login, forwarder_client, plan_id, order_id)

    assert _pallets(pohang, "/api/saas/shipper/lots", lot_id) == (2, "planned")
    assert _pallets(platform_client, "/api/saas/platform/lots", lot_id) == (2, "planned")
    sangju = saas_login("shipper-sangju")
    assert _pallets(sangju, "/api/saas/shipper/lots", "lot-sangju-gr-1") == (13, "planned")


def test_rejected_reduce_keeps_lot_record(platform_client: TestClient, saas_login) -> None:
    plan_id, order_id = _reduced_plan(platform_client, saas_login)
    order = platform_client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    first = order["shipper_confirmations"][0]["shipper_id"]
    shipper = saas_login(shipper_org_id(first))
    rejected = shipper.post(
        f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
        json={"accept": False, "shipper_id": first},
    )
    assert rejected.status_code == 200, rejected.text
    pohang = saas_login("shipper-pohang")
    assert _pallets(pohang, "/api/saas/shipper/lots", POHANG_REDUCE["lot_id"]) == (5, "planned")


def test_confirmed_cancel_marks_lot_cancelled(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, order_id = _reduced_plan(platform_client, saas_login)
    _confirm_all(platform_client, saas_login, forwarder_client, plan_id, order_id)
    cancel = {"type": "cancel", "lot_id": "lot-jinju-sb-1"}
    event = platform_client.post(f"/api/plans/{plan_id}/events", json={"event": cancel})
    assert event.status_code == 200, event.text
    alternative = next(a["id"] for a in event.json()["alternatives"] if a["feasible"])
    applied = platform_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative}
    )
    assert applied.status_code == 200, applied.text
    _confirm_all(
        platform_client, saas_login, forwarder_client, plan_id, applied.json()["active_order_id"]
    )
    jinju = saas_login("shipper-jinju")
    assert _pallets(jinju, "/api/saas/shipper/lots", "lot-jinju-sb-1") == (8, "cancelled")


def test_demo_confirm_leaves_demo_lots_alone(client: TestClient) -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    lot = scenario.lots[-1]
    saved = client.post(
        "/api/intake/lots",
        json={
            **_lot_body(lot),
            "lot_id": "lot-demo-sync-1",
            "shipper": lot.shipper_id,
            "save": True,
        },
    )
    assert saved.status_code == 200, saved.text
    created = client.post("/api/plans", json={"lot_ids": ["lot-demo-sync-1"]})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    reduce = {"type": "reduce", "lot_id": "lot-demo-sync-1", "new_pallets": 1}
    event = client.post(f"/api/plans/{plan_id}/events", json={"event": reduce})
    assert event.status_code == 200, event.text
    alternative = next(a["id"] for a in event.json()["alternatives"] if a["feasible"])
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative})
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["active_order_id"]
    order = client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    path = f"/api/plans/{plan_id}/orders/{order_id}/confirm"
    for confirmation in order["shipper_confirmations"]:
        body = {"actor": "shipper", "shipper_id": confirmation["shipper_id"], "accept": True}
        assert client.post(path, json=body).status_code == 200
    done = client.post(path, json={"actor": "forwarder", "accept": True})
    assert done.status_code == 200, done.text
    assert done.json()["status"] in ("forwarder_confirmed", "executed")
    assert client.get("/api/lots/lot-demo-sync-1").json()["pallets"] == lot.pallets
