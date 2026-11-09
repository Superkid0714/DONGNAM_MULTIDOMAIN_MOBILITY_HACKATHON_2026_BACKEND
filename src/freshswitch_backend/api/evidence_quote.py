from fastapi import APIRouter, HTTPException

from freshswitch_backend.api.auth import OptionalPrincipal
from freshswitch_backend.planning.evidence_quote import (
    EvidenceQuoteRequest,
    EvidenceQuoteResult,
    evaluate_evidence_quote,
)

router = APIRouter(prefix="/compare", tags=["compare"])


@router.post("/evidence-quote")
def evidence_quote(body: EvidenceQuoteRequest, principal: OptionalPrincipal) -> EvidenceQuoteResult:
    if principal is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return evaluate_evidence_quote(body)