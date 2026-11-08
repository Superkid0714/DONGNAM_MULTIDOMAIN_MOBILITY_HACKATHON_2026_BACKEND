from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.config import get_settings
from freshswitch_backend.execution import orders as orders_module
from freshswitch_backend.execution.models import ExecutionOrder
from freshswitch_backend.main import create_app
from freshswitch_backend.planning import store as store_module
from freshswitch_backend.replan.engine import executed_dispatches
from freshswitch_backend.replan.models import Alternative

REDUCE = {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}


def _captured(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, ExecutionOrder]:
    calls: list[tuple[dict, ExecutionOrder]] = []

    def spy(**kwargs: object) -> ExecutionOrder:
        order = orders_module.build_order(**kwargs)
        calls.append((dict(kwargs), order))
        return order

    monkeypatch.setattr(store_module, "build_order", spy)
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    response = client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE})
    assert response.status_code == 200, response.text
    assert all("warnings" not in a for a in response.json()["alternatives"])
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert applied.status_code == 200, applied.text
    return calls[-1]


def _dispatches(order: ExecutionOrder, truck_id: str) -> list:
    return [a for a in order.automatic if a.kind == "truck_dispatch" and a.truck_id == truck_id]


def test_first_log_note_carries_alternative_warnings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, order = _captured(client, monkeypatch)
    assert kwargs["alternative"].warnings == []
    assert order.log[0].action == "대안 pull_forward 적용 → 실행 지시 생성"
    assert order.log[0].note is None
    warned = kwargs["alternative"].model_copy(
        update={"warnings": ["DISPATCH_RELEASED: RT-01", "고정 해제"]}
    )
    rebuilt = orders_module.build_order(**{**kwargs, "alternative": warned})
    assert rebuilt.log[0].note == "DISPATCH_RELEASED: RT-01; 고정 해제"
    assert all(entry.note is None for entry in rebuilt.log[1:])


def _extended(
    kwargs: dict, order: ExecutionOrder, shift: timedelta, lot_id: str = "lot-split"
) -> tuple[Alternative, str]:
    alternative = kwargs["alternative"]
    voyage_pickup, route = next(
        (vp, r)
        for vp in alternative.routes
        for r in vp.pickup.routes
        if _dispatches(order, r.truck_id)
    )
    other = next(v.id for v in kwargs["scenario_after"].voyages if v.id != voyage_pickup.voyage_id)
    stop = route.stops[0].model_copy(
        update={
            "stop_id": "stop-split",
            "lot_ids": [lot_id],
            "pallets": 2,
            "sequence": route.stops[-1].sequence + 1,
        }
    )
    part = route.model_copy(
        update={
            "stops": [stop],
            "load_pallets": 2,
            "earliest_departure": route.earliest_departure + shift,
        }
    )
    extra = voyage_pickup.model_copy(
        update={
            "voyage_id": other,
            "lot_ids": [lot_id],
            "pickup": voyage_pickup.pickup.model_copy(update={"routes": [part]}),
        }
    )
    extended = alternative.model_copy(update={"routes": [*alternative.routes, extra]})
    return extended, f"{route.truck_id}|{voyage_pickup.voyage_id}|{other}|{route.load_pallets}"


def _split(kwargs: dict, order: ExecutionOrder, shift: timedelta) -> tuple[ExecutionOrder, str]:
    extended, marker = _extended(kwargs, order, shift)
    return orders_module.build_order(**{**kwargs, "alternative": extended}), marker


def _executed(order: ExecutionOrder, hours: int) -> ExecutionOrder:
    at = order.created_at + timedelta(hours=hours)
    return order.model_copy(update={"status": "executed", "updated_at": at})


def _chain(kwargs: dict, first: Alternative, second: Alternative) -> list[ExecutionOrder]:
    one = orders_module.build_order(**{**kwargs, "alternative": first})
    two = orders_module.build_order(
        **{**kwargs, "routes_before": first.routes, "alternative": second}
    )
    return [_executed(one, 1), _executed(two, 2)]


def _held(orders: list[ExecutionOrder], truck_id: str) -> dict[str | None, list[str]]:
    return {d.voyage_id: d.lot_ids for d in executed_dispatches(orders) if d.truck_id == truck_id}


def test_same_trip_across_voyages_is_one_dispatch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, order = _captured(client, monkeypatch)
    rebuilt, marker = _split(kwargs, order, timedelta(0))
    truck_id, first, second, pallets = marker.split("|")
    lines = _dispatches(rebuilt, truck_id)
    original = _dispatches(order, truck_id)
    assert len(original) == 1
    assert len(lines) == 2
    assert lines[0].description.startswith(f"{truck_id} {first}·{second}: ")
    assert lines[1].description == f"{truck_id} {first} 배차 취소"
    assert lines[1].lot_ids == []
    assert f"{int(pallets) + 2}파렛트" in lines[0].description
    assert lines[0].lot_ids == [*original[0].lot_ids, "lot-split"]


def test_different_trips_stay_separate_dispatches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, order = _captured(client, monkeypatch)
    rebuilt, marker = _split(kwargs, order, timedelta(hours=3))
    truck_id, first, second, _ = marker.split("|")
    lines = _dispatches(rebuilt, truck_id)
    original = _dispatches(order, truck_id)
    assert len(lines) == 2
    assert lines[0].description == original[0].description
    assert lines[0].lot_ids == original[0].lot_ids
    assert lines[1].description.startswith(f"{truck_id} {second}: ")
    assert lines[1].lot_ids == ["lot-split"]
    assert first != second


def test_merged_trip_then_partial_change_drops_stale_lot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, order = _captured(client, monkeypatch)
    merged, marker = _extended(kwargs, order, timedelta(0))
    truck_id, first, second, _ = marker.split("|")
    orders = _chain(kwargs, merged, kwargs["alternative"])
    lines = _dispatches(orders[1], truck_id)
    assert [line.description for line in lines][-1] == f"{truck_id} {first}·{second} 배차 취소"
    assert lines[0].description.startswith(f"{truck_id} {first}: ")
    held = _held(orders, truck_id)
    assert f"{first}·{second}" not in held
    assert held[first] == _dispatches(order, truck_id)[0].lot_ids
    assert all("lot-split" not in lots for lots in held.values())


def test_merged_trip_then_full_cancel_drops_all_lots(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, order = _captured(client, monkeypatch)
    merged, marker = _extended(kwargs, order, timedelta(0))
    truck_id, first, second, _ = marker.split("|")
    emptied = merged.model_copy(
        update={
            "routes": [
                vp.model_copy(
                    update={
                        "pickup": vp.pickup.model_copy(
                            update={
                                "routes": [r for r in vp.pickup.routes if r.truck_id != truck_id]
                            }
                        )
                    }
                )
                for vp in merged.routes
            ]
        }
    )
    orders = _chain(kwargs, merged, emptied)
    lines = _dispatches(orders[1], truck_id)
    assert [line.description for line in lines] == [f"{truck_id} {first}·{second} 배차 취소"]
    assert _held(orders, truck_id) == {}


def test_per_voyage_trips_then_merged_drops_moved_lot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, order = _captured(client, monkeypatch)
    separate, marker = _extended(kwargs, order, timedelta(hours=3))
    truck_id, first, second, _ = marker.split("|")
    merged, _ = _extended(kwargs, order, timedelta(0), "lot-merged")
    orders = _chain(kwargs, separate, merged)
    lines = _dispatches(orders[1], truck_id)
    assert lines[0].description.startswith(f"{truck_id} {first}·{second}: ")
    assert [line.description for line in lines[1:]] == sorted(
        [f"{truck_id} {first} 배차 취소", f"{truck_id} {second} 배차 취소"]
    )
    held = _held(orders, truck_id)
    assert set(held) == {f"{first}·{second}"}
    assert held[f"{first}·{second}"] == [*_dispatches(order, truck_id)[0].lot_ids, "lot-merged"]
    assert all("lot-split" not in lots for lots in held.values())


def test_openapi_lists_optional_warnings_and_seed_counts() -> None:
    settings = get_settings().model_copy(
        update={"api_prefix": "/test/api", "session_cookie_path": "/test", "saas_mode": True}
    )
    schemas = create_app(settings).openapi()["components"]["schemas"]
    alternative = schemas["Alternative"]
    assert alternative["properties"]["warnings"]["items"] == {"type": "string"}
    assert "warnings" not in alternative["required"]
    drift = schemas["OrgDrift"]["properties"]
    assert drift["counts"]["properties"]["missing_seed_lots"] == {"type": "integer"}
    assert "missing_seed_lots" in drift["counts"]["required"]
    assert drift["baseline"]["properties"]["missing_seed_lots"] == {"type": "integer"}
    assert "missing_seed_lots" in drift["drift"]["properties"]
    assert "required" not in drift["drift"]
