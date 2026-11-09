import json

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, accept_plan

from freshswitch_backend.analysis.business import (
    DEFAULT_FORWARDER_SHARE,
    FORWARDER_SHARE_SOURCE,
    business,
    business_matrix,
)
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import ContainerShipperSettlement, Scenario, Settlement
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.settlement import tariff_sheet, uses_usage_fee
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.replan.engine import replan
from freshswitch_backend.replan.models import ReduceEvent

GRAPE_SHIPPERS = {"sh-sangju", "sh-pohang"}
STRAWBERRY_SHIPPERS = {"sh-jinju", "sh-sancheong", "sh-miryang"}


def _scenario() -> Scenario:
    scenario = get_scenario("default")
    assert scenario is not None
    return scenario


def _with_billing(scenario: Scenario, **billing) -> Scenario:
    params = scenario.cost_params
    updated = params.model_copy(update={"billing": params.billing.model_copy(update=billing)})
    return scenario.model_copy(update={"cost_params": updated})


def test_usage_fee_predicate_is_grape_only() -> None:
    params = _scenario().cost_params
    assert uses_usage_fee(params, "grape_so2")
    assert not uses_usage_fee(params, "strawberry")
    assert params.billing.usage_fee_per_pallet_krw.value == 20_000
    assert params.billing.usage_fee_per_pallet_krw.source.startswith("가정")
    assert params.billing.pilot is False


def test_grape_pays_pallet_usage_fee_and_strawberry_keeps_performance_fee() -> None:
    plan = build_plan(_scenario()).plan
    rows = {s.shipper_id: s for s in plan.settlements}
    for sid in GRAPE_SHIPPERS:
        row = rows[sid]
        assert row.billing_rule == "usage_fee"
        assert row.baseline_kind == "sea_solo"
        assert row.baseline_label == "단독 해상 대비"
        assert row.savings_baseline_label == "단독 항공 대비"
        assert row.performance_fee == 0
        assert row.usage_fee == 20_000 * row.sea_pallets
        assert row.fee == row.usage_fee
        assert row.shipper_gain == row.savings - row.fee
    for sid in STRAWBERRY_SHIPPERS:
        row = rows[sid]
        assert row.billing_rule == "performance"
        assert row.baseline_kind == "air_solo"
        assert row.baseline_label == "단독 항공 대비"
        assert row.usage_fee == 0
        assert row.performance_fee == round(row.savings * 0.20)
        assert row.fee == row.performance_fee
    assert all(not s.pilot for s in plan.settlements)


def test_expected_container_and_voyage_fees() -> None:
    plan = build_plan(_scenario()).plan
    containers = {c.container_id: c for c in plan.container_settlements}
    assert containers["CA-01"].total_fee == 846_600
    assert containers["CA-02"].total_fee == 360_000
    assert [(s.shipper_id, s.fee) for s in containers["CA-02"].shippers] == [
        ("sh-sangju", 260_000),
        ("sh-pohang", 100_000),
    ]
    assert all(s.billing_rule == "usage_fee" for s in containers["CA-02"].shippers)
    assert all(s.baseline_label == "단독 해상 대비" for s in containers["CA-02"].shippers)
    assert plan.total_fee == 1_206_600
    assert plan.total_savings == 10_318_577


def test_business_uses_the_same_billing_rule_as_settlement() -> None:
    scenario = _scenario()
    plan = build_plan(scenario).plan
    result = business(scenario)
    assert result.total_fee_revenue == plan.total_fee
    voyage = result.voyages[0]
    assert voyage.fee_revenue == voyage.usage_fee_rule_revenue == 1_206_600
    assert result.season is not None
    lines = {line.product_id: line for line in result.season.lines}
    assert lines["grape_so2"].revenue == 20_000 * lines["grape_so2"].pallets


def test_pilot_voyage_charges_nothing_but_keeps_original_amounts() -> None:
    plan = build_plan(_with_billing(_scenario(), pilot=True)).plan
    assert plan.total_fee == 0
    rows = {s.shipper_id: s for s in plan.settlements}
    assert rows["sh-sangju"].usage_fee == 260_000
    assert rows["sh-jinju"].performance_fee == 398_400
    for row in plan.settlements:
        assert row.pilot is True
        assert row.fee == 0
        assert row.shipper_gain == row.savings
    assert all(c.total_fee == 0 for c in plan.container_settlements)


def test_shipper_with_both_rules_is_mixed() -> None:
    scenario = _scenario()
    lots = [
        lot.model_copy(update={"shipper_id": "sh-sangju"}) if lot.id == "lot-jinju-sb-1" else lot
        for lot in scenario.lots
    ]
    plan = build_plan(scenario.model_copy(update={"lots": lots})).plan
    row = next(s for s in plan.settlements if s.shipper_id == "sh-sangju")
    assert row.billing_rule == "mixed"
    assert row.baseline_kind == "mixed"
    assert row.usage_fee == 20_000 * 13
    assert row.performance_fee > 0
    assert row.fee == row.usage_fee + row.performance_fee
    containers = {
        s.shipper_id + c.container_id: s for c in plan.container_settlements for s in c.shippers
    }
    assert containers["sh-sangjuCA-02"].billing_rule == "usage_fee"
    assert containers["sh-sangjuCA-01"].billing_rule == "performance"


def test_replan_alternatives_bill_grape_by_usage_fee() -> None:
    scenario = _scenario()
    plan = build_plan(scenario).plan
    result = replan(
        scenario, plan, ReduceEvent(type="reduce", lot_id="lot-sancheong-sb-1", new_pallets=3)
    )
    for alternative in result.alternatives:
        for row in alternative.plan.settlements:
            if row.shipper_id in GRAPE_SHIPPERS:
                assert row.billing_rule == "usage_fee"
                assert row.fee == 20_000 * row.sea_pallets


def test_forwarder_share_defaults_to_zero() -> None:
    scenario = _scenario()
    assert DEFAULT_FORWARDER_SHARE == 0.0
    result = business(scenario)
    share = next(a for a in result.assumptions if a.key == "forwarder_share")
    assert share.value == 0.0
    assert share.source == FORWARDER_SHARE_SOURCE
    assert "성과보수 배분 없음" in share.source
    assert result.voyages[0].forwarder_share == 0
    assert result.voyages[0].contribution_margin == result.voyages[0].fee_revenue
    assert result.season is not None
    assert result.season.forwarder_share == 0
    matrix = business_matrix(scenario)
    assert matrix is not None
    assert matrix.baseline["forwarder_share"] == 0.0
    base = next(
        r
        for r in matrix.rows
        if r.fee_rate == 0.20
        and r.base_fee_per_pallet == 0
        and r.forwarder_share == 0.0
        and r.fixed_level == "full"
        and r.air_regime == "normal"
    )
    assert base.needs == []
    assert base.revenue == pytest.approx(result.season.revenue, abs=1_000)
    shared = [r for r in matrix.rows if r.forwarder_share == 0.4]
    assert shared
    assert all(any("가정(포워더 배분형)" in n for n in r.needs) for r in shared)


def test_business_api_defaults_to_zero_forwarder_share(client: TestClient) -> None:
    response = client.get("/api/analysis/business", params={"scenario_id": "default"})
    assert response.status_code == 200
    voyage = response.json()["voyages"][0]
    assert voyage["forwarder_share"] == 0
    assert voyage["fee_revenue"] == 1_206_600


def test_demo_settlement_view_carries_billing_fields(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={}).json()["plan_id"]
    body = client.get(f"/api/plans/{plan_id}/settlement").json()
    assert body["total_fee"] == 1_206_600
    assert body["total_usage_fee"] == 360_000
    assert body["total_performance_fee"] == 846_600
    rows = {r["shipper_id"]: r for r in body["settlements"]}
    assert rows["sh-pohang"]["billing_rule"] == "usage_fee"
    assert rows["sh-pohang"]["baseline_label"] == "단독 해상 대비"
    assert rows["sh-jinju"]["billing_rule"] == "performance"


def _saas_plan(saas_login) -> tuple[str, dict[str, str]]:
    ids = []
    for lot in _scenario().lots:
        shipper = saas_login(shipper_org_id(lot.shipper_id))
        response = shipper.post(
            "/api/saas/shipper/lots",
            json={
                "product": lot.product_id,
                "pallets": lot.pallets,
                "ready_date": lot.ready_date.isoformat(),
                "destination": lot.destination,
                "sell_by_date": lot.sell_by_date.isoformat(),
                "sell_window": lot.sell_window,
                "lot_id": lot.id,
            },
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    created = saas_login(FORWARDER_ORG_ID).post("/api/saas/forwarder/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    accept_plan(saas_login, created.json()["plan_id"])
    lots = {lot.id: registered for lot, registered in zip(_scenario().lots, ids, strict=True)}
    return created.json()["plan_id"], lots


def test_saas_settlements_carry_billing_rule(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, _ = _saas_plan(saas_login)
    summary = platform_client.get("/api/saas/platform/settlements").json()
    assert len(summary) == 1
    row = summary[0]
    assert row["platform_fee_krw"] == 1_206_600
    assert row["usage_fee_krw"] == 360_000
    assert row["performance_fee_krw"] == 846_600
    assert row["pilot"] is False
    shares = {s["shipper_id"]: s for s in row["shippers"]}
    assert shares["sh-sangju"]["billing_rule"] == "usage_fee"
    assert shares["sh-sangju"]["usage_fee_krw"] == 260_000
    assert shares["sh-sangju"]["fee_krw"] == 260_000
    assert shares["sh-sangju"]["baseline_label"] == "단독 해상 대비"
    assert shares["sh-sangju"]["savings_baseline_label"] == "단독 항공 대비"
    assert row["fee_before_refund_krw"] == row["platform_fee_krw"]
    assert row["quality_refund_krw"] == 0
    assert shares["sh-jinju"]["billing_rule"] == "performance"
    assert shares["sh-jinju"]["baseline_kind"] == "air_solo"

    grape = saas_login("shipper-pohang")
    rows = grape.get("/api/saas/shipper/settlements").json()
    assert [(r["plan_id"], r["shipper_id"]) for r in rows] == [(plan_id, "sh-pohang")]
    assert rows[0]["billing_rule"] == "usage_fee"
    assert rows[0]["usage_fee"] == 100_000
    assert rows[0]["fee"] == 100_000
    assert rows[0]["baseline_label"] == "단독 해상 대비"

    detail = grape.get(f"/api/saas/shipper/plans/{plan_id}").json()
    containers = detail["plan"]["container_settlements"]
    assert [c["total_fee"] for c in containers] == [100_000]
    assert containers[0]["shippers"][0]["billing_rule"] == "usage_fee"

    forwarded = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}").json()
    text = json.dumps(forwarded["plan"], ensure_ascii=False)
    assert forwarded["plan"]["settlements"] == []
    assert forwarded["plan"]["container_settlements"] == []
    assert "usage_fee" not in text and "performance_fee" not in text


def test_tariff_sheet_follows_billing_rule_and_pilot() -> None:
    scenario = _scenario()
    tariff = scenario.cost_params.pallet_tariffs["strawberry"]
    sheet = tariff_sheet(CostModel(scenario), "strawberry", tariff)
    assert sheet.billing_rule == "performance"
    assert sheet.fee_rate == 0.20
    assert sheet.fee_per_pallet == round(sheet.savings_per_pallet * 0.20)
    assert sheet.pilot is False

    pilot = _with_billing(scenario, pilot=True)
    sheet = tariff_sheet(CostModel(pilot), "strawberry", tariff)
    assert sheet.pilot is True
    assert sheet.fee_per_pallet == 0
    assert sheet.shipper_share_per_pallet == sheet.savings_per_pallet

    usage = _with_billing(scenario, usage_fee_products=["strawberry"])
    sheet = tariff_sheet(CostModel(usage), "strawberry", tariff)
    assert sheet.billing_rule == "usage_fee"
    assert sheet.fee_rate == 0.0
    assert sheet.fee_per_pallet == 20_000
    assert sheet.shipper_share_per_pallet == sheet.savings_per_pallet - 20_000

    both = _with_billing(scenario, usage_fee_products=["strawberry"], pilot=True)
    assert tariff_sheet(CostModel(both), "strawberry", tariff).fee_per_pallet == 0


def test_rows_stored_before_billing_split_backfill_performance_fee() -> None:
    legacy = {
        "shipper_id": "sh-sangju",
        "baseline_cost": 10_000_000,
        "shared_cost": 5_406_558,
        "savings": 4_593_442,
        "fee": 1_148_360,
        "shipper_gain": 3_445_082,
    }
    row = Settlement.model_validate(legacy)
    assert row.performance_fee == 1_148_360
    assert row.usage_fee == 0
    assert row.billing_rule == "performance"
    container = ContainerShipperSettlement.model_validate(
        {**legacy, "pallets": 13, "basis": "operational"}
    )
    assert container.performance_fee == 1_148_360
    assert Settlement.model_validate({**legacy, "pilot": True}).performance_fee == 0
    assert Settlement.model_validate({**legacy, "fee": 0}).performance_fee == 0
    current = Settlement.model_validate({**legacy, "fee": 260_000, "usage_fee": 260_000})
    assert current.performance_fee == 0


def test_basis_breakeven_uses_the_settlement_billing_rule() -> None:
    result = business(_scenario())
    assert result.season is not None
    basis = {b.basis: b for b in result.basis}
    total = basis["total_cost"]
    assert total.season_revenue_proposal_rule == result.season.revenue
    assert total.season_revenue_proposal_rule < total.season_revenue_performance
    pallets = sum(line.pallets for line in total.lines)
    assert total.breakeven_pallets == round(
        150_000_000 / (total.season_revenue_proposal_rule / pallets)
    )
    assert result.season.breakeven_pallets is not None
    assert total.breakeven_pallets == pytest.approx(result.season.breakeven_pallets, abs=1)
    freight = basis["freight_difference"]
    assert freight.breakeven_pallets == round(
        150_000_000 / (freight.season_revenue_proposal_rule / pallets)
    )


def test_quality_refund_keeps_fee_parts_reconcilable(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={}).json()["plan_id"]
    arrival = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": "lot-sancheong-sb-1", "inspected_loss_pct": 9},
    )
    assert arrival.status_code == 200, arrival.text
    body = client.get(f"/api/plans/{plan_id}/settlement").json()
    assert body["total_refund"] > 0
    parts = body["total_performance_fee"] + body["total_usage_fee"]
    assert body["total_fee_before_refund"] == parts == 1_206_600
    assert body["total_fee"] == body["total_fee_before_refund"] - body["total_refund"]


def test_saas_platform_settlement_shows_refund_next_to_fee_parts(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, lots = _saas_plan(saas_login)
    arrival = forwarder_client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": lots["lot-sancheong-sb-1"], "inspected_loss_pct": 9},
    )
    assert arrival.status_code == 200, arrival.text
    row = platform_client.get("/api/saas/platform/settlements").json()[0]
    assert row["quality_refund_krw"] > 0
    assert row["fee_before_refund_krw"] == row["performance_fee_krw"] + row["usage_fee_krw"]
    assert row["platform_fee_krw"] == row["fee_before_refund_krw"] - row["quality_refund_krw"]
    shares = {s["shipper_id"]: s for s in row["shippers"]}
    sancheong = shares["sh-sancheong"]
    assert sancheong["quality_refund_krw"] > 0
    assert sancheong["fee_before_refund_krw"] == sancheong["performance_fee_krw"]
    assert sancheong["fee_krw"] == (
        sancheong["fee_before_refund_krw"] - sancheong["quality_refund_krw"]
    )
