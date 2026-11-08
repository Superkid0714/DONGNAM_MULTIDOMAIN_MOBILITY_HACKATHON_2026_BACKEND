from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import same_sandbox
from freshswitch_backend.persistence.tables import (
    LotRow,
    MembershipRow,
    OrganizationRow,
    PlanRow,
    UserRow,
)

router = APIRouter(prefix="/saas/admin", tags=["saas-admin"])
AdminPrincipal = Annotated[Principal, Depends(require_role("platform"))]


class AdminCounts(BaseModel):
    companies: int
    users: int
    shipments: int
    plans: int


class AdminCompany(BaseModel):
    id: str
    name: str
    type: str
    sandbox: bool
    members_count: int


class AdminMembership(BaseModel):
    org_id: str
    org_name: str
    role: str


class AdminUser(BaseModel):
    id: str
    name: str
    email: str
    memberships: list[AdminMembership]


class AdminOverview(BaseModel):
    counts: AdminCounts
    companies: list[AdminCompany]
    users: list[AdminUser]


@router.get("/overview")
def overview(principal: AdminPrincipal) -> AdminOverview:
    with database.begin() as db:
        orgs = list(
            db.scalars(
                select(OrganizationRow)
                .where(
                    same_sandbox(OrganizationRow.sandbox, principal.org_id),
                )
                .order_by(OrganizationRow.type, OrganizationRow.name, OrganizationRow.id)
            )
        )
        org_ids = [org.id for org in orgs]
        memberships = db.execute(
            select(UserRow, MembershipRow)
            .join(
                MembershipRow,
                MembershipRow.user_id == UserRow.id,
            )
            .where(MembershipRow.org_id.in_(org_ids))
            .order_by(UserRow.created_at, UserRow.id)
        ).all()
        users: dict[str, AdminUser] = {}
        member_counts: dict[str, int] = {}
        names = {org.id: org.name for org in orgs}
        for user, membership in memberships:
            if user.id not in users:
                users[user.id] = AdminUser(
                    id=user.id, name=user.name, email=user.email, memberships=[]
                )
            users[user.id].memberships.append(
                AdminMembership(
                    org_id=membership.org_id,
                    org_name=names[membership.org_id],
                    role=membership.role,
                )
            )
            member_counts[membership.org_id] = member_counts.get(membership.org_id, 0) + 1
        companies = [
            AdminCompany(
                id=org.id,
                name=org.name,
                type=org.type,
                sandbox=org.sandbox,
                members_count=member_counts.get(org.id, 0),
            )
            for org in orgs
        ]
        return AdminOverview(
            counts=AdminCounts(
                companies=len(companies),
                users=len(users),
                shipments=db.scalar(
                    select(func.count())
                    .select_from(LotRow)
                    .where(LotRow.shipper_org_id.in_(org_ids))
                )
                or 0,
                plans=db.scalar(
                    select(func.count()).select_from(PlanRow).where(PlanRow.org_id.in_(org_ids))
                )
                or 0,
            ),
            companies=companies,
            users=list(users.values()),
        )
