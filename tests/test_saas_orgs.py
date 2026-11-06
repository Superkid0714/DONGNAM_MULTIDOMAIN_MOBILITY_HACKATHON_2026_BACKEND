import pytest
from fastapi.testclient import TestClient
from saas_fixtures import saas_app, sign_in_as
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import OrganizationRow, PlanForwarderRow
from freshswitch_backend.planning.store import store

CSRF = {"X-Requested-With": "freshswitch"}
PLATFORM = "freshswitch"
OTHER_PLATFORM = "platform-other"
DONGNAM = "forwarder-dongnam"
SINHANG = "forwarder-sinhang"
RETIRED = "플랫폼은 서비스 관리·조회만 할 수 있습니다. 운송 계획은 포워더가 관리합니다"


@pytest.fixture
def saas() -> TestClient:
    return TestClient(saas_app, base_url="https://testserver")


def _login(client: TestClient, org_type: str) -> None:
    sign_in_as(client, org_type)


def _plan(org_id: str) -> str:
    scenario = get_scenario("default")
    assert scenario is not None
    return store.create(scenario, org_id=org_id).plan_id


def _assigned(plan_id: str) -> list[str]:
    with database.begin() as db:
        return list(
            db.scalars(
                select(PlanForwarderRow.forwarder_org_id).where(PlanForwarderRow.plan_id == plan_id)
            )
        )


def _forwarder_path(plan_id: str) -> str:
    return f"/api/saas/platform/plans/{plan_id}/forwarder"


@pytest.fixture
def platform(saas: TestClient, demo_tenants) -> TestClient:
    _login(saas, "platform")
    return saas


def test_list_orgs_by_type(platform: TestClient) -> None:
    forwarders = platform.get("/api/saas/orgs", params={"type": "forwarder"})
    assert forwarders.status_code == 200
    assert {o["org_id"] for o in forwarders.json()} == {DONGNAM, SINHANG}
    assert all(o["org_type"] == "forwarder" and o["org_name"] for o in forwarders.json())

    shippers = platform.get("/api/saas/orgs", params={"type": "shipper"}).json()
    assert shippers
    assert all(o["org_type"] == "shipper" for o in shippers)
    assert "shipper-jinju" in {o["org_id"] for o in shippers}

    everything = platform.get("/api/saas/orgs").json()
    ids = {o["org_id"] for o in everything}
    assert ids == {o["org_id"] for o in shippers} | {DONGNAM, SINHANG}
    assert PLATFORM not in ids and "demo" not in ids


def test_list_orgs_rejects_unknown_type(platform: TestClient) -> None:
    assert platform.get("/api/saas/orgs", params={"type": "platform"}).status_code == 422


def test_anonymous_requests_are_rejected(saas: TestClient, demo_tenants) -> None:
    plan_id = _plan(PLATFORM)
    assert saas.get("/api/saas/orgs").status_code == 401
    put = saas.put(_forwarder_path(plan_id), json={"forwarder_org_id": DONGNAM})
    assert put.status_code == 401
    assert saas.delete(_forwarder_path(plan_id)).status_code == 401
    assert _assigned(plan_id) == []


@pytest.mark.parametrize("org_type", ["shipper", "forwarder"])
def test_non_platform_roles_are_forbidden(saas: TestClient, demo_tenants, org_type: str) -> None:
    plan_id = _plan(PLATFORM)
    _login(saas, org_type)
    assert saas.get("/api/saas/orgs").status_code == 403
    put = saas.put(_forwarder_path(plan_id), json={"forwarder_org_id": DONGNAM}, headers=CSRF)
    assert put.status_code == 403
    assert saas.delete(_forwarder_path(plan_id), headers=CSRF).status_code == 403
    assert _assigned(plan_id) == []


def test_assign_requires_csrf_header(platform: TestClient) -> None:
    plan_id = _plan(PLATFORM)
    put = platform.put(_forwarder_path(plan_id), json={"forwarder_org_id": DONGNAM})
    assert put.status_code == 403
    assert _assigned(plan_id) == []


def test_platform_cannot_assign_or_unassign_forwarder(platform: TestClient) -> None:
    plan_id = _plan(DONGNAM)
    assert _assigned(plan_id) == [DONGNAM]
    for target in (DONGNAM, SINHANG):
        put = platform.put(
            _forwarder_path(plan_id), json={"forwarder_org_id": target}, headers=CSRF
        )
        assert put.status_code == 403
        assert put.json()["detail"] == RETIRED
    removed = platform.delete(_forwarder_path(plan_id), headers=CSRF)
    assert removed.status_code == 403
    assert removed.json()["detail"] == RETIRED
    assert _assigned(plan_id) == [DONGNAM]
    listed = platform.get("/api/saas/platform/plans").json()
    item = next(p for p in listed if p["plan_id"] == plan_id)
    assert item["forwarder"] == {"org_id": DONGNAM, "org_name": "동남 콜드체인 포워딩"}


@pytest.mark.parametrize("target", ["shipper-jinju", PLATFORM, "demo", "missing-org"])
def test_assign_non_forwarder_is_forbidden(platform: TestClient, target: str) -> None:
    plan_id = _plan(DONGNAM)
    put = platform.put(_forwarder_path(plan_id), json={"forwarder_org_id": target}, headers=CSRF)
    assert put.status_code == 403
    assert _assigned(plan_id) == [DONGNAM]


def test_other_platform_plan_is_not_found(platform: TestClient) -> None:
    with database.begin() as db:
        db.execute(
            insert(OrganizationRow)
            .values(id=OTHER_PLATFORM, type="platform", name="다른 플랫폼")
            .on_conflict_do_nothing(index_elements=[OrganizationRow.id])
        )
    foreign = _plan(OTHER_PLATFORM)
    demo_plan = _plan("demo")
    for plan_id in (foreign, demo_plan, "no-such-plan"):
        confirmation = platform.get(f"/api/saas/platform/plans/{plan_id}/confirmation")
        assert confirmation.status_code == 404
        assert platform.get(f"/api/plans/{plan_id}").status_code == 404
        put = platform.put(
            _forwarder_path(plan_id), json={"forwarder_org_id": DONGNAM}, headers=CSRF
        )
        assert put.status_code == 403
        assert platform.delete(_forwarder_path(plan_id), headers=CSRF).status_code == 403
        assert _assigned(plan_id) == []
    listed = {p["plan_id"] for p in platform.get("/api/saas/platform/plans").json()}
    assert not listed & {foreign, demo_plan}
