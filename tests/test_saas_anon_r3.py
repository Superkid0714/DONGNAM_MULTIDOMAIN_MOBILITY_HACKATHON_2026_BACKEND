import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID
from test_deep_review_views import _queue_event
from test_saas_api import REDUCE, _forwarder_plan

from freshswitch_backend.api import saas_views
from freshswitch_backend.api.saas_views import shipper_fixed_reasons, shipper_route_changes
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.planning.store import store
from freshswitch_backend.replan.models import FixedLot, RouteChange
from freshswitch_backend.routing.models import PickupPlan, StopVisit, TruckRoute

CANCELLED = "lot-sancheong-sb-1"
CANCEL = {"type": "cancel", "lot_id": CANCELLED}


def _apply(forwarder_client: TestClient, plan_id: str, event: dict) -> dict:
    _queue_event(plan_id, event)
    replanned = forwarder_client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert replanned.status_code == 200, replanned.text
    alternative = next(a["id"] for a in replanned.json()["alternatives"] if a["feasible"])
    applied = forwarder_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative}
    )
    assert applied.status_code == 200, applied.text
    return replanned.json()


def _anon_id(plan: dict, raw_ids: list[str], lot_id: str) -> str:
    return plan["registered_lot_ids"][raw_ids.index(lot_id)]


def _cancelled_plan(saas_login) -> tuple[str, list[str], str]:
    created = _forwarder_plan(saas_login)
    plan_id = created["plan_id"]
    raw_ids = created["registered_lot_ids"]
    jinju = saas_login("shipper-jinju")
    before = jinju.get(f"/api/saas/shipper/plans/{plan_id}").json()
    anon = _anon_id(before, raw_ids, CANCELLED)
    _apply(saas_login(FORWARDER_ORG_ID), plan_id, CANCEL)
    return plan_id, raw_ids, anon


def test_cancelled_lot_keeps_one_anonymous_id_across_plan_and_orders(saas_login) -> None:
    plan_id, raw_ids, anon = _cancelled_plan(saas_login)
    assert anon.startswith("anon-")
    jinju = saas_login("shipper-jinju")
    plan = jinju.get(f"/api/saas/shipper/plans/{plan_id}").json()
    orders = jinju.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    text = json.dumps([plan, orders], ensure_ascii=False)
    assert "sancheong" not in text
    assert "산청" not in text
    assert "lot-jinju-sb-1" in text

    assert _anon_id(plan, raw_ids, CANCELLED) == anon
    assert plan["history"][0]["event"]["lot_id"] == anon
    order = orders[0]
    assert order["event"]["lot_id"] == anon
    assert order["cancellation"] is None
    label = anon.split("-")[1]
    moved = [a for a in order["automatic"] if a["lot_ids"] == [anon]]
    assert moved
    assert moved[0]["description"].startswith(f"{anon}(다른 화주 {label} ")
    assert all(anon in r["lot_ids"] for r in order["requests"] if len(r["lot_ids"]) > 1)
    assert all(
        lot_id == "lot-jinju-sb-1" or lot_id.startswith("anon-")
        for a in order["automatic"]
        for lot_id in a["lot_ids"]
    )


def test_cancelled_shipper_still_sees_own_lot_id(saas_login) -> None:
    plan_id, _, _ = _cancelled_plan(saas_login)
    sancheong = saas_login("shipper-sancheong")
    plan = sancheong.get(f"/api/saas/shipper/plans/{plan_id}").json()
    assert plan["history"][0]["event"]["lot_id"] == CANCELLED
    orders = sancheong.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    assert orders[0]["event"]["lot_id"] == CANCELLED
    assert "lot-jinju" not in json.dumps([plan, orders], ensure_ascii=False)


def _owners(platform_plan: dict) -> dict[str, str]:
    return {lot["id"]: lot["shipper_id"] for lot in platform_plan["scenario"]["lots"]}


def _runs(payload: dict) -> list[tuple[str, int, dict]]:
    return [
        (voyage["voyage_id"], index, route)
        for voyage in payload["routes"]
        for index, route in enumerate(voyage["pickup"]["routes"])
    ]


def test_shipper_routes_hide_money_of_shared_and_other_runs(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    full = platform_client.get(f"/api/plans/{plan_id}").json()
    owners = _owners(full)
    runs = {
        (voyage_id, index): {owners[x] for stop in route["stops"] for x in stop["lot_ids"]}
        for voyage_id, index, route in _runs(full)
    }
    solo = next(key for key, shippers in runs.items() if len(shippers) == 1)
    owner = next(iter(runs[solo]))
    viewer = saas_login(shipper_org_id(owner))
    scoped = viewer.get(f"/api/saas/shipper/plans/{plan_id}").json()
    raw = {(v, i): route for v, i, route in _runs(full)}
    for voyage_id, index, route in _runs(scoped):
        if runs[(voyage_id, index)] == {owner}:
            assert route["cost"] == raw[(voyage_id, index)]["cost"]
            assert route["km"] == raw[(voyage_id, index)]["km"]
        else:
            assert route["cost"] is None
            assert route["km"] is None
            assert all(stop["km_from_prev"] is None for stop in route["stops"])
    for voyage in scoped["routes"]:
        shared = any(
            runs[(voyage["voyage_id"], i)] != {owner}
            for i, _ in enumerate(voyage["pickup"]["routes"])
        )
        if shared:
            assert voyage["pickup"]["total_cost"] is None
            assert voyage["pickup"]["total_km"] is None
    assert all(route["cost"] is not None for _, _, route in _runs(full))


def test_route_changes_and_fixed_reasons_are_shipper_scoped(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    result = forwarder_client.post(f"/api/plans/{plan_id}/events", json={"event": CANCEL}).json()
    changes = [
        RouteChange.model_validate(change)
        for alternative in result["alternatives"]
        for change in alternative["diff"]["route_changes"]
    ]
    assert changes
    session = store.get(plan_id, org_id=FORWARDER_ORG_ID)
    assert session is not None
    owners = {lot.id: lot.shipper_id for lot in session.scenario.lots}
    packhouses = {s.id: s.packhouse_id for s in session.scenario.shippers}
    own = {"sh-jinju"}
    own_lots = {lot_id for lot_id, sid in owners.items() if sid in own}

    scoped = shipper_route_changes(session, changes, own)
    involved = [
        c
        for c in changes
        if own_lots & {*c.lot_ids_before, *c.lot_ids_after}
        or packhouses["sh-jinju"] in {*c.packhouses_before, *c.packhouses_after}
    ]
    assert len(scoped) == len(involved)
    for change in scoped:
        assert change.cost_before is None
        assert change.cost_after is None
        assert change.km_before is None
        assert change.km_after is None
        assert set(change.lot_ids_before) | set(change.lot_ids_after) <= own_lots
        for name in ("packhouses_before", "packhouses_after", "packhouses_added"):
            assert set(getattr(change, name)) <= {packhouses["sh-jinju"]}

    departed = datetime(2026, 11, 17, 7, 0, tzinfo=UTC)
    reasons = [
        FixedLot(
            lot_id="lot-jinju-sb-1",
            reason_code="truck_departed",
            reason="RT-02 진주 딸기 APC 출발",
            voyage_id="V-2611A",
            truck_id="RT-02",
            trip=1,
            packhouse_id=packhouses["sh-jinju"],
            departure=departed,
        ),
        FixedLot(
            lot_id=CANCELLED,
            reason_code="truck_departed",
            reason=f"RT-01 산청 딸기 APC 출발 {CANCELLED}",
            voyage_id="V-2611A",
            truck_id="RT-01",
            trip=1,
            packhouse_id=packhouses["sh-sancheong"],
            departure=departed,
        ),
    ]
    kept = shipper_fixed_reasons(session, reasons, own)
    assert [r.lot_id for r in kept] == ["lot-jinju-sb-1"]
    assert "sancheong" not in json.dumps([c.model_dump(mode="json") for c in scoped])

    pohang = shipper_fixed_reasons(session, reasons, {"sh-pohang"})
    assert pohang == []
    sancheong_view = shipper_fixed_reasons(session, reasons[::-1], {"sh-sancheong"})
    assert [r.lot_id for r in sancheong_view] == [CANCELLED]
    jinju_text = shipper_fixed_reasons(
        session, [reasons[0].model_copy(update={"reason": reasons[1].reason})], own
    )
    assert "산청" not in jinju_text[0].reason
    assert CANCELLED not in jinju_text[0].reason


def test_shipper_cannot_flip_a_decided_confirmation(
    platform_client: TestClient, forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    _apply(forwarder_client, plan_id, REDUCE)
    order_id = platform_client.get(f"/api/plans/{plan_id}").json()["active_order_id"]
    miryang = saas_login("shipper-miryang")
    path = f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm"
    accepted = miryang.post(path, json={"accept": True})
    assert accepted.status_code == 200, accepted.text

    again = miryang.post(path, json={"accept": True})
    assert again.status_code == 409
    flipped = miryang.post(path, json={"accept": False, "shipper_id": "sh-miryang"})
    assert flipped.status_code == 409

    orders = miryang.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    mine = next(o for o in orders if o["order_id"] == order_id)
    assert [c["status"] for c in mine["shipper_confirmations"]] == ["accepted"]
    assert mine["status"] != "rejected"


def test_shipper_rejection_is_final(
    platform_client: TestClient, forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    _apply(forwarder_client, plan_id, REDUCE)
    order_id = platform_client.get(f"/api/plans/{plan_id}").json()["active_order_id"]
    miryang = saas_login("shipper-miryang")
    path = f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm"
    rejected = miryang.post(path, json={"accept": False, "note": "수확 불가"})
    assert rejected.status_code == 200, rejected.text
    assert miryang.post(path, json={"accept": True}).status_code == 409
    assert miryang.post(path, json={"accept": False}).status_code == 409


def _history_lots(plan: dict) -> list[tuple[str, str]]:
    return [
        (h["event"]["type"], (h["event"].get("lot") or {}).get("id") or h["event"]["lot_id"])
        for h in plan["history"]
    ]


def test_added_lot_keeps_anonymous_id_after_cancel(
    platform_client: TestClient, forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    full = platform_client.get(f"/api/plans/{plan_id}").json()
    base = next(lot for lot in full["scenario"]["lots"] if lot["shipper_id"] == "sh-miryang")
    extras = [f"lot-miryang-extra-{n}" for n in (1, 2)]
    miryang = saas_login(shipper_org_id("sh-miryang"))
    for lot_id in extras:
        body = {
            "product": base["product_id"],
            "pallets": base["pallets"],
            "ready_date": base["ready_date"],
            "destination": base["destination"],
            "sell_by_date": base["sell_by_date"],
            "lot_id": lot_id,
        }
        assert miryang.post("/api/saas/shipper/lots", json=body).status_code == 201
        added = forwarder_client.post(
            f"/api/saas/forwarder/plans/{plan_id}/add-lot", json={"lot_id": lot_id}
        )
        assert added.status_code == 200, added.text
        alternative = next(a["id"] for a in added.json()["alternatives"] if a["feasible"])
        applied = forwarder_client.post(
            f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative}
        )
        assert applied.status_code == 200, applied.text
    jinju = saas_login(shipper_org_id("sh-jinju"))
    before = _history_lots(jinju.get(f"/api/saas/shipper/plans/{plan_id}").json())
    first, second = (lot_id for _, lot_id in before)
    assert first.startswith("anon-")
    assert second.startswith("anon-")
    assert first != second

    _apply(forwarder_client, plan_id, {"type": "cancel", "lot_id": extras[0]})
    plan = jinju.get(f"/api/saas/shipper/plans/{plan_id}").json()
    assert _history_lots(plan) == [*before, ("cancel", first)]
    orders = jinju.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    raw_orders = platform_client.get(f"/api/plans/{plan_id}/orders").json()
    expected = dict(zip(extras, (first, second), strict=True))
    assert len(orders) == 3
    for order in orders:
        raw = next(o for o in raw_orders if o["order_id"] == order["order_id"])
        raw_lot = (raw["event"].get("lot") or {}).get("id") or raw["event"]["lot_id"]
        seen = (order["event"].get("lot") or {}).get("id") or order["event"]["lot_id"]
        assert seen == expected.get(raw_lot, seen)
    text = json.dumps([plan, orders], ensure_ascii=False)
    assert "miryang" not in text
    assert "밀양" not in text


def test_stored_owner_fallback_never_marks_viewer_lot(saas_login, monkeypatch) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    session = store.get(plan_id, org_id=FORWARDER_ORG_ID)
    assert session is not None
    ghost = "lot-ghost-reused-1"
    stray = "lot-ghost-other-1"
    widened = session.model_copy(
        update={"registered_lot_ids": [*session.registered_lot_ids, ghost, stray]}
    )
    monkeypatch.setattr(
        saas_views,
        "_stored_lot_shippers",
        lambda lot_ids: {ghost: "sh-jinju", stray: "sh-pohang"},
    )
    anonymizer = saas_views.shipper_anonymizer(widened, {"sh-jinju"})
    assert ghost not in anonymizer.own_lot_ids
    assert anonymizer.text(ghost).startswith(f"anon-{saas_views.UNKNOWN_OWNER_LABEL}-")
    assert anonymizer.text(stray).startswith("anon-")
    assert anonymizer.text(stray) != stray
    payload = saas_views.anonymized_session(widened, {"sh-jinju"})
    assert ghost not in json.dumps(payload)


def test_hidden_runs_expose_only_reviewed_fields(saas_login) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    jinju = saas_login(shipper_org_id("sh-jinju"))
    scoped = jinju.get(f"/api/saas/shipper/plans/{plan_id}").json()
    hidden = [route for _, _, route in _runs(scoped) if route["cost"] is None]
    assert hidden
    for route in hidden:
        assert {k for k, v in route.items() if v is not None} <= saas_views.ROUTE_FIELDS
        for stop in route["stops"]:
            assert {k for k, v in stop.items() if v is not None} <= saas_views.STOP_FIELDS
    for voyage in scoped["routes"]:
        pickup = voyage["pickup"]
        if pickup["total_cost"] is None:
            assert {k for k, v in pickup.items() if v is not None} <= saas_views.PICKUP_FIELDS
    reviewed_money = {"cost", "km", "km_from_prev", "total_cost", "total_km"}
    assert set(TruckRoute.model_fields) == saas_views.ROUTE_FIELDS | {"cost", "km"}
    assert set(StopVisit.model_fields) == saas_views.STOP_FIELDS | {"km_from_prev"}
    assert set(PickupPlan.model_fields) == saas_views.PICKUP_FIELDS | {"total_cost", "total_km"}
    assert reviewed_money.isdisjoint(
        saas_views.ROUTE_FIELDS | saas_views.STOP_FIELDS | saas_views.PICKUP_FIELDS
    )
