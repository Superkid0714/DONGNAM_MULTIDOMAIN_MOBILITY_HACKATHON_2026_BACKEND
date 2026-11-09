from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID
from test_metrics import _accepted_plan, _scenario

from freshswitch_backend.analysis import business_matrix
from freshswitch_backend.analysis.benchmark import tariff_empty_slot_cost
from freshswitch_backend.analysis.business import (
    ANNUAL_FIXED_COST,
    DEFAULT_OPS_COST_PER_VOYAGE,
    MVP_CASH_FIXED_COST,
    voyage_economics,
)
from freshswitch_backend.planning.store import store


def _years(client: TestClient, **params) -> dict:
    response = client.get("/api/saas/platform/business/years", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _line(body: dict, share: float, mode: str, level: str) -> dict:
    return next(
        line
        for line in body["lines"]
        if line["forwarder_share"] == share
        and line["base_fee_mode"] == mode
        and line["fixed_level"] == level
    )


def test_years_first_year_matches_matrix(platform_client: TestClient) -> None:
    body = _years(platform_client)
    assert body["years"] == 5
    assert len(body["lines"]) == 8
    matrix = business_matrix(_scenario())
    assert matrix is not None
    for share in (0.0, 0.4):
        for level in ("full", "mvp_cash"):
            row = next(
                r
                for r in matrix.rows
                if r.fee_rate == 0.20
                and r.base_fee_per_pallet == 0
                and r.air_regime == "normal"
                and r.forwarder_share == share
                and r.fixed_level == level
            )
            for mode in ("deducted", "additive"):
                first = _line(body, share, mode, level)["rows"][0]
                assert first["revenue"] == row.revenue
                assert first["forwarder_cost"] == row.forwarder_cost
                assert first["net_before_subsidy"] == row.net
                assert first["fixed_cost"] == row.fixed_cost


def test_years_rows_add_up(platform_client: TestClient) -> None:
    body = _years(platform_client)
    volumes = body["volumes"]
    assert volumes[0]["tonnes"] == {"strawberry": 109.0, "grape_so2": 298.0}
    assert volumes[4]["tonnes"] == {"strawberry": 1316.0, "grape_so2": 1990.0}
    assert volumes[4]["pallets"]["strawberry"] == round(1_316_000 / 415)
    assert "가정" in volumes[2]["source"]
    assert all("확인 필요" in s["source"] for s in body["subsidies"])
    deducted = _line(body, 0.0, "deducted", "full")
    additive = _line(body, 0.0, "additive", "full")
    assert additive["rows"][1]["revenue"] > deducted["rows"][1]["revenue"]
    strawberry = volumes[1]["pallets"]["strawberry"]
    assert additive["rows"][1]["revenue"] - deducted["rows"][1]["revenue"] == strawberry * 20_000
    cumulative = 0
    for row, subsidy in zip(deducted["rows"], body["subsidies"], strict=True):
        assert row["revenue"] == row["performance_fee"] + row["usage_fee"]
        assert row["contribution"] == row["revenue"] - row["forwarder_cost"]
        assert row["net_before_subsidy"] == row["contribution"] - ANNUAL_FIXED_COST
        assert row["net_after_subsidy"] == row["net_before_subsidy"] + subsidy["amount"]
        cumulative += row["net_before_subsidy"]
        assert row["cumulative_before_subsidy"] == cumulative
    assert deducted["rows"][4]["excluded_upside"] == 190_000_000
    sensitivity = _line(body, 0.4, "deducted", "full")
    for base, risky in zip(deducted["rows"], sensitivity["rows"], strict=True):
        assert risky["forwarder_cost"] == round(base["revenue"] * 0.4)
    mvp = _line(body, 0.0, "deducted", "mvp_cash")
    assert mvp["rows"][0]["fixed_cost"] == MVP_CASH_FIXED_COST
    assert mvp["breakeven_year_before_subsidy"] == 1
    nets = [r["net_before_subsidy"] for r in deducted["rows"]]
    expected = next((i + 1 for i, n in enumerate(nets) if n >= 0), None)
    assert deducted["breakeven_year_before_subsidy"] == expected


def test_years_query_overrides(platform_client: TestClient) -> None:
    body = _years(
        platform_client,
        years=3,
        subsidies=[5, 6],
        fixed_cost=1_000,
        volume_scale=2,
        sensitivity_forwarder_share=0.3,
    )
    assert body["years"] == 3
    assert [s["amount"] for s in body["subsidies"]] == [5, 6, 0]
    assert body["volumes"][0]["tonnes"]["strawberry"] == 218.0
    line = _line(body, 0.3, "deducted", "full")
    assert line["rows"][0]["fixed_cost"] == 1_000
    assert line["breakeven_year_before_subsidy"] == 1
    bad = platform_client.get("/api/saas/platform/business/years", params={"subsidies": [-1]})
    assert bad.status_code == 422


def test_platform_business_uses_org_plans(platform_client: TestClient, saas_login) -> None:
    session = _accepted_plan(saas_login)
    stored = store.get(session["plan_id"], org_id=FORWARDER_ORG_ID)
    assert stored is not None
    response = platform_client.get("/api/saas/platform/business", params={"month": "2026-11"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_count"] == 1
    base, risky = body["lines"]
    expected = voyage_economics(stored.scenario, stored.plan, 0.0, DEFAULT_OPS_COST_PER_VOYAGE)
    assert base["fee_revenue"] == sum(v.fee_revenue for v in expected)
    assert base["fee_revenue"] > 0
    assert base["forwarder_cost"] == 0
    assert base["voyage_count"] == len(expected)
    assert base["fixed_cost_allocation"] == DEFAULT_OPS_COST_PER_VOYAGE * len(expected)
    assert base["platform_net"] == base["contribution_margin"] - base["fixed_cost_allocation"]
    assert base["annual_contribution"] == round(base["contribution_margin"] / len(expected) * 52)
    assert risky["forwarder_share"] == 0.4
    assert risky["forwarder_cost"] == sum(
        v.forwarder_share
        for v in voyage_economics(stored.scenario, stored.plan, 0.4, DEFAULT_OPS_COST_PER_VOYAGE)
    )
    used = [c for c in stored.plan.containers if c.used]
    assert body["empty_slot_cost"] == sum(
        round(c.container_cost * (c.capacity_pallets - c.pallets) / c.capacity_pallets)
        for c in used
    )
    assert body["tariff_operational_gap"] == (
        stored.plan.total_shared_cost - stored.plan.total_operational_cost
    )
    assert (body["empty_slot_cost"], body["tariff_empty_slot_cost"]) == (2_265_000, 1_359_000)
    assert body["tariff_empty_slot_cost"] == tariff_empty_slot_cost(stored.plan)
    definition = body["empty_slot_definition"]
    assert "쓴 컨테이너 전체 빈칸 비용 2,265,000원" in definition
    assert "정산표 품목 기준 빈칸 몫 1,359,000원" in definition
    assert "운영비 기준 화주 몫 906,000원" in definition
    assert "이미 들어 있어" not in definition
    empty = platform_client.get("/api/saas/platform/business", params={"month": "2026-01"})
    assert empty.json()["plan_count"] == 0
    assert empty.json()["lines"][0]["annual_net"] is None


def _business(client: TestClient, **params) -> dict:
    response = client.get("/api/saas/platform/business", params={"month": "2026-11", **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_platform_business_settlement_gap(platform_client: TestClient, saas_login) -> None:
    _accepted_plan(saas_login)
    body = _business(platform_client)
    for line in body["lines"]:
        gap = sum(p["tariff_operational_gap"] for p in line["plans"])
        assert line["settlement_gap"] == gap == body["tariff_operational_gap"]
        assert line["platform_net_with_settlement"] == line["platform_net"] + gap
        count = line["voyage_count"]
        assert line["annual_settlement_gap"] == round(gap / count * 52)
        assert line["annual_net_with_settlement"] == (
            line["annual_net"] + line["annual_settlement_gap"]
        )
    empty = _business(platform_client, month="2026-01")["lines"][0]
    assert empty["settlement_gap"] == 0
    assert empty["annual_settlement_gap"] is None
    assert empty["annual_net_with_settlement"] is None


def test_platform_business_applies_quality_refund(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    session = _accepted_plan(saas_login)
    plan_id = session["plan_id"]
    before = _business(platform_client)["lines"][0]
    arrival = forwarder_client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": "lot-sancheong-sb-1", "inspected_loss_pct": 9},
    )
    assert arrival.status_code == 200, arrival.text
    metrics = platform_client.get("/api/saas/platform/metrics", params={"month": "2026-11"})
    refund = metrics.json()["quality_refund_krw"]
    assert refund > 0
    after = _business(platform_client)["lines"][0]
    stored = store.get(plan_id, org_id=FORWARDER_ORG_ID)
    assert stored is not None
    shipper = next(lot.shipper_id for lot in stored.scenario.lots if lot.id == "lot-sancheong-sb-1")
    assert all(a.mode == "sea" for a in stored.plan.assignments if a.shipper_id == shipper)
    assert after["fee_revenue"] == before["fee_revenue"] - refund
    assert after["contribution_margin"] == before["contribution_margin"] - refund
    assert after["platform_net"] == before["platform_net"] - refund
    sea_fee = sum(
        s.fee
        for s in stored.plan.settlements
        if any(a.shipper_id == s.shipper_id and a.mode == "sea" for a in stored.plan.assignments)
    )
    assert before["fee_revenue"] == sea_fee
    assert after["fee_revenue"] == sea_fee - refund


def test_platform_business_override_source(platform_client: TestClient) -> None:
    default = {a["key"]: a["source"] for a in _business(platform_client)["assumptions"]}
    assert "8-1" in default["ops_cost_per_voyage"]
    assert default["variable_cost_per_pallet"].startswith("가정")
    body = _business(platform_client, ops_cost_per_voyage=1, variable_cost_per_pallet=5)
    sources = {a["key"]: a["source"] for a in body["assumptions"]}
    assert sources["ops_cost_per_voyage"] == "쿼리로 넣은 가정"
    assert sources["variable_cost_per_pallet"] == "쿼리로 넣은 가정"
