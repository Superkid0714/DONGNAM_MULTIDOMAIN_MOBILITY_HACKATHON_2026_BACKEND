import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FROZEN_NOW
from fastapi import HTTPException
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    accept_plan,
    open_quantity_window,
)
from sqlalchemy import func, select, text
from test_deep_review_views import _queue_event
from test_saas_api import _lot_body, _register_all, _scenario

from freshswitch_backend.api import plans as plans_api
from freshswitch_backend.api.plans import CreatePlanRequest, tenant_plan_scenario
from freshswitch_backend.explain import render_template
from freshswitch_backend.lots import ANY_ORG, LotUnavailable, claim_lots, lot_store
from freshswitch_backend.lots import store as lot_store_module
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import LotRow, PlanRow
from freshswitch_backend.planning.quantity_policy import load_ledger, policy_rows
from freshswitch_backend.planning.store import _mark_booked, store
from freshswitch_backend.replan import DelayEvent, replan
from freshswitch_backend.replan.engine import PREFERENCE
from freshswitch_backend.replan.explanation import build_explain_context

WAITING_ON_LOCK = text(
    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
    "AND datname = current_database()"
)
DELAY_TIE = {"type": "delay", "voyage_id": "V-2611A", "new_etd": "2026-11-20T18:00:00+09:00"}
DELAY_A = {"type": "delay", "voyage_id": "V-2611A", "new_etd": "2026-11-22T18:00:00+09:00"}


def _delay_a(plan: dict) -> dict:
    voyages = {a["voyage_id"] for a in plan["plan"]["assignments"] if a["mode"] == "sea"}
    first = next(v["id"] for v in plan["scenario"]["voyages"] if v["id"] in voyages)
    return {**DELAY_A, "voyage_id": first}


def _sea(plan: dict) -> set[str]:
    return {a["container_id"] for a in plan["assignments"] if a["mode"] == "sea"}


def _register_more(saas_login, marker: str) -> list[str]:
    ids = []
    for lot in [lot for lot in _scenario().lots if marker in lot.id]:
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post("/api/saas/shipper/lots", json=_lot_body(lot, lot_id=None))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    return ids


def _create(forwarder_client: TestClient, lot_ids: list[str]):
    return forwarder_client.post("/api/saas/forwarder/plans", json={"lot_ids": lot_ids})


def _plan_count() -> int:
    with database.begin() as db:
        return db.scalar(select(func.count()).select_from(PlanRow)) or 0


def _apply(forwarder_client: TestClient, plan_id: str, event: dict) -> dict:
    _queue_event(plan_id, event)
    response = forwarder_client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert response.status_code == 200, response.text
    alternative = next(a["id"] for a in response.json()["alternatives"] if a["feasible"])
    applied = forwarder_client.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": alternative, "replan_id": response.json()["replan_id"]},
    )
    assert applied.status_code == 200, applied.text
    return applied.json()


def test_parallel_plan_creates_only_one_succeeds(saas_login):
    ids = _register_all(saas_login)
    clients = [saas_login(FORWARDER_ORG_ID) for _ in range(2)]
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def go(client: TestClient) -> None:
        barrier.wait()
        codes.append(_create(client, ids).status_code)

    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(codes) == [201, 409]
    assert _plan_count() == 1
    assert {lot.status for lot in lot_store.get_many(ids, org_id=ANY_ORG)} == {"planned"}


def test_create_rechecks_lot_status_inside_transaction(forwarder_client, saas_login):
    ids = _register_all(saas_login)
    scenario, registered = tenant_plan_scenario(CreatePlanRequest(lot_ids=ids), FORWARDER_ORG_ID)
    assert _create(forwarder_client, ids).status_code == 201
    with pytest.raises(LotUnavailable) as caught:
        store.create(
            scenario, registered, org_id=FORWARDER_ORG_ID, lot_org_id=ANY_ORG, exclusive=True
        )
    assert set(caught.value.lot_ids) == set(ids)
    assert _plan_count() == 1


def test_create_rejects_lot_changed_after_solving(saas_login, shipper_client):
    ids = _register_all(saas_login)
    scenario, registered = tenant_plan_scenario(CreatePlanRequest(lot_ids=ids), FORWARDER_ORG_ID)
    patched = shipper_client.patch("/api/saas/shipper/lots/lot-jinju-sb-1", json={"pallets": 3})
    assert patched.status_code == 200, patched.text
    with pytest.raises(LotUnavailable) as caught:
        store.create(
            scenario, registered, org_id=FORWARDER_ORG_ID, lot_org_id=ANY_ORG, exclusive=True
        )
    assert caught.value.lot_ids == ["lot-jinju-sb-1"]
    assert _plan_count() == 0
    assert {lot.status for lot in lot_store.get_many(ids, org_id=ANY_ORG)} == {"registered"}


def _cancel_and_delete(saas_login, victim: str, chosen: list[str]) -> str:
    forwarder = saas_login(FORWARDER_ORG_ID)
    created = _create(forwarder, chosen)
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    accept_plan(saas_login, plan_id)
    order_id = _apply(forwarder, plan_id, {"type": "cancel", "lot_id": victim})["active_order_id"]
    order = forwarder.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    for item in order["shipper_confirmations"]:
        client = saas_login(shipper_org_id(item["shipper_id"]))
        confirmed = client.post(
            f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm", json={"accept": True}
        )
        assert confirmed.status_code == 200, confirmed.text
    confirmed = forwarder.post(
        f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm", json={"accept": True}
    )
    assert confirmed.status_code == 200, confirmed.text
    owner = saas_login("shipper-sancheong")
    assert owner.get(f"/api/lots/{victim}").json()["status"] == "cancelled"
    jinju = saas_login("shipper-jinju")
    assert jinju.delete(f"/api/lots/{victim}").status_code == 404
    jinju_lot = next(i for i in chosen if "jinju" in i)
    assert jinju.patch(f"/api/lots/{jinju_lot}", json={"pallets": 1}).status_code == 409
    refilling = owner.delete(f"/api/lots/{victim}")
    assert refilling.status_code == 409
    assert "빈자리를 충원" in refilling.json()["detail"]
    with database.begin() as db:
        [ledger] = [
            ledger
            for ledger in map(load_ledger, policy_rows(db, plan_id))
            if victim in ledger.reservations
        ]
    assert ledger.reservations[victim].current_quantity == 0
    open_quantity_window(ledger.policy.cutoff)
    assert owner.delete(f"/api/lots/{victim}").status_code == 204
    with database.begin() as db:
        assert db.get(LotRow, victim) is None
        [kept] = [
            row.reservations[victim]
            for row in map(load_ledger, policy_rows(db, plan_id))
            if victim in row.reservations
        ]
    assert kept == ledger.reservations[victim]
    return plan_id


def test_reused_lot_id_does_not_grant_plan_access(saas_login):
    ids = _register_all(saas_login)
    chosen = [i for i in ids if "jinju" in i or "sancheong" in i]
    victim = next(i for i in chosen if "sancheong" in i)
    plan_id = _cancel_and_delete(saas_login, victim, chosen)

    miryang = saas_login("shipper-miryang")
    lot = next(lot for lot in _scenario().lots if "miryang" in lot.id)
    reused = miryang.post("/api/saas/shipper/lots", json=_lot_body(lot, lot_id=victim))
    assert reused.status_code in (409, 422), reused.text
    assert victim in str(reused.json()["detail"])

    forced = lot.model_copy(update={"id": victim})
    lot_store.create(lambda _: (forced, "default", None), org_id="shipper-miryang")
    assert plan_id not in {p["plan_id"] for p in miryang.get("/api/saas/shipper/plans").json()}
    assert miryang.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 404
    assert miryang.get(f"/api/coldchain/containers?plan_id={plan_id}").status_code == 404

    original = saas_login("shipper-sancheong")
    assert original.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 200


def test_owner_snapshot_stays_out_of_responses(
    platform_client, forwarder_client, saas_login, shipper_client
):
    plan = _create(forwarder_client, _register_all(saas_login)).json()
    detail = platform_client.get(f"/api/plans/{plan['plan_id']}").json()
    view = shipper_client.get(f"/api/saas/shipper/plans/{plan['plan_id']}").json()
    for body in (plan, detail, view):
        assert "lot_owner_orgs" not in body
        assert "shipper_org_ids" not in body
    with database.begin() as db:
        stored = db.scalar(select(PlanRow.payload).where(PlanRow.plan_id == plan["plan_id"]))
    assert "shipper-jinju" in stored["shipper_org_ids"]


def test_second_plan_uses_only_free_containers(forwarder_client, saas_login):
    first = _create(forwarder_client, _register_all(saas_login))
    assert first.status_code == 201, first.text
    booked = _sea(first.json()["plan"])
    assert len(booked) == 2

    second = _create(forwarder_client, _register_more(saas_login, "-gr-"))
    assert second.status_code == 201, second.text
    spare = _sea(second.json()["plan"])
    assert len(spare) == 1 and not spare & booked
    assert [c["id"] for c in second.json()["scenario"]["containers"]] == sorted(spare)

    third = _create(forwarder_client, _register_more(saas_login, "-gr-"))
    assert third.status_code == 422
    assert third.json()["detail"].startswith("가용 컨테이너가 없습니다")


def test_replan_marks_alternatives_on_booked_containers(forwarder_client, saas_login):
    first = _create(forwarder_client, _register_all(saas_login)).json()
    second = _create(forwarder_client, _register_more(saas_login, "-gr-"))
    assert second.status_code == 201, second.text
    taken = _sea(second.json()["plan"])
    response = forwarder_client.post(
        f"/api/plans/{first['plan_id']}/events", json={"event": _delay_a(first)}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert not [a for a in body["alternatives"] if taken & _sea(a["plan"])]
    assert body["recommended_id"] == "pull_forward"
    assert body["diff"] is not None
    assert "실행 가능한 대안이 없습니다" not in body["explanation"]["operator_summary"]


def test_mark_booked_moves_recommendation_diff_and_explanation(client):
    plan_id = client.post("/api/plans", json={}).json()["plan_id"]
    session = store.get(plan_id)
    result = replan(session.scenario, session.plan, DelayEvent.model_validate(DELAY_TIE))
    result.explanation = render_template(
        build_explain_context(session.scenario, session.plan, result)
    )
    before = next(a for a in result.alternatives if a.id == result.recommended_id)
    assert before.id == "pull_forward"
    for assignment in before.plan.assignments:
        if assignment.mode == "sea":
            assignment.container_id = "CA-99"
    _mark_booked(session, result, {"CA-99"})
    after = next(a for a in result.alternatives if a.id == result.recommended_id)
    assert not before.feasible
    assert after.id == "sail_as_is"
    assert result.diff is after.diff
    assert f"추천안은 '{after.label}'" in result.explanation.operator_summary
    assert f"'{before.label}' 불가" in result.explanation.operator_summary
    ranked = sorted(
        (a for a in result.alternatives if a.feasible),
        key=lambda a: (a.total_cost, PREFERENCE[a.id]),
    )
    assert after.id == ranked[0].id


def test_apply_rejects_alternative_booked_meanwhile(platform_client, forwarder_client, saas_login):
    first = _create(forwarder_client, _register_all(saas_login)).json()
    response = forwarder_client.post(
        f"/api/plans/{first['plan_id']}/events", json={"event": _delay_a(first)}
    )
    assert response.status_code == 200, response.text
    booked = _sea(first["plan"])
    chosen = next(
        a for a in response.json()["alternatives"] if a["feasible"] and _sea(a["plan"]) - booked
    )
    extra = _sea(chosen["plan"]) - booked
    second = _create(forwarder_client, _register_more(saas_login, "-gr-"))
    assert second.status_code == 201, second.text
    assert _sea(second.json()["plan"]) == extra
    applied = forwarder_client.post(
        f"/api/plans/{first['plan_id']}/apply",
        json={"alternative_id": chosen["id"], "replan_id": response.json()["replan_id"]},
    )
    assert applied.status_code == 409
    assert all(container in applied.json()["detail"] for container in extra)
    assert platform_client.get(f"/api/plans/{first['plan_id']}").json()["version"] == 1


def test_demo_plans_still_share_containers(client):
    first = client.post("/api/plans", json={})
    second = client.post("/api/plans", json={})
    assert first.status_code == second.status_code == 201
    assert _sea(first.json()["plan"]) == _sea(second.json()["plan"]) == {"CA-01", "CA-02"}


def _strip_owner_snapshot(plan_id: str) -> None:
    with database.begin() as db:
        row = db.get(PlanRow, plan_id)
        payload = dict(row.payload)
        payload.pop("lot_owner_orgs")
        payload.pop("shipper_org_ids")
        row.payload = payload


def _resave(plan_id: str) -> dict:
    with database.begin() as db:
        state = store._load(db, plan_id, ANY_ORG)
        store._save(db, state)
    with database.begin() as db:
        return db.scalar(select(PlanRow.payload).where(PlanRow.plan_id == plan_id))


def test_legacy_plan_without_owner_snapshot_keeps_lot_id_guard(saas_login, monkeypatch):
    ids = _register_all(saas_login)
    chosen = [i for i in ids if "jinju" in i or "sancheong" in i]
    victim = next(i for i in chosen if "sancheong" in i)
    plan_id = _cancel_and_delete(saas_login, victim, chosen)
    _strip_owner_snapshot(plan_id)
    jinju = saas_login("shipper-jinju")
    assert jinju.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 200

    lot = next(lot for lot in _scenario().lots if "miryang" in lot.id)
    forced = lot.model_copy(update={"id": victim})
    monkeypatch.setattr(lot_store, "clock", lambda: FROZEN_NOW + timedelta(days=1))
    lot_store.create(lambda _: (forced, "default", None), org_id="shipper-miryang")
    miryang = saas_login("shipper-miryang")

    def hidden() -> None:
        assert plan_id not in {p["plan_id"] for p in miryang.get("/api/saas/shipper/plans").json()}
        assert miryang.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 404
        assert miryang.get(f"/api/coldchain/containers?plan_id={plan_id}").status_code == 404
        assert jinju.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 200

    hidden()
    stored = _resave(plan_id)
    assert stored["lot_owner_orgs"][victim] is None
    assert "shipper-miryang" not in stored["shipper_org_ids"]
    assert "shipper-jinju" in stored["shipper_org_ids"]
    hidden()
    stored = _resave(plan_id)
    assert stored["lot_owner_orgs"][victim] is None
    hidden()


def test_claim_waits_for_row_lock_held_by_other_connection(saas_login):
    ids = _register_all(saas_login)
    scenario, registered = tenant_plan_scenario(CreatePlanRequest(lot_ids=ids), FORWARDER_ORG_ID)
    errors: list[LotUnavailable] = []

    def create() -> None:
        try:
            store.create(
                scenario, registered, org_id=FORWARDER_ORG_ID, lot_org_id=ANY_ORG, exclusive=True
            )
        except LotUnavailable as exc:
            errors.append(exc)

    worker = threading.Thread(target=create)
    with database.begin() as other:
        held = other.scalars(select(LotRow).where(LotRow.id == ids[0]).with_for_update()).one()
        worker.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with database.engine.connect() as probe:
                waiting = probe.scalar(WAITING_ON_LOCK)
            if waiting:
                break
            time.sleep(0.05)
        assert waiting, "다른 연결의 행 잠금을 기다리지 않았다"
        held.status = "planned"
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert ids[0] in errors[0].lot_ids
    assert _plan_count() == 0


def test_claim_rowcount_mismatch_raises(saas_login, monkeypatch):
    ids = _register_all(saas_login)
    lots = lot_store.get_many(ids, org_id=ANY_ORG)
    monkeypatch.setattr(lot_store_module, "mark_planned", lambda *args: len(ids) - 1)
    with pytest.raises(LotUnavailable), database.begin() as db:
        claim_lots(db, [lot.to_lot() for lot in lots], datetime.now(UTC))


def test_create_reports_container_race_for_creation(forwarder_client, saas_login, monkeypatch):
    ids = _register_all(saas_login)
    grapes = _register_more(saas_login, "-gr-")
    solved = tenant_plan_scenario(CreatePlanRequest(lot_ids=grapes), FORWARDER_ORG_ID)
    assert _create(forwarder_client, ids).status_code == 201
    monkeypatch.setattr(plans_api, "tenant_plan_scenario", lambda body, org_id: solved)
    with pytest.raises(HTTPException) as caught:
        plans_api.create_tenant_plan(CreatePlanRequest(lot_ids=grapes), FORWARDER_ORG_ID)
    assert caught.value.status_code == 409
    assert "다시 만드세요" in caught.value.detail
    assert "이벤트" not in caught.value.detail
    assert _plan_count() == 1


def test_foreign_lot_lookup_matches_missing_lot(shipper_client):
    foreign = next(lot for lot in _scenario().lots if "sancheong" in lot.id)
    lot_store.create(lambda _: (foreign, "default", None), org_id="shipper-sancheong")
    requests = [
        ("get", "/api/lots/{}", {}),
        ("patch", "/api/lots/{}", {"json": {"pallets": 2}}),
        ("delete", "/api/lots/{}", {}),
        ("patch", "/api/saas/shipper/lots/{}", {"json": {"pallets": 2}}),
        ("delete", "/api/saas/shipper/lots/{}", {}),
    ]
    for method, path, extra in requests:
        hidden = getattr(shipper_client, method)(path.format(foreign.id), **extra)
        missing = getattr(shipper_client, method)(path.format("lot-none-1"), **extra)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()
    assert lot_store.get(foreign.id, org_id="shipper-sancheong").pallets == foreign.pallets


def test_scoped_reservation_hides_other_org_lot_ids(saas_login):
    _register_all(saas_login)
    with database.begin() as db:
        everything = set(db.scalars(select(LotRow.id)))
    own = lot_store.reservation("shipper-jinju").taken
    assert own == {"lot-jinju-sb-1"}
    assert lot_store.reservation(ANY_ORG).taken >= everything
    world = lot_store.reservation(ANY_ORG, peer_of=PLATFORM_ORG_ID).taken
    assert world == everything
