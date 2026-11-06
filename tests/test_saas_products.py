import json

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    create_real_platform,
    password_client,
    saas_app,
)
from test_onboarding import _lot, _onboarded, _real_forwarder, _signup

from freshswitch_backend.data.loader import get_scenario

PRODUCTS = "/api/saas/shipper/products"
PRODUCT = {
    "name": "블루베리",
    "temp_min_c": 0,
    "temp_max_c": 3,
    "o2_pct": None,
    "co2_pct": None,
    "shelf_life_days": 30,
    "so2_treated": False,
    "pallet_kg": 400,
}


def _create(client: TestClient, **changes) -> dict:
    response = client.post(PRODUCTS, json={**PRODUCT, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def _catalog(client: TestClient) -> dict[str, dict]:
    response = client.get("/api/saas/catalog")
    assert response.status_code == 200, response.text
    return {product["id"]: product for product in response.json()["products"]}


def test_org_product_creation_is_private_and_cannot_be_changed_by_other_org() -> None:
    owner, _ = _onboarded("products-owner@example.com", "품목 농장")
    other, _ = _onboarded("products-other@example.com", "다른 농장")
    created = _create(owner)
    product_id = created["id"]

    assert created["writable"] is True
    assert created["name"] == PRODUCT["name"]
    assert created["temp_max_c"] == 3
    assert product_id in _catalog(owner)
    assert product_id not in _catalog(other)
    assert product_id not in {product["id"] for product in other.get(PRODUCTS).json()}
    foreign = other.patch(f"{PRODUCTS}/{product_id}", json={"name": "가로챈 품목"})
    assert foreign.status_code == 404, foreign.text
    registration = other.post("/api/saas/shipper/lots", json=_lot(product_id))
    assert registration.status_code == 422, registration.text
    assert _catalog(owner)[product_id]["name"] == PRODUCT["name"]


def test_edit_replaces_product_and_keeps_existing_lot_conditions() -> None:
    owner, _ = _onboarded("products-version@example.com", "버전 농장")
    original = _create(owner)
    old_id = original["id"]
    registered = owner.post("/api/saas/shipper/lots", json=_lot(old_id, pallets=1))
    assert registered.status_code == 201, registered.text
    lot_id = registered.json()["registered"]["id"]
    patched = owner.patch(
        f"{PRODUCTS}/{old_id}",
        json={"name": "블루베리 새 규격", "temp_min_c": 1, "temp_max_c": 4},
    )
    assert patched.status_code == 200, patched.text
    current = patched.json()
    assert current["id"] != old_id
    assert current["replaces_id"] == old_id
    assert current["temp_min_c"] == 1
    listed_ids = {product["id"] for product in owner.get(PRODUCTS).json()}
    assert current["id"] in listed_ids
    assert old_id not in listed_ids
    catalog = _catalog(owner)
    assert catalog[old_id]["name"] == original["name"]
    assert catalog[old_id]["temp_min_c"] == 0
    assert catalog[current["id"]]["temp_max_c"] == 4
    lots = owner.get("/api/saas/shipper/lots")
    assert lots.status_code == 200, lots.text
    assert next(lot for lot in lots.json() if lot["id"] == lot_id)["product_id"] == old_id
    new_lot = owner.post("/api/saas/shipper/lots", json=_lot(current["id"], pallets=1))
    assert new_lot.status_code == 201, new_lot.text
    assert new_lot.json()["registered"]["product_id"] == current["id"]


def test_custom_product_registers_and_optimizes_without_changing_existing_plan() -> None:
    create_real_platform()
    platform = password_client(saas_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    owner, _ = _onboarded("products-plan@example.com", "운송 농장")
    product = _create(owner)
    registered = owner.post("/api/saas/shipper/lots", json=_lot(product["id"], pallets=1))
    assert registered.status_code == 201, registered.text
    lot_id = registered.json()["registered"]["id"]
    forwarder = _real_forwarder("products-forwarder@example.com", "품목 포워더")
    planned = forwarder.post("/api/saas/forwarder/plans", json={"lot_ids": [lot_id]})
    assert planned.status_code == 201, planned.text
    session = planned.json()
    assert any(
        assignment["lot_id"] == lot_id and assignment["product_id"] == product["id"]
        for assignment in session["plan"]["assignments"]
    )
    changed = owner.patch(f"{PRODUCTS}/{product['id']}", json={"temp_max_c": 5})
    assert changed.status_code == 200, changed.text
    saved = platform.get(f"/api/plans/{session['plan_id']}")
    assert saved.status_code == 200, saved.text
    saved_product = next(
        item for item in saved.json()["scenario"]["products"] if item["id"] == product["id"]
    )
    assert saved_product["temp_max_c"] == 3
    assert product["id"] in _catalog(platform)


def test_default_edit_creates_private_copy_and_preserves_global_catalog() -> None:
    owner, _ = _onboarded("products-default@example.com", "기본 품목 농장")
    other, _ = _onboarded("products-default-other@example.com", "기본 품목 다른 농장")
    original = _catalog(owner)["strawberry"]
    default = next(
        product for product in owner.get(PRODUCTS).json() if product["id"] == "strawberry"
    )
    assert default["writable"] is False
    response = owner.patch(f"{PRODUCTS}/strawberry", json={"name": "내 딸기", "temp_max_c": 4})
    assert response.status_code == 200, response.text
    custom = response.json()
    assert custom["id"] != "strawberry"
    assert custom["writable"] is True
    assert _catalog(owner)["strawberry"] == original
    assert _catalog(other)["strawberry"] == original
    assert custom["id"] not in _catalog(other)
    profile = owner.get("/api/saas/shipper/profile").json()["profile"]
    assert "strawberry" not in profile["product_ids"]
    assert custom["id"] in profile["product_ids"]
    scenario = get_scenario("default")
    assert scenario is not None
    assert (
        next(item for item in scenario.products if item.id == "strawberry").name == original["name"]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "   "},
        {"name": "품목\u0000"},
        {"name": "품" * 201},
        {"temp_min_c": 5, "temp_max_c": 3},
        {"shelf_life_days": 0},
        {"pallet_kg": 0},
        {"o2_pct": -1},
        {"o2_pct": 101},
        {"co2_pct": 101},
    ],
)
def test_product_create_validation_returns_422(changes: dict) -> None:
    owner = _signup("products-invalid@example.com", "검증 농장")
    response = owner.post(PRODUCTS, json={**PRODUCT, **changes})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_product_nonfinite_numbers_are_rejected(value: str) -> None:
    owner = _signup("products-finite@example.com", "유한 수 농장")
    payload = json.dumps(PRODUCT).replace('"temp_min_c": 0', f'"temp_min_c": {value}')
    response = owner.post(PRODUCTS, content=payload, headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text


def test_product_patch_validates_combined_temperature_and_nullable_gas() -> None:
    owner, _ = _onboarded("products-patch@example.com", "수정 농장")
    product = _create(owner, o2_pct=3, co2_pct=10)
    bad = owner.patch(f"{PRODUCTS}/{product['id']}", json={"temp_min_c": 4})
    assert bad.status_code == 422, bad.text
    assert _catalog(owner)[product["id"]]["temp_min_c"] == 0
    cleared = owner.patch(f"{PRODUCTS}/{product['id']}", json={"o2_pct": None, "co2_pct": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["o2_pct"] is None
    assert cleared.json()["co2_pct"] is None


@pytest.mark.parametrize("field", ["name", "temp_min_c", "shelf_life_days", "pallet_kg"])
def test_product_patch_rejects_null_for_required_fields(field: str) -> None:
    owner, _ = _onboarded("products-null@example.com", "필수 값 농장")
    product = _create(owner)
    response = owner.patch(f"{PRODUCTS}/{product['id']}", json={field: None})
    assert response.status_code == 422, response.text
    assert _catalog(owner)[product["id"]][field] == PRODUCT[field]


def test_duplicate_names_and_stale_updates_leave_current_product_unchanged() -> None:
    owner, _ = _onboarded("products-conflict@example.com", "충돌 농장")
    original = _create(owner)
    duplicate = owner.post(PRODUCTS, json={**PRODUCT, "name": " 블루베리 "})
    assert duplicate.status_code == 409, duplicate.text
    changed = owner.patch(f"{PRODUCTS}/{original['id']}", json={"name": "새 블루베리"})
    assert changed.status_code == 200, changed.text
    stale = owner.patch(f"{PRODUCTS}/{original['id']}", json={"name": "잃어버린 수정"})
    assert stale.status_code == 409, stale.text
    assert _catalog(owner)[changed.json()["id"]]["name"] == "새 블루베리"


def test_product_gas_total_and_merged_patch_are_validated() -> None:
    owner, _ = _onboarded("products-gas@example.com", "가스 농장")
    invalid = owner.post(PRODUCTS, json={**PRODUCT, "o2_pct": 60, "co2_pct": 50})
    assert invalid.status_code == 422, invalid.text
    original = _create(owner, o2_pct=60, co2_pct=40)
    changed = owner.patch(f"{PRODUCTS}/{original['id']}", json={"co2_pct": 41})
    assert changed.status_code == 422, changed.text
    assert _catalog(owner)[original["id"]]["co2_pct"] == 40


def test_product_write_requires_shipper_session_and_csrf() -> None:
    anonymous = TestClient(saas_app)
    assert anonymous.get(PRODUCTS).status_code == 401
    assert anonymous.post(PRODUCTS, json=PRODUCT).status_code == 401
    forwarder = _signup("products-forwarder@example.com", "포워더", "forwarder")
    assert forwarder.get(PRODUCTS).status_code == 403
    assert forwarder.post(PRODUCTS, json=PRODUCT).status_code == 403
    owner = _signup("products-csrf@example.com", "출처 농장")
    owner.headers.pop("X-Requested-With")
    denied = owner.post(PRODUCTS, json=PRODUCT)
    assert denied.status_code == 403, denied.text
    assert owner.get(PRODUCTS).status_code == 200
