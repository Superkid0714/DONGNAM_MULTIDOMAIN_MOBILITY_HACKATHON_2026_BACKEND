import pytest
from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, accept_plan, open_quantity_window, queue_quantity
from sqlalchemy import delete, select, update
from test_add_lot import _reject_order
from test_plan_confirm import FORWARDER, PLATFORM, REDUCE, _decide, _lot_body, _plan, _state

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import (
    OrganizationRow,
    PlanConfirmationRow,
    PlanForwarderRow,
    PlanRow,
)
from freshswitch_backend.planning.store import APPLY_BEFORE_CONFIRMATION

SANGJU_LOT = "lot-sangju-gr-1"
SHIPPER_NAMES = (
    "진주 딸기 수출농가 A",
    "산청 딸기 수출농가 B",
    "밀양 딸기 수출농가 C",
    "상주 포도 수출농가 D",
    "포항 포도 수출농가 E",
)


def _go_real(saas_login):
    scenario = get_scenario("default")
    assert scenario is not None
    orgs = {shipper_org_id(s.id) for s in scenario.shippers} | {FORWARDER_ORG_ID}
    clients = {org: saas_login(org) for org in sorted(orgs)}
    with database.begin() as db:
        db.execute(update(OrganizationRow).values(sandbox=False, world_id=None))
    return clients.__getitem__


def _assign(platform_client: TestClient, plan_id: str):
    return platform_client.put(
        f"{PLATFORM}/{plan_id}/forwarder", json={"forwarder_org_id": FORWARDER_ORG_ID}
    )


def _queue_reduce(shipper_login, plan_id: str) -> None:
    open_quantity_window()
    queue_quantity(
        shipper_login("shipper-sancheong"), plan_id, REDUCE["lot_id"], REDUCE["new_pallets"]
    )


def _reduce_and_apply(forwarder_client: TestClient, plan_id: str):
    event = forwarder_client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE})
    assert event.status_code == 200, event.text
    return forwarder_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"}
    )


def _accept_all(saas_login, forwarder_client: TestClient, plan_id: str) -> None:
    for c in _state(forwarder_client, plan_id)["confirmations"]:
        if c["status"] == "pending":
            accepted = _decide(saas_login, plan_id, c["shipper_id"], {"accept": True})
            assert accepted.status_code == 200, accepted.text


def _register_sangju(saas_login) -> str:
    scenario = get_scenario("default")
    assert scenario is not None
    lot = next(lot for lot in scenario.lots if lot.id == SANGJU_LOT)
    response = saas_login(shipper_org_id("sh-sangju")).post(
        "/api/saas/shipper/lots", json=_lot_body(lot)
    )
    assert response.status_code == 201, response.text
    return response.json()["registered"]["id"]


def _add_lot(forwarder_client: TestClient, plan_id: str, lot_id: str) -> str:
    computed = forwarder_client.post(f"{FORWARDER}/{plan_id}/add-lot", json={"lot_id": lot_id})
    assert computed.status_code == 200, computed.text
    return next(a["id"] for a in computed.json()["alternatives"] if a["feasible"])


def _lot_status(saas_login, org_id: str, lot_id: str) -> str:
    lots = saas_login(org_id).get("/api/saas/shipper/lots").json()
    return next(lot["status"] for lot in lots if lot["id"] == lot_id)


def test_real_org_apply_waits_for_shippers_and_platform_cannot_assign(
    forwarder_client: TestClient, platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)["plan_id"]
    real = _go_real(saas_login)
    owner = real(FORWARDER_ORG_ID)
    assert _assign(platform_client, plan_id).status_code == 403

    applied = _reduce_and_apply(owner, plan_id)
    assert applied.status_code == 409, applied.text
    assert applied.json()["detail"] == APPLY_BEFORE_CONFIRMATION
    assert owner.get(f"/api/plans/{plan_id}").json()["status"] == ("pending_shipper_confirmation")
    visible = owner.get(f"/api/saas/forwarder/plans/{plan_id}")
    assert visible.status_code == 200, visible.text

    _accept_all(real, owner, plan_id)
    _queue_reduce(real, plan_id)
    allowed = _reduce_and_apply(owner, plan_id)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "pending_confirmation"
    assert _assign(platform_client, plan_id).status_code == 403


def test_platform_assignment_is_retired_and_creator_stays_assigned(
    forwarder_client: TestClient, platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)["plan_id"]
    _go_real(saas_login)
    with database.begin() as db:
        db.execute(
            update(PlanRow).where(PlanRow.plan_id == plan_id).values(status="pending_confirmation")
        )
    assert _assign(platform_client, plan_id).status_code == 403
    unassigned = platform_client.delete(f"{PLATFORM}/{plan_id}/forwarder")
    assert unassigned.status_code == 403
    with database.begin() as db:
        rows = db.scalars(select(PlanForwarderRow).where(PlanForwarderRow.plan_id == plan_id)).all()
        assert [row.forwarder_org_id for row in rows] == [FORWARDER_ORG_ID]


def test_sandbox_apply_runs_before_shipper_confirmation(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)["plan_id"]
    _queue_reduce(saas_login, plan_id)
    applied = _reduce_and_apply(forwarder_client, plan_id)
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "pending_confirmation"
    waiting = _state(forwarder_client, plan_id)["confirmations"]
    assert waiting and all(c["status"] == "pending" for c in waiting)


def test_accepted_sandbox_plan_runs_replan(
    forwarder_client: TestClient, platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)["plan_id"]
    accept_plan(saas_login, plan_id)
    assert _state(forwarder_client, plan_id)["status"] == "committed"
    assert _assign(platform_client, plan_id).status_code == 403
    _queue_reduce(saas_login, plan_id)
    applied = _reduce_and_apply(forwarder_client, plan_id)
    assert applied.status_code == 200, applied.text


def test_real_forwarder_add_lot_of_new_shipper_asks_them_to_confirm(
    forwarder_client: TestClient, platform_client: TestClient, saas_login
) -> None:
    extra = _register_sangju(saas_login)
    plan_id = _plan(forwarder_client, saas_login, skip=("sh-sangju",))["plan_id"]
    real = _go_real(saas_login)
    owner = real(FORWARDER_ORG_ID)
    _accept_all(real, owner, plan_id)

    alternative = _add_lot(owner, plan_id, extra)
    applied = owner.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative})
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "pending_confirmation"
    assert extra in applied.json()["registered_lot_ids"]
    assert _lot_status(real, "shipper-sangju", extra) == "planned"
    state = _state(owner, plan_id)
    sangju = next(c for c in state["confirmations"] if c["shipper_id"] == "sh-sangju")
    assert sangju["status"] == "pending"
    assert sangju["lot_ids"] == [extra]
    assert _assign(platform_client, plan_id).status_code == 403

    accepted = _decide(real, plan_id, "sh-sangju", {"accept": True})
    assert accepted.status_code == 200, accepted.text
    assert all(c["status"] == "accepted" for c in _state(owner, plan_id)["confirmations"])


def test_shipper_removed_by_recompute_loses_plan_access(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)["plan_id"]
    rejected = _decide(saas_login, plan_id, "sh-pohang", {"accept": False, "reason": "가격"})
    assert rejected.status_code == 200, rejected.text
    recomputed = forwarder_client.post(f"{FORWARDER}/{plan_id}/confirmation/recompute", json={})
    assert recomputed.status_code == 200, recomputed.text
    assert "lot-pohang-gr-1" not in recomputed.json()["plan"]["registered_lot_ids"]

    pohang = saas_login("shipper-pohang")
    listed = pohang.get("/api/saas/shipper/plans").json()
    assert plan_id not in {p["plan_id"] for p in listed}
    assert pohang.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 404
    assert pohang.get(f"/api/saas/shipper/plans/{plan_id}/orders").status_code == 404
    assert pohang.get(f"/api/coldchain/containers?plan_id={plan_id}").status_code == 404
    assert pohang.get("/api/saas/shipper/plan-confirmations").json() == []

    jinju = saas_login("shipper-jinju")
    assert jinju.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 200
    with database.begin() as db:
        row = db.get(PlanRow, plan_id)
        assert shipper_org_id("sh-pohang") not in row.payload["shipper_org_ids"]
        assert "lot-pohang-gr-1" not in row.payload["lot_owner_orgs"]


def _rollback_added_sangju(forwarder_client: TestClient, saas_login, decision: dict | None) -> str:
    extra = _register_sangju(saas_login)
    plan_id = _plan(forwarder_client, saas_login, skip=("sh-sangju",))["plan_id"]
    accept_plan(saas_login, plan_id)
    sangju = saas_login("shipper-sangju")
    assert sangju.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 404
    alternative = _add_lot(forwarder_client, plan_id, extra)
    applied = forwarder_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative}
    )
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["active_order_id"]
    assert sangju.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 200
    invited = sangju.get("/api/saas/shipper/plan-confirmations").json()
    assert [(c["plan_id"], c["status"]) for c in invited] == [(plan_id, "pending")]
    if decision is not None:
        decided = _decide(saas_login, plan_id, "sh-sangju", decision)
        assert decided.status_code == 200, decided.text

    _reject_order(forwarder_client, saas_login, plan_id, order_id)
    plan = forwarder_client.get(f"/api/plans/{plan_id}").json()
    assert extra not in plan["registered_lot_ids"]
    assert extra not in {lot["id"] for lot in plan["scenario"]["lots"]}
    assert _lot_status(saas_login, "shipper-sangju", extra) == "registered"
    return plan_id


@pytest.mark.parametrize(
    "decision", [None, {"accept": True}, {"accept": False, "reason": "가격이 안 맞습니다"}]
)
def test_rolled_back_added_shipper_loses_access_and_invite(
    forwarder_client: TestClient, saas_login, decision: dict | None
) -> None:
    plan_id = _rollback_added_sangju(forwarder_client, saas_login, decision)
    sangju = saas_login("shipper-sangju")
    assert sangju.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 404
    assert plan_id not in {p["plan_id"] for p in sangju.get("/api/saas/shipper/plans").json()}
    assert sangju.get("/api/saas/shipper/plan-confirmations").json() == []
    late = _decide(saas_login, plan_id, "sh-sangju", {"accept": True})
    assert late.status_code == 404
    state = _state(forwarder_client, plan_id)
    assert "sh-sangju" not in {c["shipper_id"] for c in state["confirmations"]}
    assert state["status"] == "committed"
    assert state["needs_replan"] is False
    assert not any(c["shipper_id"] == "sh-sangju" for c in state["replan_candidates"])
    with database.begin() as db:
        left = db.scalars(
            select(PlanConfirmationRow).where(
                PlanConfirmationRow.plan_id == plan_id,
                PlanConfirmationRow.shipper_id == "sh-sangju",
            )
        ).all()
        assert left == []
    attention = forwarder_client.get("/api/saas/forwarder/plan-confirmations").json()
    assert plan_id not in {s["plan_id"] for s in attention}


def _drop_round(plan_id: str) -> None:
    with database.begin() as db:
        db.execute(delete(PlanConfirmationRow).where(PlanConfirmationRow.plan_id == plan_id))
        row = db.get(PlanRow, plan_id)
        row.confirmation_round = 0
        row.status = "committed"
        row.payload = {**row.payload, "status": "committed"}


def _register_extra_jinju(saas_login) -> str:
    scenario = get_scenario("default")
    assert scenario is not None
    lot = next(lot for lot in scenario.lots if lot.id == "lot-jinju-sb-1")
    body = {**_lot_body(lot), "lot_id": "lot-jinju-sb-extra", "pallets": 1}
    response = saas_login(shipper_org_id("sh-jinju")).post("/api/saas/shipper/lots", json=body)
    assert response.status_code == 201, response.text
    return response.json()["registered"]["id"]


def test_plan_without_round_gates_new_shipper_through_a_new_round(
    forwarder_client: TestClient, platform_client: TestClient, saas_login
) -> None:
    extra = _register_sangju(saas_login)
    plan_id = _plan(forwarder_client, saas_login, skip=("sh-sangju",))["plan_id"]
    real = _go_real(saas_login)
    owner = real(FORWARDER_ORG_ID)
    _drop_round(plan_id)

    alternative = _add_lot(owner, plan_id, extra)
    applied = owner.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative})
    assert applied.status_code == 200, applied.text
    state = _state(owner, plan_id)
    assert state["round"] == 1
    statuses = {c["shipper_id"]: c["status"] for c in state["confirmations"]}
    assert statuses.pop("sh-sangju") == "pending"
    assert statuses and set(statuses.values()) == {"accepted"}
    assert _assign(platform_client, plan_id).status_code == 403

    accepted = _decide(real, plan_id, "sh-sangju", {"accept": True})
    assert accepted.status_code == 200, accepted.text
    assert all(c["status"] == "accepted" for c in _state(owner, plan_id)["confirmations"])


def test_plan_without_round_still_takes_lots_of_shippers_already_in_it(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login, skip=("sh-sangju",))["plan_id"]
    extra = _register_extra_jinju(saas_login)
    real = _go_real(saas_login)
    owner = real(FORWARDER_ORG_ID)
    _drop_round(plan_id)

    alternative = _add_lot(owner, plan_id, extra)
    applied = owner.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative})
    assert applied.status_code == 200, applied.text
    state = _state(owner, plan_id)
    assert all(c["status"] == "accepted" for c in state["confirmations"])
    assert "sh-jinju" in {c["shipper_id"] for c in state["confirmations"]}
