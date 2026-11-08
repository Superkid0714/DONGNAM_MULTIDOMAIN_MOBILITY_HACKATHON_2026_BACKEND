import json

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    OTHER_SHIPPER_ORG_ID,
    PLATFORM_ORG_ID,
    SAAS_CSRF,
    SHIPPER_ORG_ID,
    login_client,
)

from freshswitch_backend import observability, saas_readiness
from freshswitch_backend.api import health
from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import seed_lots
from freshswitch_backend.persistence.tables import LotRow, OrganizationRow

CONTAMINATION = "/api/ops/demo-contamination"


@pytest.fixture
def events(monkeypatch):
    captured = []
    monkeypatch.setattr(
        observability.logger, "info", lambda line: captured.append(json.loads(line))
    )
    return captured


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def test_health_reports_deploy_and_migration_state(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("GIT_COMMIT", "abc1234")
    body = client.get("/api/health").json()
    assert body["commit"] == "abc1234"
    assert body["migration_version"] == body["migration_head"] == health.migration_head()
    assert body["migration_head"] is not None
    assert isinstance(body["offline_mode"], bool)
    assert isinstance(body["explain_use_llm"], bool)
    assert body["seed_completed"] is True


def test_health_seed_flag_and_missing_commit(client: TestClient, demo_tenants, monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    body = client.get("/api/health").json()
    assert body["commit"] is None
    assert body["seed_completed"] is True


def test_health_nulls_db_fields_when_db_is_down(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(health.database, "ping", lambda: False)
    response = client.get("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["migration_version"] is None
    assert body["seed_completed"] is None
    assert body["migration_head"] is not None


def test_health_stays_public_in_saas_mode(saas_app) -> None:
    response = TestClient(saas_app, base_url="https://testserver").get("/api/health")
    assert response.status_code == 200
    assert response.json()["seed_completed"] is True


def test_contamination_requires_admin_token(client: TestClient, reset_admin_headers) -> None:
    assert client.get(CONTAMINATION).status_code == 401
    wrong = client.get(CONTAMINATION, headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401


def test_contamination_disabled_without_configured_token(client: TestClient) -> None:
    response = client.get(CONTAMINATION, headers={"Authorization": "Bearer any"})
    assert response.status_code == 403


def test_contamination_rejects_anonymous_and_sessions_in_saas_mode(
    saas_app, reset_admin_headers
) -> None:
    anonymous = TestClient(saas_app, base_url="https://testserver")
    assert anonymous.get(CONTAMINATION).status_code == 401
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    assert platform.get(CONTAMINATION).status_code == 401
    admin = anonymous.get(CONTAMINATION, headers=reset_admin_headers)
    assert admin.status_code == 200


def test_fresh_seed_is_clean(client: TestClient, demo_tenants, reset_admin_headers) -> None:
    with database.begin() as db:
        seed_lots(db)
    report = client.get(CONTAMINATION, headers=reset_admin_headers).json()
    assert report["contaminated"] is False
    assert report["seed_completed"] is True
    assert report["missing_orgs"] == [] and report["unexpected_sandbox_orgs"] == []
    shipper = next(o for o in report["orgs"] if o["org_id"] == SHIPPER_ORG_ID)
    assert shipper["counts"] == shipper["baseline"]
    assert shipper["baseline"]["members"] == 1
    assert all(not org["drift"] for org in report["orgs"])


def test_demo_plan_and_extra_sandbox_org_are_reported(
    client: TestClient, demo_tenants, reset_admin_headers
) -> None:
    assert client.post("/api/plans", json={"scenario_id": "default"}).status_code == 201
    with database.begin() as db:
        db.add(OrganizationRow(id="shipper-stray", type="shipper", name="stray", sandbox=True))
    report = client.get(CONTAMINATION, headers=reset_admin_headers).json()
    assert report["contaminated"] is True
    assert report["unexpected_sandbox_orgs"] == ["shipper-stray"]
    demo = next(o for o in report["orgs"] if o["org_id"] == "demo")
    assert demo["drift"] == {"plans": 1}
    assert "이메일" not in json.dumps(report, ensure_ascii=False)


def test_request_log_carries_org_type_and_sandbox_without_identity(saas_app, events) -> None:
    shipper = login_client(SHIPPER_ORG_ID, saas_app)
    events.clear()
    assert shipper.get("/api/me").status_code == 200
    request = next(e for e in events if e["event"] == "http.request")
    assert request["org_type"] == "shipper"
    assert request["sandbox"] is True
    assert isinstance(request["elapsed_ms"], float)
    text = json.dumps(events, ensure_ascii=False)
    assert "@" not in text and SHIPPER_ORG_ID not in text


def test_anonymous_request_log_has_null_org_fields(client: TestClient, events) -> None:
    client.get("/api/health")
    request = next(e for e in events if e["event"] == "http.request")
    assert request["org_type"] is None and request["sandbox"] is None
    assert observability.request_tags.get() is None


def test_plan_build_timing_is_logged(client: TestClient, events) -> None:
    assert client.post("/api/plans", json={"scenario_id": "default"}).status_code == 201
    build = next(e for e in events if e["event"] == "plan.build")
    request = next(e for e in events if e["event"] == "http.request")
    assert build["request_id"] == request["request_id"]
    assert build["status"] in ("OPTIMAL", "FEASIBLE")
    assert build["lot_count"] > 0 and build["elapsed_ms"] >= 0


def test_saas_readiness_flow_passes_in_process(saas_app, monkeypatch) -> None:
    lines: list[str] = []
    readiness = saas_readiness.Readiness(
        base_url="https://testserver/api",
        make_client=lambda: TestClient(saas_app, base_url="https://testserver"),
        out=lines.append,
    )
    assert readiness.run(), lines
    names = [r.name for r in readiness.results]
    assert names[0] == "health" and names[-1] == "cleanup"
    assert "platform generates plan" in names and "forwarder confirms order" in names
    assert all(r.ms >= 0 for r in readiness.results)
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    assert platform.get("/api/saas/platform/plans").json() == []


def test_saas_readiness_reports_failure(saas_app) -> None:
    readiness = saas_readiness.Readiness(
        base_url="https://testserver/missing",
        make_client=lambda: TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF),
        out=lambda line: None,
    )
    assert not readiness.run()
    assert readiness.results[0].passed is False
    assert saas_readiness.summary(readiness.results)["passed"] is False


def _readiness(saas_app, lines: list[str]) -> saas_readiness.Readiness:
    return saas_readiness.Readiness(
        base_url="https://testserver/api",
        make_client=lambda: TestClient(saas_app, base_url="https://testserver"),
        out=lines.append,
    )


def test_saas_readiness_missing_order_fails_step_and_cleans_up(saas_app, monkeypatch) -> None:
    original = saas_readiness.Readiness.call

    def hide_orders(self, role, method, path, *args, **kwargs):
        result = original(self, role, method, path, *args, **kwargs)
        if role == "shipper" and method == "GET" and path.endswith("/orders"):
            return []
        return result

    monkeypatch.setattr(saas_readiness.Readiness, "call", hide_orders)
    lines: list[str] = []
    readiness = _readiness(saas_app, lines)
    assert not readiness.run()
    failed = [r for r in readiness.results if not r.passed]
    assert [r.name for r in failed] == ["shipper confirms order"]
    assert "StepFailed" in failed[0].detail
    assert any(line.startswith("[FAIL] shipper confirms order") for line in lines)
    assert readiness.results[-1].name == "cleanup" and readiness.results[-1].passed
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    assert platform.get("/api/saas/platform/plans").json() == []


def test_saas_readiness_unexpected_error_still_cleans_up(saas_app, monkeypatch) -> None:
    def broken(self):
        raise AttributeError("unexpected body")

    monkeypatch.setattr(saas_readiness.Readiness, "shipper_view", broken)
    lines: list[str] = []
    readiness = _readiness(saas_app, lines)
    assert not readiness.run()
    failed = next(r for r in readiness.results if not r.passed)
    assert failed.name == "shipper views plan"
    assert failed.detail == "AttributeError: unexpected body"
    assert readiness.results[-1].name == "cleanup" and readiness.results[-1].passed
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    assert platform.get("/api/saas/platform/plans").json() == []
    assert saas_readiness.summary(readiness.results)["passed"] is False


def test_saas_readiness_uncaught_error_still_runs_cleanup(saas_app, monkeypatch) -> None:
    class Unexpected(Exception):
        pass

    def broken(self):
        raise Unexpected("boom")

    monkeypatch.setattr(saas_readiness.Readiness, "assign_forwarder", broken)
    readiness = _readiness(saas_app, [])
    with pytest.raises(Unexpected):
        readiness.run()
    assert readiness.results[-1].name == "cleanup" and readiness.results[-1].passed
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    assert platform.get("/api/saas/platform/plans").json() == []


def test_template_fallback_check() -> None:
    assert saas_readiness.template_fallback() == "fallback=provider_error"


def test_seeded_registered_lots_are_baseline_but_new_or_planned_lots_drift(
    client: TestClient, demo_tenants, reset_admin_headers
) -> None:
    with database.begin() as db:
        assert seed_lots(db) > 0
    report = client.get(CONTAMINATION, headers=reset_admin_headers).json()
    assert report["contaminated"] is False
    assert all(not org["drift"] for org in report["orgs"])

    with database.begin() as db:
        seeded = db.query(LotRow).filter(LotRow.shipper_org_id == SHIPPER_ORG_ID).first()
        seeded.status = "planned"
    report = client.get(CONTAMINATION, headers=reset_admin_headers).json()
    shipper = next(o for o in report["orgs"] if o["org_id"] == SHIPPER_ORG_ID)
    assert report["contaminated"] is True
    assert shipper["drift"] == {"lots": 1}


def _seed_lot(org_id: str) -> LotRow:
    with database.begin() as db:
        lot = db.query(LotRow).filter(LotRow.shipper_org_id == org_id).order_by(LotRow.id).first()
        db.expunge(lot)
    return lot


def test_tenants_without_seed_lots_report_missing_seed_lots(
    client: TestClient, demo_tenants, reset_admin_headers
) -> None:
    report = client.get(CONTAMINATION, headers=reset_admin_headers).json()
    shipper = next(o for o in report["orgs"] if o["org_id"] == SHIPPER_ORG_ID)
    assert report["contaminated"] is True
    assert shipper["drift"]["missing_seed_lots"] > 0
    assert "lots" not in shipper["drift"]


def test_changed_or_deleted_seed_lots_drift(
    client: TestClient, demo_tenants, reset_admin_headers
) -> None:
    with database.begin() as db:
        assert seed_lots(db) > 0
    changed, deleted = _seed_lot(SHIPPER_ORG_ID), _seed_lot(OTHER_SHIPPER_ORG_ID)
    jinju, sancheong = login_client(SHIPPER_ORG_ID), login_client(OTHER_SHIPPER_ORG_ID)

    patched = jinju.patch(f"/api/saas/shipper/lots/{changed.id}", json={"pallets": 1})
    removed = sancheong.delete(f"/api/saas/shipper/lots/{deleted.id}")

    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "registered"
    assert removed.status_code == 204, removed.text
    report = client.get(CONTAMINATION, headers=reset_admin_headers).json()
    drift = {o["org_id"]: o["drift"] for o in report["orgs"]}
    assert report["contaminated"] is True
    assert drift[SHIPPER_ORG_ID] == {"lots": 1}
    assert drift[OTHER_SHIPPER_ORG_ID] == {"missing_seed_lots": 1}
