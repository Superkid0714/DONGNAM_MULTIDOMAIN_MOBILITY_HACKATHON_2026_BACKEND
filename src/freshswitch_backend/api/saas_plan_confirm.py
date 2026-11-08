from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.compute_limits import compute_slot, replan_rate
from freshswitch_backend.api.saas_access import org_shipper_ids, plan_for
from freshswitch_backend.api.saas_views import replan_pending
from freshswitch_backend.domain.inputs import StorableStr
from freshswitch_backend.notifications import notify_plan_confirmation
from freshswitch_backend.persistence.orgs import org_sandbox
from freshswitch_backend.planning.store import (
    ConfirmationStatus,
    ContainerBooked,
    OrderInputError,
    PlanConfirmation,
    PlanConfirmationState,
    PlanSession,
    SessionConflict,
    SessionStatus,
    store,
)

router = APIRouter(prefix="/saas", tags=["saas-plan-confirm"])

ShipperPrincipal = Annotated[Principal, Depends(require_role("shipper"))]
PlatformPrincipal = Annotated[Principal, Depends(require_role("platform"))]

NOT_FOUND = "확인할 계획을 찾을 수 없습니다"
PLAN_NOT_FOUND = "계획을 찾을 수 없습니다"
SANDBOX_ONLY = "전원 자동 수락은 데모(sandbox) 조직에서만 쓸 수 있습니다"


class ConfirmationDecision(BaseModel):
    accept: bool
    reason: StorableStr | None = Field(default=None, max_length=500)
    shipper_id: StorableStr | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _reason(self) -> "ConfirmationDecision":
        if not self.accept and not (self.reason or "").strip():
            raise ValueError("거절할 때는 사유(reason)를 적어 주세요")
        return self


class ShipperConfirmationView(BaseModel):
    plan_id: str
    plan_status: SessionStatus
    plan_version: int
    confirmations: list[PlanConfirmation]
    replan_pending: bool = False


class ShipperPlanConfirmation(PlanConfirmation):
    replan_pending: bool = False


class RecomputeRequest(BaseModel):
    shipper_ids: list[str] = Field(default_factory=list, max_length=50)
    auto_accept: Literal[False] = False


class RecomputeResult(BaseModel):
    plan: PlanSession
    confirmation: PlanConfirmationState


def require_sandbox(principal: Principal) -> None:
    if not org_sandbox(principal.org_id):
        raise HTTPException(status_code=403, detail=SANDBOX_ONLY)


def notify_round(session: PlanSession, owner_org_id: str) -> None:
    pending = {
        c.shipper_id
        for c in store.confirmation_state(session.plan_id, owner_org_id).confirmations
        if c.status == "pending"
    }
    if pending:
        notify_plan_confirmation(session, owner_org_id, store.clock(), shipper_ids=pending)


def _shipper_view(plan_id: str, principal: Principal) -> ShipperConfirmationView:
    access = plan_for(plan_id, principal)
    session = access.session
    state = store.confirmation_states([plan_id]).get(plan_id)
    return ShipperConfirmationView(
        plan_id=plan_id,
        plan_status=session.status,
        plan_version=session.version,
        confirmations=store.shipper_confirmations(principal.org_id, plan_id=plan_id),
        replan_pending=replan_pending(state, access.viewer.shipper_ids),
    )


@router.get("/shipper/plan-confirmations")
def list_shipper_confirmations(
    principal: ShipperPrincipal, status: ConfirmationStatus | None = None
) -> list[ShipperPlanConfirmation]:
    rows = store.shipper_confirmations(principal.org_id, status=status)
    states = store.confirmation_states(sorted({row.plan_id for row in rows}))
    own = org_shipper_ids(principal.org_id)
    return [
        ShipperPlanConfirmation(
            **row.model_dump(), replan_pending=replan_pending(states.get(row.plan_id), own)
        )
        for row in rows
    ]


@router.get("/shipper/plans/{plan_id}/confirmation")
def read_shipper_confirmation(plan_id: str, principal: ShipperPrincipal) -> ShipperConfirmationView:
    return _shipper_view(plan_id, principal)


@router.post("/shipper/plans/{plan_id}/confirmation")
def decide_shipper_confirmation(
    plan_id: str, body: ConfirmationDecision, principal: ShipperPrincipal
) -> ShipperConfirmationView:
    plan_for(plan_id, principal)
    try:
        store.decide_confirmation(
            plan_id,
            principal.org_id,
            body.accept,
            reason=(body.reason or "").strip() or None,
            shipper_id=body.shipper_id,
            user_id=principal.user_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _shipper_view(plan_id, principal)


@router.get("/platform/plan-confirmations")
def list_platform_attention(
    principal: PlatformPrincipal,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[PlanConfirmationState]:
    return store.needing_attention(principal.org_id, limit=limit)


@router.get("/platform/plans/{plan_id}/confirmation")
def read_platform_confirmation(plan_id: str, principal: PlatformPrincipal) -> PlanConfirmationState:
    try:
        owner = plan_for(plan_id, principal).owner_org_id
        return store.confirmation_state(plan_id, owner)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=PLAN_NOT_FOUND) from exc


@router.post("/platform/plans/{plan_id}/confirmation/auto-accept")
def auto_accept_confirmation(plan_id: str, principal: PlatformPrincipal) -> PlanConfirmationState:
    require_sandbox(principal)
    try:
        return store.accept_all(plan_id, principal.org_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=PLAN_NOT_FOUND) from exc


@router.post(
    "/platform/plans/{plan_id}/confirmation/recompute", dependencies=[Depends(replan_rate)]
)
def recompute_without_rejected(
    plan_id: str, body: RecomputeRequest, principal: PlatformPrincipal
) -> RecomputeResult:
    if body.auto_accept:
        require_sandbox(principal)
    try:
        with compute_slot():
            session, pending = store.recompute_without(
                plan_id,
                principal.org_id,
                shipper_ids=body.shipper_ids or None,
                auto_accept=body.auto_accept,
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=PLAN_NOT_FOUND) from exc
    except OrderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ContainerBooked as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "그사이 다른 계획이 컨테이너를 배정했습니다: "
                f"{', '.join(exc.container_ids)}. 다시 계산하세요"
            ),
        ) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if pending:
        notify_round(session, principal.org_id)
    return RecomputeResult(
        plan=session, confirmation=store.confirmation_state(plan_id, principal.org_id)
    )
