"""Organization-local quote accumulation. Aggregates remain non-billable estimates."""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
    select,
)

from freshswitch_backend.api.auth import OptionalPrincipal
from freshswitch_backend.config import Settings, get_settings
from freshswitch_backend.domain.inputs import StorableStr
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import Base
from freshswitch_backend.planning.robust.calculator import (
    RobustRequest,
    StrictModel,
    robust_savings,
)
from freshswitch_backend.planning.robust.estimate import (
    EstimateItem,
    EstimateResult,
    EvaluateRequest,
    estimate,
)


class QuoteRow(Base):
    __tablename__ = "robust_quote_observations"
    id = Column(String(64), primary_key=True)
    org_id = Column(String(64), ForeignKey("organizations.id"), nullable=False, index=True)
    actor_id = Column(String(64), nullable=False)
    item = Column(String(100), nullable=False)
    origin = Column(String(200), nullable=False)
    destination = Column(String(200), nullable=False)
    equipment = Column(String(100), nullable=False)
    quoted_on = Column(Date, nullable=False)
    minimum = Column(Numeric(18, 4), nullable=False)
    maximum = Column(Numeric(18, 4), nullable=False)
    currency = Column(String(3), nullable=False)
    basis = Column(String(30), nullable=False)
    reference_pallets = Column(Integer, nullable=True)
    source = Column(String(2000), nullable=False)
    is_demo = Column(Boolean, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class QuoteInput(StrictModel):
    item: StorableStr = Field(min_length=1, max_length=100)
    origin: StorableStr = Field(min_length=1, max_length=200)
    destination: StorableStr = Field(min_length=1, max_length=200)
    equipment: StorableStr = Field(min_length=1, max_length=100)
    quoted_on: date
    minimum: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    maximum: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    currency: Literal["KRW", "HKD", "USD"] = "KRW"
    basis: Literal["per_container", "per_shipper"]
    reference_pallets: int | None = Field(default=None, gt=0)
    source: StorableStr = Field(min_length=1, max_length=2000)
    is_demo: bool = False

    @model_validator(mode="after")
    def bounds(self):
        if self.maximum < self.minimum:
            raise ValueError("상한은 하한 이상이어야 합니다")
        return self


class QuoteView(QuoteInput):
    id: str
    created_at: datetime


def recent_medians(
    rows: list[QuoteInput],
    on: date,
    origin: str,
    destination: str,
    equipment: str,
    settings: Settings | None = None,
):
    settings = settings or get_settings()
    groups = {}
    for row in rows:
        if (
            row.is_demo
            or not on - timedelta(days=settings.robust_quote_lookback_days - 1)
            <= row.quoted_on
            <= on
        ):
            continue
        if (row.origin, row.destination, row.equipment) != (origin, destination, equipment):
            continue
        groups.setdefault((row.item, row.currency, row.basis, row.reference_pallets), []).append(
            row
        )
    return [
        {
            "item": key[0],
            "currency": key[1],
            "basis": key[2],
            "reference_pallets": key[3],
            "count": len(values),
            "minimum": median(r.minimum for r in values),
            "maximum": median(r.maximum for r in values),
            "estimate_source": f"최근 견적 {len(values)}건 중앙값"
            if len(values) >= settings.robust_quote_min_count
            else "시드 추정값",
        }
        for key, values in sorted(groups.items(), key=lambda pair: str(pair[0]))
    ]


def actor(principal):
    if principal is None:
        raise HTTPException(401, "로그인이 필요합니다")
    return principal


def observations(
    org_id: str,
    on: date,
    origin: str,
    destination: str,
    equipment: str,
    settings: Settings | None = None,
):
    settings = settings or get_settings()
    with database.begin() as db:
        rows = db.scalars(
            select(QuoteRow).where(
                QuoteRow.org_id == org_id,
                QuoteRow.origin == origin,
                QuoteRow.destination == destination,
                QuoteRow.equipment == equipment,
                QuoteRow.is_demo.is_(False),
                QuoteRow.quoted_on >= on - timedelta(days=settings.robust_quote_lookback_days - 1),
                QuoteRow.quoted_on <= on,
            )
        ).all()
        return [
            QuoteInput.model_validate({k: getattr(row, k) for k in QuoteInput.model_fields})
            for row in rows
        ]


class AccumulatedEvaluation(StrictModel):
    origin: StorableStr = Field(min_length=1, max_length=200)
    destination: StorableStr = Field(min_length=1, max_length=200)
    equipment: StorableStr = Field(min_length=1, max_length=100)
    evaluated_on: date
    evaluation: EvaluateRequest


def evaluate_accumulated(
    body: AccumulatedEvaluation, rows: list[QuoteInput], settings: Settings | None = None
):
    settings = settings or get_settings()
    evaluation = body.evaluation.model_copy(deep=True)
    aggregates = recent_medians(
        rows, body.evaluated_on, body.origin, body.destination, body.equipment, settings
    )
    # Different currencies/units for the same item are ambiguous, never combine automatically.
    counts = {}
    for group in aggregates:
        counts[group["item"]] = counts.get(group["item"], 0) + 1
    applied = []
    for group in aggregates:
        if group["count"] < settings.robust_quote_min_count or counts[group["item"]] != 1:
            continue
        factor = Decimal(1)
        if group["reference_pallets"]:
            if evaluation.shipper_pallets is None:
                continue
            factor = Decimal(evaluation.shipper_pallets) / group["reference_pallets"]
        # Explicit per-request estimate overrides take precedence over historical defaults.
        if any(item.key == group["item"] for item in evaluation.estimate_items):
            continue
        evaluation.estimate_items.append(
            EstimateItem(
                key=group["item"],
                supplier="organization_quote_median",
                basis=group["basis"],
                minimum=group["minimum"] * factor,
                maximum=group["maximum"] * factor,
                currency=group["currency"],
                source=group["estimate_source"],
            )
        )
        applied.append(group["item"])
    robust = robust_savings(
        RobustRequest.model_validate(
            body.evaluation.model_dump(
                exclude={"estimate_items", "estimate_equipment", "estimate_plug_days"}
            )
        )
    )
    result = estimate(evaluation)
    result["general_value_source"] = "최근 견적 중앙값 적용" if applied else "시드 추정값"
    result["quote_statistics"] = aggregates
    result["median_items"] = applied
    return EstimateResult(**robust.model_dump(), robust=robust, estimate=result)


router = APIRouter(prefix="/quotes", tags=["robust-quotes"])


@router.post("", status_code=201, response_model=QuoteView)
def create(body: QuoteInput, principal: OptionalPrincipal):
    user = actor(principal)
    if getattr(user, "world_id", None) is not None and not body.is_demo:
        raise HTTPException(403, "샌드박스 견적은 is_demo=true여야 합니다")
    with database.begin() as db:
        row = QuoteRow(
            id=str(uuid.uuid4()), org_id=user.org_id, actor_id=user.user_id, **body.model_dump()
        )
        db.add(row)
        db.flush()
        return {k: getattr(row, k) for k in [*QuoteInput.model_fields, "id", "created_at"]}


@router.get("", response_model=list[QuoteView])
def read(
    principal: OptionalPrincipal,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    user = actor(principal)
    with database.begin() as db:
        rows = db.scalars(
            select(QuoteRow)
            .where(QuoteRow.org_id == user.org_id)
            .order_by(QuoteRow.created_at.desc(), QuoteRow.id)
            .offset(offset)
            .limit(limit)
        )
        return [
            {k: getattr(row, k) for k in [*QuoteInput.model_fields, "id", "created_at"]}
            for row in rows
        ]


@router.post("/evaluate", response_model=EstimateResult)
def evaluate(body: AccumulatedEvaluation, principal: OptionalPrincipal):
    user = actor(principal)
    settings = get_settings()
    return evaluate_accumulated(
        body,
        observations(
            user.org_id, body.evaluated_on, body.origin, body.destination, body.equipment, settings
        ),
        settings,
    )
