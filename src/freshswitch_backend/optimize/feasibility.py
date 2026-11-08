from dataclasses import dataclass
from datetime import timedelta

from freshswitch_backend.domain.models import Container, Lot, Product
from freshswitch_backend.optimize.costs import CostModel


def is_ca_product(product: Product) -> bool:
    return product.o2_pct is not None or product.co2_pct is not None


def products_compatible(a: Product, b: Product) -> bool:
    if a.so2_treated != b.so2_treated:
        return False
    if a.id == b.id:
        return True
    temps_overlap = max(a.temp_min_c, b.temp_min_c) <= min(a.temp_max_c, b.temp_max_c)
    same_atmosphere = a.o2_pct == b.o2_pct and a.co2_pct == b.co2_pct
    return temps_overlap and same_atmosphere


@dataclass(frozen=True)
class SeaOption:
    container_id: str
    pull_forward_days: int


def sea_option(
    model: CostModel, lot: Lot, container: Container, max_pull_forward_days: int
) -> tuple[SeaOption | None, str | None]:
    product = model.product(lot)
    voyage = model.voyage_of(container)
    if voyage.destination != lot.destination:
        return None, "목적지 불일치"
    if lot.pallets > container.capacity_pallets:
        return None, "로트가 컨테이너 용량보다 큼"
    if is_ca_product(product) and "CA" not in container.kind.upper():
        return None, "CA 미지원 컨테이너"
    late_days = (lot.ready_date - model.cutoff_date(voyage)).days
    if late_days > max_pull_forward_days:
        return None, "출하일이 선적 마감 이후"
    pull = max(0, late_days)
    arrival = model.sea_arrival_date(voyage)
    if arrival > lot.sell_by_date:
        return None, "도착일이 판매기한 초과"
    effective_ready = lot.ready_date - timedelta(days=pull)
    if (arrival - effective_ready).days > product.shelf_life_days:
        return None, "신선 유지 기간 초과"
    return SeaOption(container.id, pull), None


def air_rejection(model: CostModel, lot: Lot) -> str | None:
    arrival = model.air_arrival_date(lot)
    if arrival > lot.sell_by_date:
        return "항공도 판매기한 초과"
    if (arrival - lot.ready_date).days > model.product(lot).shelf_life_days:
        return "항공도 신선 유지 기간 초과"
    return None
