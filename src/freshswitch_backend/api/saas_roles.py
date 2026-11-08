from fastapi import HTTPException, Request

from freshswitch_backend.api.auth import app_settings, current_principal

OPERATIONAL_PREFIXES = (
    "/plans",
    "/intake",
    "/lots",
    "/quality",
    "/coldchain",
    "/saas/platform/plans",
    "/saas/platform/change-requests",
)


def _operational(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in OPERATIONAL_PREFIXES)


def platform_operational_guard(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if not _operational(request.url.path.removeprefix(app_settings(request).api_prefix)):
        return
    principal = current_principal(request)
    if principal is None or principal.org_type != "platform":
        return
    raise HTTPException(
        status_code=403,
        detail="플랫폼은 서비스 관리·조회만 할 수 있습니다. 운송 계획은 포워더가 관리합니다",
    )
