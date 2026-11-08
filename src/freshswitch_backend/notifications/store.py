import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from freshswitch_backend.notifications.models import MOCK_CHANNEL, Notification, NotificationKind
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import same_sandbox
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    LotRow,
    NotificationRow,
    OrganizationRow,
    ShipperProfileRow,
)


@dataclass(frozen=True)
class Draft:
    org_id: str
    kind: NotificationKind
    title: str
    body: str
    link: str
    dedupe_key: str
    shipper_id: str | None = None
    plan_id: str | None = None
    order_id: str | None = None
    change_request_id: str | None = None


def _model(row: NotificationRow) -> Notification:
    return Notification(
        notification_id=row.id,
        kind=row.kind,
        channel=row.channel,
        shipper_id=row.shipper_id,
        plan_id=row.plan_id,
        order_id=row.order_id,
        change_request_id=row.change_request_id,
        title=row.title,
        body=row.body,
        link=row.link,
        created_at=row.created_at,
    )


def shipper_orgs(shipper_ids: Iterable[str], owner_org_id: str) -> dict[str, str]:
    ids = sorted(set(shipper_ids))
    if not ids or owner_org_id == DEMO_ORG:
        return {}
    with database.begin() as db:
        rows = db.execute(
            select(ShipperProfileRow.id, ShipperProfileRow.org_id)
            .join(OrganizationRow, OrganizationRow.id == ShipperProfileRow.org_id)
            .where(
                ShipperProfileRow.id.in_(ids),
                OrganizationRow.type == "shipper",
                same_sandbox(OrganizationRow.sandbox, owner_org_id),
            )
        ).all()
    return {shipper_id: org_id for shipper_id, org_id in rows}


def lot_shipper(lot_id: str | None) -> str | None:
    if lot_id is None:
        return None
    with database.begin() as db:
        return db.scalar(select(LotRow.shipper_id).where(LotRow.id == lot_id))


def record(drafts: Iterable[Draft], now: datetime) -> list[Notification]:
    created: list[Notification] = []
    with database.begin() as db:
        for draft in drafts:
            statement = (
                insert(NotificationRow)
                .values(
                    id=str(uuid.uuid4()),
                    org_id=draft.org_id,
                    shipper_id=draft.shipper_id,
                    plan_id=draft.plan_id,
                    order_id=draft.order_id,
                    change_request_id=draft.change_request_id,
                    kind=draft.kind,
                    channel=MOCK_CHANNEL,
                    title=draft.title,
                    body=draft.body,
                    link=draft.link,
                    dedupe_key=draft.dedupe_key,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=[NotificationRow.dedupe_key])
                .returning(NotificationRow.id)
            )
            notification_id = db.scalar(statement)
            if notification_id is not None:
                created.append(_model(db.get_one(NotificationRow, notification_id)))
    return created


def list_for_org(
    org_id: str, plan_id: str | None = None, limit: int = 100, offset: int = 0
) -> list[Notification]:
    query = select(NotificationRow).where(NotificationRow.org_id == org_id)
    if plan_id is not None:
        query = query.where(NotificationRow.plan_id == plan_id)
    with database.begin() as db:
        rows = db.scalars(query.order_by(NotificationRow.seq.desc()).limit(limit).offset(offset))
        return [_model(row) for row in rows]
