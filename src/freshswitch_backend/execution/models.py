from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from freshswitch_backend.domain.inputs import Note, StorableStr
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
DocumentAction = Literal["si_vgm_correction", "booking_change"]
RequestStatus = Literal["pending", "accepted", "declined"]
Actor = Literal["shipper", "forwarder", "system"]
CandidateSource = Literal["shipper_rejected", "request_declined"]

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


class RequestBasis(BaseModel):
    head: str
    container_kind: str
    si_cutoff: datetime
    vgm_cutoff: datetime
    cy_cutoff: datetime
    net_kg_before: float
    net_kg_after: float
    setting: str


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
    lot_pallets_before: dict[str, int] = Field(default_factory=dict)
    lot_pallets_after: dict[str, int] = Field(default_factory=dict)
    text: str
    document_action: DocumentAction | None = None
    document_action_label: str | None = None
    plain_text: str | None = None
    saas_basis: RequestBasis | None = Field(default=None, exclude_if=lambda value: value is None)
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
    cancellation_charges: list[CancellationCharge] = Field(default_factory=list)
    expired_reason: str | None = None
    rolled_back_to_version: int | None = None
    log: list[OrderLogEntry]


class ConfirmRequest(BaseModel):
    actor: Literal["shipper", "forwarder"]
    shipper_id: StorableStr | None = None
    accept: bool
    note: Note | None = None


class RequestResultBody(BaseModel):
    accepted: bool
    note: Note | None = None


class ReplanCandidate(BaseModel):
    id: str
    source: CandidateSource
    created_at: datetime
    lot_ids: list[str] = Field(default_factory=list)
    voyage_id: str | None = None
    container_id: str | None = None
    shipper_id: str | None = None
    order_id: str | None = None
    request_id: str | None = None
    request_kind: RequestKind | None = None
    counterparty: str | None = None
    note: str | None = None
    events: list[Event] = Field(default_factory=list)
