from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from freshswitch_backend.domain.models import (
    CollectionReassignment,
    Lot,
    LotCollection,
    Plan,
    Scenario,
)
from freshswitch_backend.optimize import AIR, solve_plan
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.solver import resettle_plan
from freshswitch_backend.planning.budget import remaining
from freshswitch_backend.routing import (
    PickupPlan,
    PickupRequest,
    build_pickup_request,
    haversine_km,
    solve_pickup,
)

ROUTE_TIME_LIMIT_S = 1.0
MAX_ROUTE_ROUNDS = 3
CUTOFF_REASON = "집하 불가(컷오프)"


class VoyagePickup(BaseModel):
    voyage_id: str
    lot_ids: list[str]
    pickup: PickupPlan
    dropped_lot_ids: list[str] = Field(default_factory=list)
    dropped_stops: dict[str, str] = Field(default_factory=dict)


class PlanWithRoutes(BaseModel):
    plan: Plan
    routes: list[VoyagePickup]


def _effective_scenario(scenario: Scenario, plan: Plan) -> Scenario:
    pulls = {a.lot_id: a.pull_forward_days for a in plan.assignments if a.pull_forward_days}
    return scenario.model_copy(
        update={
            "lots": [
                lot.model_copy(
                    update={"ready_date": lot.ready_date - timedelta(days=pulls[lot.id])}
                )
                if lot.id in pulls
                else lot
                for lot in scenario.lots
            ]
        }
    )


def _pickup_request(scenario: Scenario, lot_ids: list[str], voyage_id: str) -> PickupRequest:
    p = scenario.cost_params
    return build_pickup_request(
        scenario,
        lot_ids,
        voyage_id,
        cost_per_km=p.reefer_truck_cost_per_km_krw.value,
        road_factor=p.road_distance_factor.value,
        truck_fixed_cost=p.reefer_truck_fixed_per_route_krw.value,
    )


def build_routes(
    scenario: Scenario,
    plan: Plan,
    time_limit_s: float = ROUTE_TIME_LIMIT_S,
    *,
    deadline: float | None = None,
) -> list[VoyagePickup]:
    effective = _effective_scenario(scenario, plan)
    by_voyage: dict[str, list[str]] = defaultdict(list)
    for a in plan.assignments:
        if a.mode == "sea" and a.voyage_id:
            by_voyage[a.voyage_id].append(a.lot_id)
    order = {v.id: i for i, v in enumerate(scenario.voyages)}
    cutoff = {v.id: v.cy_cutoff for v in scenario.voyages}
    busy_until: dict[str, datetime] = {}
    solved: dict[str, VoyagePickup] = {}
    for voyage_id, lot_ids in sorted(
        by_voyage.items(), key=lambda kv: (cutoff[kv[0]], order[kv[0]])
    ):
        request = _pickup_request(effective, lot_ids, voyage_id)
        request = request.model_copy(
            update={
                "trucks": [
                    t.model_copy(update={"available_from": busy_until.get(t.id)})
                    for t in request.trucks
                ]
            }
        )
        pickup = solve_pickup(request, time_limit_s=remaining(deadline, time_limit_s))
        remaining(deadline, time_limit_s)
        for route in pickup.routes:
            free = route.port_eta + timedelta(minutes=request.service_minutes)
            busy_until[route.truck_id] = max(free, busy_until.get(route.truck_id, free))
        dropped = set(pickup.dropped_stop_ids)
        dropped_stops = {
            lid: stop.id for stop in request.stops if stop.id in dropped for lid in stop.lot_ids
        }
        solved[voyage_id] = VoyagePickup(
            voyage_id=voyage_id,
            lot_ids=lot_ids,
            pickup=pickup,
            dropped_lot_ids=list(dropped_stops),
            dropped_stops=dropped_stops,
        )
    return [solved[v] for v in sorted(solved, key=order.__getitem__)]


def _largest_remainder(total: int, weights: list[float]) -> list[int]:
    if not weights:
        return []
    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [1.0] * len(weights)
        weight_sum = float(len(weights))
    raw = [total * w / weight_sum for w in weights]
    floors = [int(r // 1) for r in raw]
    rest = total - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[:rest]:
        floors[i] += 1
    return floors


def allocate_route_costs(
    scenario: Scenario, plan: Plan, routes: list[VoyagePickup]
) -> list[LotCollection]:
    model = CostModel(scenario)
    lots: dict[str, Lot] = {lot.id: lot for lot in scenario.lots}
    voyages = {v.id: v for v in scenario.voyages}
    ports = {p.id: p for p in scenario.ports}
    packhouses = {p.id: p for p in scenario.packhouses}
    road = scenario.cost_params.road_distance_factor.value
    assignment = {a.lot_id: a for a in plan.assignments}
    result: list[LotCollection] = []
    for voyage_pickup in routes:
        voyage = voyages[voyage_pickup.voyage_id]
        port = ports[voyage.origin]
        for route in voyage_pickup.pickup.routes:
            members: list[tuple[Lot, str, float]] = []
            for stop in route.stops:
                ph = packhouses[stop.packhouse_id]
                depot_km = haversine_km(port.lat, port.lon, ph.lat, ph.lon) * road
                for lid in stop.lot_ids:
                    members.append((lots[lid], stop.stop_id, depot_km))
            shares = _largest_remainder(
                round(route.cost), [lot.pallets * km for lot, _, km in members]
            )
            for (lot, stop_id, _), share in zip(members, shares, strict=True):
                pulls = assignment[lot.id].pull_forward_days if lot.id in assignment else 0
                estimate = model.sea_lot_cost(lot, voyage, pulls).collection
                result.append(
                    LotCollection(
                        lot_id=lot.id,
                        shipper_id=lot.shipper_id,
                        voyage_id=voyage.id,
                        truck_id=route.truck_id,
                        stop_id=stop_id,
                        pallets=lot.pallets,
                        estimate=round(estimate),
                        route=share,
                        trip=route.trip,
                    )
                )
    return result


def with_routes(
    scenario: Scenario, plan: Plan, routes: list[VoyagePickup] | None = None, **updates
) -> PlanWithRoutes:
    if routes is None:
        routes = build_routes(scenario, plan)
    collections = allocate_route_costs(scenario, plan, routes)
    route_collection = {c.lot_id: c.route for c in collections}
    resettled = resettle_plan(
        scenario, plan, route_collection, lot_collections=collections, **updates
    )
    return PlanWithRoutes(plan=resettled, routes=routes)


def _location(plan: Plan, lot_id: str) -> str:
    for a in plan.assignments:
        if a.lot_id == lot_id:
            return a.container_id if a.mode == "sea" else AIR
    return "UNASSIGNED"


def _dropped(routes: list[VoyagePickup]) -> dict[str, tuple[str, str]]:
    return {
        lid: (vp.voyage_id, stop_id) for vp in routes for lid, stop_id in vp.dropped_stops.items()
    }


def solve_with_routes(
    scenario: Scenario,
    *,
    fixed: Mapping[str, str] | None = None,
    forbidden: Iterable[tuple[str, str]] | None = None,
    max_rounds: int = MAX_ROUTE_ROUNDS,
    deadline: float | None = None,
    **options,
) -> PlanWithRoutes:
    fixed = dict(fixed or {})
    forbidden = list(forbidden or ())
    reasons: dict[tuple[str, str], str] = {}
    dropped_at: dict[str, tuple[str, str]] = {}
    containers_of: dict[str, list[str]] = defaultdict(list)
    for c in scenario.containers:
        containers_of[c.voyage_id].append(c.id)
    for round_no in range(1, max_rounds + 1):
        solve_options = {
            **options,
            "time_limit_s": remaining(deadline, options.get("time_limit_s", 5.0)),
        }
        plan = solve_plan(
            scenario, fixed=fixed, forbidden=forbidden, forbidden_reasons=reasons, **solve_options
        )
        routes = build_routes(scenario, plan, deadline=deadline)
        newly = _dropped(routes)
        if not newly or round_no == max_rounds:
            break
        for lid, (voyage_id, stop_id) in newly.items():
            dropped_at.setdefault(lid, (voyage_id, stop_id))
            fixed.pop(lid, None)
            for cid in containers_of[voyage_id]:
                forbidden.append((lid, cid))
                reasons[(lid, cid)] = CUTOFF_REASON
    still = _dropped(routes)
    if still:
        kept = {
            a.lot_id: a.container_id if a.mode == "sea" else AIR
            for a in plan.assignments
            if a.lot_id not in still
        }
        for lid, (voyage_id, stop_id) in still.items():
            dropped_at.setdefault(lid, (voyage_id, stop_id))
            for c in scenario.containers:
                forbidden.append((lid, c.id))
                reasons[(lid, c.id)] = CUTOFF_REASON
        plan = solve_plan(
            scenario,
            fixed=kept,
            forbidden=forbidden,
            forbidden_reasons=reasons,
            **{**options, "time_limit_s": remaining(deadline, options.get("time_limit_s", 5.0))},
        )
        routes = build_routes(scenario, plan, deadline=deadline)
    lots = {lot.id: lot for lot in scenario.lots}
    reassigned = [
        CollectionReassignment(
            lot_id=lid,
            shipper_id=lots[lid].shipper_id,
            voyage_id=voyage_id,
            stop_id=stop_id,
            reason=f"{CUTOFF_REASON}: 재계산 {max_rounds}회 뒤에도 집하 경로 없음"
            if lid in still
            else CUTOFF_REASON,
            location_after=_location(plan, lid),
        )
        for lid, (voyage_id, stop_id) in {**dropped_at, **still}.items()
    ]
    unassigned = [
        u.model_copy(update={"reason": f"{CUTOFF_REASON}; {u.reason}"})
        if u.lot_id in dropped_at and CUTOFF_REASON not in u.reason
        else u
        for u in plan.unassigned
    ]
    return with_routes(
        scenario, plan, routes, collection_reassigned=reassigned, unassigned=unassigned
    )


def build_plan(scenario: Scenario, **options) -> PlanWithRoutes:
    return solve_with_routes(scenario, **options)
