from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from freshswitch_backend.domain.models import Lot
from freshswitch_backend.lots.models import RegisteredLot
from freshswitch_backend.replan.models import AddLotEvent


class IntakeRequest(BaseModel):
    product: str = Field(min_length=1)
    pallets: int
    ready_date: date
    shipper: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    sell_by_date: date | None = None
    sell_window: Literal["normal", "short"] = "normal"
    scenario_id: str = "default"
    plan_id: str | None = None
    lot_id: str | None = Field(default=None, max_length=120)
    save: bool = False


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


class FeasibilityHints(BaseModel):
    based_on: str
    sea_options: list[SeaHint]
    earliest_feasible: SeaHint | None
    air: AirHint
    warnings: list[str]


class IntakePreview(BaseModel):
    lot: Lot
    event: AddLotEvent
    resolved: Resolved
    defaults_applied: list[str]
    hints: FeasibilityHints
    next_step: str
    registered: RegisteredLot | None = None
