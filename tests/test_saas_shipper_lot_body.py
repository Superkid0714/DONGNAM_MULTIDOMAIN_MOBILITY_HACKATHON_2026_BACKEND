from fastapi.testclient import TestClient
from saas_fixtures import OTHER_SHIPPER_ORG_ID, saas_app
from test_saas_api import _lot_body, _scenario


def _body_without_shipper(lot, **extra) -> dict:
    body = _lot_body(lot, **extra)
    body.pop("shipper")
    return body


def test_shipper_lot_create_without_shipper_uses_session(shipper_client: TestClient) -> None:
    lot = next(x for x in _scenario().lots if x.shipper_id == "sh-jinju")
    response = shipper_client.post(
        "/api/saas/shipper/lots", json=_body_without_shipper(lot, lot_id="no-shipper-1")
    )
    assert response.status_code == 201, response.text
    registered = response.json()["registered"]
    assert registered["shipper_id"] == "sh-jinju"
    listed = shipper_client.get("/api/saas/shipper/lots").json()
    assert [x["id"] for x in listed] == ["no-shipper-1"]


def test_shipper_lot_create_ignores_body_shipper_org(
    shipper_client: TestClient, other_shipper_client: TestClient
) -> None:
    lot = next(x for x in _scenario().lots if x.shipper_id == "sh-jinju")
    body = _body_without_shipper(lot, lot_id="foreign-org-1")
    body.update(shipper="sh-sancheong", shipper_org_id=OTHER_SHIPPER_ORG_ID)
    response = shipper_client.post("/api/saas/shipper/lots", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["registered"]["shipper_id"] == "sh-jinju"
    assert other_shipper_client.get("/api/saas/shipper/lots").json() == []


def test_saas_openapi_shipper_lot_create_optional_shipper() -> None:
    schemas = saas_app.openapi()["components"]["schemas"]
    lot_create = schemas["ShipperLotCreate"]
    assert "shipper" not in lot_create.get("required", [])
    assert "shipper_org_id" in lot_create["properties"]
