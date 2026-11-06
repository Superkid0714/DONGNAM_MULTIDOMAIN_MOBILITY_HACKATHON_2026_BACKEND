import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    SHIPPER_ORG_ID,
    create_real_platform,
    login_client,
)

from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.quality import quality_history
from freshswitch_backend.quality.models import ObservationIn

CSRF = {"X-Requested-With": "freshswitch"}
SEED_ORGS = {"platform": PLATFORM_ORG_ID, "shipper": SHIPPER_ORG_ID, "forwarder": FORWARDER_ORG_ID}
GUARDED = "플랫폼은 서비스 관리·조회만 할 수 있습니다. 운송 계획은 포워더가 관리합니다"


@pytest.fixture
def saas_app():
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


@pytest.fixture
def saas_client(saas_app) -> TestClient:
    return TestClient(saas_app, base_url="https://testserver")


def _first_observation(_: TestClient) -> str:
    return quality_history.observations()[0].id


def test_saas_anonymous_quality_reset_is_rejected(saas_client: TestClient) -> None:
    before = len(quality_history.observations())
    assert saas_client.post("/api/quality/reset", params={"seed": False}).status_code == 401
    target = _first_observation(saas_client)
    assert saas_client.delete(f"/api/quality/observations/{target}").status_code == 401
    assert len(quality_history.observations()) == before


@pytest.mark.parametrize("org_type", ["shipper", "forwarder"])
def test_saas_non_platform_quality_reset_is_forbidden(
    saas_app, demo_tenants, org_type: str
) -> None:
    saas_client = login_client(SEED_ORGS[org_type], saas_app)
    before = len(quality_history.observations())
    reset = saas_client.post("/api/quality/reset", params={"seed": False}, headers=CSRF)
    assert reset.status_code == 403
    target = _first_observation(saas_client)
    deleted = saas_client.delete(f"/api/quality/observations/{target}", headers=CSRF)
    assert deleted.status_code == 403
    assert len(quality_history.observations()) == before


def test_saas_sandbox_platform_cannot_reset_quality(saas_app, demo_tenants) -> None:
    saas_client = login_client(PLATFORM_ORG_ID, saas_app)
    before = len(quality_history.observations())
    target = _first_observation(saas_client)
    deleted = saas_client.delete(f"/api/quality/observations/{target}", headers=CSRF)
    assert deleted.status_code == 403
    reset = saas_client.post("/api/quality/reset", params={"seed": False}, headers=CSRF)
    assert reset.status_code == 403
    assert len(quality_history.observations()) == before


def test_saas_real_platform_quality_writes_are_forbidden(saas_client: TestClient) -> None:
    create_real_platform()
    login = saas_client.post(
        "/api/auth/login",
        json={"email": REAL_PLATFORM_EMAIL, "password": REAL_PLATFORM_PASSWORD},
    )
    assert login.status_code == 200, login.text
    live = quality_history.add(
        ObservationIn(product_id="strawberry", pallets=2, actual_loss_pct=3), "live"
    ).id
    before = len(quality_history.observations())
    listed = saas_client.get("/api/quality/observations")
    assert listed.status_code == 200, listed.text
    assert live in {item["id"] for item in listed.json()["observations"]}
    demo_target = _first_observation(saas_client)
    attempts = [
        saas_client.delete(f"/api/quality/observations/{demo_target}", headers=CSRF),
        saas_client.delete(f"/api/quality/observations/{live}", headers=CSRF),
        saas_client.post(
            "/api/quality/observations",
            json={"product_id": "strawberry", "pallets": 2, "actual_loss_pct": 3},
            headers=CSRF,
        ),
        saas_client.post("/api/quality/reset", params={"seed": False}, headers=CSRF),
        saas_client.post("/api/quality/reset", params={"scope": "demo"}, headers=CSRF),
    ]
    for response in attempts:
        assert (response.status_code, response.json()["detail"]) == (403, GUARDED)
    assert len(quality_history.observations()) == before
    assert [o.id for o in quality_history.observations("live")] == [live]


def test_saas_admin_token_can_reset_quality(saas_client: TestClient, reset_admin_headers) -> None:
    wrong = saas_client.post("/api/quality/reset", headers={"Authorization": "Bearer wrong-token"})
    assert wrong.status_code == 401
    reset = saas_client.post(
        "/api/quality/reset", params={"seed": False}, headers=reset_admin_headers
    )
    assert reset.status_code == 200
    assert quality_history.observations() == []


def test_demo_mode_keeps_anonymous_quality_reset(client: TestClient, demo_tenants) -> None:
    assert client.post("/api/quality/reset", params={"seed": False}).status_code == 200
    assert quality_history.observations() == []


def test_demo_mode_admin_token_honours_scope(client: TestClient, reset_admin_headers) -> None:
    body = ObservationIn(product_id="strawberry", pallets=1, actual_loss_pct=2)
    live = quality_history.add(body, "live").id
    demo_before = len(quality_history.observations())
    wrong = client.post(
        "/api/quality/reset",
        params={"scope": "live"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert wrong.status_code == 401
    reset = client.post(
        "/api/quality/reset", params={"scope": "live", "seed": False}, headers=reset_admin_headers
    )
    assert reset.status_code == 200
    assert reset.json()["cleared"] == 1
    assert quality_history.observations("live") == []
    assert len(quality_history.observations()) == demo_before
    again = quality_history.add(body, "live").id
    assert again != live
    removed = client.delete(
        f"/api/quality/observations/{again}", params={"scope": "live"}, headers=reset_admin_headers
    )
    assert removed.status_code == 204
    assert quality_history.observations("live") == []


def test_demo_mode_anonymous_cannot_target_other_scope(client: TestClient) -> None:
    response = client.post("/api/quality/reset", params={"scope": "sandbox", "seed": False})
    assert response.status_code == 403
