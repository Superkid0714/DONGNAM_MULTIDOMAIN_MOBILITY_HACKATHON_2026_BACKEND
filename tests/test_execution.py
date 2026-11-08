from datetime import UTC, datetime

from fastapi.testclient import TestClient

from freshswitch_backend.planning.store import store

REDUCE = {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}


def _create(client: TestClient) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def _event(client: TestClient, plan_id: str, **extra) -> dict:
    response = client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE, **extra})
    assert response.status_code == 200
    return response.json()


def _apply(client: TestClient, plan_id: str, alternative: str, **extra) -> dict:
    response = client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _order(client: TestClient, plan_id: str) -> dict:
    orders = client.get(f"/api/plans/{plan_id}/orders").json()
    return orders[-1]


def _confirm(client: TestClient, plan_id: str, order_id: str, **body) -> dict:
    return client.post(f"/api/plans/{plan_id}/orders/{order_id}/confirm", json=body)


def test_cancellation_fee_on_sail_as_is_and_zero_when_refilled(client: TestClient) -> None:
    plan_id = _create(client)
    result = _event(client, plan_id)
    alternatives = {a["id"]: a for a in result["alternatives"]}
    refill = alternatives["pull_forward"]["diff"]["cancellation"]
    assert refill["refilled_pallets"] == 2
    assert refill["dead_pallets"] == 0
    assert refill["fee"] == 0
    charge = alternatives["sail_as_is"]["diff"]["cancellation"]
    assert charge["payer_shipper_id"] == "sh-sancheong"
    assert charge["dead_pallets"] == 2
    assert charge["share_per_pallet_before"] == round(9_060_000 / 17)
    assert charge["share_per_pallet_after"] == round(9_060_000 / 15)
    expected = round(12 * (9_060_000 / 15 - 9_060_000 / 17))
    assert charge["fee"] == expected
    transfers = {t["shipper_id"]: t["amount"] for t in charge["transfers"]}
    assert set(transfers) == {"sh-jinju", "sh-miryang"}
    assert sum(transfers.values()) == charge["fee"]
    shippers = {s["shipper_id"]: s for s in alternatives["sail_as_is"]["diff"]["shippers"]}
    assert shippers["sh-sancheong"]["cancellation_adjustment"] == charge["fee"]
    assert shippers["sh-jinju"]["cancellation_adjustment"] == -transfers["sh-jinju"]
    jinju = shippers["sh-jinju"]
    assert transfers["sh-jinju"] == jinju["container_share_after"] - jinju["container_share_before"]


def test_apply_creates_order_with_split_actions(client: TestClient) -> None:
    plan_id = _create(client)
    _event(client, plan_id)
    session = _apply(client, plan_id, "pull_forward")
    assert session["status"] == "pending_confirmation"
    assert session["committed_version"] == 1
    order = _order(client, plan_id)
    assert session["active_order_id"] == order["order_id"]
    assert order["status"] == "proposed"
    confirmations = {c["shipper_id"]: c for c in order["shipper_confirmations"]}
    assert set(confirmations) == {"sh-miryang"}
    assert "pull_forward" in confirmations["sh-miryang"]["reasons"]
    assert "mode_change" in confirmations["sh-miryang"]["reasons"]
    assert confirmations["sh-miryang"]["pull_forward_days"] == 1
    assert "sh-sancheong" in order["notified_shipper_ids"]
    assert "sh-jinju" not in order["notified_shipper_ids"]
    kinds = {a["kind"] for a in order["automatic"]}
    assert kinds == {"slot_reassignment", "truck_dispatch", "settlement_recompute"}
    requests = {r["kind"]: r for r in order["requests"]}
    assert {"booking_amendment", "air_cancel"} <= set(requests)
    amendment = requests["booking_amendment"]
    assert amendment["counterparty_role"] == "carrier"
    assert amendment["container_id"] == "CA-01"
    assert amendment["voyage_id"] == "V-2611A"
    text = amendment["text"]
    assert "CA-01" in text and "V-2611A" in text and "17→17파렛트" in text
    assert "산청 5→3" in text and "밀양 4→6" in text
    air = requests["air_cancel"]
    assert air["counterparty_role"] == "airline"
    assert "lot-miryang-sb-2" in air["text"] and "2파렛트" in air["text"]
    deadlines = order["deadlines"]
    cutoff = datetime.fromisoformat(deadlines["cy_cutoff"])
    dispatch = datetime.fromisoformat(deadlines["dispatch"])
    forwarder = datetime.fromisoformat(deadlines["forwarder"])
    shipper = datetime.fromisoformat(deadlines["shipper"])
    assert shipper < forwarder <= dispatch < cutoff
    for item in confirmations.values():
        assert datetime.fromisoformat(item["deadline"]) <= forwarder


def test_sail_as_is_requests_and_fee_confirmation(client: TestClient) -> None:
    plan_id = _create(client)
    _event(client, plan_id)
    _apply(client, plan_id, "sail_as_is")
    order = _order(client, plan_id)
    confirmations = {c["shipper_id"]: c for c in order["shipper_confirmations"]}
    assert confirmations["sh-sancheong"]["reasons"] == ["cancellation_fee"]
    assert confirmations["sh-sancheong"]["cancellation_adjustment"] == order["cancellation"]["fee"]
    assert set(confirmations) == {"sh-sancheong"}
    assert {"sh-jinju", "sh-miryang"} <= set(order["notified_shipper_ids"])
    assert "sh-miryang" in order["notified_shipper_ids"]
    amendment = next(r for r in order["requests"] if r["kind"] == "booking_amendment")
    assert amendment["pallets_before"] == 17 and amendment["pallets_after"] == 15
    assert "17→15파렛트" in amendment["text"]


def test_status_flow_to_executed(client: TestClient) -> None:
    plan_id = _create(client)
    _event(client, plan_id)
    _apply(client, plan_id, "pull_forward")
    order_id = _order(client, plan_id)["order_id"]
    early = _confirm(client, plan_id, order_id, actor="forwarder", accept=True)
    assert early.status_code == 409
    outsider = _confirm(
        client, plan_id, order_id, actor="shipper", shipper_id="sh-jinju", accept=True
    )
    assert outsider.status_code == 422
    first = _confirm(
        client, plan_id, order_id, actor="shipper", shipper_id="sh-miryang", accept=True
    )
    assert first.json()["status"] == "shipper_confirmed"
    again = _confirm(
        client, plan_id, order_id, actor="shipper", shipper_id="sh-miryang", accept=True
    )
    assert again.status_code == 200 and again.json()["status"] == "shipper_confirmed"
    assert client.get(f"/api/plans/{plan_id}").json()["status"] == "pending_confirmation"
    done = _confirm(client, plan_id, order_id, actor="forwarder", accept=True, note="부킹 반영")
    body = done.json()
    assert body["status"] == "forwarder_confirmed"
    assert all(a["done"] for a in body["automatic"])
    session = client.get(f"/api/plans/{plan_id}").json()
    assert session["status"] == "committed"
    assert session["committed_version"] == 2
    for request in body["requests"]:
        response = client.post(
            f"/api/plans/{plan_id}/orders/{order_id}/requests/{request['request_id']}",
            json={"accepted": True},
        )
        assert response.status_code == 200
    final = client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    assert final["status"] == "executed"
    assert [e["status_after"] for e in final["log"]][-1] == "executed"
    assert client.get(f"/api/plans/{plan_id}").json()["active_order_id"] is None


def test_shipper_reject_rolls_back(client: TestClient) -> None:
    plan_id = _create(client)
    original = client.get(f"/api/plans/{plan_id}").json()
    _event(client, plan_id)
    _apply(client, plan_id, "pull_forward")
    order_id = _order(client, plan_id)["order_id"]
    rejected = _confirm(
        client,
        plan_id,
        order_id,
        actor="shipper",
        shipper_id="sh-miryang",
        accept=False,
        note="수확 못 당김",
    ).json()
    assert rejected["status"] == "rejected"
    assert rejected["rolled_back_to_version"] == 1
    session = client.get(f"/api/plans/{plan_id}").json()
    assert session["version"] == 3
    assert session["status"] == "committed"
    assert session["plan"]["total_shared_cost"] == original["plan"]["total_shared_cost"]
    lots = {lot["id"]: lot["pallets"] for lot in session["scenario"]["lots"]}
    assert lots["lot-sancheong-sb-1"] == 5
    history = session["history"]
    assert [h["kind"] for h in history] == ["apply", "rollback"]
    assert history[1]["order_id"] == order_id
    late = _confirm(client, plan_id, order_id, actor="forwarder", accept=True)
    assert late.status_code == 409


def test_forwarder_reject_rolls_back(client: TestClient) -> None:
    plan_id = _create(client)
    _event(client, plan_id)
    _apply(client, plan_id, "pull_forward")
    order_id = _order(client, plan_id)["order_id"]
    for sid in ("sh-jinju", "sh-miryang"):
        _confirm(client, plan_id, order_id, actor="shipper", shipper_id=sid, accept=True)
    rejected = _confirm(client, plan_id, order_id, actor="forwarder", accept=False).json()
    assert rejected["status"] == "rejected"
    assert client.get(f"/api/plans/{plan_id}").json()["history"][-1]["kind"] == "rollback"


def test_confirm_input_errors(client: TestClient) -> None:
    plan_id = _create(client)
    _event(client, plan_id)
    _apply(client, plan_id, "pull_forward")
    order_id = _order(client, plan_id)["order_id"]
    assert _confirm(client, plan_id, order_id, actor="shipper", accept=True).status_code == 422
    outsider = _confirm(
        client, plan_id, order_id, actor="shipper", shipper_id="sh-pohang", accept=True
    )
    assert outsider.status_code == 422
    assert _confirm(client, plan_id, "nope", actor="forwarder", accept=True).status_code == 404


def test_new_apply_supersedes_pending_order(client: TestClient) -> None:
    plan_id = _create(client)
    _event(client, plan_id)
    _apply(client, plan_id, "pull_forward")
    first = _order(client, plan_id)["order_id"]
    client.post(
        f"/api/plans/{plan_id}/events",
        json={"event": {"type": "air_switch", "lot_id": "lot-jinju-sb-1"}},
    )
    _apply(client, plan_id, "pull_forward")
    orders = client.get(f"/api/plans/{plan_id}/orders").json()
    assert orders[0]["order_id"] == first
    assert orders[0]["status"] == "expired"
    assert orders[0]["expired_reason"] == "superseded"
    assert orders[1]["status"] in ("proposed", "shipper_confirmed")
    assert orders[1]["rollback_version"] == 1


def test_deadline_expiry_rolls_back(client: TestClient) -> None:
    plan_id = _create(client)
    _event(client, plan_id)
    _apply(client, plan_id, "pull_forward")
    order = _order(client, plan_id)
    store.clock = lambda: datetime(2026, 11, 17, tzinfo=UTC)
    expired = client.get(f"/api/plans/{plan_id}/orders/{order['order_id']}").json()
    assert expired["status"] == "expired"
    session = client.get(f"/api/plans/{plan_id}").json()
    assert session["history"][-1]["kind"] == "rollback"
    response = _confirm(
        client, plan_id, order["order_id"], actor="shipper", shipper_id="sh-miryang", accept=True
    )
    assert response.status_code == 409


def test_events_idempotency(client: TestClient) -> None:
    plan_id = _create(client)
    first = _event(client, plan_id, idempotency_key="evt-1")
    second = _event(client, plan_id, idempotency_key="evt-1")
    assert second["replan_id"] == first["replan_id"]
    assert second == first
    other = _event(client, plan_id, idempotency_key="evt-2")
    assert other["replan_id"] != first["replan_id"]
    mismatch = client.post(
        f"/api/plans/{plan_id}/events",
        json={
            "event": {"type": "cancel", "lot_id": "lot-sancheong-sb-1"},
            "idempotency_key": "evt-1",
        },
    )
    assert mismatch.status_code == 422


def test_apply_idempotency(client: TestClient) -> None:
    plan_id = _create(client)
    result = _event(client, plan_id)
    body = {"replan_id": result["replan_id"], "idempotency_key": "apply-1"}
    first = _apply(client, plan_id, "pull_forward", **body)
    second = _apply(client, plan_id, "pull_forward", **body)
    assert second["version"] == first["version"] == 2
    assert len(client.get(f"/api/plans/{plan_id}/orders").json()) == 1
    assert len(client.get(f"/api/plans/{plan_id}/history").json()) == 1
    without_key = client.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": "pull_forward", "replan_id": result["replan_id"]},
    )
    assert without_key.status_code == 409


def test_delay_event_requests_plug_extension(client: TestClient) -> None:
    plan_id = _create(client)
    client.post(
        f"/api/plans/{plan_id}/events",
        json={
            "event": {
                "type": "delay",
                "voyage_id": "V-2611A",
                "new_etd": "2026-11-19T18:00:00+09:00",
            }
        },
    )
    _apply(client, plan_id, "sail_as_is")
    order = _order(client, plan_id)
    plugs = [r for r in order["requests"] if r["kind"] == "reefer_plug"]
    assert plugs
    assert all(r["counterparty_role"] == "terminal" for r in plugs)
    assert "1.0일" in plugs[0]["text"]
    assert order["cancellation"] is None
