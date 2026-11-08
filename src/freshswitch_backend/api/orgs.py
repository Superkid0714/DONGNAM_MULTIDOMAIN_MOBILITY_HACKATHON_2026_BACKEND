from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, aliased

from freshswitch_backend.api.auth import AuthPrincipal, Principal, require_role
from freshswitch_backend.domain.inputs import StorableStr, storable
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import create_invitation, same_sandbox
from freshswitch_backend.persistence.sandbox_world import SEED_ID
from freshswitch_backend.persistence.tables import (
    MembershipRow,
    OrganizationRow,
    OrgInvitationRow,
    PlanForwarderRow,
    PlanRow,
    ShipperProfileRow,
    UserRow,
)
from freshswitch_backend.planning.store import awaiting_shippers

ListedOrgType = Literal["shipper", "forwarder"]
MemberRole = Literal["admin", "member"]
InvitationStatus = Literal["pending", "used", "expired"]
ADMIN_ONLY = "조직 관리자만 할 수 있습니다"
SANDBOX_INVITE = "데모(sandbox) 조직에는 초대할 수 없습니다"
LISTED_TYPES: tuple[str, ...] = ("shipper", "forwarder")

router = APIRouter(prefix="/saas", tags=["saas-orgs"])

PlatformPrincipal = Annotated[Principal, Depends(require_role("platform"))]


class OrgSummary(BaseModel):
    org_id: str
    org_type: str
    org_name: str


class OrgUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> object:
        value = storable(value)
        return value.strip() if isinstance(value, str) else value


@router.patch("/org")
def update_org(body: OrgUpdate, principal: AuthPrincipal) -> OrgSummary:
    if principal.role not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="업체 관리자만 할 수 있습니다")
    with database.begin() as db:
        org = db.scalar(
            select(OrganizationRow).where(OrganizationRow.id == principal.org_id).with_for_update()
        )
        if org is None:
            raise HTTPException(status_code=404, detail="업체를 찾을 수 없습니다")
        if org.type == "demo" or (org.sandbox and org.world_id in {None, SEED_ID}):
            raise HTTPException(
                status_code=403, detail="공용 데모 업체의 이름은 수정할 수 없습니다"
            )
        org.name = body.name
        if org.type == "shipper":
            db.execute(
                update(ShipperProfileRow)
                .where(ShipperProfileRow.org_id == org.id)
                .values(name=body.name)
            )
        return OrgSummary(org_id=org.id, org_type=org.type, org_name=org.name)


class ForwarderRef(BaseModel):
    org_id: str
    org_name: str


class ForwarderAssignRequest(BaseModel):
    forwarder_org_id: StorableStr = Field(min_length=1, max_length=64)


class ForwarderAssignment(BaseModel):
    plan_id: str
    forwarder: ForwarderRef
    before_shipper_confirmation: bool = False


ASSIGN_BEFORE_CONFIRMATION = "화주 확인이 끝난 뒤 배정할 수 있습니다"


@router.get("/orgs")
def list_orgs(
    principal: PlatformPrincipal,
    org_type: Annotated[ListedOrgType | None, Query(alias="type")] = None,
) -> list[OrgSummary]:
    types = (org_type,) if org_type else LISTED_TYPES
    with database.begin() as db:
        rows = db.execute(
            select(OrganizationRow.id, OrganizationRow.type, OrganizationRow.name)
            .where(
                OrganizationRow.type.in_(types),
                same_sandbox(OrganizationRow.sandbox, principal.org_id),
            )
            .order_by(OrganizationRow.type, OrganizationRow.name, OrganizationRow.id)
        ).all()
    return [OrgSummary(org_id=oid, org_type=kind, org_name=name) for oid, kind, name in rows]


def _lock_own_plan(db: Session, plan_id: str, org_id: str) -> PlanRow:
    found = db.scalar(
        select(PlanRow)
        .where(PlanRow.plan_id == plan_id, PlanRow.org_id == org_id)
        .with_for_update()
    )
    if found is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    return found


def _same_forwarder_world(platform: OrganizationRow, forwarder: OrganizationRow) -> bool:
    if platform.world_id and forwarder.world_id:
        return platform.world_id == forwarder.world_id and platform.sandbox and forwarder.sandbox
    return (
        platform.world_id is None
        and forwarder.world_id is None
        and not platform.sandbox
        and not forwarder.sandbox
    )


@router.put("/platform/plans/{plan_id}/forwarder")
def assign_forwarder(
    plan_id: str, body: ForwarderAssignRequest, principal: PlatformPrincipal
) -> ForwarderAssignment:
    with database.begin() as db:
        plan = _lock_own_plan(db, plan_id, principal.org_id)
        forwarder = db.get(OrganizationRow, body.forwarder_org_id)
        platform = db.get(OrganizationRow, principal.org_id)
        if (
            forwarder is None
            or forwarder.type != "forwarder"
            or platform is None
            or not _same_forwarder_world(platform, forwarder)
        ):
            raise HTTPException(status_code=422, detail="포워더 조직이 아닙니다")
        from freshswitch_backend.persistence.tables import ForwarderResourceRow

        resource_owners = set(
            db.scalars(
                select(ForwarderResourceRow.org_id).where(
                    ForwarderResourceRow.allocated_plan_id == plan_id,
                )
            )
        )
        if resource_owners and resource_owners != {forwarder.id}:
            raise HTTPException(
                status_code=422, detail="배정된 컨테이너를 제공한 포워더를 선택하세요"
            )
        awaiting = bool(awaiting_shippers(db, plan))
        if awaiting and not platform.sandbox:
            raise HTTPException(status_code=409, detail=ASSIGN_BEFORE_CONFIRMATION)
        db.execute(delete(PlanForwarderRow).where(PlanForwarderRow.plan_id == plan_id))
        db.add(
            PlanForwarderRow(
                plan_id=plan_id,
                forwarder_org_id=forwarder.id,
                assigned_by=principal.user_id,
            )
        )
        return ForwarderAssignment(
            plan_id=plan_id,
            forwarder=ForwarderRef(org_id=forwarder.id, org_name=forwarder.name),
            before_shipper_confirmation=awaiting,
        )


@router.delete("/platform/plans/{plan_id}/forwarder", status_code=status.HTTP_204_NO_CONTENT)
def unassign_forwarder(plan_id: str, principal: PlatformPrincipal) -> Response:
    with database.begin() as db:
        _lock_own_plan(db, plan_id, principal.org_id)
        db.execute(delete(PlanForwarderRow).where(PlanForwarderRow.plan_id == plan_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class OrgMember(BaseModel):
    user_id: str
    name: str
    email: str
    role: str
    joined_at: datetime


class InvitationCreate(BaseModel):
    role: MemberRole = "member"


class MemberRef(BaseModel):
    user_id: str
    name: str


class Invitation(BaseModel):
    invitation_id: str
    org_id: str
    role: str
    status: InvitationStatus
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    used_by: MemberRef | None = None


class InvitationCreated(Invitation):
    code: str
    org_name: str


def _require_admin(principal: Principal) -> None:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail=ADMIN_ONLY)


def _status(row: OrgInvitationRow, now: datetime) -> InvitationStatus:
    if row.used_at is not None:
        return "used"
    return "expired" if row.expires_at <= now else "pending"


def _invitation(row: OrgInvitationRow, used_name: str | None, now: datetime) -> Invitation:
    used_by = MemberRef(user_id=row.used_by, name=used_name or "") if row.used_by else None
    return Invitation(
        invitation_id=row.id,
        org_id=row.org_id,
        role=row.role,
        status=_status(row, now),
        created_at=row.created_at,
        expires_at=row.expires_at,
        used_at=row.used_at,
        used_by=used_by,
    )


@router.get("/org/members")
def list_members(principal: AuthPrincipal) -> list[OrgMember]:
    with database.begin() as db:
        rows = db.execute(
            select(
                UserRow.id,
                UserRow.name,
                UserRow.email,
                MembershipRow.role,
                MembershipRow.created_at,
            )
            .join(MembershipRow, MembershipRow.user_id == UserRow.id)
            .where(MembershipRow.org_id == principal.org_id)
            .order_by(MembershipRow.created_at, UserRow.id)
        ).all()
    return [
        OrgMember(user_id=uid, name=name, email=email, role=role, joined_at=joined)
        for uid, name, email, role, joined in rows
    ]


@router.post("/org/invitations", status_code=status.HTTP_201_CREATED)
def create_org_invitation(body: InvitationCreate, principal: AuthPrincipal) -> InvitationCreated:
    _require_admin(principal)
    now = datetime.now(UTC)
    with database.begin() as db:
        org = db.get(OrganizationRow, principal.org_id)
        if org is None or org.sandbox or org.type == "demo":
            raise HTTPException(status_code=403, detail=SANDBOX_INVITE)
        row, code = create_invitation(db, org.id, body.role, principal.user_id, now)
        return InvitationCreated(
            **_invitation(row, None, now).model_dump(), code=code, org_name=org.name
        )


@router.get("/org/invitations")
def list_org_invitations(principal: AuthPrincipal) -> list[Invitation]:
    _require_admin(principal)
    now = datetime.now(UTC)
    used = aliased(UserRow)
    with database.begin() as db:
        rows = db.execute(
            select(OrgInvitationRow, used.name)
            .outerjoin(used, used.id == OrgInvitationRow.used_by)
            .where(OrgInvitationRow.org_id == principal.org_id)
            .order_by(OrgInvitationRow.created_at.desc(), OrgInvitationRow.id)
        ).all()
    return [_invitation(row, name, now) for row, name in rows]


@router.delete("/org/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_org_invitation(invitation_id: str, principal: AuthPrincipal) -> Response:
    _require_admin(principal)
    with database.begin() as db:
        deleted = db.execute(
            delete(OrgInvitationRow).where(
                OrgInvitationRow.id == invitation_id,
                OrgInvitationRow.org_id == principal.org_id,
            )
        )
    if not deleted.rowcount:
        raise HTTPException(status_code=404, detail="초대 코드를 찾을 수 없습니다")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
