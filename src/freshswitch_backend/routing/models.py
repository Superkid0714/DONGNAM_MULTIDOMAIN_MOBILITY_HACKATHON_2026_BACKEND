from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, Field


class Depot(BaseModel):
    id: str
    lat: float
    lon: float


class GateWindow(BaseModel):
    start: time
    end: time
    label: str = ""
    source: str = "가정"

    def text(self) -> str:
        span = f"{self.start:%H:%M}~{self.end:%H:%M}"
        return f"{span} {self.label}" if self.label else span


GateStatus = Literal["off_peak", "avoided", "peak"]
GateMode = Literal["none", "avoid", "fallback"]


class PickupStop(BaseModel):
    id: str
    packhouse_id: str
    lat: float
    lon: float
    pallets: int = Field(gt=0)
    ready_at: datetime
    lot_ids: list[str] = Field(default_factory=list)
    cargo_group: str | None = None


class PickupTruck(BaseModel):
    id: str
    capacity_pallets: int = Field(gt=0)
    available_from: datetime | None = None


class PickupRequest(BaseModel):
    depot: Depot
    stops: list[PickupStop]
    trucks: list[PickupTruck]
    deadline: datetime
    trucks_available_from: datetime | None = None
    service_minutes: int = Field(default=30, ge=0)
    speed_kmh: float = Field(default=60.0, gt=0)
    road_factor: float = Field(default=1.3, gt=0)
    cost_per_km: float = Field(default=2500.0, ge=0)
    truck_fixed_cost: float = Field(default=0.0, ge=0)
    max_trips_per_truck: int = Field(default=2, ge=1)
    incompatible_groups: list[tuple[str, str]] = Field(default_factory=list)
    gate_windows: list[GateWindow] = Field(default_factory=list)


class StopVisit(BaseModel):
    stop_id: str
    packhouse_id: str
    lot_ids: list[str]
    sequence: int
    pallets: int
    load_after: int
    km_from_prev: float
    ready_at: datetime
    eta: datetime
    latest_arrival: datetime
    cargo_group: str | None = None


class TruckRoute(BaseModel):
    truck_id: str
    trip: int = 1
    cargo_group: str | None = None
    capacity_pallets: int
    load_pallets: int
    stops: list[StopVisit]
    km: float
    cost: float
    earliest_departure: datetime
    latest_departure: datetime
    port_eta: datetime
    slack_minutes: int
    gate_status: GateStatus = "off_peak"
    gate_window: str | None = None
    gate_delay_minutes: int = 0
    gate_slot_start: datetime | None = None
    gate_slot_end: datetime | None = None


class PickupPlan(BaseModel):
    status: str
    deadline: datetime
    routes: list[TruckRoute]
    total_km: float
    total_cost: float
    dropped_stop_ids: list[str]
    solve_ms: int
    assumptions: list[str]
    gate_mode: GateMode = "none"
    gate_windows: list[GateWindow] = Field(default_factory=list)
