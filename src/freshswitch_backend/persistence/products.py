from sqlalchemy import select
from sqlalchemy.orm import Session

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Product
from freshswitch_backend.persistence.tables import ProductArchiveRow, ProductRow


def product_rows(db: Session, org_id: str) -> list[ProductRow]:
    return list(
        db.scalars(
            select(ProductRow)
            .where(ProductRow.org_id == org_id)
            .order_by(ProductRow.created_at, ProductRow.id)
        )
    )


def archived_product_ids(db: Session, org_id: str) -> set[str]:
    return set(
        db.scalars(select(ProductArchiveRow.product_id).where(ProductArchiveRow.org_id == org_id))
    )


def active_products(
    base: list[Product], rows: list[ProductRow], archived: set[str] | None = None
) -> list[Product]:
    replaced = {row.replaces_id for row in rows if row.replaces_id is not None}
    current = [p for p in base if p.id not in replaced] + [
        Product.model_validate(row.data) for row in rows if not row.retired
    ]
    return [product for product in current if product.id not in (archived or set())]


def selectable_product_ids(db: Session, org_id: str, scenario_id: str = "default") -> set[str]:
    scenario = get_scenario(scenario_id)
    base = list(scenario.products) if scenario is not None else []
    return {
        product.id
        for product in active_products(
            base, product_rows(db, org_id), archived_product_ids(db, org_id)
        )
    }
