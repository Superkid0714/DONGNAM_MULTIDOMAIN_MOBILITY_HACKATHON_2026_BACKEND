from typing import Literal

from pydantic import BaseModel, Field


class ContainerFill(BaseModel):
    container_id: str
    before: int
    after: int
    capacity: int


class AlternativeInput(BaseModel):
    id: str
    label: str
    feasible: bool
    reason: str | None = None
    total_cost_delta_krw: int
    container_fill: list[ContainerFill] = Field(default_factory=list)


class ShipperImpact(BaseModel):
    shipper_id: str
    shipper_name: str
    cost_before_krw: int
    cost_after_krw: int
    delta_krw: int
    moved: str | None = None
    pull_forward_days: int = 0
    cancellation_adjustment_krw: int = 0


class ExplainContext(BaseModel):
    event_type: str
    event_summary: str
    recommended_alternative_id: str | None
    alternatives: list[AlternativeInput]
    shippers: list[ShipperImpact]
    warnings: list[str] = Field(default_factory=list)


class ShipperMessage(BaseModel):
    shipper_id: str
    message: str


class Explanation(BaseModel):
    operator_summary: str
    shipper_messages: list[ShipperMessage]
    generated_by: Literal["llm", "template"]
    model: str | None = None
    fallback_reason: Literal["unavailable", "timeout", "validation", "provider_error"] | None = None
