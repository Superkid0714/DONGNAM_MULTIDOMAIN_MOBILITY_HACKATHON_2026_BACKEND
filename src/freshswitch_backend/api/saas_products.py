import uuid
from typing import Annotated, Self

from fastapi import APIRouter, HTTPException, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import select

from freshswitch_backend.api.saas_shipper_profile import (
    SCENARIO_ID,
    ShipperPrincipal,
    _profile,
    _writable_org,
)
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.inputs import StorableStr
from freshswitch_backend.domain.models import Product
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.products import (
    active_products,
    archived_product_ids,
    product_rows,
)
from freshswitch_backend.persistence.tables import ProductArchiveRow, ProductRow, ShipperProfileRow

router = APIRouter(prefix="/saas/shipper/products", tags=["saas-shipper-products"])

ProductName = Annotated[
    StorableStr, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
Temperature = Annotated[float, Field(ge=-100, le=100, allow_inf_nan=False)]
Gas = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
ShelfLife = Annotated[int, Field(gt=0, le=3650)]
PalletKg = Annotated[float, Field(gt=0, le=10000, allow_inf_nan=False)]


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ProductName
    temp_min_c: Temperature
    temp_max_c: Temperature
    shelf_life_days: ShelfLife
    pallet_kg: PalletKg
    so2_treated: bool = False
    o2_pct: Gas | None = None
    co2_pct: Gas | None = None

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("품목 이름을 입력하세요")
        return value.strip()

    @model_validator(mode="after")
    def valid_conditions(self) -> Self:
        if self.temp_min_c > self.temp_max_c:
            raise ValueError("최저 보관 온도는 최고 보관 온도 이하여야 합니다")
        if (self.o2_pct or 0) + (self.co2_pct or 0) > 100:
            raise ValueError("O₂와 CO₂ 농도의 합은 100% 이하여야 합니다")
        return self


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ProductName | None = None
    temp_min_c: Temperature | None = None
    temp_max_c: Temperature | None = None
    shelf_life_days: ShelfLife | None = None
    pallet_kg: PalletKg | None = None
    so2_treated: bool | None = None
    o2_pct: Gas | None = None
    co2_pct: Gas | None = None

    @model_validator(mode="after")
    def nonnull_fields(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("수정할 품목 정보를 입력하세요")
        for field in self.model_fields_set - {"o2_pct", "co2_pct"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} 값은 비울 수 없습니다")
        return self


class ProductOut(Product):
    archived: bool = False
    writable: bool
    replaces_id: str | None = None


def _base_products() -> list[Product]:
    scenario = get_scenario(SCENARIO_ID)
    return list(scenario.products) if scenario else []


def _out(product: Product, rows: list[ProductRow], archived: set[str] | None = None) -> ProductOut:
    row = next((row for row in rows if row.id == product.id), None)
    return ProductOut(
        **product.model_dump(),
        archived=product.id in (archived or set()),
        writable=row is not None,
        replaces_id=row.replaces_id if row is not None else None,
    )


def _current_product(product_id: str, base: list[Product], rows: list[ProductRow]) -> Product:
    product = next((p for p in active_products(base, rows) if p.id == product_id), None)
    if product is not None:
        return product
    if any(row.id == product_id or row.replaces_id == product_id for row in rows):
        raise HTTPException(
            status_code=409, detail="이미 수정된 이전 품목입니다. 현재 품목을 확인하세요"
        )
    raise HTTPException(status_code=404, detail="품목을 찾을 수 없습니다")


def _unique_name(name: str, products: list[Product], except_id: str | None = None) -> None:
    if any(p.id != except_id and p.name.casefold() == name.casefold() for p in products):
        raise HTTPException(status_code=409, detail="같은 이름의 품목이 이미 있습니다")


@router.get("")
def list_products(principal: ShipperPrincipal, include_archived: bool = False) -> list[ProductOut]:
    with database.begin() as db:
        rows = product_rows(db, principal.org_id)
        archived = archived_product_ids(db, principal.org_id)
        products = active_products(_base_products(), rows, None if include_archived else archived)
        return [_out(product, rows, archived) for product in products]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_product(body: ProductCreate, principal: ShipperPrincipal) -> ProductOut:
    with database.begin() as db:
        org = _writable_org(db, principal)
        rows = product_rows(db, org.id)
        _unique_name(
            body.name, active_products(_base_products(), rows, archived_product_ids(db, org.id))
        )
        product = Product(
            id=f"prd-{uuid.uuid4().hex}",
            **body.model_dump(),
            source="화주 등록 · 비용·손실률은 기본값(가정)",
            pallet_kg_assumed=False,
        )
        row = ProductRow(id=product.id, org_id=org.id, data=product.model_dump(mode="json"))
        db.add(row)
        db.flush()
        return _out(product, [row])


@router.patch("/{product_id}")
def update_product(
    product_id: str,
    body: ProductPatch,
    principal: ShipperPrincipal,
) -> ProductOut:
    with database.begin() as db:
        org = _writable_org(db, principal)
        rows = product_rows(db, org.id)
        base = _base_products()
        archived = archived_product_ids(db, org.id)
        active = active_products(base, rows, archived)
        original = _current_product(product_id, base, rows)
        if product_id in archived:
            raise HTTPException(status_code=409, detail="삭제한 품목은 복원한 뒤 수정하세요")
        data = original.model_dump(include=set(ProductCreate.model_fields))
        data.update(body.model_dump(exclude_unset=True))
        try:
            checked = ProductCreate.model_validate(data)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _unique_name(checked.name, active, except_id=product_id)
        product = Product(
            id=f"prd-{uuid.uuid4().hex}",
            **checked.model_dump(),
            source="화주 등록 · 비용·손실률은 기본값(가정)",
            pallet_kg_assumed=False,
        )
        previous = next((row for row in rows if row.id == product_id), None)
        if previous is not None:
            previous.retired = True
        row = ProductRow(
            id=product.id,
            org_id=org.id,
            data=product.model_dump(mode="json"),
            replaces_id=product_id,
        )
        db.add(row)
        profile = _profile(db, org.id, lock=True)
        if profile is not None:
            profile.product_ids = [
                product.id if pid == product_id else pid for pid in profile.product_ids
            ]
        db.flush()
        return _out(product, [row])


@router.delete("/{product_id}")
def archive_product(product_id: str, principal: ShipperPrincipal) -> ProductOut:
    with database.begin() as db:
        org = _writable_org(db, principal)
        rows = product_rows(db, org.id)
        product = _current_product(product_id, _base_products(), rows)
        archive = db.get(ProductArchiveRow, (org.id, product_id))
        if archive is None:
            db.add(ProductArchiveRow(org_id=org.id, product_id=product_id))
        for profile in db.scalars(
            select(ShipperProfileRow).where(ShipperProfileRow.org_id == org.id).with_for_update()
        ):
            profile.product_ids = [pid for pid in (profile.product_ids or []) if pid != product_id]
        db.flush()
        return _out(product, rows, {product_id})


@router.post("/{product_id}/restore")
def restore_product(product_id: str, principal: ShipperPrincipal) -> ProductOut:
    with database.begin() as db:
        org = _writable_org(db, principal)
        rows = product_rows(db, org.id)
        base = _base_products()
        product = _current_product(product_id, base, rows)
        archived = archived_product_ids(db, org.id)
        _unique_name(product.name, active_products(base, rows, archived), except_id=product_id)
        archive = db.get(ProductArchiveRow, (org.id, product_id))
        if archive is not None:
            db.delete(archive)
        db.flush()
        return _out(product, rows)
