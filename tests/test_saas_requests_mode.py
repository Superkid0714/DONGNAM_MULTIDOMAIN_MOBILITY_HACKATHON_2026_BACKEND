import json

from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    accept_plan,
    open_quantity_window,
    queue_quantity,
    seed_client,
)
from test_saas_api import REDUCE, _forwarder_plan

from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app

SAAS_HEAD = "[SI 정정 + VGM 재제출 요청]"


def _applied(client: TestClient, plan_id: str) -> None:
    event = client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE})
    assert event.status_code == 200, event.text
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert applied.status_code == 200, applied.text


def _quantity_ready(saas_login, plan_id: str) -> None:
    accept_plan(saas_login, plan_id)
    open_quantity_window()
    queue_quantity(
        saas_login("shipper-sancheong"), plan_id, REDUCE["lot_id"], REDUCE["new_pallets"]
    )


def _amendment(order: dict) -> dict:
    return next(r for r in order["requests"] if r["kind"] == "booking_amendment")


def test_demo_plan_orders_keep_old_wording(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _applied(client, plan_id)
    orders = client.get(f"/api/plans/{plan_id}/orders").json()
    order = orders[-1]
    single = client.get(f"/api/plans/{plan_id}/orders/{order['order_id']}").json()
    assert single == order
    amendment = _amendment(order)
    assert amendment["text"].startswith("[부킹 변경 요청]")
    assert "17→17파렛트(7,055→7,055kg)" in amendment["text"]
    assert SAAS_HEAD not in json.dumps(orders, ensure_ascii=False)


def test_saas_plan_orders_use_gross_weight_wording(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    _quantity_ready(saas_login, plan_id)
    _applied(forwarder_client, plan_id)
    orders = platform_client.get(f"/api/plans/{plan_id}/orders")
    assert orders.status_code == 200, orders.text
    order = orders.json()[-1]
    amendment = _amendment(order)
    assert amendment["text"].startswith(SAAS_HEAD)
    assert "화물 총중량 8,075→8,075kg" in amendment["text"]
    assert "부킹 변경" not in amendment["text"]
    single = platform_client.get(f"/api/plans/{plan_id}/orders/{order['order_id']}")
    assert single.status_code == 200, single.text
    assert single.json() == order
    sent = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    forwarded = next(r for r in sent[-1]["requests"] if r["request_id"] == amendment["request_id"])
    assert forwarded["text"] == amendment["text"]
    missing = platform_client.get(f"/api/plans/{plan_id}/orders/missing")
    assert missing.status_code == 404


def test_saas_mode_app_rebuilds_plan_orders(saas_login) -> None:
    saas_app = create_app(get_settings().model_copy(update={"saas_mode": True}))
    platform = seed_client(PLATFORM_ORG_ID, saas_app)
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    _quantity_ready(saas_login, plan_id)
    _applied(seed_client(FORWARDER_ORG_ID, saas_app), plan_id)
    order = platform.get(f"/api/plans/{plan_id}/orders").json()[-1]
    assert _amendment(order)["text"].startswith(SAAS_HEAD)
    single = platform.get(f"/api/plans/{plan_id}/orders/{order['order_id']}").json()
    assert _amendment(single)["text"] == _amendment(order)["text"]
    anonymous = TestClient(saas_app, base_url="https://testserver")
    assert anonymous.get(f"/api/plans/{plan_id}/orders").status_code == 401
