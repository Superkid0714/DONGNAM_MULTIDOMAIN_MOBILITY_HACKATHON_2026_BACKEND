import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    SAAS_CSRF,
    SAAS_DEMO_PASSWORD,
    SHIPPER_ORG_ID,
    create_real_platform,
    login_client,
    password_client,
)
from sqlalchemy import delete, select, update

from freshswitch_backend.api.login_gate import LOGIN_REQUIRED, PUBLIC_ENDPOINTS
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.passwords import hash_password
from freshswitch_backend.persistence.seed import (
    DEMO_EMAIL_DOMAIN,
    SeedError,
    create_platform,
    demo_orgs,
    main,
    run_lots,
)
from freshswitch_backend.persistence.tables import (
    LotRow,
    MembershipRow,
    OrganizationRow,
    PackhouseRow,
    PlanForwarderRow,
    ShipperProfileRow,
    UserRow,
)
from freshswitch_backend.quality import quality_history

REAL_SHIPPER_ORG = "shipper-real"
REAL_SHIPPER_ID = "sh-real"
REAL_SHIPPER_EMAIL = "shipper@real.example.com"
REAL_PASSWORD = "real-shipper-password"
REAL_LOT_ID = "lot-real-1"
RESOURCE = {
    "name": "실제 냉장 컨테이너",
    "kind": "40RH-CA",
    "capacity_pallets": 20,
    "temp_min_c": -1,
    "temp_max_c": 4,
    "vessel_name": "등록 선박",
    "terminal": "등록 터미널",
    "origin": "KRPUS",
    "destination": "HKHKG",
    "etd": "2026-11-18T18:00:00+09:00",
    "cy_cutoff": "2026-11-17T16:00:00+09:00",
    "transit_days": 3.5,
    "enabled": True,
}


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _lot_body(lot, lot_id: str, shipper: str) -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "sell_window": lot.sell_window,
        "shipper": shipper,
        "lot_id": lot_id,
    }


def _scenario_lot(lot_id: str = "lot-jinju-sb-1"):
    scenario = get_scenario("default")
    assert scenario is not None
    return next(lot for lot in scenario.lots if lot.id == lot_id)


def _real_shipper(client_app) -> TestClient:
    with database.begin() as db:
        db.add(OrganizationRow(id=REAL_SHIPPER_ORG, type="shipper", name="실제 화주 회사"))
        db.add(
            UserRow(
                id="user-real-shipper",
                email=REAL_SHIPPER_EMAIL,
                password_hash=hash_password(REAL_PASSWORD),
                name="실제 화주 담당",
                is_demo=False,
            )
        )
        db.flush()
        db.add(MembershipRow(user_id="user-real-shipper", org_id=REAL_SHIPPER_ORG, role="admin"))
        db.add(
            PackhouseRow(
                id="ph-real",
                org_id=REAL_SHIPPER_ORG,
                name="실제 선과장",
                region="진주",
                lat=35.18,
                lon=128.1,
            )
        )
        db.flush()
        db.add(
            ShipperProfileRow(
                id=REAL_SHIPPER_ID,
                org_id=REAL_SHIPPER_ORG,
                name="실제 딸기 수출",
                packhouse_id="ph-real",
                product_ids=["strawberry"],
            )
        )
    return password_client(client_app, REAL_SHIPPER_EMAIL, REAL_PASSWORD)


def _real_forwarder(client_app) -> str:
    return _real_forwarder_client(client_app)[1]


def _real_forwarder_client(client_app) -> tuple[TestClient, str]:
    client = TestClient(client_app, base_url="https://testserver", headers=SAAS_CSRF)
    response = client.post(
        "/api/auth/signup",
        json={
            "email": "forwarder@real.example.com",
            "password": "real-forwarder-password",
            "name": "실제 포워더 담당",
            "org_name": "실제 포워더",
            "org_type": "forwarder",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["active_org"]["sandbox"] is False
    resource = client.post("/api/saas/forwarder/resources", json=RESOURCE)
    assert resource.status_code == 201, resource.text
    return client, response.json()["active_org"]["org_id"]


def _register(client: TestClient, lot_id: str, shipper: str, source: str) -> str:
    response = client.post(
        "/api/saas/shipper/lots", json=_lot_body(_scenario_lot(source), lot_id, shipper)
    )
    assert response.status_code == 201, response.text
    return response.json()["registered"]["id"]


@pytest.fixture
def worlds(saas_app) -> dict:
    real_shipper = _real_shipper(saas_app)
    real_lot = _register(real_shipper, REAL_LOT_ID, REAL_SHIPPER_ID, "lot-jinju-sb-1")
    run_lots()
    demo_platform = login_client(PLATFORM_ORG_ID, saas_app)
    demo_lots = [row["id"] for row in demo_platform.get("/api/saas/platform/lots").json()]
    real_platform_org = create_real_platform()
    real_forwarder_client, real_forwarder = _real_forwarder_client(saas_app)
    return {
        "app": saas_app,
        "real_lot": real_lot,
        "demo_lots": demo_lots,
        "real_shipper": real_shipper,
        "real_platform_org": real_platform_org,
        "real_platform": password_client(saas_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD),
        "demo_platform": demo_platform,
        "demo_forwarder": FORWARDER_ORG_ID,
        "demo_forwarder_client": login_client(FORWARDER_ORG_ID, saas_app),
        "real_forwarder": real_forwarder,
        "real_forwarder_client": real_forwarder_client,
    }


def test_seeded_demo_orgs_are_sandbox_and_signup_is_not(saas_app) -> None:
    with database.begin() as db:
        rows = dict(db.execute(select(OrganizationRow.id, OrganizationRow.sandbox)).all())
    assert all(rows[org.id] for org in demo_orgs())
    assert rows["demo"] is False
    org_id = _real_forwarder(saas_app)
    with database.begin() as db:
        assert db.get(OrganizationRow, org_id).sandbox is False


def test_reseed_restores_sandbox_flag(demo_tenants) -> None:
    with database.begin() as db:
        db.execute(update(OrganizationRow).values(sandbox=False))
    from freshswitch_backend.persistence.seed import run

    run(password="ignored")
    with database.begin() as db:
        assert db.get(OrganizationRow, PLATFORM_ORG_ID).sandbox is True


def test_saas_password_login_rejects_demo_accounts(saas_app) -> None:
    client = TestClient(saas_app, base_url="https://testserver")
    for org in demo_orgs():
        response = client.post(
            "/api/auth/login", json={"email": org.email, "password": SAAS_DEMO_PASSWORD}
        )
        assert response.status_code == 401, org.id
    main_mode = TestClient(create_app(), base_url="https://testserver")
    absent = main_mode.post(
        "/api/auth/login",
        json={"email": f"{SHIPPER_ORG_ID}@{DEMO_EMAIL_DOMAIN}", "password": SAAS_DEMO_PASSWORD},
    )
    assert absent.status_code == 404


def test_demo_platform_cannot_see_real_lots(worlds) -> None:
    demo_rows = worlds["demo_platform"].get("/api/saas/platform/lots").json()
    assert [r["id"] for r in demo_rows] == worlds["demo_lots"]
    assert worlds["real_lot"] not in worlds["demo_lots"]
    real_rows = worlds["real_platform"].get("/api/saas/platform/lots").json()
    assert [r["id"] for r in real_rows] == [worlds["real_lot"]]


def test_catalog_stays_within_sandbox(worlds) -> None:
    demo = worlds["demo_platform"].get("/api/saas/catalog").json()
    assert REAL_SHIPPER_ID not in {s["id"] for s in demo["shippers"]}
    assert "ph-real" not in {p["id"] for p in demo["packhouses"]}
    real = worlds["real_platform"].get("/api/saas/catalog").json()
    assert [s["id"] for s in real["shippers"]] == [REAL_SHIPPER_ID]
    assert [p["id"] for p in real["packhouses"]] == ["ph-real"]


def test_demo_forwarder_cannot_plan_real_lots(worlds) -> None:
    demo = worlds["demo_forwarder_client"]
    pulled = demo.post("/api/saas/forwarder/plans", json={"lot_ids": [worlds["real_lot"]]})
    assert pulled.status_code == 422
    generic = demo.post("/api/plans", json={"lot_ids": [worlds["real_lot"]]})
    assert generic.status_code == 422
    included = demo.post("/api/saas/forwarder/plans", json={"include_registered": True})
    assert included.status_code == 201, included.text
    lot_ids = {lot["id"] for lot in included.json()["scenario"]["lots"]}
    assert lot_ids == set(worlds["demo_lots"])
    assert worlds["real_shipper"].get("/api/saas/shipper/lots").json()[0]["status"] == "registered"


def test_real_forwarder_cannot_plan_demo_lots(worlds) -> None:
    real = worlds["real_forwarder_client"]
    pulled = real.post("/api/saas/forwarder/plans", json={"lot_ids": [worlds["demo_lots"][0]]})
    assert pulled.status_code == 422
    assert pulled.json()["detail"] != "가용 컨테이너가 없습니다"
    included = real.post("/api/saas/forwarder/plans", json={"include_registered": True})
    assert included.status_code == 201, included.text
    assert {lot["id"] for lot in included.json()["scenario"]["lots"]} == {worlds["real_lot"]}


def test_demo_orgs_cannot_register_for_real_shipper(worlds) -> None:
    body = _lot_body(_scenario_lot(), "lot-real-by-demo", REAL_SHIPPER_ID)
    for actor in ("demo_platform", "demo_forwarder_client"):
        response = worlds[actor].post("/api/intake/lots", json={**body, "save": True})
        assert response.status_code == 403, actor
    with database.begin() as db:
        assert db.get(LotRow, "lot-real-by-demo") is None


def test_org_list_stays_within_sandbox(worlds) -> None:
    demo = {o["org_id"] for o in worlds["demo_platform"].get("/api/saas/orgs").json()}
    assert REAL_SHIPPER_ORG not in demo
    assert worlds["real_forwarder"] not in demo
    assert worlds["demo_forwarder"] in demo
    real = {o["org_id"] for o in worlds["real_platform"].get("/api/saas/orgs").json()}
    assert real == {REAL_SHIPPER_ORG, worlds["real_forwarder"]}


def _real_plan(worlds) -> str:
    created = worlds["real_forwarder_client"].post(
        "/api/saas/forwarder/plans", json={"lot_ids": [worlds["real_lot"]]}
    )
    assert created.status_code == 201, created.text
    return created.json()["plan_id"]


def _accept_real(worlds, plan_id: str) -> None:
    accepted = worlds["real_shipper"].post(
        f"/api/saas/shipper/plans/{plan_id}/confirmation", json={"accept": True}
    )
    assert accepted.status_code == 200, accepted.text


def test_forwarder_plans_stay_within_sandbox(worlds) -> None:
    demo = worlds["demo_forwarder_client"]
    cross = demo.post(
        "/api/saas/forwarder/plans",
        json={"lot_ids": [worlds["demo_lots"][0]], "forwarder_org_id": worlds["real_forwarder"]},
    )
    assert cross.status_code == 403
    real = worlds["real_forwarder_client"]
    reverse = real.post(
        "/api/saas/forwarder/plans",
        json={"lot_ids": [worlds["real_lot"]], "forwarder_org_id": worlds["demo_forwarder"]},
    )
    assert reverse.status_code == 403
    real_plan_id = _real_plan(worlds)
    with database.begin() as db:
        owners = db.scalars(
            select(PlanForwarderRow.forwarder_org_id).where(
                PlanForwarderRow.plan_id == real_plan_id
            )
        ).all()
    assert owners == [worlds["real_forwarder"]]
    path = f"/api/saas/platform/plans/{real_plan_id}/forwarder"
    moved = worlds["real_platform"].put(path, json={"forwarder_org_id": worlds["demo_forwarder"]})
    assert moved.status_code == 403
    assert (
        worlds["demo_platform"]
        .put(path, json={"forwarder_org_id": worlds["demo_forwarder"]})
        .status_code
        == 403
    )


def test_plan_for_rejects_cross_sandbox_viewers(worlds) -> None:
    plan_id = _real_plan(worlds)
    with database.begin() as db:
        db.execute(delete(PlanForwarderRow).where(PlanForwarderRow.plan_id == plan_id))
        db.add(PlanForwarderRow(plan_id=plan_id, forwarder_org_id=FORWARDER_ORG_ID))
    demo_forwarder = worlds["demo_forwarder_client"]
    assert demo_forwarder.get(f"/api/saas/forwarder/plans/{plan_id}").status_code == 404
    assert demo_forwarder.get("/api/saas/forwarder/plans").json() == []
    params = {"plan_id": plan_id}
    assert demo_forwarder.get("/api/coldchain/containers", params=params).status_code == 404
    assert worlds["real_shipper"].get(f"/api/saas/shipper/plans/{plan_id}").status_code == 200


def test_sandbox_platform_sees_only_sandbox_plan_observations(worlds) -> None:
    plan_id = _real_plan(worlds)
    _accept_real(worlds, plan_id)
    arrival = worlds["real_forwarder_client"].post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": worlds["real_lot"], "inspected_loss_pct": 9, "note": "실제 메모"},
    )
    assert arrival.status_code == 200, arrival.text
    real = worlds["real_platform"]
    real_obs = next(o for o in quality_history.observations("live") if o.plan_id == plan_id)
    real_list = real.get("/api/quality/observations").json()["observations"]
    assert real_obs.id in {o["id"] for o in real_list}
    demo = worlds["demo_platform"]
    demo_list = demo.get("/api/quality/observations").json()["observations"]
    assert real_obs.id not in {o["id"] for o in demo_list}
    assert demo_list
    assert demo.get(f"/api/quality/observations/{real_obs.id}").status_code == 404
    added = real.post(
        "/api/quality/observations",
        json={"product_id": "strawberry", "pallets": 2, "actual_loss_pct": 3},
    )
    assert added.status_code == 403


def test_create_platform_validates_input(demo_tenants) -> None:
    with database.begin() as db:
        with pytest.raises(SeedError):
            create_platform(
                db,
                name="x",
                email=f"ops@{DEMO_EMAIL_DOMAIN}",
                user_name="x",
                password="long-enough-password",
            )
        with pytest.raises(SeedError):
            create_platform(db, name="x", email="ops@x.example.com", user_name="x", password="1")
        with pytest.raises(SeedError):
            create_platform(
                db,
                name="x",
                email="ops@x.example.com",
                user_name="x",
                password="long-enough-password",
                org_id=PLATFORM_ORG_ID,
            )


def test_platform_cli_uses_env_password(demo_tenants, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", "cli-platform-password")
    main(
        [
            "platform",
            "--name",
            "실제 플랫폼",
            "--email",
            "cli@real.example.com",
            "--user-name",
            "운영",
        ]
    )
    printed = capsys.readouterr().out
    org_id = re.search(r"org_id=(\S+)", printed).group(1)
    with database.begin() as db:
        org = db.get(OrganizationRow, org_id)
        assert (org.type, org.sandbox) == ("platform", False)
    saas = create_app(get_settings().model_copy(update={"saas_mode": True}))
    client = password_client(saas, "cli@real.example.com", "cli-platform-password")
    assert client.get("/api/me").json()["active_org"]["org_id"] == org_id


def _walk(routes, prefix: str = "") -> list[tuple[str, APIRoute]]:
    found: list[tuple[str, APIRoute]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append((prefix + route.path, route))
        elif hasattr(route, "original_router"):
            found += _walk(route.original_router.routes, prefix + route.include_context.prefix)
    return found


def _api_routes(app) -> list[tuple[str, str, APIRoute]]:
    return [
        (method, re.sub(r"\{[^}]+\}", "x", path), route)
        for path, route in _walk(app.router.routes)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    ]


def test_every_route_rejects_anonymous_in_saas_mode(saas_app) -> None:
    client = TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF)
    routes = _api_routes(saas_app)
    assert len(routes) > 60
    checked = 0
    for method, path, route in routes:
        client.cookies.clear()
        response = client.request(method, path, json={})
        detail = response.json().get("detail") if response.content else None
        if route.endpoint in PUBLIC_ENDPOINTS:
            assert detail != LOGIN_REQUIRED, (method, path)
            continue
        assert response.status_code == 401, (method, path, response.status_code)
        assert detail == LOGIN_REQUIRED, (method, path)
        checked += 1
    assert checked > 50


def test_device_token_readings_pass_the_gate(saas_app) -> None:
    client = TestClient(saas_app, base_url="https://testserver")
    reading = {"device_id": "loose-01", "container_id": "CA-01", "temp_c": 1, "power_on": True}
    without = client.post("/api/coldchain/readings", json=reading)
    assert (without.status_code, without.json()["detail"]) == (401, LOGIN_REQUIRED)
    with_token = client.post(
        "/api/coldchain/readings", json=reading, headers={"X-Device-Token": "unknown"}
    )
    assert with_token.status_code == 401
    assert with_token.json()["detail"] not in (LOGIN_REQUIRED, None)
    assert TestClient(create_app()).post("/api/coldchain/readings", json=reading).status_code == 200


def test_saas_docs_and_health_are_public(saas_app) -> None:
    client = TestClient(saas_app, base_url="https://testserver")
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/api/docs").status_code == 200
    assert client.get("/api/health").status_code in (200, 503)


def test_admin_token_passes_gate_only_with_valid_token(saas_app, reset_admin_headers) -> None:
    client = TestClient(saas_app, base_url="https://testserver")
    wrong = client.post("/api/quality/reset", headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401
    missing = client.post("/api/quality/reset")
    assert (missing.status_code, missing.json()["detail"]) == (401, LOGIN_REQUIRED)
    ok = client.post("/api/quality/reset", headers=reset_admin_headers)
    assert ok.status_code == 200
    retired = client.post("/api/demo/reset", headers=reset_admin_headers)
    assert retired.status_code == 404


def test_dead_cookie_is_rejected_on_unscoped_routes(saas_app) -> None:
    client = TestClient(saas_app, base_url="https://testserver")
    client.cookies.set("fs_session", "stale")
    assert client.get("/api/scenarios").status_code == 401


def test_demo_mode_routes_stay_anonymous(client: TestClient) -> None:
    assert client.get("/api/scenarios").status_code == 200
    assert client.get("/api/quality/observations").status_code == 200
    assert client.get("/api/coldchain/containers").json() == []
    assert client.post("/api/plans", json={}).status_code == 201
