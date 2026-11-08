from fastapi.testclient import TestClient
from sqlalchemy import func, select

from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import ObservationRow
from freshswitch_backend.planning.store import store


def _arrival_count(plan_id: str) -> int:
    with database.begin() as db:
        return db.scalar(
            select(func.count())
            .select_from(ObservationRow)
            .where(ObservationRow.origin == "arrival", ObservationRow.plan_id == plan_id)
        )


def test_deleting_a_plan_removes_its_arrival_observations(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    lot = next(
        a["lot_id"]
        for a in client.get(f"/api/plans/{plan_id}").json()["plan"]["assignments"]
        if a["mode"] == "sea"
    )
    arrival = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": lot, "inspected_loss_pct": 5},
    )
    assert arrival.status_code == 200, arrival.text
    assert _arrival_count(plan_id) == 1
    assert store.delete(plan_id)
    assert _arrival_count(plan_id) == 0
