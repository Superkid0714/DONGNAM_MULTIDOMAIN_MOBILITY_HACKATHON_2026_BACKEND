from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from freshswitch_backend.replan.models import AlternativeId, CancellationCharge, Event

OrderStatus = Literal[
    "proposed",
    "shipper_confirmed",
    "forwarder_confirmed",
    "executed",
    "rejected",
    "expired",
]
ConfirmationReason = Literal[
    "cost_increase", "pull_forward", "mode_change", "voyage_change", "cancellation_fee"
]
DecisionStatus = Literal["pending", "accepted", "rejected"]
AutomaticKind = Literal["slot_reassignment", "truck_dispatch", "settlement_recompute"]
RequestKind = Literal[
    "booking_amendment",
    "booking_cancel",
    "booking_new",
    "reefer_plug",
    "cy_entry",
    "air_booking",
    "air_cancel",
]
CounterpartyRole = Literal["carrier", "terminal", "airline"]
RequestStatus = Literal["pending", "accepted", "declined"]
Actor = Literal["shipper", "forwarder", "system"]

TERMINAL_STATUSES: tuple[OrderStatus, ...] = ("executed", "rejected", "expired")


class ShipperConfirmation(BaseModel):
    shipper_id: str
    shipper_name: str
    lot_ids: list[str]
    reasons: list[ConfirmationReason]
    reason_text: list[str]
    cost_before: int
    cost_after: int
    cost_delta: int
    cancellation_adjustment: int = 0
    net_cost_delta: int = 0
    pull_forward_days: int = 0
    deadline: datetime
    status: DecisionStatus = "pending"
    note: str | None = None
    decided_at: datetime | None = None


class AutomaticAction(BaseModel):
    kind: AutomaticKind
    description: str
    lot_ids: list[str] = Field(default_factory=list)
    container_id: str | None = None
    truck_id: str | None = None
    done: bool = False


class ExternalRequest(BaseModel):
    request_id: str
    kind: RequestKind
    counterparty_role: CounterpartyRole
    counterparty: str
    channel: str
    deadline: datetime
    voyage_id: str | None = None
    container_id: str | None = None
    lot_ids: list[str] = Field(default_factory=list)
    pallets_before: int = 0
    pallets_after: int = 0
    text: str
    status: RequestStatus = "pending"
    note: str | None = None
    answered_at: datetime | None = None


class OrderLogEntry(BaseModel):
    at: datetime
    actor: Actor
    action: str
    status_after: OrderStatus
    shipper_id: str | None = None
    note: str | None = None


class Deadlines(BaseModel):
    shipper: datetime
    forwarder: datetime
    dispatch: datetime
    cy_cutoff: datetime
    basis: str


class ExecutionOrder(BaseModel):
    order_id: str
    plan_id: str
    replan_id: str
    alternative_id: AlternativeId
    event: Event
    base_version: int
    target_version: int
    rollback_version: int
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    deadlines: Deadlines
    shipper_confirmations: list[ShipperConfirmation]
    notified_shipper_ids: list[str]
    automatic: list[AutomaticAction]
    requests: list[ExternalRequest]
    cancellation: CancellationCharge | None = None
    expired_reason: str | None = None
    rolled_back_to_version: int | None = None
    log: list[OrderLogEntry]


class ConfirmRequest(BaseModel):
    actor: Literal["shipper", "forwarder"]
    shipper_id: str | None = None
    accept: bool
    note: str | None = None


class RequestResultBody(BaseModel):
    accepted: bool
    note: str | None = None
