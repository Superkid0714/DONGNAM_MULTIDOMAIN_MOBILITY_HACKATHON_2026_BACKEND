from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import accept_plan, open_quantity_window, queue_quantity
from test_add_lot import _apply_add, _plan_with_spare
from test_plan_confirm import ATTENTION
from test_saas_api import _register_all

from freshswitch_backend.execution.models import OrderLogEntry
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.planning import store as store_module
from freshswitch_backend.planning.store import store
from freshswitch_backend.quality import quality_history

LATE = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
DELAY = {"type": "delay", "voyage_id": "V-2611A", "new_etd": "2026-11-19T18:00:00+09:00"}


def _create(client: TestClient) -> str:
    response = client.post("/api/plans", json={"scenario_id": "default"})
    assert response.status_code == 201 or response.status_code == 200, response.text
    return response.json()["plan_id"]


def _event(client: TestClient, plan_id: str, event: dict) -> dict:
    response = client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert response.status_code == 200, response.text
    return response.json()


def _apply(client: TestClient, plan_id: str, result: dict, alternative_id: str):
    return client.post(
        f"/api/plans/{plan_id}/apply",
        json={"replan_id": result["replan_id"], "alternative_id": alternative_id},
    )


def _charged(result: dict) -> str:
    for alternative in result["alternatives"]:
        charge = alternative["diff"]["cancellation"]
        if alternative["feasible"] and charge and charge["fee"] > 0:
            return alternative["id"]
    raise AssertionError("부담금이 생기는 실행 가능한 대안이 없습니다")


def _orders(client: TestClient, plan_id: str) -> list[dict]:
    return client.get(f"/api/plans/{plan_id}/orders").json()


def _expected(charges: list[dict]) -> dict[str, int]:
    adjustments: dict[str, int] = {}
    for charge in charges:
        payer = charge["payer_shipper_id"]
        adjustments[payer] = adjustments.get(payer, 0) + charge["fee"]
        for transfer in charge["transfers"]:
            sid = transfer["shipper_id"]
            adjustments[sid] = adjustments.get(sid, 0) - transfer["amount"]
    return adjustments


def _assert_cumulative(order: dict, charges: list[dict]) -> None:
    confirmations = {c["shipper_id"]: c for c in order["shipper_confirmations"]}
    expected = _expected(charges)
    for charge in charges:
        payer = charge["payer_shipper_id"]
        assert payer in confirmations, payer
        assert "cancellation_fee" in confirmations[payer]["reasons"]
        assert payer not in order["notified_shipper_ids"]
    for sid, item in confirmations.items():
        assert item["cancellation_adjustment"] == expected.get(sid, 0), sid
        assert item["net_cost_delta"] == item["cost_delta"] + item["cancellation_adjustment"]
    recompute = next(a for a in order["automatic"] if a["kind"] == "settlement_recompute")
    for charge in charges:
        assert f"{charge['fee']:,}원 →" in recompute["description"]


def test_chained_grape_reductions_keep_every_charge_and_payer(client: TestClient) -> None:
    plan_id = _create(client)
    steps = [
        {"type": "reduce", "lot_id": "lot-sangju-gr-1", "new_pallets": 10},
        {"type": "reduce", "lot_id": "lot-pohang-gr-1", "new_pallets": 3},
        {"type": "reduce", "lot_id": "lot-sangju-gr-1", "new_pallets": 8},
    ]
    charges: list[dict] = []
    for step in steps:
        result = _event(client, plan_id, step)
        chosen = next(a for a in result["alternatives"] if a["id"] == "sail_as_is")
        assert chosen["diff"]["cancellation"]["fee"] > 0
        assert _apply(client, plan_id, result, "sail_as_is").status_code == 200
        charges.append(chosen["diff"]["cancellation"])
        order = _orders(client, plan_id)[-1]
        assert order["status"] == "proposed"
        assert order["cancellation"] == charges[-1]
        _assert_cumulative(order, charges)
    payers = {c["payer_shipper_id"] for c in charges}
    assert payers == {"sh-sangju", "sh-pohang"}


def test_cancel_cancel_delay_chain_keeps_first_charge(client: TestClient) -> None:
    plan_id = _create(client)
    charges: list[dict] = []
    for lot_id in ("lot-miryang-sb-1", "lot-sancheong-sb-1"):
        result = _event(client, plan_id, {"type": "cancel", "lot_id": lot_id})
        chosen = _charged(result)
        charges.append(
            next(a for a in result["alternatives"] if a["id"] == chosen)["diff"]["cancellation"]
        )
        assert _apply(client, plan_id, result, chosen).status_code == 200
    delay = _event(client, plan_id, DELAY)
    assert delay["recommended_id"]
    assert _apply(client, plan_id, delay, delay["recommended_id"]).status_code == 200
    order = _orders(client, plan_id)[-1]
    assert order["cancellation"] == charges[-1]
    _assert_cumulative(order, charges)


def test_commit_resets_cumulative_charges(client: TestClient) -> None:
    plan_id = _create(client)
    first = _event(
        client, plan_id, {"type": "reduce", "lot_id": "lot-sangju-gr-1", "new_pallets": 10}
    )
    assert _apply(client, plan_id, first, "sail_as_is").status_code == 200
    order = _orders(client, plan_id)[-1]
    for item in order["shipper_confirmations"]:
        confirmed = client.post(
            f"/api/plans/{plan_id}/orders/{order['order_id']}/confirm",
            json={"actor": "shipper", "shipper_id": item["shipper_id"], "accept": True},
        )
        assert confirmed.status_code == 200, confirmed.text
    forwarder = client.post(
        f"/api/plans/{plan_id}/orders/{order['order_id']}/confirm",
        json={"actor": "forwarder", "accept": True},
    )
    assert forwarder.status_code == 200, forwarder.text
    second = _event(
        client, plan_id, {"type": "reduce", "lot_id": "lot-pohang-gr-1", "new_pallets": 3}
    )
    charge = next(a for a in second["alternatives"] if a["id"] == "sail_as_is")["diff"][
        "cancellation"
    ]
    assert _apply(client, plan_id, second, "sail_as_is").status_code == 200
    latest = _orders(client, plan_id)[-1]
    _assert_cumulative(latest, [charge])


def test_charge_payer_added_to_unconfirmed_order_reopens_shipper_step(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_id = _create(client)
    first = _event(client, plan_id, {"type": "cancel", "lot_id": "lot-miryang-sb-1"})
    charge = next(a for a in first["alternatives"] if a["id"] == "sail_as_is")["diff"][
        "cancellation"
    ]
    assert _apply(client, plan_id, first, "sail_as_is").status_code == 200
    real = store_module.build_order

    def without_confirmations(**kwargs):
        order = real(**kwargs)
        order.shipper_confirmations = []
        order.status = "shipper_confirmed"
        order.log.append(
            OrderLogEntry(
                at=order.created_at,
                actor="system",
                action="확인이 필요한 화주 없음 → 포워더 확정 대기",
                status_after="shipper_confirmed",
            )
        )
        return order

    monkeypatch.setattr(store_module, "build_order", without_confirmations)
    second = _event(client, plan_id, {"type": "air_switch", "lot_id": "lot-jinju-sb-1"})
    assert _apply(client, plan_id, second, "sail_as_is").status_code == 200
    order = _orders(client, plan_id)[-1]
    assert order["status"] == "proposed"
    assert [e["status_after"] for e in order["log"]] == ["proposed"]
    [item] = order["shipper_confirmations"]
    assert item["shipper_id"] == "sh-miryang"
    assert item["reasons"] == ["cancellation_fee"]
    assert item["cancellation_adjustment"] == charge["fee"]
    assert "sh-miryang" not in order["notified_shipper_ids"]
    assert order["deadlines"]["shipper"] == item["deadline"]


def _pending_with_inspection(client: TestClient) -> tuple[str, dict, dict]:
    plan_id = _create(client)
    result = _event(
        client, plan_id, {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}
    )
    assert _apply(client, plan_id, result, "sail_as_is").status_code == 200
    recorded = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": "lot-miryang-sb-1", "inspected_loss_pct": 20},
    )
    assert recorded.status_code == 200, recorded.text
    pending = client.get(f"/api/plans/{plan_id}/settlement").json()
    assert [i["lot_id"] for i in pending["inspections"]] == ["lot-miryang-sb-1"]
    return plan_id, recorded.json(), pending


def _assert_inspection_kept(client: TestClient, plan_id: str, recorded: dict) -> None:
    session = client.get(f"/api/plans/{plan_id}").json()
    assert session["status"] == "committed"
    view = client.get(f"/api/plans/{plan_id}/settlement").json()
    assert [i["lot_id"] for i in view["inspections"]] == ["lot-miryang-sb-1"]
    inspection = view["inspections"][0]
    assert inspection["excess_loss_value"] == recorded["excess_loss_value"]
    assert view["total_refund"] > 0
    observations = [o for o in quality_history.observations() if o.plan_id == plan_id]
    assert [o.lot_id for o in observations] == ["lot-miryang-sb-1"]


def test_inspection_recorded_while_pending_survives_rejection(client: TestClient) -> None:
    plan_id, recorded, _ = _pending_with_inspection(client)
    order = _orders(client, plan_id)[-1]
    shipper = order["shipper_confirmations"][0]["shipper_id"]
    rejected = client.post(
        f"/api/plans/{plan_id}/orders/{order['order_id']}/confirm",
        json={"actor": "shipper", "shipper_id": shipper, "accept": False},
    )
    assert rejected.status_code == 200, rejected.text
    _assert_inspection_kept(client, plan_id, recorded)


def test_inspection_recorded_while_pending_survives_expiry(client: TestClient) -> None:
    plan_id, recorded, _ = _pending_with_inspection(client)
    store.clock = lambda: LATE
    _assert_inspection_kept(client, plan_id, recorded)


def test_inspection_recorded_while_pending_kept_on_commit(client: TestClient) -> None:
    plan_id, recorded, _ = _pending_with_inspection(client)
    order = _orders(client, plan_id)[-1]
    for item in order["shipper_confirmations"]:
        client.post(
            f"/api/plans/{plan_id}/orders/{order['order_id']}/confirm",
            json={"actor": "shipper", "shipper_id": item["shipper_id"], "accept": True},
        )
    forwarder = client.post(
        f"/api/plans/{plan_id}/orders/{order['order_id']}/confirm",
        json={"actor": "forwarder", "accept": True},
    )
    assert forwarder.status_code == 200, forwarder.text
    _assert_inspection_kept(client, plan_id, recorded)


def test_pending_inspection_of_changed_lot_is_rejected_without_observation(
    client: TestClient,
) -> None:
    plan_id = _create(client)
    result = _event(
        client, plan_id, {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}
    )
    assert _apply(client, plan_id, result, "sail_as_is").status_code == 200
    before = client.get(f"/api/plans/{plan_id}").json()
    response = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": "lot-sancheong-sb-1", "inspected_loss_pct": 20},
    )
    assert response.status_code == 409
    assert "확정 대기" in response.json()["detail"]
    after = client.get(f"/api/plans/{plan_id}").json()
    assert after["inspection_revision"] == before["inspection_revision"]
    assert after["arrival_inspections"] == []
    assert [o for o in quality_history.observations() if o.plan_id == plan_id] == []


def test_apply_rejects_order_whose_deadline_already_passed(client: TestClient) -> None:
    plan_id = _create(client)
    result = _event(
        client, plan_id, {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}
    )
    store.clock = lambda: LATE
    response = _apply(client, plan_id, result, "sail_as_is")
    assert response.status_code == 409
    assert "기한" in response.json()["detail"]
    session = client.get(f"/api/plans/{plan_id}").json()
    assert session["version"] == 1
    assert session["status"] == "committed"
    assert _orders(client, plan_id) == []


@pytest.mark.parametrize(
    ("event", "now", "phrase"),
    [
        (
            {"type": "air_switch", "lot_id": "lot-jinju-sb-1"},
            datetime(2026, 11, 14, 16, 0, tzinfo=UTC),
            "포워더 확정 기한",
        ),
        (DELAY, datetime(2026, 11, 18, 1, 0, tzinfo=UTC), "기한"),
    ],
)
def test_apply_rejects_order_past_deadline_on_every_branch(
    client: TestClient, event: dict, now: datetime, phrase: str
) -> None:
    plan_id = _create(client)
    result = _event(client, plan_id, event)
    store.clock = lambda: now
    response = _apply(client, plan_id, result, "sail_as_is")
    assert response.status_code == 409, response.text
    assert phrase in response.json()["detail"]
    session = client.get(f"/api/plans/{plan_id}").json()
    assert session["version"] == 1
    assert session["status"] == "committed"
    assert _orders(client, plan_id) == []


def test_forwarder_attention_applies_expiry_before_listing(
    forwarder_client: TestClient, platform_client: TestClient, saas_login
) -> None:
    plan_id, extra = _plan_with_spare(forwarder_client, saas_login, skip=("sh-jinju",))
    _apply_add(forwarder_client, plan_id, extra)

    def attention() -> dict:
        items = forwarder_client.get(ATTENTION).json()
        return next(s for s in items if s["plan_id"] == plan_id)

    pending = attention()
    assert pending["status"] == "pending_confirmation"
    store.clock = lambda: LATE
    listed = forwarder_client.get(ATTENTION).json()
    assert plan_id not in {s["plan_id"] for s in listed}
    session = platform_client.get(f"/api/plans/{plan_id}").json()
    assert session["status"] == "committed"
    assert session["version"] == pending["version"] + 1


def test_demo_plan_list_applies_expiry_before_listing(client: TestClient) -> None:
    plan_id = _create(client)
    result = _event(
        client, plan_id, {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}
    )
    assert _apply(client, plan_id, result, "pull_forward").status_code == 200
    listed = next(p for p in client.get("/api/plans").json() if p["plan_id"] == plan_id)
    assert listed["status"] == "pending_confirmation"
    store.clock = lambda: LATE
    listed = next(p for p in client.get("/api/plans").json() if p["plan_id"] == plan_id)
    detail = client.get(f"/api/plans/{plan_id}").json()
    assert listed["status"] == "committed"
    assert listed["version"] == detail["version"] == 3


def _cancel_sangju(forwarder_client: TestClient, saas_login, plan_id: str, sangju: str) -> None:
    open_quantity_window()
    queued = queue_quantity(saas_login(shipper_org_id("sh-sangju")), plan_id, sangju, 0)
    path = f"/api/saas/forwarder/change-requests/{queued['change_request_id']}"
    computed = forwarder_client.post(f"{path}/replan?llm=false")
    assert computed.status_code == 200, computed.text
    chosen = computed.json()["replan"]["recommended_id"]
    applied = forwarder_client.post(f"{path}/apply", json={"alternative_id": chosen})
    assert applied.status_code == 200, applied.text


def test_saas_lists_and_settlements_apply_expiry(
    forwarder_client: TestClient, platform_client: TestClient, saas_login
) -> None:
    created = forwarder_client.post(
        "/api/saas/forwarder/plans", json={"lot_ids": _register_all(saas_login)}
    )
    assert created.status_code == 201, created.text
    plan = created.json()
    plan_id = plan["plan_id"]
    accept_plan(saas_login, plan_id)
    sangju = next(lot["id"] for lot in plan["scenario"]["lots"] if lot["shipper_id"] == "sh-sangju")
    _cancel_sangju(forwarder_client, saas_login, plan_id, sangju)

    def platform_item() -> dict:
        items = platform_client.get("/api/saas/platform/plans").json()
        return next(p for p in items if p["plan_id"] == plan_id)

    def platform_settlement() -> dict:
        rows = platform_client.get("/api/saas/platform/settlements").json()
        return next(r for r in rows if r["plan_id"] == plan_id)

    pending_settlement = platform_settlement()
    assert platform_item()["status"] == "pending_confirmation"
    store.clock = lambda: LATE
    assert platform_item()["status"] == "committed"
    settled = platform_settlement()
    assert settled != pending_settlement
    _cancel_sangju(forwarder_client, saas_login, plan_id, sangju)
    store.clock = lambda: LATE
    sangju_client = saas_login(shipper_org_id("sh-sangju"))
    mine = next(
        p for p in sangju_client.get("/api/saas/shipper/plans").json() if p["plan_id"] == plan_id
    )
    assert mine["status"] == "committed"
    assert mine["my_pallets"] > 0
    assert sangju_client.get("/api/saas/shipper/settlements").json() != []
    _cancel_sangju(forwarder_client, saas_login, plan_id, sangju)
    store.clock = lambda: LATE
    items = forwarder_client.get("/api/saas/forwarder/plans").json()
    assert next(p for p in items if p["plan_id"] == plan_id)["status"] == "committed"
