from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException
from sqlalchemy import and_, exists, func, or_, select, true

from freshswitch_backend.api.auth import AuthPrincipal, Principal
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import same_sandbox
from freshswitch_backend.persistence.tables import (
    LotRow,
    OrganizationRow,
    PlanForwarderRow,
    PlanRow,
    ShipperProfileRow,
)
from freshswitch_backend.planning.store import PlanSession, store

NOT_FOUND = "계획을 찾을 수 없습니다"


@dataclass(frozen=True)
class Viewer:
    org_type: str
    org_id: str
    shipper_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanAccess:
    session: PlanSession
    viewer: Viewer
    owner_org_id: str = ""


def org_shipper_ids(org_id: str) -> tuple[str, ...]:
    with database.begin() as db:
        return tuple(
            db.scalars(
                select(ShipperProfileRow.id)
                .where(ShipperProfileRow.org_id == org_id)
                .order_by(ShipperProfileRow.created_at, ShipperProfileRow.id)
            )
        )


def viewer_of(principal: Principal) -> Viewer:
    shipper_ids = org_shipper_ids(principal.org_id) if principal.org_type == "shipper" else ()
    return Viewer(org_type=principal.org_type, org_id=principal.org_id, shipper_ids=shipper_ids)


def shipper_lot_included(org_id: str) -> Any:
    snapshot = PlanRow.payload["shipper_org_ids"].contains(func.jsonb_build_array(org_id))
    legacy = and_(
        ~PlanRow.payload.has_key("shipper_org_ids"),
        exists().where(
            LotRow.shipper_org_id == org_id,
            LotRow.created_at <= PlanRow.created_at,
            PlanRow.payload["registered_lot_ids"].contains(func.jsonb_build_array(LotRow.id)),
        ),
    )
    return or_(snapshot, legacy)


def forwarder_assigned(org_id: str) -> Any:
    return exists().where(
        PlanForwarderRow.plan_id == PlanRow.plan_id,
        PlanForwarderRow.forwarder_org_id == org_id,
    )


def owner_in_sandbox_of(org_id: str) -> Any:
    return exists().where(
        OrganizationRow.id == PlanRow.org_id, same_sandbox(OrganizationRow.sandbox, org_id)
    )


def _role_condition(principal: Principal) -> Any:
    if principal.org_type == "platform":
        return true()
    if principal.org_type == "forwarder":
        return or_(PlanRow.org_id == principal.org_id, forwarder_assigned(principal.org_id))
    if principal.org_type == "shipper":
        return shipper_lot_included(principal.org_id)
    return None


def visible_plans(principal: Principal) -> Any:
    condition = _role_condition(principal)
    if condition is None:
        return None
    return and_(condition, owner_in_sandbox_of(principal.org_id))


def _owner_if_visible(plan_id: str, principal: Principal) -> str | None:
    condition = visible_plans(principal)
    if condition is None:
        return None
    with database.begin() as db:
        return db.scalar(select(PlanRow.org_id).where(PlanRow.plan_id == plan_id, condition))


def plan_for(plan_id: str, principal: Principal) -> PlanAccess:
    owner = _owner_if_visible(plan_id, principal)
    if owner is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    session = store.get(plan_id, org_id=owner)
    if session is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return PlanAccess(session=session, viewer=viewer_of(principal), owner_org_id=owner)


def plan_access(plan_id: str, principal: AuthPrincipal) -> PlanAccess:
    return plan_for(plan_id, principal)


PlanAccessDep = Annotated[PlanAccess, Depends(plan_access)]
