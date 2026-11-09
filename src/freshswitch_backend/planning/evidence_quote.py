"""Evaluate supplied quote evidence without changing bookings or settlements."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from freshswitch_backend.domain.inputs import StorableStr


class QuoteScope(BaseModel):
    origin: StorableStr = Field(min_length=1, max_length=200)
    destination: StorableStr = Field(min_length=1, max_length=200)
    shipment_date: date
    cargo_reference: StorableStr = Field(min_length=1, max_length=200)
    pallets: int = Field(gt=0, le=10000)
    gross_weight_kg: float = Field(gt=0, allow_inf_nan=False)
    volume_m3: float = Field(gt=0, allow_inf_nan=False)
    delivery_deadline: date
    service_requirements: StorableStr = Field(min_length=1, max_length=2000)
    included_services: list[StorableStr] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def dates(self):
        if self.delivery_deadline < self.shipment_date:
            raise ValueError("도착 요구일은 출하일보다 빠를 수 없습니다")
        if any(not item.strip() for item in self.included_services):
            raise ValueError("포함 서비스는 비어 있을 수 없습니다")
        if len(set(self.included_services)) != len(self.included_services):
            raise ValueError("포함 서비스는 중복할 수 없습니다")
        return self


class EvidenceLine(BaseModel):
    key: StorableStr = Field(min_length=1, max_length=100)
    lower_krw: int = Field(ge=0)
    upper_krw: int = Field(ge=0)
    status: Literal["confirmed", "estimated", "unknown"]
    source_reference: StorableStr = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bounds(self):
        if self.upper_krw < self.lower_krw:
            raise ValueError("비용 상한은 하한 이상이어야 합니다")
        if self.status == "confirmed" and self.lower_krw != self.upper_krw:
            raise ValueError("확정 비용은 단일 금액이어야 합니다")
        if not self.source_reference.strip():
            raise ValueError("견적 근거 참조가 필요합니다")
        return self


class SuppliedQuote(BaseModel):
    provider: StorableStr = Field(min_length=1, max_length=200)
    mode: Literal["air", "sea_solo", "sea_shared"]
    scope: QuoteScope
    issued_on: date
    valid_from: date
    valid_until: date
    scope_complete: bool = False
    capacity_confirmed: bool = False
    exclusions: list[StorableStr] = Field(default_factory=list, max_length=100)
    lines: list[EvidenceLine] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validity(self):
        if self.valid_until < self.valid_from or self.issued_on > self.valid_until:
            raise ValueError("견적 발행일·유효기간을 확인하세요")
        keys = [line.key for line in self.lines]
        if len(keys) != len(set(keys)):
            raise ValueError("중복 비용 항목은 사용할 수 없습니다")
        return self


class EvidenceQuoteRequest(BaseModel):
    evaluated_on: date
    baseline: SuppliedQuote
    proposed: SuppliedQuote
    performance_fee_rate: float = Field(default=0.25, ge=0, le=1, allow_inf_nan=False)
    expected_quality_loss_krw: int | None = Field(default=None, ge=0)


class EvidenceQuoteResult(BaseModel):
    status: Literal["confirmed_inputs", "conditional", "not_comparable"]
    baseline_logistics_lower_krw: int
    baseline_logistics_upper_krw: int
    proposed_logistics_lower_krw: int
    proposed_logistics_upper_krw: int
    logistics_saving_lower_krw: int | None = None
    logistics_saving_upper_krw: int | None = None
    performance_fee_krw: int = 0
    proposed_customer_charge_krw: int | None = None
    net_saving_krw: int | None = None
    expected_quality_loss_krw: int | None = None
    quality_loss_basis: str = "user_supplied_unverified_not_used_for_fee"
    total_economic_cost_krw: int | None = None
    price_guaranteed: bool = False
    source_independently_verified: bool = False
    warnings: list[str]


def evaluate_evidence_quote(body: EvidenceQuoteRequest) -> EvidenceQuoteResult:
    baseline, proposed = body.baseline, body.proposed
    low_b = sum(line.lower_krw for line in baseline.lines)
    high_b = sum(line.upper_krw for line in baseline.lines)
    low_p = sum(line.lower_krw for line in proposed.lines)
    high_p = sum(line.upper_krw for line in proposed.lines)
    left, right = baseline.scope.model_dump(), proposed.scope.model_dump()
    left["included_services"] = sorted(left["included_services"])
    right["included_services"] = sorted(right["included_services"])
    comparable = left == right and sorted(baseline.exclusions) == sorted(proposed.exclusions)
    warnings = [
        "입력된 문서 참조와 확정 상태는 독립 검증하지 않았습니다",
        "평가 결과는 예약·계약·가격 상한·품질·납기 보장을 생성하지 않습니다",
        "기존 계획·정산·청구액을 변경하거나 견적 원문을 저장하지 않습니다",
        "비용 범위는 공급자 입력 범위이며 통계적 신뢰구간이 아닙니다",
    ]
    confirmed = True
    for name, quote in (("baseline", baseline), ("proposed", proposed)):
        if not (
            quote.issued_on <= body.evaluated_on
            and quote.valid_from <= body.evaluated_on <= quote.valid_until
        ):
            confirmed = False
            warnings.append(f"{name}: 평가일에 유효한 견적이 아닙니다")
        if not quote.scope_complete or not quote.capacity_confirmed:
            confirmed = False
            warnings.append(f"{name}: 비용 범위 또는 운송 물량·공간이 미확정입니다")
        if any(line.status != "confirmed" for line in quote.lines):
            confirmed = False
            warnings.append(f"{name}: 추정·미확인 비용이 포함되어 있습니다")
    unknown = any(
        line.status == "unknown" for quote in (baseline, proposed) for line in quote.lines
    )
    if unknown:
        warnings.append("미확인 항목이 있어 입력 합계와 절감 범위를 완전한 비용으로 볼 수 없습니다")
    if not comparable:
        warnings.append("화물·일정·서비스 범위 또는 제외 조건이 달라 절감 비교를 제공하지 않습니다")
    ready = comparable and confirmed
    fee = round(max(0, low_b - low_p) * body.performance_fee_rate) if ready else 0
    charge = low_p + fee if ready else None
    return EvidenceQuoteResult(
        status="confirmed_inputs" if ready else "conditional" if comparable else "not_comparable",
        baseline_logistics_lower_krw=low_b,
        baseline_logistics_upper_krw=high_b,
        proposed_logistics_lower_krw=low_p,
        proposed_logistics_upper_krw=high_p,
        logistics_saving_lower_krw=low_b - high_p if comparable and not unknown else None,
        logistics_saving_upper_krw=high_b - low_p if comparable and not unknown else None,
        performance_fee_krw=fee,
        proposed_customer_charge_krw=charge,
        net_saving_krw=low_b - charge if charge is not None else None,
        expected_quality_loss_krw=body.expected_quality_loss_krw,
        total_economic_cost_krw=(
            charge + body.expected_quality_loss_krw
            if charge is not None and body.expected_quality_loss_krw is not None
            else None
        ),
        warnings=warnings,
    )