import pytest
from fastapi.testclient import TestClient
from test_intake_preview_savings import INTAKE, _body, _lot
from test_saas_api import _lot_body

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.persistence.seed import shipper_org_id


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


def _platform_plan(scenario: Scenario, platform_client: TestClient, saas_login) -> str:
    ids = []
    for lot in scenario.lots:
        if lot.shipper_id not in ("sh-jinju", "sh-sancheong"):
            continue
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    created = platform_client.post("/api/saas/platform/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    return created.json()["plan_id"]


def test_shipper_previews_against_own_plan(
    scenario: Scenario, platform_client: TestClient, saas_login
) -> None:
    plan_id = _platform_plan(scenario, platform_client, saas_login)
    jinju = saas_login(shipper_org_id("sh-jinju"))
    body = _body(_lot(scenario, "lot-jinju-sb-1"), pallets=2, plan_id=plan_id)
    response = jinju.post(INTAKE, json=body)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["hints"]["based_on"] == "plan"
    assert preview["resolved"]["shipper_id"] == "sh-jinju"
    estimate = preview["savings_estimate"]
    assert estimate["mode"] == "sea"
    container = preview["hints"]["earliest_feasible"]
    loaded = 20 - container["free_pallets"]
    assert loaded > 0
    assert estimate["assumed_container_pallets"] == loaded + 2
    assert "산청" not in response.text

    saved = jinju.post(INTAKE, json={**body, "lot_id": "lot-jinju-extra", "save": True})
    assert saved.status_code == 200, saved.text
    assert saved.json()["registered"]["shipper_id"] == "sh-jinju"
    assert saved.json()["savings_estimate"] is not None


def test_shipper_plan_preview_resolves_only_own_shippers(
    scenario: Scenario, platform_client: TestClient, saas_login
) -> None:
    plan_id = _platform_plan(scenario, platform_client, saas_login)
    jinju = saas_login(shipper_org_id("sh-jinju"))
    body = _body(_lot(scenario, "lot-sancheong-sb-1"), plan_id=plan_id)
    response = jinju.post(INTAKE, json=body)
    assert response.status_code == 422
    assert "산청 딸기" not in response.text


def test_shipper_outside_plan_gets_404(
    scenario: Scenario, platform_client: TestClient, saas_login
) -> None:
    plan_id = _platform_plan(scenario, platform_client, saas_login)
    miryang = saas_login(shipper_org_id("sh-miryang"))
    body = _body(_lot(scenario, "lot-miryang-sb-1"), plan_id=plan_id)
    assert miryang.post(INTAKE, json=body).status_code == 404
    assert miryang.post(INTAKE, json={**body, "save": True}).status_code == 404
    assert miryang.post(INTAKE, json={**body, "plan_id": "nope"}).status_code == 404


def test_platform_plan_preview_is_unchanged(
    scenario: Scenario, platform_client: TestClient, saas_login
) -> None:
    plan_id = _platform_plan(scenario, platform_client, saas_login)
    body = _body(_lot(scenario, "lot-miryang-sb-1"), plan_id=plan_id)
    response = platform_client.post(INTAKE, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["hints"]["based_on"] == "plan"


def test_forwarder_cannot_preview(forwarder_client: TestClient, scenario: Scenario) -> None:
    body = _body(_lot(scenario, "lot-jinju-sb-1"))
    assert forwarder_client.post(INTAKE, json=body).status_code == 403
