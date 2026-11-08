import math
import time
from datetime import datetime, timedelta

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from freshswitch_backend.routing.models import (
    GateMode,
    GateStatus,
    PickupPlan,
    PickupRequest,
    PickupStop,
    StopVisit,
    TruckRoute,
)

EARTH_RADIUS_KM = 6371.0
DROP_PENALTY = 10**9

Interval = tuple[int, int, str]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _assumptions(req: PickupRequest) -> list[str]:
    lines = [
        f"가정: 도로거리 = 직선(haversine) 거리 × {req.road_factor}",
        f"가정: 평균 주행속도 {req.speed_kmh:g} km/h",
        f"가정: 산지 상차 작업 {req.service_minutes}분/정차",
        f"가정: km당 운행비 {req.cost_per_km:,.0f}원",
        "가정: 트럭은 항만에서 출발해 산지를 돌고 CY cut-off 전에 항만으로 돌아온다",
        (
            f"가정: 트럭 한 대가 cut-off 전까지 최대 {req.max_trips_per_truck}회 왕복한다 "
            f"(항만 하역 {req.service_minutes}분 뒤 재출발)"
        ),
        "가정: 한 정차지 물량은 트럭 한 대에 싣는다 (분할 집하 없음)",
    ]
    if req.gate_windows:
        spans = ", ".join(w.text() for w in req.gate_windows)
        lines.append(
            f"가정: 항만 게이트 혼잡 시간({spans})에는 도착하지 않도록 출발을 늦춘다. "
            "피하면 cut-off 전 집하를 못 하는 정차지가 생길 때만 혼잡 시간 도착을 허용한다"
        )
    if req.locked_routes:
        lines.append(
            f"고정: 이미 출발했거나 배차가 실행된 트럭 운행 {len(req.locked_routes)}건은 "
            "경로·적재를 바꾸지 않고, 그 트럭은 항만 복귀 뒤에만 다시 배차한다"
        )
    if req.incompatible_groups:
        lines.append(
            "규칙: 컨테이너 혼적 규칙(SO2 처리 여부·온도·CA 조건)이 맞지 않는 품목은 "
            "같은 트럭 운행에 싣지 않는다"
        )
    return lines


def _floor_min(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _release_locked(req: PickupRequest) -> PickupRequest:
    turnaround = timedelta(minutes=req.service_minutes)
    free_at: dict[str, datetime] = {}
    until: dict[str, datetime] = {}

    def after(truck_id: str, at: datetime) -> None:
        free_at[truck_id] = max(at, free_at.get(truck_id, at))

    for route in req.locked_routes:
        after(route.truck_id, route.port_eta + turnaround)
    for route in [*req.blocking_routes, *req.reserved_routes]:
        leave, back = route.earliest_departure - turnaround, route.port_eta + turnaround
        if leave >= req.deadline:
            continue
        if back >= req.deadline:
            until[route.truck_id] = min(leave, until.get(route.truck_id, leave))
        else:
            after(route.truck_id, back)
    trucks = [
        t.model_copy(
            update={
                "available_from": max(
                    [x for x in (t.available_from, free_at.get(t.id)) if x is not None],
                    default=None,
                ),
                "available_until": min(
                    [x for x in (t.available_until, until.get(t.id)) if x is not None],
                    default=None,
                ),
            }
        )
        for t in req.trucks
    ]
    return req.model_copy(update={"trucks": trucks})


def _merge_locked(req: PickupRequest, routes: list[TruckRoute]) -> list[TruckRoute]:
    last: dict[str, int] = {}
    for route in [*req.locked_routes, *req.reserved_routes]:
        last[route.truck_id] = max(route.trip, last.get(route.truck_id, 0))
    shifted = []
    for route in sorted(routes, key=lambda r: (r.earliest_departure, r.trip)):
        trip = last.get(route.truck_id, 0) + 1
        last[route.truck_id] = trip
        shifted.append(route.model_copy(update={"trip": trip}))
    order = {t.id: i for i, t in enumerate(req.trucks)}
    merged = [*req.locked_routes, *shifted]
    return sorted(merged, key=lambda r: (order.get(r.truck_id, len(order)), r.trip))


def solve_pickup(request: PickupRequest, time_limit_s: float = 5.0) -> PickupPlan:
    started = time.perf_counter()
    pinned = request.locked_routes or request.blocking_routes or request.reserved_routes
    req = _release_locked(request) if pinned else request
    points = [(req.depot.lat, req.depot.lon)] + [(s.lat, s.lon) for s in req.stops]
    n = len(points)
    km = [
        [
            haversine_km(*points[i], *points[j]) * req.road_factor if i != j else 0.0
            for j in range(n)
        ]
        for i in range(n)
    ]
    travel = [[math.ceil(km[i][j] / req.speed_kmh * 60) for j in range(n)] for i in range(n)]
    service = [0] + [req.service_minutes for _ in req.stops]

    t0_candidates = [
        s.ready_at - timedelta(minutes=travel[0][k + 1]) for k, s in enumerate(req.stops)
    ]
    if req.trucks_available_from is not None:
        t0_candidates.append(req.trucks_available_from)
    t0 = _floor_min(min(t0_candidates, default=req.deadline))
    if req.not_before is not None and req.not_before > t0:
        available = max(req.not_before, req.trucks_available_from or req.not_before)
        req = req.model_copy(update={"trucks_available_from": available})
        t0 = _floor_min(min([*t0_candidates, available], default=req.deadline))

    def to_min(dt: datetime) -> int:
        return math.floor((dt - t0).total_seconds() / 60)

    def to_dt(m: int) -> datetime:
        return t0 + timedelta(minutes=m)

    deadline = to_min(req.deadline)
    max_cap = max((t.capacity_pallets for t in req.trucks), default=0)
    start_min = max(0, to_min(req.trucks_available_from)) if req.trucks_available_from else 0

    windows: dict[int, tuple[int, int]] = {}
    pre_dropped: list[str] = []
    for k, stop in enumerate(req.stops):
        node = k + 1
        lo = max(to_min(stop.ready_at) if stop.ready_at > t0 else 0, start_min + travel[0][node])
        hi = deadline - service[node] - travel[node][0]
        if lo > hi or stop.pallets > max_cap:
            pre_dropped.append(stop.id)
        else:
            windows[node] = (lo, hi)

    routes: list[TruckRoute] = []
    dropped = list(pre_dropped)
    status = "NO_STOPS"
    horizon = max(deadline, 0)
    blocked = _blocked_intervals(req, t0, to_min)
    gate_mode: GateMode = "none"
    truck_start = [_truck_start(t.available_from, start_min, to_min) for t in req.trucks]
    truck_end = [
        min(horizon, to_min(t.available_until)) if t.available_until else horizon
        for t in req.trucks
    ]

    if windows and req.trucks:
        args = (req, windows, km, travel, service, (truck_start, truck_end), horizon, time_limit_s)
        outcome = _run(*args, blocked) if blocked else None
        if blocked:
            gate_mode = "avoid"
        if outcome is None or outcome[1]:
            plain = _run(*args, [])
            if outcome is None or (plain is not None and len(plain[1]) < len(outcome[1])):
                outcome = plain
                gate_mode = "fallback" if blocked else "none"
        if outcome is None:
            status = "INFEASIBLE"
            dropped.extend(req.stops[node - 1].id for node in sorted(windows))
        else:
            status = "OK"
            seqs_by_truck, lost = outcome
            dropped.extend(lost)
            for t, truck in enumerate(req.trucks):
                routes.extend(
                    _build_truck_routes(
                        req,
                        truck.id,
                        truck.capacity_pallets,
                        seqs_by_truck[t],
                        windows,
                        km,
                        travel,
                        service,
                        truck_start[t],
                        min(deadline, truck_end[t]),
                        to_dt,
                        blocked,
                    )
                )
    elif windows:
        status = "NO_TRUCKS"
        dropped.extend(req.stops[node - 1].id for node in windows)

    if req.locked_routes or req.reserved_routes:
        routes = _merge_locked(req, routes)
    if req.locked_routes and status == "NO_STOPS":
        status = "OK"
    if dropped and status == "OK":
        status = "PARTIAL"
    order = {s.id: i for i, s in enumerate(req.stops)}
    dropped.sort(key=order.__getitem__)
    total_km = round(sum(r.km for r in routes), 1)
    total_cost = round(sum(r.cost for r in routes))
    return PickupPlan(
        status=status,
        deadline=req.deadline,
        routes=routes,
        total_km=total_km,
        total_cost=total_cost,
        dropped_stop_ids=dropped,
        solve_ms=round((time.perf_counter() - started) * 1000),
        assumptions=_assumptions(req),
        gate_mode=gate_mode,
        gate_windows=req.gate_windows,
    )


def _blocked_intervals(req: PickupRequest, t0: datetime, to_min) -> list[Interval]:
    if not req.gate_windows:
        return []
    tz = req.deadline.tzinfo
    first = t0.astimezone(tz).date() - timedelta(days=1)
    last = req.deadline.astimezone(tz).date() + timedelta(days=1)
    intervals: list[Interval] = []
    day = first
    while day <= last:
        for window in req.gate_windows:
            begin = datetime.combine(day, window.start, tzinfo=tz)
            end = datetime.combine(day, window.end, tzinfo=tz)
            if end <= begin:
                end += timedelta(days=1)
            intervals.append((to_min(begin), to_min(end), window.text()))
        day += timedelta(days=1)
    return sorted(intervals)


def _hit(minute: int, blocked: list[Interval]) -> Interval | None:
    return next((iv for iv in blocked if iv[0] <= minute < iv[1]), None)


def _gate_shift(eta: int, blocked: list[Interval]) -> tuple[int, str | None]:
    first = _hit(eta, blocked)
    if first is None:
        return 0, None
    arrival = eta
    hit: Interval | None = first
    while hit is not None:
        arrival = hit[1]
        hit = _hit(arrival, blocked)
    return arrival - eta, first[2]


def _gate_slot(
    eta: int, status: GateStatus, blocked: list[Interval]
) -> tuple[int | None, int | None]:
    if status == "peak":
        hit = _hit(eta, blocked)
        return (hit[0], hit[1]) if hit else (None, None)
    before = [b for _, b, _ in blocked if b <= eta]
    after = [a for a, _, _ in blocked if a > eta]
    return (max(before) if before else None, min(after) if after else None)


def _run(
    req: PickupRequest,
    windows: dict[int, tuple[int, int]],
    km: list[list[float]],
    travel: list[list[int]],
    service: list[int],
    availability: tuple[list[int], list[int]],
    horizon: int,
    time_limit_s: float,
    blocked: list[Interval],
) -> tuple[list[list[list[int]]], list[str]] | None:
    truck_start, truck_end = availability
    active = sorted(windows)
    nodes = [0] + active
    m = len(nodes)
    n_trucks = len(req.trucks)
    trips = req.max_trips_per_truck
    vehicles = n_trucks * trips
    manager = pywrapcp.RoutingIndexManager(m, vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def dist_cb(i: int, j: int) -> int:
        a, b = nodes[manager.IndexToNode(i)], nodes[manager.IndexToNode(j)]
        return round(km[a][b] * 1000)

    def time_cb(i: int, j: int) -> int:
        a, b = nodes[manager.IndexToNode(i)], nodes[manager.IndexToNode(j)]
        return service[a] + travel[a][b]

    def demand_cb(i: int) -> int:
        node = nodes[manager.IndexToNode(i)]
        return 0 if node == 0 else req.stops[node - 1].pallets

    dist_idx = routing.RegisterTransitCallback(dist_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(dist_idx)
    fixed = (
        round(req.truck_fixed_cost / req.cost_per_km * 1000)
        if req.truck_fixed_cost > 0 and req.cost_per_km > 0
        else 0
    )
    for v in range(vehicles):
        extra = v // n_trucks
        if fixed or extra:
            routing.SetFixedCostOfVehicle(fixed + extra, v)

    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimensionWithVehicleCapacity(
        demand_idx,
        0,
        [req.trucks[v % n_trucks].capacity_pallets for v in range(vehicles)],
        True,
        "Load",
    )

    time_idx = routing.RegisterTransitCallback(time_cb)
    routing.AddDimension(time_idx, horizon, horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    for pos, node in enumerate(nodes[1:], start=1):
        index = manager.NodeToIndex(pos)
        time_dim.CumulVar(index).SetRange(*windows[node])
        routing.AddDisjunction([index], DROP_PENALTY)
    usable = [
        v
        for v in range(vehicles)
        if truck_start[v % n_trucks] + (v // n_trucks) * req.service_minutes
        <= truck_end[v % n_trucks]
    ]
    if not usable:
        return None
    for v in sorted(set(range(vehicles)) - set(usable)):
        for pos in range(1, m):
            routing.VehicleVar(manager.NodeToIndex(pos)).RemoveValue(v)
    for v in usable:
        begin, end = truck_start[v % n_trucks], truck_end[v % n_trucks]
        time_dim.CumulVar(routing.Start(v)).SetRange(begin, horizon)
        time_dim.CumulVar(routing.End(v)).SetRange(begin, horizon)
        for lo, hi, _ in blocked:
            lo, hi = max(lo, begin), min(hi - 1, horizon)
            if lo <= hi:
                time_dim.CumulVar(routing.End(v)).RemoveInterval(lo, hi)
        if end < horizon:
            routing.solver().Add(
                time_dim.CumulVar(routing.End(v))
                <= end + (horizon - end) * (1 - routing.ActiveVehicleVar(v))
            )
        if v >= n_trucks:
            routing.solver().Add(
                time_dim.CumulVar(routing.End(v - n_trucks)) + req.service_minutes
                <= time_dim.CumulVar(routing.Start(v))
            )

    groups = sorted({req.stops[node - 1].cargo_group for node in active} - {None})
    type_of = {g: i for i, g in enumerate(groups)}
    conflicts = {
        (type_of[a], type_of[b])
        for a, b in req.incompatible_groups
        if a in type_of and b in type_of and a != b
    }
    if conflicts:
        for pos, node in enumerate(nodes[1:], start=1):
            group = req.stops[node - 1].cargo_group
            if group in type_of:
                routing.SetVisitType(
                    manager.NodeToIndex(pos),
                    type_of[group],
                    pywrapcp.RoutingModel.TYPE_ADDED_TO_VEHICLE,
                )
        for a, b in sorted(conflicts):
            routing.AddHardTypeIncompatibility(a, b)
            routing.AddHardTypeIncompatibility(b, a)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GREEDY_DESCENT
    params.time_limit.FromMilliseconds(max(1, round(time_limit_s * 1000)))

    solution = routing.SolveWithParameters(params)
    if solution is None:
        return None
    lost: list[str] = []
    for pos, node in enumerate(nodes[1:], start=1):
        index = manager.NodeToIndex(pos)
        if solution.Value(routing.NextVar(index)) == index:
            lost.append(req.stops[node - 1].id)
    seqs_by_truck: list[list[list[int]]] = []
    for t in range(n_trucks):
        seqs: list[list[int]] = []
        for k in range(trips):
            v = k * n_trucks + t
            seq: list[int] = []
            index = solution.Value(routing.NextVar(routing.Start(v)))
            while not routing.IsEnd(index):
                seq.append(nodes[manager.IndexToNode(index)])
                index = solution.Value(routing.NextVar(index))
            if seq:
                seqs.append(seq)
        seqs_by_truck.append(seqs)
    return seqs_by_truck, lost


def _truck_start(available_from: datetime | None, start_min: int, to_min) -> int:
    if available_from is None:
        return start_min
    return max(start_min, to_min(available_from))


def _schedule(
    seq: list[int],
    windows: dict[int, tuple[int, int]],
    travel: list[list[int]],
    service: list[int],
    free_at: int,
) -> list[int]:
    times: list[int] = []
    prev, t = 0, free_at
    for node in seq:
        t = max(windows[node][0], t + service[prev] + travel[prev][node])
        times.append(t)
        prev = node
    return times


def _latest(
    seq: list[int],
    windows: dict[int, tuple[int, int]],
    travel: list[list[int]],
    service: list[int],
    back_by: int,
) -> list[int]:
    latest = [0] * len(seq)
    nxt_time, nxt = back_by, 0
    for k in range(len(seq) - 1, -1, -1):
        node = seq[k]
        latest[k] = min(windows[node][1], nxt_time - travel[node][nxt] - service[node])
        nxt_time, nxt = latest[k], node
    return latest


def _build_truck_routes(
    req: PickupRequest,
    truck_id: str,
    capacity: int,
    seqs: list[list[int]],
    windows: dict[int, tuple[int, int]],
    km: list[list[float]],
    travel: list[list[int]],
    service: list[int],
    start_min: int,
    deadline: int,
    to_dt,
    blocked: list[Interval],
) -> list[TruckRoute]:
    turnaround = req.service_minutes
    latest: list[list[int]] = [[] for _ in seqs]
    back_by: list[int] = [deadline] * len(seqs)
    bound = deadline
    for i in range(len(seqs) - 1, -1, -1):
        back_by[i] = bound
        latest[i] = _latest(seqs[i], windows, travel, service, bound)
        bound = latest[i][0] - travel[0][seqs[i][0]] - turnaround
    earliest: list[list[int]] = []
    gates: list[tuple[GateStatus, str | None, int]] = []
    free_at = start_min
    for i, seq in enumerate(seqs):
        times = _schedule(seq, windows, travel, service, free_at)
        eta = times[-1] + service[seq[-1]] + travel[seq[-1]][0]
        shift, window = _gate_shift(eta, blocked)
        status: GateStatus = "off_peak"
        if window is not None:
            room = min(late - early for late, early in zip(latest[i], times, strict=True))
            if shift <= room:
                times = [t + shift for t in times]
                eta += shift
                status = "avoided"
            else:
                shift = 0
                status = "peak"
        gates.append((status, window, shift))
        earliest.append(times)
        free_at = eta + turnaround

    routes: list[TruckRoute] = []
    for i, seq in enumerate(seqs):
        first = seq[0]
        port_eta = earliest[i][-1] + service[seq[-1]] + travel[seq[-1]][0]
        status, window, shift = gates[i]
        slot_start, slot_end = _gate_slot(port_eta, status, blocked) if blocked else (None, None)
        visits: list[StopVisit] = []
        load, prev, route_km = 0, 0, 0.0
        for k, node in enumerate(seq):
            stop: PickupStop = req.stops[node - 1]
            load += stop.pallets
            route_km += km[prev][node]
            visits.append(
                StopVisit(
                    stop_id=stop.id,
                    packhouse_id=stop.packhouse_id,
                    lot_ids=stop.lot_ids,
                    sequence=k + 1,
                    pallets=stop.pallets,
                    load_after=load,
                    km_from_prev=round(km[prev][node], 1),
                    ready_at=stop.ready_at,
                    eta=to_dt(earliest[i][k]),
                    latest_arrival=to_dt(latest[i][k]),
                    cargo_group=stop.cargo_group,
                )
            )
            prev = node
        route_km += km[prev][0]
        groups = {req.stops[node - 1].cargo_group for node in seq} - {None}
        routes.append(
            TruckRoute(
                truck_id=truck_id,
                trip=i + 1,
                cargo_group=groups.pop() if len(groups) == 1 else None,
                capacity_pallets=capacity,
                load_pallets=load,
                stops=visits,
                km=round(route_km, 1),
                cost=round(route_km * req.cost_per_km + req.truck_fixed_cost),
                earliest_departure=to_dt(earliest[i][0] - travel[0][first]),
                latest_departure=to_dt(latest[i][0] - travel[0][first]),
                port_eta=to_dt(port_eta),
                slack_minutes=back_by[i] - port_eta,
                gate_status=status,
                gate_window=window,
                gate_delay_minutes=shift,
                gate_slot_start=to_dt(slot_start) if slot_start is not None else None,
                gate_slot_end=to_dt(slot_end) if slot_end is not None else None,
            )
        )
    return routes
