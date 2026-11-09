import uuid

from sqlalchemy import select

from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import OperationalQuoteRow
from freshswitch_backend.planning.operational_quote import OperationalQuote


def save_quote(org_id: str, actor_id: str, quote: OperationalQuote) -> dict:
    record_id = str(uuid.uuid4())
    with database.begin() as db:
        row = OperationalQuoteRow(
            id=record_id, org_id=org_id, actor_id=actor_id, payload=quote.model_dump(mode="json")
        )
        db.add(row)
        db.flush()
        return {"id": row.id, "created_at": row.created_at, "quote": row.payload}


def read_quote(org_id: str, record_id: str) -> dict | None:
    with database.begin() as db:
        row = db.scalar(
            select(OperationalQuoteRow).where(
                OperationalQuoteRow.id == record_id, OperationalQuoteRow.org_id == org_id
            )
        )
        if row is None:
            return None
        return {"id": row.id, "created_at": row.created_at, "quote": row.payload}
