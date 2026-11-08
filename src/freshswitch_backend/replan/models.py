from typing import Annotated, Literal

from pydantic import BaseModel, Field

from freshswitch_backend.domain.models import KstDatetime, Lot, Plan, Scenario
from freshswitch_backend.explain.models import Explanation
from freshswitch_backend.planning.service import VoyagePickup


class CancelEvent(BaseModel):
    type: Literal["cancel"] = "cancel"
    lot_id: str


class ReduceEvent(BaseModel):
    type: Literal["reduce"] = "reduce"
    lot_id: str
    new_pallets: int = Field(gt=0)


class AirSwitchEvent(BaseModel):
    type: Literal["air_switch"] = "air_switch"
    lot_id: str


class DelayEvent(BaseModel):
    type: Literal["delay"] = "delay"
    voyage_id: str
    new_etd: KstDatetime
    new_cy_cutoff: KstDatetime | None = None
    source: Literal["manual", "해수부", "예측"] = "manual"


class AddLotEvent(BaseModel):
    type: Literal["add_lot"] = "add_lot"
    lot: Lot


Event = Annotated[
    CancelEvent | ReduceEvent | AirSwitchEvent | DelayEvent | AddLotEvent,
    Field(discriminator="type"),
]

AlternativeId = Literal["pull_forward", "next_voyage", "sail_as_is"]


class ReplanError(ValueError):
    pass


class LotMove(BaseModel):
    lot_id: str
    shipper_id: str
    pallets: int
    from_location: str | None
    to_location: str | None
    pull_forward_days: int = 0


class ShipperDelta(BaseModel):
    shipper_id: str
    cost_before: int
    cost_after: int
    cost_delta: int
    container_share_before: int
    container_share_after: int
    share_per_pallet_before: int
    share_per_pallet_after: int
    share_per_pallet_delta_pct: float | None
    cancellation_adjustment: int = 0


class ContainerFill(BaseModel):
    container_id: str
    voyage_id: str
    capacity_pallets: int
    pallets_before: int
    pallets_after: int
    utilization_before: float
    utilization_after: float


class CancellationTransfer(BaseModel):
    shipper_id: str
    pallets: int
    share_increase: int
    amount: int


class CancellationCharge(BaseModel):
    payer_shipper_id: str
    lot_id: str
    container_id: str
    voyage_id: str
    cancelled_pallets: int
    refilled_pallets: int
    dead_pallets: int
    container_pallets_before: int
    container_pallets_after: int
    container_cost: int
    share_per_pallet_before: int
    share_per_pallet_after: int
    fee_rate: float
    fee: int
    transfers: list[CancellationTransfer]
    rule: str
    source: str


class PlanDiff(BaseModel):
    moved_lots: list[LotMove]
    shippers: list[ShipperDelta]
    containers: list[ContainerFill]
    cancellation: CancellationCharge | None = None


class Alternative(BaseModel):
    id: AlternativeId
    label: str
    summary: str
    feasible: bool
    infeasible_reasons: list[str]
    total_cost: int
    cost_delta: int
    plan: Plan
    routes: list[VoyagePickup] = Field(default_factory=list)
    diff: PlanDiff
    solve_ms: int


class ReplanResult(BaseModel):
    event: Event
    scope: Literal["container", "voyage"]
    free_lot_ids: list[str]
    fixed_lot_ids: list[str]
    alternatives: list[Alternative]
    recommended_id: AlternativeId | None
    diff: PlanDiff | None
    solve_ms: int
    explanation: Explanation | None = None
    scenario_after: Scenario | None = Field(default=None, exclude=True)
