import json
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    QUANTITY_WINDOW_NOW,
    login_client,
    open_quantity_window,
    queue_quantity,
)
from sqlalchemy import update
from test_saas_api import _forwarder_plan, _lot_body, _scenario

from freshswitch_backend.api import saas_views
from freshswitch_backend.coldchain.models import Alert
from freshswitch_backend.execution.card import iso6346_check_digit
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import PlanRow, ShipperProfileRow
from freshswitch_backend.planning.quantity_policy import load_ledger, policy_rows
from freshswitch_backend.planning.store import PlanSession, store
from freshswitch_backend.replan.models import RouteChange

CANCELLED = "lot-sancheong-sb-1"
CANCEL = {"type": "cancel", "lot_id": CANCELLED}
REQUESTER_CANCEL = {"type": "cancel", "lot_id": "lot-jinju-sb-1"}
REJECT_NOTE = "부담이 너무 큼, 단가 재협상 요구"
ACCEPT_NOTE = "산청 측 수락 메모"


def _register(saas_login, **overrides) -> list[str]:
    ids = []
    for lot in _scenario().lots:
        extra = overrides.get(lot.product_id, {})
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post("/api/saas/shipper/lots", json=_lot_body(lot, **extra))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    return ids


def _plan(forwarder_client: TestClient, ids: list[str]) -> str:
    response = forwarder_client.post("/api/saas/forwarder/plans", json={"lot_ids": ids})
    assert response.status_code == 201, response.text
    return response.json()["plan_id"]


def _owner(plan_id: str) -> str:
    with database.begin() as db:
        row = db.get(PlanRow, plan_id)
        assert row is not None
        return row.org_id


def _bound_quote(plan_id: str, lot_id: str, quantity: int) -> tuple[bool, bool]:
    with database.begin() as db:
        ledgers = [load_ledger(row) for row in policy_rows(db, plan_id)]
    bound = [ledger for ledger in ledgers if lot_id in ledger.reservations]
    queued = any(
        r.get("lot_id") == lot_id and r.get("status") == "queued" and r.get("quantity") == quantity
        for ledger in bound
        for r in ledger.requests.values()
    )
    return bool(bound), queued


def _queue_event(plan_id: str, event: dict) -> None:
    if event["type"] not in {"cancel", "reduce", "increase"}:
        return
    lot_id = event["lot_id"]
    quantity = event.get("new_pallets", 0) if event["type"] != "cancel" else 0
    bound, queued = _bound_quote(plan_id, lot_id, quantity)
    if not bound or queued:
        return
    if store.clock() < QUANTITY_WINDOW_NOW:
        open_quantity_window()
    session = _session(plan_id)
    shipper_id = next(lot.shipper_id for lot in session.scenario.lots if lot.id == lot_id)
    queue_quantity(login_client(shipper_org_id(shipper_id)), plan_id, lot_id, quantity)


def _apply(forwarder_client: TestClient, plan_id: str, event: dict) -> str:
    _queue_event(plan_id, event)
    replanned = forwarder_client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert replanned.status_code == 200, replanned.text
    alternative = next(a["id"] for a in replanned.json()["alternatives"] if a["feasible"])
    applied = forwarder_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative}
    )
    assert applied.status_code == 200, applied.text
    return applied.json()["active_order_id"]


def _confirm(saas_login, plan_id: str, order_id: str, sid: str, accept: bool, note: str):
    client = saas_login(shipper_org_id(sid))
    response = client.post(
        f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
        json={"accept": accept, "note": note},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _session(plan_id: str) -> PlanSession:
    session = store.get(plan_id, org_id=_owner(plan_id))
    assert session is not None
    return session


def _renamed(session: PlanSession, old: str, new: str) -> PlanSession:
    raw = json.dumps(session.model_dump(mode="json"), ensure_ascii=False)
    return PlanSession.model_validate(json.loads(raw.replace(f'"{old}"', f'"{new}"')))


def _shape(payload: dict) -> list[tuple]:
    return [
        (a["mode"], a["container_id"], a["product_id"], a["voyage_id"], a["pallets"])
        for a in payload["plan"]["assignments"]
    ]


def _container(session: PlanSession) -> str:
    return next(c.container_id for c in session.plan.containers if c.used)


def _structural(session: PlanSession, hostile: str) -> str:
    if hostile == "{container}":
        return _container(session)
    if hostile == "{voyage}":
        return next(a.voyage_id for a in session.plan.assignments if a.voyage_id)
    return hostile


@pytest.mark.parametrize(
    "hostile",
    ["sea", "air", "sh-jinju", "{container}", "strawberry", "{voyage}", "부산신항", "KRPUS"],
)
def test_hostile_lot_id_is_mapped_only_in_lot_fields(saas_login, hostile: str) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    original = _session(plan_id)
    hostile = _structural(original, hostile)
    session = _renamed(original, CANCELLED, hostile)
    baseline = saas_views.anonymized_session(original, {"sh-jinju"})
    payload = saas_views.anonymized_session(session, {"sh-jinju"})

    assert _shape(payload) == _shape(baseline)
    own = [a for a in payload["plan"]["assignments"] if a["lot_id"].startswith("lot-jinju")]
    assert own and {a["shipper_id"] for a in own} == {"sh-jinju"}
    lot_ids = [a["lot_id"] for a in payload["plan"]["assignments"]]
    assert hostile not in lot_ids
    assert hostile not in payload["registered_lot_ids"]
    assert hostile not in [lot["id"] for lot in payload["scenario"]["lots"]]
    index = [a["lot_id"] for a in baseline["plan"]["assignments"]]
    assert lot_ids == index
    assert payload["scenario"]["ports"] == baseline["scenario"]["ports"]
    assert payload["scenario"]["voyages"] == baseline["scenario"]["voyages"]
    assert payload["scenario"]["products"] == baseline["scenario"]["products"]
    assert [c["container_id"] for c in payload["plan"]["containers"]] == [
        c["container_id"] for c in baseline["plan"]["containers"]
    ]
    assert payload["scenario"]["shippers"] == baseline["scenario"]["shippers"]


def test_free_text_replaces_only_whole_lot_tokens(saas_login) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    session = _renamed(_session(plan_id), CANCELLED, "lot-evil")
    anonymizer = saas_views.shipper_anonymizer(session, {"sh-jinju"})
    anon = anonymizer.lot("lot-evil")
    assert anon.startswith("anon-")
    assert anonymizer.text("lot-evil 5파렛트, lot-evil-2, xlot-evil") == (
        f"{anon} 5파렛트, lot-evil-2, xlot-evil"
    )
    container = _container(_session(plan_id))
    reserved = _renamed(_session(plan_id), CANCELLED, container)
    kept = saas_views.shipper_anonymizer(reserved, {"sh-jinju"})
    assert kept.text(f"{container} 적재 17→14파렛트") == f"{container} 적재 17→14파렛트"
    assert kept.lot(container).startswith("anon-")


STRUCTURAL = {"level": "critical", "basis": "operational", "change": "changed", "stage": "vessel"}


def _route_change(session: PlanSession, hostile: str) -> RouteChange:
    own = next(lot for lot in session.scenario.lots if lot.shipper_id == "sh-jinju")
    packhouse = next(s.packhouse_id for s in session.scenario.shippers if s.id == "sh-jinju")
    return RouteChange(
        voyage_id=session.scenario.voyages[0].id,
        truck_id=session.scenario.trucks[0].id,
        change="changed",
        packhouses_before=[packhouse],
        packhouses_after=[packhouse],
        packhouses_added=[],
        packhouses_removed=[],
        lot_ids_before=[own.id, hostile],
        lot_ids_after=[own.id],
        trips_before=1,
        trips_after=1,
        departure_before=None,
        departure_after=None,
        km_before=10.0,
        km_after=8.0,
        cost_before=100,
        cost_after=80,
    )


@pytest.mark.parametrize("hostile", ["critical", "operational", "changed", "warning", "added"])
def test_enum_literal_lot_id_keeps_structural_values(saas_login, hostile: str) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    original = _session(plan_id)
    session = _renamed(original, CANCELLED, hostile)
    anonymizer = saas_views.shipper_anonymizer(session, {"sh-jinju"})
    assert anonymizer.lot(hostile).startswith("anon-")
    assert anonymizer.walk(STRUCTURAL) == STRUCTURAL
    assert anonymizer.walk({"lot_id": hostile})["lot_id"] == anonymizer.lot(hostile)

    baseline = saas_views.anonymized_session(original, {"sh-jinju"})
    payload = saas_views.anonymized_session(session, {"sh-jinju"})
    for name in ("settlements", "container_settlements"):
        assert json.dumps(payload["plan"][name]) == json.dumps(baseline["plan"][name])
    assert [s["basis"] for s in payload["plan"]["settlements"]] == [
        s["basis"] for s in baseline["plan"]["settlements"]
    ]

    view = saas_views.shipper_coldchain_view(session, {"sh-jinju"})
    for level in ("info", "warning", "critical"):
        alert = Alert(
            key="k",
            container_id="CA-01",
            level=level,
            kind="power_cut",
            title="전원 차단",
            message="전원 차단",
            at=datetime(2026, 11, 18, tzinfo=UTC),
        )
        assert view.alert(alert).level == level

    changes = saas_views.shipper_route_changes(
        session, [_route_change(session, hostile)], {"sh-jinju"}
    )
    assert [c.change for c in changes] == ["changed"]
    assert hostile not in changes[0].lot_ids_before


def _other_names(session: PlanSession) -> tuple[str, str]:
    shipper = next(s for s in session.scenario.shippers if s.id == "sh-sancheong")
    packhouse = next(p for p in session.scenario.packhouses if p.id == shipper.packhouse_id)
    return shipper.name, packhouse.name


@pytest.mark.parametrize(
    "ending", ["에서", "까지", "만 영향", "이며", "님께서", "로부터", "의", "가"]
)
def test_full_name_followed_by_any_ending_is_anonymized(saas_login, ending: str) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    session = _session(plan_id)
    name, packhouse = _other_names(session)
    anonymizer = saas_views.shipper_anonymizer(session, {"sh-jinju"})
    shipper_text = anonymizer.text(f"{name}{ending} 출하를 취소했습니다")
    assert name not in shipper_text
    assert shipper_text.startswith(f"다른 화주 {anonymizer.labels['sh-sancheong']}{ending}")
    packhouse_text = anonymizer.text(f"{packhouse}{ending} 집하")
    assert packhouse not in packhouse_text
    assert packhouse_text.endswith(f"선과장{ending} 집하")


def test_single_word_name_keeps_ordinary_words(saas_login) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    session = _session(plan_id)
    scenario = session.scenario.model_copy(
        update={
            "shippers": [
                s.model_copy(update={"name": "부산"}) if s.id == "sh-sancheong" else s
                for s in session.scenario.shippers
            ]
        }
    )
    anonymizer = saas_views.Anonymizer(scenario, {"sh-jinju"})
    label = f"다른 화주 {anonymizer.labels['sh-sancheong']}"
    assert anonymizer.text("부산에서 취소") == f"{label}에서 취소"
    assert anonymizer.text("부산까지 이동") == f"{label}까지 이동"
    assert anonymizer.text("부산신항 2부두") == "부산신항 2부두"


@pytest.mark.parametrize("lot_id", ["20261118-01", "B1"])
def test_numeric_or_short_lot_id_is_replaced_in_moved_text(saas_login, lot_id: str) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    session = _renamed(_session(plan_id), CANCELLED, lot_id)
    anonymizer = saas_views.shipper_anonymizer(session, {"sh-jinju"})
    anon = anonymizer.lot(lot_id)
    label = anonymizer.label("sh-sancheong")
    assert anon.startswith("anon-")
    moved = anonymizer.text(f"{lot_id}(산청 5파렛트): CA-01 → 항공")
    assert moved == f"{anon}({label} 5파렛트): CA-01 → 항공"
    if lot_id == "20261118-01":
        assert anonymizer.text(f"{lot_id} 출하 취소") == f"{anon} 출하 취소"


def test_container_text_is_not_relabelled_when_lot_collides(saas_login) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    container = _container(_session(plan_id))
    session = _renamed(_session(plan_id), CANCELLED, container)
    anonymizer = saas_views.shipper_anonymizer(session, {"sh-jinju"})
    request = f"선적 예정 {container}(40RH-CA, 12파렛트) 반입 즉시 전원 연결"
    assert anonymizer.text(request) == request
    moved = anonymizer.text(f"{container}(산청 5파렛트): CA-02 → 항공")
    label = anonymizer.label("sh-sancheong")
    assert moved == f"{anonymizer.lot(container)}({label} 5파렛트): CA-02 → 항공"


def _cancel_order(
    platform_client: TestClient, forwarder_client: TestClient, saas_login
) -> tuple[str, str]:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    order_id = _apply(forwarder_client, plan_id, REQUESTER_CANCEL)
    order = platform_client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    assert {c["shipper_id"] for c in order["shipper_confirmations"]} == {
        "sh-miryang",
        "sh-sancheong",
    }
    return plan_id, order_id


def _miryang_order(saas_login, plan_id: str, order_id: str) -> dict:
    orders = saas_login(shipper_org_id("sh-miryang"))
    listed = orders.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    return next(o for o in listed if o["order_id"] == order_id)


def test_shipper_order_log_hides_other_shipper_rejection(
    platform_client: TestClient, forwarder_client: TestClient, saas_login
) -> None:
    plan_id, order_id = _cancel_order(platform_client, forwarder_client, saas_login)
    _confirm(saas_login, plan_id, order_id, "sh-sancheong", False, REJECT_NOTE)
    raw = platform_client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    assert any(entry["note"] == REJECT_NOTE for entry in raw["log"])

    order = _miryang_order(saas_login, plan_id, order_id)
    assert order["status"] == "rejected"
    text = json.dumps(order["log"], ensure_ascii=False)
    assert REJECT_NOTE not in text
    assert "화주 거절" not in text
    assert all(entry["shipper_id"] in (None, "sh-miryang") for entry in order["log"])
    others = [e for e in order["log"] if e["actor"] == "shipper"]
    assert [e["action"] for e in others] == ["화주 확인 → 이전 확정 계획으로 복귀"]
    assert others[0]["note"] is None

    own = saas_login(shipper_org_id("sh-sancheong"))
    listed = own.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    mine = next(o for o in listed if o["order_id"] == order_id)
    assert any(entry["note"] == REJECT_NOTE for entry in mine["log"])


def test_shipper_order_log_drops_other_acceptance_and_keeps_own(
    platform_client: TestClient, forwarder_client: TestClient, saas_login
) -> None:
    plan_id, order_id = _cancel_order(platform_client, forwarder_client, saas_login)
    _confirm(saas_login, plan_id, order_id, "sh-sancheong", True, ACCEPT_NOTE)
    order = _miryang_order(saas_login, plan_id, order_id)
    assert [e for e in order["log"] if e["actor"] == "shipper"] == []
    assert ACCEPT_NOTE not in json.dumps(order, ensure_ascii=False)

    _confirm(saas_login, plan_id, order_id, "sh-miryang", True, "밀양 메모")
    order = _miryang_order(saas_login, plan_id, order_id)
    shipper_log = [e for e in order["log"] if e["actor"] == "shipper"]
    assert [(e["action"], e["shipper_id"], e["note"]) for e in shipper_log] == [
        ("모든 화주 확인 완료", "sh-miryang", "밀양 메모")
    ]
    assert ACCEPT_NOTE not in json.dumps(order, ensure_ascii=False)


def _rename(saas_login, sid: str, name: str) -> None:
    with database.begin() as db:
        db.execute(update(ShipperProfileRow).where(ShipperProfileRow.id == sid).values(name=name))


def _shipper_view(saas_login, sid: str, plan_id: str) -> tuple[dict, list[dict]]:
    client = saas_login(shipper_org_id(sid))
    plan = client.get(f"/api/saas/shipper/plans/{plan_id}").json()
    orders = client.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    return plan, orders


@pytest.mark.parametrize("name", ["부산 수출농가 F", "CA 수출", "sea farm", "홍콩항 농가"])
def test_short_name_does_not_rewrite_ordinary_words(
    platform_client: TestClient, forwarder_client: TestClient, saas_login, name: str
) -> None:
    _rename(saas_login, "sh-sancheong", name)
    plan_id = _plan(forwarder_client, _register(saas_login))
    _apply(forwarder_client, plan_id, CANCEL)
    full = platform_client.get(f"/api/plans/{plan_id}").json()
    assert name in [s["name"] for s in full["scenario"]["shippers"]]
    plan, orders = _shipper_view(saas_login, "sh-miryang", plan_id)

    assert [v["terminal"] for v in plan["scenario"]["voyages"]] == [
        v["terminal"] for v in full["scenario"]["voyages"]
    ]
    assert [p["name"] for p in plan["scenario"]["ports"]] == [
        p["name"] for p in full["scenario"]["ports"]
    ]
    assert {a["mode"] for a in plan["plan"]["assignments"]} == {
        a["mode"] for a in full["plan"]["assignments"]
    }
    assert [c["container_id"] for c in plan["plan"]["containers"]] == [
        c["container_id"] for c in full["plan"]["containers"]
    ]
    text = json.dumps([plan, orders], ensure_ascii=False)
    assert name not in text
    assert "다른 화주 1신항" not in text
    assert "다른 화주 1-0" not in text
    for order in orders:
        for request in order["requests"]:
            if request["counterparty_role"] == "terminal":
                assert request["counterparty"].startswith("부산신항")


def test_same_region_other_shipper_is_still_anonymized(
    platform_client: TestClient, forwarder_client: TestClient, saas_login
) -> None:
    _rename(saas_login, "sh-sancheong", "밀양 산청 수출농가 B")
    plan_id = _plan(forwarder_client, _register(saas_login))
    _apply(forwarder_client, plan_id, CANCEL)
    raw_orders = platform_client.get(f"/api/plans/{plan_id}/orders").json()
    raw_text = json.dumps(raw_orders, ensure_ascii=False)
    assert "밀양 5→0" in raw_text
    assert "밀양 4→6" in raw_text

    plan, orders = _shipper_view(saas_login, "sh-miryang", plan_id)
    text = json.dumps([plan, orders], ensure_ascii=False)
    assert "밀양 산청 수출농가 B" not in text
    assert "밀양 5→0" not in text
    assert "밀양 4→6" in text
    anon = next(
        a["shipper_id"]
        for a in plan["plan"]["assignments"]
        if a["shipper_id"] not in ("sh-miryang",) and a["lot_id"].startswith("anon-")
    )
    assert anon.startswith("anon-")
    compositions = [
        a["description"]
        for o in orders
        for a in o["automatic"]
        if a["kind"] == "slot_reassignment" and "적재" in a["description"]
    ]
    assert compositions
    for description in compositions:
        assert "밀양 4→6" in description
        assert "다른 화주 " in description
        assert description.count("밀양 ") == 1
    moved = [
        a["description"]
        for o in orders
        for a in o["automatic"]
        if a["kind"] == "slot_reassignment" and "파렛트):" in a["description"]
    ]
    assert any(d.startswith("anon-") and "(다른 화주 " in d for d in moved)
    assert any(d.startswith("lot-miryang") and "(밀양 " in d for d in moved)

    sancheong_plan, sancheong_orders = _shipper_view(saas_login, "sh-sancheong", plan_id)
    other = json.dumps([sancheong_plan, sancheong_orders], ensure_ascii=False)
    assert "밀양 딸기 수출농가 C" not in other
    assert "밀양 4→6" not in other


LATE_STRAWBERRY = {
    "strawberry": {
        "ready_date": date(2026, 11, 19).isoformat(),
        "sell_by_date": date(2026, 11, 30).isoformat(),
    },
}


def test_shipper_list_and_settlements_use_own_voyage(
    platform_client: TestClient, forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, _register(saas_login, **LATE_STRAWBERRY))
    full = platform_client.get(f"/api/plans/{plan_id}").json()
    voyages = {v["id"]: v for v in full["scenario"]["voyages"]}
    sea = {
        a["shipper_id"]: a["voyage_id"] for a in full["plan"]["assignments"] if a["mode"] == "sea"
    }
    assert sea["sh-jinju"] != sea["sh-pohang"]
    earliest = min(voyages.values(), key=lambda v: v["etd"])
    mine = voyages[sea["sh-jinju"]]
    assert earliest["id"] != mine["id"]

    jinju = saas_login(shipper_org_id("sh-jinju"))
    item = next(p for p in jinju.get("/api/saas/shipper/plans").json() if p["plan_id"] == plan_id)
    assert item["vessel"] == mine["vessel_name"]
    assert item["departure"][:16] == mine["etd"][:16]
    rows = [r for r in jinju.get("/api/saas/shipper/settlements").json() if r["plan_id"] == plan_id]
    assert rows
    assert {r["vessel"] for r in rows} == {mine["vessel_name"]}

    pohang = saas_login(shipper_org_id("sh-pohang"))
    grape = next(p for p in pohang.get("/api/saas/shipper/plans").json() if p["plan_id"] == plan_id)
    assert grape["vessel"] == voyages[sea["sh-pohang"]]["vessel_name"]

    listed = platform_client.get("/api/saas/platform/plans").json()
    platform_item = next(p for p in listed if p["plan_id"] == plan_id)
    assert platform_item["vessel"] == earliest["vessel_name"]


def test_air_only_shipper_has_no_vessel(saas_login) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    session = _session(plan_id)
    air = session.plan.model_copy(
        update={
            "assignments": [
                a.model_copy(update={"mode": "air", "container_id": None, "voyage_id": None})
                if a.shipper_id == "sh-jinju"
                else a
                for a in session.plan.assignments
            ]
        }
    )
    widened = session.model_copy(update={"plan": air})
    item = saas_views.shipper_plan_item(widened, {"lot-jinju-sb-1"})
    assert (item.vessel, item.departure) == (None, None)
    rows = saas_views.shipper_settlements(widened, {"sh-jinju"})
    assert rows
    assert {(r.vessel, r.departure) for r in rows} == {(None, None)}


def _number(serial: str, owner: str) -> str:
    code = owner + serial
    return f"{code}{iso6346_check_digit(code)}"


def test_shipper_plan_detail_shows_only_own_container_numbers(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    card = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/execution-card").json()
    by_container = {c["container_id"]: c for c in card["containers"]}
    full = platform_client.get(f"/api/plans/{plan_id}").json()
    owned = {
        a["shipper_id"]: a["container_id"]
        for a in full["plan"]["assignments"]
        if a["mode"] == "sea"
    }
    strawberry, grape = owned["sh-jinju"], owned["sh-pohang"]
    assert strawberry != grape
    entered = _number("305438", "CSQU")
    response = forwarder_client.put(
        f"/api/saas/forwarder/plans/{plan_id}/execution-card/containers/{strawberry}",
        json={"booking_no": "HDMU-BK1234", "container_no": entered},
    )
    assert response.status_code == 200, response.text

    jinju = saas_login(shipper_org_id("sh-jinju"))
    plan = jinju.get(f"/api/saas/shipper/plans/{plan_id}").json()
    numbers = plan["container_numbers"]
    assert [n["container_id"] for n in numbers] == [strawberry]
    row = numbers[0]
    assert row["container_no"] == entered
    assert row["container_no_mock"] is False
    assert row["seal_no"] == by_container[strawberry]["seal_no"]
    assert row["seal_no_mock"] is True
    assert row["mock"] is True
    assert "booking_no" not in row
    text = json.dumps(plan, ensure_ascii=False)
    assert "HDMU-BK1234" not in text
    assert by_container[strawberry]["booking_no"] not in text
    assert by_container[grape]["container_no"] not in text
    assert jinju.get(f"/api/saas/shipper/plans/{plan_id}/containers").json() == numbers

    pohang = saas_login(shipper_org_id("sh-pohang"))
    grape_numbers = pohang.get(f"/api/saas/shipper/plans/{plan_id}").json()["container_numbers"]
    assert [n["container_id"] for n in grape_numbers] == [grape]
    assert grape_numbers[0]["container_no"] == by_container[grape]["container_no"]
    assert grape_numbers[0]["mock"] is True
    assert entered not in json.dumps(grape_numbers)
