from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from freshswitch_backend.api.auth import OptionalPrincipal, current_org_id
from freshswitch_backend.api.saas_access import plan_for
from freshswitch_backend.domain.models import ContainerSettlement, TariffSheet
from freshswitch_backend.execution.quality import (
    ArrivalInspection,
    ArrivalRequest,
    ArrivalSettlementResult,
    QualityError,
    QualitySettlement,
    adjusted_settlements,
    current_inspections,
)
from freshswitch_backend.planning.store import SessionConflict, store
from freshswitch_backend.quality import quality_history

router = APIRouter(prefix="/plans", tags=["settlement"])

OrgId = Annotated[str, Depends(current_org_id)]


class SettlementView(BaseModel):
    plan_id: str
    version: int
    settlements: list[QualitySettlement]
    inspections: list[ArrivalInspection]
    total_fee_before_refund: int
    total_refund: int
    total_fee: int
    total_savings: int
    total_performance_fee: int = Field(
        default=0,
        description="환급 전 금액. total_fee_before_refund = total_performance_fee + total_usage_fee(시범 항차 제외)",
    )
    total_usage_fee: int = Field(
        default=0,
        description="환급 전 금액. total_fee_before_refund = total_performance_fee + total_usage_fee(시범 항차 제외)",
    )
    containers: list[ContainerSettlement]
    tariffs: list[TariffSheet]
    assumed_items: list[str]


@router.get("/{plan_id}/settlement")
def read_settlement(plan_id: str, org_id: OrgId, principal: OptionalPrincipal) -> SettlementView:
    if principal is not None and principal.org_type == "platform":
        org_id = plan_for(plan_id, principal).owner_org_id
    session = store.get(plan_id, org_id=org_id)
    if session is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    settlements = adjusted_settlements(session.plan, session.arrival_inspections)
    return SettlementView(
        plan_id=plan_id,
        version=session.version,
        settlements=settlements,
        inspections=current_inspections(session.plan, session.arrival_inspections),
        total_fee_before_refund=sum(s.fee_before_refund for s in settlements),
        total_refund=sum(s.quality_refund for s in settlements),
        total_fee=sum(s.fee for s in settlements),
        total_savings=sum(s.savings for s in settlements),
        total_performance_fee=sum(s.performance_fee for s in settlements),
        total_usage_fee=sum(s.usage_fee for s in settlements),
        containers=session.plan.container_settlements,
        tariffs=session.plan.settlement_tariffs,
        assumed_items=sorted(
            {
                f"{t.product_id}.{item}"
                for t in session.plan.settlement_tariffs
                for item in t.assumed_items
            }
        ),
    )


@router.post("/{plan_id}/settlement/arrival")
def post_arrival(plan_id: str, body: ArrivalRequest, org_id: OrgId) -> ArrivalSettlementResult:
    if store.get(plan_id, org_id=org_id) is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    try:
        result, session = store.record_arrival_snapshot(plan_id, body, org_id=org_id)
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QualityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다") from exc
    quality_history.record_arrival(plan_id, session, result, org_id=org_id)
    return result
