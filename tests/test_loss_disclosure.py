import json
from typing import get_args

from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, SHIPPER_ORG_ID
from test_saas_api import _platform_plan

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import app
from freshswitch_backend.planning.store import SessionStatus
from freshswitch_backend.quality.disclosure import DEFAULT_BASIS, LEARNED_BASIS, expected_loss
from freshswitch_backend.quality.params import QualityParams

LOSS_KEYS = {"rate", "mode", "basis", "n_observations", "source"}
SHIPPER_FORM = {
    "product": "strawberry",
    "pallets": 2,
    "ready_date": "2026-11-17",
    "destination": "HKHKG",
}
DEMO_FORM = {**SHIPPER_FORM, "product": "딸기", "shipper": "산청", "destination": "홍콩"}


def _cell(client: TestClient, product_id: str, destination: str, mode: str = "sea") -> dict:
    response = client.get("/api/quality/loss-rates")
    assert response.status_code == 200, response.text
    return next(
        c
        for c in response.json()["cells"]
        if (c["product_id"], c["destination"], c["mode"]) == (product_id, destination, mode)
    )


def _assert_matches(loss: dict, cell: dict) -> None:
    assert set(loss) == LOSS_KEYS
    assert loss["mode"] == "sea"
    assert loss["rate"] == cell["posterior_rate"]
    assert loss["n_observations"] == cell["observations"]
    if cell["observations"]:
        assert loss["basis"] == LEARNED_BASIS
        assert loss["source"] == cell["basis"]
    else:
        assert loss["basis"] == DEFAULT_BASIS
        assert loss["source"] == cell["prior_source"]


def _other_shippers(own: str) -> list[str]:
    scenario = get_scenario("default")
    assert scenario is not None
    return [text for s in scenario.shippers if s.id != own for text in (s.id, s.name, s.id[3:])]


def test_openapi_exposes_status_enum_warnings_and_expected_loss() -> None:
    schemas = app.openapi()["components"]["schemas"]
    for name in ("PlanSession", "PlanSummary", "PlanConfirmationState"):
        assert set(schemas[name]["properties"]["status"]["enum"]) == set(get_args(SessionStatus))
    assert "pending_shipper_confirmation" in schemas["PlanSession"]["properties"]["status"]["enum"]
    warnings = schemas["RegisteredLot"]["properties"]["warnings"]
    assert warnings["type"] == "array" and warnings["items"] == {"type": "string"}
    assert "warnings" not in schemas["RegisteredLot"]["required"]
    assert set(schemas["ExpectedLoss"]["properties"]) == LOSS_KEYS
    assert set(schemas["ExpectedLoss"]["required"]) == {"rate", "mode", "basis", "n_observations"}
    assert "expected_loss" in schemas["IntakePreview"]["properties"]
    assert "expected_loss" in schemas["ShipperLot"]["required"]
    assert "warnings" in schemas["ShipperLot"]["properties"]


def test_pending_shipper_confirmation_plan_status_is_in_response_enum(
    platform_client: TestClient, saas_login
) -> None:
    allowed = set(
        app.openapi()["components"]["schemas"]["PlanSession"]["properties"]["status"]["enum"]
    )
    plan = _platform_plan(platform_client, saas_login)
    plan_id = plan["plan_id"]
    assert plan["status"] == "pending_shipper_confirmation"
    assigned = platform_client.put(
        f"/api/saas/platform/plans/{plan_id}/forwarder",
        json={"forwarder_org_id": FORWARDER_ORG_ID},
    )
    assert assigned.status_code == 200, assigned.text
    statuses = [
        plan["status"],
        saas_login(FORWARDER_ORG_ID).get(f"/api/saas/forwarder/plans/{plan_id}").json()["status"],
        saas_login(SHIPPER_ORG_ID).get(f"/api/saas/shipper/plans/{plan_id}").json()["status"],
        platform_client.get(f"/api/saas/platform/plans/{plan_id}/confirmation").json()["status"],
    ]
    assert set(statuses) == {"pending_shipper_confirmation"}
    assert set(statuses) <= allowed


def test_shipper_preview_and_registration_carry_learned_expected_loss(shipper_client) -> None:
    cell = _cell(shipper_client, "strawberry", "HKHKG")
    preview = shipper_client.post("/api/intake/lots", json={**SHIPPER_FORM, "shipper": "sh-jinju"})
    assert preview.status_code == 200, preview.text
    _assert_matches(preview.json()["expected_loss"], cell)

    created = shipper_client.post("/api/saas/shipper/lots", json=SHIPPER_FORM)
    assert created.status_code == 201, created.text
    _assert_matches(created.json()["expected_loss"], cell)


def test_shipper_lot_list_carries_expected_loss_without_other_shipper_data(
    shipper_client, other_shipper_client
) -> None:
    created = shipper_client.post("/api/saas/shipper/lots", json=SHIPPER_FORM)
    assert created.status_code == 201, created.text
    grape = {**SHIPPER_FORM, "product": "grape_so2"}
    assert shipper_client.post("/api/saas/shipper/lots", json=grape).status_code == 201
    other = other_shipper_client.post("/api/saas/shipper/lots", json=SHIPPER_FORM)
    assert other.status_code == 201, other.text

    listed = shipper_client.get("/api/saas/shipper/lots")
    assert listed.status_code == 200, listed.text
    lots = listed.json()
    own = created.json()["registered"]["shipper_id"]
    assert {lot["shipper_id"] for lot in lots} == {own}
    assert {lot["product_id"] for lot in lots} == {"strawberry", "grape_so2"}
    for lot in lots:
        assert "warnings" not in lot or lot["warnings"]
        _assert_matches(lot["expected_loss"], _cell(shipper_client, lot["product_id"], "HKHKG"))
    first = next(lot for lot in lots if lot["id"] == created.json()["registered"]["id"])
    assert first["expected_loss"] == created.json()["expected_loss"]
    disclosed = json.dumps([lot["expected_loss"] for lot in lots], ensure_ascii=False)
    for text in _other_shippers(own):
        assert text not in disclosed


def test_demo_intake_adds_only_expected_loss(client: TestClient) -> None:
    body = client.post("/api/intake/lots", json=DEMO_FORM).json()
    _assert_matches(body["expected_loss"], _cell(client, "strawberry", "HKHKG"))
    assert body["lot"]["shipper_id"] == "sh-sancheong"
    assert "registered" in body and body["registered"] is None


def test_expected_loss_without_observations_falls_back_to_scenario_default() -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    prior = scenario.cost_params.for_product("strawberry").sea_base_loss_rate
    loss = expected_loss(scenario, "strawberry", "HKHKG", [], QualityParams())
    assert loss.rate == prior.value
    assert loss.basis == DEFAULT_BASIS
    assert loss.n_observations == 0
    assert loss.source == prior.source
