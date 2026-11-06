import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import REAL_PLATFORM_EMAIL, create_real_platform, saas_app
from sqlalchemy import select

from freshswitch_backend.config import Settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import AuthSessionRow, UserRow

MAIN_MODE_ROUTES = frozenset(
    {
        "DELETE /api/coldchain/containers/{container_id}",
        "DELETE /api/coldchain/simulate/{run_id}",
        "DELETE /api/lots/{lot_id}",
        "DELETE /api/plans/{plan_id}",
        "DELETE /api/quality/observations/{obs_id}",
        "GET /api/analysis/business",
        "GET /api/analysis/business/matrix",
        "GET /api/coldchain/alerts",
        "GET /api/coldchain/alerts/stream",
        "GET /api/coldchain/bindings",
        "GET /api/coldchain/containers",
        "GET /api/coldchain/containers/{container_id}",
        "GET /api/coldchain/containers/{container_id}/settlement-preview",
        "GET /api/coldchain/params",
        "GET /api/coldchain/simulate/presets",
        "GET /api/coldchain/simulate/{run_id}",
        "GET /api/data/provenance",
        "GET /api/demo/script",
        "GET /api/demo/status",
        "GET /api/health",
        "GET /api/lots",
        "GET /api/lots/{lot_id}",
        "GET /api/plans",
        "GET /api/plans/{plan_id}",
        "GET /api/plans/{plan_id}/history",
        "GET /api/plans/{plan_id}/orders",
        "GET /api/plans/{plan_id}/orders/{order_id}",
        "GET /api/plans/{plan_id}/settlement",
        "GET /api/quality/impact",
        "GET /api/quality/loss-rates",
        "GET /api/quality/observations",
        "GET /api/quality/observations/{obs_id}",
        "GET /api/scenarios",
        "GET /api/scenarios/{scenario_id}",
        "GET /api/vessels/busan",
        "GET /api/vessels/ulsan",
        "GET /api/vessels/ulsan/congestion",
        "PATCH /api/lots/{lot_id}",
        "POST /api/analysis/benchmark",
        "POST /api/analysis/fuel-shock",
        "POST /api/analysis/sensitivity",
        "POST /api/coldchain/bindings",
        "POST /api/coldchain/readings",
        "POST /api/coldchain/simulate",
        "POST /api/compare/air-vs-sea",
        "POST /api/demo/reset",
        "POST /api/intake/lots",
        "POST /api/lots",
        "POST /api/plans",
        "POST /api/plans/{plan_id}/apply",
        "POST /api/plans/{plan_id}/delay-forecast",
        "POST /api/plans/{plan_id}/events",
        "POST /api/plans/{plan_id}/orders/{order_id}/confirm",
        "POST /api/plans/{plan_id}/orders/{order_id}/requests/{request_id}",
        "POST /api/plans/{plan_id}/schedule-check",
        "POST /api/plans/{plan_id}/settlement/arrival",
        "POST /api/quality/observations",
        "POST /api/quality/reset",
    }
)

FRONTEND_MAIN_ROUTES = frozenset(
    {
        "GET /api/scenarios/{scenario_id}",
        "POST /api/intake/lots",
        "POST /api/plans",
        "GET /api/plans/{plan_id}",
        "POST /api/plans/{plan_id}/events",
        "POST /api/plans/{plan_id}/apply",
        "GET /api/plans/{plan_id}/orders",
        "POST /api/plans/{plan_id}/orders/{order_id}/confirm",
        "POST /api/analysis/fuel-shock",
        "POST /api/demo/reset",
        "POST /api/coldchain/bindings",
        "POST /api/coldchain/simulate",
        "GET /api/coldchain/containers/{container_id}",
        "GET /api/coldchain/alerts/stream",
    }
)

BRIDGE_AND_HEALTH = frozenset({"POST /api/coldchain/readings", "GET /api/health"})


def _routes(app) -> set[str]:
    return {
        f"{method.upper()} {path}"
        for path, item in app.openapi()["paths"].items()
        for method in item
    }


def test_main_mode_exposes_only_old_demo_routes() -> None:
    routes = _routes(create_app(Settings(saas_mode=False)))
    assert routes == MAIN_MODE_ROUTES
    assert FRONTEND_MAIN_ROUTES <= routes
    assert BRIDGE_AND_HEALTH <= routes
    assert not any(re.search(r"/api/(saas|auth|me|ops|public)(/|$)", r) for r in routes)


def test_main_mode_routes_stay_in_saas_mode() -> None:
    saas_routes = _routes(saas_app)
    retired = {"GET /api/demo/script", "GET /api/demo/status", "POST /api/demo/reset"}
    assert MAIN_MODE_ROUTES - retired <= saas_routes
    assert "GET /api/me" in saas_routes


@pytest.mark.parametrize(
    "path",
    [
        "/api/me",
        "/api/auth/login",
        "/api/auth/demo-login",
        "/api/saas/sandbox/reset",
        "/api/saas/devices",
        "/api/ops/demo-contamination",
    ],
)
def test_main_mode_hides_saas_endpoints(path: str) -> None:
    client = TestClient(create_app(Settings(saas_mode=False)), base_url="https://testserver")
    assert client.get(path).status_code in (404, 405)
    assert client.post(path, json={}).status_code in (404, 405)


def _live_token() -> str:
    create_real_platform()
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with database.begin() as db:
        user_id = db.scalar(select(UserRow.id).where(UserRow.email == REAL_PLATFORM_EMAIL))
        db.add(
            AuthSessionRow(
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                user_id=user_id,
                active_org_id="platform-real",
                expires_at=now + timedelta(hours=1),
                last_seen_at=now,
            )
        )
    return token


@pytest.mark.parametrize("cookie_name", ["fs_session", "fs_demo_session"])
@pytest.mark.parametrize("token_kind", ["stale", "live"])
def test_main_mode_demo_ignores_session_cookies(
    cookie_name: str, token_kind: str, reset_admin_headers
) -> None:
    app = create_app(Settings(saas_mode=False, session_cookie_name=cookie_name))
    client = TestClient(app, base_url="https://testserver")
    token = "expired-session" if token_kind == "stale" else _live_token()
    for name in ("fs_session", "fs_demo_session"):
        client.cookies.set(name, token, path="/")
    responses = []

    def call(method: str, path: str, **kwargs):
        response = client.request(method, path, **kwargs)
        responses.append((method, path, response))
        assert response.status_code < 400, (method, path, response.text)
        return response

    call("GET", "/api/health")
    call("GET", "/api/scenarios/default")
    call(
        "POST",
        "/api/intake/lots",
        json={
            "product": "딸기",
            "pallets": 2,
            "ready_date": "2026-11-17",
            "shipper": "산청",
            "destination": "홍콩",
        },
    )
    plan_id = call("POST", "/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    call("GET", f"/api/plans/{plan_id}")
    event = call(
        "POST",
        f"/api/plans/{plan_id}/events",
        json={"event": {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}},
    ).json()
    call(
        "POST",
        f"/api/plans/{plan_id}/apply",
        json={"replan_id": event["replan_id"], "alternative_id": "sail_as_is"},
    )
    order = call("GET", f"/api/plans/{plan_id}/orders").json()[-1]
    confirmation = order["shipper_confirmations"][0]
    call(
        "POST",
        f"/api/plans/{plan_id}/orders/{order['order_id']}/confirm",
        json={"actor": "shipper", "shipper_id": confirmation["shipper_id"], "accept": True},
    )
    call("POST", "/api/analysis/fuel-shock", json={"surcharges": [0, 1000]})
    call(
        "POST",
        "/api/coldchain/bindings",
        json={"device_id": "arduino-01", "container_id": "CA-01", "plan_id": plan_id},
    )
    call(
        "POST",
        "/api/coldchain/readings",
        json={"device_id": "arduino-01", "temp_c": 1.5, "power_on": True},
    )
    call(
        "POST",
        "/api/coldchain/simulate",
        json={"preset": "power_cut_restore", "plan_id": plan_id, "container_id": "CA-02"},
    )
    call("GET", "/api/coldchain/containers/CA-01", params={"plan_id": plan_id})
    stream = call("GET", "/api/coldchain/alerts/stream", params={"plan_id": plan_id, "timeout": 0})
    assert stream.headers["content-type"].startswith("text/event-stream")
    call("POST", "/api/demo/reset", headers=reset_admin_headers)
    for method, path, response in responses:
        assert "set-cookie" not in response.headers, (method, path)
    assert client.cookies.get("fs_session") == token
    assert client.cookies.get("fs_demo_session") == token
