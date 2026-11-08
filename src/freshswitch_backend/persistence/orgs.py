import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from freshswitch_backend.persistence.database import Database, database
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    MembershipRow,
    OrganizationRow,
    OrgInvitationRow,
)

DEMO_ORG_NAME = "시연"
INVITATION_TTL = timedelta(days=7)
INVALID_INVITATION = "초대 코드가 올바르지 않거나 만료되었습니다"
ALREADY_MEMBER = "이미 소속된 조직입니다"


class InvalidInvitation(ValueError):
    pass


class AlreadyMember(ValueError):
    pass


def insert_demo_org(db: Session) -> None:
    db.execute(
        insert(OrganizationRow)
        .values(id=DEMO_ORG, type="demo", name=DEMO_ORG_NAME)
        .on_conflict_do_nothing(index_elements=[OrganizationRow.id])
    )


def ensure_demo_org(target: Database | None = None) -> None:
    with (target or database).begin() as db:
        insert_demo_org(db)


def sandbox_of(org_id: str) -> Any:
    return select(OrganizationRow.sandbox).where(OrganizationRow.id == org_id).scalar_subquery()


def same_sandbox(column: Any, org_id: str) -> Any:
    world_id = column.table.c.world_id
    org_type = column.table.c.type
    viewer_world = (
        select(OrganizationRow.world_id).where(OrganizationRow.id == org_id).scalar_subquery()
    )
    return case(
        (viewer_world.is_not(None), world_id == viewer_world),
        else_=and_(world_id.is_(None), column.is_(False), org_type != "demo"),
    )


def org_sandbox(org_id: str, target: Database | None = None) -> bool:
    with (target or database).begin() as db:
        return bool(db.scalar(select(OrganizationRow.sandbox).where(OrganizationRow.id == org_id)))


def invitation_code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def create_invitation(
    db: Session, org_id: str, role: str, created_by: str | None, now: datetime | None = None
) -> tuple[OrgInvitationRow, str]:
    code = secrets.token_urlsafe(12)
    issued = now or datetime.now(UTC)
    row = OrgInvitationRow(
        id=f"inv-{uuid.uuid4().hex[:16]}",
        org_id=org_id,
        code_hash=invitation_code_hash(code),
        role=role,
        created_by=created_by,
        created_at=issued,
        expires_at=issued + INVITATION_TTL,
    )
    db.add(row)
    db.flush()
    return row, code


def usable_invitation(
    db: Session, code: str, now: datetime | None = None
) -> tuple[OrgInvitationRow, OrganizationRow]:
    moment = now or datetime.now(UTC)
    invitation = db.scalar(
        select(OrgInvitationRow)
        .where(OrgInvitationRow.code_hash == invitation_code_hash(code))
        .with_for_update()
    )
    if invitation is None or invitation.used_at is not None or invitation.expires_at <= moment:
        raise InvalidInvitation(INVALID_INVITATION)
    org = db.get(OrganizationRow, invitation.org_id)
    if org is None or org.sandbox or org.type == "demo":
        raise InvalidInvitation(INVALID_INVITATION)
    return invitation, org


def redeem_invitation(
    db: Session, code: str, user_id: str, now: datetime | None = None
) -> OrgInvitationRow:
    moment = now or datetime.now(UTC)
    invitation, org = usable_invitation(db, code, moment)
    inserted = db.scalar(
        insert(MembershipRow)
        .values(user_id=user_id, org_id=org.id, role=invitation.role, created_at=moment)
        .on_conflict_do_nothing(index_elements=[MembershipRow.user_id, MembershipRow.org_id])
        .returning(MembershipRow.user_id)
    )
    if inserted is None:
        raise AlreadyMember(ALREADY_MEMBER)
    invitation.used_by = user_id
    invitation.used_at = moment
    db.flush()
    return invitation
