from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.planning.robust.display import display_plan, display_replan
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.replan.engine import apply_event, replan
from freshswitch_backend.replan.models import AirSwitchEvent, ReduceEvent


def test_actual_unforced_sequential_demo():
    scenario = get_scenario("default").model_copy(deep=True)
    built = build_plan(scenario)
    assert next(a for a in built.plan.assignments if a.lot_id == "lot-sancheong-sb-1").mode == "sea"
    assert next(c for c in built.plan.containers if c.container_id == "CA-01").pallets == 17
    for event in (
        ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3),
        AirSwitchEvent(lot_id="lot-sancheong-sb-1"),
    ):
        result = replan(scenario, built.plan, event, routes=built.routes)
        original = result.model_dump(mode="json")
        shown = display_replan(scenario, built.plan, result)
        assert result.model_dump(mode="json") == original
        for old, new in zip(result.alternatives, shown.alternatives, strict=True):
            assert old.plan.assignments == new.plan.assignments
            assert old.plan.objective == new.plan.objective
            assert (
                new.cost_delta
                == new.total_cost - display_plan(scenario, built.plan).total_shared_cost
            )
        chosen = next(a for a in result.alternatives if a.id == result.recommended_id)
        scenario = result.scenario_after or apply_event(scenario, event)
        built = built.model_copy(update={"plan": chosen.plan, "routes": chosen.routes})
        assert (
            next(a for a in built.plan.assignments if a.lot_id == "lot-sancheong-sb-1").mode == ("air" if event.type == "air_switch" else "sea")
        )
