from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from freshswitch_backend.api import auth, coldchain, demo, health, ops, quality, showcase
from freshswitch_backend.api.access import require_demo_reset
from freshswitch_backend.api.auth import app_settings, current_principal
from freshswitch_backend.observability import tag_request
from freshswitch_backend.persistence.orgs import org_sandbox

LOGIN_REQUIRED = "로그인이 필요합니다"

Endpoint = Callable[..., Any]


def _endpoints(*routers: APIRouter) -> frozenset[Endpoint]:
    return frozenset(
        route.endpoint
        for router in routers
        for route in router.routes
        if isinstance(route, APIRoute)
    )


PUBLIC_ENDPOINTS = _endpoints(auth.router, health.router, showcase.router) - {
    auth.switch_active_org
}
DEVICE_ENDPOINTS: frozenset[Endpoint] = frozenset({coldchain.ingest})
ADMIN_TOKEN_ENDPOINTS: frozenset[Endpoint] = frozenset(
    {demo.reset, quality.reset, quality.delete_observation, ops.demo_contamination}
)


def _bearer(request: Request) -> HTTPAuthorizationCredentials | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token.strip())


def _anonymous_allowed(endpoint: Endpoint | None, request: Request) -> bool:
    if endpoint in PUBLIC_ENDPOINTS:
        return True
    if endpoint in DEVICE_ENDPOINTS:
        return bool(request.headers.get(coldchain.DEVICE_TOKEN_HEADER))
    if endpoint in ADMIN_TOKEN_ENDPOINTS:
        credential = _bearer(request)
        if credential is None:
            return False
        require_demo_reset(credential)
        return True
    return False


def require_login(request: Request) -> None:
    if not app_settings(request).saas_mode:
        return
    if _anonymous_allowed(request.scope.get("endpoint"), request):
        return
    principal = current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail=LOGIN_REQUIRED)
    tag_request(org_type=principal.org_type, sandbox=org_sandbox(principal.org_id))
