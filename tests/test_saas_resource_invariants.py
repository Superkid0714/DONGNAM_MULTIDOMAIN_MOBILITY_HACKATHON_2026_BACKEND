import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from conftest import FROZEN_NOW
from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, password_client, saas_app
from sqlalchemy import func, select
from test_onboarding import _lot, _onboarded, _signup
from test_saas_api import _register_all

from freshswitch_backend.api.plans import CreatePlanRequest, tenant_plan_scenario
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.lots import ANY_ORG, lot_store
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import create_platform
from freshswitch_backend.persistence.tables import (
    ForwarderResourceRow,
    MembershipRow,
    OrderRow,
    PlanForwarderRow,
    PlanRow,
)
from freshswitch_backend.planning.store import ContainerBooked, SessionStore, store

RESOURCES = "/api/saas/forwarder/resources"
PLATFORM_RESOURCES = "/api/saas/platform/resources"
PLANS = "/api/saas/forwarder/plans"
PASSWORD = "resource-invariant-password"
RESOURCE = {
    "name": "직접 등록한 냉장 컨테이너",
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


def _platform(suffix: str = "one") -> TestClient:
    email = f"resource-platform-{suffix}@example.com"
    with database.begin() as db:
        create_platform(
            db,
            name=f"자원 운영 플랫폼 {suffix}",
            email=email,
            user_name="운영자",
            password=PASSWORD,
            org_id=f"resource-platform-{suffix}",
        )
    return password_client(saas_app, email, PASSWORD)


def _org(client: TestClient) -> str:
    response = client.get("/api/me")
    assert response.status_code == 200, response.text
    return response.json()["active_org"]["org_id"]


def _forwarder(suffix: str = "one") -> TestClient:
    return _signup(f"resource-forwarder-{suffix}@example.com", f"자원 포워더 {suffix}", "forwarder")


def _resource(client: TestClient, **changes) -> dict:
    response = client.post(RESOURCES, json={**RESOURCE, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def _registered(suffix: str = "one") -> tuple[TestClient, str]:
    client, _ = _onboarded(f"resource-shipper-{suffix}@example.com", f"출하 업체 {suffix}")
    response = client.post("/api/saas/shipper/lots", json=_lot(pallets=16))
    assert response.status_code == 201, response.text
    return client, response.json()["registered"]["id"]


def _plan_count() -> int:
    with database.begin() as db:
        return db.scalar(select(func.count()).select_from(PlanRow)) or 0


def _sea(payload: dict) -> set[str]:
    return {
        item["container_id"] for item in payload["plan"]["assignments"] if item["mode"] == "sea"
    }


def _listed(client: TestClient, resource_id: str) -> dict:
    response = client.get(RESOURCES)
    assert response.status_code == 200, response.text
    return next(item for item in response.json() if item["id"] == resource_id)


def test_forwarder_resource_crud_enforces_owner_and_role_boundaries():
    owner, outsider = _forwarder("owner"), _forwarder("outsider")
    platform = _platform()
    shipper, _ = _registered()
    resource = _resource(owner)
    resource_id = resource["id"]
    assert resource["forwarder_org_id"] == _org(owner)
    assert resource["allocated_plan_id"] is None
    assert resource["status"] == "available"
    assert resource["available_pallets"] == 20
    assert outsider.get(RESOURCES).json() == []
    assert (
        outsider.patch(f"{RESOURCES}/{resource_id}", json={"name": "외부 수정"}).status_code == 404
    )
    assert outsider.delete(f"{RESOURCES}/{resource_id}").status_code == 404
    for forbidden in (shipper, platform):
        assert forbidden.get(RESOURCES).status_code == 403
        assert forbidden.post(RESOURCES, json=RESOURCE).status_code == 403
    assert owner.get(PLATFORM_RESOURCES).status_code == 403
    exposed = platform.get(PLATFORM_RESOURCES)
    assert exposed.status_code == 200, exposed.text
    assert [item["id"] for item in exposed.json()] == [resource_id]
    patched = owner.patch(f"{RESOURCES}/{resource_id}", json={"name": "변경된 자원"})
    assert patched.status_code == 200, patched.text
    assert _listed(owner, resource_id)["name"] == "변경된 자원"
    assert owner.delete(f"{RESOURCES}/{resource_id}").status_code == 204
    assert owner.get(RESOURCES).json() == []


@pytest.mark.parametrize("disabled", [False, True])
def test_live_planning_cannot_fall_back_to_fictional_scenario_containers(disabled):
    planner = _forwarder()
    shipper, lot_id = _registered()
    if disabled:
        _resource(planner, enabled=False)
    for path in (f"{PLANS}/preview", PLANS):
        response = planner.post(path, json={"lot_ids": [lot_id]})
        assert response.status_code == 422, response.text
    assert _plan_count() == 0
    assert shipper.get("/api/saas/shipper/lots").json()[0]["status"] == "registered"


def test_preview_does_not_allocate_and_single_owner_plan_reserves_then_releases_whole_container():
    owner = _forwarder()
    _, lot_id = _registered()
    resource = _resource(owner)
    resource_id = resource["id"]
    preview = owner.post(f"{PLANS}/preview", json={"lot_ids": [lot_id]})
    assert preview.status_code == 200, preview.text
    assert _sea(preview.json()) == {resource_id}, str(
        {
            "assignments": preview.json()["plan"]["assignments"],
            "unassigned": preview.json()["plan"]["unassigned"],
            "collection_reassigned": preview.json()["plan"]["collection_reassigned"],
        }
    )
    assert _listed(owner, resource_id)["allocated_plan_id"] is None
    assert _plan_count() == 0
    created = owner.post(PLANS, json={"lot_ids": [lot_id]})
    assert created.status_code == 201, created.text
    payload = created.json()
    plan_id = payload["plan_id"]
    assert _sea(payload) == {resource_id}
    assert payload["status"] == "pending_shipper_confirmation"
    assert owner.get(f"{PLANS}/{plan_id}/orders").json() == []
    assert {item["id"] for item in payload["scenario"]["containers"]} == {resource_id}
    assert {item["forwarder_org_id"] for item in payload["scenario"]["containers"]} == {_org(owner)}
    allocated = _listed(owner, resource_id)
    assert allocated["allocated_plan_id"] == plan_id
    assert allocated["allocated_pallets"] == 16
    assert allocated["available_pallets"] == 0
    assert allocated["status"] == "allocated"
    assert owner.get(PLANS).json()[0]["forwarder"]["org_id"] == _org(owner)
    assert (
        owner.patch(f"{RESOURCES}/{resource_id}", json={"capacity_pallets": 19}).status_code == 409
    )
    assert owner.patch(f"{RESOURCES}/{resource_id}", json={"enabled": False}).status_code == 409
    assert owner.delete(f"{RESOURCES}/{resource_id}").status_code == 409
    assert owner.delete(f"/api/plans/{plan_id}").status_code == 204
    released = _listed(owner, resource_id)
    assert released["allocated_plan_id"] is None
    assert released["allocated_pallets"] == 0
    assert released["available_pallets"] == 20
    assert released["status"] == "available"
    assert owner.delete(f"{RESOURCES}/{resource_id}").status_code == 204


def test_forwarder_plans_use_own_resources_and_reject_foreign_owner_override():
    first, second = _forwarder("first"), _forwarder("second")
    a, b = _resource(first), _resource(second)
    _, lot_id = _registered()
    for path in (f"{PLANS}/preview", PLANS):
        response = first.post(path, json={"lot_ids": [lot_id], "forwarder_org_id": _org(second)})
        assert response.status_code in (403, 422), response.text
    assert _plan_count() == 0
    created = first.post(PLANS, json={"lot_ids": [lot_id]})
    assert created.status_code == 201, created.text
    assert _sea(created.json()) == {a["id"]}
    assert {item["id"] for item in created.json()["scenario"]["containers"]} == {a["id"]}
    assert _listed(second, b["id"])["allocated_plan_id"] is None
    assert second.get(f"{PLANS}/{created.json()['plan_id']}").status_code == 404
    assert first.get(PLANS).json()[0]["forwarder"]["org_id"] == _org(first)
    _, next_lot_id = _registered("second")
    next_plan = second.post(PLANS, json={"lot_ids": [next_lot_id]})
    assert next_plan.status_code == 201, next_plan.text
    assert _sea(next_plan.json()) == {b["id"]}
    assert first.get(f"{PLANS}/{next_plan.json()['plan_id']}").status_code == 404


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_allocation_rechecks_resource_snapshot_after_solve_and_rolls_back_stale_plan(
    monkeypatch, mutation
):
    owner = _forwarder()
    shipper, lot_id = _registered()
    resource = _resource(owner)
    module = importlib.import_module("freshswitch_backend.planning.store")
    original = module.build_plan
    solved, resume = threading.Event(), threading.Event()

    def paused(scenario):
        result = original(scenario)
        solved.set()
        assert resume.wait(30), "resource race test did not release optimizer"
        return result

    monkeypatch.setattr(module, "build_plan", paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(owner.post, PLANS, json={"lot_ids": [lot_id]})
        try:
            assert solved.wait(30), "optimizer did not receive resource snapshot"
            path = f"{RESOURCES}/{resource['id']}"
            changed = (
                owner.patch(path, json={"capacity_pallets": 10})
                if mutation == "edit"
                else owner.delete(path)
            )
            assert changed.status_code == (200 if mutation == "edit" else 204), changed.text
        finally:
            resume.set()
        response = pending.result(timeout=30)
    assert response.status_code == 409, response.text
    assert _plan_count() == 0
    assert shipper.get("/api/saas/shipper/lots").json()[0]["status"] == "registered"
    if mutation == "edit":
        assert _listed(owner, resource["id"])["allocated_plan_id"] is None


def test_independent_workers_cannot_double_allocate_one_physical_container_across_platforms(
    monkeypatch,
):
    platforms = [_platform("first"), _platform("second")]
    owner = _forwarder()
    resource = _resource(owner)
    registered = [_registered("first"), _registered("second")]
    snapshots = [
        tenant_plan_scenario(CreatePlanRequest(lot_ids=[lot_id]), _org(platform))
        for platform, (_, lot_id) in zip(platforms, registered, strict=True)
    ]
    module = importlib.import_module("freshswitch_backend.planning.store")
    original = module.build_plan
    barrier = threading.Barrier(2)

    def synchronized(scenario):
        result = original(scenario)
        barrier.wait(timeout=30)
        return result

    monkeypatch.setattr(module, "build_plan", synchronized)
    workers = [SessionStore(db=database), SessionStore(db=database)]
    for worker in workers:
        worker.clock = lambda: FROZEN_NOW

    def allocate(index):
        scenario, lot_ids = snapshots[index]
        try:
            return workers[index].create(
                scenario, lot_ids, org_id=_org(platforms[index]), lot_org_id=ANY_ORG, exclusive=True
            )
        except ContainerBooked as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(allocate, range(2)))
    winners = [result for result in results if not isinstance(result, ContainerBooked)]
    assert len(winners) == 1
    assert sum(isinstance(result, ContainerBooked) for result in results) == 1
    assert _plan_count() == 1
    winner = winners[0]
    assert _sea(winner.model_dump(mode="json")) == {resource["id"]}
    assert _listed(owner, resource["id"])["allocated_plan_id"] == winner.plan_id
    states = [lot_store.get_many([lot_id], org_id=ANY_ORG)[0].status for _, lot_id in registered]
    assert sorted(states) == ["planned", "registered"]
    with database.begin() as db:
        row = db.get(ForwarderResourceRow, resource["id"])
        assert row is not None
        assert row.allocated_plan_id == winner.plan_id


def test_two_forwarders_cannot_claim_the_same_registered_shipment(monkeypatch):
    forwarders = [_forwarder("first"), _forwarder("second")]
    resources = [_resource(client) for client in forwarders]
    shipper, lot_id = _registered()
    module = importlib.import_module("freshswitch_backend.planning.store")
    original = module.build_plan
    barrier = threading.Barrier(2)

    def synchronized(scenario):
        built = original(scenario)
        barrier.wait(timeout=30)
        return built

    monkeypatch.setattr(module, "build_plan", synchronized)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(lambda client: client.post(PLANS, json={"lot_ids": [lot_id]}), forwarders)
        )
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert _plan_count() == 1
    winner = next(response.json() for response in responses if response.status_code == 201)
    allocations = [
        _listed(client, resource["id"])["allocated_plan_id"]
        for client, resource in zip(forwarders, resources, strict=True)
    ]
    assert allocations.count(winner["plan_id"]) == 1
    assert allocations.count(None) == 1
    assert shipper.get("/api/saas/shipper/lots").json()[0]["status"] == "planned"


def test_legacy_platform_plans_preserve_admin_read_and_assigned_forwarder_scope():
    platform, owner, outsider = _platform(), _forwarder(), _forwarder("other")
    resource = _resource(owner)
    scenario = get_scenario("default")
    assert scenario is not None
    legacy = store.create(scenario, org_id=_org(platform))
    owner_id = _org(owner)
    with database.begin() as db:
        db.add(
            PlanForwarderRow(plan_id=legacy.plan_id, forwarder_org_id=owner_id, assigned_by=None)
        )
        row = db.get(PlanRow, legacy.plan_id)
        assert row is not None
        for field in ("payload", "committed"):
            payload = deepcopy(getattr(row, field))
            for container in payload["scenario"]["containers"]:
                container.pop("forwarder_org_id", None)
                container.pop("resource_revision", None)
            setattr(row, field, payload)
    loaded = SessionStore(db=database).get(legacy.plan_id, org_id=_org(platform))
    assert loaded is not None
    assert {item.id for item in loaded.scenario.containers} == {
        item.id for item in scenario.containers
    }
    assert all(item.forwarder_org_id is None for item in loaded.scenario.containers)
    assert platform.get(f"/api/plans/{legacy.plan_id}").status_code == 200
    assert owner.get(f"{PLANS}/{legacy.plan_id}").status_code == 200
    assert outsider.get(f"{PLANS}/{legacy.plan_id}").status_code == 404
    assert _listed(owner, resource["id"])["allocated_plan_id"] is None


def test_platform_admin_is_readonly_and_cannot_create_or_reassign_operational_plans():
    platform, owner = _platform(), _forwarder()
    resource = _resource(owner)
    _, lot_id = _registered()
    for path in (
        "/api/saas/platform/plans",
        "/api/saas/platform/plans/preview",
        PLANS,
        f"{PLANS}/preview",
        "/api/plans",
    ):
        response = platform.post(path, json={"lot_ids": [lot_id]})
        assert response.status_code == 403, (path, response.text)
    assert _plan_count() == 0
    created = owner.post(PLANS, json={"lot_ids": [lot_id]})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    overview = platform.get("/api/saas/platform/plans")
    assert overview.status_code == 200, overview.text
    assert plan_id in {item["plan_id"] for item in overview.json()}
    assert platform.get(f"/api/plans/{plan_id}").status_code == 200
    assert platform.get(PLATFORM_RESOURCES).status_code == 200
    moved = platform.put(
        f"/api/saas/platform/plans/{plan_id}/forwarder", json={"forwarder_org_id": _org(owner)}
    )
    assert moved.status_code == 403, moved.text
    assert platform.delete(f"/api/plans/{plan_id}").status_code == 403
    assert _listed(owner, resource["id"])["allocated_plan_id"] == plan_id


def test_explicit_sandbox_demo_keeps_demo_resources_and_cannot_see_live_inventory(
    platform_client, saas_login
):
    owner = _forwarder()
    resource = _resource(owner)
    listed = platform_client.get(PLATFORM_RESOURCES)
    assert listed.status_code == 200, listed.text
    assert resource["id"] not in {item["id"] for item in listed.json()}
    ids = _register_all(saas_login)
    demo_forwarder = saas_login(FORWARDER_ORG_ID)
    response = demo_forwarder.post(PLANS, json={"lot_ids": ids})
    assert response.status_code == 201, response.text
    assert _sea(response.json())
    assert resource["id"] not in _sea(response.json())
    assert _listed(owner, resource["id"])["allocated_plan_id"] is None


def test_admin_overview_is_platform_only_scoped_and_excludes_authentication_secrets(
    platform_client,
):
    admin, owner = _platform(), _forwarder()
    shipper, lot_id = _registered()
    _resource(owner)
    created = owner.post(PLANS, json={"lot_ids": [lot_id]})
    assert created.status_code == 201, created.text
    admin_user_id = admin.get("/api/me").json()["user"]["id"]
    owner_id = _org(owner)
    with database.begin() as db:
        db.add(MembershipRow(user_id=admin_user_id, org_id=owner_id, role="admin"))
    path = "/api/saas/admin/overview"
    assert owner.get(path).status_code == 403
    assert shipper.get(path).status_code == 403
    response = admin.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"] == {"companies": 3, "users": 3, "shipments": 1, "plans": 1}
    assert {item["id"] for item in body["companies"]} == {_org(admin), owner_id, _org(shipper)}
    assert next(item for item in body["companies"] if item["id"] == owner_id)["members_count"] == 2
    assert (
        len(next(item for item in body["users"] if item["id"] == admin_user_id)["memberships"]) == 2
    )
    serialized = json.dumps(body)
    assert PASSWORD not in serialized
    assert not any(
        key in serialized.lower() for key in ("password", "token_hash", "session_cookie")
    )
    sandbox = platform_client.get(path)
    assert sandbox.status_code == 200, sandbox.text
    assert {item["id"] for item in sandbox.json()["companies"]}.isdisjoint(
        {item["id"] for item in body["companies"]}
    )
    assert {item["id"] for item in sandbox.json()["users"]}.isdisjoint(
        {item["id"] for item in body["users"]}
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"capacity_pallets": 0},
        {"capacity_pallets": 41},
        {"capacity_pallets": 1.5},
        {"temp_min_c": -101},
        {"temp_min_c": 5, "temp_max_c": 4},
        {"cy_cutoff": "2026-11-19T18:00:00+09:00"},
        {"origin": "UNKNOWN"},
        {"destination": "UNKNOWN"},
        {"origin": "HKHKG"},
        {"transit_days": 0},
        {"transit_days": 61},
        {"name": "   "},
        {"forwarder_org_id": "foreign-injected-owner"},
    ],
)
def test_invalid_resource_conditions_cannot_create_or_mutate_registered_capacity(invalid):
    owner = _forwarder()
    rejected = owner.post(RESOURCES, json={**RESOURCE, **invalid})
    assert rejected.status_code == 422, rejected.text
    assert owner.get(RESOURCES).json() == []
    original = _resource(owner)
    changed = owner.patch(f"{RESOURCES}/{original['id']}", json=invalid)
    assert changed.status_code == 422, changed.text
    assert _listed(owner, original["id"]) == original


def test_live_catalog_and_intake_show_no_fictional_voyages_before_capacity_exists():
    admin, owner = _platform(), _forwarder()
    shipper, _ = _onboarded("resource-intake@example.com", "자원 없는 출하 업체")
    for client in (admin, owner, shipper):
        catalog = client.get("/api/saas/catalog")
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["containers"] == []
        assert catalog.json()["voyages"] == []
    profile = shipper.get("/api/saas/shipper/profile").json()["profile"]
    body = {**_lot(pallets=1), "shipper": profile["shipper_id"]}
    preview = shipper.post("/api/intake/lots", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["hints"]["sea_options"] == []
    assert preview.json()["hints"]["earliest_feasible"] is None
    registered = shipper.post("/api/saas/shipper/lots", json=body)
    assert registered.status_code == 201, registered.text
    physical = _resource(owner)
    catalog = owner.get("/api/saas/catalog")
    assert {item["id"] for item in catalog.json()["containers"]} == {physical["id"]}
    assert all(item["id"].startswith("rv-") for item in catalog.json()["voyages"])
    available = shipper.post("/api/intake/lots", json=body)
    assert available.status_code == 200, available.text
    assert {item["container_id"] for item in available.json()["hints"]["sea_options"]} == {
        physical["id"]
    }


def test_forwarder_confirmation_and_external_execution_remain_gated_by_shipper_acceptance():
    owner = _forwarder()
    shipper, lot_id = _registered()
    resource = _resource(owner)
    created = owner.post(PLANS, json={"lot_ids": [lot_id]})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    confirmation_path = f"{PLANS}/{plan_id}/confirmation"
    state = owner.get(confirmation_path)
    assert state.status_code == 200, state.text
    assert state.json()["status"] == "pending_shipper_confirmation"
    assert owner.post(f"{confirmation_path}/auto-accept").status_code == 404
    event_path = f"/api/plans/{plan_id}/events"
    event_body = {"event": {"type": "air_switch", "lot_id": lot_id}}
    proposed = owner.post(event_path, json=event_body)
    assert proposed.status_code == 200, proposed.text
    choice = next(item["id"] for item in proposed.json()["alternatives"] if item["feasible"])
    blocked = owner.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": choice, "replan_id": proposed.json()["replan_id"]},
    )
    assert blocked.status_code == 409, blocked.text
    accepted = shipper.post(
        f"/api/saas/shipper/plans/{plan_id}/confirmation", json={"accept": True}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["plan_status"] == "committed"
    assert owner.get(confirmation_path).json()["status"] == "committed"
    proposed = owner.post(event_path, json=event_body)
    assert proposed.status_code == 200, proposed.text
    choice = next(item["id"] for item in proposed.json()["alternatives"] if item["feasible"])
    applied = owner.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": choice, "replan_id": proposed.json()["replan_id"]},
    )
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["active_order_id"]
    confirm_path = f"{PLANS}/{plan_id}/orders/{order_id}/confirm"
    assert owner.post(confirm_path, json={"accept": True}).status_code == 409
    order = next(
        item
        for item in owner.get(f"{PLANS}/{plan_id}/orders").json()
        if item["order_id"] == order_id
    )
    pending = [item for item in order["shipper_confirmations"] if item["status"] == "pending"]
    assert pending
    assert _listed(owner, resource["id"])["allocated_plan_id"] == plan_id
    for item in pending:
        accepted = shipper.post(
            f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": True, "shipper_id": item["shipper_id"]},
        )
        assert accepted.status_code == 200, accepted.text
    request = next(item for item in order["requests"] if item["status"] == "pending")
    request_path = f"{PLANS}/{plan_id}/orders/{order_id}/requests/{request['request_id']}"
    assert owner.post(request_path, json={"accepted": True}).status_code == 409
    confirmed = owner.post(confirm_path, json={"accept": True})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "forwarder_confirmed"
    assert any(item["status"] == "pending" for item in confirmed.json()["requests"])


def test_self_owned_forwarder_can_add_registered_shipper_and_requires_new_confirmation():
    owner = _forwarder()
    first, first_id = _registered("first")
    second, second_id = _registered("second")
    _resource(owner, name="첫 번째 실제 자원")
    _resource(owner, name="두 번째 실제 자원")
    created = owner.post(PLANS, json={"lot_ids": [first_id]})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    accepted = first.post(f"/api/saas/shipper/plans/{plan_id}/confirmation", json={"accept": True})
    assert accepted.status_code == 200, accepted.text
    added = owner.post(f"{PLANS}/{plan_id}/add-lot", json={"lot_id": second_id})
    assert added.status_code == 200, added.text
    choice = next(item["id"] for item in added.json()["alternatives"] if item["feasible"])
    applied = owner.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": choice, "replan_id": added.json()["replan_id"]},
    )
    assert applied.status_code == 200, applied.text
    state = owner.get(f"{PLANS}/{plan_id}/confirmation")
    assert state.status_code == 200, state.text
    second_profile = second.get("/api/saas/shipper/profile").json()["profile"]["shipper_id"]
    assert any(
        item["shipper_id"] == second_profile and item["status"] == "pending"
        for item in state.json()["confirmations"]
    )
    order_id = applied.json()["active_order_id"]
    assert (
        owner.post(
            f"{PLANS}/{plan_id}/orders/{order_id}/confirm", json={"accept": True}
        ).status_code
        == 409
    )


def test_authenticated_shipper_change_request_can_supply_its_own_change_consent():
    owner = _forwarder()
    shipper, lot_id = _registered()
    _resource(owner)
    created = owner.post(PLANS, json={"lot_ids": [lot_id]})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    accepted = shipper.post(
        f"/api/saas/shipper/plans/{plan_id}/confirmation", json={"accept": True}
    )
    assert accepted.status_code == 200, accepted.text
    requested = shipper.post(
        f"/api/saas/shipper/plans/{plan_id}/change-requests",
        json={"type": "air_switch", "lot_id": lot_id, "reason": "화주가 직접 항공 전환 요청"},
    )
    assert requested.status_code == 201, requested.text
    request_id = requested.json()["request_id"]
    route = f"/api/saas/forwarder/change-requests/{request_id}"
    computed = owner.post(f"{route}/replan")
    assert computed.status_code == 200, computed.text
    choice = next(
        item["id"] for item in computed.json()["replan"]["alternatives"] if item["feasible"]
    )
    applied = owner.post(f"{route}/apply", json={"alternative_id": choice})
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["plan"]["active_order_id"]
    order = next(
        item
        for item in owner.get(f"{PLANS}/{plan_id}/orders").json()
        if item["order_id"] == order_id
    )
    assert not any(item["status"] == "pending" for item in order["shipper_confirmations"])
    confirmed = owner.post(f"{PLANS}/{plan_id}/orders/{order_id}/confirm", json={"accept": True})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "forwarder_confirmed"
    assert any(item["status"] == "pending" for item in confirmed.json()["requests"])


def test_persisted_forwarder_confirmed_order_cannot_ack_external_request_with_pending_new_shipper():
    owner = _forwarder()
    first, first_id = _registered("first")
    _, second_id = _registered("second")
    _resource(owner, name="첫 번째 실제 자원")
    _resource(owner, name="두 번째 실제 자원")
    created = owner.post(PLANS, json={"lot_ids": [first_id]})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    assert (
        first.post(
            f"/api/saas/shipper/plans/{plan_id}/confirmation", json={"accept": True}
        ).status_code
        == 200
    )
    added = owner.post(f"{PLANS}/{plan_id}/add-lot", json={"lot_id": second_id})
    assert added.status_code == 200, added.text
    choice = next(item["id"] for item in added.json()["alternatives"] if item["feasible"])
    applied = owner.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": choice, "replan_id": added.json()["replan_id"]},
    )
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["active_order_id"]
    with database.begin() as db:
        row = db.get(OrderRow, order_id)
        assert row is not None
        payload = deepcopy(row.payload)
        payload["status"] = "forwarder_confirmed"
        for item in payload["shipper_confirmations"]:
            item["status"] = "accepted"
            item["decided_at"] = FROZEN_NOW.isoformat()
        row.status = "forwarder_confirmed"
        row.payload = payload
        request = next(item for item in payload["requests"] if item["status"] == "pending")
    blocked = owner.post(
        f"{PLANS}/{plan_id}/orders/{order_id}/requests/{request['request_id']}",
        json={"accepted": True},
    )
    assert blocked.status_code == 409, blocked.text
    orders = owner.get(f"{PLANS}/{plan_id}/orders").json()
    saved = next(item for item in orders if item["order_id"] == order_id)
    assert (
        next(item for item in saved["requests"] if item["request_id"] == request["request_id"])[
            "status"
        ]
        == "pending"
    )


def test_forwarder_pool_hides_claimed_and_cancelled_lots_but_own_plan_remains_readable():
    owner, outsider = _forwarder("owner"), _forwarder("outsider")
    _, first_id = _registered("first")
    remaining, second_id = _registered("second")
    _resource(owner)
    created = owner.post(PLANS, json={"lot_ids": [first_id]})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    pool = outsider.get("/api/saas/forwarder/lots")
    assert pool.status_code == 200, pool.text
    assert {item["id"] for item in pool.json()} == {second_id}
    assert all(item["status"] == "registered" for item in pool.json())
    assert outsider.get("/api/saas/forwarder/lots", params={"status": "planned"}).status_code == 422
    assert (
        outsider.get("/api/saas/forwarder/lots", params={"status": "cancelled"}).status_code == 422
    )
    assert owner.get(f"{PLANS}/{plan_id}").status_code == 200
    assert outsider.get(f"{PLANS}/{plan_id}").status_code == 404
    cancelled = remaining.patch(f"/api/saas/shipper/lots/{second_id}", json={"status": "cancelled"})
    assert cancelled.status_code == 200, cancelled.text
    assert outsider.get("/api/saas/forwarder/lots").json() == []
    assert owner.get(f"{PLANS}/{plan_id}").status_code == 200


def test_forwarder_catalog_and_new_plan_include_only_registered_cargo_participants():
    admin, owner = _platform(), _forwarder()
    first, first_id = _registered("selected")
    second, _ = _registered("eligible-unselected")
    inactive, inactive_site = _onboarded("inactive-private@example.com", "비공개 미출하 업체")
    custom = inactive.post(
        "/api/saas/shipper/products",
        json={
            "name": "아직 출하하지 않은 비공개 품목",
            "temp_min_c": 0,
            "temp_max_c": 3,
            "o2_pct": None,
            "co2_pct": None,
            "shelf_life_days": 30,
            "so2_treated": False,
            "pallet_kg": 400,
        },
    )
    assert custom.status_code == 201, custom.text
    custom_id = custom.json()["id"]
    active_profiles = [
        client.get("/api/saas/shipper/profile").json()["profile"] for client in (first, second)
    ]
    inactive_profile = inactive.get("/api/saas/shipper/profile").json()["profile"]
    _resource(owner)
    catalog = owner.get("/api/saas/catalog")
    assert catalog.status_code == 200, catalog.text
    created = owner.post(PLANS, json={"lot_ids": [first_id]})
    assert created.status_code == 201, created.text
    for scenario in (catalog.json(), created.json()["scenario"]):
        assert {item["id"] for item in scenario["shippers"]} == {
            profile["shipper_id"] for profile in active_profiles
        }
        assert {item["id"] for item in scenario["packhouses"]} == {
            profile["packhouse_id"] for profile in active_profiles
        }
        assert {"strawberry", "grape_so2"}.issubset({item["id"] for item in scenario["products"]})
        serialized = json.dumps(scenario, ensure_ascii=False)
        assert inactive_profile["shipper_id"] not in serialized
        assert inactive_site["packhouse_id"] not in serialized
        assert custom_id not in serialized
        assert "비공개 미출하 업체" not in serialized
        assert "아직 출하하지 않은 비공개 품목" not in serialized
    overview = admin.get("/api/saas/admin/overview")
    assert overview.status_code == 200, overview.text
    assert _org(inactive) in {item["id"] for item in overview.json()["companies"]}
