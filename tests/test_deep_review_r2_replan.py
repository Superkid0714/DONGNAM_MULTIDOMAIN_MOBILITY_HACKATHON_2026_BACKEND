from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.api import plans as plans_api
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST, Scenario
from freshswitch_backend.explain import render_template
from freshswitch_backend.optimize import AIR
from freshswitch_backend.planning.service import (
    PlanWithRoutes,
    allocate_route_costs,
    build_plan,
    build_routes,
    solve_with_routes,
)
from freshswitch_backend.replan import (
    DelayEvent,
    ExecutedDispatch,
    ReduceEvent,
    locations,
    replan,
)
from freshswitch_backend.replan.engine import DISPATCH_RELEASED
from freshswitch_backend.replan.explanation import build_explain_context
from freshswitch_backend.routing import (
    Depot,
    GateWindow,
    PickupRequest,
    PickupStop,
    PickupTruck,
    build_pickup_request,
    locked_routes,
    solve_pickup,
)
from freshswitch_backend.routing.scenario import route_lot_ids

EARLY = datetime(2026, 11, 10, 9, 0, tzinfo=KST)
STRAWBERRIES = ("lot-jinju-sb-1", "lot-sancheong-sb-1", "lot-miryang-sb-1")
GRAPES = ("lot-sangju-gr-1", "lot-pohang-gr-1")


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def built(scenario: Scenario) -> PlanWithRoutes:
    return build_plan(scenario)


def _rt01_pin() -> ExecutedDispatch:
    return ExecutedDispatch(voyage_id="V-2611A", truck_id="RT-01", lot_ids=list(GRAPES))


def _best(result):
    return next(a for a in result.alternatives if a.id == result.recommended_id)


def _route_of(alt, lot_id: str):
    return next(
        (vp.voyage_id, route)
        for vp in alt.routes
        for route in vp.pickup.routes
        if lot_id in route_lot_ids([route])
    )


def _with_late_grape(scenario: Scenario) -> Scenario:
    source = next(lot for lot in scenario.lots if lot.id == "lot-sangju-gr-1")
    late = source.model_copy(update={"id": "lot-sangju-gr-late", "ready_date": date(2026, 11, 19)})
    return scenario.model_copy(update={"lots": [*scenario.lots, late]})


def test_engine1_pin_on_later_voyage_does_not_block_earlier_voyage(scenario: Scenario) -> None:
    extended = _with_late_grape(scenario)
    base = build_plan(extended)
    late_voyage, late_route = next(
        (vp.voyage_id, r)
        for vp in base.routes
        for r in vp.pickup.routes
        if "lot-sangju-gr-late" in route_lot_ids([r])
    )
    assert (late_voyage, late_route.truck_id) == ("V-2611B", "RT-01")
    pin = ExecutedDispatch(voyage_id="V-2611B", truck_id="RT-01", lot_ids=["lot-sangju-gr-late"])
    event = ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3, occurred_at=EARLY)

    pinned = replan(extended, base.plan, event, routes=base.routes, dispatched=[pin])
    free = replan(extended, base.plan, event, routes=base.routes)

    best = _best(pinned)
    assert best.total_cost == _best(free).total_cost
    where = locations(best.plan)
    assert all(where[lid] != AIR for lid in (*STRAWBERRIES, *GRAPES))
    assert pinned.warnings == []
    with locked_routes({"V-2611B": [late_route]}):
        request = build_pickup_request(extended, ["lot-jinju-sb-1", *GRAPES], "V-2611A")
    plan = solve_pickup(request)
    assert plan.status == "OK"
    assert plan.dropped_stop_ids == []


def test_engine1_pinned_last_trip_at_cutoff_keeps_other_trucks(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    event = DelayEvent(
        voyage_id="V-2611A", new_etd=datetime(2026, 11, 16, 18, tzinfo=KST), occurred_at=EARLY
    )
    pinned = replan(scenario, built.plan, event, routes=built.routes, dispatched=[_rt01_pin()])
    free = replan(scenario, built.plan, event, routes=built.routes)

    best = _best(pinned)
    assert best.total_cost == _best(free).total_cost
    where = locations(best.plan)
    assert all(where[lid] != AIR for lid in STRAWBERRIES)
    assert {f.lot_id for f in pinned.fixed_reasons} == set(GRAPES)


def test_engine1_truck_free_only_after_horizon_is_left_out_of_the_model() -> None:
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    deadline = datetime(2026, 11, 17, 18, 0, tzinfo=KST)
    request = PickupRequest(
        depot=Depot(id="P", lat=35.07, lon=128.82),
        stops=[
            PickupStop(id="a", packhouse_id="ph-a", lat=35.2, lon=128.6, pallets=4, ready_at=ready)
        ],
        trucks=[
            PickupTruck(
                id="T1", capacity_pallets=16, available_from=deadline - timedelta(minutes=10)
            ),
            PickupTruck(id="T2", capacity_pallets=16),
        ],
        deadline=deadline,
    )
    plan = solve_pickup(request)
    assert plan.status == "OK"
    assert [r.truck_id for r in plan.routes] == ["T2"]


def test_engine1_truck_booked_across_the_cutoff_still_serves_earlier_stops() -> None:
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    deadline = datetime(2026, 11, 17, 18, 0, tzinfo=KST)
    request = PickupRequest(
        depot=Depot(id="P", lat=35.07, lon=128.82),
        stops=[
            PickupStop(id="a", packhouse_id="ph-a", lat=35.2, lon=128.6, pallets=4, ready_at=ready)
        ],
        trucks=[PickupTruck(id="T1", capacity_pallets=16)],
        deadline=deadline,
    )
    free = solve_pickup(request).routes[0]
    leave = deadline - timedelta(hours=2)
    later = free.model_copy(
        update={"earliest_departure": leave, "port_eta": deadline + timedelta(hours=4)}
    )
    plan = solve_pickup(request.model_copy(update={"blocking_routes": [later]}))
    assert plan.status == "OK"
    assert plan.routes[0].port_eta + timedelta(minutes=request.service_minutes) <= leave
    ignored = later.model_copy(
        update={"earliest_departure": deadline + timedelta(hours=1), "port_eta": deadline}
    )
    assert solve_pickup(request.model_copy(update={"blocking_routes": [ignored]})).routes == [free]


def test_engine2_lot_moving_voyage_keeps_pinned_truck_and_departure(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    before = {lid: _route_of_plan(built, lid) for lid in GRAPES}
    event = DelayEvent(
        voyage_id="V-2611A", new_etd=datetime(2026, 11, 22, 15, tzinfo=KST), occurred_at=EARLY
    )
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=[_rt01_pin()])

    best = _best(result)
    where = locations(best.plan)
    assert {where[lid] for lid in GRAPES} == {"CA-03"}
    for lid in GRAPES:
        voyage_id, route = _route_of(best, lid)
        assert voyage_id == "V-2611B"
        assert (route.truck_id, route.earliest_departure) == (
            before[lid].truck_id,
            before[lid].earliest_departure,
        )
    assert best.feasible
    reasons = {f.lot_id: f for f in result.fixed_reasons}
    assert set(reasons) == set(GRAPES)
    assert {f.voyage_id for f in reasons.values()} == {"V-2611B"}


def test_engine2_fixed_reasons_only_list_pins_the_recommendation_keeps(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    pin = ExecutedDispatch(voyage_id="V-2611A", truck_id="RT-03", lot_ids=["lot-miryang-sb-1"])
    event = ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3, occurred_at=EARLY)
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=[pin])

    best = _best(result)
    kept = locations(best.plan)["lot-miryang-sb-1"] != AIR
    assert {f.lot_id for f in result.fixed_reasons} == ({"lot-miryang-sb-1"} if kept else set())


def test_engine2_alternative_that_moves_a_pinned_lot_off_its_truck_is_flagged(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    from freshswitch_backend.replan import engine

    result = replan(
        scenario,
        built.plan,
        ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3, occurred_at=EARLY),
        routes=built.routes,
        dispatched=[_rt01_pin()],
    )
    best = _best(result)
    moved = [
        vp.model_copy(
            update={
                "pickup": vp.pickup.model_copy(
                    update={
                        "routes": [
                            r.model_copy(
                                update={
                                    "earliest_departure": r.earliest_departure + timedelta(hours=1)
                                }
                            )
                            for r in vp.pickup.routes
                        ]
                    }
                )
            }
        )
        for vp in best.routes
    ]
    ctx = engine._Context(
        scenario,
        built.plan,
        result.event,
        set(),
        built.routes,
        set(),
        pins={f.lot_id: f for f in result.fixed_reasons},
    )
    assert engine._broken_pins(ctx, best.plan, best.routes) == []
    broken = engine._broken_pins(ctx, best.plan, moved)
    assert broken == [
        f"{lid}: 배차가 실행된 트럭 RT-01 {trip}회차의 트럭·출발 시각을 유지할 수 없음"
        for lid, trip in sorted({"lot-sangju-gr-1": 1, "lot-pohang-gr-1": 2}.items())
    ]


def _route_of_plan(built: PlanWithRoutes, lot_id: str):
    return next(
        route
        for vp in built.routes
        for route in vp.pickup.routes
        if lot_id in route_lot_ids([route])
    )


RT02_PIN = ExecutedDispatch(
    voyage_id="V-2611A", truck_id="RT-02", lot_ids=["lot-sancheong-sb-1", "lot-jinju-sb-1"]
)
RELEASING = DelayEvent(
    voyage_id="V-2611A", new_etd=datetime(2026, 11, 17, 0, tzinfo=KST), occurred_at=EARLY
)


def test_engine3_released_pins_reach_explanation_and_alternatives(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    result = replan(scenario, built.plan, RELEASING, routes=built.routes, dispatched=[RT02_PIN])
    assert result.warnings == [DISPATCH_RELEASED]

    summary = render_template(build_explain_context(scenario, built.plan, result)).operator_summary
    assert f"주의: {DISPATCH_RELEASED}." in summary
    best = _best(result)
    assert best.warnings == [DISPATCH_RELEASED]
    assert best.model_dump(mode="json")["warnings"] == [DISPATCH_RELEASED]


def test_engine3_no_warning_keeps_payload_shape(scenario: Scenario, built: PlanWithRoutes) -> None:
    result = replan(
        scenario,
        built.plan,
        ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3, occurred_at=EARLY),
        routes=built.routes,
    )
    assert all("warnings" not in a.model_dump(mode="json") for a in result.alternatives)
    summary = render_template(build_explain_context(scenario, built.plan, result)).operator_summary
    assert "주의:" not in summary


def test_engine3_post_event_response_carries_released_warning(
    client: TestClient, monkeypatch
) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    monkeypatch.setattr(plans_api, "executed_dispatches", lambda orders: [RT02_PIN])
    response = client.post(
        f"/api/plans/{plan_id}/events?llm=false",
        json={"event": RELEASING.model_dump(mode="json")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["warnings"] == [DISPATCH_RELEASED]
    assert f"주의: {DISPATCH_RELEASED}." in body["explanation"]["operator_summary"]
    best = next(a for a in body["alternatives"] if a["id"] == body["recommended_id"])
    assert best["warnings"] == [DISPATCH_RELEASED]


def test_engine7_next_voyage_names_container_booked_by_other_plan(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    result = replan(
        scenario,
        built.plan,
        ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3),
        routes=built.routes,
        unavailable_containers={"CA-03"},
    )
    nxt = next(a for a in result.alternatives if a.id == "next_voyage")
    assert not nxt.feasible
    assert "lot-jinju-sb-1: V-2611B/CA-03 다른 계획에 배정됨" in nxt.infeasible_reasons
    assert not any("이후 항차 없음" in r for r in nxt.infeasible_reasons)
    summary = render_template(build_explain_context(scenario, built.plan, result)).operator_summary
    assert "이후 항차 없음" not in summary
    assert "V-2611B/CA-03 다른 계획에 배정됨" in summary


def test_engine2_pin_carried_to_new_voyage_survives_next_replan(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    delay = DelayEvent(
        voyage_id="V-2611A", new_etd=datetime(2026, 11, 22, 15, tzinfo=KST), occurred_at=EARLY
    )
    first = replan(scenario, built.plan, delay, routes=built.routes, dispatched=[_rt01_pin()])
    applied = _best(first)
    trips = {lid: _route_of(applied, lid) for lid in GRAPES}
    assert {voyage_id for voyage_id, _ in trips.values()} == {"V-2611B"}

    second = replan(
        first.scenario_after,
        applied.plan,
        ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3, occurred_at=EARLY),
        routes=applied.routes,
        dispatched=[_rt01_pin()],
    )
    assert {f.lot_id for f in second.fixed_reasons} == set(GRAPES)
    for alternative in second.alternatives:
        where = locations(alternative.plan)
        for lid in GRAPES:
            if where[lid] == AIR:
                continue
            _, route = _route_of(alternative, lid)
            assert (route.truck_id, route.earliest_departure) == (
                trips[lid][1].truck_id,
                trips[lid][1].earliest_departure,
            )


def test_engine2_pinned_trip_without_riding_lots_still_blocks_other_voyages(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    pinned = next(
        r for vp in built.routes for r in vp.pickup.routes if (r.truck_id, r.trip) == ("RT-01", 1)
    )
    source = next(lot for lot in scenario.lots if lot.id == "lot-sangju-gr-1")
    other = source.model_copy(update={"id": "lot-b", "ready_date": date(2026, 11, 15)})
    extended = scenario.model_copy(update={"lots": [*scenario.lots, other]})
    with locked_routes({"V-2611A": [pinned]}):
        request = build_pickup_request(extended, ["lot-b"], "V-2611B", placed={"lot-b": "V-2611B"})
    assert [(r.truck_id, r.earliest_departure) for r in request.blocking_routes] == [
        (pinned.truck_id, pinned.earliest_departure)
    ]
    plan = solve_pickup(request)
    assert plan.status == "OK"
    for route in plan.routes:
        if route.truck_id == pinned.truck_id:
            assert (
                route.earliest_departure >= pinned.port_eta
                or route.port_eta <= pinned.earliest_departure
            )


def _two_trucks(ready: datetime, deadline: datetime, **options) -> PickupRequest:
    return PickupRequest(
        depot=Depot(id="P", lat=35.07, lon=128.82),
        stops=[
            PickupStop(id="a", packhouse_id="ph-a", lat=35.2, lon=128.6, pallets=4, ready_at=ready)
        ],
        trucks=[
            PickupTruck(id="T1", capacity_pallets=16),
            PickupTruck(id="T2", capacity_pallets=16),
        ],
        deadline=deadline,
        **options,
    )


def test_engine1_truck_booked_from_before_t0_leaves_other_trucks_serving() -> None:
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    deadline = datetime(2026, 11, 17, 18, 0, tzinfo=KST)
    request = _two_trucks(ready, deadline)
    crossing = (
        solve_pickup(request)
        .routes[0]
        .model_copy(
            update={
                "truck_id": "T1",
                "earliest_departure": ready - timedelta(hours=10),
                "port_eta": deadline + timedelta(hours=2),
            }
        )
    )
    plan = solve_pickup(request.model_copy(update={"blocking_routes": [crossing]}))
    assert plan.status == "OK"
    assert [r.truck_id for r in plan.routes] == ["T2"]


def test_engine1_truck_window_inside_gate_peak_keeps_avoid_mode() -> None:
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    deadline = datetime(2026, 11, 17, 18, 0, tzinfo=KST)
    peaks = [
        GateWindow(start=time(7, 0), end=time(10, 0), label="am"),
        GateWindow(start=time(15, 0), end=time(18, 0), label="pm"),
    ]
    request = _two_trucks(ready, deadline, gate_windows=peaks)
    free = solve_pickup(request)
    assert free.gate_mode == "avoid"
    crossing = free.routes[0].model_copy(
        update={
            "truck_id": "T1",
            "earliest_departure": datetime(2026, 11, 16, 9, 0, tzinfo=KST),
            "port_eta": deadline + timedelta(hours=2),
        }
    )
    plan = solve_pickup(request.model_copy(update={"blocking_routes": [crossing]}))
    assert plan.status == "OK"
    assert plan.gate_mode == "avoid"
    assert [(r.truck_id, r.port_eta) for r in plan.routes] == [("T2", free.routes[0].port_eta)]


def test_engine2_pinned_trip_split_across_voyages_is_billed_once(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    pinned = next(r for vp in built.routes for r in vp.pickup.routes if r.truck_id == "RT-02")
    assert route_lot_ids([pinned]) == {"lot-jinju-sb-1", "lot-sancheong-sb-1"}
    plan = built.plan.model_copy(
        update={
            "assignments": [
                a.model_copy(update={"container_id": "CA-03", "voyage_id": "V-2611B"})
                if a.lot_id == "lot-jinju-sb-1"
                else a
                for a in built.plan.assignments
            ]
        }
    )
    with locked_routes({"V-2611A": [pinned]}):
        routes = build_routes(scenario, plan)
    copies = [
        (vp.voyage_id, r)
        for vp in routes
        for r in vp.pickup.routes
        if (r.truck_id, r.earliest_departure) == (pinned.truck_id, pinned.earliest_departure)
    ]
    assert {voyage_id for voyage_id, _ in copies} == {"V-2611A", "V-2611B"}
    assert sum(r.cost for _, r in copies) == pytest.approx(pinned.cost, abs=1)
    assert sum(r.km for _, r in copies) == pytest.approx(pinned.km, abs=0.2)
    charged = {
        c.lot_id: c.route
        for c in allocate_route_costs(scenario, plan, routes)
        if c.lot_id in route_lot_ids([pinned])
    }
    assert sum(charged.values()) == pytest.approx(round(pinned.cost), abs=1)


def test_engine2_renumbered_carried_pin_reason_matches_its_trip(scenario: Scenario) -> None:
    from freshswitch_backend.replan import engine

    extended = _with_late_grape(scenario)
    base = build_plan(extended)
    pins = [
        _rt01_pin(),
        ExecutedDispatch(voyage_id="V-2611B", truck_id="RT-01", lot_ids=["lot-sangju-gr-late"]),
    ]
    held, reasons = engine.fixed_routes(base.routes, None, pins)
    smaller = extended.model_copy(
        update={
            "lots": [
                lot.model_copy(update={"pallets": 4}) if lot.id == "lot-sangju-gr-late" else lot
                for lot in extended.lots
            ]
        }
    )
    with locked_routes(held):
        moved = solve_with_routes(
            smaller, fixed={"lot-sangju-gr-1": "CA-03", "lot-sangju-gr-late": "CA-03"}
        )
    ctx = engine._Context(
        smaller,
        base.plan,
        ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3),
        set(),
        base.routes,
        set(),
        pins={f.lot_id: f for f in reasons},
    )
    kept = engine._kept_pins(ctx, moved.plan, moved.routes)
    assert {"lot-sangju-gr-1", "lot-sangju-gr-late"} <= set(kept)
    for pin in kept.values():
        assert pin.reason.startswith(f"{pin.truck_id} {pin.trip}회차 ")
    rt01 = sorted(
        (pin.departure, pin.trip)
        for pin in kept.values()
        if (pin.voyage_id, pin.truck_id) == ("V-2611B", "RT-01")
    )
    assert [trip for _, trip in rt01] == list(range(1, len(rt01) + 1))
