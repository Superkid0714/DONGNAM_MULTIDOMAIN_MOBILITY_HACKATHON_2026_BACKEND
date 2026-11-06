from datetime import UTC, datetime

from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    accept_plan,
    create_real_platform,
    open_quantity_window,
    password_client,
    queue_quantity,
    saas_app,
)
from sqlalchemy import func, select, update

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Assignment, Plan
from freshswitch_backend.execution.models import ReplanCandidate
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import (
    OrganizationRow,
    PlanConfirmationRow,
    PlanRow,
)
from freshswitch_backend.planning.store import _still_relevant

PLATFORM = "/api/saas/platform/plans"
FORWARDER = "/api/saas/forwarder/plans"
ATTENTION = "/api/saas/forwarder/plan-confirmations"
REDUCE = {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}


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


def _register(saas_login, skip: tuple[str, ...] = ()) -> list[str]:
    scenario = get_scenario("default")
    assert scenario is not None
    ids = []
    for lot in scenario.lots:
        if lot.shipper_id in skip:
            continue
        response = saas_login(shipper_org_id(lot.shipper_id)).post(
            "/api/saas/shipper/lots", json=_lot_body(lot)
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    return ids


def _plan(forwarder_client: TestClient, saas_login, **options) -> dict:
    skip = options.pop("skip", ())
    body = {"lot_ids": _register(saas_login, skip), **options}
    created = forwarder_client.post(FORWARDER, json=body)
    assert created.status_code == 201, created.text
    return created.json()


def _state(forwarder_client: TestClient, plan_id: str) -> dict:
    response = forwarder_client.get(f"{FORWARDER}/{plan_id}/confirmation")
    assert response.status_code == 200, response.text
    return response.json()


def _decide(saas_login, plan_id: str, shipper_id: str, body: dict):
    return saas_login(shipper_org_id(shipper_id)).post(
        f"/api/saas/shipper/plans/{plan_id}/confirmation", json=body
    )


def _kinds(client: TestClient, plan_id: str) -> list[str]:
    items = client.get(f"/api/saas/shipper/notifications?plan_id={plan_id}").json()
    return [n["kind"] for n in items]


def test_saas_plan_waits_for_every_shipper_then_commits(
    forwarder_client: TestClient, saas_login
) -> None:
    plan = _plan(forwarder_client, saas_login)
    plan_id = plan["plan_id"]
    assert plan["status"] == "pending_shipper_confirmation"

    state = _state(forwarder_client, plan_id)
    shippers = sorted({lot["shipper_id"] for lot in plan["scenario"]["lots"]})
    assert state["round"] == 1
    assert [c["shipper_id"] for c in state["confirmations"]] == shippers
    assert all(c["status"] == "pending" for c in state["confirmations"])
    assert state["needs_replan"] is False

    jinju = saas_login("shipper-jinju")
    assert _kinds(jinju, plan_id) == ["plan_confirmation_requested"]
    mine = jinju.get(f"/api/saas/shipper/plans/{plan_id}/confirmation").json()
    assert mine["plan_status"] == "pending_shipper_confirmation"
    assert [c["shipper_id"] for c in mine["confirmations"]] == ["sh-jinju"]
    assert mine["confirmations"][0]["lot_ids"] == ["lot-jinju-sb-1"]
    listed = jinju.get("/api/saas/shipper/plan-confirmations?status=pending").json()
    assert [(c["plan_id"], c["shipper_id"]) for c in listed] == [(plan_id, "sh-jinju")]

    attention = forwarder_client.get(ATTENTION).json()
    assert [s["plan_id"] for s in attention] == [plan_id]

    for shipper_id in shippers[:-1]:
        accepted = _decide(saas_login, plan_id, shipper_id, {"accept": True})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["plan_status"] == "pending_shipper_confirmation"
    again = _decide(saas_login, plan_id, shippers[0], {"accept": True})
    assert again.status_code == 409

    last = _decide(saas_login, plan_id, shippers[-1], {"accept": True})
    assert last.status_code == 200, last.text
    assert last.json()["plan_status"] == "committed"
    assert forwarder_client.get(f"/api/plans/{plan_id}").json()["status"] == "committed"
    assert _state(forwarder_client, plan_id)["status"] == "committed"
    assert forwarder_client.get(ATTENTION).json() == []


def test_shipper_outside_plan_cannot_decide(forwarder_client: TestClient, saas_login) -> None:
    plan_id = _plan(forwarder_client, saas_login, skip=("sh-sangju",))["plan_id"]
    outsider = _decide(saas_login, plan_id, "sh-sangju", {"accept": True})
    assert outsider.status_code == 404
    shippers = [c["shipper_id"] for c in _state(forwarder_client, plan_id)["confirmations"]]
    assert "sh-sangju" not in shippers


def test_rejection_needs_reason_and_opens_replan_entry(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)["plan_id"]
    blank = _decide(saas_login, plan_id, "sh-pohang", {"accept": False, "reason": "  "})
    assert blank.status_code == 422

    rejected = _decide(
        saas_login, plan_id, "sh-pohang", {"accept": False, "reason": "출하일이 안 맞습니다"}
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["confirmations"][0]["status"] == "rejected"
    flipped = _decide(saas_login, plan_id, "sh-pohang", {"accept": True})
    assert flipped.status_code == 409

    state = _state(forwarder_client, plan_id)
    assert state["status"] == "pending_shipper_confirmation"
    assert state["needs_replan"] is True
    [candidate] = state["replan_candidates"]
    assert candidate["source"] == "shipper_rejected"
    assert candidate["shipper_id"] == "sh-pohang"
    assert candidate["lot_ids"] == ["lot-pohang-gr-1"]
    assert candidate["note"] == "출하일이 안 맞습니다"
    assert candidate["events"] == [{"type": "cancel", "lot_id": "lot-pohang-gr-1"}]

    jinju = saas_login("shipper-jinju").get(f"/api/saas/shipper/plans/{plan_id}/confirmation")
    assert "replan_candidates" not in jinju.json()
    assert "sh-pohang" not in jinju.text


def test_recompute_without_rejected_shipper_releases_lots_and_reasks(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)["plan_id"]
    _decide(saas_login, plan_id, "sh-jinju", {"accept": True})
    _decide(saas_login, plan_id, "sh-pohang", {"accept": False, "reason": "가격"})

    recomputed = forwarder_client.post(f"{FORWARDER}/{plan_id}/confirmation/recompute", json={})
    assert recomputed.status_code == 200, recomputed.text
    plan = recomputed.json()["plan"]
    state = recomputed.json()["confirmation"]
    assert plan["version"] == 2
    assert plan["status"] == "pending_shipper_confirmation"
    assert "lot-pohang-gr-1" not in {lot["id"] for lot in plan["scenario"]["lots"]}
    assert "lot-pohang-gr-1" not in plan["registered_lot_ids"]
    assert all(a["shipper_id"] != "sh-pohang" for a in plan["plan"]["assignments"])
    assert state["round"] == 2
    assert "sh-pohang" not in {c["shipper_id"] for c in state["confirmations"]}
    assert all(c["status"] == "pending" for c in state["confirmations"])
    assert state["needs_replan"] is False

    pohang = saas_login("shipper-pohang")
    lots = pohang.get("/api/saas/shipper/lots").json()
    assert [lot["status"] for lot in lots] == ["registered"]
    assert pohang.get(f"/api/saas/shipper/plans/{plan_id}/confirmation").status_code == 404
    assert pohang.get("/api/saas/shipper/plan-confirmations").json() == []
    jinju = saas_login("shipper-jinju")
    assert _kinds(jinju, plan_id).count("plan_confirmation_requested") == 2

    nothing = forwarder_client.post(f"{FORWARDER}/{plan_id}/confirmation/recompute", json={})
    assert nothing.status_code == 422
    for c in state["confirmations"]:
        assert _decide(saas_login, plan_id, c["shipper_id"], {"accept": True}).status_code == 200
    assert _state(forwarder_client, plan_id)["status"] == "committed"
    late = forwarder_client.post(
        f"{FORWARDER}/{plan_id}/confirmation/recompute", json={"shipper_ids": ["sh-jinju"]}
    )
    assert late.status_code == 409


def test_auto_accept_switch_is_retired(forwarder_client: TestClient, saas_login) -> None:
    refused = forwarder_client.post(
        FORWARDER, json={"lot_ids": _register(saas_login), "auto_accept": True}
    )
    assert refused.status_code == 422

    pending = forwarder_client.post(FORWARDER, json={"lot_ids": [], "include_registered": True})
    assert pending.status_code == 201, pending.text
    plan_id = pending.json()["plan_id"]
    assert pending.json()["status"] == "pending_shipper_confirmation"
    switched = forwarder_client.post(f"{FORWARDER}/{plan_id}/confirmation/auto-accept")
    assert switched.status_code == 404
    state = _state(forwarder_client, plan_id)
    assert state["status"] == "pending_shipper_confirmation"
    assert state["confirmations"]
    assert all(c["status"] == "pending" and not c["auto_accepted"] for c in state["confirmations"])


def test_auto_accept_is_sandbox_only(forwarder_client: TestClient, saas_login) -> None:
    plan_id = _plan(forwarder_client, saas_login)["plan_id"]
    with database.begin() as db:
        db.execute(
            update(OrganizationRow)
            .where(OrganizationRow.id == FORWARDER_ORG_ID)
            .values(sandbox=False)
        )
    switched = forwarder_client.post(f"{FORWARDER}/{plan_id}/confirmation/auto-accept")
    assert switched.status_code == 404
    assert _state(forwarder_client, plan_id)["status"] == "pending_shipper_confirmation"

    create_real_platform()
    real = password_client(saas_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    created = real.post(PLATFORM, json={"lot_ids": ["x"], "auto_accept": True})
    assert created.status_code == 403
    missing = real.post(f"{PLATFORM}/nope/confirmation/auto-accept")
    assert missing.status_code == 404


def test_order_commit_waits_for_shippers_to_accept_the_plan(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)["plan_id"]
    open_quantity_window()
    queue_quantity(
        saas_login("shipper-sancheong"), plan_id, REDUCE["lot_id"], REDUCE["new_pallets"]
    )
    assert (
        forwarder_client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE}).status_code
        == 200
    )
    applied = forwarder_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"}
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "pending_confirmation"
    order_id = applied.json()["active_order_id"]
    order = forwarder_client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    for c in order["shipper_confirmations"]:
        answered = saas_login(shipper_org_id(c["shipper_id"])).post(
            f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": True, "shipper_id": c["shipper_id"]},
        )
        assert answered.status_code == 200, answered.text
    confirm = f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm"
    early = forwarder_client.post(confirm, json={"accept": True})
    assert early.status_code == 409, early.text
    assert "모든 화주가 배정을 수락" in early.json()["detail"]
    assert forwarder_client.get(f"/api/plans/{plan_id}").json()["status"] == (
        "pending_confirmation"
    )
    waiting = _state(forwarder_client, plan_id)["confirmations"]
    assert all(c["status"] == "pending" for c in waiting)

    accept_plan(saas_login, plan_id)
    done = forwarder_client.post(confirm, json={"accept": True})
    assert done.status_code == 200, done.text
    session = forwarder_client.get(f"/api/plans/{plan_id}").json()
    assert session["status"] == "committed"
    assert session["committed_version"] == session["version"]

    request = next(
        r
        for r in forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()[0][
            "requests"
        ]
        if r["status"] == "pending" and r["counterparty_role"] in ("carrier", "terminal")
    )
    declined = forwarder_client.post(
        f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/requests/{request['request_id']}",
        json={"accepted": False, "note": "선복 부족"},
    )
    assert declined.status_code == 200, declined.text
    state = _state(forwarder_client, plan_id)
    assert state["needs_replan"] is True
    [candidate] = state["replan_candidates"]
    assert candidate["source"] == "request_declined"
    assert candidate["request_id"] == request["request_id"]
    assert candidate["order_id"] == order_id
    assert candidate["voyage_id"] == request["voyage_id"]
    assert candidate["lot_ids"] == request["lot_ids"]
    assert candidate["note"] == "선복 부족"
    if request["kind"] != "booking_cancel":
        assert candidate["events"] == [
            {"type": "air_switch", "lot_id": lot_id} for lot_id in request["lot_ids"]
        ]
    attention = forwarder_client.get(ATTENTION).json()
    assert [s["plan_id"] for s in attention] == [plan_id]


def test_demo_plan_is_committed_without_confirmations(client: TestClient) -> None:
    created = client.post("/api/plans", json={"scenario_id": "default"})
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["status"] == "committed"
    assert "needs_replan" not in plan
    assert "replan_candidates" not in plan
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(PlanConfirmationRow)) == 0
        row = db.get(PlanRow, plan["plan_id"])
        assert row.confirmation_round == 0
        assert row.needs_replan is False
    event = client.post(
        f"/api/plans/{plan['plan_id']}/events",
        json={"event": {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}},
    )
    assert event.status_code == 200, event.text
    applied = client.post(
        f"/api/plans/{plan['plan_id']}/apply", json={"alternative_id": "pull_forward"}
    )
    assert applied.json()["status"] == "pending_confirmation"


def test_forwarder_api_plans_create_asks_shippers(
    forwarder_client: TestClient, platform_client: TestClient, saas_login
) -> None:
    lot_ids = _register(saas_login)
    refused = platform_client.post("/api/plans", json={"lot_ids": lot_ids})
    assert refused.status_code == 403
    created = forwarder_client.post("/api/plans", json={"lot_ids": lot_ids})
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["status"] == "pending_shipper_confirmation"
    shipper = saas_login(shipper_org_id("sh-miryang"))
    assert _kinds(shipper, plan["plan_id"]) == ["plan_confirmation_requested"]


def test_auto_accept_create_is_retired(forwarder_client: TestClient, saas_login) -> None:
    lot_ids = _register(saas_login)
    refused = forwarder_client.post(FORWARDER, json={"lot_ids": lot_ids, "auto_accept": True})
    assert refused.status_code == 422
    created = forwarder_client.post(FORWARDER, json={"lot_ids": lot_ids})
    assert created.status_code == 201, created.text
    plan = created.json()
    shipper = saas_login(shipper_org_id("sh-miryang"))
    assert _kinds(shipper, plan["plan_id"]) == ["plan_confirmation_requested"]


def _placed(lot_id: str, container_id: str, voyage_id: str) -> Assignment:
    return Assignment(
        lot_id=lot_id,
        shipper_id="sh-a",
        product_id="strawberry",
        pallets=1,
        mode="sea",
        container_id=container_id,
        voyage_id=voyage_id,
    )


def test_unrelated_apply_keeps_declined_request_candidate() -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    declined = ReplanCandidate(
        id="c1",
        source="request_declined",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        lot_ids=["lot-x"],
        voyage_id="v1",
        container_id="box-1",
    )
    same = Plan.model_construct(assignments=[_placed("lot-x", "box-1", "v1")])
    moved = Plan.model_construct(assignments=[_placed("lot-x", "box-2", "v2")])
    assert _still_relevant([declined], scenario, same) == [declined]
    assert _still_relevant([declined], scenario, moved) == []
