import re
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from freshswitch_backend.domain.models import KST, Plan, Scenario
from freshswitch_backend.optimize import AIR
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.feasibility import air_rejection, sea_option
from freshswitch_backend.planning.budget import PlanningTimeout
from freshswitch_backend.planning.budget import remaining as remaining_budget
from freshswitch_backend.planning.service import (
    PlanWithRoutes,
    VoyagePickup,
    solve_with_routes,
)
from freshswitch_backend.replan.models import (
    AddLotEvent,
    AirSwitchEvent,
    Alternative,
    AlternativeId,
    CancelEvent,
    ContainerFill,
    DelayEvent,
    Event,
    ExecutedDispatch,
    FixedLot,
    IncreaseEvent,
    LotMove,
    PlanDiff,
    ReduceEvent,
    ReplanError,
    ReplanResult,
    RouteChange,
    ShipperDelta,
    VoyageDelay,
)
from freshswitch_backend.routing import TruckRoute, locked_routes, route_lot_ids

if TYPE_CHECKING:
    from freshswitch_backend.execution.models import ExecutionOrder

UNASSIGNED = "UNASSIGNED"
DISPATCH_RELEASED = "실행된 배차를 고정하면 실행 가능한 대안이 없어 고정을 풀고 계산했습니다"
DISPATCH_REUSED = "이미 출발한 배차를 다른 화물에 쓸 수 없음"
MAX_PULL_FORWARD_DAYS = 2
SOLVED = ("OPTIMAL", "FEASIBLE")
DELAY_MAX_ADVANCE = timedelta(days=7)
DELAY_MAX_POSTPONE = timedelta(days=30)
LABELS: dict[AlternativeId, str] = {
    "pull_forward": "당겨 채우기",
    "next_voyage": "다음 배 이월",
    "sail_as_is": "그대로 출항",
}
PREFERENCE: dict[AlternativeId, int] = {"pull_forward": 0, "sail_as_is": 1, "next_voyage": 2}
LockedRoutes = dict[str, list[TruckRoute]]


def event_time(event: Event, now: datetime | None = None) -> datetime | None:
    at = event.occurred_at or now
    return at.astimezone(KST) if at is not None else None


def _dispatch_voyage(action) -> str | None:
    found = re.match(rf"{re.escape(action.truck_id)} (\S+?):?(?:\s|$)", action.description)
    return found.group(1) if found else None


def executed_dispatches(orders: Iterable["ExecutionOrder"]) -> list[ExecutedDispatch]:
    current: dict[tuple[str | None, str], list[str]] = {}
    executed = [o for o in orders if o.status == "executed"]
    for order in sorted(executed, key=lambda o: (o.updated_at, o.created_at)):
        touched: dict[tuple[str | None, str], list[str]] = {}
        for action in order.automatic:
            if action.kind != "truck_dispatch" or not action.truck_id:
                continue
            key = (_dispatch_voyage(action), action.truck_id)
            touched.setdefault(key, []).extend(action.lot_ids)
        current.update(touched)
    return [
        ExecutedDispatch(voyage_id=voyage_id, truck_id=truck_id, lot_ids=lot_ids)
        for (voyage_id, truck_id), lot_ids in current.items()
        if lot_ids
    ]


def _clock(at: datetime) -> str:
    return f"{at.astimezone(KST):%m-%d %H:%M}"


def _dispatch_reason(truck_id: str, trip: int) -> str:
    return f"{truck_id} {trip}회차 배차 실행됨: 트럭·출발 시각 고정(해상·항공 선택은 변경 가능)"


def fixed_routes(
    routes: list[VoyagePickup],
    at: datetime | None,
    dispatched: Iterable[ExecutedDispatch] = (),
) -> tuple[LockedRoutes, list[FixedLot]]:
    executed = {(d.truck_id, lid) for d in dispatched for lid in d.lot_ids}
    held: LockedRoutes = {}
    reasons: list[FixedLot] = []
    for voyage_pickup in routes:
        for route in voyage_pickup.pickup.routes:
            departed = at is not None and route.earliest_departure <= at
            ordered = any((route.truck_id, lid) in executed for lid in route_lot_ids([route]))
            if not departed and not ordered:
                continue
            held.setdefault(voyage_pickup.voyage_id, []).append(route)
            trip = f"{route.truck_id} {route.trip}회차"
            if departed and at is not None:
                code = "truck_departed"
                why = (
                    f"{trip} {_clock(route.earliest_departure)} 출발(이벤트 {_clock(at)} 기준)"
                    ": 경로·적재 고정"
                )
            else:
                code = "dispatch_executed"
                why = _dispatch_reason(route.truck_id, route.trip)
            reasons.extend(
                FixedLot(
                    lot_id=lid,
                    reason_code=code,
                    reason=why,
                    voyage_id=voyage_pickup.voyage_id,
                    truck_id=route.truck_id,
                    trip=route.trip,
                    packhouse_id=stop.packhouse_id,
                    departure=route.earliest_departure,
                )
                for stop in route.stops
                for lid in stop.lot_ids
            )
    return held, reasons


def _truck_trips(routes: list[VoyagePickup]) -> dict[tuple[str, str], list[TruckRoute]]:
    trips: dict[tuple[str, str], list[TruckRoute]] = {}
    for voyage_pickup in routes:
        for route in voyage_pickup.pickup.routes:
            trips.setdefault((voyage_pickup.voyage_id, route.truck_id), []).append(route)
    return {key: sorted(value, key=lambda r: r.trip) for key, value in trips.items()}


def _route_signature(trips: list[TruckRoute]) -> tuple:
    return tuple(
        (
            tuple(tuple(stop.lot_ids) for stop in route.stops),
            route.earliest_departure,
            route.km,
            round(route.cost),
        )
        for route in trips
    )


def _visited(trips: list[TruckRoute]) -> list[str]:
    return list(dict.fromkeys(stop.packhouse_id for route in trips for stop in route.stops))


def route_changes(
    scenario: Scenario, before: list[VoyagePickup], after: list[VoyagePickup]
) -> list[RouteChange]:
    old, new = _truck_trips(before), _truck_trips(after)
    order = {v.id: i for i, v in enumerate(scenario.voyages)}
    changes = []
    for key in sorted(set(old) | set(new), key=lambda k: (order.get(k[0], len(order)), k)):
        was, now = old.get(key, []), new.get(key, [])
        if _route_signature(was) == _route_signature(now):
            continue
        kind = "changed"
        if not was:
            kind = "added"
        elif not now:
            kind = "removed"
        visited_before, visited_after = _visited(was), _visited(now)
        changes.append(
            RouteChange(
                voyage_id=key[0],
                truck_id=key[1],
                change=kind,
                packhouses_before=visited_before,
                packhouses_after=visited_after,
                packhouses_added=[p for p in visited_after if p not in visited_before],
                packhouses_removed=[p for p in visited_before if p not in visited_after],
                lot_ids_before=sorted(route_lot_ids(was)),
                lot_ids_after=sorted(route_lot_ids(now)),
                trips_before=len(was),
                trips_after=len(now),
                departure_before=was[0].earliest_departure if was else None,
                departure_after=now[0].earliest_departure if now else None,
                km_before=round(sum(r.km for r in was), 1),
                km_after=round(sum(r.km for r in now), 1),
                cost_before=round(sum(r.cost for r in was)),
                cost_after=round(sum(r.cost for r in now)),
            )
        )
    return changes


def _check_shift(label: str, value: datetime, base: datetime, voyage_id: str) -> None:
    if not base - DELAY_MAX_ADVANCE <= value <= base + DELAY_MAX_POSTPONE:
        raise ReplanError(
            f"{voyage_id} {label}은 원래 일정({base.astimezone(KST):%Y-%m-%d %H:%M}) 기준 "
            f"{DELAY_MAX_ADVANCE.days}일 앞당김부터 {DELAY_MAX_POSTPONE.days}일 지연까지만 받습니다"
        )


def _delay_voyage(scenario: Scenario, delay: VoyageDelay) -> Scenario:
    voyage_id, new_etd, new_cutoff = delay.voyage_id, delay.new_etd, delay.new_cy_cutoff
    voyage = next((v for v in scenario.voyages if v.id == voyage_id), None)
    if voyage is None:
        raise ReplanError(f"항차를 찾을 수 없음: {voyage_id}")
    base_etd = voyage.original_etd or voyage.etd
    base_cutoff = voyage.original_cy_cutoff or voyage.cy_cutoff
    if voyage.original_etd is not None and voyage.original_cy_cutoff is None:
        base_cutoff = voyage.cy_cutoff + max(base_etd - voyage.etd, timedelta(0))
    _check_shift("출항 시각", new_etd, base_etd, voyage_id)
    if new_cutoff is not None:
        _check_shift("선적 마감", new_cutoff, base_cutoff, voyage_id)
    cutoff = new_cutoff or voyage.cy_cutoff
    if new_cutoff is None and new_etd < base_etd:
        cutoff = base_cutoff + (new_etd - base_etd)
    elif new_cutoff is None and voyage.etd < base_etd:
        cutoff = base_cutoff
    if cutoff > new_etd:
        raise ReplanError("선적 마감이 출항 시각보다 늦을 수 없음")
    original_cutoff = voyage.original_cy_cutoff
    if original_cutoff is None:
        original_cutoff = voyage.cy_cutoff if voyage.original_etd is None else base_cutoff
    changed = voyage.model_copy(
        update={
            "etd": new_etd,
            "cy_cutoff": cutoff,
            "original_etd": base_etd,
            "original_cy_cutoff": original_cutoff,
        }
    )
    return scenario.model_copy(
        update={"voyages": [changed if v.id == voyage_id else v for v in scenario.voyages]}
    )


def apply_event(scenario: Scenario, event: Event) -> Scenario:
    lots = {lot.id: lot for lot in scenario.lots}
    lot_id = getattr(event, "lot_id", None)
    if lot_id is not None and lot_id not in lots:
        raise ReplanError(f"로트를 찾을 수 없음: {lot_id}")
    match event:
        case CancelEvent(lot_id=lot_id):
            return scenario.model_copy(
                update={"lots": [lot for lot in scenario.lots if lot.id != lot_id]}
            )
        case ReduceEvent(lot_id=lot_id, new_pallets=new_pallets):
            if isinstance(event, IncreaseEvent) and new_pallets <= lots[lot_id].pallets:
                raise ReplanError(
                    f"증량 후 파렛트({new_pallets})가 현재({lots[lot_id].pallets})보다 커야 함"
                )
            if not isinstance(event, IncreaseEvent) and new_pallets >= lots[lot_id].pallets:
                raise ReplanError(
                    f"감량 후 파렛트({new_pallets})가 현재({lots[lot_id].pallets})보다 작아야 함"
                )
            return scenario.model_copy(
                update={
                    "lots": [
                        lot.model_copy(update={"pallets": new_pallets}) if lot.id == lot_id else lot
                        for lot in scenario.lots
                    ]
                }
            )
        case AirSwitchEvent(lot_id=lot_id):
            model = CostModel(scenario)
            reason = air_rejection(model, lots[lot_id])
            if reason:
                raise ReplanError(f"항공 전환 불가: {lot_id} {reason}")
            if model.tariff(lots[lot_id]) is None:
                return scenario
            return scenario.model_copy(
                update={
                    "lots": [
                        lot.model_copy(update={"sell_window": "short"}) if lot.id == lot_id else lot
                        for lot in scenario.lots
                    ]
                }
            )
        case DelayEvent():
            for delay in event.delays():
                scenario = _delay_voyage(scenario, delay)
            return scenario
        case AddLotEvent(lot=lot):
            if lot.id in lots:
                raise ReplanError(f"이미 있는 로트 ID: {lot.id}")
            if lot.shipper_id not in {s.id for s in scenario.shippers}:
                raise ReplanError(f"화주를 찾을 수 없음: {lot.shipper_id}")
            if lot.packhouse_id is not None and lot.packhouse_id not in {
                p.id for p in scenario.packhouses
            }:
                raise ReplanError(f"출하 장소를 찾을 수 없음: {lot.packhouse_id}")
            if lot.product_id not in {p.id for p in scenario.products}:
                raise ReplanError(f"품목을 찾을 수 없음: {lot.product_id}")
            if lot.destination not in {p.id for p in scenario.ports}:
                raise ReplanError(f"항구를 찾을 수 없음: {lot.destination}")
            capacity = max((c.capacity_pallets for c in scenario.containers), default=20)
            if lot.pallets > capacity:
                raise ReplanError(f"컨테이너 1대 용량({capacity}파렛트)을 넘음: {lot.id}")
            if lot.sell_by_date < lot.ready_date:
                raise ReplanError(f"판매기한이 출하 가능일보다 빠름: {lot.id}")
            return scenario.model_copy(update={"lots": [*scenario.lots, lot]})
    raise ReplanError(f"알 수 없는 이벤트: {event}")


def locks_air(scenario: Scenario, event: Event) -> bool:
    if not isinstance(event, AirSwitchEvent):
        return False
    lot = next((x for x in scenario.lots if x.id == event.lot_id), None)
    return lot is not None and CostModel(scenario).tariff(lot) is None


def plan_score(plan: Plan) -> int:
    return plan.total_operational_cost


def locations(plan: Plan) -> dict[str, str]:
    where = {a.lot_id: a.container_id if a.mode == "sea" else AIR for a in plan.assignments}
    where.update({u.lot_id: UNASSIGNED for u in plan.unassigned})
    return where


def container_shares(plan: Plan) -> dict[str, tuple[float, int]]:
    loads = {c.container_id: c for c in plan.containers}
    shares: dict[str, tuple[float, int]] = {}
    for a in plan.assignments:
        load = loads.get(a.container_id) if a.mode == "sea" else None
        if load and load.pallets:
            total, pallets = shares.get(a.shipper_id, (0.0, 0))
            share = load.container_cost * a.pallets / load.pallets
            shares[a.shipper_id] = (total + share, pallets + a.pallets)
    return shares


def _per_pallet(entry: tuple[float, int] | None) -> int:
    return round(entry[0] / entry[1]) if entry else 0


def _pulls(plan: Plan) -> dict[str, int]:
    return {a.lot_id: a.pull_forward_days for a in plan.assignments}


class _Context:
    def __init__(
        self,
        after: Scenario,
        current: Plan,
        event: Event,
        air_locked: set[str],
        routes_before: list[VoyagePickup] | None,
        held: set[str],
        before: Scenario | None = None,
        unavailable: Iterable[str] = (),
        pinned: Iterable[tuple[str, datetime]] = (),
        dispatched_lots: Iterable[str] = (),
        pins: dict[str, FixedLot] | None = None,
    ) -> None:
        self.after = after
        self.current = current
        self.event = event
        self.routes_before = routes_before
        self.model = CostModel(after)
        self.where = locations(current)
        self.lot_ids = [lot.id for lot in after.lots]
        self.voyage_of = {c.id: c.voyage_id for c in after.containers}
        self.etd = {v.id: v.etd for v in after.voyages}
        self.etd_before = {v.id: v.etd for v in (before or after).voyages}
        self.unavailable = set(unavailable) - set(self.where.values())
        self.pinned = set(pinned)
        self.dispatched_lots = set(dispatched_lots)
        self.pin_of = {lid: pin for lid, pin in (pins or {}).items() if lid in self.lot_ids}
        self.air_locked = set(air_locked) & set(self.lot_ids)
        self.held = held & set(self.lot_ids)
        self.rule_switched: set[str] = set()
        if locks_air(after, event):
            self.air_locked.add(event.lot_id)
        elif isinstance(event, AirSwitchEvent):
            self.rule_switched.add(event.lot_id)
        self.touched_lots = self._touched_lots()
        self.touched_containers = self._touched_containers()

    def _touched_lots(self) -> set[str]:
        match self.event:
            case (
                CancelEvent(lot_id=lot_id)
                | ReduceEvent(lot_id=lot_id)
                | AirSwitchEvent(lot_id=lot_id)
            ):
                return {lot_id}
            case AddLotEvent(lot=lot):
                return {lot.id}
            case DelayEvent():
                voyages = set(self.event.voyage_ids)
                return {
                    lid for lid, loc in self.where.items() if self.voyage_of.get(loc) in voyages
                }
        return set()

    def _touched_containers(self) -> set[str]:
        if isinstance(self.event, DelayEvent):
            voyages = set(self.event.voyage_ids)
            return {c.id for c in self.after.containers if c.voyage_id in voyages}
        return {
            self.where[lid] for lid in self.touched_lots if self.where.get(lid) in self.voyage_of
        }

    def scope_voyages(self) -> set[str]:
        return {self.voyage_of[c] for c in self.touched_containers}

    def free_lots(self, scope: str) -> set[str]:
        containers = set(self.touched_containers)
        if scope == "voyage":
            voyages = self.scope_voyages()
            containers |= {c for c, v in self.voyage_of.items() if v in voyages}
        free = set()
        for lid in self.lot_ids:
            loc = self.where.get(lid)
            if lid in self.touched_lots or loc in containers or loc in (AIR, UNASSIGNED, None):
                free.add(lid)
        return free - self.air_locked - self.held

    def fixed_for(self, free: set[str]) -> dict[str, str]:
        fixed = {
            lid: self.where[lid]
            for lid in self.lot_ids
            if lid not in free and self.where.get(lid) not in (None, UNASSIGNED)
        }
        fixed.update({lid: AIR for lid in self.air_locked})
        return fixed

    def booked_pairs(self) -> list[tuple[str, str]]:
        return [(lid, cid) for lid in self.lot_ids for cid in sorted(self.unavailable)]

    def new_unassigned(self, plan: Plan) -> list[str]:
        return [u.lot_id for u in plan.unassigned if self.where.get(u.lot_id) != UNASSIGNED]


def _timed(fn: Callable[[], PlanWithRoutes]) -> tuple[PlanWithRoutes, int]:
    started = time.perf_counter()
    built = fn()
    return built, round((time.perf_counter() - started) * 1000)


def diff_plans(
    scenario: Scenario,
    before: Plan,
    after: Plan,
    *,
    routes_before: list[VoyagePickup] | None = None,
    routes_after: list[VoyagePickup] | None = None,
) -> PlanDiff:
    where_before, where_after = locations(before), locations(after)
    pulls_before, pulls_after = _pulls(before), _pulls(after)
    lots = {lot.id: lot for lot in scenario.lots}
    moved = []
    for lot_id, loc in where_after.items():
        prev = where_before.get(lot_id)
        pull = pulls_after.get(lot_id, 0)
        if prev == loc and pulls_before.get(lot_id, 0) == pull:
            continue
        lot = lots[lot_id]
        moved.append(
            LotMove(
                lot_id=lot_id,
                shipper_id=lot.shipper_id,
                pallets=lot.pallets,
                from_location=prev,
                to_location=loc,
                pull_forward_days=pull,
            )
        )
    gone = {
        item.lot_id: item
        for item in [*before.assignments, *before.unassigned]
        if item.lot_id not in where_after
    }
    for lot_id, item in gone.items():
        moved.append(
            LotMove(
                lot_id=lot_id,
                shipper_id=item.shipper_id,
                pallets=item.pallets,
                from_location=where_before[lot_id],
                to_location=None,
            )
        )
    cost_before = {c.shipper_id: c for c in before.shipper_costs}
    cost_after = {c.shipper_id: c for c in after.shipper_costs}
    share_before, share_after = container_shares(before), container_shares(after)
    shippers = []
    for shipper in scenario.shippers:
        b, a = cost_before.get(shipper.id), cost_after.get(shipper.id)
        if b is None and a is None:
            continue
        sb, sa = share_before.get(shipper.id), share_after.get(shipper.id)
        tb, ta = _per_pallet(sb), _per_pallet(sa)
        cb = b.plan.total if b else 0
        ca = a.plan.total if a else 0
        shippers.append(
            ShipperDelta(
                shipper_id=shipper.id,
                cost_before=cb,
                cost_after=ca,
                cost_delta=ca - cb,
                container_share_before=round(sb[0]) if sb else 0,
                container_share_after=round(sa[0]) if sa else 0,
                share_per_pallet_before=tb,
                share_per_pallet_after=ta,
                share_per_pallet_delta_pct=round((ta - tb) / tb * 100, 1) if tb and ta else None,
            )
        )
    load_before = {c.container_id: c for c in before.containers}
    containers = []
    for load in after.containers:
        prev = load_before.get(load.container_id)
        containers.append(
            ContainerFill(
                container_id=load.container_id,
                voyage_id=load.voyage_id,
                capacity_pallets=load.capacity_pallets,
                pallets_before=prev.pallets if prev else 0,
                pallets_after=load.pallets,
                utilization_before=prev.utilization if prev else 0.0,
                utilization_after=load.utilization,
            )
        )
    changes = (
        route_changes(scenario, routes_before, routes_after)
        if routes_before is not None and routes_after is not None
        else []
    )
    return PlanDiff(
        moved_lots=moved, shippers=shippers, containers=containers, route_changes=changes
    )


def _place(loc: str | None) -> str:
    return {None: "없음", AIR: "항공", UNASSIGNED: "미배정"}.get(loc, loc)


def _describe(ctx: _Context, diff: PlanDiff) -> str:
    moves = [
        f"{m.lot_id} {m.pallets}파렛트 {_place(m.from_location)}→{_place(m.to_location)}"
        + (f"({m.pull_forward_days}일 당김)" if m.pull_forward_days else "")
        for m in diff.moved_lots
    ]
    fills = [
        f"{c.container_id} {c.pallets_after}/{c.capacity_pallets}"
        for c in diff.containers
        if c.pallets_after
        and (c.container_id in ctx.touched_containers or c.pallets_before != c.pallets_after)
    ]
    parts = []
    if moves:
        parts.append("이동: " + ", ".join(moves))
    if fills:
        parts.append("적재: " + ", ".join(fills))
    return " / ".join(parts) or "변경 없음"


def _pulled(ctx: _Context, plan: Plan) -> bool:
    before = _pulls(ctx.current)
    return any(
        a.pull_forward_days and before.get(a.lot_id, 0) != a.pull_forward_days
        for a in plan.assignments
    )


def _moved_for_next_voyage(ctx: _Context) -> tuple[dict[str, str], set[str], set[str]]:
    if isinstance(ctx.event, AddLotEvent):
        lot = ctx.event.lot
        candidates = [
            c
            for c in ctx.after.containers
            if c.id not in ctx.unavailable and sea_option(ctx.model, lot, c, 0)[0] is not None
        ]
        if not candidates:
            return {}, set(), set()
        first = min(candidates, key=lambda c: ctx.etd[c.voyage_id])
        return {lot.id: first.voyage_id}, {first.voyage_id}, set()
    moved = {
        lid: ctx.voyage_of[ctx.where[lid]]
        for lid in ctx.lot_ids
        if ctx.where.get(lid) in ctx.touched_containers
        and lid not in ctx.air_locked
        and lid not in ctx.rule_switched
        and lid not in ctx.held
    }
    origin = ctx.voyage_of.get(ctx.where.get(getattr(ctx.event, "lot_id", None)))
    if isinstance(ctx.event, ReduceEvent) and origin is not None:
        moved.setdefault(ctx.event.lot_id, origin)
    kept = set(moved) & ctx.dispatched_lots
    free = {lid: voyage for lid, voyage in moved.items() if lid not in kept}
    return free, ctx.scope_voyages(), kept


def _next_voyage_blocked(ctx: _Context, moved: dict[str, str]) -> dict[str, set[str]]:
    etd = ctx.etd_before if isinstance(ctx.event, DelayEvent) else ctx.etd
    by_origin = {
        origin: {
            c.id
            for c in ctx.after.containers
            if c.voyage_id == origin or ctx.etd[c.voyage_id] <= etd[origin]
        }
        for origin in set(moved.values())
    }
    return {lid: by_origin[origin] for lid, origin in moved.items()}


def _next_voyage_reasons(ctx: _Context, plan: Plan, blocked: dict[str, set[str]]) -> list[str]:
    lots = {lot.id: lot for lot in ctx.after.lots}
    reasons = []
    for u in plan.unassigned:
        if u.lot_id not in blocked:
            continue
        candidates = [c for c in ctx.after.containers if c.id not in blocked[u.lot_id]]
        later = [c for c in candidates if c.id not in ctx.unavailable]
        if not candidates:
            reasons.append(f"{u.lot_id}: 이후 항차 없음")
            continue
        if not later:
            taken = ", ".join(f"{c.voyage_id}/{c.id} 다른 계획에 배정됨" for c in candidates)
            reasons.append(f"{u.lot_id}: {taken}")
            continue
        details = []
        for container in later:
            _, reason = sea_option(ctx.model, lots[u.lot_id], container, 0)
            details.append(f"{container.voyage_id}/{container.id} {reason or '용량·혼적 부족'}")
        reasons.append(f"{u.lot_id}: " + ", ".join(details))
    return reasons


def _pinned_conflicts(
    ctx: _Context, built: PlanWithRoutes, kept: set[str], blocked: dict[str, set[str]]
) -> tuple[bool, list[str]]:
    lots = {lot.id: lot for lot in ctx.after.lots}
    pulls = _pulls(ctx.current)
    where = locations(built.plan)
    details = []
    for lid in sorted(kept):
        if lid not in lots or ctx.where.get(lid) not in ctx.voyage_of:
            continue
        if where.get(lid) != ctx.where[lid]:
            details.append(f"{lid}: 배차가 실행된 트럭의 배정을 유지할 수 없음")
            continue
        container = ctx.model.containers[ctx.where[lid]]
        _, reason = sea_option(ctx.model, lots[lid], container, pulls.get(lid, 0))
        if reason:
            details.append(f"{lid}: {reason}")
    pinned_in = {where[lid] for lid in ctx.dispatched_lots if where.get(lid) in ctx.voyage_of}
    crowded = any(
        u.lot_id in blocked and pinned_in - blocked[u.lot_id] for u in built.plan.unassigned
    )
    return bool(details or crowded or _late_pinned(ctx, built.routes)), details


def _kept_pins(ctx: _Context, plan: Plan, routes: list[VoyagePickup]) -> dict[str, FixedLot]:
    where = locations(plan)
    trips = {
        lid: (vp.voyage_id, route)
        for vp in routes
        for route in vp.pickup.routes
        for lid in route_lot_ids([route])
    }
    kept = {}
    for lid, pin in ctx.pin_of.items():
        voyage_id, route = trips.get(lid, (None, None))
        if where.get(lid) not in ctx.voyage_of or route is None:
            continue
        if (route.truck_id, route.earliest_departure) == (pin.truck_id, pin.departure):
            kept[lid] = pin.model_copy(
                update={
                    "voyage_id": voyage_id,
                    "trip": route.trip,
                    "reason": _dispatch_reason(route.truck_id, route.trip),
                }
            )
    return kept


def _broken_pins(ctx: _Context, plan: Plan, routes: list[VoyagePickup]) -> list[str]:
    where = locations(plan)
    kept = _kept_pins(ctx, plan, routes)
    return [
        f"{lid}: 배차가 실행된 트럭 {pin.truck_id} {pin.trip}회차의 트럭·출발 시각을 유지할 수 없음"
        for lid, pin in sorted(ctx.pin_of.items())
        if where.get(lid) in ctx.voyage_of and lid not in kept
    ]


def _late_pinned(ctx: _Context, routes: list[VoyagePickup]) -> list[str]:
    cutoff = {v.id: v.cy_cutoff for v in ctx.after.voyages}
    return [
        f"{lid}: 배차가 실행된 트럭 {route.truck_id}이 선적 마감 뒤에 항만에 도착"
        for vp in routes
        for route in vp.pickup.routes
        if (route.truck_id, route.earliest_departure) in ctx.pinned
        and route.port_eta > cutoff[vp.voyage_id]
        for lid in sorted(route_lot_ids([route]))
    ]


def _alternative(
    ctx: _Context,
    alt_id: AlternativeId,
    built: PlanWithRoutes,
    elapsed: int,
    label: str,
    reasons: list[str],
) -> Alternative:
    plan = built.plan
    if plan.status not in SOLVED:
        reasons = [f"해 없음({plan.status})", *reasons]
    lost = [lid for lid in ctx.new_unassigned(plan) if not any(lid in r for r in reasons)]
    reasons = [*reasons, *(f"{lid}: 배정되지 않음" for lid in lost)]
    where = locations(plan)
    reasons += [
        f"{lid}: 이미 떠난 트럭에 실린 로트의 배정이 바뀜"
        for lid in sorted(ctx.held)
        if where.get(lid) != ctx.where.get(lid)
    ]
    reasons += _late_pinned(ctx, built.routes)
    reasons += [
        r
        for r in _broken_pins(ctx, plan, built.routes)
        if not any(r.partition(": ")[0] in other for other in reasons)
    ]
    diff = diff_plans(
        ctx.after, ctx.current, plan, routes_before=ctx.routes_before, routes_after=built.routes
    )
    return Alternative(
        id=alt_id,
        label=label,
        summary=_describe(ctx, diff),
        feasible=not reasons,
        infeasible_reasons=reasons,
        total_cost=plan_score(plan),
        cost_delta=plan_score(plan) - plan_score(ctx.current),
        plan=plan,
        routes=built.routes,
        diff=diff,
        solve_ms=elapsed,
    )


def replan(
    scenario: Scenario,
    current_plan: Plan,
    event: Event,
    *,
    air_locked: Iterable[str] = (),
    routes: list[VoyagePickup] | None = None,
    dispatched: Iterable[ExecutedDispatch] = (),
    unavailable_containers: Iterable[str] = (),
    now: datetime | None = None,
    time_limit_s: float = 8.0,
    deadline: float | None = None,
) -> ReplanResult:
    started = time.perf_counter()
    dispatched = list(dispatched)
    options = {
        "air_locked": set(air_locked),
        "routes": routes,
        "unavailable": set(unavailable_containers),
        "now": now,
        "time_limit_s": time_limit_s,
        "deadline": deadline,
    }
    result = _replan_with(scenario, current_plan, event, dispatched, **options)
    pinned = any(f.reason_code == "dispatch_executed" for f in result.fixed_reasons)
    first = time.perf_counter() - started
    if result.recommended_id is None and pinned and _has_time(deadline, first):
        try:
            released = _replan_with(scenario, current_plan, event, [], **options)
        except PlanningTimeout:
            released = None
        if released is not None:
            released = _keep_pinned_next_voyage(released, result)
        if released is not None and released.recommended_id is not None:
            released.warnings.append(DISPATCH_RELEASED)
            honored = {id(a) for a in result.alternatives}
            for alternative in released.alternatives:
                if id(alternative) not in honored:
                    alternative.warnings.append(DISPATCH_RELEASED)
            result = released
    result.solve_ms = round((time.perf_counter() - started) * 1000)
    return result


def _recommend(alternatives: list[Alternative]) -> Alternative | None:
    feasible = [a for a in alternatives if a.feasible]
    return min(feasible, key=lambda a: (a.total_cost, PREFERENCE[a.id]), default=None)


def _keep_pinned_next_voyage(released: ReplanResult, pinned: ReplanResult) -> ReplanResult:
    honored = {a.id: a for a in pinned.alternatives if a.id == "next_voyage"}
    alternatives = [
        honored.get(a.id, a)
        for a in released.alternatives
        if a.id != "next_voyage" or a.id in honored
    ]
    best = _recommend(alternatives)
    return released.model_copy(
        update={
            "alternatives": alternatives,
            "recommended_id": best.id if best else None,
            "diff": best.diff if best else None,
        }
    )


def _has_time(deadline: float | None, needed: float) -> bool:
    return deadline is None or deadline - time.perf_counter() > needed


def _replan_with(
    scenario: Scenario,
    current_plan: Plan,
    event: Event,
    dispatched: list[ExecutedDispatch],
    *,
    air_locked: set[str],
    routes: list[VoyagePickup] | None,
    unavailable: set[str],
    now: datetime | None,
    time_limit_s: float,
    deadline: float | None,
) -> ReplanResult:
    started = time.perf_counter()
    occurred_at = event_time(event, now)
    held, held_reasons = (
        fixed_routes(routes, occurred_at, dispatched) if routes is not None else ({}, [])
    )
    departed = [f for f in held_reasons if f.reason_code == "truck_departed"]
    lot_id = getattr(event, "lot_id", None)
    blocked_by = next((f for f in departed if f.lot_id == lot_id), None)
    if blocked_by is not None:
        raise ReplanError(
            "이미 출발했거나 배차가 실행된 트럭에 실린 로트라 바꿀 수 없음: "
            f"{lot_id} ({blocked_by.reason})"
        )
    after = apply_event(scenario, event)
    held_lots = {f.lot_id for f in departed}
    pinned = {
        (f.truck_id, f.departure) for f in held_reasons if f.reason_code == "dispatch_executed"
    }
    pins = {f.lot_id: f for f in held_reasons if f.reason_code == "dispatch_executed"}
    dispatched_lots = set(pins)
    ctx = _Context(
        after,
        current_plan,
        event,
        air_locked,
        routes,
        held_lots,
        scenario,
        unavailable,
        pinned,
        dispatched_lots,
        pins,
    )
    with locked_routes(held, not_before=occurred_at):
        return _replan(ctx, held_reasons, occurred_at, started, time_limit_s, deadline)


def _replan(
    ctx: _Context,
    held_reasons: list[FixedLot],
    occurred_at: datetime | None,
    started: float,
    time_limit_s: float,
    deadline: float | None,
) -> ReplanResult:
    after, current_plan, event = ctx.after, ctx.current, ctx.event
    per_solve = time_limit_s / 5

    scope = "container"
    free = ctx.free_lots(scope)

    def solve_pull(free_lots: set[str]) -> PlanWithRoutes:
        return solve_with_routes(
            after,
            fixed=ctx.fixed_for(free_lots),
            forbidden=ctx.booked_pairs(),
            max_pull_forward_days=MAX_PULL_FORWARD_DAYS,
            time_limit_s=per_solve,
            deadline=deadline,
        )

    pull_built, pull_ms = _timed(lambda: solve_pull(free))
    pull_plan = pull_built.plan
    if ctx.new_unassigned(pull_plan) or pull_plan.status not in SOLVED:
        wider = ctx.free_lots("voyage")
        if wider != free:
            scope, free = "voyage", wider
            pull_built, extra = _timed(lambda: solve_pull(free))
            pull_plan = pull_built.plan
            pull_ms += extra
    fixed = ctx.fixed_for(free)
    alternatives = [
        _alternative(
            ctx,
            "pull_forward",
            pull_built,
            pull_ms,
            LABELS["pull_forward"] if _pulled(ctx, pull_plan) else "재배정",
            [],
        )
    ]

    moved, event_voyages, kept = _moved_for_next_voyage(ctx)
    if (moved or kept) and event_voyages:
        blocked = _next_voyage_blocked(ctx, moved)
        forbidden = [(lid, cid) for lid in moved for cid in sorted(blocked[lid])]
        forbidden += [(lid, AIR) for lid in moved]
        forbidden += ctx.booked_pairs()
        next_fixed = {k: v for k, v in fixed.items() if k not in moved}
        next_fixed.update(
            {
                lid: ctx.where[lid]
                for lid in ctx.dispatched_lots
                if lid in ctx.lot_ids
                and lid not in ctx.air_locked
                and lid not in ctx.rule_switched
                and ctx.where.get(lid) not in (None, UNASSIGNED)
            }
        )
        next_built, next_ms = _timed(
            lambda: solve_with_routes(
                after,
                fixed=next_fixed,
                forbidden=forbidden,
                require_savings=False,
                time_limit_s=per_solve,
                deadline=deadline,
            )
        )
        next_reasons = _next_voyage_reasons(ctx, next_built.plan, blocked)
        reused, details = _pinned_conflicts(ctx, next_built, kept, blocked)
        if reused or not moved:
            next_reasons = [DISPATCH_REUSED, *details, *next_reasons]
        alternatives.append(
            _alternative(
                ctx,
                "next_voyage",
                next_built,
                next_ms,
                LABELS["next_voyage"],
                next_reasons,
            )
        )

    remaining = set(ctx.lot_ids)
    as_is_fixed = {
        lid: loc
        for lid, loc in ctx.where.items()
        if lid in remaining and loc != UNASSIGNED and lid not in ctx.rule_switched
    }
    as_is_fixed.update({lid: AIR for lid in ctx.air_locked})
    as_is_forbidden = ctx.booked_pairs()
    if isinstance(event, AddLotEvent):
        used = {c.container_id for c in current_plan.containers if c.used}
        as_is_forbidden += [(event.lot.id, cid) for cid in used]
    as_is_built, as_is_ms = _timed(
        lambda: solve_with_routes(
            after,
            fixed=as_is_fixed,
            forbidden=as_is_forbidden,
            time_limit_s=per_solve,
            deadline=deadline,
        )
    )
    as_is_reasons = []
    if isinstance(event, DelayEvent):
        lots = {lot.id: lot for lot in after.lots}
        pulls = _pulls(current_plan)
        for lid in sorted(ctx.touched_lots):
            if lid not in lots or ctx.where.get(lid) not in ctx.voyage_of:
                continue
            container = ctx.model.containers[ctx.where[lid]]
            _, reason = sea_option(ctx.model, lots[lid], container, pulls.get(lid, 0))
            if reason:
                as_is_reasons.append(f"{lid}: {reason}")
    alternatives.append(
        _alternative(
            ctx,
            "sail_as_is",
            as_is_built,
            as_is_ms,
            LABELS["sail_as_is"],
            as_is_reasons,
        )
    )

    remaining_budget(deadline, time_limit_s)
    best = _recommend(alternatives)
    kept_pins = _kept_pins(ctx, best.plan, best.routes) if best else ctx.pin_of
    return ReplanResult(
        event=event,
        scope=scope,
        free_lot_ids=sorted(free),
        fixed_lot_ids=sorted(fixed),
        fixed_reasons=[
            kept_pins[f.lot_id] if f.reason_code == "dispatch_executed" else f
            for f in held_reasons
            if f.lot_id in ctx.held
            or (f.reason_code == "dispatch_executed" and f.lot_id in kept_pins)
        ],
        occurred_at=occurred_at,
        alternatives=alternatives,
        recommended_id=best.id if best else None,
        diff=best.diff if best else None,
        solve_ms=round((time.perf_counter() - started) * 1000),
        scenario_after=after,
    )
