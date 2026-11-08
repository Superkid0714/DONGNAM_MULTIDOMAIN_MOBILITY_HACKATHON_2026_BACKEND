from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.optimize import AIR, solve_plan
from freshswitch_backend.planning.service import build_plan, with_routes
from freshswitch_backend.replan import ReduceEvent

SOLVE_LIMIT_S = 2.0
REPLAN_LIMIT_S = 2.0
UNASSIGNED = "UNASSIGNED"
PROPOSAL_EVENT = ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3)


def solve(scenario: Scenario, **options) -> Plan:
    options.setdefault("time_limit_s", SOLVE_LIMIT_S)
    return solve_plan(scenario, **options)


def solve_routed(scenario: Scenario) -> Plan:
    return build_plan(scenario).plan


def route_costed(scenario: Scenario, plan: Plan) -> tuple[Plan, list[str]]:
    built = with_routes(scenario, plan)
    dropped = [lid for vp in built.routes for lid in vp.dropped_lot_ids]
    return built.plan, dropped


def routes(plan: Plan) -> dict[str, str]:
    where = {a.lot_id: a.voyage_id if a.mode == "sea" else AIR for a in plan.assignments}
    where.update({u.lot_id: UNASSIGNED for u in plan.unassigned})
    return where


def locations(plan: Plan) -> dict[str, str]:
    where = {a.lot_id: a.container_id if a.mode == "sea" else AIR for a in plan.assignments}
    where.update({u.lot_id: UNASSIGNED for u in plan.unassigned})
    return where


def shipper_savings(plan: Plan) -> dict[str, int]:
    return {s.shipper_id: s.savings for s in plan.settlements}


def container_fill(plan: Plan) -> dict[str, int]:
    return {c.container_id: c.pallets for c in plan.containers if c.used}


def proposal_lot_id(scenario: Scenario) -> str | None:
    lot = next(
        (
            item
            for item in scenario.lots
            if item.id == "lot-sancheong-sb-1" or item.id.endswith("-lot-sancheong-sb-1")
        ),
        None,
    )
    if lot is None or lot.pallets <= PROPOSAL_EVENT.new_pallets:
        return None
    return lot.id


def proposal_event(scenario: Scenario) -> ReduceEvent | None:
    lot_id = proposal_lot_id(scenario)
    if lot_id is None:
        return None
    return ReduceEvent(lot_id=lot_id, new_pallets=PROPOSAL_EVENT.new_pallets)


def proposal_event_applies(scenario: Scenario) -> bool:
    return proposal_lot_id(scenario) is not None
