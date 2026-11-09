import pytest

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.planning.service import build_plan


def test_shipper_fixed_fee_once_across_lots_and_containers():
    scenario = get_scenario("default").model_copy(deep=True)
    scenario.lots = scenario.lots[:2]
    for lot in scenario.lots:
        lot.shipper_id = scenario.lots[0].shipper_id
    plan = build_plan(
        scenario,
        fixed={scenario.lots[0].id: "CA-01", scenario.lots[1].id: "CA-02"},
        require_savings=False,
    ).plan
    row = plan.shipper_costs[0]
    assert row.plan.collection == 350000
    assert row.plan.handling == (1382230 * 2 + 108000 + 42875 * 13)
    assert sum(c.total_fee for c in plan.container_settlements) == plan.total_fee


def test_fixed_pickup_replaces_route_price_and_loss_is_separate():
    scenario = get_scenario("default").model_copy(deep=True)
    plan = build_plan(scenario).plan
    row = next(r for r in plan.shipper_costs if r.shipper_id == "sh-jinju")
    assert row.plan.collection == 350000
    assert row.plan.loss_risk == pytest.approx(597600)
    # Engine additionally retains 2 days × 8 pallets × 3000 warehouse storage.
    assert row.plan.storage == 48000
