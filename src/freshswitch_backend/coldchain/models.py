from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from freshswitch_backend.domain.ids import ID_MAX_LENGTH, EntityId
from freshswitch_backend.domain.inputs import FiniteFloat, StorableStr
from freshswitch_backend.domain.models import KstDatetime, Quantity
from freshswitch_backend.routing.models import GateStatus

TwinStatus = Literal["no_data", "normal", "warning", "critical"]
AlertLevel = Literal["info", "warning", "critical"]
Stage = Literal["trucking", "gate", "terminal", "vessel"]
ActionKind = Literal[
    "terminal_priority_plug",
    "genset_dispatch",
    "entry_time_adjust",
    "cold_storage_booking",
    "insurance_evidence",
]
AlertKind = Literal["power_cut", "power_restored", "status_change", "action"]
CounterpartyRole = Literal["terminal", "trucker", "destination_warehouse", "insurer"]
ActionStatus = Literal["open", "accepted", "done"]
HandlerRole = Literal["platform", "forwarder"]
ResidualRiskKind = Literal["heat_accumulation", "temperature_swing", "door_open", "sensor_gap"]
RiskLevel = Literal["info", "warning"]
PowerStage = Literal["trucking", "gate", "plug"]
HandoverOutageKind = Literal["none", "planned", "planned_warning", "abnormal"]
READING_MAX_AGE = timedelta(days=30)
READING_MAX_AHEAD = timedelta(minutes=10)
SANDBOX_READING_MAX_AHEAD = timedelta(days=60)


def _span(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes % (24 * 60) == 0:
        return f"{minutes // (24 * 60)}일"
    if minutes % 60 == 0:
        return f"{minutes // 60}시간"
    return f"{minutes}분"


def reading_window_error(ts: datetime | None, now: datetime, sandbox: bool = False) -> str | None:
    if ts is None:
        return None
    ahead = SANDBOX_READING_MAX_AHEAD if sandbox else READING_MAX_AHEAD
    if ts > now + ahead:
        return f"측정 시각이 서버 시각보다 {_span(ahead)} 넘게 앞섭니다: {ts.isoformat()}"
    if ts < now - READING_MAX_AGE:
        return (
            f"측정 시각이 서버 시각보다 {_span(READING_MAX_AGE)} 넘게 지났습니다: {ts.isoformat()}"
        )
    return None


class ReadingIn(BaseModel):
    device_id: EntityId = Field(min_length=1, max_length=ID_MAX_LENGTH)
    container_id: StorableStr | None = Field(default=None, min_length=1, max_length=100)
    plan_id: StorableStr | None = None
    ts: KstDatetime | None = None
    temp_c: FiniteFloat = Field(ge=-40, le=80)
    power_on: bool
    humidity_pct: FiniteFloat | None = Field(default=None, ge=0, le=100)
    door_open: bool | None = None
    stage: Stage | None = None


class HandoverReadingIn(ReadingIn):
    handover_step: int


class Reading(BaseModel):
    device_id: str
    container_id: str
    ts: KstDatetime
    temp_c: float
    power_on: bool
    humidity_pct: float | None = None
    door_open: bool | None = None
    handover_step: int | None = None


class BindingRequest(BaseModel):
    device_id: EntityId = Field(min_length=1, max_length=ID_MAX_LENGTH)
    container_id: StorableStr = Field(min_length=1, max_length=100)
    plan_id: StorableStr | None = None
    stage: Stage | None = None


class Binding(BaseModel):
    device_id: str
    container_id: str
    plan_id: str | None = None
    stage: Stage | None = None
    bound_at: datetime


class RequestDraft(BaseModel):
    counterparty_role: CounterpartyRole
    counterparty: str
    channel: str
    text: str


class ActionHandler(BaseModel):
    org_type: HandlerRole
    org_id: str | None = None
    user_id: str | None = None


class ColdchainAction(BaseModel):
    key: str
    kind: ActionKind
    severity: AlertLevel
    title: str
    description: str
    container_id: str
    triggered_at: datetime
    lot_ids: list[str] = Field(default_factory=list)
    request: RequestDraft | None = None
    created_at: datetime | None = None
    status: ActionStatus = "open"
    note: str | None = None
    accepted_at: datetime | None = None
    accepted_by: ActionHandler | None = None
    done_at: datetime | None = None
    done_by: ActionHandler | None = None


class ActionUpdateBody(BaseModel):
    note: StorableStr | None = Field(default=None, max_length=500)


class ResidualRisk(BaseModel):
    kind: ResidualRiskKind
    level: RiskLevel
    title: str
    value: float
    unit: str
    threshold: Quantity
    message: str


class Alert(BaseModel):
    seq: int = 0
    key: str
    container_id: str
    plan_id: str | None = None
    level: AlertLevel
    kind: AlertKind
    title: str
    message: str
    at: datetime
    created_at: datetime | None = None
    action: ColdchainAction | None = None


class ProductBand(BaseModel):
    product_id: str
    name: str
    temp_min_c: float
    temp_max_c: float


class OutageSummary(BaseModel):
    cumulative_minutes: float
    current_continuous_minutes: float
    longest_continuous_minutes: float
    episodes: int
    reference_minutes: float
    insurance_threshold_minutes: float
    insurance_eligible: bool


class ExcursionSummary(BaseModel):
    band_min_c: float
    band_max_c: float
    degree_minutes: float
    minutes_above_band: float
    max_temp_c: float | None


class LotLoss(BaseModel):
    lot_id: str
    shipper_id: str
    product_id: str
    pallets: int
    lot_value: int
    planned_loss_rate: float
    sensor_loss_rate: float
    predicted_loss_rate: float
    planned_loss_krw: int
    predicted_loss_krw: int
    delta_krw: int


class ShipperLoss(BaseModel):
    shipper_id: str
    shipper_name: str
    lot_ids: list[str]
    lot_value: int
    planned_loss_krw: int
    predicted_loss_krw: int
    delta_krw: int


class LossSummary(BaseModel):
    exposure_index: float
    sensor_loss_rate: float
    no_outage_loss_rate: float
    reference_outage_loss_rate: float
    planned_loss_rate: float | None = None
    predicted_loss_rate: float | None = None
    cargo_value: int | None = None
    planned_loss_krw: int | None = None
    predicted_loss_krw: int | None = None
    delta_krw: int | None = None
    lots: list[LotLoss] = Field(default_factory=list)
    shippers: list[ShipperLoss] = Field(default_factory=list)
    rule: str


class CurrentReading(BaseModel):
    ts: datetime
    device_id: str
    temp_c: float
    power_on: bool
    humidity_pct: float | None = None
    door_open: bool | None = None
    excursion_c: float


class TimelinePoint(BaseModel):
    ts: datetime
    temp_c: float
    power_on: bool
    humidity_pct: float | None = None
    door_open: bool | None = None
    status: TwinStatus
    cumulative_outage_minutes: float
    degree_minutes: float
    sensor_loss_rate: float
    predicted_loss_rate: float | None = None
    handover_step: int | None = None
    handover_kind: HandoverOutageKind | None = None
    handover_excess_minutes: float | None = None


class HandoverStep(BaseModel):
    step: int
    title: str
    power: str
    system: str


class HandoverState(BaseModel):
    current_step: int
    outage_kind: HandoverOutageKind
    continuous_off_minutes: float
    cumulative_off_minutes: float
    excess_minutes: float
    warning: Quantity
    allowance: Quantity
    steps: list[HandoverStep]
    rule: str


class TwinSummary(BaseModel):
    container_id: str
    plan_id: str | None
    status: TwinStatus
    reading_count: int
    last_ts: datetime | None
    temp_c: float | None
    power_on: bool | None
    cumulative_outage_minutes: float
    sensor_loss_rate: float
    predicted_loss_rate: float | None
    delta_krw: int | None
    open_actions: int


class TwinState(BaseModel):
    container_id: str
    plan_id: str | None
    scenario_id: str | None
    voyage_id: str | None
    stage: Stage
    device_ids: list[str]
    products: list[ProductBand]
    status: TwinStatus
    current: CurrentReading | None
    outage: OutageSummary
    excursion: ExcursionSummary
    loss: LossSummary
    reading_count: int
    timeline: list[TimelinePoint]
    actions: list[ColdchainAction]
    alerts: list[Alert]
    params: dict[str, Quantity]
    has_data: bool = True
    residual_risks: list[ResidualRisk] = Field(default_factory=list)
    handover: HandoverState | None = None


class IngestRejected(BaseModel):
    index: int
    device_id: str
    reason: str


class IngestResult(BaseModel):
    accepted: int
    duplicates: int
    rejected: list[IngestRejected]
    containers: list[TwinSummary]
    new_alerts: list[Alert]


class AlertFeed(BaseModel):
    alerts: list[Alert]
    next_since: int


SimulationPreset = Literal["power_cut_restore", "short_outage", "normal", "sea_to_land"]


class SimulationRequest(BaseModel):
    preset: SimulationPreset = "power_cut_restore"
    container_id: str = "CA-01"
    plan_id: str | None = None
    device_id: EntityId = Field(default="sim-01", min_length=1, max_length=ID_MAX_LENGTH)
    stage: Stage = "terminal"
    start: KstDatetime | None = None
    interval_seconds: float = Field(default=0, ge=0, le=10)
    reset: bool = True


class PresetInfo(BaseModel):
    id: SimulationPreset
    title: str
    description: str
    step_minutes: int
    readings: int
    outage_minutes: int


class SimulationRun(BaseModel):
    run_id: str
    preset: SimulationPreset
    container_id: str
    plan_id: str | None
    device_id: str
    mode: Literal["instant", "stream"]
    total: int
    sent: int
    interval_seconds: float
    done: bool
    start: datetime
    end: datetime
    state: TwinSummary | None = None
    error: str | None = None


class SettlementPreviewLot(BaseModel):
    lot_id: str
    shipper_id: str
    planned_loss_rate: float
    predicted_loss_rate: float
    excess_loss_value: int
    refund_before: int
    refund_after: int
    refund_change: int
    capped: bool


class SettlementPreview(BaseModel):
    plan_id: str
    container_id: str
    lots: list[SettlementPreviewLot]
    total_refund_change: int
    rule: str
    note: str


class PowerSegment(BaseModel):
    stage: PowerStage
    start: datetime | None
    end: datetime | None
    minutes: float
    powered: bool
    basis: str


class LotPowerPlan(BaseModel):
    lot_id: str
    shipper_id: str
    container_id: str
    voyage_id: str
    truck_id: str | None
    trip: int | None
    gate_status: GateStatus | None
    segments: list[PowerSegment]
    unpowered_minutes: float
    threshold_minutes: float
    exceeds: bool


class GensetCandidate(BaseModel):
    key: str
    equipment: str
    voyage_id: str
    container_ids: list[str]
    truck_id: str | None
    trip: int | None
    lot_ids: list[str]
    window_start: datetime | None
    window_end: datetime | None
    unpowered_minutes: float
    threshold_minutes: float
    reason: str
    gate_peak: bool = False


class PowerPlan(BaseModel):
    plan_id: str
    lots: list[LotPowerPlan]
    candidates: list[GensetCandidate]
    params: dict[str, Quantity]
    rule: str
