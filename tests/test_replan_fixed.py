from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST, Scenario
from freshswitch_backend.optimize import AIR
from freshswitch_backend.planning.service import PlanWithRoutes, VoyagePickup, build_plan
from freshswitch_backend.replan import (
    AirSwitchEvent,
    CancelEvent,
    DelayEvent,
    Event,
    ExecutedDispatch,
    ReduceEvent,
    ReplanError,
    ReplanResult,
    event_time,
    executed_dispatches,
    locations,
    replan,
)
from freshswitch_backend.routing import (
    Depot,
    PickupRequest,
    PickupStop,
    PickupTruck,
    TruckRoute,
    solve_pickup,
)

MORNING_16 = datetime(2026, 11, 16, 6, 0, tzinfo=KST)
MORNING_15 = datetime(2026, 11, 15, 6, 0, tzinfo=KST)
DEPARTED_16 = {"lot-sangju-gr-1", "lot-pohang-gr-1", "lot-sancheong-sb-1", "lot-jinju-sb-1"}


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def built(scenario: Scenario) -> PlanWithRoutes:
    return build_plan(scenario)


def _run(scenario: Scenario, built: PlanWithRoutes, event, **kwargs) -> ReplanResult:
    return replan(scenario, built.plan, event, routes=built.routes, **kwargs)


def _trips(routes: list[VoyagePickup]) -> dict[tuple[str, str, int], TruckRoute]:
    return {
        (vp.voyage_id, route.truck_id, route.trip): route
        for vp in routes
        for route in vp.pickup.routes
    }


def _locked_keys(built: PlanWithRoutes, at: datetime) -> set[tuple[str, str, int]]:
    return {key for key, route in _trips(built.routes).items() if route.earliest_departure <= at}


def _assert_locked_kept(built: PlanWithRoutes, result: ReplanResult, at: datetime) -> None:
    before = _trips(built.routes)
    held = {f.lot_id for f in result.fixed_reasons}
    where = locations(built.plan)
    for alt in result.alternatives:
        after = _trips(alt.routes)
        for key in _locked_keys(built, at):
            assert after[key] == before[key], (alt.id, key)
        moved = locations(alt.plan)
        assert {lid: moved[lid] for lid in held} == {lid: where[lid] for lid in held}
        locked = {id(after[key]) for key in _locked_keys(built, at)}
        for route in after.values():
            if id(route) not in locked:
                assert route.earliest_departure >= at, (alt.id, route.truck_id)


def _change(alt, truck_id: str, voyage_id: str = "V-2611A"):
    return next(
        (c for c in alt.diff.route_changes if c.truck_id == truck_id and c.voyage_id == voyage_id),
        None,
    )


def test_cancel_keeps_departed_trucks_and_reports_route_change(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    result = _run(scenario, built, CancelEvent(lot_id="lot-miryang-sb-1", occurred_at=MORNING_16))
    assert result.occurred_at == MORNING_16
    assert {f.lot_id for f in result.fixed_reasons} == DEPARTED_16
    assert {f.reason_code for f in result.fixed_reasons} == {"truck_departed"}
    assert DEPARTED_16 <= set(result.fixed_lot_ids)
    assert not DEPARTED_16 & set(result.free_lot_ids)
    jinju = next(f for f in result.fixed_reasons if f.lot_id == "lot-jinju-sb-1")
    assert (jinju.truck_id, jinju.trip, jinju.packhouse_id) == ("RT-02", 1, "ph-jinju")
    assert "출발" in jinju.reason and "11-16 06:00" in jinju.reason
    _assert_locked_kept(built, result, MORNING_16)
    for alt in result.alternatives:
        assert alt.diff.route_changes
        assert _change(alt, "RT-02") is None
        gone = _change(alt, "RT-03")
        assert gone is not None and gone.change == "removed"
        assert gone.packhouses_removed == ["ph-miryang"]
        assert gone.lot_ids_before == ["lot-miryang-sb-1"]
        assert gone.departure_before == datetime(2026, 11, 16, 7, 26, tzinfo=KST)
        assert gone.departure_after is None
        assert gone.km_after == 0.0 and gone.cost_after == 0
        assert gone.km_before > 0 and gone.cost_before > 0


def test_event_on_departed_lot_is_rejected(scenario: Scenario, built: PlanWithRoutes) -> None:
    for event in (
        CancelEvent(lot_id="lot-jinju-sb-1", occurred_at=MORNING_16),
        AirSwitchEvent(lot_id="lot-sancheong-sb-1", occurred_at=MORNING_16),
        ReduceEvent(lot_id="lot-sangju-gr-1", new_pallets=10, occurred_at=MORNING_16),
    ):
        with pytest.raises(ReplanError, match="이미 출발했거나 배차가 실행된 트럭"):
            _run(scenario, built, event)


def test_air_switch_moves_only_undeparted_lot(scenario: Scenario, built: PlanWithRoutes) -> None:
    result = _run(
        scenario, built, AirSwitchEvent(lot_id="lot-miryang-sb-1", occurred_at=MORNING_16)
    )
    assert {f.lot_id for f in result.fixed_reasons} == DEPARTED_16
    _assert_locked_kept(built, result, MORNING_16)
    best = next(a for a in result.alternatives if a.id == result.recommended_id)
    assert locations(best.plan)["lot-miryang-sb-1"] == AIR
    gone = _change(best, "RT-03")
    assert gone is not None and gone.change == "removed"
    assert result.diff is not None and result.diff.route_changes == best.diff.route_changes


def test_delay_fixes_first_trip_and_redispatches_rest(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    event = DelayEvent(
        voyage_id="V-2611A",
        new_etd=datetime(2026, 11, 17, 6, 0, tzinfo=KST),
        occurred_at=MORNING_15,
    )
    result = _run(scenario, built, event)
    assert [f.lot_id for f in result.fixed_reasons] == ["lot-sangju-gr-1"]
    assert result.fixed_reasons[0].trip == 1
    assert "lot-pohang-gr-1" in result.free_lot_ids
    _assert_locked_kept(built, result, MORNING_15)
    for alt in result.alternatives:
        assert alt.diff.route_changes, alt.id
        assert _change(alt, "RT-02").change == "removed"
    nxt = next(a for a in result.alternatives if a.id == "next_voyage")
    rt01 = _change(nxt, "RT-01")
    assert rt01.packhouses_removed == ["ph-pohang"]
    assert rt01.departure_before == rt01.departure_after
    assert rt01.trips_before == 2 and rt01.trips_after == 1
    added = _change(nxt, "RT-01", "V-2611B")
    assert added.change == "added" and added.packhouses_added == ["ph-pohang"]


def test_executed_dispatch_fixes_route_before_departure(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    early = datetime(2026, 11, 10, 9, 0, tzinfo=KST)
    result = _run(
        scenario,
        built,
        CancelEvent(lot_id="lot-pohang-gr-1", occurred_at=early),
        dispatched=[ExecutedDispatch(truck_id="RT-03", lot_ids=["lot-miryang-sb-1"])],
    )
    assert [(f.lot_id, f.reason_code) for f in result.fixed_reasons] == [
        ("lot-miryang-sb-1", "dispatch_executed")
    ]
    assert "배차 실행" in result.fixed_reasons[0].reason
    before = _trips(built.routes)[("V-2611A", "RT-03", 1)]
    for alt in result.alternatives:
        assert _trips(alt.routes)[("V-2611A", "RT-03", 1)] == before
        assert _change(alt, "RT-03") is None


def _order(status: str, at: int, *actions) -> SimpleNamespace:
    stamp = datetime(2026, 11, 10, at, 0, tzinfo=KST)
    return SimpleNamespace(
        status=status, automatic=list(actions), updated_at=stamp, created_at=stamp
    )


def _dispatch(truck_id, voyage_id: str, lot_ids, kind: str = "truck_dispatch") -> SimpleNamespace:
    text = (
        f"{truck_id} {voyage_id}: 포항 → 부산신항"
        if lot_ids
        else f"{truck_id} {voyage_id} 배차 취소"
    )
    return SimpleNamespace(kind=kind, truck_id=truck_id, lot_ids=lot_ids, description=text)


def test_executed_dispatches_reads_only_executed_orders() -> None:
    orders = [
        _order(
            "executed",
            1,
            _dispatch("RT-01", "V-2611A", ["a"]),
            _dispatch(None, "V-2611A", ["b"], kind="slot_reassignment"),
        ),
        _order("proposed", 2, _dispatch("RT-03", "V-2611A", ["c"])),
    ]
    assert executed_dispatches(orders) == [
        ExecutedDispatch(voyage_id="V-2611A", truck_id="RT-01", lot_ids=["a"])
    ]


def test_executed_dispatches_follow_later_orders() -> None:
    orders = [
        _order("executed", 9, _dispatch("RT-01", "V-2611A", [])),
        _order(
            "executed",
            8,
            _dispatch("RT-01", "V-2611A", ["a"]),
            _dispatch("RT-01", "V-2611A", ["b"]),
            _dispatch("RT-02", "V-2611B", ["c"]),
        ),
        _order("executed", 10, _dispatch("RT-02", "V-2611B", ["d"])),
    ]
    assert executed_dispatches(orders) == [
        ExecutedDispatch(voyage_id="V-2611B", truck_id="RT-02", lot_ids=["d"])
    ]


def test_dispatch_lock_follows_its_truck_and_lot_across_voyages(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    early = datetime(2026, 11, 10, 9, 0, tzinfo=KST)
    other_truck = _run(
        scenario,
        built,
        CancelEvent(lot_id="lot-pohang-gr-1", occurred_at=early),
        dispatched=[
            ExecutedDispatch(voyage_id="V-2611A", truck_id="RT-02", lot_ids=["lot-miryang-sb-1"])
        ],
    )
    assert other_truck.fixed_reasons == []
    relabelled = _run(
        scenario,
        built,
        CancelEvent(lot_id="lot-pohang-gr-1", occurred_at=early),
        dispatched=[
            ExecutedDispatch(voyage_id="V-2611B", truck_id="RT-03", lot_ids=["lot-miryang-sb-1"])
        ],
    )
    assert [(f.lot_id, f.voyage_id, f.truck_id) for f in relabelled.fixed_reasons] == [
        ("lot-miryang-sb-1", "V-2611A", "RT-03")
    ]


def test_occurred_at_defaults_to_now(scenario: Scenario, built: PlanWithRoutes) -> None:
    event = CancelEvent(lot_id="lot-miryang-sb-1")
    assert event.occurred_at is None
    assert event_time(event) is None
    assert event_time(event, MORNING_16.astimezone(UTC)) == MORNING_16
    late = datetime(2026, 11, 17, 6, 0, tzinfo=KST)
    assert event_time(event.model_copy(update={"occurred_at": late}), MORNING_16) == late
    pinned = _run(scenario, built, event, now=MORNING_16.astimezone(UTC))
    assert pinned.occurred_at == MORNING_16
    assert pinned.occurred_at.utcoffset() == timedelta(hours=9)
    assert {f.lot_id for f in pinned.fixed_reasons} == DEPARTED_16


def test_no_event_time_locks_nothing(scenario: Scenario, built: PlanWithRoutes) -> None:
    event = CancelEvent(lot_id="lot-sancheong-sb-1")
    result = _run(scenario, built, event)
    assert result.occurred_at is None
    assert result.fixed_reasons == []
    assert "occurred_at" not in result.model_dump()
    best = next(a for a in result.alternatives if a.id == result.recommended_id)
    assert _change(best, "RT-02") is not None


def test_without_committed_routes_nothing_is_locked(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    result = replan(
        scenario,
        built.plan,
        CancelEvent(lot_id="lot-sancheong-sb-1", occurred_at=MORNING_16),
    )
    assert result.occurred_at == MORNING_16
    assert result.fixed_reasons == []
    assert all(alt.diff.route_changes == [] for alt in result.alternatives)


def test_occurred_at_parses_from_event_json() -> None:
    event = TypeAdapter(Event).validate_python(
        {"type": "cancel", "lot_id": "x", "occurred_at": "2026-11-15T21:00:00Z"}
    )
    assert event.occurred_at == MORNING_16
    assert event.occurred_at.utcoffset() == timedelta(hours=9)


def test_pickup_solver_keeps_locked_route_and_waits_for_truck() -> None:
    depot = Depot(id="P", lat=35.07, lon=128.82)
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    base = PickupRequest(
        depot=depot,
        stops=[
            PickupStop(id="a", packhouse_id="ph-a", lat=35.2, lon=128.6, pallets=4, ready_at=ready),
            PickupStop(id="b", packhouse_id="ph-b", lat=35.5, lon=128.7, pallets=4, ready_at=ready),
        ],
        trucks=[PickupTruck(id="T1", capacity_pallets=16)],
        deadline=datetime(2026, 11, 17, 18, 0, tzinfo=KST),
    )
    first = solve_pickup(base.model_copy(update={"stops": base.stops[:1]}))
    locked = first.routes[0]
    plan = solve_pickup(
        base.model_copy(update={"stops": base.stops[1:], "locked_routes": [locked]})
    )
    assert plan.status == "OK"
    assert plan.routes[0] == locked
    rest = plan.routes[1]
    assert rest.truck_id == "T1" and rest.trip == locked.trip + 1
    assert rest.earliest_departure >= locked.port_eta + timedelta(minutes=base.service_minutes)
    assert plan.total_km == pytest.approx(locked.km + rest.km, abs=0.2)
    assert any("고정" in line for line in plan.assumptions)


def test_pickup_solver_respects_not_before() -> None:
    depot = Depot(id="P", lat=35.07, lon=128.82)
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    request = PickupRequest(
        depot=depot,
        stops=[
            PickupStop(id="a", packhouse_id="ph-a", lat=35.2, lon=128.6, pallets=4, ready_at=ready)
        ],
        trucks=[PickupTruck(id="T1", capacity_pallets=16)],
        deadline=datetime(2026, 11, 17, 18, 0, tzinfo=KST),
    )
    free = solve_pickup(request)
    later = free.routes[0].earliest_departure + timedelta(hours=3)
    held = solve_pickup(request.model_copy(update={"not_before": later}))
    assert held.routes[0].earliest_departure >= later
    early = solve_pickup(request.model_copy(update={"not_before": ready - timedelta(days=3)}))
    assert early.routes == free.routes


def _single_stop_request() -> tuple[PickupRequest, TruckRoute]:
    depot = Depot(id="P", lat=35.07, lon=128.82)
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    base = PickupRequest(
        depot=depot,
        stops=[
            PickupStop(id="a", packhouse_id="ph-a", lat=35.2, lon=128.6, pallets=4, ready_at=ready),
            PickupStop(id="b", packhouse_id="ph-b", lat=35.5, lon=128.7, pallets=4, ready_at=ready),
        ],
        trucks=[PickupTruck(id="T1", capacity_pallets=16)],
        deadline=datetime(2026, 11, 17, 18, 0, tzinfo=KST),
    )
    first = solve_pickup(base.model_copy(update={"stops": base.stops[:1]}))
    return base.model_copy(update={"stops": base.stops[1:]}), first.routes[0]


def test_pickup_solver_numbers_trips_after_a_later_locked_trip() -> None:
    request, route = _single_stop_request()
    locked = route.model_copy(update={"trip": 2})
    plan = solve_pickup(request.model_copy(update={"locked_routes": [locked]}))
    trips = [(r.truck_id, r.trip) for r in plan.routes]
    assert len(trips) == len(set(trips)) == 2
    assert plan.routes[0] == locked
    assert plan.routes[1].trip == 3
    assert plan.routes[1].earliest_departure >= locked.port_eta


def test_pickup_solver_waits_for_truck_busy_on_another_voyage() -> None:
    request, route = _single_stop_request()
    free = solve_pickup(request)
    plan = solve_pickup(request.model_copy(update={"blocking_routes": [route]}))
    assert route not in plan.routes
    assert [r.truck_id for r in plan.routes] == ["T1"]
    assert plan.routes[0].trip == 1
    assert plan.routes[0].earliest_departure >= route.port_eta + timedelta(
        minutes=request.service_minutes
    )
    assert free.routes[0].earliest_departure < plan.routes[0].earliest_departure
