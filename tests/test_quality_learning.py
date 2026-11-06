import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import LossAdjustment
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.quality import quality_history
from freshswitch_backend.quality.learning import with_adjustments

DEFAULT_SAVINGS = 10318577


@pytest.fixture(autouse=True)
def fresh_history():
    quality_history.reset()
    yield
    quality_history.reset()


def _cell(table: dict, product: str = "strawberry", mode: str = "sea") -> dict:
    return next(
        c
        for c in table["cells"]
        if c["product_id"] == product and c["mode"] == mode and c["destination"] == "HKHKG"
    )


def _poor(client: TestClient, pct: float = 9.0, pallets: int = 8) -> dict:
    response = client.post(
        "/api/quality/observations",
        json={
            "product_id": "strawberry",
            "destination": "HKHKG",
            "mode": "sea",
            "pallets": pallets,
            "actual_loss_pct": pct,
            "outage_minutes": 300,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_seed_is_labelled_and_shrinks_toward_prior(client: TestClient) -> None:
    listing = client.get("/api/quality/observations").json()
    assert listing["count"] == 6
    assert "가정/시드" in listing["seed_label"]
    assert all(o["origin"] == "seed" for o in listing["observations"])
    assert all(o["source"].startswith("가정/시드") for o in listing["observations"])
    table = client.get("/api/quality/loss-rates").json()
    cell = _cell(table)
    expected = (20 * 0.015 + 4 * 0.012 + 4 * 0.018 + 2 * 0.07) / 30
    assert cell["posterior_rate"] == round(expected, 6)
    assert cell["prior_rate"] == 0.015
    assert cell["range_low"] < cell["posterior_rate"] < cell["range_high"]
    assert [b["key"] for b in cell["outage_breakdown"]] == ["none", "long"]
    assert _cell(table, "grape_so2", "air")["observations"] == 0
    assert _cell(table, "grape_so2", "air")["delta"] == 0
    assert len(table["adjustments"]) == 1


def test_poor_arrivals_raise_rate_and_wait_effect_is_removed(client: TestClient) -> None:
    before = _cell(client.get("/api/quality/loss-rates").json())
    added = _poor(client)
    assert added["outage_bucket"] == "long"
    assert added["condition_uplift"] == 0
    waited = client.post(
        "/api/quality/observations",
        json={"product_id": "strawberry", "pallets": 4, "actual_loss_pct": 3.3, "wait_days": 2},
    ).json()
    assert waited["condition_uplift"] == pytest.approx(0.018)
    assert waited["adjusted_loss_rate"] == pytest.approx(0.015)
    assert waited["planned_loss_rate"] == pytest.approx(0.033)
    after = _cell(client.get("/api/quality/loss-rates").json())
    assert after["observations"] == before["observations"] + 2
    assert after["posterior_rate"] > before["posterior_rate"]
    w = after["prior_weight_pallets"]
    total = 20 * 0.015 + 4 * 0.012 + 4 * 0.018 + 2 * 0.07 + 8 * 0.09 + 4 * 0.015
    assert after["posterior_rate"] == round(total / (w + 22), 6)


def test_empty_history_keeps_prior(client: TestClient) -> None:
    assert client.post("/api/quality/reset", params={"seed": False}).json()["seeded"] == 0
    table = client.get("/api/quality/loss-rates").json()
    assert table["total_observations"] == 0
    assert table["adjustments"] == []
    assert all(c["posterior_rate"] == c["prior_rate"] for c in table["cells"])
    plan = client.post("/api/plans", json={"use_learned_loss": True}).json()
    assert plan["plan"]["total_savings"] == DEFAULT_SAVINGS
    reset = client.post("/api/quality/reset").json()
    assert reset == {"cleared": 0, "seeded": 6}


def test_flag_off_unchanged_and_flag_on_uses_learned_rates(client: TestClient) -> None:
    _poor(client)
    default = client.post("/api/plans", json={"scenario_id": "default"}).json()
    assert default["plan"]["total_savings"] == DEFAULT_SAVINGS
    assert default["scenario"]["cost_params"]["loss_adjustments"] == []
    learned = client.post("/api/plans", json={"use_learned_loss": True}).json()
    adjustments = learned["scenario"]["cost_params"]["loss_adjustments"]
    assert {(a["product_id"], a["mode"]) for a in adjustments} == {("strawberry", "sea")}
    assert learned["plan"]["total_savings"] < DEFAULT_SAVINGS
    strawberry = {"sh-jinju", "sh-sancheong", "sh-miryang"}
    by_id = {s["shipper_id"]: s for s in default["plan"]["settlements"]}
    for s in learned["plan"]["settlements"]:
        if s["shipper_id"] in strawberry:
            assert s["savings"] < by_id[s["shipper_id"]]["savings"]
        else:
            assert s["savings"] == by_id[s["shipper_id"]]["savings"]
    plan_id = learned["plan_id"]
    arrival = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": "lot-jinju-sb-1", "inspected_loss_pct": 1.0},
    ).json()
    assert arrival["planned_loss_rate"] > 0.033
    observation = client.get("/api/quality/observations").json()["observations"][-1]
    assert observation["condition_uplift"] == pytest.approx(0.018)


def test_impact_compares_prior_and_learned(client: TestClient) -> None:
    _poor(client)
    body = client.get("/api/quality/impact").json()
    assert body["total_savings_prior"] == DEFAULT_SAVINGS
    assert body["total_savings_change"] == body["total_savings_learned"] - DEFAULT_SAVINGS
    assert body["total_savings_change"] < 0
    shippers = {s["shipper_id"]: s for s in body["shippers"]}
    assert shippers["sh-jinju"]["savings_change"] < 0
    assert shippers["sh-jinju"]["loss_risk_learned"] > shippers["sh-jinju"]["loss_risk_prior"]
    assert shippers["sh-sangju"]["savings_change"] == 0
    assert 0 < shippers["sh-jinju"]["air_switch_margin_rate"] < 1
    assert body["lot_shifts"] == []
    assert "학습 손실률 1칸" in body["summary"]
    assert client.get("/api/quality/impact", params={"scenario_id": "nope"}).status_code == 404


def test_repeated_poor_arrivals_shift_strawberry_to_air(client: TestClient) -> None:
    for _ in range(3):
        _poor(client)
    body = client.get("/api/quality/impact").json()
    shifts = {s["lot_id"]: (s["before"], s["after"]) for s in body["lot_shifts"]}
    assert shifts == {
        "lot-jinju-sb-1": ("해상 V-2611A", "항공"),
        "lot-sancheong-sb-1": ("해상 V-2611A", "항공"),
        "lot-miryang-sb-1": ("해상 V-2611A", "항공"),
    }
    shippers = {s["shipper_id"]: s for s in body["shippers"]}
    assert shippers["sh-jinju"]["air_pallets_learned"] == 8
    assert shippers["sh-jinju"]["air_switch_margin_rate"] is None
    assert shippers["sh-pohang"]["savings_change"] == 0


def test_arrival_hook_is_idempotent_and_reads_outage(
    client: TestClient, reset_admin_headers
) -> None:
    plan_id = client.post("/api/plans", json={}).json()["plan_id"]
    run = client.post(
        "/api/coldchain/simulate",
        json={
            "preset": "power_cut_restore",
            "container_id": "CA-01",
            "plan_id": plan_id,
            "device_id": "arduino-q",
        },
    )
    assert run.status_code in (200, 201), run.text
    for pct in (8.0, 9.0):
        response = client.post(
            f"/api/plans/{plan_id}/settlement/arrival",
            json={"lot_id": "lot-jinju-sb-1", "inspected_loss_pct": pct},
        )
        assert response.status_code == 200
    arrivals = [
        o
        for o in client.get("/api/quality/observations").json()["observations"]
        if o["origin"] == "arrival"
    ]
    assert len(arrivals) == 1
    obs = arrivals[0]
    assert obs["actual_loss_rate"] == 0.09
    assert obs["outage_minutes"] == 240.0
    assert obs["outage_bucket"] == "long"
    assert obs["mix"] == "shared_same_product"
    assert obs["so2_in_container"] is False
    assert obs["container_id"] == "CA-01"
    assert obs["pallets"] == 8
    grape = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": "lot-sangju-gr-1", "inspected_loss_pct": 1.0},
    )
    assert grape.status_code == 200
    last = client.get("/api/quality/observations").json()["observations"][-1]
    assert last["outage_bucket"] == "unknown"
    assert last["so2_in_container"] is True
    table = client.get("/api/quality/loss-rates").json()
    assert _cell(table)["origins"] == {"seed": 3, "arrival": 1}
    client.post("/api/demo/reset", headers=reset_admin_headers)
    assert client.get("/api/quality/observations").json()["count"] == 6


def test_manual_observation_validation(client: TestClient) -> None:
    bad = client.post(
        "/api/quality/observations",
        json={"product_id": "banana", "pallets": 1, "actual_loss_pct": 2},
    )
    assert bad.status_code == 422
    over = client.post(
        "/api/quality/observations",
        json={"product_id": "strawberry", "pallets": 1, "actual_loss_pct": 120},
    )
    assert over.status_code == 422
    short = client.post(
        "/api/quality/observations",
        json={"product_id": "strawberry", "pallets": 1, "actual_loss_pct": 2, "outage_minutes": 60},
    ).json()
    assert short["outage_bucket"] == "short"
    assert short["origin"] == "manual"


def test_cost_model_applies_adjustment_by_route_and_mode() -> None:
    scenario = get_scenario("default")
    lot = next(x for x in scenario.lots if x.id == "lot-jinju-sb-1")
    base = CostModel(scenario)
    voyage = base.voyages["V-2611A"]
    adjusted = CostModel(
        with_adjustments(
            scenario,
            [
                LossAdjustment(
                    product_id="strawberry", destination="HKHKG", mode="sea", delta=0.02
                ),
                LossAdjustment(product_id="strawberry", destination="SGSIN", mode="sea", delta=0.5),
                LossAdjustment(
                    product_id="strawberry", destination="HKHKG", mode="air", delta=-0.5
                ),
            ],
        )
    )
    assert adjusted.sea_loss_rate(lot, voyage) == pytest.approx(
        base.sea_loss_rate(lot, voyage) + 0.02
    )
    assert adjusted.air_loss_rate(lot) == 0.0
    assert base.sea_lot_cost(lot, voyage).loss_risk == pytest.approx(
        base.lot_value(lot) * base.sea_loss_rate(lot, voyage)
    )
