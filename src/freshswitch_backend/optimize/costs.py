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
    def __init__(self, scenario: Scenario) -> None:
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
        return per_day * self.terminal_extra_days(self.voyage_of(container))

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
        wait = self.sea_wait_days(lot, voyage, pull_forward_days)
        rate = min(
            pc.max_sea_loss_rate.value,
            pc.sea_base_loss_rate.value + pc.loss_rate_per_wait_day.value * wait,
        )
        rate += pc.early_harvest_loss_rate_per_day.value * pull_forward_days
        delta = self.params.loss_delta(lot.product_id, lot.destination, "sea")
        if delta:
            rate = min(1.0, max(0.0, rate + delta))
        return rate

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
            collection=lot.pallets * self._trucking_per_pallet(self.collection_km(lot, voyage)),
            storage=lot.pallets * wait * self.params.storage_per_pallet_day_krw.value,
            loss_risk=self.lot_value(lot) * rate,
        )

    def air_lot_cost(self, lot: Lot) -> LotCost:
        p = self.params
        return LotCost(
            freight=self.lot_kg(lot) * self.air_rate_per_kg,
            collection=lot.pallets * self._trucking_per_pallet(p.air_trucking_km.value),
            storage=lot.pallets * p.air_storage_days.value * p.storage_per_pallet_day_krw.value,
            loss_risk=self.lot_value(lot) * self.air_loss_rate(lot),
        )

    def unassigned_penalty(self, lot: Lot) -> float:
        return lot.pallets * self.params.unassigned_penalty_per_pallet_krw.value

    def tariff(self, lot: Lot) -> PalletTariff | None:
        return self.params.pallet_tariffs.get(lot.product_id)

    def value_per_pallet(self, lot: Lot) -> float:
        rate = self.params.for_product(lot.product_id).value_per_kg_krw.value
        return self.product(lot).net_pallet_kg * rate

    def delay_wait_days(self, lot: Lot, voyage: Voyage, pull_forward_days: int = 0) -> int:
        if voyage.original_etd is None:
            return 0
        planned = voyage.model_copy(update={"etd": voyage.original_etd})
        now = self.sea_wait_days(lot, voyage, pull_forward_days)
        return max(0, now - self.sea_wait_days(lot, planned, pull_forward_days))

    def tariff_air_cost(self, lot: Lot) -> LotCost:
        t = self.tariff(lot)
        n = lot.pallets
        kg = self.product(lot).net_pallet_kg
        freight = t.air_freight.value + (self.air_rate_per_kg - t.air_rate_basis_per_kg.value) * kg
        delta = self.params.loss_delta(lot.product_id, lot.destination, "air")
        loss = n * t.air_loss_risk.value + self.lot_value(lot) * delta
        return LotCost(freight=n * freight, inland=n * t.air_inland.value, loss_risk=max(0.0, loss))

    def tariff_sea_cost(
        self, lot: Lot, voyage: Voyage, pull_forward_days: int = 0, plug_share: float = 0.0
    ) -> LotCost:
        t = self.tariff(lot)
        n = lot.pallets
        pc = self.params.for_product(lot.product_id)
        extra_wait = self.delay_wait_days(lot, voyage, pull_forward_days)
        base = t.sea_loss_risk_short_window if lot.sell_window == "short" else t.sea_loss_risk
        extra_rate = (
            pc.loss_rate_per_wait_day.value * extra_wait
            + pc.early_harvest_loss_rate_per_day.value * pull_forward_days
            + self.params.loss_delta(lot.product_id, lot.destination, "sea")
        )
        loss = n * base.value + self.lot_value(lot) * extra_rate
        storage = n * extra_wait * self.params.storage_per_pallet_day_krw.value
        return LotCost(
            freight=n * t.sea_freight.value,
            inland=n * t.sea_inland.value + storage + plug_share,
            loss_risk=max(0.0, loss),
        )

    def settle_air_cost(self, lot: Lot) -> LotCost:
        return self.tariff_air_cost(lot) if self.tariff(lot) else self.air_lot_cost(lot)
