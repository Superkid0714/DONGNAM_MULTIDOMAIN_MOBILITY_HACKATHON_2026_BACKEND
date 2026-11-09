"""Dedicated sea baseline: no air choices, no shared containers with other shippers."""

import math
from collections import defaultdict
from functools import lru_cache

from freshswitch_backend.domain.models import Scenario


@lru_cache(maxsize=256)
def _sea_solo(serialized: str, lot_ids: tuple[str, ...]) -> int | None:
    from freshswitch_backend.optimize.solver import solve_plan
    from freshswitch_backend.planning.service import allocate_route_costs, build_routes

    scenario = Scenario.model_validate_json(serialized)
    lots = [lot for lot in scenario.lots if lot.id in lot_ids]
    if not lots:
        return None
    containers = []
    for voyage in scenario.voyages:
        template = next((c for c in scenario.containers if c.voyage_id == voyage.id), None)
        if template is None:
            continue
        pallets = defaultdict(int)
        for lot in lots:
            pallets[lot.product_id] += lot.pallets
        count = sum(math.ceil(n / template.capacity_pallets) for n in pallets.values())
        containers.extend(
            template.model_copy(update={"id": f"solo/{voyage.id}/{i}"}) for i in range(count)
        )
    scenario = scenario.model_copy(update={"lots": lots, "containers": containers})
    plan = solve_plan(
        scenario,
        allow_air=False,
        require_savings=False,
        compute_solo=False,
        time_limit_s=0.5,
    )
    if plan.unassigned or len(plan.assignments) != len(lots):
        return None
    routes = build_routes(scenario, plan, time_limit_s=0.2)
    if any(route.dropped_lot_ids for route in routes):
        return None
    collections = allocate_route_costs(scenario, plan, routes)
    # Replace the independent-leg estimate with this shipper's dedicated route.
    return (
        plan.total_operational_cost
        - plan.total_collection_estimate
        + sum(c.route for c in collections)
    )


def sea_solo_cost(scenario: Scenario, lot_ids: list[str]) -> int | None:
    return _sea_solo(scenario.model_dump_json(), tuple(sorted(lot_ids)))


def quote_amounts(air: int, solo: int | None, shared: int, fee: int) -> dict:
    paid = shared + fee
    return {
        "air_cost": air,
        "sea_solo_cost": solo,
        "shipper_net_saving": air - paid,
        "savings_vs_air_pct": round((air - paid) / air * 100, 2) if air > 0 else None,
        "savings_vs_solo_pct": round((solo - paid) / solo * 100, 2) if solo and solo > 0 else None,
    }
