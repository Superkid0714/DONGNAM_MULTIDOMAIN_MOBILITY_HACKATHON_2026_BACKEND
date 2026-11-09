from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, model_validator

KST = timezone(timedelta(hours=9))
DEFAULT_PALLET_KG = 415.0


def to_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    try:
        return value.astimezone(KST)
    except OverflowError as exc:
        raise ValueError("시각 범위를 벗어났습니다") from exc


KstDatetime = Annotated[datetime, AfterValidator(to_kst)]


class Sourced(BaseModel):
    source: str = "가정"


class Product(Sourced):
    id: str
    name: str
    temp_min_c: float
    temp_max_c: float
    o2_pct: float | None = None
    co2_pct: float | None = None
    shelf_life_days: int
    so2_treated: bool = False
    pallet_kg: float = DEFAULT_PALLET_KG
    pallet_kg_assumed: bool = False

    @model_validator(mode="before")
    @classmethod
    def _mark_default_pallet_kg(cls, data: object) -> object:
        if not isinstance(data, dict) or "pallet_kg_assumed" in data:
            return data
        if data.get("pallet_kg") is None:
            rest = {k: v for k, v in data.items() if k != "pallet_kg"}
            return {**rest, "pallet_kg_assumed": True}
        return data

    @property
    def net_pallet_kg(self) -> float:
        return self.pallet_kg


class Packhouse(Sourced):
    id: str
    name: str
    region: str
    lat: float
    lon: float


class GatePeakWindow(Sourced):
    start: time
    end: time
    label: str = ""


class Port(Sourced):
    id: str
    name: str
    unlocode: str
    lat: float
    lon: float
    gate_peak_windows: list[GatePeakWindow] = Field(default_factory=list)


class Shipper(Sourced):
    id: str
    name: str
    packhouse_id: str


class Lot(Sourced):
    id: str
    shipper_id: str
    packhouse_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    product_id: str
    pallets: int = Field(gt=0)
    ready_date: date
    destination: str
    sell_by_date: date
    sell_window: Literal["normal", "short"] = "normal"


class Voyage(Sourced):
    id: str
    vessel_name: str
    terminal: str
    origin: str
    destination: str
    etd: KstDatetime
    cy_cutoff: KstDatetime
    transit_days: float
    original_etd: KstDatetime | None = None
    original_cy_cutoff: KstDatetime | None = Field(default=None, exclude_if=lambda v: v is None)
    call_sign: str | None = None
    delay_p90_h: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    delay_forecast_source: Literal["model", "stat", "provided"] | None = None
    truck_departure_adjustment_h: float = Field(default=0, ge=0, allow_inf_nan=False)


class Container(Sourced):
    id: str
    name: str | None = None
    voyage_id: str
    kind: str = "40RH-CA"
    capacity_pallets: int = 20
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    forwarder_org_id: str | None = None
    resource_revision: int | None = None


class Truck(Sourced):
    id: str
    depot_id: str
    capacity_pallets: int


class Quantity(BaseModel):
    value: float
    unit: str
    source: str = "가정"


def _q(value: float, unit: str, source: str = "가정") -> Quantity:
    return Quantity(value=value, unit=unit, source=source)


class ProductCostParams(BaseModel):
    value_per_kg_krw: Quantity
    sea_base_loss_rate: Quantity
    loss_rate_per_wait_day: Quantity
    max_sea_loss_rate: Quantity
    early_harvest_loss_rate_per_day: Quantity
    air_loss_rate: Quantity


def _default_product_cost() -> ProductCostParams:
    return ProductCostParams(
        value_per_kg_krw=_q(18000, "원/kg"),
        sea_base_loss_rate=_q(0.015, "비율"),
        loss_rate_per_wait_day=_q(0.009, "비율/일"),
        max_sea_loss_rate=_q(
            0.07, "비율", "JLST 2025 단전 손실6~8%의 중앙값7%; 시간 보간·상한 가정"
        ),
        early_harvest_loss_rate_per_day=_q(0.02, "비율/일"),
        air_loss_rate=_q(0.01, "비율"),
    )


class LossAdjustment(BaseModel):
    product_id: str
    destination: str
    mode: Literal["sea", "air"]
    delta: float
    source: str = "도착 검수 학습"


class PalletTariff(BaseModel):
    air_freight: Quantity
    air_inland: Quantity
    air_loss_risk: Quantity
    sea_freight: Quantity
    sea_inland: Quantity
    sea_loss_risk: Quantity
    sea_loss_risk_short_window: Quantity | None = None
    air_rate_basis_per_kg: Quantity


BillingRule = Literal["performance", "usage_fee"]
BaselineKind = Literal["air_solo", "sea_solo"]
SettlementBillingRule = Literal["performance", "usage_fee", "mixed"]
SettlementBaselineKind = Literal["air_solo", "sea_solo", "mixed"]


class BillingParams(BaseModel):
    usage_fee_per_pallet_krw: Quantity = _q(
        20_000,
        "원/파렛트",
        "가정: 기획서 ㅍ3 8-1 파렛트 이용료 2만 원(이미 배로 가는 품목은 성과보수 대신 이용료)",
    )
    usage_fee_products: list[str] = Field(default_factory=lambda: ["grape_so2"])
    usage_fee_source: str = (
        "기획서 ㅍ3 8-1: 포도처럼 이미 배로 가는 품목은 성과보수 없이 파렛트 이용료만 받는다"
    )
    pilot: bool = False
    pilot_source: str = (
        "기획서 ㅍ3 8-1: 첫 항차 무료 시범. 켜면 수수료 0원, 원래 금액은 함께 싣는다"
    )


class CostParams(BaseModel):
    sea_shipper_document_krw: Quantity = Field(default_factory=lambda: _q(0, "원/화주"))
    sea_shipper_pickup_krw: Quantity = Field(default_factory=lambda: _q(0, "원/화주"))
    sea_cfs_per_pallet_krw: Quantity = Field(default_factory=lambda: _q(0, "원/파렛트"))
    sea_fixed_pickup: bool = False
    sea_freight_per_container_krw: Quantity = _q(
        3_320_000,
        "원/컨테이너",
        "원예원 CA 해상 400,000원/t × 0.415t × 20P (이데일리 2024.5 보도 참고)",
    )
    sea_ancillary_per_container_krw: Quantity = _q(
        8_422_256,
        "원/컨테이너",
        "보정 가정: 20 × 0.7 × (1681+470) × 415 - 3320000 - 기준 집하비 755054 = 8422256원. 기준 경로 진주8P·산청6P·밀양6P, 손실·보관 제외. 70%는 요청된 보정 목표이며 실견적 아님",
    )
    air_freight_per_kg_krw: Quantity = _q(1681, "원/kg")
    air_fuel_surcharge_per_kg_krw: Quantity = _q(470, "원/kg")
    air_fuel_surcharge_by_month: dict[str, Quantity] = Field(default_factory=dict)
    reefer_truck_cost_per_km_krw: Quantity = _q(2000, "원/km")
    reefer_truck_fixed_per_route_krw: Quantity = _q(0, "원/대")
    truck_capacity_pallets: Quantity = _q(16, "파렛트")
    road_distance_factor: Quantity = _q(1.3, "배")
    air_trucking_km: Quantity = _q(350, "km")
    storage_per_pallet_day_krw: Quantity = _q(3000, "원/파렛트·일")
    terminal_reefer_plug_per_container_day: Quantity = _q(50_000, "원/컨테이너·일")
    air_storage_days: Quantity = _q(1.5, "일")
    air_transit_days: Quantity = _q(0.5, "일")
    unassigned_penalty_per_pallet_krw: Quantity = _q(10_000_000, "원/파렛트")
    platform_fee_rate: Quantity = _q(0.25, "비율")
    product_costs: dict[str, ProductCostParams] = Field(default_factory=dict)
    default_product_cost: ProductCostParams = Field(default_factory=_default_product_cost)
    loss_adjustments: list[LossAdjustment] = Field(default_factory=list)
    pallet_tariffs: dict[str, PalletTariff] = Field(default_factory=dict)
    billing: BillingParams = Field(default_factory=BillingParams)

    def for_product(self, product_id: str) -> ProductCostParams:
        return self.product_costs.get(product_id, self.default_product_cost)

    def loss_delta(self, product_id: str, destination: str, mode: str) -> float:
        return sum(
            a.delta
            for a in self.loss_adjustments
            if a.product_id == product_id and a.destination == destination and a.mode == mode
        )


class Scenario(BaseModel):
    id: str
    name: str
    description: str = ""
    products: list[Product]
    packhouses: list[Packhouse]
    ports: list[Port]
    shippers: list[Shipper]
    lots: list[Lot]
    voyages: list[Voyage]
    containers: list[Container]
    trucks: list[Truck]
    cost_params: CostParams = Field(default_factory=CostParams)


class ScenarioSummary(BaseModel):
    id: str
    name: str
    description: str


Mode = Literal["sea", "air"]


AssignmentReasonCode = Literal[
    "fixed",
    "capacity_or_cost",
    "destination_mismatch",
    "over_capacity",
    "ca_unsupported",
    "sea_deadline_miss",
    "sea_sell_by_miss",
    "sea_shelf_life_miss",
    "sea_forbidden",
    "pickup_cutoff_miss",
    "air_not_allowed",
    "air_forbidden",
    "air_sell_by_miss",
    "air_shelf_life_miss",
    "no_feasible_mode",
    "no_solution",
    "other",
]


class AssignmentReason(BaseModel):
    code: AssignmentReasonCode
    message: str


class Assignment(BaseModel):
    lot_id: str
    shipper_id: str
    product_id: str
    pallets: int
    mode: Mode
    container_id: str | None = None
    voyage_id: str | None = None
    pull_forward_days: int = 0
    reason: AssignmentReason | None = None


class UnassignedLot(BaseModel):
    lot_id: str
    shipper_id: str
    pallets: int
    reason: str
    reason_code: AssignmentReasonCode = "other"


class ContainerLoad(BaseModel):
    container_id: str
    voyage_id: str
    used: bool
    pallets: int
    capacity_pallets: int
    utilization: float
    lot_ids: list[str]
    product_ids: list[str]
    shipper_ids: list[str]
    container_cost: int
    terminal_extra_days: float = 0.0
    terminal_plug_cost: int = 0


class CostBreakdown(BaseModel):
    freight: int = 0
    inland: int = 0
    handling: int = 0
    collection: int = 0
    storage: int = 0
    loss_risk: int = 0
    total: int = 0
    collection_estimate: int | None = None
    collection_route: int | None = None


class ShipperCost(BaseModel):
    shipper_id: str
    pallets: int
    sea_pallets: int
    air_pallets: int
    plan: CostBreakdown
    baseline: CostBreakdown
    operational: CostBreakdown | None = None
    operational_baseline: CostBreakdown | None = None


SettlementBasis = Literal["tariff", "operational", "mixed"]


def backfill_fee_split[T: "Settlement | ContainerShipperSettlement"](row: T) -> T:
    if row.performance_fee == 0 and row.usage_fee == 0 and row.fee > 0 and not row.pilot:
        row.performance_fee = row.fee
    return row


class QuoteDelayInput(BaseModel):
    voyage_id: str
    delay_p90_h: float
    truck_departure_adjustment_h: float
    unpowered_h: float
    source: str


class QuoteAmounts(BaseModel):
    air_cost: int | None = None
    sea_solo_cost: int | None = None
    shipper_net_saving: int | None = None
    savings_vs_air_pct: float | None = None
    savings_vs_solo_pct: float | None = None
    air_switch_candidate: bool = False
    quote_assumptions: list[str] = Field(default_factory=list)
    delay_inputs: list[QuoteDelayInput] = Field(default_factory=list)


class Settlement(QuoteAmounts):
    shipper_id: str
    baseline_cost: int
    shared_cost: int
    savings: int
    fee: int
    shipper_gain: int
    sea_pallets: int = 0
    air_pallets: int = 0
    basis: SettlementBasis = "operational"
    billing_rule: SettlementBillingRule = "performance"
    performance_fee: int = 0
    usage_fee: int = 0
    baseline_kind: SettlementBaselineKind = "air_solo"
    baseline_label: str = "단독 항공 대비"
    savings_baseline_label: str = "단독 항공 대비"
    pilot: bool = False

    _legacy_fee_split = model_validator(mode="after")(backfill_fee_split)


class ContainerShipperSettlement(QuoteAmounts):
    shipper_id: str
    pallets: int
    baseline_cost: int
    shared_cost: int
    savings: int
    fee: int
    shipper_gain: int
    basis: SettlementBasis
    billing_rule: SettlementBillingRule = "performance"
    performance_fee: int = 0
    usage_fee: int = 0
    baseline_kind: SettlementBaselineKind = "air_solo"
    baseline_label: str = "단독 항공 대비"
    savings_baseline_label: str = "단독 항공 대비"
    pilot: bool = False

    _legacy_fee_split = model_validator(mode="after")(backfill_fee_split)


class ContainerSettlement(BaseModel):
    container_id: str
    voyage_id: str
    pallets: int
    capacity_pallets: int
    shippers: list[ContainerShipperSettlement]
    total_savings: int
    total_fee: int
    total_shipper_gain: int


class TariffLine(BaseModel):
    key: Literal["freight", "inland", "loss_risk"]
    label: str
    air: int
    sea: int
    air_assumed: bool
    sea_assumed: bool
    air_source: str
    sea_source: str


class TariffSheet(BaseModel):
    product_id: str
    unit: str = "원/파렛트"
    lines: list[TariffLine]
    air_total: int
    sea_total: int
    savings_per_pallet: int
    fee_rate: float
    fee_per_pallet: int
    shipper_share_per_pallet: int
    billing_rule: BillingRule = "performance"
    pilot: bool = False
    sea_loss_risk_short_window: int
    sea_total_short_window: int
    short_window_rule: str
    assumed_items: list[str]


CollectionBasis = Literal["estimate", "route"]


class LotCollection(BaseModel):
    lot_id: str
    shipper_id: str
    voyage_id: str
    truck_id: str | None
    stop_id: str
    pallets: int
    estimate: int
    route: int
    trip: int = 1


class CollectionReassignment(BaseModel):
    lot_id: str
    shipper_id: str
    voyage_id: str
    stop_id: str
    reason: str
    location_after: str


class Plan(BaseModel):
    scenario_id: str
    status: str
    objective: int
    solve_ms: int
    assignments: list[Assignment]
    unassigned: list[UnassignedLot]
    containers: list[ContainerLoad]
    shipper_costs: list[ShipperCost]
    settlements: list[Settlement]
    total_baseline_cost: int
    total_shared_cost: int
    total_savings: int
    total_fee: int
    total_operational_baseline_cost: int = 0
    total_operational_cost: int = 0
    total_operational_savings: int = 0
    container_settlements: list[ContainerSettlement] = Field(default_factory=list)
    settlement_tariffs: list[TariffSheet] = Field(default_factory=list)
    collection_basis: CollectionBasis = "estimate"
    total_collection_estimate: int = 0
    total_collection_route: int | None = None
    lot_collections: list[LotCollection] = Field(default_factory=list)
    collection_reassigned: list[CollectionReassignment] = Field(default_factory=list)
