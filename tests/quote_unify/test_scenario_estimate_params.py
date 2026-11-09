import pytest

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.optimize.costs import CostModel


@pytest.mark.parametrize("name", ["default", "fuel-shock"])
def test_scenario_parameters_and_fixed_loss(name):
    scenario = get_scenario(name).model_copy(deep=True)
    params = scenario.cost_params
    assert params.sea_freight_per_container_krw.value == 2822000
    assert params.sea_ancillary_per_container_krw.value == 1382230
    assert params.air_freight_per_kg_krw.value == 2000
    assert params.air_fuel_surcharge_per_kg_krw.value == 0
    assert params.air_fuel_surcharge_by_month == {}
    lot, voyage = scenario.lots[0], scenario.voyages[0]
    for delay in (0, 2.75, 14, 48):
        voyage.delay_p90_h = delay
        assert CostModel(scenario).sea_loss_rate(lot, voyage) == pytest.approx(0.01)