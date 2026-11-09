from fastapi.testclient import TestClient

LOT = "lot-sancheong-sb-1"


def _plan(client: TestClient) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def _arrival(client: TestClient, plan_id: str, pct: float, lot: str = LOT) -> dict:
    response = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": lot, "inspected_loss_pct": pct, "note": "도착 검수"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_no_refund_within_planned_loss(client: TestClient) -> None:
    plan_id = _plan(client)
    result = _arrival(client, plan_id, 1.0)
    assert result["planned_loss_rate"] > 0.01
    assert result["refund_after"] == 0
    assert result["platform_net_change"] == 0
    assert result["settlement_after"] == result["settlement_before"]


def test_partial_refund_below_cap(client: TestClient) -> None:
    plan_id = _plan(client)
    result = _arrival(client, plan_id, 2.2)
    assert result["planned_loss_rate"] == 0.01593
    excess = round(result["lot_value"] * (0.022 - result["planned_loss_rate"]))
    assert abs(result["excess_loss_value"] - excess) <= 1
    assert result["refund_after"] == result["excess_loss_value"] < result["refund_cap"]
    assert result["capped"] is False
    after = result["settlement_after"]
    before = result["settlement_before"]
    assert after["fee"] == before["fee"] - result["refund_after"]
    assert after["shipper_gain"] == before["shipper_gain"] + result["refund_after"]
    assert result["platform_net_change"] == -result["refund_after"]


def test_refund_capped_at_fee(client: TestClient) -> None:
    plan_id = _plan(client)
    result = _arrival(client, plan_id, 20)
    assert result["capped"] is True
    assert result["refund_after"] == result["refund_cap"] == result["settlement_before"]["fee"]
    assert result["settlement_after"]["fee"] == 0
    assert result["uncovered_loss"] == result["excess_loss_value"] - result["refund_after"]
    again = _arrival(client, plan_id, 25)
    assert again["refund_change"] == 0
    assert again["platform_net_change"] == 0


def test_reinspection_replaces_and_shipper_cap_is_shared(client: TestClient) -> None:
    plan_id = _plan(client)
    first = _arrival(client, plan_id, 20, "lot-miryang-sb-1")
    second = _arrival(client, plan_id, 2.0, "lot-miryang-sb-1")
    assert second["refund_before"] == first["refund_after"]
    assert second["refund_after"] < second["refund_before"]
    view = client.get(f"/api/plans/{plan_id}/settlement").json()
    assert len(view["inspections"]) == 1
    assert view["total_fee"] == view["total_fee_before_refund"] - view["total_refund"]


def test_arrival_errors(client: TestClient) -> None:
    plan_id = _plan(client)
    response = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": "nope", "inspected_loss_pct": 5},
    )
    assert response.status_code == 422
    bad = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": LOT, "inspected_loss_pct": 120},
    )
    assert bad.status_code == 422
    missing = client.post(
        "/api/plans/nope/settlement/arrival", json={"lot_id": LOT, "inspected_loss_pct": 5}
    )
    assert missing.status_code == 404
