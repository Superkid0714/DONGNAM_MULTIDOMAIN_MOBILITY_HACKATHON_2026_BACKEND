from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from freshswitch_backend.api.auth import Principal, _token_hash, app_settings, require_role
from freshswitch_backend.persistence.sandbox_reset import (
    RESET_COOLDOWN,
    SandboxResetCooldown,
    SandboxResetResult,
    reset_sandbox,
)
from freshswitch_backend.persistence.sandbox_world import WorldNotFound

router = APIRouter(prefix="/saas/sandbox", tags=["saas-sandbox"])

PlatformPrincipal = Annotated[Principal, Depends(require_role("platform", "forwarder", "shipper"))]

NOT_SANDBOX = "데모(sandbox) 조직에서만 데모 세계를 초기화할 수 있습니다"


class SandboxResetResponse(BaseModel):
    scope: Literal["sandbox"] = "sandbox"
    orgs: int
    reset_at: datetime
    world_id: str | None = None


def sandbox_reset_response(result: SandboxResetResult) -> SandboxResetResponse:
    return SandboxResetResponse(
        orgs=result.orgs, reset_at=result.reset_at, world_id=result.world_id
    )


class SandboxResetBlocked(BaseModel):
    detail: str


RESET_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {"description": NOT_SANDBOX},
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": SandboxResetBlocked,
        "description": "직전 초기화 뒤 60초가 지나지 않았습니다",
        "headers": {
            "Retry-After": {
                "description": "다시 시도할 수 있을 때까지 남은 초",
                "schema": {"type": "integer"},
            }
        },
    },
}


@router.post("/reset", responses=RESET_RESPONSES)
def reset_world(request: Request, principal: PlatformPrincipal) -> SandboxResetResponse:
    if principal.world_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_SANDBOX)
    token = request.cookies.get(app_settings(request).session_cookie_name)
    try:
        result = reset_sandbox(
            keep_token_hash=_token_hash(token) if token else None,
            cooldown=RESET_COOLDOWN,
            world_id=principal.world_id,
        )
    except WorldNotFound as exc:
        raise HTTPException(status_code=404, detail="세계를 찾을 수 없습니다") from exc
    except SandboxResetCooldown as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"방금 초기화했습니다. {exc.retry_after}초 뒤에 다시 시도하세요",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    return sandbox_reset_response(result)
