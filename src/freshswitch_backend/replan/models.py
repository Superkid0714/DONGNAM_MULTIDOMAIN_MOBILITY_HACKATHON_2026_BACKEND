from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from freshswitch_backend.domain.models import KstDatetime, Lot, Plan, Scenario
from freshswitch_backend.explain.models import Explanation
from freshswitch_backend.planning.service import VoyagePickup


def _unset(value: object) -> bool:
    return value is None


def _empty(value: object) -> bool:
    return not value


class CancelEvent(BaseModel):
    type: Literal["cancel"] = "cancel"
    lot_id: str
    occurred_at: KstDatetime | None = Field(default=None, exclude_if=_unset)


class ReduceEvent(BaseModel):
    type: Literal["reduce"] = "reduce"
    lot_id: str
    new_pallets: int = Field(gt=0)
    occurred_at: KstDatetime | None = Field(default=None, exclude_if=_unset)


class IncreaseEvent(ReduceEvent):
    type: Literal["increase"] = "increase"


class AirSwitchEvent(BaseModel):
    type: Literal["air_switch"] = "air_switch"
    lot_id: str
    occurred_at: KstDatetime | None = Field(default=None, exclude_if=_unset)


class VoyageDelay(BaseModel):
    voyage_id: str
    new_etd: KstDatetime
    new_cy_cutoff: KstDatetime | None = None
    delay_p90_h: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    delay_forecast_source: Literal["model", "stat", "provided"] | None = None
    truck_departure_adjustment_h: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class DelayEvent(BaseModel):
    type: Literal["delay"] = "delay"
    voyage_id: str
    new_etd: KstDatetime
    new_cy_cutoff: KstDatetime | None = None
    delay_p90_h: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    delay_forecast_source: Literal["model", "stat", "provided"] | None = None
    truck_departure_adjustment_h: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    additional_voyages: list[VoyageDelay] = Field(default_factory=list, exclude_if=_empty)
    source: Literal["manual", "해수부", "예측"] = "manual"
    occurred_at: KstDatetime | None = Field(default=None, exclude_if=_unset)

    @model_validator(mode="after")
    def _distinct_voyages(self) -> "DelayEvent":
        ids = self.voyage_ids
        if len(set(ids)) != len(ids):
            raise ValueError("같은 항차를 한 이벤트에 두 번 넣을 수 없음")
        return self

    @property
    def voyage_ids(self) -> list[str]:
        return [d.voyage_id for d in self.delays()]

    def delays(self) -> list[VoyageDelay]:
        first = VoyageDelay(
            voyage_id=self.voyage_id,
            new_etd=self.new_etd,
            new_cy_cutoff=self.new_cy_cutoff,
            delay_p90_h=self.delay_p90_h,
            delay_forecast_source=self.delay_forecast_source,
            truck_departure_adjustment_h=self.truck_departure_adjustment_h,
        )
        return [first, *self.additional_voyages]


class AddLotEvent(BaseModel):
    type: Literal["add_lot"] = "add_lot"
    lot: Lot
    occurred_at: KstDatetime | None = Field(default=None, exclude_if=_unset)


Event = Annotated[
    CancelEvent | ReduceEvent | IncreaseEvent | AirSwitchEvent | DelayEvent | AddLotEvent,
    Field(discriminator="type"),
]

AlternativeId = Literal["pull_forward", "next_voyage", "sail_as_is"]
FixedReason = Literal["truck_departed", "dispatch_executed"]
RouteChangeKind = Literal["added", "removed", "changed"]


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
    recipient_kind: Literal["shippers", "consolidator", "mixed"] | None = None
    consolidator: Literal["platform", "forwarder"] = "platform"
    consolidator_amount: int = 0
    rule: str
    source: str


class RouteChange(BaseModel):
    voyage_id: str
    truck_id: str
    change: RouteChangeKind
    packhouses_before: list[str]
    packhouses_after: list[str]
    packhouses_added: list[str]
    packhouses_removed: list[str]
    lot_ids_before: list[str]
    lot_ids_after: list[str]
    trips_before: int
    trips_after: int
    departure_before: KstDatetime | None
    departure_after: KstDatetime | None
    km_before: float
    km_after: float
    cost_before: int
    cost_after: int


class PlanDiff(BaseModel):
    moved_lots: list[LotMove]
    shippers: list[ShipperDelta]
    containers: list[ContainerFill]
    cancellation: CancellationCharge | None = None
    route_changes: list[RouteChange] = Field(default_factory=list)


class ExecutedDispatch(BaseModel):
    voyage_id: str | None = None
    truck_id: str
    lot_ids: list[str]


class FixedLot(BaseModel):
    lot_id: str
    reason_code: FixedReason
    reason: str
    voyage_id: str
    truck_id: str
    trip: int
    packhouse_id: str
    departure: KstDatetime


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
    warnings: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)


class ReplanResult(BaseModel):
    event: Event
    scope: Literal["container", "voyage"]
    free_lot_ids: list[str]
    fixed_lot_ids: list[str]
    fixed_reasons: list[FixedLot] = Field(default_factory=list)
    occurred_at: KstDatetime | None = Field(default=None, exclude_if=_unset)
    alternatives: list[Alternative]
    recommended_id: AlternativeId | None
    diff: PlanDiff | None
    solve_ms: int
    warnings: list[str] = Field(default_factory=list)
    explanation: Explanation | None = None
    scenario_after: Scenario | None = Field(default=None, exclude=True)
