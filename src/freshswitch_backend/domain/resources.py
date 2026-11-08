from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from freshswitch_backend.domain.inputs import StorableStr, storable
from freshswitch_backend.domain.models import KstDatetime, Port

ResourceName = Annotated[StorableStr, Field(min_length=1, max_length=200)]
PortId = Annotated[StorableStr, Field(min_length=1, max_length=100)]
ResourceTemperature = Annotated[float, Field(ge=-100, le=100, allow_inf_nan=False)]
ContainerKind = Literal["40RH-CA", "40RH", "20RF"]
ResourceCapacity = Annotated[int, Field(ge=1, le=40)]
TransitDays = Annotated[float, Field(gt=0, le=60, allow_inf_nan=False)]
RESOURCE_NOTICE = "앱 안의 가용 공간 배정입니다. 실제 선사 예약·운송은 포워더가 별도로 진행합니다."


class ResourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ResourceName
    kind: ContainerKind
    capacity_pallets: ResourceCapacity
    temp_min_c: ResourceTemperature
    temp_max_c: ResourceTemperature
    vessel_name: ResourceName
    terminal: ResourceName
    origin: PortId
    destination: PortId
    etd: KstDatetime
    cy_cutoff: KstDatetime
    transit_days: TransitDays
    enabled: bool = True

    @field_validator("name", "vessel_name", "terminal", "origin", "destination", mode="before")
    @classmethod
    def clean_names(cls, value: object) -> object:
        value = storable(value)
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def valid_conditions(self) -> Self:
        if self.temp_min_c > self.temp_max_c:
            raise ValueError("최저 보관 온도는 최고 보관 온도 이하여야 합니다")
        if self.cy_cutoff > self.etd:
            raise ValueError("반입 마감은 출항 시각 이하여야 합니다")
        if self.origin == self.destination:
            raise ValueError("출발항과 도착항은 달라야 합니다")
        return self


class ResourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ResourceName | None = None
    kind: ContainerKind | None = None
    capacity_pallets: ResourceCapacity | None = None
    temp_min_c: ResourceTemperature | None = None
    temp_max_c: ResourceTemperature | None = None
    vessel_name: ResourceName | None = None
    terminal: ResourceName | None = None
    origin: PortId | None = None
    destination: PortId | None = None
    etd: KstDatetime | None = None
    cy_cutoff: KstDatetime | None = None
    transit_days: TransitDays | None = None
    enabled: bool | None = None

    @field_validator("name", "vessel_name", "terminal", "origin", "destination", mode="before")
    @classmethod
    def clean_names(cls, value: object) -> object:
        value = storable(value)
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def present_fields(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("수정할 컨테이너 정보를 입력하세요")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("컨테이너 정보는 null로 비울 수 없습니다")
        return self


class ResourceOut(ResourceCreate):
    id: str
    forwarder_org_id: str
    forwarder_org_name: str
    allocated_plan_id: str | None
    allocated_pallets: int
    available_pallets: int
    status: Literal["available", "allocated", "disabled"]
    source: str


class ResourceOptions(BaseModel):
    ports: list[Port]
    container_kinds: list[str]
    allocation_notice: str = RESOURCE_NOTICE
