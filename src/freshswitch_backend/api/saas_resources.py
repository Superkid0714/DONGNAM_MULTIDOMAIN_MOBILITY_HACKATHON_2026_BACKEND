import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy import select

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.resources import (
    ResourceCreate,
    ResourceOptions,
    ResourceOut,
    ResourcePatch,
)
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.resources import (
    ensure_sandbox_resources,
    resource_out,
    resource_query,
)
from freshswitch_backend.persistence.tables import ForwarderResourceRow, OrganizationRow

router = APIRouter(prefix="/saas", tags=["saas-resources"])
ForwarderPrincipal = Annotated[Principal, Depends(require_role("forwarder"))]
PlatformPrincipal = Annotated[Principal, Depends(require_role("platform"))]
ResourcePrincipal = Annotated[Principal, Depends(require_role("forwarder", "platform"))]


def _check_ports(body: ResourceCreate) -> None:
    scenario = get_scenario("default")
    known = {p.id for p in scenario.ports} if scenario else set()
    if body.origin not in known or body.destination not in known:
        raise HTTPException(status_code=422, detail="지원하는 출발항·도착항을 선택하세요")


def _owner(db, principal: Principal) -> OrganizationRow:
    org = db.scalar(
        select(OrganizationRow)
        .where(
            OrganizationRow.id == principal.org_id,
            OrganizationRow.type == "forwarder",
        )
        .with_for_update()
    )
    if org is None:
        raise HTTPException(status_code=404, detail="포워더 조직을 찾을 수 없습니다")
    return org


def _resource(db, resource_id: str, org_id: str) -> ForwarderResourceRow:
    row = db.scalar(
        select(ForwarderResourceRow)
        .where(
            ForwarderResourceRow.id == resource_id,
            ForwarderResourceRow.org_id == org_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="컨테이너를 찾을 수 없습니다")
    if row.allocated_plan_id is not None:
        raise HTTPException(
            status_code=409, detail="계획에 배정된 컨테이너는 수정·삭제할 수 없습니다"
        )
    return row


@router.get("/resources/options")
def options(principal: ResourcePrincipal) -> ResourceOptions:
    scenario = get_scenario("default")
    return ResourceOptions(
        ports=list(scenario.ports) if scenario else [],
        container_kinds=["40RH-CA", "40RH", "20RF"],
    )


@router.get("/forwarder/resources")
def list_own_resources(principal: ForwarderPrincipal) -> list[ResourceOut]:
    with database.begin() as db:
        ensure_sandbox_resources(db, db.get(OrganizationRow, principal.org_id))
        return [
            resource_out(db, row, name)
            for row, name in db.execute(resource_query(principal.org_id, own=True)).all()
        ]


@router.get("/platform/resources")
def list_available_resources(principal: PlatformPrincipal) -> list[ResourceOut]:
    with database.begin() as db:
        ensure_sandbox_resources(db, db.get(OrganizationRow, principal.org_id))
        return [
            resource_out(db, row, name)
            for row, name in db.execute(resource_query(principal.org_id)).all()
        ]


@router.post("/forwarder/resources", status_code=status.HTTP_201_CREATED)
def create_resource(body: ResourceCreate, principal: ForwarderPrincipal) -> ResourceOut:
    _check_ports(body)
    with database.begin() as db:
        org = _owner(db, principal)
        row = ForwarderResourceRow(
            id=f"fr-{uuid.uuid4().hex[:24]}",
            org_id=org.id,
            data={**body.model_dump(mode="json"), "source": "포워더 등록 · 실제 예약 별도"},
        )
        db.add(row)
        db.flush()
        return resource_out(db, row, org.name)


@router.patch("/forwarder/resources/{resource_id}")
def update_resource(
    resource_id: str,
    body: ResourcePatch,
    principal: ForwarderPrincipal,
) -> ResourceOut:
    with database.begin() as db:
        org = _owner(db, principal)
        row = _resource(db, resource_id, org.id)
        merged = {k: v for k, v in row.data.items() if k != "source"}
        merged.update(body.model_dump(mode="json", exclude_unset=True))
        try:
            checked = ResourceCreate.model_validate(merged)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=[
                    {"loc": ["body", *e["loc"]], "msg": e["msg"], "type": e["type"]}
                    for e in exc.errors()
                ],
            ) from exc
        _check_ports(checked)
        row.data = {
            **checked.model_dump(mode="json"),
            "source": row.data.get("source", "포워더 등록 · 실제 예약 별도"),
        }
        row.revision += 1
        db.flush()
        return resource_out(db, row, org.name)


@router.delete("/forwarder/resources/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resource(resource_id: str, principal: ForwarderPrincipal) -> Response:
    with database.begin() as db:
        org = _owner(db, principal)
        row = _resource(db, resource_id, org.id)
        db.delete(row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
