from decimal import Decimal

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.planning.robust_plan import plan_robust, robust_changes
from freshswitch_backend.planning.service import build_plan


def test_solver_assignments_reduce_17_to_15():
    scenario = get_scenario("default").model_copy(deep=True)
    scenario.lots = scenario.lots[:3]
    first = scenario.lots[0]

    def item(key, value, basis="per_container"):
        return {
            "key": key,
            "supplier": "fixture",
            "basis": basis,
            "grade": "reference",
            "minimum": value,
            "maximum": value,
        }

    inputs = {
        "shipper_pallets": 8,
        "container_pallets": 17,
        "shipment_date": "2026-07-20",
        "actual_weight_kg": 3320,
        "inventory_complete": True,
        "air": [item("fuel", 3253600, "per_shipper")],
        "ocean": [
            item("known_container", 1306330),
            item("ddf", 108000, "per_shipper"),
            {"key": "freight", "supplier": "fixture", "basis": "per_container", "grade": "unknown"},
        ],
    }
    key = f"CA-01:{first.shipper_id}"
    from types import SimpleNamespace

    context = SimpleNamespace(**scenario.model_dump())
    context.robust_quote_inputs = {key: inputs}
    context.container_gate_in = {}
    context.voyages = scenario.voyages
    before = build_plan(
        scenario, fixed={lot.id: "CA-01" for lot in scenario.lots}, require_savings=False
    ).plan
    changed = scenario.model_copy(deep=True)
    changed.lots[1].pallets -= 2
    after = build_plan(
        changed, fixed={lot.id: "CA-01" for lot in changed.lots}, require_savings=False
    ).plan
    a, b = plan_robust(context, before)[key], plan_robust(context, after)[key]
    assert a["container_pallets"] == 17 and b["container_pallets"] == 15
    assert Decimal(b["margin"]) < Decimal(a["margin"])
    assert Decimal(b["break_even_threshold_container"]) < Decimal(
        a["break_even_threshold_container"]
    )
    assert key in robust_changes(context, before, context, after)
    print("BEFORE", a["margin"], a["break_even_threshold_container"])
    print("AFTER", b["margin"], b["break_even_threshold_container"])
