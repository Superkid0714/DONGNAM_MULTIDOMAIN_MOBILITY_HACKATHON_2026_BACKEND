import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.optimize.settlement import billing_rule, usage_fee
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.planning.service import build_plan

INTAKE = "/api/intake/lots"


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


def _body(lot, **extra) -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "shipper": lot.shipper_id,
        "lot_id": f"{lot.id}-new",
        **extra,
    }


def _lot(scenario: Scenario, lot_id: str):
    return next(lot for lot in scenario.lots if lot.id == lot_id)


def test_strawberry_estimate_matches_plan_settlement(client: TestClient, scenario: Scenario):
    response = client.post(INTAKE, json=_body(_lot(scenario, "lot-jinju-sb-1")))
    assert response.status_code == 200, response.text
    estimate = response.json()["savings_estimate"]
    settled = next(
        s
        for s in build_plan(scenario.model_copy(deep=True)).plan.settlements
        if s.shipper_id == "sh-jinju"
    )
    assert estimate["mode"] == "sea"
    assert estimate["basis"] == "tariff"
    assert estimate["billing_rule"] == "performance"
    assert estimate["baseline_cost"] == settled.baseline_cost
    assert estimate["shared_cost"] == settled.shared_cost
    assert estimate["savings"] == settled.savings
    assert estimate["fee"] == settled.fee
    assert estimate["shipper_gain"] == settled.shipper_gain
    assert estimate["savings_per_pallet"] == round(settled.savings / 8)
    assert estimate["assumptions"] and "20%" in estimate["rule"]


PULL_FORWARD_NOTE = "출하를 1일 앞당긴다고 가정(조기 수확 손실 포함)"


def test_grape_uses_pallet_usage_fee(saas_login, scenario: Scenario):
    sangju = saas_login(shipper_org_id("sh-sangju"))
    response = sangju.post(INTAKE, json=_body(_lot(scenario, "lot-sangju-gr-1")))
    assert response.status_code == 200, response.text
    estimate = response.json()["savings_estimate"]
    assert estimate["mode"] == "sea"
    assert estimate["basis"] == "operational"
    assert estimate["billing_rule"] == "usage_fee"
    assert estimate["fee"] == 20_000 * 13
    assert estimate["shipper_gain"] == estimate["savings"] - estimate["fee"]
    assert estimate["savings"] == estimate["baseline_cost"] - estimate["shared_cost"] > 0
    assert estimate["assumed_container_pallets"] == 17
    assert "기획서" in estimate["rule"] and "성과보수 없음" in estimate["rule"]
    assert any("85%" in note for note in estimate["assumptions"])


def test_lot_after_every_cutoff_estimates_air_without_savings(
    client: TestClient, scenario: Scenario
):
    body = _body(
        _lot(scenario, "lot-miryang-sb-2"), ready_date="2026-11-24", sell_by_date="2026-11-30"
    )
    response = client.post(INTAKE, json=body)
    assert response.status_code == 200, response.text
    estimate = response.json()["savings_estimate"]
    assert estimate["mode"] == "air"
    assert estimate["savings"] == estimate["fee"] == estimate["shipper_gain"] == 0
    assert estimate["shared_cost"] == estimate["baseline_cost"]
    assert estimate["container_id"] is None


def test_shipper_session_gets_own_estimate(shipper_client: TestClient, scenario: Scenario):
    response = shipper_client.post(INTAKE, json=_body(_lot(scenario, "lot-jinju-sb-1")))
    assert response.status_code == 200, response.text
    estimate = response.json()["savings_estimate"]
    assert estimate["mode"] == "sea"
    assert estimate["pallets"] == 8
    assert "산청" not in response.text


def test_demo_grape_uses_usage_fee_like_settlement(client: TestClient, scenario: Scenario):
    response = client.post(INTAKE, json=_body(_lot(scenario, "lot-sangju-gr-1")))
    assert response.status_code == 200, response.text
    estimate = response.json()["savings_estimate"]
    params = scenario.cost_params
    assert estimate["mode"] == "sea"
    assert estimate["billing_rule"] == billing_rule(params, "grape_so2") == "usage_fee"
    assert estimate["fee"] == usage_fee(params, 13)
    assert estimate["shipper_gain"] == estimate["savings"] - estimate["fee"]
    settled = next(
        s
        for s in build_plan(scenario.model_copy(deep=True)).plan.settlements
        if s.shipper_id == "sh-sangju"
    )
    assert settled.billing_rule == "usage_fee"
    assert estimate["fee"] == settled.fee
    assert estimate["baseline_cost"] == settled.baseline_cost


def test_no_plan_estimate_skips_pull_forward_options(client: TestClient, scenario: Scenario):
    response = client.post(INTAKE, json=_body(_lot(scenario, "lot-miryang-sb-2")))
    assert response.status_code == 200, response.text
    body = response.json()
    hints = {o["container_id"]: o for o in body["hints"]["sea_options"]}
    assert hints["CA-01"]["feasible"] and hints["CA-01"]["pull_forward_days"] == 1
    estimate = body["savings_estimate"]
    assert estimate["mode"] == "sea"
    assert estimate["container_id"] == "CA-03"
    assert hints["CA-03"]["pull_forward_days"] == 0
    assert not any("앞당긴다고" in note for note in estimate["assumptions"])
    assert any("항공으로 배정될 수 있습니다" in note for note in estimate["assumptions"])


def test_plan_estimate_discloses_pull_forward(client: TestClient, scenario: Scenario):
    created = client.post("/api/plans", json={"scenario_id": "default"})
    assert created.status_code in (200, 201), created.text
    plan_id = created.json()["plan_id"]
    body = _body(_lot(scenario, "lot-miryang-sb-2"), plan_id=plan_id)
    response = client.post(INTAKE, json=body)
    assert response.status_code == 200, response.text
    estimate = response.json()["savings_estimate"]
    assert estimate["mode"] == "sea"
    assert estimate["container_id"] == "CA-01"
    assert PULL_FORWARD_NOTE in estimate["assumptions"]
