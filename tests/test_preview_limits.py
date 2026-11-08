import threading
import time

import pytest
from fastapi import HTTPException, Request
from saas_fixtures import PLATFORM_ORG_ID, login_client
from sqlalchemy import func, select

from freshswitch_backend.api import compute_limits, saas_platform
from freshswitch_backend.api.auth import Principal, RateRule
from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import run_lots
from freshswitch_backend.persistence.tables import MetaRow, ObservationRow, PlanRow
from freshswitch_backend.planning.budget import PlanningTimeout
from freshswitch_backend.quality import quality_history
from freshswitch_backend.quality.store import ScopeRef

PREVIEW = "/api/saas/platform/plans/preview"
CREATE = "/api/saas/platform/plans"
TENANT_CREATE = "/api/plans"


@pytest.fixture
def saas_app(demo_tenants):
    run_lots()
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


@pytest.fixture
def platform(saas_app):
    return login_client(PLATFORM_ORG_ID, saas_app)


@pytest.fixture
def pool(platform) -> list[dict]:
    lots = platform.get("/api/saas/platform/lots").json()
    assert len(lots) >= 3
    return lots


@pytest.fixture
def build_calls(monkeypatch) -> list[int]:
    calls: list[int] = []
    original = saas_platform.build_plan

    def counting(scenario, **options):
        calls.append(len(scenario.lots))
        return original(scenario, **options)

    monkeypatch.setattr(saas_platform, "build_plan", counting)
    return calls


@pytest.fixture
def gate(monkeypatch) -> compute_limits.ComputeGate:
    fresh = compute_limits.ComputeGate(slots=1, wait_seconds=0.05)
    monkeypatch.setattr(compute_limits, "gate", fresh)
    return fresh


def _ids(pool: list[dict], count: int | None = None) -> list[str]:
    return [lot["id"] for lot in pool[:count]]


def _plan_count() -> int:
    with database.begin() as db:
        return db.scalar(select(func.count()).select_from(PlanRow)) or 0


def test_too_many_lots_is_422_before_lookup(platform, build_calls) -> None:
    ids = [f"lot-{n}" for n in range(compute_limits.MAX_PLAN_LOTS + 1)]
    for path in (PREVIEW, CREATE, TENANT_CREATE):
        response = platform.post(path, json={"lot_ids": ids})
        assert response.status_code == 422, (path, response.text)
        assert str(compute_limits.MAX_PLAN_LOTS) in response.json()["detail"]
    assert build_calls == []
    assert _plan_count() == 0


def test_lot_limit_counts_included_pool(platform, pool, build_calls, monkeypatch) -> None:
    monkeypatch.setattr(compute_limits, "MAX_PLAN_LOTS", 2)
    for path in (PREVIEW, CREATE, TENANT_CREATE):
        listed = platform.post(path, json={"lot_ids": _ids(pool, 3)})
        assert listed.status_code == 422, (path, listed.text)
        pooled = platform.post(path, json={"lot_ids": _ids(pool, 1), "include_registered": True})
        assert pooled.status_code == 422, (path, pooled.text)
    assert build_calls == []
    assert _plan_count() == 0
    statuses = {lot["status"] for lot in platform.get("/api/saas/platform/lots").json()}
    assert statuses == {"registered"}


def _fake_request(client_app, host: str = "testclient") -> Request:
    return Request({"type": "http", "app": client_app, "headers": [], "client": (host, 50000)})


def test_preview_rate_limit_per_visitor(platform, pool, saas_app, monkeypatch) -> None:
    monkeypatch.setattr(compute_limits, "PREVIEW_RATE", RateRule(per_client=2, window_seconds=60))
    first = {"lot_ids": _ids(pool, 1)}
    second = {"lot_ids": _ids(pool, 2)}
    assert platform.post(PREVIEW, json=first).status_code == 200
    assert platform.post(PREVIEW, json=second).status_code == 200
    assert platform.post(PREVIEW, json=first).status_code == 200
    assert platform.post(PREVIEW, json=second).status_code == 200
    limited = platform.post(PREVIEW, json={"lot_ids": _ids(pool, 3)})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert platform.post(CREATE, json=first).status_code == 201

    me = platform.get("/api/me")
    assert me.status_code == 200, me.text
    body = me.json()
    user_id = body["user"]["id"]
    world_id = body["world"]["world_id"]
    org_id = body["active_org"]["org_id"]
    same_visitor = Principal(user_id, org_id, "platform", "admin", world_id)
    with pytest.raises(HTTPException) as caught:
        compute_limits.check_preview_rate(_fake_request(saas_app), same_visitor)
    assert caught.value.status_code == 429
    other_org = Principal(user_id, "platform-other", "platform", "admin")
    compute_limits.check_preview_rate(_fake_request(saas_app), other_org)
    other_host = Principal(user_id, org_id, "platform", "admin", world_id)
    compute_limits.check_preview_rate(_fake_request(saas_app, "10.0.0.9"), other_host)


def test_preview_org_cap_spans_visitors(saas_app, monkeypatch) -> None:
    monkeypatch.setattr(
        compute_limits, "PREVIEW_ORG_RATE", RateRule(per_client=2, window_seconds=60)
    )
    for host in ("10.0.0.1", "10.0.0.2"):
        principal = Principal(f"user-{host}", PLATFORM_ORG_ID, "platform", "admin")
        compute_limits.check_preview_rate(_fake_request(saas_app, host), principal)
    late = Principal("user-late", PLATFORM_ORG_ID, "platform", "admin")
    with pytest.raises(HTTPException) as caught:
        compute_limits.check_preview_rate(_fake_request(saas_app, "10.0.0.3"), late)
    assert caught.value.status_code == 429
    elsewhere = Principal("user-late", "platform-other", "platform", "admin")
    compute_limits.check_preview_rate(_fake_request(saas_app, "10.0.0.3"), elsewhere)


def test_cache_hits_do_not_use_rate_budget(platform, pool, build_calls, monkeypatch) -> None:
    monkeypatch.setattr(compute_limits, "PREVIEW_RATE", RateRule(per_client=1, window_seconds=60))
    body = {"lot_ids": _ids(pool)}
    for _ in range(5):
        assert platform.post(PREVIEW, json=body).status_code == 200
    assert len(build_calls) == 1
    assert platform.post(PREVIEW, json={"lot_ids": _ids(pool, 1)}).status_code == 429
    assert len(build_calls) == 1


def test_identical_preview_hits_cache(platform, pool, build_calls) -> None:
    body = {"lot_ids": _ids(pool)}
    first = platform.post(PREVIEW, json=body)
    second = platform.post(PREVIEW, json=body)
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert len(build_calls) == 1

    learned = platform.post(PREVIEW, json={**body, "use_learned_loss": True})
    assert learned.status_code == 200
    assert len(build_calls) == 2


def test_lot_change_invalidates_cache(platform, pool, build_calls, saas_app) -> None:
    body = {"lot_ids": _ids(pool)}
    assert platform.post(PREVIEW, json=body).status_code == 200
    target = pool[0]
    shipper = login_client(target["shipper_org_id"], saas_app)
    patched = shipper.patch(
        f"/api/saas/shipper/lots/{target['id']}", json={"pallets": target["pallets"] + 1}
    )
    assert patched.status_code == 200, patched.text
    after = platform.post(PREVIEW, json=body)
    assert after.status_code == 200
    assert len(build_calls) == 2
    lot = next(x for x in after.json()["scenario"]["lots"] if x["id"] == target["id"])
    assert lot["pallets"] == target["pallets"] + 1


def test_saturated_compute_returns_503(platform, pool, gate, build_calls) -> None:
    body = {"lot_ids": _ids(pool)}
    with gate.slot():
        preview = platform.post(PREVIEW, json=body)
        created = platform.post(CREATE, json=body)
    assert preview.status_code == 503
    assert preview.headers["Retry-After"] == str(compute_limits.RETRY_AFTER_SECONDS)
    assert created.status_code == 503
    assert build_calls == []
    assert _plan_count() == 0
    assert platform.post(PREVIEW, json=body).status_code == 200


def test_preview_deadline_returns_503(platform, pool, gate, monkeypatch) -> None:
    release = threading.Event()

    def slow(scenario, **options):
        release.wait(5)
        raise AssertionError("deadline should have cut this preview")

    monkeypatch.setattr(saas_platform, "build_plan", slow)
    monkeypatch.setattr(compute_limits, "PREVIEW_DEADLINE_SECONDS", 0.5)
    started = time.perf_counter()
    try:
        response = platform.post(PREVIEW, json={"lot_ids": _ids(pool)})
    finally:
        release.set()
    assert response.status_code == 503
    assert response.json()["detail"] == compute_limits.PREVIEW_TIMEOUT
    assert time.perf_counter() - started < 3


def test_preview_planning_timeout_is_503(platform, pool, gate, monkeypatch) -> None:
    def expired(scenario, **options):
        raise PlanningTimeout("budget exhausted")

    monkeypatch.setattr(saas_platform, "build_plan", expired)
    response = platform.post(PREVIEW, json={"lot_ids": _ids(pool)})
    assert response.status_code == 503
    assert response.json()["detail"] == compute_limits.PREVIEW_TIMEOUT
    assert response.headers["Retry-After"] == str(compute_limits.RETRY_AFTER_SECONDS)
    assert platform.post(PREVIEW, json={"lot_ids": _ids(pool, 1)}).status_code == 503


def _counts() -> tuple[int, int, list[str]]:
    with database.begin() as db:
        observations = db.scalar(select(func.count()).select_from(ObservationRow)) or 0
        keys = sorted(db.scalars(select(MetaRow.key)))
        plans = db.scalar(select(func.count()).select_from(PlanRow)) or 0
    return observations, plans, keys


def test_learned_preview_is_read_only(platform, pool) -> None:
    before = _counts()
    response = platform.post(PREVIEW, json={"lot_ids": _ids(pool), "use_learned_loss": True})
    assert response.status_code == 200, response.text
    assert _counts() == before
    adjustments = response.json()["scenario"]["cost_params"]["loss_adjustments"]
    assert adjustments

    world_id = platform.get("/api/me").json()["world"]["world_id"]
    assert len(quality_history.observations(ScopeRef("sandbox", world_id))) == len(
        quality_history._seed
    )
    assert _counts() == before
    quality_history.ensure_seeded()
    fresh = login_client(
        PLATFORM_ORG_ID, create_app(get_settings().model_copy(update={"saas_mode": True}))
    )
    fresh_ids = [lot["id"] for lot in fresh.get("/api/saas/platform/lots").json()[: len(pool)]]
    seeded = fresh.post(PREVIEW, json={"lot_ids": fresh_ids, "use_learned_loss": True})
    assert seeded.status_code == 200, seeded.text
    assert seeded.json()["scenario"]["cost_params"]["loss_adjustments"] == adjustments
    assert _counts()[0] > before[0]
