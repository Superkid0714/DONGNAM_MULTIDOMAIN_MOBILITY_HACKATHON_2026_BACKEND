import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, accept_plan, open_quantity_window, queue_quantity
from sqlalchemy import delete, select

from freshswitch_backend.api import saas_change_requests
from freshswitch_backend.api.saas_change_requests import ALREADY_AIR, ALREADY_OPEN
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.notifications import MOCK_CHANNEL, notify_plan_confirmation
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import DEMO_ORG, ChangeRequestRow, NotificationRow
from freshswitch_backend.planning.quantity_policy import lot_bound
from freshswitch_backend.planning.store import store

POHANG_LOT = "lot-pohang-gr-1"
SANGJU_LOT = "lot-sangju-gr-1"
MIRYANG_AIR_LOT = "lot-miryang-sb-2"
REDUCE = {"type": "reduce", "lot_id": POHANG_LOT, "new_pallets": 2, "reason": "작황 부진"}
AIR_SWITCH = {"type": "air_switch", "lot_id": POHANG_LOT}
QUOTE_FIRST = "수량 변경 견적을 확인한 뒤 요청해 주세요"
FORWARDER = "/api/saas/forwarder/change-requests"


def _lot_body(lot, suffix: str = "") -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "sell_window": lot.sell_window,
        "lot_id": lot.id + suffix,
    }


def _plan(
    forwarder_client: TestClient, saas_login, skip: tuple[str, ...] = (), suffix: str = ""
) -> str:
    scenario = get_scenario("default")
    assert scenario is not None
    ids = []
    for lot in scenario.lots:
        if lot.id in skip:
            continue
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post("/api/saas/shipper/lots", json=_lot_body(lot, suffix))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    created = forwarder_client.post("/api/saas/forwarder/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    accept_plan(saas_login, plan_id)
    return plan_id


def _request(client: TestClient, plan_id: str, body: dict = REDUCE):
    return client.post(f"/api/saas/shipper/plans/{plan_id}/change-requests", json=body)


def _quantity_request(shipper: TestClient, plan_id: str, lot_id: str, quantity: int) -> str:
    open_quantity_window()
    queued = queue_quantity(shipper, plan_id, lot_id, quantity)
    assert queued["status"] == "queued", queued
    return queued["change_request_id"]


def _order_kinds(items: list[dict]) -> list[str]:
    return [n["kind"] for n in items if n["kind"] != "plan_confirmation_requested"]


def _replan(forwarder_client: TestClient, request_id: str) -> dict:
    computed = forwarder_client.post(f"{FORWARDER}/{request_id}/replan")
    assert computed.status_code == 200, computed.text
    return computed.json()["replan"]


def _apply(forwarder_client: TestClient, request_id: str, body: dict):
    return forwarder_client.post(f"{FORWARDER}/{request_id}/apply", json=body)


def _notifications(client: TestClient) -> list[dict]:
    return client.get("/api/saas/shipper/notifications").json()


def _commit_order(
    reader: TestClient,
    saas_login,
    forwarder_client: TestClient,
    plan_id: str,
    order_id: str,
) -> dict:
    order = reader.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    for confirmation in order["shipper_confirmations"]:
        if confirmation["status"] != "pending":
            continue
        shipper_id = confirmation["shipper_id"]
        answered = saas_login(shipper_org_id(shipper_id)).post(
            f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": True, "shipper_id": shipper_id},
        )
        assert answered.status_code == 200, answered.text
    for _ in range(2):
        done = forwarder_client.post(
            f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": True},
        )
        assert done.status_code == 200, done.text
    return order


def _open_rows(plan_id: str, lot_id: str) -> list[ChangeRequestRow]:
    with database.begin() as db:
        return db.scalars(
            select(ChangeRequestRow).where(
                ChangeRequestRow.plan_id == plan_id,
                ChangeRequestRow.lot_id == lot_id,
                ChangeRequestRow.status == "open",
            )
        ).all()


def test_change_request_flow_replan_apply_and_notify(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    pohang = saas_login("shipper-pohang")
    sangju = saas_login("shipper-sangju")

    request_id = _quantity_request(pohang, plan_id, POHANG_LOT, 2)
    [request] = pohang.get("/api/saas/shipper/change-requests").json()
    assert request["request_id"] == request_id
    assert request["status"] == "open"
    assert request["shipper_id"] == "sh-pohang"
    assert request["type"] == "reduce"
    assert request["new_pallets"] == 2
    legacy = _request(pohang, plan_id)
    assert legacy.status_code == 409
    assert legacy.json()["detail"] == QUOTE_FIRST

    mine = pohang.get("/api/saas/shipper/change-requests").json()
    assert [r["request_id"] for r in mine] == [request_id]
    assert sangju.get("/api/saas/shipper/change-requests").json() == []

    listed = forwarder_client.get("/api/saas/forwarder/change-requests?status=open").json()
    assert [r["request_id"] for r in listed] == [request_id]
    assert listed[0]["event"] == {"type": "reduce", "lot_id": POHANG_LOT, "new_pallets": 2}
    assert listed[0]["shipper_org_id"] == "shipper-pohang"
    assert listed[0]["shipper_org_name"]

    early = forwarder_client.post(
        f"/api/saas/forwarder/change-requests/{request_id}/apply",
        json={"alternative_id": "pull_forward"},
    )
    assert early.status_code == 409

    computed = forwarder_client.post(f"/api/saas/forwarder/change-requests/{request_id}/replan")
    assert computed.status_code == 200, computed.text
    replan = computed.json()["replan"]
    assert {a["id"] for a in replan["alternatives"]} == {
        "pull_forward",
        "next_voyage",
        "sail_as_is",
    }
    assert replan["event"]["lot_id"] == POHANG_LOT
    assert computed.json()["change_request"]["replan_id"] == replan["replan_id"]

    applied = forwarder_client.post(
        f"/api/saas/forwarder/change-requests/{request_id}/apply",
        json={"alternative_id": "pull_forward", "note": "감량 반영"},
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    order_id = body["plan"]["active_order_id"]
    assert order_id
    assert body["plan"]["status"] == "pending_confirmation"
    assert body["change_request"]["status"] == "applied"
    assert body["change_request"]["order_id"] == order_id
    assert body["change_request"]["alternative_id"] == "pull_forward"
    assert body["change_request"]["decided_at"] is not None
    lot = next(x for x in body["plan"]["scenario"]["lots"] if x["id"] == POHANG_LOT)
    assert lot["pallets"] == 2

    again = forwarder_client.post(
        f"/api/saas/forwarder/change-requests/{request_id}/apply",
        json={"alternative_id": "pull_forward"},
    )
    assert again.status_code == 409
    assert pohang.get("/api/saas/shipper/change-requests").json()[0]["status"] == "applied"

    order = forwarder_client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    confirming = {c["shipper_id"] for c in order["shipper_confirmations"]}
    assert "sh-pohang" not in confirming
    assert "sh-pohang" in order["notified_shipper_ids"]
    notified = pohang.get("/api/saas/shipper/notifications").json()
    kinds = {n["kind"] for n in notified}
    assert {"change_request_applied", "order_notified"} <= kinds
    assert "order_confirmation_requested" not in kinds
    for item in notified:
        assert item["channel"] == MOCK_CHANNEL
        assert item["sent"] is False
        assert item["shipper_id"] == "sh-pohang"
        assert plan_id in item["link"]
    applied_note = next(n for n in notified if n["kind"] == "change_request_applied")
    assert applied_note["change_request_id"] == request_id
    assert POHANG_LOT in applied_note["body"]

    for shipper_id in confirming:
        other = saas_login(shipper_org_id(shipper_id))
        items = other.get("/api/saas/shipper/notifications").json()
        assert _order_kinds(items) == ["order_confirmation_requested"]
        assert all(n["shipper_id"] == shipper_id for n in items)
        assert all(POHANG_LOT not in n["body"] for n in items)
    for shipper_id in set(order["notified_shipper_ids"]) - {"sh-pohang"}:
        items = _notifications(saas_login(shipper_org_id(shipper_id)))
        assert _order_kinds(items) == ["order_notified"]
        notified_item = next(n for n in items if n["kind"] == "order_notified")
        assert notified_item["order_id"] == order_id
        assert POHANG_LOT not in notified_item["body"]
    assert {n["kind"] for n in body["notifications"]} == {
        "change_request_applied",
        "order_confirmation_requested",
        *(["order_notified"] if order["notified_shipper_ids"] else []),
    }

    _commit_order(forwarder_client, saas_login, forwarder_client, plan_id, order_id)
    committed = [
        n
        for n in pohang.get("/api/saas/shipper/notifications").json()
        if n["kind"] == "order_committed"
    ]
    assert len(committed) == 1
    assert committed[0]["order_id"] == order_id


def test_shipper_cannot_request_change_on_other_shipper_lot(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    sangju = saas_login("shipper-sangju")

    foreign = _request(sangju, plan_id, AIR_SWITCH)
    assert foreign.status_code == 404
    missing = _request(sangju, plan_id, {"type": "air_switch", "lot_id": "lot-none"})
    assert missing.status_code == 404
    assert foreign.json()["detail"] == missing.json()["detail"]
    with database.begin() as db:
        assert lot_bound(db, plan_id, POHANG_LOT)
        assert not lot_bound(db, plan_id, MIRYANG_AIR_LOT)
    probes = [
        REDUCE,
        {"type": "cancel", "lot_id": POHANG_LOT},
        {"type": "increase", "lot_id": POHANG_LOT, "new_pallets": 9},
        {"type": "reduce", "lot_id": MIRYANG_AIR_LOT, "new_pallets": 1},
        {"type": "cancel", "lot_id": MIRYANG_AIR_LOT},
        {"type": "cancel", "lot_id": "lot-none"},
        {"type": "reduce", "lot_id": "lot-none", "new_pallets": 1},
    ]
    for probe in probes:
        response = _request(sangju, plan_id, probe)
        assert (response.status_code, response.json()) == (
            missing.status_code,
            missing.json(),
        ), probe
    assert sangju.get("/api/saas/shipper/change-requests").json() == []
    assert forwarder_client.get(FORWARDER).json() == []
    open_quantity_window()
    quoted = sangju.post(
        f"/api/saas/shipper/plans/{plan_id}/lots/{POHANG_LOT}/quantity/quote",
        json={"quantity": 2},
    )
    assert quoted.status_code == 404

    pohang = saas_login("shipper-pohang")
    no_pallets = _request(pohang, plan_id, {"type": "reduce", "lot_id": POHANG_LOT})
    assert no_pallets.status_code == 422
    negative = pohang.post(
        f"/api/saas/shipper/plans/{plan_id}/lots/{POHANG_LOT}/quantity/quote",
        json={"quantity": -1},
    )
    assert negative.status_code == 422, negative.text
    assert forwarder_client.get(FORWARDER).json() == []


def test_shipper_outside_plan_gets_404(forwarder_client: TestClient, saas_login) -> None:
    plan_id = _plan(forwarder_client, saas_login, skip=(POHANG_LOT,))
    pohang = saas_login("shipper-pohang")
    assert _request(pohang, plan_id).status_code == 404


def test_reject_records_notification_and_blocks_duplicates(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    sangju = saas_login("shipper-sangju")
    request_id = _quantity_request(sangju, plan_id, SANGJU_LOT, 0)
    detail = forwarder_client.get(f"{FORWARDER}/{request_id}").json()
    assert detail["event"] == {"type": "cancel", "lot_id": SANGJU_LOT}
    path = f"/api/saas/shipper/plans/{plan_id}/lots/{SANGJU_LOT}/quantity/quote"
    duplicate = sangju.post(path, json={"quantity": 0})
    assert duplicate.status_code == 409
    assert _request(sangju, plan_id, {"type": "cancel", "lot_id": SANGJU_LOT}).status_code == 409

    rejected = forwarder_client.post(
        f"{FORWARDER}/{request_id}/reject", json={"note": "선적 마감 지남"}
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["decision_note"] == "선적 마감 지남"
    assert forwarder_client.post(f"{FORWARDER}/{request_id}/reject", json={}).status_code == 409
    assert forwarder_client.post(f"{FORWARDER}/{request_id}/replan").status_code == 409
    items = sangju.get("/api/saas/shipper/notifications").json()
    assert _order_kinds(items) == ["change_request_rejected"]
    rejected_note = next(n for n in items if n["kind"] == "change_request_rejected")
    assert "선적 마감 지남" in rejected_note["body"]
    assert rejected_note["channel"] == MOCK_CHANNEL

    assert _quantity_request(sangju, plan_id, SANGJU_LOT, 0) != request_id


def test_other_forwarder_and_roles_cannot_see_requests(
    forwarder_client: TestClient,
    other_forwarder_client: TestClient,
    platform_client: TestClient,
    saas_login,
) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    request_id = _quantity_request(saas_login("shipper-pohang"), plan_id, POHANG_LOT, 2)

    other = other_forwarder_client
    assert other.get(FORWARDER).json() == []
    assert other.get(f"{FORWARDER}/{request_id}").status_code == 404
    assert other.post(f"{FORWARDER}/{request_id}/replan").status_code == 404
    assert (
        other.post(
            f"{FORWARDER}/{request_id}/apply", json={"alternative_id": "pull_forward"}
        ).status_code
        == 404
    )
    assert other.post(f"{FORWARDER}/{request_id}/reject", json={}).status_code == 404
    assert saas_login("shipper-pohang").get(FORWARDER).status_code == 403
    assert forwarder_client.get("/api/saas/shipper/notifications").status_code == 403
    assert platform_client.get(FORWARDER).status_code == 403
    assert platform_client.post(f"{FORWARDER}/{request_id}/replan").status_code == 403
    retired = platform_client.post(
        f"/api/saas/platform/change-requests/{request_id}/reject", json={}
    )
    assert retired.status_code == 403
    assert (
        platform_client.post(f"/api/saas/shipper/plans/{plan_id}/change-requests", json=REDUCE)
    ).status_code == 403
    assert len(forwarder_client.get(FORWARDER).json()) == 1


def test_plan_confirmation_hook_records_per_shipper(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    with database.begin() as db:
        db.execute(delete(NotificationRow).where(NotificationRow.plan_id == plan_id))
    owner = store.org_of(plan_id)
    assert owner == FORWARDER_ORG_ID
    session = store.get(plan_id, org_id=owner)
    created = notify_plan_confirmation(session, owner, store.clock())
    assert {n.shipper_id for n in created} == {a.shipper_id for a in session.plan.assignments}
    assert notify_plan_confirmation(session, owner, store.clock()) == []
    items = saas_login("shipper-jinju").get("/api/saas/shipper/notifications").json()
    assert [n["kind"] for n in items] == ["plan_confirmation_requested"]
    assert items[0]["shipper_id"] == "sh-jinju"
    assert notify_plan_confirmation(session, DEMO_ORG, store.clock()) == []


def test_apply_idempotency_key_replays_and_rollback_shows_order_status(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    pohang = saas_login("shipper-pohang")
    request_id = _quantity_request(pohang, plan_id, POHANG_LOT, 2)
    _replan(forwarder_client, request_id)

    keyed = {"alternative_id": "pull_forward", "idempotency_key": "k1"}
    first = _apply(forwarder_client, request_id, keyed)
    assert first.status_code == 200, first.text
    order_id = first.json()["plan"]["active_order_id"]
    before = len(_notifications(pohang))

    second = _apply(forwarder_client, request_id, keyed)
    assert second.status_code == 200, second.text
    assert second.json()["plan"]["active_order_id"] == order_id
    assert second.json()["plan"]["version"] == first.json()["plan"]["version"]
    assert second.json()["notifications"] == []
    assert second.json()["change_request"]["status"] == "applied"
    assert len(_notifications(pohang)) == before

    mismatch = _apply(
        forwarder_client, request_id, {"alternative_id": "next_voyage", "idempotency_key": "k1"}
    )
    assert mismatch.status_code == 422
    assert (
        _apply(forwarder_client, request_id, {"alternative_id": "pull_forward"}).status_code == 409
    )

    listed = pohang.get("/api/saas/shipper/change-requests").json()[0]
    assert listed["order_id"] == order_id
    assert listed["order_status"] == "proposed"
    order = forwarder_client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    decliner = order["shipper_confirmations"][0]["shipper_id"]
    declined = saas_login(shipper_org_id(decliner)).post(
        f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
        json={"accept": False, "shipper_id": decliner},
    )
    assert declined.status_code == 200, declined.text
    listed = pohang.get("/api/saas/shipper/change-requests").json()[0]
    assert listed["status"] == "applied"
    assert listed["order_status"] == "rejected"
    assert forwarder_client.get(f"{FORWARDER}/{request_id}").json()["order_status"] == "rejected"
    assert _quantity_request(pohang, plan_id, POHANG_LOT, 2) != request_id


def test_concurrent_requests_on_same_lot_leave_one_open(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    pohang = saas_login("shipper-pohang")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_request, pohang, plan_id, AIR_SWITCH) for _ in range(2)]
        responses = [f.result() for f in futures]
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert next(r for r in responses if r.status_code == 409).json()["detail"] == ALREADY_OPEN
    assert len(_open_rows(plan_id, POHANG_LOT)) == 1


def test_apply_blocks_concurrent_reject(
    forwarder_client: TestClient, saas_login, monkeypatch
) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    pohang = saas_login("shipper-pohang")
    request_id = _quantity_request(pohang, plan_id, POHANG_LOT, 2)
    _replan(forwarder_client, request_id)
    rejecter = saas_login(FORWARDER_ORG_ID)
    outcome: dict = {}
    real_apply = saas_change_requests.apply_alternative

    def reject() -> None:
        started = time.monotonic()
        outcome["reject"] = rejecter.post(f"{FORWARDER}/{request_id}/reject", json={"note": "경합"})
        outcome["waited"] = time.monotonic() - started

    def racing_apply(*args, **kwargs):
        outcome["thread"] = threading.Thread(target=reject)
        outcome["thread"].start()
        time.sleep(0.5)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(saas_change_requests, "apply_alternative", racing_apply)
    applied = _apply(forwarder_client, request_id, {"alternative_id": "pull_forward"})
    outcome["thread"].join(timeout=30)
    assert applied.status_code == 200, applied.text
    assert outcome["reject"].status_code == 409
    assert outcome["waited"] >= 0.5
    detail = forwarder_client.get(f"{FORWARDER}/{request_id}").json()
    assert detail["status"] == "applied"
    assert detail["order_id"] == applied.json()["plan"]["active_order_id"]
    kinds = [n["kind"] for n in _notifications(pohang)]
    assert "change_request_applied" in kinds
    assert "change_request_rejected" not in kinds


def test_cancel_air_lot_names_own_lot_and_skips_confirmation(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    miryang = saas_login("shipper-miryang")
    created = _request(miryang, plan_id, {"type": "cancel", "lot_id": MIRYANG_AIR_LOT})
    assert created.status_code == 201, created.text
    assert created.json()["cancellation_estimate"] is None
    request_id = created.json()["request_id"]
    replan = _replan(forwarder_client, request_id)
    applied = _apply(forwarder_client, request_id, {"alternative_id": replan["recommended_id"]})
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["plan"]["active_order_id"]
    order = forwarder_client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    assert "sh-miryang" not in {c["shipper_id"] for c in order["shipper_confirmations"]}
    assert "sh-miryang" in order["notified_shipper_ids"]

    items = _notifications(miryang)
    assert sorted(_order_kinds(items)) == ["change_request_applied", "order_notified"]
    assert all("다른 화주" not in n["body"] for n in items)
    notified = next(n for n in items if n["kind"] == "order_notified")
    assert f"출하 {MIRYANG_AIR_LOT} 취소" in notified["body"]
    assert notified["order_id"] == order_id
    applied_note = next(n for n in items if n["kind"] == "change_request_applied")
    assert "확인 요청을 함께 보냈습니다" not in applied_note["body"]
    assert "포워더 확정" in applied_note["body"]

    _commit_order(forwarder_client, saas_login, forwarder_client, plan_id, order_id)
    committed = [n for n in _notifications(miryang) if n["kind"] == "order_committed"]
    assert len(committed) == 1
    assert f"출하 {MIRYANG_AIR_LOT} 취소" in committed[0]["body"]
    assert "다른 화주" not in committed[0]["body"]


def test_air_switch_request_flow(forwarder_client: TestClient, saas_login) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    pohang = saas_login("shipper-pohang")
    already = _request(
        saas_login("shipper-miryang"), plan_id, {"type": "air_switch", "lot_id": MIRYANG_AIR_LOT}
    )
    assert already.status_code == 409
    assert already.json()["detail"] == ALREADY_AIR

    body = {"type": "air_switch", "lot_id": POHANG_LOT, "new_pallets": 4}
    created = _request(pohang, plan_id, body)
    assert created.status_code == 201, created.text
    assert created.json()["type"] == "air_switch"
    assert created.json()["new_pallets"] is None
    assert created.json()["cancellation_estimate"] is None
    request_id = created.json()["request_id"]
    assert _request(pohang, plan_id, body).status_code == 409

    detail = forwarder_client.get(f"{FORWARDER}/{request_id}")
    assert detail.status_code == 200
    assert detail.json()["event"] == {"type": "air_switch", "lot_id": POHANG_LOT}
    assert detail.json()["replan_id"] is None
    assert detail.json()["status"] == "open"
    assert detail.json()["shipper_org_id"] == "shipper-pohang"

    replan = _replan(forwarder_client, request_id)
    assert replan["recommended_id"] is not None
    applied = _apply(forwarder_client, request_id, {"alternative_id": replan["recommended_id"]})
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assignment = next(a for a in result["plan"]["plan"]["assignments"] if a["lot_id"] == POHANG_LOT)
    assert assignment["mode"] == "air"
    assert POHANG_LOT in result["plan"]["air_locked_lot_ids"]
    assert result["change_request"]["status"] == "applied"
    assert result["change_request"]["alternative_id"] == replan["recommended_id"]
    kinds = {n["kind"] for n in result["notifications"]}
    assert "change_request_applied" in kinds
    assert kinds <= {"change_request_applied", "order_confirmation_requested", "order_notified"}

    again = _request(pohang, plan_id, body)
    assert again.status_code == 409
    assert again.json()["detail"] == ALREADY_AIR


def test_stale_replan_apply_conflicts_and_recovers(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)
    pohang_id = _quantity_request(saas_login("shipper-pohang"), plan_id, POHANG_LOT, 2)
    sangju_id = _quantity_request(saas_login("shipper-sangju"), plan_id, SANGJU_LOT, 0)
    _replan(forwarder_client, pohang_id)
    stale_replan_id = forwarder_client.get(f"{FORWARDER}/{pohang_id}").json()["replan_id"]
    _replan(forwarder_client, sangju_id)
    assert (
        _apply(forwarder_client, sangju_id, {"alternative_id": "pull_forward"}).status_code == 200
    )

    stale = _apply(forwarder_client, pohang_id, {"alternative_id": "pull_forward"})
    assert stale.status_code == 409
    detail = forwarder_client.get(f"{FORWARDER}/{pohang_id}").json()
    assert detail["status"] == "open"
    assert detail["replan_id"] == stale_replan_id

    fresh = _replan(forwarder_client, pohang_id)
    assert fresh["replan_id"] != stale_replan_id
    recovered = _apply(forwarder_client, pohang_id, {"alternative_id": "pull_forward"})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["change_request"]["status"] == "applied"


def test_plan_id_filters_lists_and_notifications(forwarder_client: TestClient, saas_login) -> None:
    first = _plan(forwarder_client, saas_login)
    second = _plan(forwarder_client, saas_login, suffix="-b")
    pohang = saas_login("shipper-pohang")
    first_id = _quantity_request(pohang, first, POHANG_LOT, 2)
    second_created = _request(pohang, second, {"type": "air_switch", "lot_id": POHANG_LOT + "-b"})
    assert second_created.status_code == 201, second_created.text
    second_id = second_created.json()["request_id"]

    mine = pohang.get(f"/api/saas/shipper/change-requests?plan_id={first}").json()
    assert [r["request_id"] for r in mine] == [first_id]
    listed = forwarder_client.get(f"{FORWARDER}?plan_id={second}").json()
    assert [r["request_id"] for r in listed] == [second_id]
    assert len(forwarder_client.get(FORWARDER).json()) == 2

    assert forwarder_client.post(f"{FORWARDER}/{second_id}/reject", json={}).status_code == 200
    first_items = pohang.get(f"/api/saas/shipper/notifications?plan_id={first}").json()
    assert _order_kinds(first_items) == []
    only = pohang.get(f"/api/saas/shipper/notifications?plan_id={second}").json()
    assert [n["change_request_id"] for n in only if n["change_request_id"]] == [second_id]
