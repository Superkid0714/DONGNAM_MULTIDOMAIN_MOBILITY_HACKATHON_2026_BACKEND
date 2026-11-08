import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.inputs import StorableStr
from freshswitch_backend.domain.models import Product
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.products import (
    active_products,
    archived_product_ids,
    product_rows,
)
from freshswitch_backend.persistence.tables import (
    LotRow,
    OrganizationRow,
    PackhouseRow,
    ShipperProfileRow,
)

SCENARIO_ID = "default"
REGISTERED_SOURCE = "등록"
SANDBOX_LOCKED = "데모(sandbox) 조직의 화주 정보는 바꿀 수 없습니다"
PACKHOUSE_NOT_FOUND = "선과장을 찾을 수 없습니다"
PROFILE_NOT_FOUND = "화주 프로필이 없습니다"
PACKHOUSE_IN_USE = "화주 프로필이나 출하가 쓰는 선과장은 지울 수 없습니다"
LAT_RANGE = (33.0, 39.0)
LON_RANGE = (124.0, 132.0)

router = APIRouter(prefix="/saas/shipper", tags=["saas-shipper-profile"])

ShipperPrincipal = Annotated[Principal, Depends(require_role("shipper"))]
Name = Annotated[
    StorableStr, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
Region = Annotated[
    StorableStr, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Lat = Annotated[float, Field(ge=LAT_RANGE[0], le=LAT_RANGE[1])]
Lon = Annotated[float, Field(ge=LON_RANGE[0], le=LON_RANGE[1])]


class ProductOption(BaseModel):
    product_id: str
    name: str


class PackhouseOut(BaseModel):
    packhouse_id: str
    name: str
    region: str
    lat: float
    lon: float
    source: str
    in_use: bool


class PackhouseCreate(BaseModel):
    name: Name
    region: Region
    lat: Lat
    lon: Lon
    product_ids: list[StorableStr] = Field(default_factory=list, max_length=20)


class PackhousePatch(BaseModel):
    name: Name | None = None
    region: Region | None = None
    lat: Lat | None = None
    lon: Lon | None = None


class ShipperProfileOut(BaseModel):
    shipper_id: str
    name: str
    packhouse_id: str
    product_ids: list[str]
    source: str


class ShipperProfilePatch(BaseModel):
    name: Name | None = None
    packhouse_id: StorableStr | None = Field(default=None, min_length=1, max_length=100)
    product_ids: list[StorableStr] | None = Field(default=None, max_length=20)


class ShipperProfileView(BaseModel):
    org_id: str
    org_name: str
    sandbox: bool
    ready: bool
    profile: ShipperProfileOut | None
    packhouses: list[PackhouseOut]
    product_options: list[ProductOption]


def _products(org_id: str) -> list[Product]:
    scenario = get_scenario(SCENARIO_ID)
    with database.begin() as db:
        return active_products(
            list(scenario.products) if scenario is not None else [],
            product_rows(db, org_id),
            archived_product_ids(db, org_id),
        )


def _checked_products(product_ids: list[str], org_id: str) -> list[str]:
    known = {p.id for p in _products(org_id)}
    unknown = [pid for pid in product_ids if pid not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"없는 품목입니다: {', '.join(unknown)}. 가능: {', '.join(sorted(known))}",
        )
    return list(dict.fromkeys(product_ids))


def _org(db: Session, principal: Principal) -> OrganizationRow:
    org = db.get(OrganizationRow, principal.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="조직을 찾을 수 없습니다")
    return org


def _writable_org(db: Session, principal: Principal) -> OrganizationRow:
    org = db.scalar(
        select(OrganizationRow).where(OrganizationRow.id == principal.org_id).with_for_update()
    )
    if org is None:
        raise HTTPException(status_code=404, detail="조직을 찾을 수 없습니다")
    if org.sandbox:
        raise HTTPException(status_code=403, detail=SANDBOX_LOCKED)
    return org


def _profile(db: Session, org_id: str, lock: bool = False) -> ShipperProfileRow | None:
    query = (
        select(ShipperProfileRow)
        .where(ShipperProfileRow.org_id == org_id)
        .order_by(ShipperProfileRow.created_at, ShipperProfileRow.id)
        .limit(1)
    )
    return db.scalar(query.with_for_update() if lock else query)


def _packhouse(db: Session, org_id: str, packhouse_id: str) -> PackhouseRow:
    row = db.get(PackhouseRow, packhouse_id)
    if row is None or row.org_id != org_id:
        raise HTTPException(status_code=404, detail=PACKHOUSE_NOT_FOUND)
    return row


def _own_packhouse_id(db: Session, org_id: str, packhouse_id: str) -> str:
    row = db.get(PackhouseRow, packhouse_id)
    if row is None or row.org_id != org_id:
        raise HTTPException(status_code=422, detail=f"조직의 선과장이 아닙니다: {packhouse_id}")
    return row.id


def _used_packhouses(db: Session, org_id: str) -> set[str]:
    profiles = db.scalars(
        select(ShipperProfileRow.packhouse_id).where(ShipperProfileRow.org_id == org_id)
    )
    lots = db.scalars(
        select(LotRow.packhouse).where(
            LotRow.shipper_org_id == org_id,
            LotRow.packhouse.is_not(None),
            LotRow.status != "cancelled",
        )
    )
    return {*profiles, *(packhouse for packhouse in lots if packhouse is not None)}


def _packhouse_out(row: PackhouseRow, used: set[str]) -> PackhouseOut:
    return PackhouseOut(
        packhouse_id=row.id,
        name=row.name,
        region=row.region,
        lat=row.lat,
        lon=row.lon,
        source=row.source,
        in_use=row.id in used,
    )


def _profile_out(row: ShipperProfileRow) -> ShipperProfileOut:
    return ShipperProfileOut(
        shipper_id=row.id,
        name=row.name,
        packhouse_id=row.packhouse_id,
        product_ids=list(row.product_ids or []),
        source=row.source,
    )


def _packhouses(db: Session, org_id: str) -> list[PackhouseOut]:
    used = _used_packhouses(db, org_id)
    rows = db.scalars(
        select(PackhouseRow)
        .where(PackhouseRow.org_id == org_id)
        .order_by(PackhouseRow.created_at, PackhouseRow.id)
    )
    return [_packhouse_out(row, used) for row in rows]


@router.get("/profile")
def read_profile(principal: ShipperPrincipal) -> ShipperProfileView:
    with database.begin() as db:
        org = _org(db, principal)
        profile = _profile(db, org.id)
        return ShipperProfileView(
            org_id=org.id,
            org_name=org.name,
            sandbox=org.sandbox,
            ready=profile is not None,
            profile=_profile_out(profile) if profile is not None else None,
            packhouses=_packhouses(db, org.id),
            product_options=[
                ProductOption(product_id=p.id, name=p.name) for p in _products(org.id)
            ],
        )


@router.patch("/profile")
def update_profile(body: ShipperProfilePatch, principal: ShipperPrincipal) -> ShipperProfileOut:
    with database.begin() as db:
        org = _writable_org(db, principal)
        products = (
            _checked_products(body.product_ids, org.id) if body.product_ids is not None else None
        )
        row = _profile(db, org.id, lock=True)
        if row is None:
            raise HTTPException(status_code=404, detail=PROFILE_NOT_FOUND)
        if body.name is not None:
            row.name = body.name
        if body.packhouse_id is not None and body.packhouse_id != row.packhouse_id:
            row.packhouse_id = _own_packhouse_id(db, org.id, body.packhouse_id)
            db.execute(
                update(LotRow)
                .where(
                    LotRow.shipper_org_id == org.id,
                    LotRow.shipper_id == row.id,
                    LotRow.status == "registered",
                )
                .values(packhouse=row.packhouse_id, updated_at=datetime.now(UTC))
            )
        if products is not None:
            row.product_ids = products
        db.flush()
        return _profile_out(row)


@router.get("/packhouses")
def list_packhouses(principal: ShipperPrincipal) -> list[PackhouseOut]:
    with database.begin() as db:
        return _packhouses(db, principal.org_id)


@router.post("/packhouses", status_code=status.HTTP_201_CREATED)
def create_packhouse(body: PackhouseCreate, principal: ShipperPrincipal) -> PackhouseOut:
    with database.begin() as db:
        org = _writable_org(db, principal)
        products = _checked_products(body.product_ids, org.id)
        row = PackhouseRow(
            id=f"ph-{uuid.uuid4().hex[:12]}",
            org_id=org.id,
            name=body.name,
            region=body.region,
            lat=body.lat,
            lon=body.lon,
            source=REGISTERED_SOURCE,
        )
        db.add(row)
        db.flush()
        profile = _profile(db, org.id, lock=True)
        if profile is None:
            db.add(
                ShipperProfileRow(
                    id=f"sh-{uuid.uuid4().hex[:12]}",
                    org_id=org.id,
                    name=org.name,
                    packhouse_id=row.id,
                    product_ids=products,
                    source=REGISTERED_SOURCE,
                )
            )
        elif products:
            profile.product_ids = list(dict.fromkeys([*(profile.product_ids or []), *products]))
        db.flush()
        return _packhouse_out(row, _used_packhouses(db, org.id))


@router.patch("/packhouses/{packhouse_id}")
def update_packhouse(
    packhouse_id: str, body: PackhousePatch, principal: ShipperPrincipal
) -> PackhouseOut:
    with database.begin() as db:
        org = _writable_org(db, principal)
        row = _packhouse(db, org.id, packhouse_id)
        for name in ("name", "region", "lat", "lon"):
            value = getattr(body, name)
            if value is not None:
                setattr(row, name, value)
        db.flush()
        return _packhouse_out(row, _used_packhouses(db, org.id))


@router.delete("/packhouses/{packhouse_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_packhouse(packhouse_id: str, principal: ShipperPrincipal) -> Response:
    with database.begin() as db:
        org = _writable_org(db, principal)
        row = _packhouse(db, org.id, packhouse_id)
        if row.id in _used_packhouses(db, org.id):
            raise HTTPException(status_code=409, detail=PACKHOUSE_IN_USE)
        db.delete(row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
