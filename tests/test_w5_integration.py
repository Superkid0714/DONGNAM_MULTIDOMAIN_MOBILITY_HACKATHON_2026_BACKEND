import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, PLATFORM_ORG_ID
from test_add_lot import _apply_add, _plan_with_spare, _reject_order, _status
from test_saas_api import _register_all
from test_saas_e2e_round1 import _confirm_all

from freshswitch_backend.api.saas_views import (
    forwarder_order_view,
    replan_pending,
    shipper_genset_instructions,
    shipper_lot_ids,
    shipper_order_view,
)
from freshswitch_backend.coldchain.power_plan import power_plan
from freshswitch_backend.execution.models import ReplanCandidate
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import PlanForwarderRow, PlanRow
from freshswitch_backend.planning.store import (
    PlanConfirmation,
    PlanConfirmationState,
    PlanSession,
    _unregister,
    store,
)
from freshswitch_backend.quality import quality_history

PLATFORM = "/api/saas/platform/plans"
LATE = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
GRAPE_STEPS = (
    {"type": "reduce", "lot_id": "lot-sangju-gr-1", "new_pallets": 10},
    {"type": "reduce", "lot_id": "lot-pohang-gr-1", "new_pallets": 3},
    {"type": "reduce", "lot_id": "lot-sangju-gr-1", "new_pallets": 8},
)
REDUCE = {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}


def _saas_plan(platform: TestClient, saas_login, **options) -> str:
    body = {"lot_ids": _register_all(saas_login), **options}
    created = platform.post(PLATFORM, json=body)
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    with database.begin() as db:
        db.add(PlanForwarderRow(plan_id=plan_id, forwarder_org_id=FORWARDER_ORG_ID))
    return plan_id


def _apply(client: TestClient, plan_id: str, event: dict, alternative_id: str) -> dict:
    result = client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert result.status_code == 200, result.text
    body = result.json()
    applied = client.post(
        f"/api/plans/{plan_id}/apply",
        json={"replan_id": body["replan_id"], "alternative_id": alternative_id},
    )
    assert applied.status_code == 200, applied.text
    return next(a for a in body["alternatives"] if a["id"] == alternative_id)


def _apply_best(client: TestClient, plan_id: str, event: dict) -> None:
    result = client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert result.status_code == 200, result.text
    body = result.json()
    chosen = body["recommended_id"] or next(a["id"] for a in body["alternatives"] if a["feasible"])
    applied = client.post(
        f"/api/plans/{plan_id}/apply",
        json={"replan_id": body["replan_id"], "alternative_id": chosen},
    )
    assert applied.status_code == 200, applied.text


def _last_order(client: TestClient, path: str) -> dict:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()[-1]


def _settlement_text(order: dict) -> str:
    return next(a["description"] for a in order["automatic"] if a["kind"] == "settlement_recompute")


def _expected_text(charges: list[dict], shipper_id: str) -> str:
    paid = sum(c["fee"] for c in charges if c["payer_shipper_id"] == shipper_id)
    received = [
        t["amount"] for c in charges for t in c["transfers"] if t["shipper_id"] == shipper_id
    ]
    parts = ["정산 재계산"]
    if paid:
        parts.append(f"취소 부담금 {paid:,}원 부담")
    if received:
        parts.append(f"취소 부담금 {sum(received):,}원 보전")
    return ". ".join(parts)


def _chained_charges(platform: TestClient, plan_id: str) -> list[dict]:
    charges = []
    for step in GRAPE_STEPS:
        chosen = _apply(platform, plan_id, step, "sail_as_is")
        assert chosen["diff"]["cancellation"]["fee"] > 0
        charges.append(chosen["diff"]["cancellation"])
    return charges


def test_order_keeps_every_cumulative_charge(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _saas_plan(platform_client, saas_login, auto_accept=True)
    charges = _chained_charges(platform_client, plan_id)
    order = _last_order(platform_client, f"/api/plans/{plan_id}/orders")
    assert order["cancellation_charges"] == charges
    assert order["cancellation"] == charges[-1]

    for shipper_id in ("sh-sangju", "sh-pohang"):
        shipper = saas_login(shipper_org_id(shipper_id))
        mine = _last_order(shipper, f"/api/saas/shipper/plans/{plan_id}/orders")
        assert _settlement_text(mine) == _expected_text(charges, shipper_id)
        listed = mine["cancellation_charges"]
        assert listed
        for charge in listed:
            payer = charge["payer_shipper_id"] == shipper_id
            assert payer or charge["transfers"]
            if payer:
                assert charge["fee"] is not None
            else:
                assert charge["fee"] is None
                assert charge["consolidator_amount"] is None
            assert all(t["shipper_id"] == shipper_id for t in charge["transfers"])
        other = "sh-pohang" if shipper_id == "sh-sangju" else "sh-sangju"
        dumped = json.dumps(listed, ensure_ascii=False)
        assert other not in dumped
        assert f"lot-{other.removeprefix('sh-')}-gr-1" not in dumped

    seen = _last_order(forwarder_client, f"/api/saas/forwarder/plans/{plan_id}/orders")
    assert len(seen["cancellation_charges"]) == len(charges)
    for charge in seen["cancellation_charges"]:
        for name in ("container_cost", "fee", "consolidator_amount", "share_per_pallet_before"):
            assert charge[name] is None
        assert all(t["amount"] is None and t["share_increase"] is None for t in charge["transfers"])
    assert _settlement_text(seen) == "정산 재계산"


def test_single_charge_order_lists_it(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    chosen = _apply(client, plan_id, GRAPE_STEPS[0], "sail_as_is")
    order = _last_order(client, f"/api/plans/{plan_id}/orders")
    assert order["cancellation_charges"] == [chosen["diff"]["cancellation"]]


def test_legacy_order_lists_its_single_charge(platform_client: TestClient, saas_login) -> None:
    plan_id = _saas_plan(platform_client, saas_login, auto_accept=True)
    chosen = _apply(platform_client, plan_id, GRAPE_STEPS[0], "sail_as_is")
    charge = chosen["diff"]["cancellation"]
    session = store.get(plan_id, org_id=PLATFORM_ORG_ID)
    assert session is not None
    order = store.orders(plan_id, org_id=PLATFORM_ORG_ID)[-1]
    legacy = order.model_copy(update={"cancellation_charges": []})

    payer = shipper_order_view(session, legacy, {charge["payer_shipper_id"]})
    assert [c["fee"] for c in payer["cancellation_charges"]] == [charge["fee"]]
    assert _settlement_text(payer) == _expected_text([charge], charge["payer_shipper_id"])

    seen = forwarder_order_view(legacy).model_dump(mode="json")
    assert len(seen["cancellation_charges"]) == 1
    assert seen["cancellation_charges"][0]["fee"] is None


def test_pending_arrival_of_changed_lot_is_conflict(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _apply(client, plan_id, REDUCE, "sail_as_is")
    response = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": "lot-sancheong-sb-1", "inspected_loss_pct": 20},
    )
    assert response.status_code == 409, response.text
    assert "확정 대기" in response.json()["detail"]
    assert [o for o in quality_history.observations() if o.plan_id == plan_id] == []
    unknown = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": "lot-nope", "inspected_loss_pct": 20},
    )
    assert unknown.status_code == 422


def test_platform_metrics_expire_due_orders_first(platform_client: TestClient, saas_login) -> None:
    plan_id = _saas_plan(platform_client, saas_login, auto_accept=True)
    _apply(platform_client, plan_id, REDUCE, "pull_forward")
    with database.begin() as db:
        assert db.get(PlanRow, plan_id).status == "pending_confirmation"
    store.clock = lambda: LATE
    for path in ("/api/saas/platform/metrics", "/api/saas/platform/business"):
        assert platform_client.get(path).status_code == 200
    with database.begin() as db:
        row = db.get(PlanRow, plan_id)
        assert row.status == "committed"
        assert row.version == 3


def _all_peak(session: PlanSession) -> PlanSession:
    routes = []
    for pickup in session.routes:
        trucks = [r.model_copy(update={"gate_status": "peak"}) for r in pickup.pickup.routes]
        routes.append(
            pickup.model_copy(
                update={"pickup": pickup.pickup.model_copy(update={"routes": trucks})}
            )
        )
    return session.model_copy(update={"routes": routes})


def test_shipper_plan_detail_has_own_genset_lines(platform_client: TestClient, saas_login) -> None:
    plan_id = _saas_plan(platform_client, saas_login, auto_accept=True)
    jinju = saas_login("shipper-jinju")
    plain = jinju.get(f"/api/saas/shipper/plans/{plan_id}")
    assert plain.status_code == 200, plain.text
    assert plain.json()["genset_instructions"] == []

    session = store.get(plan_id, org_id=PLATFORM_ORG_ID)
    assert session is not None
    peak = _all_peak(session)
    with database.begin() as db:
        row = db.get(PlanRow, plan_id)
        row.payload = {**row.payload, "routes": peak.model_dump(mode="json")["routes"]}
    own = {"sh-jinju"}
    mine = shipper_lot_ids(peak, own)
    expected = shipper_genset_instructions(peak, own, {})
    assert expected
    assert len(power_plan(peak).candidates) > len(expected)

    detail = jinju.get(f"/api/saas/shipper/plans/{plan_id}").json()
    lines = detail["genset_instructions"]
    assert [line["key"] for line in lines] == [line.key for line in expected]
    others = {lot.id for lot in peak.scenario.lots} - mine
    for line in lines:
        assert set(line["lot_ids"]) <= mine
        assert not any(lot_id in line["text"] for lot_id in others)
        assert line["text"].startswith("[배터리 젯셋 사전 배정]")
    numbers = {n["container_id"]: n["container_no"] for n in detail["container_numbers"]}
    for line in lines:
        for container_id, container_no in zip(
            line["container_ids"], line["container_nos"], strict=True
        ):
            assert numbers[container_id] == container_no


def _confirmation(plan_id: str, shipper_id: str, status: str) -> PlanConfirmation:
    return PlanConfirmation(
        plan_id=plan_id,
        round=1,
        shipper_id=shipper_id,
        shipper_org_id=shipper_org_id(shipper_id),
        lot_ids=[],
        status=status,
        created_at=LATE,
    )


def _candidate(source: str, shipper_id: str | None = None) -> ReplanCandidate:
    return ReplanCandidate(id=source, source=source, created_at=LATE, shipper_id=shipper_id)


def test_replan_pending_ignores_own_rejection_only() -> None:
    def state(confirmations, candidates) -> PlanConfirmationState:
        return PlanConfirmationState(
            plan_id="p",
            status="pending_shipper_confirmation",
            version=1,
            round=1,
            confirmations=confirmations,
            needs_replan=bool(candidates),
            replan_candidates=candidates,
        )

    own = ["sh-jinju"]
    assert replan_pending(None, own) is False
    assert replan_pending(state([_confirmation("p", "sh-jinju", "pending")], []), own) is False
    mine_rejected = state(
        [_confirmation("p", "sh-jinju", "rejected")],
        [_candidate("shipper_rejected", "sh-jinju")],
    )
    assert replan_pending(mine_rejected, own) is False
    other = state(
        [_confirmation("p", "sh-pohang", "rejected")],
        [_candidate("shipper_rejected", "sh-pohang")],
    )
    assert replan_pending(other, own) is True
    third_party = state(
        [_confirmation("p", "sh-jinju", "accepted")], [_candidate("request_declined")]
    )
    assert replan_pending(third_party, own) is True


def test_shipper_sees_replan_pending_without_details(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _saas_plan(platform_client, saas_login)
    jinju = saas_login("shipper-jinju")
    pohang = saas_login("shipper-pohang")

    def views(client: TestClient) -> tuple[dict, dict, dict]:
        confirmation = client.get(f"/api/saas/shipper/plans/{plan_id}/confirmation")
        detail = client.get(f"/api/saas/shipper/plans/{plan_id}")
        listed = client.get("/api/saas/shipper/plans")
        for response in (confirmation, detail, listed):
            assert response.status_code == 200, response.text
        item = next(p for p in listed.json() if p["plan_id"] == plan_id)
        return confirmation.json(), detail.json(), item

    assert all(view["replan_pending"] is False for view in views(jinju))
    reason = "출하일이 안 맞습니다"
    rejected = pohang.post(
        f"/api/saas/shipper/plans/{plan_id}/confirmation",
        json={"accept": False, "reason": reason},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["replan_pending"] is False
    assert all(view["replan_pending"] is False for view in views(pohang))

    confirmation, detail, item = views(jinju)
    assert confirmation["replan_pending"] is True
    assert detail["replan_pending"] is True
    assert item["replan_pending"] is True
    pending = jinju.get("/api/saas/shipper/plan-confirmations", params={"status": "pending"})
    assert pending.status_code == 200, pending.text
    rows = [row for row in pending.json() if row["plan_id"] == plan_id]
    assert rows
    assert all(row["replan_pending"] is True for row in rows)
    for view in (confirmation, item, *rows):
        text = str(view)
        assert "sh-pohang" not in text
        assert reason not in text
        assert "replan_candidates" not in view
    assert "replan_candidates" not in detail
    assert reason not in str(detail)
    accepted = jinju.post(f"/api/saas/shipper/plans/{plan_id}/confirmation", json={"accept": True})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["replan_pending"] is True

    recomputed = platform_client.post(f"{PLATFORM}/{plan_id}/confirmation/recompute", json={})
    assert recomputed.status_code == 200, recomputed.text
    assert all(view["replan_pending"] is False for view in views(jinju))
    listed = jinju.get("/api/saas/shipper/plan-confirmations").json()
    assert all(row["replan_pending"] is False for row in listed if row["plan_id"] == plan_id)


def _registered(plan_id: str) -> tuple[list[str], set[str], set[str]]:
    with database.begin() as db:
        row = db.get(PlanRow, plan_id)
        return (
            list(row.payload["registered_lot_ids"]),
            {lot["id"] for lot in row.payload["scenario"]["lots"]},
            {lot["id"] for lot in row.committed["scenario"]["lots"]},
        )


def test_lot_committed_through_superseded_add_is_not_released_on_rollback(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login)
    _apply_add(platform_client, plan_id, extra)
    _apply_best(platform_client, plan_id, REDUCE)
    orders = platform_client.get(f"/api/plans/{plan_id}/orders").json()
    assert [o["status"] for o in orders] == ["expired", "proposed"]
    _confirm_all(platform_client, saas_login, forwarder_client, plan_id, orders[-1]["order_id"])
    registered, lots, committed = _registered(plan_id)
    assert extra in registered and extra in lots and extra in committed
    assert _status(saas_login, extra) == "planned"

    _apply_best(
        platform_client, plan_id, {"type": "reduce", "lot_id": "lot-pohang-gr-1", "new_pallets": 3}
    )
    latest = platform_client.get(f"/api/plans/{plan_id}").json()["active_order_id"]
    _reject_order(platform_client, saas_login, plan_id, latest)
    session = platform_client.get(f"/api/plans/{plan_id}").json()
    assert session["status"] == "committed"
    registered, lots, committed = _registered(plan_id)
    assert extra in registered and extra in lots and extra in committed
    assert _status(saas_login, extra) == "planned"


def test_rolled_back_add_leaves_plan_and_snapshot(platform_client: TestClient, saas_login) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login)
    applied = _apply_add(platform_client, plan_id, extra)
    _reject_order(platform_client, saas_login, plan_id, applied["active_order_id"])
    registered, lots, committed = _registered(plan_id)
    assert extra not in registered and extra not in lots and extra not in committed
    assert _status(saas_login, extra) == "registered"


def test_unregister_drops_lot_from_committed_snapshot(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    with database.begin() as db:
        state = store._load(db, plan_id)
    assert state is not None
    lot_id = state.committed.scenario.lots[0].id
    state.session.registered_lot_ids = [lot_id, "lot-other"]
    remaining = _unregister(state, {lot_id})
    assert remaining == ["lot-other"]
    assert lot_id not in {lot.id for lot in state.committed.scenario.lots}
    assert state.release == {lot_id}
    assert lot_id in {lot.id for lot in state.session.scenario.lots}
    untouched = state.committed
    assert _unregister(state, {"lot-missing"}) == [lot_id, "lot-other"]
    assert state.committed is untouched
