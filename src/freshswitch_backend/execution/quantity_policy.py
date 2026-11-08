from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuantityPolicy(BaseModel):
    version: Literal["slot-v1"] = "slot-v1"
    cutoff: datetime
    departure: datetime
    hold_days: int = Field(default=7, ge=1, le=365)
    confirm_days: int = Field(default=3, ge=0, le=365)
    late_days: int = Field(default=2, ge=0, le=365)
    free_adjust: int = Field(default=1, ge=0, le=1000)
    slot_rate_krw: int = Field(default=166000, ge=0, le=1000000000)
    rate_mid: Decimal = Field(default=Decimal("0.3"), ge=0, le=1)
    rate_late: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    offer_timeout_minutes: int = Field(default=30, ge=1, le=1440)
    detour_max_minutes: int = Field(default=30, ge=0, le=1440)
    pilot: bool = False

    @model_validator(mode="after")
    def valid_window(self):
        if self.cutoff.tzinfo is None or self.departure.tzinfo is None:
            raise ValueError("시각에는 시간대가 필요합니다")
        if not self.hold_days >= self.confirm_days >= self.late_days:
            raise ValueError("예약·확정·임박 기간 순서가 올바르지 않습니다")
        if self.departure < self.cutoff:
            raise ValueError("출항 시각은 마감 이후여야 합니다")
        if self.rate_late < self.rate_mid:
            raise ValueError("임박 요율은 확정 요율 이상이어야 합니다")
        return self

    @property
    def holds_at(self) -> datetime:
        return self.cutoff - timedelta(days=self.hold_days)

    @property
    def confirms_at(self) -> datetime:
        return self.cutoff - timedelta(days=self.confirm_days)

    @property
    def late_at(self) -> datetime:
        return self.cutoff - timedelta(days=self.late_days)

    def rate_at(self, now: datetime) -> Decimal:
        if now < self.confirms_at:
            return Decimal(0)
        return self.rate_mid if now < self.late_at else self.rate_late


class ReductionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_id: str
    at: datetime
    reservation_id: str
    container_id: str
    before_quantity: int = Field(ge=0)
    after_quantity: int = Field(ge=0)
    reduced_quantity: int = Field(ge=0)
    charged_quantity: int = Field(ge=0)
    rate: Decimal = Field(ge=0, le=1)
    slot_rate_krw: int = Field(ge=0)
    fee_krw: int = Field(ge=0)
    exemption: Literal["pilot", "ai_air"] | None = None


class RefillOffset(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_id: str
    reduction_event_id: str
    at: datetime
    refill_id: str
    quantity: int = Field(gt=0)
    charged_quantity: int = Field(ge=0)


class QuantityReservation(BaseModel):
    reservation_id: str
    lot_id: str
    shipper_id: str
    shipper_org_id: str
    container_id: str
    current_quantity: int = Field(ge=0)
    confirmed_quantity: int | None = Field(default=None, ge=0)
    confirmed_at: datetime | None = None
    confirmed_by: Literal["shipper", "system"] | None = None
    assessed_quantity: int = Field(default=0, ge=0)
    policy_snapshot: QuantityPolicy | None = None
    events: list[ReductionEvent] = Field(default_factory=list)
    offsets: list[RefillOffset] = Field(default_factory=list)


class QuantityQuote(BaseModel):
    quantity: int = Field(ge=0)
    reduced_quantity: int
    cumulative_reduced_quantity: int
    newly_charged_quantity: int
    free_adjust_remaining: int
    rate: Decimal
    slot_rate_krw: int
    fee_krw: int
    gross_fee_krw: int
    refill_credit_krw: int
    final_fee_krw: int
    exemption: Literal["pilot", "ai_air"] | None = None
    window: Literal["reservation", "confirmed", "late"]


def money(quantity: int, rate: Decimal, slot_rate_krw: int) -> int:
    return int((quantity * rate * slot_rate_krw).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def confirm_quantity(
    reservation: QuantityReservation, policy: QuantityPolicy, now: datetime, actor="system"
) -> QuantityReservation:
    updated = reservation.model_copy(deep=True)
    if updated.confirmed_quantity is None:
        updated.confirmed_quantity = updated.current_quantity
        updated.confirmed_at = now
        updated.confirmed_by = actor
        updated.policy_snapshot = policy.model_copy(deep=True)
    return updated


def amounts(reservation: QuantityReservation) -> tuple[int, int, int]:
    gross = sum(event.fee_krw for event in reservation.events)
    final = 0
    for event in reservation.events:
        offset = sum(
            item.charged_quantity
            for item in reservation.offsets
            if item.reduction_event_id == event.event_id
        )
        if offset > event.charged_quantity:
            raise ValueError("중복 충원 상계입니다")
        final += money(event.charged_quantity - offset, event.rate, event.slot_rate_krw)
    return gross, gross - final, final


def quote_quantity(
    reservation: QuantityReservation,
    policy: QuantityPolicy,
    quantity: int,
    now: datetime,
    *,
    ai_air: bool = False,
) -> QuantityQuote:
    if quantity < 0:
        raise ValueError("수량은 0 이상이어야 합니다")
    active = reservation.policy_snapshot or policy
    if now >= active.cutoff:
        raise ValueError("수량 변경 마감이 지났습니다")
    confirmed = reservation.confirmed_quantity
    if confirmed is None and now >= active.confirms_at:
        confirmed = reservation.current_quantity
    reduced = max(0, reservation.current_quantity - quantity)
    cumulative = max(0, (confirmed if confirmed is not None else quantity) - quantity)
    assessed = max(0, cumulative - active.free_adjust)
    maximum_reduced = max(
        [
            cumulative,
            *[max(0, (confirmed or 0) - event.after_quantity) for event in reservation.events],
        ]
    )
    charged = max(0, assessed - reservation.assessed_quantity) if reduced else 0
    rate = active.rate_at(now)
    exemption = "pilot" if active.pilot else "ai_air" if ai_air else None
    if exemption:
        rate = Decimal(0)
    if now < active.confirms_at:
        charged = 0
    fee = money(charged, rate, active.slot_rate_krw)
    gross, credit, final = amounts(reservation)
    return QuantityQuote(
        quantity=quantity,
        reduced_quantity=reduced,
        cumulative_reduced_quantity=cumulative,
        newly_charged_quantity=charged,
        free_adjust_remaining=max(0, active.free_adjust - maximum_reduced),
        rate=rate,
        slot_rate_krw=active.slot_rate_krw,
        fee_krw=fee,
        gross_fee_krw=gross + fee,
        refill_credit_krw=credit,
        final_fee_krw=final + fee,
        exemption=exemption,
        window="reservation"
        if now < active.confirms_at
        else "confirmed"
        if now < active.late_at
        else "late",
    )


def change_quantity(
    reservation: QuantityReservation,
    policy: QuantityPolicy,
    quantity: int,
    now: datetime,
    event_id: str,
    *,
    ai_air: bool = False,
) -> QuantityReservation:
    if any(event.event_id == event_id for event in reservation.events):
        prior = next(event for event in reservation.events if event.event_id == event_id)
        if prior.after_quantity != quantity:
            raise ValueError("같은 요청 키에 다른 수량을 사용할 수 없습니다")
        return reservation.model_copy(deep=True)
    updated = reservation.model_copy(deep=True)
    active = updated.policy_snapshot or policy
    if now >= active.confirms_at:
        updated = confirm_quantity(updated, active, now)
    quote = quote_quantity(updated, active, quantity, now, ai_air=ai_air)
    if quote.reduced_quantity:
        updated.events.append(
            ReductionEvent(
                event_id=event_id,
                at=now,
                reservation_id=updated.reservation_id,
                container_id=updated.container_id,
                before_quantity=updated.current_quantity,
                after_quantity=quantity,
                reduced_quantity=quote.reduced_quantity,
                charged_quantity=quote.newly_charged_quantity,
                rate=quote.rate,
                slot_rate_krw=quote.slot_rate_krw,
                fee_krw=quote.fee_krw,
                exemption=quote.exemption,
            )
        )
        updated.assessed_quantity += quote.newly_charged_quantity
    updated.current_quantity = quantity
    if now < active.confirms_at and updated.confirmed_quantity is not None:
        updated.confirmed_quantity = quantity
    return updated


def vacancy_quantity(reservation: QuantityReservation, container_id: str | None = None) -> int:
    return sum(
        event.reduced_quantity
        - sum(
            offset.quantity
            for offset in reservation.offsets
            if offset.reduction_event_id == event.event_id
        )
        for event in reservation.events
        if container_id is None or event.container_id == container_id
    )


def allocate_refill(
    reservations: list[QuantityReservation],
    container_id: str,
    quantity: int,
    refill_id: str,
    now: datetime,
) -> list[QuantityReservation]:
    if quantity <= 0:
        raise ValueError("충원 수량은 1 이상이어야 합니다")
    prior = [item for r in reservations for item in r.offsets if item.refill_id == refill_id]
    if prior:
        if sum(item.quantity for item in prior) != quantity:
            raise ValueError("같은 충원 키에 다른 수량을 사용할 수 없습니다")
        return [r.model_copy(deep=True) for r in reservations]
    updated = [r.model_copy(deep=True) for r in reservations]
    if sum(vacancy_quantity(r, container_id) for r in updated) < quantity:
        raise ValueError("실제 빈자리보다 많은 수량을 충원할 수 없습니다")
    candidates = []
    for reservation in updated:
        for event in reservation.events:
            if event.container_id != container_id:
                continue
            offsets = [o for o in reservation.offsets if o.reduction_event_id == event.event_id]
            vacant = event.reduced_quantity - sum(o.quantity for o in offsets)
            charged = event.charged_quantity - sum(o.charged_quantity for o in offsets)
            if vacant:
                candidates.append((reservation, event, vacant, charged))
    candidates.sort(key=lambda item: (-item[1].rate, item[1].at, item[1].event_id))
    remaining = quantity
    allocations: dict[str, tuple[QuantityReservation, ReductionEvent, int, int]] = {}
    for charged_only in (True, False):
        for reservation, event, vacant, charged in candidates:
            if not remaining:
                break
            used = allocations.get(event.event_id, (reservation, event, 0, 0))[2]
            available = charged if charged_only else vacant - used
            take = min(remaining, available)
            if not take:
                continue
            old = allocations.get(event.event_id, (reservation, event, 0, 0))
            allocations[event.event_id] = (
                reservation,
                event,
                old[2] + take,
                old[3] + (take if charged_only else 0),
            )
            remaining -= take
    for reservation, event, used, charged in allocations.values():
        reservation.offsets.append(
            RefillOffset(
                event_id=f"{refill_id}:{event.event_id}",
                reduction_event_id=event.event_id,
                at=now,
                refill_id=refill_id,
                quantity=used,
                charged_quantity=charged,
            )
        )
    return updated


def distribute_credits(total_krw: int, pallets: dict[str, int]) -> dict[str, int]:
    if total_krw < 0 or any(value < 0 for value in pallets.values()):
        raise ValueError("금액과 수량은 음수일 수 없습니다")
    keys = sorted(key for key, value in pallets.items() if value > 0)
    total = sum(pallets[key] for key in keys)
    if not total:
        return {}
    shares = {key: divmod(total_krw * pallets[key], total) for key in keys}
    result = {key: shares[key][0] for key in keys}
    ranked = sorted(keys, key=lambda key: (-shares[key][1], key))
    for key in ranked[: total_krw - sum(result.values())]:
        result[key] += 1
    return result
