from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import SHIPPER_ORG_ID, saas_app
from sqlalchemy import func, select
from test_onboarding import _lot, _onboarded
from test_saas_products import PRODUCT, PRODUCTS, _catalog, _create
from test_saas_resource_invariants import PLANS, _forwarder, _platform, _resource

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PlanRow, ProductArchiveRow, ProductRow


def _ids(client, archived=False):
    response = client.get(PRODUCTS, params={"include_archived": archived})
    assert response.status_code == 200, response.text
    return {product["id"]: product for product in response.json()}


def test_archive_restore_used_custom_product_preserves_records_and_is_idempotent():
    owner, _ = _onboarded("archive-used@example.com", "보관 품목 업체")
    product = _create(owner)
    product_id = product["id"]
    preferred = owner.patch(
        "/api/saas/shipper/profile", json={"product_ids": ["strawberry", product_id]}
    )
    assert preferred.status_code == 200, preferred.text
    registered = owner.post("/api/saas/shipper/lots", json=_lot(product_id, pallets=1))
    assert registered.status_code == 201, registered.text
    lot_id = registered.json()["registered"]["id"]
    with database.begin() as db:
        original = deepcopy(db.get(ProductRow, product_id).data)
    for _ in range(2):
        archived = owner.delete(f"{PRODUCTS}/{product_id}")
        assert archived.status_code == 200, archived.text
        assert archived.json()["archived"] is True
        assert archived.json()["id"] == product_id
    assert product_id not in _ids(owner)
    assert _ids(owner, archived=True)[product_id]["archived"] is True
    profile = owner.get("/api/saas/shipper/profile").json()
    assert profile["profile"]["product_ids"] == ["strawberry"]
    assert product_id not in {item["product_id"] for item in profile["product_options"]}
    assert owner.post("/api/saas/shipper/lots", json=_lot(product_id)).status_code == 422
    preview = owner.post(
        "/api/intake/lots", json={**_lot(product_id), "shipper": profile["profile"]["shipper_id"]}
    )
    assert preview.status_code == 422, preview.text
    generic = owner.post(
        "/api/lots", json={**_lot(product_id), "shipper": profile["profile"]["shipper_id"]}
    )
    assert generic.status_code == 422, generic.text
    unchanged = owner.patch(
        f"/api/saas/shipper/lots/{lot_id}",
        json={"product": product_id, "destination": "HKHKG", "pallets": 2},
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["product_id"] == product_id
    assert unchanged.json()["pallets"] == 2
    assert _catalog(owner)[product_id]["name"] == PRODUCT["name"]
    for _ in range(2):
        restored = owner.post(f"{PRODUCTS}/{product_id}/restore")
        assert restored.status_code == 200, restored.text
        assert restored.json()["archived"] is False
        assert restored.json()["id"] == product_id
    assert product_id in _ids(owner)
    assert owner.get("/api/saas/shipper/profile").json()["profile"]["product_ids"] == ["strawberry"]
    assert owner.post("/api/saas/shipper/lots", json=_lot(product_id, pallets=1)).status_code == 201
    with database.begin() as db:
        assert db.get(ProductRow, product_id).data == original
        assert db.scalar(select(func.count()).select_from(ProductArchiveRow)) == 0
        assert db.scalar(select(func.count()).select_from(ProductRow)) == 1


def test_shared_product_archive_is_company_local_and_restore_checks_active_name_conflict():
    owner, _ = _onboarded("archive-default@example.com", "기본 품목 업체")
    other, _ = _onboarded("archive-default-other@example.com", "다른 기본 품목 업체")
    scenario = get_scenario("default")
    before = scenario.model_dump(mode="json")
    assert owner.post("/api/saas/shipper/lots", json=_lot(pallets=1)).status_code == 201
    archived = owner.delete(f"{PRODUCTS}/strawberry")
    assert archived.status_code == 200, archived.text
    assert archived.json()["writable"] is False
    assert archived.json()["archived"] is True
    assert "strawberry" not in _ids(owner)
    assert _ids(other)["strawberry"]["archived"] is False
    assert owner.post("/api/saas/shipper/lots", json=_lot(pallets=1)).status_code == 422
    assert other.post("/api/saas/shipper/lots", json=_lot(pallets=1)).status_code == 201
    conflict = _create(owner, name=archived.json()["name"])
    denied = owner.post(f"{PRODUCTS}/strawberry/restore")
    assert denied.status_code == 409, denied.text
    assert _ids(owner, archived=True)["strawberry"]["archived"] is True
    assert owner.delete(f"{PRODUCTS}/{conflict['id']}").status_code == 200
    assert owner.post(f"{PRODUCTS}/strawberry/restore").status_code == 200
    assert "strawberry" in _ids(owner)
    assert get_scenario("default").model_dump(mode="json") == before


def test_archiving_current_replacement_never_resurrects_global_or_retired_versions():
    owner, _ = _onboarded("archive-lineage@example.com", "버전 품목 업체")
    first = owner.patch(f"{PRODUCTS}/strawberry", json={"name": "내 딸기"})
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]
    second = owner.patch(f"{PRODUCTS}/{first_id}", json={"temp_max_c": 3})
    assert second.status_code == 200, second.text
    current_id = second.json()["id"]
    assert owner.delete(f"{PRODUCTS}/{current_id}").status_code == 200
    assert not {"strawberry", first_id, current_id} & set(_ids(owner))
    all_products = _ids(owner, archived=True)
    assert current_id in all_products
    assert all_products[current_id]["archived"] is True
    assert "strawberry" not in all_products
    assert first_id not in all_products
    assert (
        owner.patch(f"{PRODUCTS}/{current_id}", json={"name": "숨긴 상태 수정"}).status_code == 409
    )
    for stale in ("strawberry", first_id):
        assert owner.delete(f"{PRODUCTS}/{stale}").status_code == 409
        assert owner.post(f"{PRODUCTS}/{stale}/restore").status_code == 409
    assert owner.post(f"{PRODUCTS}/{current_id}/restore").status_code == 200
    assert set(_ids(owner)) & {"strawberry", first_id, current_id} == {current_id}
    assert owner.post("/api/saas/shipper/lots", json=_lot("strawberry")).status_code == 422
    assert owner.post("/api/saas/shipper/lots", json=_lot(first_id)).status_code == 422
    assert owner.post("/api/saas/shipper/lots", json=_lot(current_id, pallets=1)).status_code == 201


def test_archived_old_product_cannot_be_new_choice_but_existing_product_stays_editable():
    owner, _ = _onboarded("archive-edit@example.com", "출하 수정 업체")
    archived = _create(owner)
    existing = owner.post("/api/saas/shipper/lots", json=_lot(archived["id"], pallets=1))
    baseline = owner.post("/api/saas/shipper/lots", json=_lot(pallets=1))
    assert existing.status_code == baseline.status_code == 201
    assert owner.delete(f"{PRODUCTS}/{archived['id']}").status_code == 200
    target = baseline.json()["registered"]["id"]
    denied = owner.patch(f"/api/saas/shipper/lots/{target}", json={"product": archived["id"]})
    assert denied.status_code == 422, denied.text
    original = existing.json()["registered"]["id"]
    allowed = owner.patch(f"/api/saas/shipper/lots/{original}", json={"pallets": 2})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["product_id"] == archived["id"]
    forwarder = _forwarder()
    _resource(forwarder)
    planned = forwarder.post(PLANS, json={"lot_ids": [original]})
    assert planned.status_code == 201, planned.text
    assert archived["id"] in {item["id"] for item in planned.json()["scenario"]["products"]}


def test_archive_does_not_rewrite_historical_plan_or_product_snapshots():
    owner, _ = _onboarded("archive-plan@example.com", "과거 기록 업체")
    forwarder = _forwarder()
    _resource(forwarder)
    product = _create(owner)
    registered = owner.post("/api/saas/shipper/lots", json=_lot(product["id"], pallets=1))
    assert registered.status_code == 201, registered.text
    lot_id = registered.json()["registered"]["id"]
    created = forwarder.post(PLANS, json={"lot_ids": [lot_id]})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    with database.begin() as db:
        row = db.get(PlanRow, plan_id)
        original = (deepcopy(row.payload), deepcopy(row.committed))
        original_product = deepcopy(db.get(ProductRow, product["id"]).data)
    assert owner.delete(f"{PRODUCTS}/{product['id']}").status_code == 200
    assert owner.post(f"{PRODUCTS}/{product['id']}/restore").status_code == 200
    changed = owner.patch(f"{PRODUCTS}/{product['id']}", json={"name": "다음 버전 품목"})
    assert changed.status_code == 200, changed.text
    assert owner.delete(f"{PRODUCTS}/{changed.json()['id']}").status_code == 200
    with database.begin() as db:
        row = db.get(PlanRow, plan_id)
        assert (row.payload, row.committed) == original
        assert db.get(ProductRow, product["id"]).data == original_product
    saved = next(
        item for item in owner.get("/api/saas/shipper/lots").json() if item["id"] == lot_id
    )
    assert saved["product_id"] == product["id"]
    assert forwarder.get(f"{PLANS}/{plan_id}").status_code == 200


def test_profile_and_pickup_cannot_select_archived_product():
    owner, _ = _onboarded("archive-profile@example.com", "품목 선택 업체")
    assert owner.delete(f"{PRODUCTS}/grape_so2").status_code == 200
    assert (
        owner.patch("/api/saas/shipper/profile", json={"product_ids": ["grape_so2"]}).status_code
        == 422
    )
    denied = owner.post(
        "/api/saas/shipper/packhouses",
        json={
            "name": "새 장소",
            "region": "부산",
            "lat": 35.1,
            "lon": 129.1,
            "product_ids": ["grape_so2"],
        },
    )
    assert denied.status_code == 422, denied.text
    assert len(owner.get("/api/saas/shipper/packhouses").json()) == 1
    assert owner.post(f"{PRODUCTS}/grape_so2/restore").status_code == 200
    assert (
        owner.patch("/api/saas/shipper/profile", json={"product_ids": ["grape_so2"]}).status_code
        == 200
    )


def test_archive_restore_enforce_tenant_roles_and_csrf(saas_login):
    owner, _ = _onboarded("archive-owner@example.com", "삭제 소유 업체")
    other, _ = _onboarded("archive-other@example.com", "삭제 외부 업체")
    product = _create(owner)
    path = f"{PRODUCTS}/{product['id']}"
    assert other.delete(path).status_code == 404
    assert other.post(f"{path}/restore").status_code == 404
    for blocked in (_platform(), _forwarder()):
        assert blocked.delete(path).status_code == 403
        assert blocked.post(f"{path}/restore").status_code == 403
    anonymous = TestClient(saas_app)
    assert anonymous.delete(path).status_code == 401
    assert anonymous.post(f"{path}/restore").status_code == 401
    demo = saas_login(SHIPPER_ORG_ID)
    assert demo.delete(f"{PRODUCTS}/strawberry").status_code == 403
    assert demo.post(f"{PRODUCTS}/strawberry/restore").status_code == 403
    owner.headers.pop("X-Requested-With")
    assert owner.delete(path).status_code == 403
    assert owner.post(f"{path}/restore").status_code == 403
    assert product["id"] in _ids(owner)


@pytest.mark.parametrize("invalid", ["missing", "foreign-id"])
def test_unknown_products_cannot_be_archived_or_restored(invalid):
    owner, _ = _onboarded("archive-missing@example.com", "없는 품목 업체")
    assert owner.delete(f"{PRODUCTS}/{invalid}").status_code == 404
    assert owner.post(f"{PRODUCTS}/{invalid}/restore").status_code == 404
