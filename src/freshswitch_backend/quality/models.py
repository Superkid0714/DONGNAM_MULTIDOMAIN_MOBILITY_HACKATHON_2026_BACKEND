from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from freshswitch_backend.domain.models import LossAdjustment, Mode

OutageBucket = Literal["none", "short", "long", "unknown"]
MixKind = Literal["solo", "shared_same_product", "mixed_products"]
ObservationOrigin = Literal["seed", "arrival", "manual"]


class ObservationIn(BaseModel):
    product_id: str
    destination: str = "HKHKG"
    mode: Mode = "sea"
    pallets: int = Field(gt=0, le=1000)
    actual_loss_pct: float = Field(ge=0, le=100)
    planned_loss_pct: float | None = Field(default=None, ge=0, le=100)
    wait_days: int = Field(default=0, ge=0, le=60)
    outage_minutes: float | None = Field(default=None, ge=0)
    mix: MixKind = "solo"
    so2_in_container: bool = False
    co_loaded_product_ids: list[str] = Field(default_factory=list)
    observed_on: date | None = None
    note: str | None = Field(default=None, max_length=500)
    source: str = Field(default="수동 입력", max_length=300)


class SeedObservation(ObservationIn):
    id: str


class Observation(BaseModel):
    id: str
    origin: ObservationOrigin
    source: str
    product_id: str
    destination: str
    mode: Mode
    pallets: int
    wait_days: int
    pull_forward_days: int = 0
    outage_minutes: float | None
    outage_bucket: OutageBucket
    mix: MixKind
    so2_in_container: bool
    co_loaded_product_ids: list[str]
    planned_loss_rate: float
    actual_loss_rate: float
    condition_uplift: float
    adjusted_loss_rate: float
    observed_on: date
    recorded_at: datetime
    plan_id: str | None = None
    lot_id: str | None = None
    shipper_id: str | None = None
    container_id: str | None = None
    voyage_id: str | None = None
    note: str | None = None


class BreakdownRow(BaseModel):
    key: str
    label: str
    observations: int
    pallets: int
    pallet_share: float
    mean_outage_minutes: float | None
    mean_actual_rate: float
    mean_adjusted_rate: float


class LossCell(BaseModel):
    product_id: str
    product_name: str
    destination: str
    mode: Mode
    prior_rate: float
    prior_source: str
    prior_weight_pallets: float
    observations: int
    pallets: int
    origins: dict[str, int]
    mean_actual_rate: float | None
    mean_adjusted_rate: float | None
    posterior_rate: float
    delta: float
    range_low: float
    range_high: float
    outage_breakdown: list[BreakdownRow]
    mix_breakdown: list[BreakdownRow]
    basis: str


class LossTable(BaseModel):
    scenario_id: str
    method: str
    seed_label: str
    prior_weight_pallets: float
    range_z: float
    outage_thresholds_minutes: dict[str, float]
    total_observations: int
    cells: list[LossCell]
    adjustments: list[LossAdjustment]


class ObservationList(BaseModel):
    seed_label: str
    count: int
    observations: list[Observation]


class ResetResult(BaseModel):
    cleared: int
    seeded: int


class ShipperImpact(BaseModel):
    shipper_id: str
    shipper_name: str
    savings_prior: int
    savings_learned: int
    savings_change: int
    fee_prior: int
    fee_learned: int
    loss_risk_prior: int
    loss_risk_learned: int
    sea_pallets_prior: int
    sea_pallets_learned: int
    air_pallets_prior: int
    air_pallets_learned: int
    sea_cargo_value: int
    air_switch_margin_rate: float | None


class LotShift(BaseModel):
    lot_id: str
    shipper_id: str
    before: str
    after: str


class ImpactResult(BaseModel):
    scenario_id: str
    adjustments: list[LossAdjustment]
    cells: list[LossCell]
    shippers: list[ShipperImpact]
    lot_shifts: list[LotShift]
    total_savings_prior: int
    total_savings_learned: int
    total_savings_change: int
    total_fee_prior: int
    total_fee_learned: int
    summary: str
