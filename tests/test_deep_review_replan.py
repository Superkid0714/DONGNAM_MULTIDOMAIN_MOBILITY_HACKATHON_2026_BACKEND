from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from test_plan_confirm import _plan

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST, Scenario
from freshswitch_backend.execution.cancellation import annotate_cancellations
from freshswitch_backend.explain import render_template
from freshswitch_backend.explain.validation import shipper_error
from freshswitch_backend.optimize import AIR
from freshswitch_backend.planning.service import PlanWithRoutes, build_plan
from freshswitch_backend.planning.store import store
from freshswitch_backend.replan import (
    AddLotEvent,
    AirSwitchEvent,
    CancelEvent,
    DelayEvent,
    ExecutedDispatch,
    ReduceEvent,
    ReplanError,
    apply_event,
    engine,
    executed_dispatches,
    locations,
    replan,
)
from freshswitch_backend.replan.engine import DISPATCH_RELEASED, DISPATCH_REUSED
from freshswitch_backend.replan.explanation import build_explain_context, event_summary
from freshswitch_backend.replan.models import Alternative
from freshswitch_backend.routing.scenario import route_lot_ids

EARLY = datetime(2026, 11, 10, 9, 0, tzinfo=KST)
TYPHOON = DelayEvent(voyage_id="V-2611A", new_etd=datetime(2026, 11, 22, 15, 0, tzinfo=KST))
REDUCE = {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def built(scenario: Scenario) -> PlanWithRoutes:
    return build_plan(scenario)


def _sea(alt: Alternative) -> set[str]:
    return {a.container_id for a in alt.plan.assignments if a.mode == "sea"}


def test_replan1_booked_container_is_excluded_before_solving(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    result = replan(
        scenario, built.plan, TYPHOON, routes=built.routes, unavailable_containers={"CA-03"}
    )
    assert result.recommended_id == "pull_forward"
    for alt in result.alternatives:
        assert "CA-03" not in _sea(alt), alt.id
    best = next(a for a in result.alternatives if a.id == "pull_forward")
    assert best.feasible
    assert locations(best.plan)["lot-sangju-gr-1"] == "CA-01"


def test_replan1_reason_keeps_text_unless_prefix_is_lot(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    result = replan(scenario, built.plan, TYPHOON, routes=built.routes)
    alt = result.alternatives[0].model_copy(
        update={
            "feasible": False,
            "infeasible_reasons": [
                "다른 계획에 이미 배정된 컨테이너를 씁니다: CA-03",
                "lot-jinju-sb-1: 도착일이 판매기한 초과",
            ],
        }
    )
    result = result.model_copy(update={"alternatives": [alt], "recommended_id": None})
    summary = render_template(build_explain_context(scenario, built.plan, result))
    assert "다른 계획에 이미 배정된 컨테이너를 씁니다: CA-03; 도착일이 판매기한 초과" in (
        summary.operator_summary
    )


def test_replan1_post_event_passes_booked_containers(client: TestClient, monkeypatch) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    monkeypatch.setattr(store, "booked_containers", lambda *args: {"CA-03"})
    response = client.post(
        f"/api/plans/{plan_id}/events?llm=false",
        json={"event": TYPHOON.model_dump(mode="json")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recommended_id"] == "pull_forward"
    for alt in body["alternatives"]:
        used = {a["container_id"] for a in alt["plan"]["assignments"] if a["mode"] == "sea"}
        assert "CA-03" not in used


def test_replan2_recovery_after_delay_keeps_cutoff(scenario: Scenario) -> None:
    delayed = apply_event(
        scenario, DelayEvent(voyage_id="V-2611A", new_etd=datetime(2026, 11, 20, 18, tzinfo=KST))
    )
    recovered = apply_event(
        delayed, DelayEvent(voyage_id="V-2611A", new_etd=datetime(2026, 11, 19, 6, tzinfo=KST))
    )
    voyage = next(v for v in recovered.voyages if v.id == "V-2611A")
    assert voyage.cy_cutoff == datetime(2026, 11, 17, 18, tzinfo=KST)
    assert voyage.original_etd == datetime(2026, 11, 18, 18, tzinfo=KST)
    advanced = apply_event(
        delayed, DelayEvent(voyage_id="V-2611A", new_etd=datetime(2026, 11, 18, 6, tzinfo=KST))
    )
    voyage = next(v for v in advanced.voyages if v.id == "V-2611A")
    assert voyage.cy_cutoff == datetime(2026, 11, 17, 6, tzinfo=KST)
    back = apply_event(
        advanced, DelayEvent(voyage_id="V-2611A", new_etd=datetime(2026, 11, 19, 18, tzinfo=KST))
    )
    voyage = next(v for v in back.voyages if v.id == "V-2611A")
    assert voyage.cy_cutoff == datetime(2026, 11, 17, 18, tzinfo=KST)


def test_replan2_original_cutoff_is_not_serialized_until_set(scenario: Scenario) -> None:
    assert "original_cy_cutoff" not in scenario.voyages[0].model_dump(mode="json")


def test_replan5_shipper_notice_includes_cancellation_fee(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    result = replan(scenario, built.plan, CancelEvent(lot_id="lot-pohang-gr-1"))
    annotate_cancellations(result, scenario, built.plan)
    ctx = build_explain_context(scenario, built.plan, result)
    messages = {m.shipper_id: m.message for m in render_template(ctx).shipper_messages}
    assert "취소 부담금 2,516,667원을 별도로 부담합니다." in messages["sh-pohang"]
    assert "취소 보전금 2,516,667원을 받습니다." in messages["sh-sangju"]
    assert "실제 부담 변화는 없습니다" in messages["sh-sangju"]
    for impact in ctx.shippers:
        assert shipper_error(ctx, impact, messages[impact.shipper_id]) is None


def test_replan5_adjustment_only_shipper_gets_notice(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    result = replan(scenario, built.plan, CancelEvent(lot_id="lot-pohang-gr-1"))
    annotate_cancellations(result, scenario, built.plan)
    best = next(a for a in result.alternatives if a.id == result.recommended_id)
    sangju = next(d for d in best.diff.shippers if d.shipper_id == "sh-sangju")
    sangju.cost_delta = 0
    sangju.cost_after = sangju.cost_before
    ctx = build_explain_context(scenario, built.plan, result)
    assert "sh-sangju" in {s.shipper_id for s in ctx.shippers}


def test_replan6_logged_in_platform_cannot_answer_requests(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(platform_client, saas_login)["plan_id"]
    response = platform_client.post(
        f"/api/plans/{plan_id}/orders/any/requests/REQ-1", json={"accepted": True}
    )
    assert response.status_code == 403


def _execute(client: TestClient, plan_id: str) -> dict:
    client.post(f"/api/plans/{plan_id}/events?llm=false", json={"event": REDUCE})
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["active_order_id"]
    base = f"/api/plans/{plan_id}/orders/{order_id}"
    order = client.get(base).json()
    for c in order["shipper_confirmations"]:
        client.post(
            f"{base}/confirm",
            json={"actor": "shipper", "shipper_id": c["shipper_id"], "accept": True},
        )
    client.post(f"{base}/confirm", json={"actor": "forwarder", "accept": True})
    for request in order["requests"]:
        answered = client.post(f"{base}/requests/{request['request_id']}", json={"accepted": True})
        assert answered.status_code == 200, answered.text
    order = client.get(base).json()
    assert order["status"] == "executed"
    return order


def test_replan7_post_event_pins_executed_dispatch(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _execute(client, plan_id)
    dispatched = executed_dispatches(store.orders(plan_id))
    assert dispatched
    target = next(d for d in dispatched if len(d.lot_ids) > 1)
    cancelled, kept = target.lot_ids[0], target.lot_ids[1]
    response = client.post(
        f"/api/plans/{plan_id}/events?llm=false",
        json={"event": {"type": "cancel", "lot_id": cancelled}},
    )
    assert response.status_code == 200, response.text
    reasons = {(f["lot_id"], f["reason_code"]) for f in response.json()["fixed_reasons"]}
    assert (kept, "dispatch_executed") in reasons
    switched = client.post(
        f"/api/plans/{plan_id}/events?llm=false",
        json={"event": {"type": "air_switch", "lot_id": kept}},
    )
    assert switched.status_code == 200, switched.text


def test_replan7_dispatch_lock_keeps_truck_but_allows_air(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    dispatched = [
        ExecutedDispatch(voyage_id="V-2611A", truck_id="RT-03", lot_ids=["lot-miryang-sb-1"])
    ]
    result = replan(
        scenario,
        built.plan,
        AirSwitchEvent(lot_id="lot-miryang-sb-1", occurred_at=EARLY),
        routes=built.routes,
        dispatched=dispatched,
    )
    best = next(a for a in result.alternatives if a.id == result.recommended_id)
    assert locations(best.plan)["lot-miryang-sb-1"] == "AIR"
    assert result.warnings == []


def test_replan7_releases_dispatch_when_infeasible(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    dispatched = [
        ExecutedDispatch(
            voyage_id="V-2611A", truck_id="RT-02", lot_ids=["lot-sancheong-sb-1", "lot-jinju-sb-1"]
        )
    ]
    event = DelayEvent(
        voyage_id="V-2611A", new_etd=datetime(2026, 11, 17, 0, tzinfo=KST), occurred_at=EARLY
    )
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=dispatched)
    assert result.recommended_id is not None
    assert result.warnings == [DISPATCH_RELEASED]
    assert result.fixed_reasons == []
    pinned = replan(
        scenario,
        built.plan,
        event.model_copy(update={"new_etd": datetime(2026, 11, 17, 12, tzinfo=KST)}),
        routes=built.routes,
        dispatched=dispatched,
    )
    assert pinned.warnings == []
    assert {f.reason_code for f in pinned.fixed_reasons} == {"dispatch_executed"}


def test_replan7_late_pinned_truck_is_infeasible(scenario: Scenario, built: PlanWithRoutes) -> None:
    dispatched = [
        ExecutedDispatch(voyage_id="V-2611A", truck_id="RT-03", lot_ids=["lot-miryang-sb-1"])
    ]
    event = DelayEvent(
        voyage_id="V-2611A", new_etd=datetime(2026, 11, 17, 0, tzinfo=KST), occurred_at=EARLY
    )
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=dispatched)
    as_is = next(a for a in result.alternatives if a.id == "sail_as_is")
    assert not as_is.feasible
    assert any("선적 마감 뒤에 항만에 도착" in r for r in as_is.infeasible_reasons)


def _truck(routes, truck_id: str, lot_id: str):
    return next(
        route
        for vp in routes
        if vp.voyage_id == "V-2611A"
        for route in vp.pickup.routes
        if route.truck_id == truck_id and any(lot_id in s.lot_ids for s in route.stops)
    )


def test_replan7_pinned_route_follows_reduced_pallets(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    before = _truck(built.routes, "RT-02", "lot-sancheong-sb-1")
    dispatched = [
        ExecutedDispatch(
            voyage_id="V-2611A", truck_id="RT-02", lot_ids=["lot-sancheong-sb-1", "lot-jinju-sb-1"]
        )
    ]
    event = ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3, occurred_at=EARLY)
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=dispatched)
    assert result.recommended_id is not None
    assert result.warnings == []
    lots = {lot.id: lot for lot in apply_event(scenario, event).lots}
    for alt in result.alternatives:
        if alt.id not in {result.recommended_id, "sail_as_is"}:
            continue
        route = _truck(alt.routes, "RT-02", "lot-sancheong-sb-1")
        assert (route.trip, route.earliest_departure) == (before.trip, before.earliest_departure)
        for stop in route.stops:
            assert stop.pallets == sum(lots[lid].pallets for lid in stop.lot_ids), alt.id
        assert route.load_pallets == sum(s.pallets for s in route.stops), alt.id
        assert route.stops[-1].load_after == route.load_pallets, alt.id
        assert route.load_pallets == before.load_pallets - 2, alt.id


def test_replan7_cancel_keeps_other_lot_on_dispatched_truck(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    before = _truck(built.routes, "RT-02", "lot-jinju-sb-1")
    dispatched = [
        ExecutedDispatch(
            voyage_id="V-2611A", truck_id="RT-02", lot_ids=["lot-sancheong-sb-1", "lot-jinju-sb-1"]
        )
    ]
    event = CancelEvent(lot_id="lot-sancheong-sb-1", occurred_at=EARLY)
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=dispatched)
    assert result.recommended_id is not None
    assert result.warnings == []
    lots = {lot.id: lot for lot in scenario.lots}
    for alt_id in {result.recommended_id, "sail_as_is"}:
        alt = next(a for a in result.alternatives if a.id == alt_id)
        route = _truck(alt.routes, "RT-02", "lot-jinju-sb-1")
        assert (route.trip, route.earliest_departure) == (before.trip, before.earliest_departure)
        ids = route_lot_ids([route])
        assert "lot-sancheong-sb-1" not in ids
        for stop in route.stops:
            assert stop.pallets == sum(lots[lid].pallets for lid in stop.lot_ids), alt_id


def test_replan7_skips_release_without_time(
    scenario: Scenario, built: PlanWithRoutes, monkeypatch
) -> None:
    dispatched = [
        ExecutedDispatch(
            voyage_id="V-2611A", truck_id="RT-02", lot_ids=["lot-sancheong-sb-1", "lot-jinju-sb-1"]
        )
    ]
    event = DelayEvent(
        voyage_id="V-2611A", new_etd=datetime(2026, 11, 17, 0, tzinfo=KST), occurred_at=EARLY
    )
    monkeypatch.setattr(engine, "_has_time", lambda deadline, needed: False)
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=dispatched)
    assert result.recommended_id is None
    assert result.warnings == []
    assert {f.reason_code for f in result.fixed_reasons} == {"dispatch_executed"}


def _trip_lots(
    alt: Alternative, truck_id: str, trip: int, voyage_id: str = "V-2611A"
) -> set[str] | None:
    return next(
        (
            route_lot_ids([route])
            for vp in alt.routes
            if vp.voyage_id == voyage_id
            for route in vp.pickup.routes
            if (route.truck_id, route.trip) == (truck_id, trip)
        ),
        None,
    )


def _pin(built: PlanWithRoutes, truck_id: str) -> ExecutedDispatch:
    route = next(
        route
        for vp in built.routes
        if vp.voyage_id == "V-2611A"
        for route in vp.pickup.routes
        if (route.truck_id, route.trip) == (truck_id, 1)
    )
    return ExecutedDispatch(
        voyage_id="V-2611A", truck_id=truck_id, lot_ids=sorted(route_lot_ids([route]))
    )


def _assert_pins_kept(result, pins: list[ExecutedDispatch]) -> None:
    for alt in result.alternatives:
        where = locations(alt.plan)
        for pin in pins:
            carried = _trip_lots(alt, pin.truck_id, 1, pin.voyage_id)
            if all(where.get(lid) in (None, AIR) for lid in pin.lot_ids):
                assert carried is None or carried <= set(pin.lot_ids), (alt.id, carried)
                continue
            assert carried is not None, (alt.id, pin.truck_id)
            assert carried <= set(pin.lot_ids), (alt.id, pin.truck_id, carried)


def test_replan7_next_voyage_keeps_executed_dispatch(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    pin = _pin(built, "RT-02")
    assert set(pin.lot_ids) == {"lot-sancheong-sb-1", "lot-jinju-sb-1"}
    event = ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3, occurred_at=EARLY)
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=[pin])
    _assert_pins_kept(result, [pin])
    nxt = next(a for a in result.alternatives if a.id == "next_voyage")
    assert _trip_lots(nxt, "RT-02", 1) == set(pin.lot_ids) or (
        not nxt.feasible and DISPATCH_REUSED in nxt.infeasible_reasons
    )
    assert "lot-pohang-gr-1" not in (_trip_lots(nxt, "RT-02", 1) or set())


def test_replan7_next_voyage_infeasible_when_only_pinned_lots_would_move(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    pins = [_pin(built, "RT-02"), _pin(built, "RT-03")]
    event = ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3, occurred_at=EARLY)
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=pins)
    _assert_pins_kept(result, pins)
    nxt = next(a for a in result.alternatives if a.id == "next_voyage")
    assert not nxt.feasible
    assert nxt.infeasible_reasons == [DISPATCH_REUSED]
    assert result.recommended_id is not None
    assert result.recommended_id != "next_voyage"
    assert result.warnings == []


def test_replan7_air_switch_does_not_reuse_dispatched_trip(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    pin = _pin(built, "RT-03")
    event = AirSwitchEvent(lot_id="lot-miryang-sb-1", occurred_at=EARLY)
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=[pin])
    assert result.recommended_id is not None
    _assert_pins_kept(result, [pin])


def test_replan7_release_keeps_pinned_next_voyage(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    pin = _pin(built, "RT-02")
    event = DelayEvent(
        voyage_id="V-2611A", new_etd=datetime(2026, 11, 17, 0, tzinfo=KST), occurred_at=EARLY
    )
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=[pin])
    assert result.warnings == [DISPATCH_RELEASED]
    assert result.recommended_id != "next_voyage"
    nxt = next(a for a in result.alternatives if a.id == "next_voyage")
    assert not nxt.feasible
    assert DISPATCH_REUSED in nxt.infeasible_reasons
    assert _trip_lots(nxt, "RT-02", 1) == set(pin.lot_ids)
    where = locations(nxt.plan)
    assert where["lot-jinju-sb-1"] == where["lot-sancheong-sb-1"] == "CA-01"


def test_replan7_next_voyage_delay_cannot_keep_pinned_lots_past_sell_by(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    pins = [_pin(built, "RT-02"), _pin(built, "RT-03")]
    event = TYPHOON.model_copy(update={"occurred_at": EARLY})
    result = replan(scenario, built.plan, event, routes=built.routes, dispatched=pins)
    _assert_pins_kept(result, pins)
    nxt = next(a for a in result.alternatives if a.id == "next_voyage")
    assert not nxt.feasible
    assert DISPATCH_REUSED in nxt.infeasible_reasons
    assert "lot-jinju-sb-1: 도착일이 판매기한 초과" in nxt.infeasible_reasons
    assert result.recommended_id == "pull_forward"


def test_replan10_cutoff_wording_follows_applied_cutoff(scenario: Scenario) -> None:
    advance = DelayEvent(voyage_id="V-2611A", new_etd=datetime(2026, 11, 18, 6, tzinfo=KST))
    assert "CY 마감 11/17 18:00→11/17 06:00 변경" in event_summary(scenario, advance)
    explicit = DelayEvent(
        voyage_id="V-2611A",
        new_etd=datetime(2026, 11, 19, 18, tzinfo=KST),
        new_cy_cutoff=datetime(2026, 11, 18, 18, tzinfo=KST),
    )
    assert "CY 마감 11/17 18:00→11/18 18:00 변경" in event_summary(scenario, explicit)
    delay = DelayEvent(voyage_id="V-2611A", new_etd=datetime(2026, 11, 20, 18, tzinfo=KST))
    assert "CY 마감 11/17 18:00 유지" in event_summary(scenario, delay)


def test_replan11_delay_past_next_voyage_still_considers_it(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    result = replan(scenario, built.plan, TYPHOON, routes=built.routes)
    nxt = next(a for a in result.alternatives if a.id == "next_voyage")
    assert not any("이후 항차 없음" in r for r in nxt.infeasible_reasons)
    assert any("V-2611B/CA-03" in r for r in nxt.infeasible_reasons)
    assert "V-2611B" in {
        a.voyage_id for a in nxt.plan.assignments if a.mode == "sea" and a.voyage_id
    }


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"destination": "XXNOPE"}, "항구를 찾을 수 없음"),
        ({"pallets": 60}, "용량"),
        ({"sell_by_date": date(2026, 11, 1)}, "판매기한"),
    ],
)
def test_replan13_add_lot_is_validated(scenario: Scenario, updates, message) -> None:
    template = next(lot for lot in scenario.lots if lot.id == "lot-pohang-gr-1")
    lot = template.model_copy(update={"id": "lot-new-1", **updates})
    with pytest.raises(ReplanError, match=message):
        apply_event(scenario, AddLotEvent(lot=lot))


def test_replan13_add_lot_api_rejects_unknown_port(client: TestClient, scenario: Scenario) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    template = next(lot for lot in scenario.lots if lot.id == "lot-pohang-gr-1")
    lot = template.model_copy(update={"id": "lot-new-1", "destination": "XXNOPE"})
    response = client.post(
        f"/api/plans/{plan_id}/events?llm=false",
        json={"event": {"type": "add_lot", "lot": lot.model_dump(mode="json")}},
    )
    assert response.status_code == 422
