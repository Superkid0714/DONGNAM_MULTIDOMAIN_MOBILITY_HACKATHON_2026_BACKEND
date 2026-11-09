"""Focused pricing tests; no database, live provider call, or production writes."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from freshswitch_backend.api import compare, settlement
from freshswitch_backend.api.auth import current_org_id, current_principal
from freshswitch_backend.config import Settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Quantity
from freshswitch_backend.execution.quality import ArrivalRequest, arrival_settlement
from freshswitch_backend.forecast import quote
from freshswitch_backend.forecast.ml.adapter import clear_delay_model_cache
from freshswitch_backend.main import create_app
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.settlement import charge, lot_settle_cost
from freshswitch_backend.optimize.solo import sea_solo_cost
from freshswitch_backend.planning.compare import compare_air_sea
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.replan.engine import apply_event
from freshswitch_backend.replan.models import DelayEvent


@pytest.fixture
def scenario():
    scenario = get_scenario("default").model_copy(deep=True)
    scenario.lots = scenario.lots[:3]
    scenario.cost_params.sea_fixed_pickup = False
    scenario.cost_params.sea_shipper_document_krw.value = 0
    scenario.cost_params.sea_cfs_per_pallet_krw.value = 0
    # Historical algorithm tests explicitly own the old parameter fixture.
    scenario.cost_params.sea_freight_per_container_krw.value = 3320000
    scenario.cost_params.sea_ancillary_per_container_krw.value = 8422256
    scenario.cost_params.air_freight_per_kg_krw.value = 1681
    scenario.cost_params.air_fuel_surcharge_per_kg_krw.value = 470
    pc = scenario.cost_params.product_costs["strawberry"]
    pc.sea_base_loss_rate.value = 0.015
    pc.max_sea_loss_rate.value = 0.07
    pc.early_harvest_loss_rate_per_day.value = 0.02
    for voyage in scenario.voyages:
        voyage.delay_p90_h = 0
        voyage.delay_forecast_source = "provided"
    return scenario


def shared_plan(scenario):
    return build_plan(
        scenario, fixed={lot.id: "CA-01" for lot in scenario.lots}, require_savings=False
    ).plan


def test_shared_cost_per_pallet_decreases_with_actual_load(scenario):
    model = CostModel(scenario)
    lot = scenario.lots[0]
    assignment = SimpleNamespace(mode="sea", container_id="CA-01", pull_forward_days=0)
    costs = [
        lot_settle_cost(model, lot, assignment, {"CA-01": n}).total / lot.pallets
        for n in (8, 17, 20)
    ]
    assert costs[0] > costs[1] > costs[2]


def test_full_reference_container_transport_is_seventy_percent(scenario):
    for lot, pallets in zip(scenario.lots, (8, 6, 6), strict=True):
        lot.pallets = pallets
    plan = shared_plan(scenario)
    transport = sum(
        c.operational.freight + c.operational.handling + c.operational.collection
        for c in plan.shipper_costs
    )
    assert plan.total_collection_route == 755_054
    assert transport == pytest.approx(20 * (1681 + 470) * 415 * 0.7, abs=2)


@pytest.mark.parametrize(
    "delay,shift,pull,expected",
    [
        (0, 0, 0, 0.015),
        (2.75, 0, 0, 0.0425),
        (5.5, 0, 0, 0.07),
        (48, 0, 0, 0.07),
        (5.5, 5.5, 0, 0.015),
        (5.5, 0, 2, 0.07),
    ],
)
def test_hourly_loss_and_final_cap(scenario, delay, shift, pull, expected):
    voyage = scenario.voyages[0]
    voyage.delay_p90_h = delay
    voyage.truck_departure_adjustment_h = shift
    model = CostModel(scenario)
    assert model.sea_loss_rate(scenario.lots[0], voyage, pull) == pytest.approx(expected)


def test_tariff_changes_do_not_change_quote_or_settlement(scenario):
    before = shared_plan(scenario)
    tariff = scenario.cost_params.pallet_tariffs["strawberry"]
    tariff.air_freight.value = 1
    tariff.sea_freight.value = 999_999_999
    tariff.sea_loss_risk.value = 999_999_999
    for lot in scenario.lots:
        lot.sell_window = "short"
    after = shared_plan(scenario)
    assert after.total_shared_cost == before.total_shared_cost
    assert after.total_baseline_cost == before.total_baseline_cost
    assert after.total_fee == before.total_fee
    assert after.settlement_tariffs == []
    assert all(
        row.basis == "operational" and row.baseline_kind == "air_solo" for row in after.settlements
    )


@pytest.mark.parametrize("surcharge", [470, 2060])
def test_dedicated_sea_has_no_air_and_costs_at_least_shared(scenario, surcharge, monkeypatch):
    from freshswitch_backend.optimize import solo, solver

    scenario.cost_params.air_fuel_surcharge_per_kg_krw.value = surcharge
    solo._sea_solo.cache_clear()
    original = solver.solve_plan
    called = []

    def capture(*args, **kwargs):
        if kwargs.get("compute_solo") is False:
            called.append(kwargs["allow_air"])
        return original(*args, **kwargs)

    monkeypatch.setattr(solver, "solve_plan", capture)
    plan = shared_plan(scenario)
    row = next(x for x in plan.settlements if x.shipper_id == scenario.lots[0].shipper_id)
    assert row.sea_solo_cost >= row.shared_cost
    assert called and not any(called)
    assert row.air_cost == row.baseline_cost
    assert row.shipper_net_saving == row.air_cost - row.shared_cost - row.fee


def test_monthly_air_rate_and_loss_are_operational(scenario):
    lot = scenario.lots[0]
    key = lot.ready_date.strftime("%Y-%m")
    scenario.cost_params.air_fuel_surcharge_by_month[key] = Quantity(
        value=2060, unit="원/kg", source="test"
    )
    model = CostModel(scenario)
    air = model.settle_air_cost(lot)
    assert air.freight == lot.pallets * 415 * (1681 + 2060)
    assert air.loss_risk == pytest.approx(lot.pallets * 415 * 18000 * 0.01)


@pytest.mark.parametrize("savings", [-1, 0, 10_000])
def test_fee_zero_when_shared_plus_usage_fee_exceeds_air(scenario, savings):
    bill = charge(scenario.cost_params, "usage_fee", savings, 0, 1)
    assert bill.fee == bill.usage_fee == bill.performance_fee == 0


def test_container_fee_totals_match_shipper_cap_across_multiple_containers(scenario):
    scenario.lots[1].shipper_id = scenario.lots[0].shipper_id
    plan = build_plan(
        scenario,
        fixed={
            scenario.lots[0].id: "CA-01",
            scenario.lots[1].id: "CA-03",
            scenario.lots[2].id: "CA-01",
        },
        require_savings=False,
    ).plan
    for shipper in plan.settlements:
        rows = [
            row
            for c in plan.container_settlements
            for row in c.shippers
            if row.shipper_id == shipper.shipper_id
        ]
        assert sum(row.fee for row in rows) == shipper.fee
    assert sum(c.total_fee for c in plan.container_settlements) == plan.total_fee


def test_insufficient_sell_days_is_candidate_without_fake_loss(scenario):
    lot = scenario.lots[0]
    voyage = scenario.voyages[0]
    lot.sell_by_date = (voyage.etd + timedelta(days=voyage.transit_days)).date()
    plan = shared_plan(scenario)
    result = compare_air_sea(scenario, plan, lot.id)
    assert result.air_switch_candidate
    model = CostModel(scenario)
    assert model.sea_loss_rate(lot, voyage) == 0.015


def test_dedicated_sea_infeasible_returns_null_not_air_price(scenario):
    scenario.lots[0].sell_by_date = scenario.lots[0].ready_date - timedelta(days=1)
    assert sea_solo_cost(scenario, [scenario.lots[0].id]) is None


def test_forecast_p90_survives_replan_event(scenario):
    voyage = scenario.voyages[0]
    event = DelayEvent(
        voyage_id=voyage.id,
        new_etd=voyage.etd + timedelta(hours=2),
        delay_p90_h=5.5,
        delay_forecast_source="model",
        truck_departure_adjustment_h=2.75,
    )
    changed = apply_event(scenario, event)
    model = CostModel(changed)
    assert model.voyages[voyage.id].delay_p90_h == 5.5
    assert model.sea_loss_rate(scenario.lots[0], model.voyages[voyage.id]) == pytest.approx(0.0425)


def test_quote_predictor_missing_models_falls_back_to_stat(scenario, monkeypatch, tmp_path):
    settings = Settings(_env_file=None, feature_p1_delay_ml=True, delay_ml_model_dir=tmp_path)
    monkeypatch.setattr(quote, "get_settings", lambda: settings)
    scenario.voyages[0].delay_p90_h = None
    scenario.voyages[0].call_sign = "5LOE3"
    result = quote.prepare_quote_scenario(scenario).voyages[0]
    assert result.delay_forecast_source == "stat"
    assert isinstance(result.delay_p90_h, float) and result.delay_p90_h >= 0


def test_actual_ml_p90_reaches_hourly_loss(scenario, monkeypatch):
    settings = Settings(_env_file=None, feature_p1_delay_ml=True)
    monkeypatch.setattr(quote, "get_settings", lambda: settings)
    history = (
        quote._history(quote.HISTORY_CSV.stat().st_mtime_ns)
        if quote.HISTORY_CSV.is_file()
        else None
    )
    if history is None or history.empty:
        pytest.skip("ML history CSV is not supplied")
    clear_delay_model_cache()
    scenario.voyages[0].delay_p90_h = None
    scenario.voyages[0].call_sign = str(history.iloc[0]["clsgn"])
    result = quote.prepare_quote_scenario(scenario)
    voyage = result.voyages[0]
    assert voyage.delay_forecast_source == "model"
    model = CostModel(result)
    expected = 0.015 + min(voyage.delay_p90_h / 5.5, 1) * (0.07 - 0.015)
    assert model.sea_loss_rate(scenario.lots[0], voyage) == pytest.approx(expected)


def test_refund_uses_total_fee_and_updates_quote_metrics(scenario):
    plan = shared_plan(scenario)
    lot = scenario.lots[0]
    result, _ = arrival_settlement(
        "p",
        scenario,
        plan,
        [],
        ArrivalRequest(lot_id=lot.id, inspected_loss_pct=100),
        scenario.voyages[0].etd,
    )
    assert result.refund_after == result.settlement_before.fee
    assert result.settlement_after.fee == 0
    assert result.settlement_after.shipper_net_saving == result.settlement_after.shipper_gain
    assert "총수수료" in result.rule


def test_compare_and_settlement_http_expose_real_service_amounts(scenario, monkeypatch):
    plan = shared_plan(scenario)
    app = FastAPI()
    app.state.settings = Settings(_env_file=None)
    app.include_router(compare.router)
    app.include_router(settlement.router)
    app.dependency_overrides[current_principal] = lambda: None
    app.dependency_overrides[current_org_id] = lambda: "test-org"
    monkeypatch.setattr(compare, "_source", lambda *args: (scenario, plan))
    monkeypatch.setattr(
        settlement.store,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            scenario=scenario, plan=plan, version=1, arrival_inspections=[]
        ),
    )
    client = TestClient(app)
    response = client.post("/compare/air-vs-sea", json={"lot_id": scenario.lots[0].id})
    assert response.status_code == 200, response.text
    from freshswitch_backend.planning.robust.display import display_plan

    projected = display_plan(scenario, plan)
    row = next(s for s in projected.settlements if s.shipper_id == scenario.lots[0].shipper_id)
    assert response.json()["shared_cost"] == row.shared_cost
    assert response.json()["sea_solo_cost"] == row.sea_solo_cost
    response = client.get("/plans/p/settlement")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["shared_cost"] == projected.total_shared_cost
    assert body["fee"] == projected.total_fee
    assert body["air_cost"] == projected.total_baseline_cost
    assert all(s["basis"] == "operational" for s in body["settlements"])


def test_saas_compare_still_requires_login():
    app = create_app(Settings(_env_file=None, saas_mode=True))
    response = TestClient(app).post(
        "/api/compare/air-vs-sea",
        json={"lot_id": "anything"},
        headers={"X-Requested-With": "freshswitch"},
    )
    assert response.status_code == 401


def test_legacy_tariff_snapshot_is_repriced_without_mutating_saved_plan(scenario):
    from freshswitch_backend.planning.service import operational_quote_plan

    current = shared_plan(scenario)
    legacy = current.model_copy(deep=True)
    legacy.settlements[0].basis = "tariff"
    legacy.settlements[0].shared_cost = 1
    legacy.settlements[0].air_cost = None
    repriced = operational_quote_plan(scenario, legacy)
    assert repriced.settlements[0].basis == "operational"
    assert repriced.settlements[0].shared_cost == current.settlements[0].shared_cost
    assert legacy.settlements[0].shared_cost == 1
