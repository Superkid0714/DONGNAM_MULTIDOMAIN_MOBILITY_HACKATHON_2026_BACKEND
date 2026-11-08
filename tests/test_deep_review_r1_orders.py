from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.domain.models import KST, Quantity
from freshswitch_backend.execution import orders as orders_module
from freshswitch_backend.execution.models import ExecutionOrder
from freshswitch_backend.execution.params import ExecutionParams
from freshswitch_backend.planning import store as store_module
from freshswitch_backend.planning.store import store


def _create(client: TestClient) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def _alternatives(client: TestClient, plan_id: str, event: dict) -> dict:
    response = client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert response.status_code == 200, response.text
    return {a["id"]: a for a in response.json()["alternatives"]}


def _apply(client: TestClient, plan_id: str, alternative: str) -> None:
    response = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative})
    assert response.status_code == 200, response.text


def _rejected(client: TestClient, plan_id: str, alternative: str, phrase: str) -> None:
    response = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative})
    assert response.status_code == 409, response.text
    assert phrase in response.json()["detail"]
    session = client.get(f"/api/plans/{plan_id}").json()
    assert session["version"] == 1
    assert _orders(client, plan_id) == []


def _orders(client: TestClient, plan_id: str) -> list[dict]:
    return client.get(f"/api/plans/{plan_id}/orders").json()


def test_replan3_order_past_air_deadline_is_rejected_without_floor(client: TestClient) -> None:
    plan_id = _create(client)
    now = datetime(2026, 11, 17, 9, 0, tzinfo=KST)
    store.clock = lambda: now
    alternatives = _alternatives(client, plan_id, {"type": "cancel", "lot_id": "lot-miryang-sb-2"})
    _rejected(client, plan_id, next(iter(alternatives)), "포워더 확정 기한")


def test_replan3_delay_after_cutoff_has_no_fallback_dispatch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[ExecutionOrder] = []

    def spy(**kwargs: object) -> ExecutionOrder:
        order = orders_module.build_order(**kwargs)
        built.append(order)
        return order

    monkeypatch.setattr(store_module, "build_order", spy)
    plan_id = _create(client)
    now = datetime(2026, 11, 18, 10, 0, tzinfo=KST)
    store.clock = lambda: now
    event = {
        "type": "delay",
        "voyage_id": "V-2611A",
        "new_etd": "2026-11-19T18:00:00+09:00",
        "source": "해수부",
    }
    alternatives = _alternatives(client, plan_id, event)
    assert "sail_as_is" in alternatives
    _rejected(client, plan_id, "sail_as_is", "기한")
    order = built[-1]
    assert "이미 터미널에 반입" in order.deadlines.basis
    assert order.deadlines.forwarder == min(r.deadline for r in order.requests)
    assert "하한" not in order.deadlines.basis


def test_replan3_default_deadlines_unchanged(client: TestClient) -> None:
    plan_id = _create(client)
    _alternatives(
        client, plan_id, {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}
    )
    _apply(client, plan_id, "pull_forward")
    order = _orders(client, plan_id)[-1]
    assert "하한" not in order["deadlines"]["basis"]
    dispatch = datetime.fromisoformat(order["deadlines"]["dispatch"])
    forwarder = datetime.fromisoformat(order["deadlines"]["forwarder"])
    assert forwarder <= dispatch


def test_replan3_passed_harvest_notice_is_rejected_without_floor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = ExecutionParams(
        harvest_notice_hours=Quantity(value=24 * 30, unit="시간", source="가정: 테스트")
    )
    monkeypatch.setattr(orders_module, "ExecutionParams", lambda: params)
    now = datetime(2026, 11, 10, 9, 0, tzinfo=KST)
    store.clock = lambda: now
    plan_id = _create(client)
    _alternatives(
        client, plan_id, {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}
    )
    _rejected(client, plan_id, "pull_forward", "화주 확인 기한")


def _etas(routes: list[dict], container_lots: set[str]) -> set[tuple[str, str]]:
    found = set()
    for voyage_pickup in routes:
        for route in voyage_pickup["pickup"]["routes"]:
            if any(lot in container_lots for stop in route["stops"] for lot in stop["lot_ids"]):
                found.add((route["truck_id"], route["port_eta"]))
    return found


def test_replan12_unchanged_container_with_new_truck_eta_gets_cy_entry(client: TestClient) -> None:
    plan_id = _create(client)
    before = client.get(f"/api/plans/{plan_id}").json()
    alternatives = _alternatives(client, plan_id, {"type": "cancel", "lot_id": "lot-jinju-sb-1"})
    chosen = None
    for alt_id, alternative in alternatives.items():
        if not alternative.get("feasible", True):
            continue
        lots = {
            a["lot_id"]
            for a in alternative["plan"]["assignments"]
            if a["mode"] == "sea" and a["container_id"] == "CA-02"
        }
        same = lots == {
            a["lot_id"]
            for a in before["plan"]["assignments"]
            if a["mode"] == "sea" and a["container_id"] == "CA-02"
        }
        if same and _etas(alternative["routes"], lots) != _etas(before["routes"], lots):
            chosen = alt_id
            break
    assert chosen is not None
    _apply(client, plan_id, chosen)
    order = _orders(client, plan_id)[-1]
    entries = [r for r in order["requests"] if r["kind"] == "cy_entry"]
    assert any(r["container_id"] == "CA-02" for r in entries)
