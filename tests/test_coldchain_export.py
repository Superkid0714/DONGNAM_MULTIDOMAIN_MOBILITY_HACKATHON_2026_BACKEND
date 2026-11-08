import csv
import io
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import seed_client

from freshswitch_backend.coldchain import ColdchainParams
from freshswitch_backend.coldchain.models import ReadingIn, TimelinePoint
from freshswitch_backend.coldchain.store import coldchain_store, outage_episodes, residual_risks
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST
from freshswitch_backend.lots import ANY_ORG, lot_store
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PlanForwarderRow
from freshswitch_backend.planning.store import store

CSRF = {"X-Requested-With": "freshswitch"}
PLATFORM = "freshswitch"
SHIPPER = "shipper-jinju"
OUTSIDER_SHIPPER = "shipper-sancheong"
FORWARDER = "forwarder-dongnam"
OUTSIDER_FORWARDER = "forwarder-sinhang"
LOT_ID = "lot-jinju-sb-1"
START = datetime(2026, 11, 17, 9, 0, tzinfo=KST)


def _rows(response) -> list[dict]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    return list(csv.DictReader(io.StringIO(response.text)))


def _point(minute: int, temp: float, power_on: bool, door: bool | None = None) -> TimelinePoint:
    return TimelinePoint(
        ts=START + timedelta(minutes=minute),
        temp_c=temp,
        power_on=power_on,
        door_open=door,
        status="normal",
        cumulative_outage_minutes=0,
        degree_minutes=0,
        sensor_loss_rate=0,
    )


def test_outage_episodes_follow_power_flags() -> None:
    points = [
        _point(0, 1.0, True),
        _point(10, 1.5, False),
        _point(40, 4.0, False),
        _point(50, 3.0, True),
        _point(60, 2.0, False),
    ]
    first, second = outage_episodes(points, 60)
    assert first.start == points[1].ts
    assert first.end == points[3].ts
    assert first.minutes == 40
    assert first.max_temp_c == 4.0
    assert second.end is None
    assert second.minutes == 0


def test_residual_risks_flag_swing_gap_and_door() -> None:
    params = ColdchainParams()
    points = [
        _point(0, 1.0, True, False),
        _point(10, 6.0, True, True),
        _point(200, 0.5, True, False),
    ]
    risks = {r.kind: r for r in residual_risks(points, 0.0, 2.0, params)}
    assert risks["temperature_swing"].level == "warning"
    assert risks["sensor_gap"].level == "warning"
    assert risks["sensor_gap"].value == 130
    assert risks["door_open"].value == 1
    assert risks["door_open"].level == "warning"
    calm = residual_risks([_point(0, 1.0, True), _point(30, 1.0, True)], 0.0, 2.0, params)
    assert {r.kind: r.level for r in calm} == {
        "heat_accumulation": "info",
        "temperature_swing": "info",
        "sensor_gap": "info",
    }


def test_demo_csv_downloads(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    params = {"plan_id": plan_id}
    before = client.get("/api/coldchain/containers/CA-01/readings.csv", params=params)
    assert before.status_code == 404
    client.post(
        "/api/coldchain/simulate",
        json={"preset": "power_cut_restore", "plan_id": plan_id, "container_id": "CA-01"},
    )
    state = client.get("/api/coldchain/containers/CA-01", params=params).json()
    readings = _rows(client.get("/api/coldchain/containers/CA-01/readings.csv", params=params))
    assert len(readings) == state["reading_count"]
    assert set(readings[0]) == {
        "ts",
        "device_id",
        "temp_c",
        "power_on",
        "humidity_pct",
        "door_open",
        "source",
    }
    assert {r["power_on"] for r in readings} == {"true", "false"}
    outages = _rows(client.get("/api/coldchain/containers/CA-01/outages.csv", params=params))
    assert len(outages) == state["outage"]["episodes"]
    total = sum(float(r["minutes"]) for r in outages)
    assert total == pytest.approx(state["outage"]["cumulative_minutes"])
    missing = client.get("/api/coldchain/containers/CA-02/readings.csv", params=params)
    assert missing.status_code == 404


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _login(app, org_id: str) -> TestClient:
    return seed_client(org_id, app)


@pytest.fixture
def saas_plan(saas_app) -> str:
    scenario = get_scenario("default")
    assert scenario is not None
    lot = next(item for item in scenario.lots if item.id == LOT_ID)
    lot_store.create(lambda _: (lot, scenario.id, None), org_id=SHIPPER)
    session = store.create(
        scenario, registered_lot_ids=[LOT_ID], org_id=PLATFORM, lot_org_id=ANY_ORG
    )
    with database.begin() as db:
        db.add(PlanForwarderRow(plan_id=session.plan_id, forwarder_org_id=FORWARDER))
    platform = _login(saas_app, PLATFORM)
    registered = platform.post("/api/saas/devices", json={"device_id": "demo-sensor-01"})
    assert registered.status_code == 201, registered.text
    for container_id in ("CA-01", "CA-02"):
        run = platform.post(
            "/api/coldchain/simulate",
            json={
                "preset": "short_outage",
                "plan_id": session.plan_id,
                "container_id": container_id,
                "device_id": "demo-sensor-01",
            },
        )
        assert run.status_code == 200, run.text
    return session.plan_id


@pytest.mark.parametrize("kind", ["readings", "outages"])
def test_csv_role_scope(saas_app, saas_plan: str, kind: str) -> None:
    params = {"plan_id": saas_plan}
    own = f"/api/coldchain/containers/CA-01/{kind}.csv"
    other = f"/api/coldchain/containers/CA-02/{kind}.csv"
    for org_id in (PLATFORM, FORWARDER):
        client = _login(saas_app, org_id)
        _rows(client.get(own, params=params))
        _rows(client.get(other, params=params))
    shipper = _login(saas_app, SHIPPER)
    _rows(shipper.get(own, params=params))
    assert shipper.get(other, params=params).status_code == 404
    assert shipper.get(own).status_code == 404
    for org_id in (OUTSIDER_SHIPPER, OUTSIDER_FORWARDER):
        outsider = _login(saas_app, org_id)
        assert outsider.get(own, params=params).status_code == 404
    anonymous = TestClient(saas_app, base_url="https://testserver")
    assert anonymous.get(own, params=params).status_code == 401


def test_readings_csv_neutralises_formula_cells(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    reading = {
        "device_id": "=1+1",
        "container_id": "CA-01",
        "plan_id": plan_id,
        "ts": START.isoformat(),
        "temp_c": -1.5,
        "power_on": True,
    }
    rejected = client.post("/api/coldchain/readings", json=reading)
    assert rejected.status_code == 422, rejected.text
    stored = coldchain_store.ingest(
        [ReadingIn.model_construct(**{**reading, "ts": START, "stage": None})]
    )
    assert stored.accepted == 1
    params = {"plan_id": plan_id}
    readings = _rows(client.get("/api/coldchain/containers/CA-01/readings.csv", params=params))
    raw = [r for r in readings if r["source"] == "raw"]
    assert [r["device_id"] for r in raw] == ["'=1+1"]
    assert raw[0]["temp_c"] == "-1.5"
