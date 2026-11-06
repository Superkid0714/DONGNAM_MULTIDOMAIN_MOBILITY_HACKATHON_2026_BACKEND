import re

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, create_real_platform, seed_client
from sqlalchemy import update

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import PackhouseRow
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.planning.store import store

SHOWCASE = "/api/public/showcase"
SECRET_VESSEL = "REAL SECRET VESSEL"
SECRET_PACKHOUSE = "실제 비밀 선과장"
TOP_KEYS = {
    "basis",
    "saving_rate",
    "packhouse_count",
    "total_pallets",
    "containers",
    "air",
    "packhouses",
    "ports",
    "products",
    "voyages",
    "routes",
}
NESTED_KEYS = {
    "containers": {"container_id", "voyage_id", "product_ids", "pallets", "capacity_pallets"},
    "air": {"packhouse_id", "pallets"},
    "packhouses": {"id", "name", "lat", "lon"},
    "ports": {"id", "name", "lat", "lon"},
    "products": {"id", "name", "so2_treated"},
    "voyages": {"id", "vessel_name", "origin", "destination", "transit_days"},
    "routes": {
        "voyage_id",
        "truck_id",
        "trip",
        "cargo_group",
        "load_pallets",
        "stops",
        "stop_pallets",
    },
}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _default():
    scenario = get_scenario("default")
    assert scenario is not None
    return scenario


def _anonymous(app) -> TestClient:
    return TestClient(app, base_url="https://testserver")


def _real_plan() -> None:
    base = _default()
    secret = base.model_copy(
        update={
            "voyages": [v.model_copy(update={"vessel_name": SECRET_VESSEL}) for v in base.voyages],
            "packhouses": [
                p.model_copy(update={"name": f"{SECRET_PACKHOUSE} {p.id}"}) for p in base.packhouses
            ],
        }
    )
    store.create(secret, org_id=create_real_platform())


def _lot_body(lot) -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "sell_window": lot.sell_window,
        "shipper": "무시",
        "lot_id": lot.id,
    }


def _sandbox_activity(app) -> None:
    lots = [lot for lot in _default().lots if lot.shipper_id in ("sh-jinju", "sh-sangju")]
    for lot in lots:
        client = seed_client(shipper_org_id(lot.shipper_id), app)
        body = {**_lot_body(lot), "pallets": 1}
        assert client.post("/api/saas/shipper/lots", json=body).status_code == 201
    forwarder = seed_client(FORWARDER_ORG_ID, app)
    created = forwarder.post(
        "/api/saas/forwarder/plans", json={"lot_ids": [lot.id for lot in lots]}
    )
    assert created.status_code == 201, created.text
    with database.begin() as db:
        db.execute(update(PackhouseRow).values(name=SECRET_PACKHOUSE, lat=37.5, lon=127.0))


def _assert_shape(body: dict) -> None:
    assert set(body) == TOP_KEYS
    for key, fields in NESTED_KEYS.items():
        for item in body[key]:
            assert set(item) == fields, key


def _assert_no_identifiers(text: str) -> None:
    assert "lot-" not in text
    assert UUID.search(text) is None
    assert SECRET_VESSEL not in text
    assert SECRET_PACKHOUSE not in text
    for money in ("cost", "saving", "fee", "gain", "krw", "settlement"):
        assert money not in text.replace("saving_rate", "")


def test_anonymous_showcase_is_default_scenario_aggregate(saas_app) -> None:
    response = _anonymous(saas_app).get(SHOWCASE)
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_shape(body)
    _assert_no_identifiers(response.text)
    expected = build_plan(_default().model_copy(deep=True)).plan
    assert body["basis"] == "default_scenario"
    assert body["saving_rate"] == round(expected.total_savings / expected.total_baseline_cost, 3)
    assert body["total_pallets"] == sum(a.pallets for a in expected.assignments)
    assert {c["container_id"] for c in body["containers"]} == {
        c.container_id for c in expected.containers if c.used
    }
    assert body["routes"] and all(route["stops"] for route in body["routes"])
    shown = {p["id"] for p in body["packhouses"]}
    assert all(stop in shown for route in body["routes"] for stop in route["stops"])


def test_showcase_ignores_sandbox_and_real_org_data(saas_app) -> None:
    before = _anonymous(saas_app).get(SHOWCASE).json()
    _sandbox_activity(saas_app)
    _real_plan()
    response = _anonymous(saas_app).get(SHOWCASE)
    assert response.status_code == 200
    assert response.json() == before
    _assert_no_identifiers(response.text)


def test_showcase_routes_carry_pallets_per_stop(saas_app) -> None:
    body = _anonymous(saas_app).get(SHOWCASE).json()
    routes = build_plan(_default().model_copy(deep=True)).routes
    expected = {
        (voyage.voyage_id, truck.truck_id, truck.trip): [
            s.pallets for s in sorted(truck.stops, key=lambda s: s.sequence)
        ]
        for voyage in routes
        for truck in voyage.pickup.routes
    }
    assert body["routes"]
    for route in body["routes"]:
        assert len(route["stop_pallets"]) == len(route["stops"])
        assert all(pallets > 0 for pallets in route["stop_pallets"])
        assert sum(route["stop_pallets"]) == route["load_pallets"]
        key = (route["voyage_id"], route["truck_id"], route["trip"])
        assert route["stop_pallets"] == expected[key]
