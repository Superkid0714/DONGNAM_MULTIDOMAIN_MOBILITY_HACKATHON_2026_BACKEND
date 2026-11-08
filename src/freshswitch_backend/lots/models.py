from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from freshswitch_backend.domain.models import Lot

LotStatus = Literal["registered", "planned", "shipped", "cancelled"]


class RegisteredLot(BaseModel):
    id: str
    scenario_id: str
    shipper_id: str
    product_id: str
    pallets: int
    ready_date: date
    destination: str
    sell_by_date: date
    sell_window: Literal["normal", "short"] = "normal"
    packhouse: str | None = None
    source: str
    status: LotStatus
    created_at: datetime
    updated_at: datetime

    def to_lot(self) -> Lot:
        return Lot(
            id=self.id,
            shipper_id=self.shipper_id,
            product_id=self.product_id,
            pallets=self.pallets,
            ready_date=self.ready_date,
            destination=self.destination,
            sell_by_date=self.sell_by_date,
            sell_window=self.sell_window,
            source=self.source,
        )


class LotPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str | None = Field(default=None, min_length=1)
    shipper: str | None = Field(default=None, min_length=1)
    destination: str | None = Field(default=None, min_length=1)
    pallets: int | None = None
    ready_date: date | None = None
    sell_by_date: date | None = None
    sell_window: Literal["normal", "short"] | None = None
    status: LotStatus | None = None


class LotFilter(BaseModel):
    scenario_id: str | None = None
    shipper: str | None = None
    product: str | None = None
    destination: str | None = None
    status: LotStatus | None = None
    ready_from: date | None = None
    ready_to: date | None = None
    limit: int = 200
    offset: int = 0
