import time
from collections.abc import Callable, Iterable

from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.optimize import AIR
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.feasibility import air_rejection, sea_option
from freshswitch_backend.planning.budget import remaining as remaining_budget
from freshswitch_backend.planning.service import PlanWithRoutes, solve_with_routes
from freshswitch_backend.replan.models import (
    AddLotEvent,
    AirSwitchEvent,
    Alternative,
    AlternativeId,
    CancelEvent,
    ContainerFill,
    DelayEvent,
    Event,
    LotMove,
    PlanDiff,
    ReduceEvent,
    ReplanError,
    ReplanResult,
    ShipperDelta,
)

UNASSIGNED = "UNASSIGNED"
MAX_PULL_FORWARD_DAYS = 2
SOLVED = ("OPTIMAL", "FEASIBLE")
LABELS: dict[AlternativeId, str] = {
    "pull_forward": "당겨 채우기",
    "next_voyage": "다음 배 이월",
    "sail_as_is": "그대로 출항",
}
PREFERENCE: dict[AlternativeId, int] = {"pull_forward": 0, "sail_as_is": 1, "next_voyage": 2}


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
            if new_pallets >= lots[lot_id].pallets:
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
        case DelayEvent(voyage_id=voyage_id, new_etd=new_etd, new_cy_cutoff=new_cutoff):
            voyage = next((v for v in scenario.voyages if v.id == voyage_id), None)
            if voyage is None:
                raise ReplanError(f"항차를 찾을 수 없음: {voyage_id}")
            cutoff = new_cutoff or voyage.cy_cutoff
            if new_cutoff is None and new_etd < voyage.etd:
                cutoff = voyage.cy_cutoff + (new_etd - voyage.etd)
            if cutoff > new_etd:
                raise ReplanError("선적 마감이 출항 시각보다 늦을 수 없음")
            changed = voyage.model_copy(
                update={
                    "etd": new_etd,
                    "cy_cutoff": cutoff,
                    "original_etd": voyage.original_etd or voyage.etd,
                }
            )
            return scenario.model_copy(
                update={"voyages": [changed if v.id == voyage_id else v for v in scenario.voyages]}
            )
        case AddLotEvent(lot=lot):
            if lot.id in lots:
                raise ReplanError(f"이미 있는 로트 ID: {lot.id}")
            if lot.shipper_id not in {s.id for s in scenario.shippers}:
                raise ReplanError(f"화주를 찾을 수 없음: {lot.shipper_id}")
            if lot.product_id not in {p.id for p in scenario.products}:
                raise ReplanError(f"품목을 찾을 수 없음: {lot.product_id}")
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
    ) -> None:
        self.after = after
        self.current = current
        self.event = event
        self.model = CostModel(after)
        self.where = locations(current)
        self.lot_ids = [lot.id for lot in after.lots]
        self.voyage_of = {c.id: c.voyage_id for c in after.containers}
        self.etd = {v.id: v.etd for v in after.voyages}
        self.air_locked = set(air_locked) & set(self.lot_ids)
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
            case DelayEvent(voyage_id=voyage_id):
                return {
                    lid for lid, loc in self.where.items() if self.voyage_of.get(loc) == voyage_id
                }
        return set()

    def _touched_containers(self) -> set[str]:
        if isinstance(self.event, DelayEvent):
            return {c.id for c in self.after.containers if c.voyage_id == self.event.voyage_id}
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
        return free - self.air_locked

    def fixed_for(self, free: set[str]) -> dict[str, str]:
        fixed = {
            lid: self.where[lid]
            for lid in self.lot_ids
            if lid not in free and self.where.get(lid) not in (None, UNASSIGNED)
        }
        fixed.update({lid: AIR for lid in self.air_locked})
        return fixed

    def new_unassigned(self, plan: Plan) -> list[str]:
        return [u.lot_id for u in plan.unassigned if self.where.get(u.lot_id) != UNASSIGNED]


def _timed(fn: Callable[[], PlanWithRoutes]) -> tuple[PlanWithRoutes, int]:
    started = time.perf_counter()
    built = fn()
    return built, round((time.perf_counter() - started) * 1000)


def diff_plans(scenario: Scenario, before: Plan, after: Plan) -> PlanDiff:
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
    return PlanDiff(moved_lots=moved, shippers=shippers, containers=containers)


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


def _moved_for_next_voyage(ctx: _Context) -> tuple[set[str], set[str]]:
    if isinstance(ctx.event, AddLotEvent):
        lot = ctx.event.lot
        candidates = [
            c for c in ctx.after.containers if sea_option(ctx.model, lot, c, 0)[0] is not None
        ]
        if not candidates:
            return set(), set()
        first = min(candidates, key=lambda c: ctx.etd[c.voyage_id])
        return {lot.id}, {first.voyage_id}
    moved = {
        lid
        for lid in ctx.lot_ids
        if ctx.where.get(lid) in ctx.touched_containers
        and lid not in ctx.air_locked
        and lid not in ctx.rule_switched
    }
    if isinstance(ctx.event, ReduceEvent):
        moved.add(ctx.event.lot_id)
    return moved, ctx.scope_voyages()


def _next_voyage_reasons(ctx: _Context, plan: Plan, moved: set[str], horizon) -> list[str]:
    lots = {lot.id: lot for lot in ctx.after.lots}
    later = [c for c in ctx.after.containers if ctx.etd[c.voyage_id] > horizon]
    reasons = []
    for u in plan.unassigned:
        if u.lot_id not in moved:
            continue
        if not later:
            reasons.append(f"{u.lot_id}: 이후 항차 없음")
            continue
        details = []
        for container in later:
            _, reason = sea_option(ctx.model, lots[u.lot_id], container, 0)
            details.append(f"{container.voyage_id}/{container.id} {reason or '용량·혼적 부족'}")
        reasons.append(f"{u.lot_id}: " + ", ".join(details))
    return reasons


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
    diff = diff_plans(ctx.after, ctx.current, plan)
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
    time_limit_s: float = 8.0,
    deadline: float | None = None,
) -> ReplanResult:
    started = time.perf_counter()
    after = apply_event(scenario, event)
    ctx = _Context(after, current_plan, event, set(air_locked))
    per_solve = time_limit_s / 5

    scope = "container"
    free = ctx.free_lots(scope)

    def solve_pull(free_lots: set[str]) -> PlanWithRoutes:
        return solve_with_routes(
            after,
            fixed=ctx.fixed_for(free_lots),
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

    moved, event_voyages = _moved_for_next_voyage(ctx)
    if moved and event_voyages:
        horizon = max(ctx.etd[v] for v in event_voyages)
        blocked = [c.id for c in after.containers if ctx.etd[c.voyage_id] <= horizon]
        forbidden = [(lid, cid) for lid in moved for cid in blocked]
        forbidden += [(lid, AIR) for lid in moved]
        next_fixed = {k: v for k, v in fixed.items() if k not in moved}
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
        alternatives.append(
            _alternative(
                ctx,
                "next_voyage",
                next_built,
                next_ms,
                LABELS["next_voyage"],
                _next_voyage_reasons(ctx, next_built.plan, moved, horizon),
            )
        )

    remaining = set(ctx.lot_ids)
    as_is_fixed = {
        lid: loc
        for lid, loc in ctx.where.items()
        if lid in remaining and loc != UNASSIGNED and lid not in ctx.rule_switched
    }
    as_is_fixed.update({lid: AIR for lid in ctx.air_locked})
    as_is_forbidden = []
    if isinstance(event, AddLotEvent):
        used = {c.container_id for c in current_plan.containers if c.used}
        as_is_forbidden = [(event.lot.id, cid) for cid in used]
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

    feasible = [a for a in alternatives if a.feasible]
    remaining_budget(deadline, time_limit_s)
    best = min(feasible, key=lambda a: (a.total_cost, PREFERENCE[a.id]), default=None)
    return ReplanResult(
        event=event,
        scope=scope,
        free_lot_ids=sorted(free),
        fixed_lot_ids=sorted(fixed),
        alternatives=alternatives,
        recommended_id=best.id if best else None,
        diff=best.diff if best else None,
        solve_ms=round((time.perf_counter() - started) * 1000),
        scenario_after=after,
    )
