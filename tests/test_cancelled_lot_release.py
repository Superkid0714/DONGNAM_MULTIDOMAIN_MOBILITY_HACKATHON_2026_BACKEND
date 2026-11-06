from datetime import datetime, timedelta

import pytest
from test_onboarding import _lot, _onboarded
from test_saas_resource_invariants import PLANS, _forwarder, _resource

from freshswitch_backend.persistence import database
from freshswitch_backend.planning.quantity_policy import load_ledger, policy_rows
from freshswitch_backend.planning.store import store

BOUND = "수량 변경 견적"


@pytest.fixture
def shared_plan(monkeypatch):
    owner = _forwarder("cancel-release")
    first, _ = _onboarded("cancel-release-a@example.com", "취소 화주 A")
    second, _ = _onboarded("cancel-release-b@example.com", "취소 화주 B")
    resource = _resource(owner)
    lots = {}
    for name, client, pallets in (
        ("target", first, 5),
        ("support", first, 8),
        ("other", second, 4),
    ):
        registered = client.post(
            "/api/saas/shipper/lots", json=_lot(pallets=pallets, ready="2026-11-17")
        )
        assert registered.status_code == 201, registered.text
        lots[name] = registered.json()["registered"]
    created = owner.post(PLANS, json={"lot_ids": [lot["id"] for lot in lots.values()]})
    assert created.status_code == 201, created.text
    plan = created.json()
    sea = {a["lot_id"]: a for a in plan["plan"]["assignments"] if a["mode"] == "sea"}
    assert set(sea) == {lot["id"] for lot in lots.values()}
    assert len({a["voyage_id"] for a in sea.values()}) == 1
    clock = [store.clock()]
    monkeypatch.setattr(store, "clock", lambda: clock[0])
    clients = {lots["target"]["shipper_id"]: first, lots["other"]["shipper_id"]: second}
    for client in {id(c): c for c in clients.values()}.values():
        accepted = client.post(
            f"/api/saas/shipper/plans/{plan['plan_id']}/confirmation", json={"accept": True}
        )
        assert accepted.status_code == 200, accepted.text
    clock[0] = datetime.fromisoformat(resource["cy_cutoff"]) - timedelta(days=2, hours=12)
    return {
        "owner": owner,
        "first": first,
        "second": second,
        "clients": clients,
        "lots": {name: lot["id"] for name, lot in lots.items()},
        "plan_id": plan["plan_id"],
        "voyage_id": sea[lots["target"]["id"]]["voyage_id"],
        "clock": clock,
    }


def _quantity_path(context, lot_id):
    return f"/api/saas/shipper/plans/{context['plan_id']}/lots/{lot_id}/quantity"


def _complete_order(context, order_id):
    plan_id = context["plan_id"]
    order = context["owner"].get(f"/api/plans/{plan_id}/orders/{order_id}")
    assert order.status_code == 200, order.text
    for confirmation in order.json()["shipper_confirmations"]:
        if confirmation["status"] != "pending":
            continue
        accepted = context["clients"][confirmation["shipper_id"]].post(
            f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": True, "shipper_id": confirmation["shipper_id"]},
        )
        assert accepted.status_code == 200, accepted.text
    accepted = context["owner"].post(
        f"{PLANS}/{plan_id}/orders/{order_id}/confirm", json={"accept": True}
    )
    assert accepted.status_code == 200, accepted.text


def _cancel_target(context):
    path = _quantity_path(context, context["lots"]["target"])
    quote = context["first"].post(f"{path}/quote", json={"quantity": 0})
    assert quote.status_code == 200, quote.text
    queued = context["first"].post(
        path, json={"quote_id": quote.json()["quote_id"], "idempotency_key": "cancel-target"}
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "queued"
    return queued.json()["change_request_id"]


def _apply_request(context, request_id):
    path = f"/api/saas/forwarder/change-requests/{request_id}"
    calculated = context["owner"].post(f"{path}/replan?llm=false")
    assert calculated.status_code == 200, calculated.text
    feasible = [a for a in calculated.json()["replan"]["alternatives"] if a["feasible"]]
    assert feasible, calculated.text
    alternative = next((a for a in feasible if a["id"] == "sail_as_is"), feasible[0])
    applied = context["owner"].post(
        f"{path}/apply", json={"alternative_id": alternative["id"], "idempotency_key": request_id}
    )
    assert applied.status_code == 200, applied.text
    _complete_order(context, applied.json()["plan"]["active_order_id"])


def _reservations(plan_id):
    with database.begin() as db:
        return {
            lot_id: reservation
            for row in policy_rows(db, plan_id)
            for lot_id, reservation in load_ledger(row).reservations.items()
        }


def _assert_bound(client, lot_id):
    lot_path = f"/api/saas/shipper/lots/{lot_id}"
    for response in (
        client.patch(lot_path, json={"pallets": 1}),
        client.patch(lot_path, json={"status": "cancelled"}),
        client.delete(lot_path),
    ):
        assert response.status_code == 409, response.text
        assert BOUND in response.json()["detail"]


def _cutoff(context):
    with database.begin() as db:
        [row] = policy_rows(db, context["plan_id"])
        return load_ledger(row).policy.cutoff


def _refilling(client, lot_id):
    response = client.delete(f"/api/saas/shipper/lots/{lot_id}")
    assert response.status_code == 409, response.text
    assert "빈자리를 충원" in response.json()["detail"]


def _waitlist(client, context, lot_id, quantity):
    return client.post(
        f"/api/saas/shipper/plans/{context['plan_id']}/voyages/{context['voyage_id']}/refill-waitlist",
        json={"lot_id": lot_id, "quantity": quantity},
    )


def _pending_offers(client, lot_id):
    offers = client.get("/api/saas/shipper/refill-offers")
    assert offers.status_code == 200, offers.text
    return [o for o in offers.json() if o["status"] == "pending" and o["lot_id"] == lot_id]


def _refill_with_fresh_lot(context):
    registered = context["first"].post(
        "/api/saas/shipper/lots", json=_lot(pallets=5, ready="2026-11-17")
    )
    assert registered.status_code == 201, registered.text
    fresh = registered.json()["registered"]["id"]
    waited = _waitlist(context["first"], context, fresh, 5)
    assert waited.status_code == 200, waited.text
    refreshed = context["owner"].get(f"{PLANS}/{context['plan_id']}/quantity-policy")
    assert refreshed.status_code == 200, refreshed.text
    [offer] = _pending_offers(context["first"], fresh)
    _refilling(context["first"], context["lots"]["target"])
    accepted = context["first"].post(
        f"/api/saas/shipper/refill-offers/{offer['offer_id']}/accept",
        json={"quantity": 5, "idempotency_key": "refill-fresh"},
    )
    assert accepted.status_code == 200, accepted.text
    _complete_order(context, accepted.json()["order_id"])
    refreshed = context["owner"].get(f"{PLANS}/{context['plan_id']}/quantity-policy")
    assert refreshed.status_code == 200, refreshed.text
    return fresh


def test_queued_cancellation_keeps_lot_bound(shared_plan):
    context = shared_plan
    _cancel_target(context)
    _assert_bound(context["first"], context["lots"]["target"])
    assert _reservations(context["plan_id"])[context["lots"]["target"]].current_quantity == 5


def test_cancelled_lot_is_released_after_refill_window_without_touching_others(shared_plan):
    context = shared_plan
    target, support, other = (context["lots"][k] for k in ("target", "support", "other"))
    _apply_request(context, _cancel_target(context))
    before = _reservations(context["plan_id"])
    assert before[target].current_quantity == 0
    assert before[target].events
    state = context["first"].get(_quantity_path(context, target))
    assert state.status_code == 200, state.text
    assert state.json()["quantity_status"] == "cancelled"
    fee = state.json()["final_fee_krw"]
    assert fee > 0
    assert context["first"].get(f"/api/lots/{target}").json()["status"] == "cancelled"

    _refilling(context["first"], target)
    _assert_bound(context["first"], support)
    _assert_bound(context["second"], other)
    assert context["second"].delete(f"/api/saas/shipper/lots/{target}").status_code == 404

    context["clock"][0] = _cutoff(context)
    deleted = context["first"].delete(f"/api/saas/shipper/lots/{target}")
    assert deleted.status_code == 204, deleted.text
    assert context["first"].get(f"/api/lots/{target}").status_code == 404
    assert _reservations(context["plan_id"]) == before
    _assert_bound(context["first"], support)
    _assert_bound(context["second"], other)
    statement = context["first"].get(
        f"/api/saas/shipper/plans/{context['plan_id']}/quantity-statement"
    )
    assert statement.status_code == 200, statement.text
    charged = {item["lot_id"]: item["cancellation_charge_krw"] for item in statement.json()}
    assert charged[target] == fee


def test_refilled_cancellation_releases_lot_and_revived_lot_gets_fresh_reservation(shared_plan):
    context = shared_plan
    target = context["lots"]["target"]
    _apply_request(context, _cancel_target(context))
    cancelled = _reservations(context["plan_id"])[target]
    _refilling(context["first"], target)

    fresh = _refill_with_fresh_lot(context)

    reservations = _reservations(context["plan_id"])
    assert reservations[fresh].current_quantity == 5
    assert reservations[fresh].events == []
    old = reservations[target]
    assert old.current_quantity == 0
    assert old.events == cancelled.events
    assert sum(offset.quantity for offset in old.offsets) == 5
    _assert_bound(context["first"], fresh)

    revived = context["first"].patch(
        f"/api/saas/shipper/lots/{target}", json={"status": "registered"}
    )
    assert revived.status_code == 200, revived.text
    assert revived.json()["status"] == "registered"
    companion = context["first"].post(
        "/api/saas/shipper/lots", json=_lot(pallets=8, ready="2026-11-17")
    )
    assert companion.status_code == 201, companion.text
    _resource(context["owner"])
    replanned = context["owner"].post(
        PLANS, json={"lot_ids": [target, companion.json()["registered"]["id"]]}
    )
    assert replanned.status_code == 201, replanned.text
    second_plan = replanned.json()["plan_id"]
    again = _reservations(second_plan)[target]
    assert again.current_quantity == 5
    assert again.events == []
    assert again.reservation_id == f"{second_plan}:{target}"
    _assert_bound(context["first"], target)
    assert _reservations(context["plan_id"]) == reservations


def test_revived_lot_cannot_refill_vacancy_in_plan_it_cancelled(shared_plan):
    context = shared_plan
    target, other = context["lots"]["target"], context["lots"]["other"]
    _apply_request(context, _cancel_target(context))
    cancelled = _reservations(context["plan_id"])[target]
    _refill_with_fresh_lot(context)
    revived = context["first"].patch(
        f"/api/saas/shipper/lots/{target}", json={"status": "registered", "pallets": 4}
    )
    assert revived.status_code == 200, revived.text

    path = _quantity_path(context, other)
    quote = context["second"].post(f"{path}/quote", json={"quantity": 0})
    assert quote.status_code == 200, quote.text
    queued = context["second"].post(
        path, json={"quote_id": quote.json()["quote_id"], "idempotency_key": "cancel-other"}
    )
    assert queued.status_code == 200, queued.text
    _apply_request(context, queued.json()["change_request_id"])
    before = _reservations(context["plan_id"])

    waited = _waitlist(context["first"], context, target, 4)
    assert waited.status_code == 409, waited.text
    assert "취소한 출하" in waited.json()["detail"]
    added = context["owner"].post(f"{PLANS}/{context['plan_id']}/add-lot", json={"lot_id": target})
    assert added.status_code == 409, added.text
    assert "취소한 출하" in added.json()["detail"]
    refreshed = context["owner"].get(f"{PLANS}/{context['plan_id']}/quantity-policy")
    assert refreshed.status_code == 200, refreshed.text
    assert _pending_offers(context["first"], target) == []

    after = _reservations(context["plan_id"])
    assert after[target] == before[target]
    assert after[target].current_quantity == 0
    assert after[target].events == cancelled.events
    assert sum(offset.quantity for offset in after[target].offsets) == 5
    assert after[other].current_quantity == 0
    assert after[other].offsets == []
    statement = context["second"].get(
        f"/api/saas/shipper/plans/{context['plan_id']}/quantity-statement"
    )
    assert statement.status_code == 200, statement.text
    [row] = statement.json()
    assert row["refill_credit_krw"] == 0
    assert row["net_adjustment_krw"] == row["cancellation_charge_krw"] > 0
