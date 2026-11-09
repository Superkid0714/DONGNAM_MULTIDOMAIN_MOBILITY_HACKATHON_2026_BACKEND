"""Real DB-backed API handlers; requires the isolated PostgreSQL test fixture."""

from decimal import Decimal

from freshswitch_backend.planning.store import store


def test_sequential_display_fee_http(client):
    intake = client.post(
        "/api/intake/lots",
        json={
            "product": "strawberry",
            "pallets": 8,
            "ready_date": "2026-11-16",
            "shipper": "sh-jinju",
            "destination": "HKHKG",
        },
    )
    assert intake.status_code == 200, intake.text
    response = client.post("/api/plans", json={"scenario_id": "default"})
    assert response.status_code == 201, response.text
    plan_id = response.json()["plan_id"]

    def check(stage, payload):
        plan = payload.get("plan", payload)
        row = next(r for r in plan["settlements"] if r["shipper_id"] == "sh-jinju")
        assert row["fee"] == round(Decimal(row["savings"]) * Decimal(".20"))
        assert row["shipper_gain"] == row["savings"] - row["fee"]
        print(
            stage,
            {
                key: row[key]
                for key in ("air_cost", "shared_cost", "savings", "fee", "shipper_gain")
            },
        )
        return row

    first = check("created", response.json())
    assert (first["savings"], first["fee"], first["shipper_gain"]) == (3812538, 762508, 3050030)
    assert (
        next(r for r in store.get(plan_id).plan.settlements if r.shipper_id == "sh-jinju").fee
        != first["fee"]
    )
    for event in (
        {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3},
        {"type": "air_switch", "lot_id": "lot-sancheong-sb-1"},
    ):
        response = client.post(f"/api/plans/{plan_id}/events", json={"event": event})
        assert response.status_code == 200, response.text
        pending = response.json()
        chosen = next(a for a in pending["alternatives"] if a["id"] == pending["recommended_id"])
        check(event["type"] + " candidate", chosen)
        applied = client.post(
            f"/api/plans/{plan_id}/apply",
            json={
                "alternative_id": chosen["id"],
                "replan_id": pending["replan_id"],
            },
        )
        assert applied.status_code == 200, applied.text
        check(event["type"] + " applied", applied.json())
        orders = client.get(f"/api/plans/{plan_id}/orders").json()
        order = orders[-1]
        path = f"/api/plans/{plan_id}/orders/{order['order_id']}/confirm"
        for confirmation in order["shipper_confirmations"]:
            response = client.post(
                path,
                json={"actor": "shipper", "accept": True, "shipper_id": confirmation["shipper_id"]},
            )
            assert response.status_code == 200, response.text
        response = client.post(path, json={"actor": "forwarder", "accept": True})
        assert response.status_code == 200, response.text
    response = client.get(f"/api/plans/{plan_id}/settlement")
    assert response.status_code == 200, response.text
    check("settlement", response.json())
