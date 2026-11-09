"""Conditional cost bounds, not a promise about unverified market prices."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BoundItem(StrictModel):
    key: str = Field(min_length=1, max_length=100)
    supplier: str = Field(min_length=1, max_length=200)
    basis: Literal["per_container", "per_shipper"]
    grade: Literal["confirmed", "reference", "range", "unknown"]
    minimum: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=4)
    maximum: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=4)
    currency: Literal["KRW", "HKD", "USD"] = "KRW"
    source: str = Field(default="", max_length=2000)
    checked_at: date | None = None
    applicability_confirmed: bool = False
    exclude_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_amount(self):
        if self.grade != "unknown":
            if self.minimum is None or self.maximum is None or self.maximum < self.minimum:
                raise ValueError("알려진 비용은 올바른 하한·상한이 필요합니다")
            if self.grade in {"confirmed", "reference"} and self.minimum != self.maximum:
                raise ValueError("단일 요율은 같은 하한·상한이 필요합니다")
        if self.grade == "confirmed" and (not self.applicability_confirmed or not self.source):
            raise ValueError("확정 등급에는 출하 적용 확인과 근거가 필요합니다")
        return self


class FxRate(StrictModel):
    currency: Literal["HKD", "USD"]
    krw_per_unit: Decimal = Field(gt=0, max_digits=12, decimal_places=6)
    confirmed: bool = False
    source: str = Field(min_length=1, max_length=2000)


class RobustRequest(StrictModel):
    shipper_pallets: int | None = Field(default=None, gt=0)
    container_pallets: int | None = Field(default=None, gt=0)
    shipment_date: date | None = None
    actual_weight_kg: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=3)
    air: list[BoundItem] = Field(default_factory=list, max_length=100)
    ocean: list[BoundItem] = Field(default_factory=list, max_length=100)
    fx: list[FxRate] = Field(default_factory=list, max_length=10)
    flags: list[Literal["fuel_min_fallback", "plug_days_default"]] = Field(default_factory=list)
    selected_baseline: Literal["air", "sea_solo"] = "air"
    inventory_complete: bool = False

    @model_validator(mode="after")
    def loads(self):
        if (
            self.shipper_pallets
            and self.container_pallets
            and self.shipper_pallets > self.container_pallets
        ):
            raise ValueError("화주 물량이 총 적재량보다 클 수 없습니다")
        if len({rate.currency for rate in self.fx}) != len(self.fx):
            raise ValueError("통화별 환율은 하나만 입력하세요")
        return self


class RobustResult(StrictModel):
    components: list[dict] = Field(default_factory=list)
    status: Literal[
        "not_computable", "not_saving", "conditional", "savings_reference", "savings_confirmed"
    ]
    simulation: bool = False
    selected_baseline: Literal["air", "sea_solo"]
    air_lower: Decimal | None = None
    ocean_known: Decimal | None = None
    ocean_upper: Decimal | None = None
    margin: Decimal | None = None
    break_even_threshold_container: Decimal | None = None
    savings_lower_bound: Decimal | None = None
    shipper_net_savings_lower_bound: Decimal | None = None
    solo_cost: Decimal | None = None
    solo_savings: Decimal | None = None
    unknown_items: list[dict] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    excluded_items: list[dict] = Field(default_factory=list)
    condition: str | None = None
    fee_rate: Decimal = Decimal("0.20")
    settlement_eligible: bool = False
    air_switch_candidate: bool = False
    display_message: str
    warnings: list[str] = Field(
        default_factory=lambda: [
            "reference·범위·가정 환율은 입력 조건부 계산이며 실제 시장가격의 절감 증명이 아닙니다",
            "누락 항목이 없다는 확인은 입력자의 진술이며 독립 검증이 아닙니다",
        ]
    )


def robust_savings(body: RobustRequest) -> RobustResult:
    result = RobustResult(
        status="not_computable",
        selected_baseline=body.selected_baseline,
        display_message="필수 입력 또는 비교 근거가 부족합니다",
    )
    if not all(
        (body.shipper_pallets, body.container_pallets, body.shipment_date, body.actual_weight_kg)
    ):
        return result
    ratio = Decimal(body.shipper_pallets) / Decimal(body.container_pallets)
    fx = {rate.currency: rate for rate in body.fx}
    all_confirmed = not body.flags
    result.flags = list(body.flags)

    def converted(item, upper):
        nonlocal all_confirmed
        value = item.maximum if upper else item.minimum
        if item.currency != "KRW":
            rate = fx.get(item.currency)
            if rate is None:
                raise ValueError("missing_fx")
            value *= rate.krw_per_unit
            all_confirmed = all_confirmed and rate.confirmed
        all_confirmed = all_confirmed and item.grade == "confirmed"
        return value

    def grouped(items):
        groups = {}
        for item in items:
            if item.exclude_reason:
                result.excluded_items.append({"key": item.key, "reason": item.exclude_reason})
                continue
            if item.key == "pnc_basic_handling":
                result.excluded_items.append({"key": item.key, "reason": "THC 중복 방지"})
                continue
            groups.setdefault(item.key, []).append(item)
        return groups.values()

    try:
        air = Decimal(0)
        air_evidence = False
        for alternatives in grouped(body.air):
            known = [item for item in alternatives if item.grade != "unknown"]
            for item in alternatives:
                result.components.append(
                    {
                        "side": "air",
                        **item.model_dump(mode="json"),
                        "allocation": "1",
                        "unknown_as_zero": item.grade == "unknown",
                    }
                )
            if known:
                air += min(converted(item, False) for item in known)
                air_evidence = True
        if not air_evidence and body.selected_baseline == "air":
            return result
        container, shipper = Decimal(0), Decimal(0)
        for alternatives in grouped(body.ocean):
            for item in alternatives:
                result.components.append(
                    {
                        "side": "ocean",
                        **item.model_dump(mode="json"),
                        "allocation": str(ratio if item.basis == "per_container" else 1),
                    }
                )
            if len({item.basis for item in alternatives}) != 1:
                raise ValueError("basis_conflict")
            known = [item for item in alternatives if item.grade != "unknown"]
            unknown = [item for item in alternatives if item.grade == "unknown"]
            if unknown:
                result.unknown_items.append(
                    {"key": alternatives[0].key, "basis": alternatives[0].basis}
                )
            value = max((converted(item, True) for item in known), default=Decimal(0))
            if alternatives[0].basis == "per_container":
                container += value
            else:
                shipper += value
    except ValueError as exc:
        result.flags.append(str(exc))
        return result
    if not body.inventory_complete:
        result.unknown_items.append({"key": "unlisted_costs", "basis": "per_shipper"})
    known_cost = container * ratio + shipper
    result.air_lower = air
    result.ocean_known = known_cost
    if not result.unknown_items:
        result.solo_cost = container + shipper
        result.solo_savings = container * (1 - ratio)
    if body.selected_baseline == "sea_solo" and result.solo_cost is None:
        result.display_message = "단독 해상 대안에 미확인 비용이 있어 비교할 수 없습니다"
        return result
    baseline = air if body.selected_baseline == "air" else result.solo_cost
    result.margin = baseline - known_cost
    if result.margin <= 0:
        result.status = "not_saving"
        result.air_switch_candidate = body.selected_baseline == "air"
        result.display_message = "미확인 비용을 제외해도 선택 대안 대비 절감이 없습니다"
    elif result.unknown_items:
        result.status = "conditional"
        result.break_even_threshold_container = result.margin / ratio
        result.condition = "U_container * n/N + U_shipper < margin"
        if any(item["basis"] == "per_shipper" for item in result.unknown_items):
            result.flags.append("shipper_unknowns_present")
        result.display_message = "미확인 비용이 제시된 조건 미만일 때만 절감 가능합니다"
    else:
        result.status = "savings_confirmed" if all_confirmed else "savings_reference"
        result.savings_lower_bound = result.margin
        result.shipper_net_savings_lower_bound = result.margin * Decimal("0.8")
        result.display_message = "입력 근거 조건하의 절감 하한입니다. 가격 보장이 아닙니다"
    if not result.unknown_items:
        result.ocean_upper = known_cost
    return result
