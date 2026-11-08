import importlib

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    OTHER_SHIPPER_ORG_ID,
    PLATFORM_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    SHIPPER_ORG_ID,
    create_real_platform,
    login_client,
    password_client,
)

from freshswitch_backend.analysis import business
from freshswitch_backend.api import analysis as analysis_api
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence.tables import DEMO_ORG
from freshswitch_backend.planning.compare import compare_air_sea
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.planning.store import store
from freshswitch_backend.replan import replan

JINJU_LOT = "lot-jinju-sb-1"
SANCHEONG_LOT = "lot-sancheong-sb-1"
SANGJU_LOT = "lot-sangju-gr-1"
FUEL_SHOCK = {"surcharges": [0, 1000], "find_thresholds": False}


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _lot(lot_id: str):
    scenario = get_scenario("default")
    assert scenario is not None
    return next(lot for lot in scenario.lots if lot.id == lot_id)


def _member(client: TestClient, slug: str) -> str:
    me = client.get("/api/me").json()
    return next(
        item["org_id"]
        for item in me["memberships"]
        if item["org_id"] == slug or item["org_id"].endswith(f"-{slug}")
    )


def _plan(client_app, *lot_ids: str) -> tuple[TestClient, str, list[str]]:
    platform = login_client(PLATFORM_ORG_ID, client_app)
    rows = platform.get("/api/saas/platform/lots").json()
    ids = []
    for lot_id in lot_ids:
        shipper = _lot(lot_id).shipper_id
        ids.append(next(row["id"] for row in rows if str(row["shipper_id"]).endswith(shipper)))
    created = platform.post("/api/saas/platform/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    return platform, created.json()["plan_id"], ids


def _money_requests(plan_id: str | None) -> list[tuple[str, str, dict]]:
    scoped = {"plan_id": plan_id} if plan_id is not None else {}
    return [
        ("POST", "/api/analysis/fuel-shock", {"json": {**FUEL_SHOCK, **scoped}}),
        ("POST", "/api/analysis/sensitivity", {"json": scoped}),
        ("POST", "/api/analysis/benchmark", {"json": scoped}),
        ("GET", "/api/analysis/business", {"params": scoped}),
        ("GET", "/api/analysis/business/matrix", {"params": scoped}),
    ]


def _call(client: TestClient, method: str, path: str, kwargs: dict):
    return client.request(method, path, **kwargs)


def test_platform_runs_money_analyses_on_own_plan(saas_app) -> None:
    platform, plan_id, ids = _plan(saas_app, JINJU_LOT, SANCHEONG_LOT)
    for method, path, kwargs in _money_requests(plan_id):
        response = _call(platform, method, path, kwargs)
        assert response.status_code == 200, (path, response.text)
    fuel = platform.post("/api/analysis/fuel-shock", json={**FUEL_SHOCK, "plan_id": plan_id})
    assert fuel.json()["plan_id"] == plan_id
    compared = platform.post("/api/compare/air-vs-sea", json={"plan_id": plan_id, "lot_id": ids[1]})
    assert compared.status_code == 200, compared.text
    assert compared.json()["sea"]["in_plan"] is True
    forecast = platform.post(f"/api/plans/{plan_id}/delay-forecast?llm=false", json={})
    assert forecast.status_code == 200, forecast.text
    assert forecast.json()["plan_id"] == plan_id


def test_benchmark_optimizer_row_is_the_stored_plan(saas_app) -> None:
    platform, plan_id, _ = _plan(saas_app, JINJU_LOT, SANCHEONG_LOT)
    stored = platform.get(f"/api/plans/{plan_id}").json()["plan"]
    result = platform.post("/api/analysis/benchmark", json={"plan_id": plan_id}).json()
    optimizer = next(m for m in result["methods"] if m["method"] == "optimizer")
    assert optimizer["total_savings"] == stored["total_savings"]
    assert optimizer["total_fee"] == stored["total_fee"]
    assert optimizer["solve_ms"] == stored["solve_ms"]
    assert set(optimizer["locations"]) == {a["lot_id"] for a in stored["assignments"]}


def test_benchmark_replan_starts_from_stored_plan_with_air_locks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_id = client.post("/api/plans", json={}).json()["plan_id"]
    session = store.get(plan_id, org_id=DEMO_ORG)
    assert session is not None
    locked = session.model_copy(update={"air_locked_lot_ids": [JINJU_LOT]})
    monkeypatch.setattr(analysis_api, "plan_session", lambda *_: locked)
    seen = []

    def recording(scenario, current_plan, event, **options):
        result = replan(scenario, current_plan, event, **options)
        seen.append((current_plan, list(options["air_locked"]), result))
        return result

    module = importlib.import_module("freshswitch_backend.analysis.benchmark")
    monkeypatch.setattr(module, "replan", recording)
    response = client.post("/api/analysis/benchmark", json={"plan_id": plan_id})
    assert response.status_code == 200, response.text
    [(base, air_locked, result)] = seen
    assert base == session.plan
    assert air_locked == [JINJU_LOT]
    for alternative in result.alternatives:
        if alternative.id != "sail_as_is":
            modes = {a.lot_id: a.mode for a in alternative.plan.assignments}
            assert modes[JINJU_LOT] == "air", alternative.id
    rows = response.json()["replan"]["rows"]
    assert [row["id"] for row in rows] == [a.id for a in result.alternatives]
    assert [row["total_savings"] for row in rows] == [
        a.plan.total_savings for a in result.alternatives
    ]


def test_business_on_plan_uses_plan_scenario(saas_app) -> None:
    platform, plan_id, _ = _plan(saas_app, JINJU_LOT, SANCHEONG_LOT)
    scenario = platform.get(f"/api/plans/{plan_id}").json()["scenario"]
    result = platform.get("/api/analysis/business", params={"plan_id": plan_id}).json()
    default = platform.get("/api/analysis/business").json()
    assert result["scenario_id"] == scenario["id"]
    assert result != default


def test_other_org_plan_is_not_found(saas_app, client: TestClient) -> None:
    _, plan_id, ids = _plan(saas_app, JINJU_LOT)
    create_real_platform()
    real = password_client(saas_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    for method, path, kwargs in _money_requests(plan_id):
        response = _call(real, method, path, kwargs)
        assert response.status_code == 404, (path, response.status_code)
    lot = {"plan_id": plan_id, "lot_id": ids[0]}
    assert real.post("/api/compare/air-vs-sea", json=lot).status_code == 404
    assert real.post(f"/api/plans/{plan_id}/delay-forecast", json={}).status_code == 404
    for method, path, kwargs in _money_requests(plan_id):
        assert _call(client, method, path, kwargs).status_code == 404, path
    assert client.post("/api/compare/air-vs-sea", json=lot).status_code == 404


def test_sandbox_platform_cannot_reach_demo_plans(saas_app, client: TestClient) -> None:
    demo_plan = client.post("/api/plans", json={}).json()["plan_id"]
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    for method, path, kwargs in _money_requests(demo_plan):
        response = _call(platform, method, path, kwargs)
        assert response.status_code == 404, (path, response.status_code)
    lot = {"plan_id": demo_plan, "lot_id": JINJU_LOT}
    assert platform.post("/api/compare/air-vs-sea", json=lot).status_code == 404
    assert platform.post(f"/api/plans/{demo_plan}/delay-forecast", json={}).status_code == 404


@pytest.mark.parametrize("org_id", [SHIPPER_ORG_ID, FORWARDER_ORG_ID])
def test_shipper_and_forwarder_cannot_run_money_analyses(saas_app, org_id: str) -> None:
    platform, plan_id, _ = _plan(saas_app, JINJU_LOT)
    if org_id == FORWARDER_ORG_ID:
        assigned = platform.put(
            f"/api/saas/platform/plans/{plan_id}/forwarder",
            json={"forwarder_org_id": _member(platform, FORWARDER_ORG_ID)},
        )
        assert assigned.status_code == 200, assigned.text
    viewer = login_client(org_id, saas_app)
    for target in (plan_id, None):
        for method, path, kwargs in _money_requests(target):
            response = _call(viewer, method, path, kwargs)
            assert response.status_code == 403, (path, target, response.status_code)
    assert viewer.post(f"/api/plans/{plan_id}/delay-forecast", json={}).status_code == 403
    assert viewer.get("/api/quality/impact").status_code == 403
    assert viewer.get("/api/quality/loss-rates").status_code == 200


def test_forwarder_cannot_compare_air_vs_sea(saas_app) -> None:
    platform, plan_id, ids = _plan(saas_app, JINJU_LOT)
    assigned = platform.put(
        f"/api/saas/platform/plans/{plan_id}/forwarder",
        json={"forwarder_org_id": _member(platform, FORWARDER_ORG_ID)},
    )
    assert assigned.status_code == 200, assigned.text
    forwarder = login_client(FORWARDER_ORG_ID, saas_app)
    lot = {"plan_id": plan_id, "lot_id": ids[0]}
    assert forwarder.post("/api/compare/air-vs-sea", json=lot).status_code == 403
    default = {"lot_id": JINJU_LOT}
    assert forwarder.post("/api/compare/air-vs-sea", json=default).status_code == 403


def test_shipper_compares_only_own_lots_in_own_plans(saas_app) -> None:
    platform, plan_id, ids = _plan(saas_app, JINJU_LOT, SANCHEONG_LOT)
    jinju, sancheong = ids
    shipper = login_client(SHIPPER_ORG_ID, saas_app)
    own = shipper.post("/api/compare/air-vs-sea", json={"plan_id": plan_id, "lot_id": jinju})
    assert own.status_code == 200, own.text
    body = own.json()
    assert body["lot_id"] == jinju
    assert body["shipper_id"].endswith("sh-jinju")
    expected = platform.post(
        "/api/compare/air-vs-sea", json={"plan_id": plan_id, "lot_id": jinju}
    ).json()
    assert body == expected
    other = shipper.post("/api/compare/air-vs-sea", json={"plan_id": plan_id, "lot_id": sancheong})
    missing = shipper.post("/api/compare/air-vs-sea", json={"plan_id": plan_id, "lot_id": "nope"})
    assert (other.status_code, other.json()) == (missing.status_code, missing.json())
    assert other.status_code == 404
    no_plan = shipper.post("/api/compare/air-vs-sea", json={"lot_id": JINJU_LOT})
    assert no_plan.status_code == 403

    _, grape_plan, grape_ids = _plan(saas_app, SANGJU_LOT)
    outside = shipper.post(
        "/api/compare/air-vs-sea", json={"plan_id": grape_plan, "lot_id": grape_ids[0]}
    )
    assert outside.status_code == 404
    neighbour = login_client(OTHER_SHIPPER_ORG_ID, saas_app)
    theirs = neighbour.post(
        "/api/compare/air-vs-sea", json={"plan_id": plan_id, "lot_id": sancheong}
    )
    assert theirs.status_code == 200
    assert theirs.json()["shipper_id"].endswith("sh-sancheong")


def test_anonymous_demo_responses_unchanged(client: TestClient) -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    compared = client.post("/api/compare/air-vs-sea", json={"lot_id": JINJU_LOT})
    assert compared.status_code == 200
    expected = compare_air_sea(scenario, build_plan(scenario).plan, JINJU_LOT)
    assert compared.json() == expected.model_dump(mode="json")
    result = client.get("/api/analysis/business").json()
    assert result == business(scenario).model_dump(mode="json")

    plan_id = client.post("/api/plans", json={}).json()["plan_id"]
    fuel = client.post("/api/analysis/fuel-shock", json={**FUEL_SHOCK, "plan_id": plan_id})
    assert fuel.status_code == 200
    assert fuel.json()["plan_id"] == plan_id
    on_plan = client.post("/api/compare/air-vs-sea", json={"plan_id": plan_id, "lot_id": JINJU_LOT})
    assert on_plan.status_code == 200
    assert client.get("/api/quality/impact").status_code == 200
    assert client.post(f"/api/plans/{plan_id}/delay-forecast?llm=false", json={}).status_code == 200


def test_money_analyses_require_login_in_saas_mode(saas_app) -> None:
    anonymous = TestClient(saas_app, base_url="https://testserver")
    for method, path, kwargs in _money_requests(None):
        assert _call(anonymous, method, path, kwargs).status_code == 401, path
    assert anonymous.post("/api/compare/air-vs-sea", json={"lot_id": JINJU_LOT}).status_code == 401
