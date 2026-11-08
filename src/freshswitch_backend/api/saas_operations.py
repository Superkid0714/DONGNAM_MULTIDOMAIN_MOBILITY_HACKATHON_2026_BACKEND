from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from freshswitch_backend.api import saas_change_requests, saas_plan_confirm, saas_platform
from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.compute_limits import replan_rate
from freshswitch_backend.api.plans import ReplanResponse
from freshswitch_backend.domain.inputs import StorableStr
from freshswitch_backend.lots import LotStatus
from freshswitch_backend.planning.store import PlanConfirmationState, PlanSession

router = APIRouter(prefix="/saas/forwarder", tags=["saas-forwarder-operations"])
ForwarderPrincipal = Annotated[Principal, Depends(require_role("forwarder"))]


def _own_request(body: saas_platform.PlatformPlanRequest, principal: Principal):
    if body.forwarder_org_id not in {None, principal.org_id}:
        raise HTTPException(
            status_code=403, detail="자기 조직의 컨테이너로만 계획을 만들 수 있습니다"
        )
    return body.model_copy(update={"forwarder_org_id": principal.org_id})


@router.get("/lots")
def shipping_pool(
    principal: ForwarderPrincipal,
    status: LotStatus | None = None,
    scenario_id: Annotated[StorableStr | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[saas_platform.PlatformLot]:
    if status not in {None, "registered"}:
        raise HTTPException(
            status_code=422,
            detail="출하 풀에서는 아직 배정되지 않은 등록 출하만 조회할 수 있습니다",
        )
    return saas_platform.list_lots(principal, "registered", scenario_id, limit, offset)


@router.post("/plans", status_code=201)
def create_plan(
    body: saas_platform.PlatformPlanRequest,
    principal: ForwarderPrincipal,
) -> PlanSession:
    return saas_platform.create_plan(_own_request(body, principal), principal)


@router.post("/plans/preview")
def preview_plan(
    body: saas_platform.PlatformPlanRequest,
    request: Request,
    principal: ForwarderPrincipal,
) -> saas_platform.PlanPreview:
    return saas_platform.preview_plan(_own_request(body, principal), request, principal)


@router.post("/plans/{plan_id}/add-lot", dependencies=[Depends(replan_rate)])
async def add_lot(
    plan_id: str,
    body: saas_platform.AddLotRequest,
    principal: ForwarderPrincipal,
    llm: Annotated[bool | None, Query()] = None,
) -> ReplanResponse:
    return await saas_platform.add_registered_lot(plan_id, body, principal, llm)


@router.get("/plans/{plan_id}/confirmation")
def confirmation(plan_id: str, principal: ForwarderPrincipal) -> PlanConfirmationState:
    return saas_plan_confirm.read_platform_confirmation(plan_id, principal)


@router.post("/plans/{plan_id}/confirmation/auto-accept")
def auto_accept(plan_id: str, principal: ForwarderPrincipal) -> PlanConfirmationState:
    return saas_plan_confirm.auto_accept_confirmation(plan_id, principal)


@router.post("/plans/{plan_id}/confirmation/recompute", dependencies=[Depends(replan_rate)])
def recompute(
    plan_id: str,
    body: saas_plan_confirm.RecomputeRequest,
    principal: ForwarderPrincipal,
) -> saas_plan_confirm.RecomputeResult:
    return saas_plan_confirm.recompute_without_rejected(plan_id, body, principal)


@router.get("/plan-confirmations")
def attention(
    principal: ForwarderPrincipal,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[PlanConfirmationState]:
    return saas_plan_confirm.list_platform_attention(principal, limit)


@router.get("/change-requests")
def change_requests(
    principal: ForwarderPrincipal,
    status: saas_change_requests.ChangeStatus | None = None,
    plan_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[saas_change_requests.PlatformChangeRequest]:
    return saas_change_requests.list_platform_requests(principal, status, plan_id, limit, offset)


@router.get("/change-requests/{request_id}")
def change_request(
    request_id: str,
    principal: ForwarderPrincipal,
) -> saas_change_requests.PlatformChangeRequest:
    return saas_change_requests.read_platform_request(request_id, principal)


@router.post("/change-requests/{request_id}/replan", dependencies=[Depends(replan_rate)])
async def replan_request(
    request_id: str,
    principal: ForwarderPrincipal,
    llm: Annotated[bool | None, Query()] = None,
) -> saas_change_requests.ChangeRequestReplan:
    return await saas_change_requests.replan_change_request(request_id, principal, llm)


@router.post("/change-requests/{request_id}/apply")
def apply_request(
    request_id: str,
    body: saas_change_requests.ChangeRequestApply,
    principal: ForwarderPrincipal,
) -> saas_change_requests.ChangeRequestApplied:
    return saas_change_requests.apply_change_request(request_id, body, principal)


@router.post("/change-requests/{request_id}/reject")
def reject_request(
    request_id: str,
    body: saas_change_requests.ChangeRequestReject,
    principal: ForwarderPrincipal,
) -> saas_change_requests.PlatformChangeRequest:
    return saas_change_requests.reject_change_request(request_id, body, principal)
