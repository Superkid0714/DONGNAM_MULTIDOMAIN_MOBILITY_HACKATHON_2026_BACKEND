import math
from dataclasses import dataclass
from datetime import date, timedelta

from freshswitch_backend.domain.models import (
    Container,
    Lot,
    Packhouse,
    PalletTariff,
    Port,
    Product,
    Scenario,
    Shipper,
    Voyage,
)

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class LotCost:
    freight: float = 0.0
    inland: float = 0.0
    handling: float = 0.0
    collection: float = 0.0
    storage: float = 0.0
    loss_risk: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.freight
            + self.inland
            + self.handling
            + self.collection
            + self.storage
            + self.loss_risk
        )


class CostModel:
    def __init__(self, scenario: Scenario, *, compute_solo: bool = True) -> None:
        from freshswitch_backend.forecast.quote import prepare_quote_scenario

        scenario = prepare_quote_scenario(scenario)
        self.compute_solo = compute_solo
        self.scenario = scenario
        self.params = scenario.cost_params
        self.products: dict[str, Product] = {p.id: p for p in scenario.products}
        self.shippers: dict[str, Shipper] = {s.id: s for s in scenario.shippers}
        self.packhouses: dict[str, Packhouse] = {p.id: p for p in scenario.packhouses}
        self.ports: dict[str, Port] = {p.id: p for p in scenario.ports}
        self.voyages: dict[str, Voyage] = {v.id: v for v in scenario.voyages}
        self.containers: dict[str, Container] = {c.id: c for c in scenario.containers}

    @property
    def container_freight(self) -> float:
        return self.params.sea_freight_per_container_krw.value

    @property
    def container_handling(self) -> float:
        return self.params.sea_ancillary_per_container_krw.value

    @property
    def container_fixed_cost(self) -> float:
        return self.container_freight + self.container_handling

    @staticmethod
    def terminal_extra_days(voyage: Voyage) -> float:
        if voyage.original_etd is None:
            return 0.0
        return max(0.0, (voyage.etd - voyage.original_etd).total_seconds() / 86400)

    def container_plug_cost(self, container: Container) -> float:
        per_day = self.params.terminal_reefer_plug_per_container_day.value
        voyage = self.voyage_of(container)
        if voyage.etd.month in (3, 4, 5):
            per_day *= 1.2
        return per_day * self.terminal_extra_days(voyage)

    def container_total_fixed_cost(self, container: Container) -> float:
        return self.container_fixed_cost + self.container_plug_cost(container)

    def product(self, lot: Lot) -> Product:
        return self.products[lot.product_id]

    def lot_kg(self, lot: Lot) -> float:
        return lot.pallets * self.product(lot).net_pallet_kg

    def lot_value(self, lot: Lot) -> float:
        rate = self.params.for_product(lot.product_id).value_per_kg_krw.value
        return self.lot_kg(lot) * rate

    def voyage_of(self, container: Container) -> Voyage:
        return self.voyages[container.voyage_id]

    @staticmethod
    def cutoff_date(voyage: Voyage) -> date:
        return voyage.cy_cutoff.date()

    @staticmethod
    def sea_arrival_date(voyage: Voyage) -> date:
        return (voyage.etd + timedelta(days=voyage.transit_days)).date()

    @property
    def air_rate_per_kg(self) -> float:
        p = self.params
        return p.air_freight_per_kg_krw.value + p.air_fuel_surcharge_per_kg_krw.value

    def air_rate_for(self, lot: Lot) -> float:
        monthly = self.params.air_fuel_surcharge_by_month.get(lot.ready_date.strftime("%Y-%m"))
        surcharge = monthly or self.params.air_fuel_surcharge_per_kg_krw
        return self.params.air_freight_per_kg_krw.value + surcharge.value

    def terminal_unpowered_hours(self, voyage: Voyage) -> float:
        resolved = self.voyages[voyage.id]
        return max(0.0, (resolved.delay_p90_h or 0.0) - resolved.truck_departure_adjustment_h)

    def remaining_sell_days(self, lot: Lot, voyage: Voyage) -> int:
        resolved = self.voyages[voyage.id]
        base = resolved.original_etd or resolved.etd
        predicted = max(resolved.etd, base + timedelta(hours=resolved.delay_p90_h or 0.0))
        return (lot.sell_by_date - (predicted + timedelta(days=resolved.transit_days)).date()).days

    def air_lead_days(self) -> int:
        return math.ceil(self.params.air_storage_days.value + self.params.air_transit_days.value)

    def air_arrival_date(self, lot: Lot) -> date:
        return lot.ready_date + timedelta(days=self.air_lead_days())

    def _trucking_per_pallet(self, one_way_km: float) -> float:
        p = self.params
        trip = 2 * one_way_km * p.reefer_truck_cost_per_km_krw.value
        return trip / p.truck_capacity_pallets.value

    def collection_km(self, lot: Lot, voyage: Voyage) -> float:
        shipper = self.shippers.get(lot.shipper_id)
        packhouse_id = lot.packhouse_id or (shipper.packhouse_id if shipper else None)
        packhouse = self.packhouses.get(packhouse_id)
        port = self.ports.get(voyage.origin)
        if packhouse is None or port is None:
            return 0.0
        straight = haversine_km(packhouse.lat, packhouse.lon, port.lat, port.lon)
        return straight * self.params.road_distance_factor.value

    def sea_wait_days(self, lot: Lot, voyage: Voyage, pull_forward_days: int) -> int:
        effective_ready = lot.ready_date - timedelta(days=pull_forward_days)
        return max(0, (voyage.etd.date() - effective_ready).days)

    def sea_loss_rate(self, lot: Lot, voyage: Voyage, pull_forward_days: int = 0) -> float:
        pc = self.params.for_product(lot.product_id)
        h = self.terminal_unpowered_hours(voyage)
        base = pc.sea_base_loss_rate.value
        maximum = pc.max_sea_loss_rate.value
        rate = base + min(h / 5.5, 1.0) * (maximum - base)
        rate += pc.early_harvest_loss_rate_per_day.value * pull_forward_days
        delta = self.params.loss_delta(lot.product_id, lot.destination, "sea")
        return min(maximum, max(0.0, rate + delta))

    def air_loss_rate(self, lot: Lot) -> float:
        rate = self.params.for_product(lot.product_id).air_loss_rate.value
        delta = self.params.loss_delta(lot.product_id, lot.destination, "air")
        if delta:
            rate = min(1.0, max(0.0, rate + delta))
        return rate

    def sea_lot_cost(self, lot: Lot, voyage: Voyage, pull_forward_days: int = 0) -> LotCost:
        wait = self.sea_wait_days(lot, voyage, pull_forward_days)
        rate = self.sea_loss_rate(lot, voyage, pull_forward_days)
        return LotCost(
            handling=lot.pallets * self.params.sea_cfs_per_pallet_krw.value,
            collection=0
            if self.params.sea_fixed_pickup
            else lot.pallets * self._trucking_per_pallet(self.collection_km(lot, voyage)),
            storage=lot.pallets * wait * self.params.storage_per_pallet_day_krw.value,
            loss_risk=self.lot_value(lot) * rate,
        )

    @property
    def sea_shipper_fixed_cost(self) -> float:
        return self.params.sea_shipper_document_krw.value + (
            self.params.sea_shipper_pickup_krw.value if self.params.sea_fixed_pickup else 0
        )

    def air_lot_cost(self, lot: Lot) -> LotCost:
        p = self.params
        return LotCost(
            freight=self.lot_kg(lot) * self.air_rate_for(lot),
            collection=lot.pallets * self._trucking_per_pallet(p.air_trucking_km.value),
            storage=lot.pallets * p.air_storage_days.value * p.storage_per_pallet_day_krw.value,
            loss_risk=self.lot_value(lot) * self.air_loss_rate(lot),
        )

    def unassigned_penalty(self, lot: Lot) -> float:
        return lot.pallets * self.params.unassigned_penalty_per_pallet_krw.value

    def tariff(self, lot: Lot) -> PalletTariff | None:
        # Retained JSON tariff sheets are historical comparison data only.
        return None

    def value_per_pallet(self, lot: Lot) -> float:
        rate = self.params.for_product(lot.product_id).value_per_kg_krw.value
        return self.product(lot).net_pallet_kg * rate

    def delay_wait_days(self, lot: Lot, voyage: Voyage, pull_forward_days: int = 0) -> int:
        if voyage.original_etd is None:
            return 0
        planned = voyage.model_copy(update={"etd": voyage.original_etd})
        now = self.sea_wait_days(lot, voyage, pull_forward_days)
        return max(0, now - self.sea_wait_days(lot, planned, pull_forward_days))

    def settle_air_cost(self, lot: Lot) -> LotCost:
        return self.air_lot_cost(lot)
