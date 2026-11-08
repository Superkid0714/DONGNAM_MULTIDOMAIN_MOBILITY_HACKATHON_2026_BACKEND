import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Assignment, Scenario
from freshswitch_backend.optimize import AIR, solve_plan
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.settlement import shipper_costs_and_settlements, tariff_sheet
from freshswitch_backend.replan import AirSwitchEvent, locations, replan

CA01_TABLE = {
    "sh-jinju": (8, 1_992_000, 498_000, 1_494_000),
    "sh-sancheong": (5, 1_245_000, 311_250, 933_750),
    "sh-miryang": (4, 996_000, 249_000, 747_000),
}


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


def _man(won: int) -> float:
    return round(won / 10_000, 1)


def test_strawberry_tariff_sheet_matches_spec(scenario: Scenario) -> None:
    model = CostModel(scenario)
    sheet = tariff_sheet(model, "strawberry", scenario.cost_params.pallet_tariffs["strawberry"])
    lines = {line.key: (line.air, line.sea) for line in sheet.lines}
    assert lines == {
        "freight": (830_000, 166_000),
        "inland": (20_000, 435_000),
        "loss_risk": (119_000, 119_000),
    }
    assert (sheet.air_total, sheet.sea_total) == (969_000, 720_000)
    assert sheet.savings_per_pallet == 249_000
    assert (sheet.fee_per_pallet, sheet.shipper_share_per_pallet) == (62_250, 186_750)
    assert (sheet.sea_loss_risk_short_window, sheet.sea_total_short_window) == (
        1_191_000,
        1_792_000,
    )
    assert set(sheet.assumed_items) == {
        "air.loss_risk",
        "sea.inland",
        "sea.loss_risk",
        "sea.loss_risk_short_window",
    }


def test_default_plan_ca01_settlement_matches_table(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    view = client.get(f"/api/plans/{plan_id}/settlement").json()
    ca01 = next(c for c in view["containers"] if c["container_id"] == "CA-01")
    rows = {
        r["shipper_id"]: (r["pallets"], r["savings"], r["fee"], r["shipper_gain"])
        for r in ca01["shippers"]
    }
    assert rows == CA01_TABLE
    assert [r["shipper_id"] for r in ca01["shippers"]] == list(CA01_TABLE)
    assert all(r["basis"] == "tariff" for r in ca01["shippers"])
    assert (ca01["pallets"], ca01["total_savings"]) == (17, 4_233_000)
    assert (ca01["total_fee"], ca01["total_shipper_gain"]) == (1_058_250, 3_174_750)
    assert [_man(v) for v in (4_233_000, 1_058_250, 3_174_750)] == [423.3, 105.8, 317.5]
    assert [(_man(s), _man(f), _man(g)) for _, s, f, g in CA01_TABLE.values()] == [
        (199.2, 49.8, 149.4),
        (124.5, 31.1, 93.4),
        (99.6, 24.9, 74.7),
    ]
    assert "strawberry.sea.inland" in view["assumed_items"]
    assert "strawberry.sea.loss_risk" in view["assumed_items"]


def test_short_sell_window_sends_lot_by_air(scenario: Scenario) -> None:
    short = scenario.model_copy(
        update={
            "lots": [
                lot.model_copy(update={"sell_window": "short"})
                if lot.id == "lot-sancheong-sb-1"
                else lot
                for lot in scenario.lots
            ]
        }
    )
    plan = solve_plan(short)
    assert locations(plan)["lot-sancheong-sb-1"] == AIR
    assert locations(plan)["lot-jinju-sb-1"] == "CA-01"
    sancheong = next(s for s in plan.settlements if s.shipper_id == "sh-sancheong")
    assert (sancheong.savings, sancheong.fee) == (0, 0)


def test_air_switch_event_marks_short_window_and_engine_picks_air(scenario: Scenario) -> None:
    plan = solve_plan(scenario)
    result = replan(scenario, plan, AirSwitchEvent(lot_id="lot-sancheong-sb-1"))
    lot = next(x for x in result.scenario_after.lots if x.id == "lot-sancheong-sb-1")
    assert lot.sell_window == "short"
    assert "lot-sancheong-sb-1" in result.free_lot_ids
    for alt in result.alternatives:
        assert locations(alt.plan)["lot-sancheong-sb-1"] == AIR


def test_container_row_basis_uses_only_lots_in_that_container(scenario: Scenario) -> None:
    lots = {
        lot.id: lot.model_copy(update={"shipper_id": "sh-jinju"})
        for lot in scenario.lots
        if lot.id in ("lot-jinju-sb-1", "lot-pohang-gr-1")
    }
    placed = {"lot-jinju-sb-1": "CA-01", "lot-pohang-gr-1": "CA-02"}
    assignments = [
        Assignment(
            lot_id=lot.id,
            shipper_id=lot.shipper_id,
            product_id=lot.product_id,
            pallets=lot.pallets,
            mode="sea",
            container_id=placed[lot.id],
            voyage_id="V-2611A",
        )
        for lot in lots.values()
    ]
    shared = scenario.model_copy(update={"lots": list(lots.values())})
    result = shipper_costs_and_settlements(CostModel(shared), lots, assignments)
    rows = {c.container_id: c.shippers[0].basis for c in result.containers}
    assert rows == {"CA-01": "tariff", "CA-02": "operational"}
    assert [s.basis for s in result.settlements] == ["mixed"]
