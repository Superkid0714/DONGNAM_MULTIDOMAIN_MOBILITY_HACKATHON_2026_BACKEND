import pytest
from fastapi.testclient import TestClient
from saas_fixtures import forwarder_scenario_plan, seed_client

from freshswitch_backend.coldchain.power_plan import PowerPlanParams, power_plan
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.lots import ANY_ORG, lot_store
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PlanForwarderRow
from freshswitch_backend.planning.store import PlanSession, store

CSRF = {"X-Requested-With": "freshswitch"}
PLATFORM = "freshswitch"
SHIPPER = "shipper-jinju"
FORWARDER = "forwarder-dongnam"
OUTSIDER_FORWARDER = "forwarder-sinhang"
LOT_ID = "lot-jinju-sb-1"
PARAMS = PowerPlanParams()


def _with_peak(session: PlanSession, truck_id: str) -> PlanSession:
    routes = []
    for pickup in session.routes:
        trucks = [
            r.model_copy(update={"gate_status": "peak"}) if r.truck_id == truck_id else r
            for r in pickup.pickup.routes
        ]
        routes.append(
            pickup.model_copy(
                update={"pickup": pickup.pickup.model_copy(update={"routes": trucks})}
            )
        )
    return session.model_copy(update={"routes": routes})


def test_forwarder_power_plan_segments(demo_tenants) -> None:
    client, plan_id = forwarder_scenario_plan()
    response = client.get(f"/api/coldchain/plans/{plan_id}/power-plan")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lots"]
    off_peak = PARAMS.gate_wait_off_peak_minutes.value + PARAMS.plug_wait_minutes.value
    for lot in body["lots"]:
        stages = [s["stage"] for s in lot["segments"]]
        assert stages == ["trucking", "gate", "plug"]
        assert [s["powered"] for s in lot["segments"]] == [True, False, False]
        assert lot["unpowered_minutes"] == sum(
            s["minutes"] for s in lot["segments"] if not s["powered"]
        )
        assert lot["exceeds"] == (lot["unpowered_minutes"] >= lot["threshold_minutes"])
        if lot["gate_status"] != "peak":
            assert lot["unpowered_minutes"] == off_peak
        trucking = lot["segments"][0]
        if lot["truck_id"]:
            assert trucking["end"] == lot["segments"][1]["start"]
    exceeding = {lot["lot_id"] for lot in body["lots"] if lot["exceeds"]}
    assert {i for c in body["candidates"] for i in c["lot_ids"]} == exceeding
    assert all(q["source"].startswith("가정") for q in body["params"].values())
    assert client.get("/api/coldchain/plans/missing/power-plan").status_code == 404


def test_peak_gate_arrival_makes_battery_genset_candidate(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    session = store.get(plan_id)
    assert session is not None
    truck_id = session.routes[0].pickup.routes[0].truck_id
    trip = session.routes[0].pickup.routes[0].trip
    plan = power_plan(_with_peak(session, truck_id))
    peak = [lot for lot in plan.lots if lot.truck_id == truck_id]
    assert peak and all(lot.exceeds for lot in peak)
    assert all(
        lot.unpowered_minutes
        == PARAMS.gate_wait_peak_minutes.value + PARAMS.plug_wait_minutes.value
        for lot in peak
    )
    candidate = next(c for c in plan.candidates if c.truck_id == truck_id and c.trip == trip)
    assert candidate.equipment.startswith("배터리 젯셋")
    assert set(candidate.lot_ids) == {
        lot.lot_id for lot in peak if lot.trip == trip and lot.voyage_id == candidate.voyage_id
    }
    assert candidate.window_start is not None and candidate.window_end > candidate.window_start
    assert "혼잡" in candidate.reason


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _login(app, org_id: str) -> TestClient:
    return seed_client(org_id, app)


def test_power_plan_role_scope(saas_app) -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    lot = next(item for item in scenario.lots if item.id == LOT_ID)
    lot_store.create(lambda _: (lot, scenario.id, None), org_id=SHIPPER)
    session = store.create(
        scenario, registered_lot_ids=[LOT_ID], org_id=PLATFORM, lot_org_id=ANY_ORG
    )
    with database.begin() as db:
        db.add(PlanForwarderRow(plan_id=session.plan_id, forwarder_org_id=FORWARDER))
    path = f"/api/coldchain/plans/{session.plan_id}/power-plan"
    full = _login(saas_app, PLATFORM).get(path)
    assert full.status_code == 200
    assert len({lot["shipper_id"] for lot in full.json()["lots"]}) > 1
    forwarder = _login(saas_app, FORWARDER).get(path)
    assert forwarder.json()["lots"] == full.json()["lots"]
    mine = _login(saas_app, SHIPPER).get(path)
    assert mine.status_code == 200
    assert {lot["shipper_id"] for lot in mine.json()["lots"]} == {"sh-jinju"}
    assert _login(saas_app, OUTSIDER_FORWARDER).get(path).status_code == 404
    assert TestClient(saas_app, base_url="https://testserver").get(path).status_code == 401
