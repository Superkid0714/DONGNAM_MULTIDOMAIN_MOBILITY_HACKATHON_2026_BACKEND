import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.analysis.business import (
    ANNUAL_FIXED_COST,
    DEFAULT_OPS_COST_PER_VOYAGE,
    MAX_VARIABLE_COST_PER_PALLET,
    NO_BREAKEVEN,
    business,
    business_matrix,
)
from freshswitch_backend.data.loader import get_scenario


@pytest.fixture(scope="module")
def scenario():
    found = get_scenario("default")
    assert found is not None
    return found


def _containers(result):
    return {c.container_id: c for v in result.voyages for c in v.containers}


def test_settlement3_voyage_breakeven_uses_operational_savings_and_billing_rule(scenario) -> None:
    result = business(scenario)
    strawberry = _containers(result)["CA-01"]
    assert strawberry.margin_basis == "operational"
    assert strawberry.platform_basis == "tariff"
    assert strawberry.margin_per_pallet == 762_440
    assert strawberry.breakeven_savings_pallets == 11.88
    ops_per_container = DEFAULT_OPS_COST_PER_VOYAGE / 2
    assert strawberry.breakeven_platform_pallets == round(ops_per_container / 62_250, 2)
    assert strawberry.breakeven_platform_fill > 1
    shared = business(scenario, forwarder_share=0.4)
    assert _containers(shared)["CA-01"].breakeven_platform_pallets == round(
        ops_per_container / (62_250 * 0.6), 2
    )
    grape = _containers(result)["CA-02"]
    assert grape.platform_basis == "usage_fee"
    assert grape.breakeven_platform_pallets == 72.12


def test_settlement4_matrix_with_large_variable_cost_does_not_fail(scenario) -> None:
    matrix = business_matrix(scenario, variable_cost_per_pallet=130_000)
    assert matrix is not None
    full = [r for r in matrix.rows if r.fixed_level == "full"]
    assert all(r.breakeven_pallets is None for r in full)
    assert any(NO_BREAKEVEN in h.summary for h in matrix.highlights)


def test_settlement4_variable_cost_query_upper_bound(client: TestClient) -> None:
    for path in ("/api/analysis/business/matrix", "/api/analysis/business"):
        over = client.get(
            path, params={"variable_cost_per_pallet": MAX_VARIABLE_COST_PER_PALLET + 1}
        )
        assert over.status_code == 422, path
    big = client.get("/api/analysis/business/matrix", params={"variable_cost_per_pallet": 130_000})
    assert big.status_code == 200, big.text


@pytest.mark.parametrize("variable", [0, 20_000, 130_000])
def test_settlement5_variable_cost_matches_matrix(scenario, variable: int) -> None:
    result = business(scenario, forwarder_share=0.4, variable_cost_per_pallet=variable)
    matrix = business_matrix(scenario, variable_cost_per_pallet=variable)
    assert matrix is not None
    row = next(
        r
        for r in matrix.rows
        if r.fee_rate == 0.25
        and r.base_fee_per_pallet == 0
        and r.forwarder_share == 0.4
        and r.fixed_level == "full"
        and r.air_regime == "normal"
    )
    total = next(b for b in result.basis if b.basis == "total_cost")
    assert total.variable_cost_per_pallet == variable
    assert total.breakeven_pallets == row.breakeven_pallets
    season = result.season
    assert season is not None
    pallets = sum(line.pallets for line in season.lines)
    assert pallets == row.pallets
    assert season.variable_cost == variable * pallets == row.variable_cost
    assert season.platform_net == (
        season.revenue - season.forwarder_share - season.variable_cost - ANNUAL_FIXED_COST
    )
    assert season.platform_net == pytest.approx(row.net, abs=pallets)
    assert (season.breakeven_pallets is None) == (row.breakeven_pallets is None)
    for voyage in result.voyages:
        kept = voyage.fee_revenue * 0.6 - voyage.variable_cost
        expected = round(voyage.ops_cost / (kept / voyage.containers_used), 2) if kept > 0 else None
        assert voyage.breakeven_containers == expected


def test_settlement5_default_numbers_unchanged(scenario) -> None:
    result = business(scenario)
    assert result.voyages[0].breakeven_containers == 4.07
    assert result.season is not None
    assert result.season.breakeven_pallets == 4788
    assert [b.breakeven_pallets for b in result.basis] == [2536, 4788]
