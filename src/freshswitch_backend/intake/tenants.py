from dataclasses import dataclass, field

from sqlalchemy import select

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Lot, Packhouse, Product, Scenario, Shipper
from freshswitch_backend.lots import ANY_ORG
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import same_sandbox
from freshswitch_backend.persistence.tables import (
    LotRow,
    OrganizationRow,
    PackhouseRow,
    ProductRow,
    ShipperProfileRow,
)


@dataclass(frozen=True)
class TenantShipper:
    shipper: Shipper
    org_id: str
    org_name: str
    product_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TenantCatalog:
    shippers: list[TenantShipper] = field(default_factory=list)
    packhouses: list[Packhouse] = field(default_factory=list)
    products: list[Product] = field(default_factory=list)

    @property
    def shipper_ids(self) -> list[str]:
        return [s.shipper.id for s in self.shippers]

    def org_of(self, shipper_id: str) -> str | None:
        return next((s.org_id for s in self.shippers if s.shipper.id == shipper_id), None)

    def org_names(self) -> dict[str, str]:
        return {s.shipper.id: s.org_name for s in self.shippers}


def _profile_rank_id(profile_id: str) -> str:
    if profile_id.startswith("sh-"):
        return profile_id
    mark = profile_id.rfind("-sh-")
    if mark < 0:
        return profile_id
    return profile_id[mark + 1 :]


def load_catalog(
    org_id: str, base: Scenario | None = None, peer_of: str | None = None
) -> TenantCatalog:
    query = (
        select(ShipperProfileRow, PackhouseRow, OrganizationRow.name)
        .join(PackhouseRow, PackhouseRow.id == ShipperProfileRow.packhouse_id)
        .join(OrganizationRow, OrganizationRow.id == ShipperProfileRow.org_id)
        .where(OrganizationRow.type == "shipper")
        .order_by(ShipperProfileRow.created_at, ShipperProfileRow.id)
    )
    if org_id != ANY_ORG:
        query = query.where(ShipperProfileRow.org_id == org_id)
    if peer_of is not None:
        query = query.where(same_sandbox(OrganizationRow.sandbox, peer_of))
    products_query = (
        select(ProductRow)
        .join(OrganizationRow, OrganizationRow.id == ProductRow.org_id)
        .where(OrganizationRow.type == "shipper")
    )
    if org_id != ANY_ORG:
        products_query = products_query.where(ProductRow.org_id == org_id)
    if peer_of is not None:
        products_query = products_query.where(same_sandbox(OrganizationRow.sandbox, peer_of))
    packhouses_query = (
        select(PackhouseRow)
        .join(OrganizationRow, OrganizationRow.id == PackhouseRow.org_id)
        .where(OrganizationRow.type == "shipper")
        .order_by(PackhouseRow.created_at, PackhouseRow.id)
    )
    if org_id != ANY_ORG:
        packhouses_query = packhouses_query.where(PackhouseRow.org_id == org_id)
    if peer_of is not None:
        packhouses_query = packhouses_query.where(same_sandbox(OrganizationRow.sandbox, peer_of))
    with database.begin() as db:
        rows = db.execute(query).all()
        products = [Product.model_validate(row.data) for row in db.scalars(products_query)]
        owned_packhouses = list(db.scalars(packhouses_query))
    order = {s.id: i for i, s in enumerate(base.shippers)} if base is not None else {}
    rows = sorted(rows, key=lambda row: order.get(_profile_rank_id(row[0].id), len(order)))
    shippers: list[TenantShipper] = []
    packhouses: dict[str, Packhouse] = {}
    for profile, packhouse, org_name in rows:
        shippers.append(
            TenantShipper(
                shipper=Shipper(
                    id=profile.id,
                    name=profile.name,
                    packhouse_id=profile.packhouse_id,
                    source=profile.source,
                ),
                org_id=profile.org_id,
                org_name=org_name,
                product_ids=tuple(profile.product_ids or ()),
            )
        )
        packhouses.setdefault(
            packhouse.id,
            Packhouse(
                id=packhouse.id,
                name=packhouse.name,
                region=packhouse.region,
                lat=packhouse.lat,
                lon=packhouse.lon,
                source=packhouse.source,
            ),
        )
    for packhouse in owned_packhouses:
        packhouses.setdefault(
            packhouse.id,
            Packhouse(
                id=packhouse.id,
                name=packhouse.name,
                region=packhouse.region,
                lat=packhouse.lat,
                lon=packhouse.lon,
                source=packhouse.source,
            ),
        )
    return TenantCatalog(shippers=shippers, packhouses=list(packhouses.values()), products=products)


def tenant_scenario(
    base: Scenario, catalog: TenantCatalog, lots: list[Lot] | None = None
) -> Scenario:
    return base.model_copy(
        update={
            "shippers": [s.shipper for s in catalog.shippers],
            "packhouses": catalog.packhouses,
            "lots": list(lots or []),
            "products": [*base.products, *catalog.products],
        },
        deep=True,
    )


def eligible_cargo_scenario(scenario: Scenario, viewer_org_id: str) -> Scenario:
    with database.begin() as db:
        rows = list(
            db.scalars(
                select(LotRow)
                .join(
                    OrganizationRow,
                    OrganizationRow.id == LotRow.shipper_org_id,
                )
                .where(
                    LotRow.status == "registered",
                    LotRow.scenario_id == scenario.id,
                    OrganizationRow.type == "shipper",
                    same_sandbox(OrganizationRow.sandbox, viewer_org_id),
                )
            )
        )
    shipper_ids = {row.shipper_id for row in rows}
    product_ids = {row.product_id for row in rows}
    shippers = [s for s in scenario.shippers if s.id in shipper_ids]
    packhouses = {row.packhouse for row in rows if row.packhouse is not None}
    packhouses |= {s.packhouse_id for s in shippers}
    base = get_scenario(scenario.id)
    public_products = {p.id for p in base.products} if base is not None else set()
    return scenario.model_copy(
        update={
            "shippers": shippers,
            "packhouses": [p for p in scenario.packhouses if p.id in packhouses],
            "products": [p for p in scenario.products if p.id in product_ids | public_products],
        },
        deep=True,
    )
