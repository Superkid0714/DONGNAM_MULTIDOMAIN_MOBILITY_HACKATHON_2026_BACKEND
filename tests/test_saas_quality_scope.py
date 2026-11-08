import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    PLATFORM_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    create_real_platform,
    login_client,
    password_client,
)

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.quality import learning_scope, quality_history, with_learned_loss
from freshswitch_backend.quality import store as quality_store
from freshswitch_backend.quality.models import ObservationIn
from freshswitch_backend.quality.store import ScopeRef


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _sandbox_inspected_plan(client_app) -> tuple[TestClient, str, str]:
    platform = login_client(PLATFORM_ORG_ID, client_app)
    created = platform.post("/api/saas/platform/plans", json={"include_registered": True})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    lot_id = next(lot["id"] for lot in created.json()["scenario"]["lots"] if "sancheong" in lot["id"])
    arrival = platform.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": lot_id, "inspected_loss_pct": 40},
    )
    assert arrival.status_code == 200, arrival.text
    world_id = platform.get("/api/me").json()["world"]["world_id"]
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
    world_id = platform.get("/api/me").json()["world"]["world_id"]
    assert all(o["id"].startswith(f"{world_id}-") for o in listed if o["origin"] == "seed")


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
    added = real.post(
        "/api/quality/observations",
        json={"product_id": "strawberry", "pallets": 50, "actual_loss_pct": 30},
    )
    assert added.status_code == 201, added.text
    obs_id = added.json()["id"]
    assert [o.id for o in quality_history.observations("live")] == [obs_id]
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
