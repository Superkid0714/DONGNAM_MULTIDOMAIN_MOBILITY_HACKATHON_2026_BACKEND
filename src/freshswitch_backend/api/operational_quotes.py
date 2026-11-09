from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from freshswitch_backend.api.auth import OptionalPrincipal, Principal
from freshswitch_backend.persistence.operational_quotes import read_quote, save_quote
from freshswitch_backend.planning.operational_quote import (
    OperationalAssessment,
    OperationalQuote,
    QualityRisk,
    StrictModel,
    assess_quote,
)

router = APIRouter(prefix="/quotes", tags=["operational-quotes"])


class QuoteRecord(BaseModel):
    id: str
    created_at: datetime
    quote: OperationalQuote


class AssessmentRequest(StrictModel):
    evaluated_on: date
    quality_risk: QualityRisk | None = None


def authorized(principal: Principal | None) -> Principal:
    if principal is None:
        raise HTTPException(401, "로그인이 필요합니다")
    if principal.org_type not in {"shipper", "forwarder"} or principal.world_id is not None:
        raise HTTPException(403, "실제 화주·포워더 조직만 운영 견적을 사용할 수 있습니다")
    return principal


@router.post("", status_code=201)
def create_quote(body: OperationalQuote, principal: OptionalPrincipal) -> QuoteRecord:
    actor = authorized(principal)
    return QuoteRecord.model_validate(save_quote(actor.org_id, actor.user_id, body))


@router.get("/{quote_id}")
def get_quote(quote_id: str, principal: OptionalPrincipal) -> QuoteRecord:
    actor = authorized(principal)
    record = read_quote(actor.org_id, quote_id)
    if record is None:
        raise HTTPException(404, "견적을 찾을 수 없습니다")
    return QuoteRecord.model_validate(record)


@router.post("/{quote_id}/assessment")
def assess_saved_quote(
    quote_id: str, body: AssessmentRequest, principal: OptionalPrincipal
) -> OperationalAssessment:
    record = get_quote(quote_id, principal)
    return assess_quote(record.quote, body.evaluated_on, body.quality_risk)
