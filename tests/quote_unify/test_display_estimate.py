import pytest

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.optimize.solver import solve_plan
from freshswitch_backend.planning.robust.display import display_plan
from freshswitch_backend.planning.service import build_plan


@pytest.mark.parametrize("stage", ["initial", "sancheong_reduce", "miryang_air"])
def test_display_never_mutates_solver_assignments(stage):
    scenario = get_scenario("default").model_copy(deep=True)
    scenario.lots = scenario.lots[:3]
    if stage == "sancheong_reduce":
        scenario.lots[1].pallets -= 2
    fixed = {lot.id: "CA-01" for lot in scenario.lots}
    if stage == "miryang_air":
        fixed[scenario.lots[2].id] = "AIR"
    plan = build_plan(scenario, fixed=fixed, require_savings=False).plan
    original = plan.model_dump(mode="json")
    projected = display_plan(scenario, plan)
    assert plan.model_dump(mode="json") == original
    assert projected.assignments == plan.assignments
    assert projected.containers == plan.containers
    assert projected.objective == plan.objective
    assert projected.total_fee == sum(r.fee for r in projected.settlements)
    assert projected.model_dump().keys() == plan.model_dump().keys()
    old, new = plan.settlements[0], projected.settlements[0]
    assert new.savings == new.baseline_cost - new.shared_cost
    assert new.shipper_gain == new.savings - new.fee
    assert new.fee == round(max(0, new.savings) * 0.2)
    print(
        stage,
        "before",
        old.baseline_cost,
        old.shared_cost,
        old.savings,
        "after",
        new.baseline_cost,
        new.shared_cost,
        new.savings,
    )


def test_engine_is_not_importing_display_projection():
    assert "display_plan" not in solve_plan.__code__.co_names
