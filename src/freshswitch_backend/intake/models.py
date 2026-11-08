from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from freshswitch_backend.domain.ids import ID_MAX_LENGTH, EntityId
from freshswitch_backend.domain.inputs import StorableStr
from freshswitch_backend.domain.models import Lot, Mode
from freshswitch_backend.lots.models import RegisteredLot
from freshswitch_backend.quality.models import ExpectedLoss
from freshswitch_backend.replan.models import AddLotEvent


class IntakeRequest(BaseModel):
    product: str = Field(min_length=1)
    pallets: int
    ready_date: date
    shipper: str = Field(min_length=1)
    packhouse_id: StorableStr | None = Field(default=None, min_length=1, max_length=100)
    destination: str = Field(min_length=1)
    sell_by_date: date | None = None
    sell_window: Literal["normal", "short"] = "normal"
    scenario_id: str = "default"
    plan_id: str | None = None
    lot_id: EntityId | None = Field(default=None, max_length=ID_MAX_LENGTH)
    save: bool = False

    @field_validator("lot_id", mode="before")
    @classmethod
    def _blank_lot_id(cls, value: object) -> object:
        return None if value == "" else value


class FieldError(BaseModel):
    field: str
    message: str


class Resolved(BaseModel):
    product_id: str
    product_name: str
    shipper_id: str
    shipper_name: str
    packhouse_id: str
    packhouse_name: str
    destination_port_id: str
    destination_name: str


class SeaHint(BaseModel):
    container_id: str
    voyage_id: str
    vessel_name: str
    etd: datetime
    cy_cutoff: datetime
    arrival_date: date
    feasible: bool
    reasons: list[str]
    pull_forward_days: int = 0
    free_pallets: int | None = None
    loaded_product_ids: list[str] = Field(default_factory=list)


class AirHint(BaseModel):
    feasible: bool
    arrival_date: date
    reason: str | None = None


class SeasonCheck(BaseModel):
    product_id: str
    label: str
    months: list[int]
    in_season: bool
    source: str


class SavingsEstimate(BaseModel):
    mode: Mode
    container_id: str | None = None
    voyage_id: str | None = None
    pallets: int
    basis: Literal["tariff", "operational"]
    billing_rule: Literal["performance", "usage_fee"]
    rule: str
    baseline_cost: int
    shared_cost: int
    savings: int
    savings_per_pallet: int
    fee: int
    shipper_gain: int
    assumed_container_pallets: int | None = None
    assumptions: list[str]


class FeasibilityHints(BaseModel):
    based_on: str
    sea_options: list[SeaHint]
    earliest_feasible: SeaHint | None
    air: AirHint
    warnings: list[str]
    season: SeasonCheck | None = Field(default=None, exclude_if=lambda value: value is None)


class IntakePreview(BaseModel):
    lot: Lot
    event: AddLotEvent
    resolved: Resolved
    defaults_applied: list[str]
    hints: FeasibilityHints
    next_step: str
    registered: RegisteredLot | None = None
    savings_estimate: SavingsEstimate | None = None
    expected_loss: ExpectedLoss | None = None
