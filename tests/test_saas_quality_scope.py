from functools import partial

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    accept_plan,
    create_real_platform,
    login_client,
    password_client,
)

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.quality import learning_scope, quality_history, with_learned_loss
from freshswitch_backend.quality import store as quality_store
from freshswitch_backend.quality.models import ObservationIn
from freshswitch_backend.quality.store import ScopeRef


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _lot_body(lot) -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "sell_window": lot.sell_window,
        "shipper": lot.shipper_id,
        "lot_id": lot.id,
    }


def _sandbox_inspected_plan(client_app) -> tuple[TestClient, str, str]:
    scenario = get_scenario("default")
    assert scenario is not None
    for lot in scenario.lots:
        shipper = login_client(shipper_org_id(lot.shipper_id), client_app)
        registered = shipper.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert registered.status_code == 201, registered.text
    forwarder = login_client(FORWARDER_ORG_ID, client_app)
    created = forwarder.post("/api/saas/forwarder/plans", json={"include_registered": True})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    lot_id = next(
        lot["id"] for lot in created.json()["scenario"]["lots"] if "sancheong" in lot["id"]
    )
    accept_plan(partial(login_client, client_app=client_app), plan_id)
    arrival = forwarder.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": lot_id, "inspected_loss_pct": 40},
    )
    assert arrival.status_code == 200, arrival.text
    platform = login_client(PLATFORM_ORG_ID, client_app)
    world_id = learning_scope(FORWARDER_ORG_ID).world_id
    assert learning_scope(PLATFORM_ORG_ID) == ScopeRef("sandbox", world_id)
    return platform, plan_id, world_id


def _real_platform(client_app) -> TestClient:
    create_real_platform()
    return password_client(client_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)


def test_learning_scope_of_orgs(demo_tenants) -> None:
    assert learning_scope(None) == ScopeRef("demo", None)
    assert learning_scope("demo") == ScopeRef("demo", None)
    assert learning_scope(PLATFORM_ORG_ID) == ScopeRef("sandbox", "seed")
    assert learning_scope(create_real_platform()) == ScopeRef("live", None)


def test_sandbox_arrival_stays_in_sandbox_scope(saas_app, client: TestClient) -> None:
    demo_before = client.get("/api/quality/loss-rates").json()
    platform, plan_id, world_id = _sandbox_inspected_plan(saas_app)
    scope = ScopeRef("sandbox", world_id)
    assert any(o.plan_id == plan_id for o in quality_history.observations(scope))
    assert all(o.plan_id != plan_id for o in quality_history.observations())
    assert quality_history.observations("live") == []
    assert client.get("/api/quality/loss-rates").json() == demo_before
    sandbox_table = platform.get("/api/quality/loss-rates").json()
    assert sandbox_table["total_observations"] == len(quality_history.observations(scope))
    real = _real_platform(saas_app)
    live_table = real.get("/api/quality/loss-rates").json()
    assert live_table["total_observations"] == 0
    assert live_table["adjustments"] == []
    assert live_table["seed_label"] == quality_store.LIVE_LABEL
    assert real.get("/api/quality/observations").json()["count"] == 0
    listed = platform.get("/api/quality/observations").json()["observations"]
    assert any(o["plan_id"] == plan_id for o in listed)
    seeds = sorted(o["id"] for o in listed if o["origin"] == "seed")
    assert seeds == sorted(f"sandbox-{item.id}" for item in quality_history._seed)


def test_live_plan_learned_loss_ignores_sandbox_arrivals(saas_app) -> None:
    _, _, world_id = _sandbox_inspected_plan(saas_app)
    scenario = get_scenario("default")
    assert scenario is not None
    live = with_learned_loss(scenario, scope="live")
    assert live.cost_params.loss_adjustments == []
    sandbox = with_learned_loss(scenario, scope=ScopeRef("sandbox", world_id))
    demo = with_learned_loss(scenario)
    assert sandbox.cost_params.loss_adjustments != demo.cost_params.loss_adjustments


def test_live_observation_is_invisible_to_sandbox_and_demo(saas_app, client: TestClient) -> None:
    real = _real_platform(saas_app)
    body = {"product_id": "strawberry", "pallets": 50, "actual_loss_pct": 30}
    assert real.post("/api/quality/observations", json=body).status_code == 403
    obs_id = quality_history.add(ObservationIn(**body), "live").id
    assert [o.id for o in quality_history.observations("live")] == [obs_id]
    assert real.get(f"/api/quality/observations/{obs_id}").status_code == 200
    sandbox = login_client(PLATFORM_ORG_ID, saas_app)
    assert sandbox.get(f"/api/quality/observations/{obs_id}").status_code == 404
    assert client.get(f"/api/quality/observations/{obs_id}").status_code == 404
    assert real.get("/api/quality/loss-rates").json()["adjustments"]


def test_admin_reset_targets_one_scope(saas_app, reset_admin_headers) -> None:
    _, _, world_id = _sandbox_inspected_plan(saas_app)
    scope = ScopeRef("sandbox", world_id)
    demo_count = len(quality_history.observations())
    admin = TestClient(saas_app, base_url="https://testserver")
    missing = admin.post(
        "/api/quality/reset",
        params={"seed": False, "scope": "sandbox"},
        headers=reset_admin_headers,
    )
    assert missing.status_code == 422
    reset = admin.post(
        "/api/quality/reset",
        params={"seed": False, "scope": "sandbox", "world_id": world_id},
        headers=reset_admin_headers,
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["cleared"] > 0
    assert quality_history.observations(scope) == []
    assert len(quality_history.observations()) == demo_count
    reseeded = admin.post(
        "/api/quality/reset",
        params={"scope": "sandbox", "world_id": world_id},
        headers=reset_admin_headers,
    )
    assert reseeded.json()["seeded"] == len(quality_store._load_seed()[1])
    live = admin.post("/api/quality/reset", params={"scope": "live"}, headers=reset_admin_headers)
    assert live.json()["seeded"] == 0


def test_retention_limit_is_per_scope(monkeypatch, demo_tenants) -> None:
    monkeypatch.setattr(quality_store, "MAX_OBSERVATIONS", 3)
    demo_count = len(quality_history.observations())
    body = ObservationIn(product_id="strawberry", pallets=1, actual_loss_pct=2)
    added = [quality_history.add(body, "live").id for _ in range(5)]
    assert [o.id for o in quality_history.observations("live")] == added[-3:]
    assert len(quality_history.observations()) == demo_count
