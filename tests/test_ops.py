import json
from datetime import datetime, timedelta
from datetime import time as clock_time

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    OTHER_SHIPPER_ORG_ID,
    PLATFORM_ORG_ID,
    SAAS_CSRF,
    SHIPPER_ORG_ID,
    login_client,
)
from sqlalchemy import func, or_, select

from freshswitch_backend import observability, saas_readiness
from freshswitch_backend.api import health
from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import seed_lots
from freshswitch_backend.persistence.tables import (
    AuthSessionRow,
    ForwarderResourceRow,
    LotRow,
    MembershipRow,
    OrganizationRow,
    PackhouseRow,
    PlanConfirmationRow,
    PlanRow,
    ShipperProfileRow,
    UserRow,
)

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


@pytest.fixture
def ops(saas_app) -> TestClient:
    return TestClient(saas_app, base_url="https://testserver")


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


def test_contamination_requires_admin_token(ops: TestClient, reset_admin_headers) -> None:
    assert ops.get(CONTAMINATION).status_code == 401
    wrong = ops.get(CONTAMINATION, headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401


def test_contamination_disabled_without_configured_token(ops: TestClient) -> None:
    response = ops.get(CONTAMINATION, headers={"Authorization": "Bearer any"})
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


def test_fresh_seed_is_clean(ops: TestClient, reset_admin_headers) -> None:
    with database.begin() as db:
        seed_lots(db)
    report = ops.get(CONTAMINATION, headers=reset_admin_headers).json()
    assert report["contaminated"] is False
    assert report["seed_completed"] is True
    assert report["missing_orgs"] == [] and report["unexpected_sandbox_orgs"] == []
    shipper = next(o for o in report["orgs"] if o["org_id"] == SHIPPER_ORG_ID)
    assert shipper["counts"] == shipper["baseline"]
    assert shipper["baseline"]["members"] == 1
    assert all(not org["drift"] for org in report["orgs"])


def test_demo_plan_and_extra_sandbox_org_are_reported(
    client: TestClient, ops: TestClient, reset_admin_headers
) -> None:
    assert client.post("/api/plans", json={"scenario_id": "default"}).status_code == 201
    with database.begin() as db:
        db.add(OrganizationRow(id="shipper-stray", type="shipper", name="stray", sandbox=True))
    report = ops.get(CONTAMINATION, headers=reset_admin_headers).json()
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


def _counts() -> dict[str, int]:
    with database.begin() as db:
        return {
            table.__tablename__: db.scalar(select(func.count()).select_from(table))
            for table in (
                UserRow,
                OrganizationRow,
                MembershipRow,
                AuthSessionRow,
                PlanRow,
                LotRow,
                ForwarderResourceRow,
                PackhouseRow,
                ShipperProfileRow,
            )
        }


def _assert_purged(readiness: saas_readiness.Readiness, before: dict[str, int]) -> None:
    emails = list(readiness.emails.values())
    org_ids = [readiness.state[f"{role}_org"] for role in saas_readiness.ROLES]
    user_ids = [readiness.state[f"{role}_user"] for role in saas_readiness.ROLES]
    assert len(emails) == len(set(org_ids)) == len(set(user_ids)) == 2
    with database.begin() as db:
        leftovers = {
            "users": select(UserRow.id).where(
                or_(UserRow.id.in_(user_ids), UserRow.email.in_(emails))
            ),
            "orgs": select(OrganizationRow.id).where(OrganizationRow.id.in_(org_ids)),
            "memberships": select(MembershipRow.org_id).where(MembershipRow.org_id.in_(org_ids)),
            "sessions": select(AuthSessionRow.user_id).where(AuthSessionRow.user_id.in_(user_ids)),
            "plans": select(PlanRow.plan_id).where(PlanRow.org_id.in_(org_ids)),
            "lots": select(LotRow.id).where(LotRow.shipper_org_id.in_(org_ids)),
            "resources": select(ForwarderResourceRow.id).where(
                ForwarderResourceRow.org_id.in_(org_ids)
            ),
            "packhouses": select(PackhouseRow.id).where(PackhouseRow.org_id.in_(org_ids)),
            "profiles": select(ShipperProfileRow.id).where(ShipperProfileRow.org_id.in_(org_ids)),
        }
        assert {name: list(db.scalars(query)) for name, query in leftovers.items()} == {
            name: [] for name in leftovers
        }
    assert _counts() == before
    assert readiness.results[-1].name == "cleanup" and readiness.results[-1].passed


def _readiness(saas_app, lines: list[str]) -> saas_readiness.Readiness:
    return saas_readiness.Readiness(
        base_url="https://testserver/api",
        make_client=lambda: TestClient(saas_app, base_url="https://testserver"),
        out=lines.append,
    )


def test_saas_readiness_flow_passes_in_process(saas_app) -> None:
    before = _counts()
    lines: list[str] = []
    readiness = _readiness(saas_app, lines)
    assert readiness.run(), "\n".join(lines)
    names = [r.name for r in readiness.results]
    assert names[0] == "health" and names[-1] == "cleanup"
    assert "demo-login" not in " ".join(names)
    assert names.index("shipper signs up") < names.index("forwarder generates plan")
    assert names.index("shipper accepts plan") < names.index("forwarder change event")
    assert "forwarder confirms order" in names and "settlement lookup" in names
    assert all(r.ms >= 0 for r in readiness.results)
    assert readiness.state["plan_id"] and readiness.state["order_id"]
    assert readiness.results[-1].detail == (
        "users=2 orgs=2 plans=1 lots=1 resources=1 packhouses=1"
    )
    _assert_purged(readiness, before)
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    assert platform.get("/api/saas/platform/plans").json() == []


def test_saas_readiness_uses_real_accounts_not_demo_login(saas_app, monkeypatch) -> None:
    paths: list[str] = []
    original = saas_readiness.Readiness.call

    def record(self, role, method, path, *args, **kwargs):
        paths.append(path)
        return original(self, role, method, path, *args, **kwargs)

    monkeypatch.setattr(saas_readiness.Readiness, "call", record)
    readiness = _readiness(saas_app, [])
    readiness.keep = True
    try:
        assert readiness.run()
        with database.begin() as db:
            orgs = {
                row.id: row
                for row in db.scalars(
                    select(OrganizationRow).where(
                        OrganizationRow.id.in_(
                            [readiness.state[f"{r}_org"] for r in saas_readiness.ROLES]
                        )
                    )
                )
            }
            plan = db.get(PlanRow, readiness.state["plan_id"])
            assert plan.org_id == readiness.state["forwarder_org"]
        assert {o.type for o in orgs.values()} == {"shipper", "forwarder"}
        assert not any(o.sandbox or o.world_id for o in orgs.values())
        assert "/saas/forwarder/plans" in paths
        assert "/saas/platform/plans" not in paths
        assert not any("demo-login" in p or "sandbox" in p for p in paths)
        assert readiness.results[-1].name != "cleanup"
    finally:
        purged = saas_readiness.purge_accounts(readiness.emails.values())
    assert (purged.users, purged.orgs, purged.plans) == (2, 2, 1)


def test_saas_readiness_cleanup_keeps_orgs_held_by_another_forwarders_plan(
    saas_app, monkeypatch
) -> None:
    real = TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF)
    signup = {
        "email": "real-forwarder@example.com",
        "password": "real-forwarder-pw-123",
        "name": "실사용 포워더",
        "org_name": "실사용 포워더",
        "org_type": "forwarder",
    }
    created = real.post("/api/auth/signup", json=signup)
    assert created.status_code == 201, created.text
    real_org = created.json()["active_org"]["org_id"]
    grabbed: dict[str, str] = {}
    original = saas_readiness.Readiness.register

    def register_then_grabbed(self):
        detail = original(self)
        lot_id = self.state["lot_id"]
        assert lot_id in {lot["id"] for lot in real.get("/api/saas/forwarder/lots").json()}
        cutoff = datetime.combine(self.ready_date(), clock_time(16), saas_readiness.KST)
        resource = {
            "name": "실사용 냉장 컨테이너",
            "kind": "40RH-CA",
            "capacity_pallets": 20,
            "temp_min_c": -1,
            "temp_max_c": 4,
            "vessel_name": "실사용 선박",
            "terminal": "실사용 터미널",
            "origin": "KRPUS",
            "destination": saas_readiness.DESTINATION,
            "etd": (cutoff + timedelta(days=1, hours=2)).isoformat(),
            "cy_cutoff": cutoff.isoformat(),
            "transit_days": 3.5,
            "enabled": True,
        }
        assert real.post("/api/saas/forwarder/resources", json=resource).status_code == 201
        made = real.post("/api/saas/forwarder/plans", json={"lot_ids": [lot_id]})
        assert made.status_code == 201, made.text
        grabbed["plan_id"] = made.json()["plan_id"]
        return detail

    monkeypatch.setattr(saas_readiness.Readiness, "register", register_then_grabbed)
    lines: list[str] = []
    readiness = _readiness(saas_app, lines)
    assert not readiness.run()
    names = [r.name for r in readiness.results]
    assert names.index("forwarder registers resource") < names.index("shipper registers lot")
    assert [r.name for r in readiness.results if not r.passed] == [
        "forwarder generates plan",
        "cleanup",
    ]
    plan_id, shipper_org = grabbed["plan_id"], readiness.state["shipper_org"]
    cleanup = readiness.results[-1].detail
    assert plan_id in cleanup and shipper_org in cleanup
    assert "users=1 orgs=1 plans=0 lots=0 resources=1 packhouses=0" in cleanup
    with database.begin() as db:
        assert db.get(PlanRow, plan_id).org_id == real_org
        assert db.get(LotRow, readiness.state["lot_id"]) is not None
        assert db.get(OrganizationRow, shipper_org) is not None
        assert db.get(UserRow, readiness.state["shipper_user"]) is not None
        assert db.get(OrganizationRow, readiness.state["forwarder_org"]) is None
        assert db.get(UserRow, readiness.state["forwarder_user"]) is None
        confirmations = list(
            db.scalars(select(PlanConfirmationRow).where(PlanConfirmationRow.plan_id == plan_id))
        )
    assert {c.shipper_org_id for c in confirmations} == {shipper_org}
    state = real.get(f"/api/saas/forwarder/plans/{plan_id}/confirmation").json()
    assert state["status"] == "pending_shipper_confirmation"
    assert {c["shipper_org_id"] for c in state["confirmations"]} == {shipper_org}
    with database.begin() as db:
        db.delete(db.get(PlanRow, plan_id))
    purged = saas_readiness.purge_accounts(readiness.emails.values())
    assert (purged.users, purged.orgs, purged.lots, purged.held_orgs) == (1, 1, 1, [])
    assert saas_readiness.leftover_accounts(readiness.emails.values()) == 0


def test_saas_readiness_reports_failure(saas_app) -> None:
    readiness = saas_readiness.Readiness(
        base_url="https://testserver/missing",
        make_client=lambda: TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF),
        out=lambda line: None,
    )
    assert not readiness.run()
    assert readiness.results[0].passed is False
    assert saas_readiness.summary(readiness.results)["passed"] is False


def test_saas_readiness_missing_order_fails_step_and_cleans_up(saas_app, monkeypatch) -> None:
    before = _counts()
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
    assert readiness.state["order_id"]
    _assert_purged(readiness, before)
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    assert platform.get("/api/saas/platform/plans").json() == []


def test_saas_readiness_unexpected_error_still_cleans_up(saas_app, monkeypatch) -> None:
    before = _counts()

    def broken(self):
        raise AttributeError("unexpected body")

    monkeypatch.setattr(saas_readiness.Readiness, "shipper_view", broken)
    lines: list[str] = []
    readiness = _readiness(saas_app, lines)
    assert not readiness.run()
    failed = next(r for r in readiness.results if not r.passed)
    assert failed.name == "shipper views plan"
    assert failed.detail == "AttributeError: unexpected body"
    assert readiness.state["plan_id"]
    _assert_purged(readiness, before)
    assert saas_readiness.summary(readiness.results)["passed"] is False


def test_saas_readiness_uncaught_error_still_runs_cleanup(saas_app, monkeypatch) -> None:
    before = _counts()

    class Unexpected(Exception):
        pass

    def broken(self):
        raise Unexpected("boom")

    monkeypatch.setattr(saas_readiness.Readiness, "accept_plan", broken)
    readiness = _readiness(saas_app, [])
    with pytest.raises(Unexpected):
        readiness.run()
    assert readiness.state["plan_id"]
    _assert_purged(readiness, before)


def test_template_fallback_check() -> None:
    assert saas_readiness.template_fallback() == "fallback=provider_error"


def test_seeded_registered_lots_are_baseline_but_new_or_planned_lots_drift(
    ops: TestClient, reset_admin_headers
) -> None:
    with database.begin() as db:
        assert seed_lots(db) > 0
    report = ops.get(CONTAMINATION, headers=reset_admin_headers).json()
    assert report["contaminated"] is False
    assert all(not org["drift"] for org in report["orgs"])

    with database.begin() as db:
        seeded = db.query(LotRow).filter(LotRow.shipper_org_id == SHIPPER_ORG_ID).first()
        seeded.status = "planned"
    report = ops.get(CONTAMINATION, headers=reset_admin_headers).json()
    shipper = next(o for o in report["orgs"] if o["org_id"] == SHIPPER_ORG_ID)
    assert report["contaminated"] is True
    assert shipper["drift"] == {"lots": 1}


def _seed_lot(org_id: str) -> LotRow:
    with database.begin() as db:
        lot = db.query(LotRow).filter(LotRow.shipper_org_id == org_id).order_by(LotRow.id).first()
        db.expunge(lot)
    return lot


def test_tenants_without_seed_lots_report_missing_seed_lots(
    ops: TestClient, reset_admin_headers
) -> None:
    report = ops.get(CONTAMINATION, headers=reset_admin_headers).json()
    shipper = next(o for o in report["orgs"] if o["org_id"] == SHIPPER_ORG_ID)
    assert report["contaminated"] is True
    assert shipper["drift"]["missing_seed_lots"] > 0
    assert "lots" not in shipper["drift"]


def test_changed_or_deleted_seed_lots_drift(ops: TestClient, reset_admin_headers) -> None:
    with database.begin() as db:
        assert seed_lots(db) > 0
    changed, deleted = _seed_lot(SHIPPER_ORG_ID), _seed_lot(OTHER_SHIPPER_ORG_ID)
    jinju, sancheong = login_client(SHIPPER_ORG_ID), login_client(OTHER_SHIPPER_ORG_ID)

    patched = jinju.patch(f"/api/saas/shipper/lots/{changed.id}", json={"pallets": 1})
    removed = sancheong.delete(f"/api/saas/shipper/lots/{deleted.id}")

    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "registered"
    assert removed.status_code == 204, removed.text
    report = ops.get(CONTAMINATION, headers=reset_admin_headers).json()
    drift = {o["org_id"]: o["drift"] for o in report["orgs"]}
    assert report["contaminated"] is True
    assert drift[SHIPPER_ORG_ID] == {"lots": 1, "members": 1}
    assert drift[OTHER_SHIPPER_ORG_ID] == {"missing_seed_lots": 1, "members": 1}
