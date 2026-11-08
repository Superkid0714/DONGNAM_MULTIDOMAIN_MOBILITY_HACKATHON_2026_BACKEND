from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials

from freshswitch_backend.api.access import bearer, require_demo_reset
from freshswitch_backend.api.auth import OptionalPrincipal, Principal, app_settings
from freshswitch_backend.api.compute_limits import analysis_rate, run_analysis
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import org_sandbox
from freshswitch_backend.persistence.tables import SandboxWorldRow
from freshswitch_backend.quality import (
    LearningScope,
    QualityAccessDenied,
    QualityInputError,
    learned_table,
    learning_scope,
    quality_history,
)
from freshswitch_backend.quality.impact import learning_impact
from freshswitch_backend.quality.models import (
    ImpactResult,
    LossTable,
    Observation,
    ObservationIn,
    ObservationList,
    ResetResult,
)
from freshswitch_backend.quality.store import ScopeRef

router = APIRouter(prefix="/quality", tags=["quality"])

PLATFORM_ONLY = "품질 관측 원본은 플랫폼 조직만 다룰 수 있습니다"
IMPACT_PLATFORM_ONLY = "학습 손실률의 금액 영향은 플랫폼 조직만 볼 수 있습니다"
SANDBOX_WRITE = "데모 플랫폼은 품질 관측을 추가·삭제·초기화할 수 없습니다"
OTHER_SCOPE = "다른 학습 범위의 품질 관측은 다룰 수 없습니다"


def _principal_scope(principal: Principal | None) -> ScopeRef:
    return learning_scope(principal.org_id if principal is not None else None)


def caller_scope(principal: OptionalPrincipal) -> ScopeRef:
    return _principal_scope(principal)


Scope = Annotated[ScopeRef, Depends(caller_scope)]


def impact_viewer(principal: OptionalPrincipal) -> ScopeRef:
    if principal is not None and principal.org_type not in {"platform", "forwarder"}:
        raise HTTPException(status_code=403, detail=IMPACT_PLATFORM_ONLY)
    return _principal_scope(principal)


ImpactScope = Annotated[ScopeRef, Depends(impact_viewer)]


def observation_viewer(request: Request, principal: OptionalPrincipal) -> ScopeRef:
    if not app_settings(request).saas_mode:
        return _principal_scope(principal)
    if principal is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    if principal.org_type != "platform":
        raise HTTPException(status_code=403, detail=PLATFORM_ONLY)
    return _principal_scope(principal)


ViewerScope = Annotated[ScopeRef, Depends(observation_viewer)]


def observation_writer(request: Request, principal: OptionalPrincipal) -> ScopeRef:
    if not app_settings(request).saas_mode:
        return _principal_scope(principal)
    if principal is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    if principal.org_type != "platform":
        raise HTTPException(status_code=403, detail=PLATFORM_ONLY)
    if org_sandbox(principal.org_id):
        raise HTTPException(status_code=403, detail=SANDBOX_WRITE)
    return _principal_scope(principal)


WriterScope = Annotated[ScopeRef, Depends(observation_writer)]


class ResetTarget:
    def __init__(self, org_type: str | None, scope: ScopeRef | str) -> None:
        self.org_type = org_type
        self.scope = scope


def reset_target(
    request: Request,
    principal: OptionalPrincipal,
    credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    scope: Annotated[LearningScope | None, Query()] = None,
    world_id: str | None = None,
) -> ResetTarget:
    if credential is not None:
        require_demo_reset(credential)
        chosen = scope or "demo"
        if chosen == "sandbox" and not world_id:
            raise HTTPException(status_code=422, detail="world_id가 필요합니다")
        if chosen == "sandbox":
            return ResetTarget(None, ScopeRef("sandbox", world_id))
        return ResetTarget(None, chosen)
    if principal is None and not app_settings(request).saas_mode:
        if scope not in (None, "demo"):
            raise HTTPException(status_code=403, detail=OTHER_SCOPE)
        return ResetTarget(None, "demo")
    if principal is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    if app_settings(request).saas_mode and org_sandbox(principal.org_id):
        raise HTTPException(status_code=403, detail=SANDBOX_WRITE)
    own = _principal_scope(principal)
    if scope is not None and scope != own.kind:
        raise HTTPException(status_code=403, detail=OTHER_SCOPE)
    return ResetTarget(principal.org_type, own)


Target = Annotated[ResetTarget, Depends(reset_target)]


def _scenario(scenario_id: str) -> Scenario:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    return scenario


@router.get("/loss-rates")
def loss_rates(scope: Scope, scenario_id: Annotated[str, Query()] = "default") -> LossTable:
    return learned_table(_scenario(scenario_id), scope=scope)


@router.get("/observations")
def list_observations(scope: ViewerScope) -> ObservationList:
    observations = quality_history.observations(scope)
    return ObservationList(
        seed_label=quality_history.seed_label_for(scope),
        count=len(observations),
        observations=observations,
    )


@router.post("/observations", status_code=201)
def add_observation(body: ObservationIn, scope: WriterScope) -> Observation:
    try:
        return quality_history.add(body, scope)
    except QualityInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/observations/{obs_id}")
def read_observation(obs_id: str, scope: ViewerScope) -> Observation:
    observation = quality_history.get(obs_id, scope)
    if observation is None:
        raise HTTPException(status_code=404, detail="관측을 찾을 수 없습니다")
    return observation


@router.delete("/observations/{obs_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_observation(obs_id: str, target: Target) -> None:
    try:
        deleted = quality_history.delete(obs_id, org_type=target.org_type, scope=target.scope)
    except QualityAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="관측을 찾을 수 없습니다")


@router.post("/reset")
def reset(target: Target, seed: Annotated[bool, Query()] = True) -> ResetResult:
    scope = target.scope
    if isinstance(scope, ScopeRef) and scope.kind == "sandbox" and scope.world_id:
        with database.begin() as db:
            if db.get(SandboxWorldRow, scope.world_id) is None:
                raise HTTPException(status_code=404, detail="세계를 찾을 수 없습니다")
    try:
        cleared, seeded = quality_history.reset(
            seed=seed, org_type=target.org_type, scope=target.scope
        )
    except QualityAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except QualityInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ResetResult(cleared=cleared, seeded=seeded)


@router.get("/impact", dependencies=[Depends(analysis_rate)])
async def impact(
    scope: ImpactScope, scenario_id: Annotated[str, Query()] = "default"
) -> ImpactResult:
    scenario = _scenario(scenario_id)
    table = await run_in_threadpool(learned_table, scenario, quality_history, scope)
    cells = [c for c in table.cells if c.observations]
    return await run_in_threadpool(
        run_analysis, learning_impact, scenario, table.adjustments, cells
    )
