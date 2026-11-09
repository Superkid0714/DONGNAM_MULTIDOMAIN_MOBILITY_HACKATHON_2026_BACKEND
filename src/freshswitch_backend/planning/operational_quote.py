"""Evidence-only logistics amounts; never imports simulation cost or delay models."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from freshswitch_backend.domain.inputs import StorableStr
from freshswitch_backend.domain.models import KstDatetime

EvidenceStatus = Literal["original_checked", "borrowed", "assumption", "unknown"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Applicability(StrictModel):
    confirmed: bool = False
    carrier: StorableStr = Field(default="", max_length=200)
    equipment: StorableStr = Field(default="", max_length=200)
    valid_from: date | None = None
    valid_until: date | None = None
    checked_on: date | None = None
    reference: StorableStr = Field(default="", max_length=2000)
    duplicate_charge_checked: bool = False

    @model_validator(mode="after")
    def validity(self):
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("적용 확인 기간이 잘못되었습니다")
        return self

    def matches(self, carrier: str, equipment: str, shipment: date, on: date) -> bool:
        return bool(
            self.confirmed
            and carrier
            and equipment
            and self.carrier == carrier
            and self.equipment == equipment
            and self.valid_from
            and self.valid_until
            and self.valid_from <= shipment <= self.valid_until
            and self.checked_on
            and self.checked_on <= on
            and self.reference
            and self.duplicate_charge_checked
        )


class Evidence(StrictModel):
    status: EvidenceStatus = "unknown"
    reference: StorableStr = Field(default="", max_length=2000)
    checked_on: date | None = None
    applicability: Applicability = Field(default_factory=Applicability)

    @model_validator(mode="after")
    def checked(self):
        if self.status == "original_checked" and (not self.reference or not self.checked_on):
            raise ValueError("원문 확인 상태에는 참조와 확인일이 필요합니다")
        return self


class OriginalDocument(StrictModel):
    filename: StorableStr = Field(min_length=1, max_length=200)
    text: StorableStr = Field(min_length=1, max_length=100000)
    checked_on: date
    reference: StorableStr = Field(min_length=1, max_length=2000)


class Dimension(StrictModel):
    length_cm: Decimal = Field(gt=0, max_digits=10, decimal_places=3)
    width_cm: Decimal = Field(gt=0, max_digits=10, decimal_places=3)
    height_cm: Decimal = Field(gt=0, max_digits=10, decimal_places=3)
    pieces: int = Field(gt=0, le=100000)


class Cargo(StrictModel):
    product: StorableStr = Field(min_length=1, max_length=200)
    packaging: StorableStr = Field(min_length=1, max_length=1000)
    pallets: int = Field(gt=0, le=10000)
    gross_weight_kg: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    dimensions: list[Dimension] = Field(min_length=1, max_length=100)
    chargeable_weight_kg: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=3
    )
    chargeable_weight_evidence: Evidence = Field(default_factory=Evidence)


class MoneyLine(StrictModel):
    key: StorableStr = Field(min_length=1, max_length=100)
    currency: StorableStr = Field(pattern=r"^[A-Z]{3}$")
    lower: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=4)
    upper: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=4)
    evidence: Evidence = Field(default_factory=Evidence)

    @model_validator(mode="after")
    def bounds(self):
        if (self.lower is None) != (self.upper is None):
            raise ValueError("비용 하한·상한은 함께 입력하세요")
        if self.lower is not None and self.upper < self.lower:
            raise ValueError("상한은 하한 이상이어야 합니다")
        return self


class FuelSurcharge(StrictModel):
    origin: StorableStr = Field(min_length=1, max_length=200)
    destination: StorableStr = Field(min_length=1, max_length=200)
    effective_from: date
    effective_until: date
    applied_on: date
    weight_basis: Literal["chargeable", "gross"]
    currency: StorableStr = Field(pattern=r"^[A-Z]{3}$")
    rate_per_kg: Decimal = Field(ge=0, max_digits=12, decimal_places=4)
    evidence: Evidence

    @model_validator(mode="after")
    def dates(self):
        if self.effective_until < self.effective_from:
            raise ValueError("할증료 효력기간을 확인하세요")
        return self


class OperationalQuote(StrictModel):
    fuel_segment: Literal["short", "medium", "long"] | None = None
    provider: StorableStr = Field(min_length=1, max_length=200)
    carrier: StorableStr = Field(default="", max_length=200)
    equipment: StorableStr = Field(default="", max_length=200)
    stuffing_method: Literal["port_cfs", "origin", "unknown"] = "unknown"
    stuffing_facility: StorableStr = Field(default="", max_length=500)
    # Each service can be separately priced or included in an all-in quote.
    port_cfs_services: dict[
        Literal["reefer_pickup", "stuffing", "storage", "terminal_transfer"], Evidence
    ] = Field(default_factory=dict)
    provider_type: Literal["forwarder", "airline", "carrier"]
    mode: Literal["air", "ca_sea", "reefer_sea", "consolidated"]
    origin: StorableStr = Field(min_length=1, max_length=200)
    destination: StorableStr = Field(min_length=1, max_length=200)
    shipment_date: date
    cargo: Cargo
    issued_on: date
    valid_from: date
    valid_until: date
    state: Literal["collecting", "conditional", "confirmed", "settled"] = "collecting"
    original: OriginalDocument | None = None
    scope_complete: bool = False
    capacity_confirmed: bool = False
    minimum_pallets: int = Field(default=1, ge=1, le=10000)
    conditions: list[StorableStr] = Field(default_factory=list, max_length=100)
    additional_charges: list[StorableStr] = Field(default_factory=list, max_length=100)
    # None means not determined; False means explicitly excluded.
    includes: dict[Literal["fuel", "inland", "customs", "destination"], bool | None]
    lines: list[MoneyLine] = Field(default_factory=list, max_length=100)
    fuel_surcharge: FuelSurcharge | None = None
    cy_cutoff: KstDatetime | None = None
    cutoff_kind: Literal["original", "official_change"] = "original"
    cutoff_evidence: Evidence = Field(default_factory=Evidence)

    @model_validator(mode="after")
    def validate_quote(self):
        if not self.issued_on <= self.valid_until or self.valid_until < self.valid_from:
            raise ValueError("견적 발행일·효력기간이 잘못되었습니다")
        if set(self.includes) != {"fuel", "inland", "customs", "destination"}:
            raise ValueError("유류·내륙·통관·현지비 포함 여부를 모두 명시하세요")
        if len({line.key for line in self.lines}) != len(self.lines):
            raise ValueError("비용 항목 키는 중복할 수 없습니다")
        if self.mode != "air" and self.fuel_surcharge is not None:
            raise ValueError("kg당 항공 할증료는 항공 견적에만 적용할 수 있습니다")
        if self.cy_cutoff is not None and self.cutoff_evidence.status != "original_checked":
            raise ValueError("CY 마감에는 원문 확인 근거가 필요합니다")
        return self


class QualityRisk(StrictModel):
    expected_krw: int | None = Field(default=None, ge=0)
    lower_krw: int | None = Field(default=None, ge=0)
    upper_krw: int | None = Field(default=None, ge=0)
    evidence: Evidence = Field(default_factory=Evidence)

    @model_validator(mode="after")
    def bounds(self):
        if (self.lower_krw is None) != (self.upper_krw is None):
            raise ValueError("위험 범위는 하한·상한을 함께 입력하세요")
        if self.lower_krw is not None and self.upper_krw < self.lower_krw:
            raise ValueError("품질 위험 범위를 확인하세요")
        if (
            self.expected_krw is not None
            and self.lower_krw is not None
            and not self.lower_krw <= self.expected_krw <= self.upper_krw
        ):
            raise ValueError("기대값은 위험 범위 안이어야 합니다")
        return self


class LogisticsAmount(StrictModel):
    status: Literal["unknown", "conditional", "document_checked"]
    currency: str | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    amount: Decimal | None = None


class OperationalAssessment(StrictModel):
    logistics_billed: LogisticsAmount
    quality_risk: QualityRisk
    fee: int | None = None
    fee_status: Literal["not_assessed"] = "not_assessed"
    total_economic_cost: int | None = None
    price_guaranteed: bool = False
    warnings: list[str]


def assess_quote(quote: OperationalQuote, on: date, quality: QualityRisk | None = None):
    warnings = [
        "원문 확인은 등록 조직의 확인 진술이며 독립적 진위 검증·가격 보장이 아닙니다",
        "수수료·정산 확정은 다음 단계이며 이 평가는 기존 시뮬레이션을 사용하지 않습니다",
    ]
    currencies = {line.currency for line in quote.lines}
    fuel = quote.fuel_surcharge
    add_fuel = quote.mode == "air" and quote.includes["fuel"] is False
    if add_fuel and fuel:
        currencies.add(fuel.currency)
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    unknown = (
        not quote.lines
        or currency is None
        or any(line.lower is None or line.evidence.status == "unknown" for line in quote.lines)
    )
    ready = (
        quote.state in {"confirmed", "settled"}
        and quote.original is not None
        and quote.original.checked_on <= on
        and quote.issued_on <= on
        and quote.valid_from <= on <= quote.valid_until
        and quote.scope_complete
        and quote.capacity_confirmed
        and quote.cargo.pallets >= quote.minimum_pallets
        and not quote.conditions
        and not quote.additional_charges
        and all(value is True for key, value in quote.includes.items() if key != "fuel")
        and quote.includes["fuel"] is not None
    )
    ready = ready and all(
        line.evidence.status == "original_checked"
        and line.evidence.checked_on <= on
        and line.lower is not None
        and line.lower == line.upper
        for line in quote.lines
    )
    applications = [line.evidence for line in quote.lines]
    if add_fuel and fuel:
        applications.append(fuel.evidence)
    if quote.mode != "air":
        services = {"reefer_pickup", "stuffing", "storage", "terminal_transfer"}
        cfs_ready = (
            quote.stuffing_method == "port_cfs"
            and bool(quote.stuffing_facility)
            and set(quote.port_cfs_services) == services
            and all(
                evidence.status == "original_checked" and evidence.checked_on <= on
                for evidence in quote.port_cfs_services.values()
            )
        )
        ready = ready and cfs_ready
        applications.extend(quote.port_cfs_services.values())
        if not cfs_ready:
            warnings.append(
                "항만 공동 적입 시설·냉장 집하·적입·보관·터미널 이송 근거가 미확인입니다"
            )
    applicable = all(
        evidence.applicability.matches(quote.carrier, quote.equipment, quote.shipment_date, on)
        for evidence in applications
    )
    ready = ready and applicable
    if not applicable:
        warnings.append(
            "원문 확인과 별개로 이번 출하의 운송사·장비·기간·중복 청구 적용 확인이 필요합니다"
        )
    lower = sum((line.lower or Decimal(0) for line in quote.lines), Decimal(0))
    upper = sum((line.upper or Decimal(0) for line in quote.lines), Decimal(0))
    if add_fuel:
        weight = None
        valid_fuel = (
            fuel is not None
            and fuel.origin == quote.origin
            and fuel.destination == quote.destination
            and fuel.effective_from <= fuel.applied_on <= fuel.effective_until
        )
        if valid_fuel:
            weight = (
                quote.cargo.chargeable_weight_kg
                if fuel.weight_basis == "chargeable"
                else quote.cargo.gross_weight_kg
            )
        if weight is None:
            unknown = True
            warnings.append("할증료 구간·적용일 또는 적용 중량이 미확인입니다")
        else:
            surcharge = (fuel.rate_per_kg * weight).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            lower += surcharge
            upper += surcharge
            ready = (
                ready
                and fuel.evidence.status == "original_checked"
                and fuel.evidence.checked_on <= on
            )
            if fuel.weight_basis == "chargeable":
                evidence = quote.cargo.chargeable_weight_evidence
                ready = (
                    ready and evidence.status == "original_checked" and evidence.checked_on <= on
                )
    if unknown:
        warnings.append("견적 없음·미확인 항목·혼합 통화가 있어 총 물류비를 산출하지 않습니다")
    if not ready:
        warnings.append("유효기간·포함 범위·성립 조건·공간·원문 확인 요건이 미충족입니다")
    if fuel and not add_fuel:
        warnings.append("견적에 포함되었거나 포함 여부 미확인인 할증료는 별도 가산하지 않습니다")
    return OperationalAssessment(
        logistics_billed=LogisticsAmount(
            status="unknown" if unknown else "document_checked" if ready else "conditional",
            currency=currency,
            lower=None if unknown else lower,
            upper=None if unknown else upper,
            amount=lower if ready and not unknown else None,
        ),
        quality_risk=quality or QualityRisk(),
        warnings=warnings,
    )
