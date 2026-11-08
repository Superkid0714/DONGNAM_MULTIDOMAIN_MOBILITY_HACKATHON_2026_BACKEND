from fastapi import APIRouter, HTTPException, Request, status

from freshswitch_backend.api.auth import AuthPrincipal, app_settings
from freshswitch_backend.coldchain.devices import (
    Device,
    DeviceConflict,
    DeviceIdError,
    DeviceRequest,
    RegisteredDevice,
    device_prefix_error,
    device_store,
    is_visitor_world,
    rewrite_registered_id,
)
from freshswitch_backend.persistence.orgs import org_sandbox

router = APIRouter(prefix="/saas/devices", tags=["saas-devices"])


@router.get("")
def list_devices(principal: AuthPrincipal) -> list[Device]:
    return device_store.for_org(principal.org_id)


@router.post("", status_code=status.HTTP_201_CREATED)
def register_device(
    body: DeviceRequest, request: Request, principal: AuthPrincipal
) -> RegisteredDevice:
    if app_settings(request).saas_mode and is_visitor_world(principal.world_id):
        try:
            device_id = rewrite_registered_id(body.device_id, principal.world_id)
        except DeviceIdError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if device_id != body.device_id:
            body = body.model_copy(update={"device_id": device_id})
    elif app_settings(request).saas_mode:
        reason = device_prefix_error(body.device_id, org_sandbox(principal.org_id))
        if reason is not None:
            raise HTTPException(status_code=422, detail=reason)
    try:
        return device_store.register(body, principal.org_id)
    except DeviceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(device_id: str, principal: AuthPrincipal) -> None:
    if not device_store.delete(device_id, principal.org_id):
        raise HTTPException(status_code=404, detail="장치를 찾을 수 없습니다")
