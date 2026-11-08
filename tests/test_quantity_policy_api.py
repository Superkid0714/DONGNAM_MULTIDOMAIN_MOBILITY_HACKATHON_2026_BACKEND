import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import SAAS_CSRF
from sqlalchemy import func, select
from test_onboarding import _lot, _onboarded
from test_saas_resource_invariants import PLANS, _forwarder, _resource

from freshswitch_backend.main import app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import ChangeRequestRow, PlanRow
from freshswitch_backend.planning.store import store


@pytest.fixture
def policy_plan(monkeypatch):
    owner = _forwarder("quantity-policy")
    shipper, _ = _onboarded("quantity-shipper@example.com", "수량 검증 화주")
    resource = _resource(owner)
    registered = shipper.post("/api/saas/shipper/lots", json=_lot(pallets=5, ready="2026-11-17"))
    assert registered.status_code == 201, registered.text
    lot_id = registered.json()["registered"]["id"]
    support_ids = []
    for quantity in (8, 4):
        support = shipper.post(
            "/api/saas/shipper/lots", json=_lot(pallets=quantity, ready="2026-11-17")
        )
        assert support.status_code == 201, support.text
        support_ids.append(support.json()["registered"]["id"])
    created = owner.post(PLANS, json={"lot_ids": [lot_id, *support_ids]})
    assert created.status_code == 201, created.text
    plan = created.json()
    assigned = next(item for item in plan["plan"]["assignments"] if item["lot_id"] == lot_id)
    assert assigned["mode"] == "sea"
    cutoff = datetime.fromisoformat(resource["cy_cutoff"])
    clock = [store.clock()]
    monkeypatch.setattr(store, "clock", lambda: clock[0])
    return {
        "owner": owner,
        "shipper": shipper,
        "lot_id": lot_id,
        "support_ids": support_ids,
        "plan": plan,
        "plan_id": plan["plan_id"],
        "voyage_id": assigned["voyage_id"],
        "cutoff": cutoff,
        "clock": clock,
        "path": f"/api/saas/shipper/plans/{plan['plan_id']}/lots/{lot_id}/quantity",
    }


def _quote(context, quantity=0, **extra):
    response = context["shipper"].post(
        f"{context['path']}/quote", json={"quantity": quantity, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _submit(context, quote, key="quantity-change"):
    return context["shipper"].post(
        context["path"], json={"quote_id": quote["quote_id"], "idempotency_key": key}
    )


def _request_count(context):
    with database.begin() as db:
        return db.scalar(
            select(func.count())
            .select_from(ChangeRequestRow)
            .where(ChangeRequestRow.plan_id == context["plan_id"])
        )


def test_t3_auto_confirmation_does_not_accept_execution_costs(policy_plan):
    context = policy_plan
    context["clock"][0] = context["cutoff"] - timedelta(days=3)
    response = context["shipper"].get(context["path"])
    assert response.status_code == 200, response.text
    assert response.json()["quantity_status"] == "confirmed"
    assert response.json()["confirmed_quantity"] == 5
    consent = context["owner"].get(f"{PLANS}/{context['plan_id']}/confirmation")
    assert consent.status_code == 200, consent.text
    assert consent.json()["status"] == "pending_shipper_confirmation"
    assert all(item["status"] == "pending" for item in consent.json()["confirmations"])
    blocked = context["owner"].post(
        f"/api/plans/{context['plan_id']}/settlement/arrival",
        json={"lot_id": context["lot_id"], "inspected_loss_pct": 2},
    )
    assert blocked.status_code == 409, blocked.text


def test_quote_expires_exactly_when_fee_rate_changes(policy_plan):
    context = policy_plan
    boundary = context["cutoff"] - timedelta(days=2)
    context["clock"][0] = boundary - timedelta(seconds=1)
    quote = _quote(context)
    assert quote["fee_krw"] == 199_200
    assert datetime.fromisoformat(quote["expires_at"]) == boundary
    context["clock"][0] = boundary
    stale = _submit(context, quote)
    assert stale.status_code == 409, stale.text
    assert _request_count(context) == 0
    assert _quote(context)["fee_krw"] == 332_000


def test_quote_replay_is_one_request_and_changed_key_payload_is_conflict(policy_plan):
    context = policy_plan
    context["clock"][0] = context["cutoff"] - timedelta(days=1)
    first, other = _quote(context), _quote(context, 3)
    submitted = _submit(context, first)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "queued"
    replay = _submit(context, first)
    assert replay.status_code == 200, replay.text
    assert replay.json()["change_request_id"] == submitted.json()["change_request_id"]
    conflict = _submit(context, other)
    assert conflict.status_code == 409, conflict.text
    assert _request_count(context) == 1
    assert submitted.json()["state"]["final_fee_krw"] == 0


def test_concurrent_same_nonce_creates_one_change_request(policy_plan):
    context = policy_plan
    context["clock"][0] = context["cutoff"] - timedelta(days=1)
    quote = _quote(context)
    clients = [TestClient(app, base_url="https://testserver", headers=SAAS_CSRF) for _ in range(2)]
    for client in clients:
        client.cookies.update(context["shipper"].cookies)
    barrier = threading.Barrier(2)

    def submit(client):
        barrier.wait(timeout=10)
        return client.post(
            context["path"],
            json={"quote_id": quote["quote_id"], "idempotency_key": "concurrent"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, clients))
    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["change_request_id"] for response in responses}) == 1
    assert _request_count(context) == 1


def test_changed_plan_version_invalidates_quote(policy_plan):
    context = policy_plan
    context["clock"][0] = context["cutoff"] - timedelta(days=1)
    quote = _quote(context)
    with database.begin() as db:
        db.get(PlanRow, context["plan_id"]).version += 1
    stale = _submit(context, quote)
    assert stale.status_code == 409, stale.text
    assert _request_count(context) == 0


def test_quantity_endpoint_is_scoped_to_actual_shipper(policy_plan):
    context = policy_plan
    outsider, _ = _onboarded("quantity-outsider@example.com", "다른 화주")
    assert outsider.get(context["path"]).status_code == 404
    assert outsider.post(f"{context['path']}/quote", json={"quantity": 0}).status_code == 404
    assert (
        context["owner"].post(f"{context['path']}/quote", json={"quantity": 0}).status_code == 403
    )


def test_caller_cannot_claim_ai_or_pilot_exemption(policy_plan):
    context = policy_plan
    context["clock"][0] = context["cutoff"] - timedelta(days=1)
    quote = _quote(context, ai_air=True, pilot=True, exemption="ai_air")
    assert quote["fee_krw"] == 332_000
    assert quote["exemption"] is None


def test_policy_cannot_be_changed_after_t3_without_prior_read(policy_plan):
    context = policy_plan
    initial = context["shipper"].get(context["path"])
    assert initial.status_code == 200, initial.text
    assert initial.json()["confirmed_quantity"] is None
    context["clock"][0] = context["cutoff"] - timedelta(days=3)
    body = {**initial.json()["policy"], "slot_rate_krw": 1}
    response = context["owner"].put(
        f"{PLANS}/{context['plan_id']}/voyages/{context['voyage_id']}/quantity-policy",
        json=body,
    )
    assert response.status_code == 409, response.text
    current = context["shipper"].get(context["path"])
    assert current.json()["policy"]["slot_rate_krw"] == 166_000


def _accept_plan(context):
    response = context["shipper"].post(
        f"/api/saas/shipper/plans/{context['plan_id']}/confirmation", json={"accept": True}
    )
    assert response.status_code == 200, response.text


def _complete_order(context, order_id):
    response = context["owner"].get(f"/api/plans/{context['plan_id']}/orders/{order_id}")
    assert response.status_code == 200, response.text
    order = response.json()
    assert order["cancellation_charges"] == []
    for confirmation in order["shipper_confirmations"]:
        if confirmation["status"] == "pending":
            accepted = context["shipper"].post(
                f"/api/saas/shipper/plans/{context['plan_id']}/orders/{order_id}/confirm",
                json={"accept": True, "shipper_id": confirmation["shipper_id"]},
            )
            assert accepted.status_code == 200, accepted.text
    accepted = context["owner"].post(
        f"{PLANS}/{context['plan_id']}/orders/{order_id}/confirm", json={"accept": True}
    )
    assert accepted.status_code == 200, accepted.text


def _apply_request(context, request_id):
    path = f"/api/saas/forwarder/change-requests/{request_id}"
    calculated = context["owner"].post(f"{path}/replan?llm=false")
    assert calculated.status_code == 200, calculated.text
    feasible = [item for item in calculated.json()["replan"]["alternatives"] if item["feasible"]]
    assert feasible, calculated.text
    alternative = next((item for item in feasible if item["id"] == "sail_as_is"), feasible[0])
    applied = context["owner"].post(
        f"{path}/apply", json={"alternative_id": alternative["id"], "idempotency_key": request_id}
    )
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["plan"]["active_order_id"]
    assert order_id
    _complete_order(context, order_id)
    return applied.json()


@pytest.mark.parametrize(
    ("quantity", "days_before", "refill_count", "gross", "remaining"),
    [(3, 2.5, 2, 49_800, 0), (0, 1, 1, 332_000, 249_000)],
)
def test_committed_cancellation_and_actual_refill_preserve_user_fees(
    policy_plan, quantity, days_before, refill_count, gross, remaining
):
    context = policy_plan
    _accept_plan(context)
    context["clock"][0] = context["cutoff"] - timedelta(days=days_before)
    registered = context["shipper"].post(
        "/api/saas/shipper/lots", json=_lot(pallets=refill_count, ready="2026-11-17")
    )
    assert registered.status_code == 201, registered.text
    spare_id = registered.json()["registered"]["id"]
    waited = context["shipper"].post(
        f"/api/saas/shipper/plans/{context['plan_id']}/voyages/{context['voyage_id']}/refill-waitlist",
        json={"lot_id": spare_id, "quantity": refill_count},
    )
    assert waited.status_code == 200, waited.text
    quote = _quote(context, quantity)
    assert quote["fee_krw"] == gross
    queued = _submit(context, quote)
    assert queued.status_code == 200, queued.text
    _apply_request(context, queued.json()["change_request_id"])
    charged = context["shipper"].get(context["path"])
    assert charged.status_code == 200, charged.text
    assert charged.json()["final_fee_krw"] == gross
    if quantity == 0:
        statement = context["shipper"].get(
            f"/api/saas/shipper/plans/{context['plan_id']}/quantity-statement"
        )
        assert statement.status_code == 200, statement.text
        credits = {item["lot_id"]: item["compensation_credit_krw"] for item in statement.json()}
        assert [credits[lot_id] for lot_id in context["support_ids"]] == [221_333, 110_667]
    offers = context["shipper"].get("/api/saas/shipper/refill-offers")
    assert offers.status_code == 200, offers.text
    [offer] = [item for item in offers.json() if item["status"] == "pending"]
    assert offer["lot_id"] == spare_id
    assert offer["voyage_id"] == context["voyage_id"]
    accepted = context["shipper"].post(
        f"/api/saas/shipper/refill-offers/{offer['offer_id']}/accept",
        json={"quantity": refill_count, "idempotency_key": "accept-refill"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "pending_execution"
    assert context["shipper"].get(context["path"]).json()["final_fee_krw"] == gross
    _complete_order(context, accepted.json()["order_id"])
    refreshed = context["owner"].get(f"{PLANS}/{context['plan_id']}/quantity-policy")
    assert refreshed.status_code == 200, refreshed.text
    final = context["shipper"].get(context["path"]).json()
    assert final["gross_fee_krw"] == gross
    assert final["refill_credit_krw"] == gross - remaining
    assert final["final_fee_krw"] == remaining
    statement = context["shipper"].get(
        f"/api/saas/shipper/plans/{context['plan_id']}/quantity-statement"
    )
    assert statement.status_code == 200, statement.text
    assert sum(item["net_adjustment_krw"] for item in statement.json()) == 0
    assert all(item["platform_revenue_krw"] == 0 for item in statement.json())
    assert all(item["finalized"] is False for item in statement.json())


def test_rejected_quantity_request_releases_quote_workflow_immediately(policy_plan):
    context = policy_plan
    context["clock"][0] = context["cutoff"] - timedelta(days=1)
    submitted = _submit(context, _quote(context, 3))
    assert submitted.status_code == 200, submitted.text
    rejected = context["owner"].post(
        f"/api/saas/forwarder/change-requests/{submitted.json()['change_request_id']}/reject",
        json={"note": "다른 수량으로 재요청"},
    )
    assert rejected.status_code == 200, rejected.text
    quoted = context["shipper"].post(f"{context['path']}/quote", json={"quantity": 4})
    assert quoted.status_code == 200, quoted.text
    assert quoted.json()["fee_krw"] == 0


def _prepare_refill_offer(context):
    _accept_plan(context)
    context["clock"][0] = context["cutoff"] - timedelta(days=2, hours=12)
    registered = context["shipper"].post(
        "/api/saas/shipper/lots", json=_lot(pallets=2, ready="2026-11-17")
    )
    assert registered.status_code == 201, registered.text
    lot_id = registered.json()["registered"]["id"]
    waited = context["shipper"].post(
        f"/api/saas/shipper/plans/{context['plan_id']}/voyages/{context['voyage_id']}/refill-waitlist",
        json={"lot_id": lot_id, "quantity": 2},
    )
    assert waited.status_code == 200, waited.text
    queued = _submit(context, _quote(context, 3))
    assert queued.status_code == 200, queued.text
    _apply_request(context, queued.json()["change_request_id"])
    offered = context["shipper"].get("/api/saas/shipper/refill-offers")
    assert offered.status_code == 200, offered.text
    [offer] = [item for item in offered.json() if item["status"] == "pending"]
    return offer


def test_two_sessions_accept_one_offer_without_duplicate_lot_claim(policy_plan):
    context = policy_plan
    offer = _prepare_refill_offer(context)
    clients = [TestClient(app, base_url="https://testserver", headers=SAAS_CSRF) for _ in range(2)]
    for client in clients:
        client.cookies.update(context["shipper"].cookies)
    barrier = threading.Barrier(2)

    def accept(item):
        index, client = item
        barrier.wait(timeout=10)
        return client.post(
            f"/api/saas/shipper/refill-offers/{offer['offer_id']}/accept",
            json={"quantity": 2, "idempotency_key": f"accept-{index}"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(accept, enumerate(clients)))
    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["order_id"] for response in responses}) == 1
    state = context["owner"].get(f"/api/plans/{context['plan_id']}")
    assert state.status_code == 200, state.text
    assignments = state.json()["plan"]["assignments"]
    assert sum(item["pallets"] for item in assignments if item["mode"] == "sea") == 17
    assert len([item for item in assignments if item["lot_id"] == offer["lot_id"]]) == 1
    assert context["shipper"].get(context["path"]).json()["refill_credit_krw"] == 0


def test_offer_acceptance_exactly_at_expiry_cannot_claim_capacity(policy_plan):
    context = policy_plan
    offer = _prepare_refill_offer(context)
    context["clock"][0] = datetime.fromisoformat(offer["expires_at"])
    restarted_client = TestClient(app, base_url="https://testserver", headers=SAAS_CSRF)
    restarted_client.cookies.update(context["shipper"].cookies)
    response = restarted_client.post(
        f"/api/saas/shipper/refill-offers/{offer['offer_id']}/accept",
        json={"quantity": 2, "idempotency_key": "late-accept"},
    )
    assert response.status_code in (200, 409), response.text
    if response.status_code == 200:
        assert response.json()["status"] == "expired"
    persisted = restarted_client.get("/api/saas/shipper/refill-offers")
    assert persisted.status_code == 200, persisted.text
    assert (
        next(item for item in persisted.json() if item["offer_id"] == offer["offer_id"])["status"]
        == "expired"
    )
    state = context["owner"].get(f"/api/plans/{context['plan_id']}").json()
    assert state["status"] == "committed"
    assert offer["lot_id"] not in {item["lot_id"] for item in state["plan"]["assignments"]}
    assert context["shipper"].get(context["path"]).json()["final_fee_krw"] == 49_800


def test_unrelated_same_voyage_addition_does_not_create_refill_credit(policy_plan):
    context = policy_plan
    _accept_plan(context)
    context["clock"][0] = context["cutoff"] - timedelta(days=2, hours=12)
    queued = _submit(context, _quote(context, 3))
    assert queued.status_code == 200, queued.text
    _apply_request(context, queued.json()["change_request_id"])
    registered = context["shipper"].post(
        "/api/saas/shipper/lots", json=_lot(pallets=2, ready="2026-11-17")
    )
    assert registered.status_code == 201, registered.text
    lot_id = registered.json()["registered"]["id"]
    calculated = context["owner"].post(
        f"{PLANS}/{context['plan_id']}/add-lot?llm=false", json={"lot_id": lot_id}
    )
    assert calculated.status_code == 200, calculated.text
    result = calculated.json()
    feasible = [item for item in result["alternatives"] if item["feasible"]]
    assert feasible, calculated.text
    applied = context["owner"].post(
        f"/api/plans/{context['plan_id']}/apply",
        json={"replan_id": result["replan_id"], "alternative_id": feasible[0]["id"]},
    )
    assert applied.status_code == 200, applied.text
    _complete_order(context, applied.json()["active_order_id"])
    refreshed = context["owner"].get(f"{PLANS}/{context['plan_id']}/quantity-policy")
    assert refreshed.status_code == 200, refreshed.text
    current = context["shipper"].get(context["path"]).json()
    assert current["gross_fee_krw"] == 49_800
    assert current["refill_credit_krw"] == 0
    assert current["final_fee_krw"] == 49_800


def test_departure_statement_requires_actual_confirmation_and_replays_frozen_snapshot(policy_plan):
    from freshswitch_backend.persistence.tables import QuantityPolicyRow
    from freshswitch_backend.planning.quantity_policy import load_ledger, statement

    context = policy_plan
    _accept_plan(context)
    context["clock"][0] = context["cutoff"] - timedelta(days=2, hours=12)
    queued = _submit(context, _quote(context, 3))
    assert queued.status_code == 200, queued.text
    _apply_request(context, queued.json()["change_request_id"])
    path = f"{PLANS}/{context['plan_id']}/voyages/{context['voyage_id']}/quantity-departure"
    body = {"idempotency_key": "departure-proof", "departure_observed": True}
    before = context["owner"].post(path, json=body)
    assert before.status_code == 409, before.text
    departure = datetime.fromisoformat(context["plan"]["scenario"]["voyages"][0]["etd"])
    context["clock"][0] = departure
    provisional = context["shipper"].get(
        f"/api/saas/shipper/plans/{context['plan_id']}/quantity-statement"
    )
    assert provisional.status_code == 200, provisional.text
    assert all(item["finalized"] is False for item in provisional.json())
    confirmed = context["owner"].post(path, json=body)
    assert confirmed.status_code == 200, confirmed.text
    assert all(item["finalized"] is True for item in confirmed.json())
    assert sum(item["net_adjustment_krw"] for item in confirmed.json()) == 0
    assert context["owner"].post(path, json=body).json() == confirmed.json()
    with database.begin() as db:
        row = db.get(QuantityPolicyRow, (context["plan_id"], context["voyage_id"]))
        ledger = load_ledger(row)
        assert ledger.finalized_statement["rows"] == confirmed.json()
        ledger.reservations[context["lot_id"]].current_quantity = 1
        snapshot_read = [item.model_dump(mode="json") for item in statement(row, ledger)]
        assert snapshot_read == confirmed.json()


def test_departure_guard_uses_locked_plan_status_after_concurrent_change(policy_plan, monkeypatch):
    from freshswitch_backend.api import saas_quantity_policy

    context = policy_plan
    _accept_plan(context)
    context["clock"][0] = datetime.fromisoformat(context["plan"]["scenario"]["voyages"][0]["etd"])
    original = saas_quantity_policy.plan_for

    def changed_after_read(plan_id, principal):
        access = original(plan_id, principal)
        with database.begin() as db:
            row = db.get(PlanRow, plan_id)
            row.status = "pending_confirmation"
            row.payload = {**row.payload, "status": "pending_confirmation"}
        return access

    monkeypatch.setattr(saas_quantity_policy, "plan_for", changed_after_read)
    response = context["owner"].post(
        f"{PLANS}/{context['plan_id']}/voyages/{context['voyage_id']}/quantity-departure",
        json={"idempotency_key": "stale-access", "departure_observed": True},
    )
    assert response.status_code == 409, response.text


@pytest.mark.parametrize(
    "body",
    [{"idempotency_key": "missing"}, {"idempotency_key": "false", "departure_observed": False}],
)
def test_departure_requires_explicit_observed_departure(policy_plan, body):
    context = policy_plan
    _accept_plan(context)
    context["clock"][0] = datetime.fromisoformat(context["plan"]["scenario"]["voyages"][0]["etd"])
    response = context["owner"].post(
        f"{PLANS}/{context['plan_id']}/voyages/{context['voyage_id']}/quantity-departure",
        json=body,
    )
    assert response.status_code == 422, response.text


def test_quantity_holding_window_opens_exactly_at_t7(policy_plan):
    context = policy_plan
    opens = context["cutoff"] - timedelta(days=7)
    context["clock"][0] = opens - timedelta(microseconds=1)
    initial = context["shipper"].get(context["path"])
    assert initial.status_code == 200, initial.text
    assert initial.json()["quantity_status"] == "scheduled"
    assert initial.json()["can_adjust"] is False
    blocked = context["shipper"].post(f"{context['path']}/quote", json={"quantity": 3})
    assert blocked.status_code == 409, blocked.text
    context["clock"][0] = opens
    opened = context["shipper"].get(context["path"])
    assert opened.json()["quantity_status"] == "reserved"
    assert opened.json()["can_adjust"] is True
    assert _quote(context, 3)["fee_krw"] == 0


def test_plan_deletion_cannot_skip_due_quantity_confirmation(policy_plan):
    context = policy_plan
    context["clock"][0] = context["cutoff"] - timedelta(days=3)
    response = context["owner"].delete(f"/api/plans/{context['plan_id']}")
    assert response.status_code == 409, response.text
    assert context["shipper"].get(context["path"]).status_code == 200


def test_direct_lot_mutation_and_plan_deletion_preserve_cancellation_ledger(policy_plan):
    context = policy_plan
    _accept_plan(context)
    context["clock"][0] = context["cutoff"] - timedelta(days=2, hours=12)
    queued = _submit(context, _quote(context, 3))
    assert queued.status_code == 200, queued.text
    _apply_request(context, queued.json()["change_request_id"])
    lot_path = f"/api/saas/shipper/lots/{context['lot_id']}"
    assert context["shipper"].patch(lot_path, json={"pallets": 1}).status_code == 409
    assert context["shipper"].patch(lot_path, json={"status": "cancelled"}).status_code == 409
    assert context["shipper"].delete(lot_path).status_code == 409
    assert context["owner"].delete(f"/api/plans/{context['plan_id']}").status_code == 409
    current = context["shipper"].get(context["path"])
    assert current.status_code == 200, current.text
    assert current.json()["current_quantity"] == 3
    assert current.json()["final_fee_krw"] == 49_800


@pytest.mark.parametrize("previously_accepted", [False, True])
def test_initial_rejection_exemption_does_not_erase_previously_accepted_commitment(
    policy_plan, previously_accepted
):
    from fastapi import HTTPException

    from freshswitch_backend.persistence.tables import PlanConfirmationRow, QuantityPolicyRow
    from freshswitch_backend.planning.quantity_policy import load_ledger, sync_committed
    from freshswitch_backend.planning.store import PlanSession

    context = policy_plan
    context["clock"][0] = context["cutoff"] - timedelta(days=1)
    with database.begin() as db:
        plan_row = db.get(PlanRow, context["plan_id"], with_for_update=True)
        confirmation = db.scalar(
            select(PlanConfirmationRow).where(
                PlanConfirmationRow.plan_id == context["plan_id"], PlanConfirmationRow.round == 1
            )
        )
        confirmation.status = "accepted" if previously_accepted else "rejected"
        if previously_accepted:
            db.add(
                PlanConfirmationRow(
                    plan_id=context["plan_id"],
                    round=2,
                    shipper_id=confirmation.shipper_id,
                    shipper_org_id=confirmation.shipper_org_id,
                    lot_ids=confirmation.lot_ids,
                    status="rejected",
                    reason="새 배정 조건 거절",
                    auto_accepted=False,
                    created_at=context["clock"][0],
                    decided_at=context["clock"][0],
                )
            )
            plan_row.confirmation_round = 2
        db.flush()
        session = PlanSession.model_validate(plan_row.payload)
        after = session.model_copy(deep=True)
        after.plan.assignments = [
            item for item in after.plan.assignments if item.lot_id != context["lot_id"]
        ]
        after.scenario.lots = [item for item in after.scenario.lots if item.id != context["lot_id"]]
        after.version = after.committed_version = 2
        after.updated_at = context["clock"][0]
        if previously_accepted:
            with pytest.raises(HTTPException) as caught:
                sync_committed(db, plan_row, after, context["clock"][0])
            assert caught.value.status_code == 409
            row = db.get(QuantityPolicyRow, (context["plan_id"], context["voyage_id"]))
            assert load_ledger(row).reservations[context["lot_id"]].current_quantity == 5
            return
        sync_committed(db, plan_row, after, context["clock"][0])
        row = db.get(QuantityPolicyRow, (context["plan_id"], context["voyage_id"]))
        reservation = load_ledger(row).reservations[context["lot_id"]]
        assert reservation.current_quantity == 0
        assert reservation.events == []


@pytest.mark.parametrize("forged_exemption", [False, True])
def test_manual_air_switch_cannot_bypass_positive_cancellation_fee(policy_plan, forged_exemption):
    from freshswitch_backend.persistence.tables import QuantityPolicyRow
    from freshswitch_backend.planning.quantity_policy import load_ledger

    context = policy_plan
    _accept_plan(context)
    context["clock"][0] = context["cutoff"] - timedelta(days=2, hours=12)
    assert _quote(context, 0)["fee_krw"] == 199_200
    event = {"type": "air_switch", "lot_id": context["lot_id"]}
    if forged_exemption:
        event.update(
            {
                "ai_air": True,
                "pilot": True,
                "exemption": "ai_air",
                "source": "server_forecast",
                "occurred_at": (context["cutoff"] - timedelta(days=4)).isoformat(),
            }
        )
    calculated = context["owner"].post(
        f"/api/plans/{context['plan_id']}/events?llm=false", json={"event": event}
    )
    assert calculated.status_code == 200, calculated.text
    alternative = next(item for item in calculated.json()["alternatives"] if item["feasible"])
    applied = context["owner"].post(
        f"/api/plans/{context['plan_id']}/apply",
        json={"replan_id": calculated.json()["replan_id"], "alternative_id": alternative["id"]},
    )
    assert applied.status_code == 409, applied.text
    assert "수량 변경 견적" in applied.json()["detail"]
    with database.begin() as db:
        ledger = load_ledger(db.get(QuantityPolicyRow, (context["plan_id"], context["voyage_id"])))
        assert ledger.ai_air_recommendations == {}
        assert ledger.requests == {}
    current = context["shipper"].get(context["path"])
    assert current.status_code == 200, current.text
    assert current.json()["current_quantity"] == 5
    assert current.json()["events"] == []
    assert context["owner"].get(f"/api/plans/{context['plan_id']}/orders").json() == []


def test_zero_fee_manual_air_switch_keeps_existing_execution_consent(policy_plan):
    context = policy_plan
    _accept_plan(context)
    context["clock"][0] = context["cutoff"] - timedelta(days=4)
    assert _quote(context, 0)["fee_krw"] == 0
    calculated = context["owner"].post(
        f"/api/plans/{context['plan_id']}/events?llm=false",
        json={"event": {"type": "air_switch", "lot_id": context["lot_id"]}},
    )
    assert calculated.status_code == 200, calculated.text
    alternative = next(item for item in calculated.json()["alternatives"] if item["feasible"])
    applied = context["owner"].post(
        f"/api/plans/{context['plan_id']}/apply",
        json={"replan_id": calculated.json()["replan_id"], "alternative_id": alternative["id"]},
    )
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["active_order_id"]
    assert order_id
    order = context["owner"].get(f"/api/plans/{context['plan_id']}/orders/{order_id}").json()
    assert any(item["status"] == "pending" for item in order["shipper_confirmations"])
    premature = context["owner"].post(
        f"{PLANS}/{context['plan_id']}/orders/{order_id}/confirm", json={"accept": True}
    )
    assert premature.status_code == 409, premature.text
    assert context["shipper"].get(context["path"]).json()["current_quantity"] == 5
    _complete_order(context, order_id)
    final = context["shipper"].get(context["path"])
    assert final.status_code == 200, final.text
    assert final.json()["current_quantity"] == 0
    assert final.json()["final_fee_krw"] == 0
    assert final.json()["events"][0]["exemption"] is None
