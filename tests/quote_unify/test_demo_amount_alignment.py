from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.planning.robust.display import (
    display_lot_amounts,
    display_plan,
    display_replan,
)
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.replan.engine import apply_event, replan
from freshswitch_backend.replan.models import AirSwitchEvent, ReduceEvent


def test_sequential_display_alignment():
    scenario = get_scenario("default").model_copy(deep=True)
    built = build_plan(scenario)
    for stage in range(3):
        shown = display_plan(scenario, built.plan)
        lot = next(l for l in scenario.lots if l.shipper_id == "sh-jinju")
        assignment = next(a for a in built.plan.assignments if a.lot_id == lot.id)
        load = sum(
            a.pallets for a in built.plan.assignments if a.container_id == assignment.container_id
        )
        values = display_lot_amounts(
            scenario, lot, assignment.container_id, load, assignment.pull_forward_days
        )
        row = next(r for r in shown.settlements if r.shipper_id == lot.shipper_id)
        assert values == (row.air_cost, row.shared_cost)
        assert row.fee == round(row.savings * 0.2)
        assert row.shipper_gain == row.savings - row.fee
        if stage == 0:
            assert (row.savings, row.fee, row.shipper_gain) == (3812538, 762508, 3050030)
        assert scenario.cost_params.platform_fee_rate.value == 0.2
        print(stage, load, values, "saving", row.savings, "fee", row.fee)
        if stage == 2:
            break
        event = (
            ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3)
            if stage == 0
            else AirSwitchEvent(lot_id="lot-sancheong-sb-1")
        )
        result = replan(scenario, built.plan, event, routes=built.routes)
        projected = display_replan(scenario, built.plan, result)
        chosen = next(a for a in result.alternatives if a.id == result.recommended_id)
        displayed = next(a for a in projected.alternatives if a.id == result.recommended_id)
        scenario = result.scenario_after or apply_event(scenario, event)
        assert displayed.plan.settlements == display_plan(scenario, chosen.plan).settlements
        built = built.model_copy(update={"plan": chosen.plan, "routes": chosen.routes})
