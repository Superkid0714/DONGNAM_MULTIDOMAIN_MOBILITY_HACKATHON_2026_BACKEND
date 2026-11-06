import json

from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    SAAS_CSRF,
    accept_plan,
    login_client,
    open_quantity_window,
    queue_quantity,
)
from sqlalchemy import delete

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import PlanForwarderRow

TOTALS = (
    "total_baseline_cost",
    "total_shared_cost",
    "total_savings",
    "total_fee",
    "total_operational_baseline_cost",
    "total_operational_cost",
    "total_operational_savings",
    "objective",
)
REDUCE = {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}


def _scenario():
    scenario = get_scenario("default")
    assert scenario is not None
    return scenario


def _lot_body(lot, **extra) -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "sell_window": lot.sell_window,
        "shipper": "바디 값은 무시된다",
        "lot_id": lot.id,
        **extra,
    }


def _register_all(saas_login) -> list[str]:
    ids = []
    for lot in _scenario().lots:
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert response.status_code == 201, response.text
        registered = response.json()["registered"]
        assert registered["shipper_id"] == lot.shipper_id
        ids.append(registered["id"])
    return ids


def _forwarder_plan(saas_login, forwarder_org_id: str = FORWARDER_ORG_ID) -> dict:
    ids = _register_all(saas_login)
    planner = saas_login(forwarder_org_id)
    response = planner.post("/api/saas/forwarder/plans", json={"lot_ids": ids})
    assert response.status_code == 201, response.text
    return response.json()


def _assign(plan_id: str, forwarder_org_id: str = FORWARDER_ORG_ID) -> None:
    with database.begin() as db:
        db.execute(delete(PlanForwarderRow).where(PlanForwarderRow.plan_id == plan_id))
        db.add(PlanForwarderRow(plan_id=plan_id, forwarder_org_id=forwarder_org_id))


def _shape(plan: dict) -> dict:
    return {
        "totals": {name: plan[name] for name in TOTALS},
        "assignments": sorted(
            (a["lot_id"], a["mode"], a["pallets"], a["pull_forward_days"])
            for a in plan["assignments"]
        ),
        "containers": sorted(
            (c["pallets"], sorted(c["lot_ids"]), c["container_cost"])
            for c in plan["containers"]
            if c["used"]
        ),
        "settlements": sorted(
            (s["shipper_id"], s["baseline_cost"], s["shared_cost"], s["savings"], s["fee"])
            for s in plan["settlements"]
        ),
        "unassigned": plan["unassigned"],
    }


def test_forwarder_plan_from_seeded_orgs_matches_demo_numbers(
    client: TestClient, platform_client: TestClient, saas_login
) -> None:
    saas = _forwarder_plan(saas_login)
    demo = client.post("/api/plans", json={}).json()
    assert _shape(saas["plan"]) == _shape(demo["plan"])
    assert saas["plan"]["total_savings"] > 0
    pallets = sum(a["pallets"] for a in saas["plan"]["assignments"])
    assert pallets == sum(lot.pallets for lot in _scenario().lots)
    assert {s["id"] for s in saas["scenario"]["shippers"]} == {s.id for s in _scenario().shippers}
    assert sorted(saas["registered_lot_ids"]) == sorted(lot.id for lot in _scenario().lots)
    statuses = {
        lot["id"]: lot["status"] for lot in platform_client.get("/api/saas/platform/lots").json()
    }
    assert set(statuses.values()) == {"planned"}


def test_shipper_lot_crud_uses_session_org(
    shipper_client: TestClient, other_shipper_client: TestClient, client: TestClient
) -> None:
    lot = _scenario().lots[0]
    created = shipper_client.post(
        "/api/saas/shipper/lots",
        json=_lot_body(lot, lot_id=None, shipper="sh-sancheong", shipper_org_id="shipper-x"),
    )
    assert created.status_code == 201, created.text
    registered = created.json()["registered"]
    assert registered["shipper_id"] == "sh-jinju"
    lot_id = registered["id"]

    assert [x["id"] for x in shipper_client.get("/api/saas/shipper/lots").json()] == [lot_id]
    assert other_shipper_client.get("/api/saas/shipper/lots").json() == []
    assert client.get("/api/lots").json() == []
    assert [x["id"] for x in shipper_client.get("/api/lots").json()] == [lot_id]

    patched = shipper_client.patch(
        f"/api/saas/shipper/lots/{lot_id}", json={"pallets": 6, "shipper": "sh-sancheong"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["pallets"] == 6
    assert patched.json()["shipper_id"] == "sh-jinju"
    blocked = shipper_client.patch(f"/api/saas/shipper/lots/{lot_id}", json={"status": "planned"})
    assert blocked.status_code == 422

    foreign = other_shipper_client.patch(f"/api/saas/shipper/lots/{lot_id}", json={"pallets": 1})
    assert foreign.status_code == 404
    assert other_shipper_client.delete(f"/api/saas/shipper/lots/{lot_id}").status_code == 404
    assert other_shipper_client.get(f"/api/lots/{lot_id}").status_code == 404
    assert client.get(f"/api/lots/{lot_id}").status_code == 404

    assert shipper_client.delete(f"/api/saas/shipper/lots/{lot_id}").status_code == 204
    assert shipper_client.get("/api/saas/shipper/lots").json() == []


def test_shipper_cannot_register_for_other_shipper_via_generic_intake(
    shipper_client: TestClient,
) -> None:
    body = _lot_body(_scenario().lots[1], shipper="sh-sancheong", save=True)
    response = shipper_client.post("/api/intake/lots", json=body)
    assert response.status_code == 422
    assert shipper_client.get("/api/saas/shipper/lots").json() == []


def test_planned_lots_are_locked(
    forwarder_client: TestClient, saas_login, shipper_client: TestClient
) -> None:
    _forwarder_plan(saas_login)
    patch = shipper_client.patch("/api/saas/shipper/lots/lot-jinju-sb-1", json={"pallets": 2})
    assert patch.status_code == 409
    assert "수량 변경 견적" in patch.json()["detail"]
    removed = shipper_client.delete("/api/saas/shipper/lots/lot-jinju-sb-1")
    assert removed.status_code == 409
    assert removed.json()["detail"] == patch.json()["detail"]
    again = forwarder_client.post("/api/saas/forwarder/plans", json={"lot_ids": ["lot-jinju-sb-1"]})
    assert again.status_code == 409
    empty = forwarder_client.post("/api/saas/forwarder/plans", json={"lot_ids": []})
    assert empty.status_code == 422


def test_role_guards(
    anonymous_saas: TestClient,
    platform_client: TestClient,
    shipper_client: TestClient,
    forwarder_client: TestClient,
) -> None:
    assert anonymous_saas.get("/api/saas/shipper/lots").status_code == 401
    assert anonymous_saas.get("/api/saas/catalog").status_code == 401
    assert shipper_client.get("/api/saas/platform/lots").status_code == 403
    assert shipper_client.post("/api/saas/platform/plans", json={}).status_code == 403
    assert forwarder_client.get("/api/saas/shipper/plans").status_code == 403
    assert platform_client.get("/api/saas/shipper/lots").status_code == 403
    assert platform_client.get("/api/saas/forwarder/plans").status_code == 403
    assert forwarder_client.get("/api/saas/catalog").status_code == 200
    assert shipper_client.post("/api/plans", json={}).status_code == 403
    assert forwarder_client.post("/api/intake/lots", json={}).status_code in (403, 422)


def test_catalog_scopes(platform_client: TestClient, shipper_client: TestClient) -> None:
    every = platform_client.get("/api/saas/catalog").json()
    assert [s["id"] for s in every["shippers"]] == [s.id for s in _scenario().shippers]
    assert len(every["packhouses"]) == 5
    assert every["lots"] == []
    own = shipper_client.get("/api/saas/catalog").json()
    assert [s["id"] for s in own["shippers"]] == ["sh-jinju"]
    assert [p["id"] for p in own["packhouses"]] == ["ph-jinju"]
    assert own["products"] and own["ports"] and own["voyages"]


def test_plan_access_rules(
    client: TestClient,
    platform_client: TestClient,
    saas_login,
    forwarder_client: TestClient,
    other_forwarder_client: TestClient,
) -> None:
    saas = _forwarder_plan(saas_login)
    plan_id = saas["plan_id"]
    demo_id = client.post("/api/plans", json={}).json()["plan_id"]

    assert platform_client.get(f"/api/plans/{plan_id}").status_code == 200
    assert platform_client.get(f"/api/plans/{plan_id}/settlement").status_code == 200
    assert platform_client.get(f"/api/plans/{demo_id}").status_code == 404
    assert platform_client.get("/api/plans").json() == []
    assert client.get(f"/api/plans/{plan_id}").status_code == 404
    assert client.get(f"/api/plans/{plan_id}/settlement").status_code == 404
    assert client.delete(f"/api/plans/{plan_id}").status_code == 404
    assert [p["plan_id"] for p in client.get("/api/plans").json()] == [demo_id]

    assert other_forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}").status_code == 404
    assert other_forwarder_client.get("/api/saas/forwarder/plans").json() == []
    listed = forwarder_client.get("/api/saas/forwarder/plans").json()
    assert [p["plan_id"] for p in listed] == [plan_id]
    assert listed[0]["forwarder"] == {
        "org_id": FORWARDER_ORG_ID,
        "org_name": "동남 콜드체인 포워딩",
    }
    detail = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}").json()
    assert detail["plan"]["settlements"] == []
    assert detail["plan"]["total_fee"] is None
    assert all(p["lat"] is not None for p in detail["scenario"]["packhouses"])
    assert other_forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}").status_code == 404
    assert forwarder_client.get(f"/api/saas/forwarder/plans/{demo_id}").status_code == 404
    assert forwarder_client.get(f"/api/plans/{plan_id}").status_code == 200
    assert other_forwarder_client.get(f"/api/plans/{plan_id}").status_code == 404

    shipper = saas_login("shipper-jinju")
    assert shipper.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 200
    assert shipper.get(f"/api/saas/shipper/plans/{demo_id}").status_code == 404
    assert shipper.get(f"/api/plans/{plan_id}").status_code == 404

    platform_list = platform_client.get("/api/saas/platform/plans").json()
    assert len(platform_list) == 1
    item = platform_list[0]
    assert item["plan_id"] == plan_id
    assert item["shipper_count"] == 5
    assert item["total_pallets"] == 37
    assert item["vessel"] and item["departure"]
    assert item["forwarder"]["org_id"] == FORWARDER_ORG_ID


def test_shipper_without_lots_in_plan_cannot_see_it(
    forwarder_client: TestClient, shipper_client: TestClient, other_shipper_client: TestClient
) -> None:
    lot = _scenario().lots[0]
    assert shipper_client.post("/api/saas/shipper/lots", json=_lot_body(lot)).status_code == 201
    plan = forwarder_client.post("/api/saas/forwarder/plans", json={"lot_ids": [lot.id]}).json()
    plan_id = plan["plan_id"]
    assert shipper_client.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 200
    assert other_shipper_client.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 404
    assert other_shipper_client.get("/api/saas/shipper/plans").json() == []
    assert other_shipper_client.get(f"/api/saas/shipper/plans/{plan_id}/orders").status_code == 404
    assert other_shipper_client.get("/api/saas/shipper/settlements").json() == []


def test_shipper_view_anonymizes_other_shippers(saas_login, shipper_client: TestClient) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    response = shipper_client.get(f"/api/saas/shipper/plans/{plan_id}")
    assert response.status_code == 200
    body = response.json()
    text = json.dumps(body, ensure_ascii=False)
    assert "sh-jinju" in text and "진주 딸기 수출농가 A" in text
    for hidden in ("sh-sancheong", "ph-sancheong", "lot-sancheong-sb-1", "산청", "밀양", "상주"):
        assert hidden not in text, hidden
    packhouses = {p["id"]: p for p in body["scenario"]["packhouses"]}
    assert packhouses["ph-jinju"]["lat"] == 35.18
    others = [p for pid, p in packhouses.items() if pid != "ph-jinju"]
    assert len(others) == 4 and all(p["lat"] is None and p["lon"] is None for p in others)
    names = [s["name"] for s in body["scenario"]["shippers"]]
    assert names[0] == "진주 딸기 수출농가 A"
    assert names[1:] == ["다른 화주 1", "다른 화주 2", "다른 화주 3", "다른 화주 4"]
    assert sum(c["pallets"] for c in body["plan"]["containers"]) > 0

    listed = shipper_client.get("/api/saas/shipper/plans").json()
    assert len(listed) == 1
    assert listed[0]["my_lot_ids"] == ["lot-jinju-sb-1"]
    assert listed[0]["my_pallets"] == 8
    assert listed[0]["total_pallets"] == 37


def test_settlement_views(
    client: TestClient, platform_client: TestClient, saas_login, shipper_client: TestClient
) -> None:
    saas = _forwarder_plan(saas_login)
    demo = client.post("/api/plans", json={}).json()
    rows = shipper_client.get("/api/saas/shipper/settlements").json()
    assert [(r["plan_id"], r["shipper_id"]) for r in rows] == [(saas["plan_id"], "sh-jinju")]
    expected = next(s for s in demo["plan"]["settlements"] if s["shipper_id"] == "sh-jinju")
    assert rows[0]["savings"] == expected["savings"]
    assert rows[0]["fee"] == expected["fee"]

    summary = platform_client.get("/api/saas/platform/settlements").json()
    assert len(summary) == 1
    row = summary[0]
    assert row["plan_id"] == saas["plan_id"]
    assert row["total_saving_krw"] == sum(s["savings"] for s in demo["plan"]["settlements"])
    assert row["platform_fee_krw"] == sum(s["fee"] for s in demo["plan"]["settlements"])
    assert row["platform_fee_krw"] == row["performance_fee_krw"] + row["usage_fee_krw"]
    assert row["usage_fee_krw"] == 20_000 * 18
    names = {s["shipper_id"]: s["shipper_org_name"] for s in row["shippers"]}
    assert names["sh-jinju"] == "진주 딸기 수출농가 A"


def test_order_flow_with_server_decided_actors(saas_login, forwarder_client: TestClient) -> None:
    plan_id = _forwarder_plan(saas_login)["plan_id"]
    accept_plan(saas_login, plan_id)
    open_quantity_window()
    queue_quantity(
        saas_login("shipper-sancheong"), plan_id, REDUCE["lot_id"], REDUCE["new_pallets"]
    )
    event = forwarder_client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE})
    assert event.status_code == 200, event.text
    applied = forwarder_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"}
    )
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["active_order_id"]

    miryang = saas_login("shipper-miryang")
    orders = miryang.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    assert [o["order_id"] for o in orders] == [order_id]
    assert [c["shipper_id"] for c in orders[0]["shipper_confirmations"]] == ["sh-miryang"]
    assert "sh-sancheong" not in json.dumps(orders, ensure_ascii=False)

    early = forwarder_client.post(
        f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm",
        json={"actor": "shipper", "shipper_id": "sh-miryang", "accept": True},
    )
    assert early.status_code == 409

    sancheong = saas_login("shipper-sancheong")
    denied = sancheong.post(
        f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm", json={"accept": True}
    )
    assert denied.status_code == 404

    confirmed = miryang.post(
        f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
        json={"accept": True, "shipper_id": "sh-jinju"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "shipper_confirmed"

    outsider = login_client("forwarder-sinhang")
    path = f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm"
    assert outsider.post(path, json={"accept": True}).status_code == 404
    done = forwarder_client.post(path, json={"actor": "shipper", "accept": True})
    assert done.status_code == 200, done.text
    assert done.json()["status"] in ("forwarder_confirmed", "executed")
    assert done.json()["log"][-1]["actor"] in ("forwarder", "system")
    assert any(entry["actor"] == "forwarder" for entry in done.json()["log"])

    forwarder_orders = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    request = next(
        r
        for r in forwarder_orders[0]["requests"]
        if set(r["lot_ids"]) & {"lot-miryang-sb-1", "lot-miryang-sb-2"}
    )
    shipper_answer = miryang.post(
        f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/requests/{request['request_id']}",
        json={"accepted": True},
    )
    assert shipper_answer.status_code == 403
    request_path = (
        f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/requests/{request['request_id']}"
    )
    assert outsider.post(request_path, json={"accepted": True}).status_code == 404
    answer = forwarder_client.post(request_path, json={"accepted": True})
    assert answer.status_code == 200, answer.text
    answered = next(
        r for r in answer.json()["requests"] if r["request_id"] == request["request_id"]
    )
    assert answered["status"] == "accepted"
    shipper_view = miryang.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    seen = next(r for r in shipper_view[0]["requests"] if r["request_id"] == request["request_id"])
    assert seen["status"] == "accepted"
    jinju = saas_login("shipper-jinju")
    foreign = jinju.post(
        f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/requests/missing",
        json={"accepted": True},
    )
    assert foreign.status_code == 404


def test_saas_mode_rejects_inline_lots(client: TestClient, demo_tenants) -> None:
    settings = get_settings().model_copy(update={"saas_mode": True})
    saas = TestClient(create_app(settings), base_url="https://testserver", headers=SAAS_CSRF)
    lot = _scenario().lots[0].model_dump(mode="json")
    anonymous = saas.post("/api/plans", json={"lots": [{**lot, "id": "lot-inline-1"}]})
    assert anonymous.status_code == 401
    forwarder = login_client(FORWARDER_ORG_ID, saas.app)
    inline = forwarder.post("/api/plans", json={"lots": [{**lot, "id": "lot-inline-1"}]})
    assert inline.status_code == 422
    allowed = client.post("/api/plans", json={"lots": [{**lot, "id": "lot-inline-1"}]})
    assert allowed.status_code == 201


def test_forwarder_uses_generic_routes_with_org_lots(
    platform_client: TestClient,
    forwarder_client: TestClient,
    other_shipper_client: TestClient,
    client: TestClient,
) -> None:
    lot = _scenario().lots[1]
    body = _lot_body(lot, shipper="sh-sancheong", save=True)
    assert platform_client.post("/api/intake/lots", json=body).status_code == 403
    saved = other_shipper_client.post("/api/saas/shipper/lots", json=_lot_body(lot))
    assert saved.status_code == 201, saved.text
    lot_id = saved.json()["registered"]["id"]
    rows = platform_client.get("/api/saas/platform/lots", params={"status": "registered"}).json()
    assert [(r["id"], r["shipper_org_id"]) for r in rows] == [(lot_id, "shipper-sancheong")]
    assert rows[0]["shipper_org_name"] == "산청 딸기 수출농가 B"

    assert platform_client.post("/api/plans", json={"lot_ids": [lot_id]}).status_code == 403
    inline = forwarder_client.post("/api/plans", json={"lots": [lot.model_dump(mode="json")]})
    assert inline.status_code == 422
    created = forwarder_client.post("/api/plans", json={"lot_ids": [lot_id]})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    assert [lot["id"] for lot in created.json()["scenario"]["lots"]] == [lot_id]
    assert client.get(f"/api/plans/{plan_id}").status_code == 404
    assert other_shipper_client.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 200
    assert platform_client.delete(f"/api/plans/{plan_id}").status_code == 403
    assert forwarder_client.delete(f"/api/plans/{plan_id}").status_code == 204
    assert other_shipper_client.get("/api/saas/shipper/lots").json()[0]["status"] == "registered"
