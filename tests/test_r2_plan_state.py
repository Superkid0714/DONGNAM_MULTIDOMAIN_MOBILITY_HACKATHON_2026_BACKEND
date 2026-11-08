import asyncio

from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, PLATFORM_ORG_ID
from sqlalchemy import select, update
from test_add_lot import _plan_with_spare, _reject_order
from test_plan_confirm import PLATFORM, REDUCE, _decide, _plan, _state

from freshswitch_backend.api.plans import EventRequest, replan_event
from freshswitch_backend.lots import RegisteredLot
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import LotRow, PendingRow, PlanConfirmationRow
from freshswitch_backend.planning.store import RECOMPUTE_AFTER_ORDER, REGISTERED_ADD

POHANG_LOT = "lot-pohang-gr-1"
CANCEL_POHANG = {"type": "cancel", "lot_id": POHANG_LOT}


def _assign(platform_client: TestClient, plan_id: str) -> None:
    assigned = platform_client.put(
        f"{PLATFORM}/{plan_id}/forwarder", json={"forwarder_org_id": FORWARDER_ORG_ID}
    )
    assert assigned.status_code == 200, assigned.text


def _event(platform_client: TestClient, plan_id: str, event: dict) -> dict:
    response = platform_client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert response.status_code == 200, response.text
    return response.json()


def _apply(platform_client: TestClient, plan_id: str, result: dict) -> dict:
    applied = platform_client.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": result["recommended_id"], "replan_id": result["replan_id"]},
    )
    assert applied.status_code == 200, applied.text
    return applied.json()


def _order(platform_client: TestClient, plan_id: str, order_id: str) -> dict:
    response = platform_client.get(f"/api/plans/{plan_id}/orders/{order_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _confirm_order(
    platform_client: TestClient,
    saas_login,
    forwarder_client: TestClient,
    plan_id: str,
    order_id: str,
) -> dict:
    for c in _order(platform_client, plan_id, order_id)["shipper_confirmations"]:
        answered = saas_login(shipper_org_id(c["shipper_id"])).post(
            f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": True, "shipper_id": c["shipper_id"]},
        )
        assert answered.status_code == 200, answered.text
    done = forwarder_client.post(
        f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm", json={"accept": True}
    )
    assert done.status_code == 200, done.text
    return done.json()


def _reject_pohang(saas_login, plan_id: str) -> None:
    rejected = _decide(saas_login, plan_id, "sh-pohang", {"accept": False, "reason": "가격"})
    assert rejected.status_code == 200, rejected.text


def _accept_others(saas_login, platform_client: TestClient, plan_id: str) -> None:
    for c in _state(platform_client, plan_id)["confirmations"]:
        if c["shipper_id"] != "sh-pohang" and c["status"] == "pending":
            accepted = _decide(saas_login, plan_id, c["shipper_id"], {"accept": True})
            assert accepted.status_code == 200, accepted.text


def _rejected_candidate(platform_client: TestClient, plan_id: str) -> dict:
    state = _state(platform_client, plan_id)
    assert state["needs_replan"] is True
    return next(c for c in state["replan_candidates"] if c["source"] == "shipper_rejected")


def _charges(result: dict) -> list:
    diffs = [a["diff"] for a in result["alternatives"]]
    return [d.get("cancellation") for d in diffs if d.get("cancellation")]


def test_recompute_refuses_plan_whose_order_the_forwarder_confirmed(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _plan(platform_client, saas_login)["plan_id"]
    _assign(platform_client, plan_id)
    reduced = _apply(platform_client, plan_id, _event(platform_client, plan_id, REDUCE))
    first_order = reduced["active_order_id"]
    _confirm_order(platform_client, saas_login, forwarder_client, plan_id, first_order)
    before = platform_client.get(f"/api/plans/{plan_id}").json()
    assert before["status"] == "pending_shipper_confirmation"
    _reject_pohang(saas_login, plan_id)

    blocked = platform_client.post(f"{PLATFORM}/{plan_id}/confirmation/recompute", json={})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == RECOMPUTE_AFTER_ORDER
    after = platform_client.get(f"/api/plans/{plan_id}").json()
    assert after["version"] == before["version"]
    assert after["plan"]["assignments"] == before["plan"]["assignments"]

    candidate = _rejected_candidate(platform_client, plan_id)
    result = _event(platform_client, plan_id, candidate["events"][0])
    applied = _apply(platform_client, plan_id, result)
    assert applied["status"] == "pending_confirmation"
    assert applied["active_order_id"] != first_order
    assert POHANG_LOT not in {lot["id"] for lot in applied["scenario"]["lots"]}


def test_rejected_shipper_candidate_waives_fee_and_skips_their_confirmation(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(platform_client, saas_login)["plan_id"]
    charged = _event(platform_client, plan_id, CANCEL_POHANG)
    assert any(c["fee"] > 0 for c in _charges(charged))

    _reject_pohang(saas_login, plan_id)
    candidate = _rejected_candidate(platform_client, plan_id)
    assert candidate["events"] == [CANCEL_POHANG]
    result = _event(platform_client, plan_id, candidate["events"][0])
    assert _charges(result) == []
    for alternative in result["alternatives"]:
        assert all(d["cancellation_adjustment"] == 0 for d in alternative["diff"]["shippers"])

    applied = _apply(platform_client, plan_id, result)
    order = _order(platform_client, plan_id, applied["active_order_id"])
    assert "sh-pohang" not in {c["shipper_id"] for c in order["shipper_confirmations"]}
    assert order["cancellation"] is None
    assert order["cancellation_charges"] == []


def test_applied_rejection_candidate_survives_order_rollback(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(platform_client, saas_login)["plan_id"]
    _assign(platform_client, plan_id)
    _reject_pohang(saas_login, plan_id)
    candidate = _rejected_candidate(platform_client, plan_id)
    applied = _apply(
        platform_client, plan_id, _event(platform_client, plan_id, candidate["events"][0])
    )
    assert _rejected_candidate(platform_client, plan_id)["id"] == candidate["id"]

    _reject_order(platform_client, saas_login, plan_id, applied["active_order_id"])
    plan = platform_client.get(f"/api/plans/{plan_id}").json()
    assert POHANG_LOT in {lot["id"] for lot in plan["scenario"]["lots"]}
    assert _rejected_candidate(platform_client, plan_id)["id"] == candidate["id"]


def test_applied_declined_request_candidate_survives_order_rollback(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _plan(platform_client, saas_login, auto_accept=True)["plan_id"]
    _assign(platform_client, plan_id)
    reduced = _apply(platform_client, plan_id, _event(platform_client, plan_id, REDUCE))
    order_id = reduced["active_order_id"]
    order = _confirm_order(platform_client, saas_login, forwarder_client, plan_id, order_id)
    request = next(
        r
        for r in order["requests"]
        if r["status"] == "pending"
        and r["counterparty_role"] in ("carrier", "terminal")
        and r["kind"] != "booking_cancel"
    )
    declined = forwarder_client.post(
        f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/requests/{request['request_id']}",
        json={"accepted": False, "note": "선복 부족"},
    )
    assert declined.status_code == 200, declined.text
    [candidate] = _state(platform_client, plan_id)["replan_candidates"]
    assert candidate["events"]

    switched = _apply(
        platform_client, plan_id, _event(platform_client, plan_id, candidate["events"][0])
    )
    state = _state(platform_client, plan_id)
    assert state["needs_replan"] is True
    assert [c["id"] for c in state["replan_candidates"]] == [candidate["id"]]

    _reject_order(platform_client, saas_login, plan_id, switched["active_order_id"])
    state = _state(platform_client, plan_id)
    assert state["needs_replan"] is True
    assert [c["id"] for c in state["replan_candidates"]] == [candidate["id"]]
    attention = platform_client.get("/api/saas/platform/plan-confirmations").json()
    assert plan_id in {s["plan_id"] for s in attention}


def test_rejected_shipper_removed_by_cancel_leaves_attention_list(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _plan(platform_client, saas_login)["plan_id"]
    _assign(platform_client, plan_id)
    _accept_others(saas_login, platform_client, plan_id)
    _reject_pohang(saas_login, plan_id)
    candidate = _rejected_candidate(platform_client, plan_id)
    applied = _apply(
        platform_client, plan_id, _event(platform_client, plan_id, candidate["events"][0])
    )
    attention = platform_client.get("/api/saas/platform/plan-confirmations").json()
    assert plan_id in {s["plan_id"] for s in attention}

    _confirm_order(
        platform_client, saas_login, forwarder_client, plan_id, applied["active_order_id"]
    )
    state = _state(platform_client, plan_id)
    assert state["status"] == "committed"
    assert "sh-pohang" not in {c["shipper_id"] for c in state["confirmations"]}
    assert state["needs_replan"] is False
    assert state["replan_candidates"] == []
    attention = platform_client.get("/api/saas/platform/plan-confirmations").json()
    assert plan_id not in {s["plan_id"] for s in attention}
    pohang = saas_login(shipper_org_id("sh-pohang"))
    assert pohang.get("/api/saas/shipper/plan-confirmations").json() == []


def test_rejection_without_lots_in_plan_opens_no_candidate(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(platform_client, saas_login)["plan_id"]
    with database.begin() as db:
        db.execute(
            update(PlanConfirmationRow)
            .where(
                PlanConfirmationRow.plan_id == plan_id,
                PlanConfirmationRow.shipper_id == "sh-jinju",
            )
            .values(lot_ids=["lot-gone"])
        )
    rejected = _decide(saas_login, plan_id, "sh-jinju", {"accept": False, "reason": "가격"})
    assert rejected.status_code == 200, rejected.text
    state = _state(platform_client, plan_id)
    assert state["needs_replan"] is False
    assert state["replan_candidates"] == []


def test_lot_deleted_between_add_lot_and_apply_is_refused(
    platform_client: TestClient, saas_login
) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login)
    computed = platform_client.post(f"{PLATFORM}/{plan_id}/add-lot", json={"lot_id": extra})
    assert computed.status_code == 200, computed.text
    feasible = next(a["id"] for a in computed.json()["alternatives"] if a["feasible"])
    deleted = saas_login("shipper-jinju").delete(f"/api/saas/shipper/lots/{extra}")
    assert deleted.status_code == 204, deleted.text
    before = platform_client.get(f"/api/plans/{plan_id}").json()

    applied = platform_client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": feasible})
    assert applied.status_code == 409, applied.text
    plan = platform_client.get(f"/api/plans/{plan_id}").json()
    assert plan["version"] == before["version"]
    assert extra not in {lot["id"] for lot in plan["scenario"]["lots"]}
    assert extra not in plan["registered_lot_ids"]
    with database.begin() as db:
        rows = db.scalars(
            select(PlanConfirmationRow).where(PlanConfirmationRow.plan_id == plan_id)
        ).all()
        assert all(extra not in row.lot_ids for row in rows)


def test_declined_airline_request_survives_later_request_answers(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _plan(platform_client, saas_login, auto_accept=True)["plan_id"]
    _assign(platform_client, plan_id)
    reduced = _apply(platform_client, plan_id, _event(platform_client, plan_id, REDUCE))
    order_id = reduced["active_order_id"]
    order = _confirm_order(platform_client, saas_login, forwarder_client, plan_id, order_id)
    pending = [r for r in order["requests"] if r["status"] == "pending"]
    air = next(r for r in pending if r["counterparty_role"] == "airline")
    others = [r for r in pending if r["request_id"] != air["request_id"]]
    assert others
    url = f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/requests/"
    declined = forwarder_client.post(
        url + air["request_id"], json={"accepted": False, "note": "항공 취소 불가"}
    )
    assert declined.status_code == 200, declined.text
    [candidate] = _state(platform_client, plan_id)["replan_candidates"]

    for other in others:
        answered = forwarder_client.post(url + other["request_id"], json={"accepted": True})
        assert answered.status_code == 200, answered.text
        state = _state(platform_client, plan_id)
        assert state["needs_replan"] is True
        assert [c["id"] for c in state["replan_candidates"]] == [candidate["id"]]


def test_add_lot_event_is_marked_in_the_same_transaction_as_its_pending_row(
    platform_client: TestClient, saas_login
) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login)
    with database.begin() as db:
        row = RegisteredLot.model_validate(db.get(LotRow, extra), from_attributes=True)
    request = EventRequest.model_validate({"event": {"type": "add_lot", "lot": row.to_lot()}})
    response = asyncio.run(replan_event(plan_id, request, PLATFORM_ORG_ID, llm=False))
    result = response.model_dump(mode="json")
    with database.begin() as db:
        assert db.get(PendingRow, result["replan_id"]).payload[REGISTERED_ADD] == extra
    feasible = next(a["id"] for a in result["alternatives"] if a["feasible"])
    deleted = saas_login("shipper-jinju").delete(f"/api/saas/shipper/lots/{extra}")
    assert deleted.status_code == 204, deleted.text

    applied = platform_client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": feasible})
    assert applied.status_code == 409, applied.text
    plan = platform_client.get(f"/api/plans/{plan_id}").json()
    assert extra not in {lot["id"] for lot in plan["scenario"]["lots"]}
