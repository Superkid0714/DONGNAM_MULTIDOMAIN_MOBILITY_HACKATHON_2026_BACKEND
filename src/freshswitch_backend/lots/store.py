import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from freshswitch_backend.domain.models import Lot
from freshswitch_backend.lots.models import LotFilter, RegisteredLot
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.tables import LotRow, PlanRow

LOT_ID_LOCK = 7_310_001
PLAN_LOT_IDS = text(
    "SELECT DISTINCT lot->>'id' FROM plan_sessions, "
    "jsonb_array_elements(payload->'scenario'->'lots') AS lot"
)


class LotConflict(ValueError):
    pass


@dataclass
class Reservation:
    taken: set[str]
    reserved: set[str]


def mark_planned(db: Session, lot_ids: list[str], now: datetime) -> None:
    if lot_ids:
        db.execute(
            update(LotRow)
            .where(LotRow.id.in_(lot_ids), LotRow.status == "registered")
            .values(status="planned", updated_at=now)
        )


def release_lots(db: Session, lot_ids: set[str] | None, now: datetime) -> None:
    if lot_ids is not None and not lot_ids:
        return
    referenced = exists().where(
        PlanRow.payload["registered_lot_ids"].contains(func.jsonb_build_array(LotRow.id))
    )
    statement = update(LotRow).where(LotRow.status == "planned", ~referenced)
    if lot_ids is not None:
        statement = statement.where(LotRow.id.in_(lot_ids))
    db.execute(statement.values(status="registered", updated_at=now))


def _now() -> datetime:
    return datetime.now(UTC)


def _model(row: LotRow) -> RegisteredLot:
    return RegisteredLot.model_validate(row, from_attributes=True)


class LotStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db or database
        self._lock = threading.RLock()
        self.clock: Callable[[], datetime] = _now

    def _row(self, lot: Lot, scenario_id: str, packhouse: str | None) -> LotRow:
        now = self.clock()
        return LotRow(
            id=lot.id,
            scenario_id=scenario_id,
            shipper_id=lot.shipper_id,
            product_id=lot.product_id,
            pallets=lot.pallets,
            ready_date=lot.ready_date,
            destination=lot.destination,
            sell_by_date=lot.sell_by_date,
            sell_window=lot.sell_window,
            packhouse=packhouse,
            source=lot.source,
            status="registered",
            created_at=now,
            updated_at=now,
        )

    def _reservation(self, db: Session) -> Reservation:
        return Reservation(
            taken=set(db.scalars(select(LotRow.id))),
            reserved={i for i in db.scalars(PLAN_LOT_IDS) if i},
        )

    def reservation(self) -> Reservation:
        with self._db.begin() as db:
            return self._reservation(db)

    def create(self, build: Callable[[Reservation], tuple[Lot, str, str | None]]) -> RegisteredLot:
        try:
            with self._lock, self._db.begin() as db:
                db.execute(select(func.pg_advisory_xact_lock(LOT_ID_LOCK)))
                lot, scenario_id, packhouse = build(self._reservation(db))
                row = self._row(lot, scenario_id, packhouse)
                db.add(row)
                db.flush()
        except IntegrityError as exc:
            raise LotConflict(f"이미 있는 로트 ID입니다: {lot.id}") from exc
        return _model(row)

    def get(self, lot_id: str) -> RegisteredLot | None:
        with self._db.begin() as db:
            row = db.get(LotRow, lot_id)
            return _model(row) if row is not None else None

    def get_many(self, lot_ids: list[str]) -> list[RegisteredLot]:
        if not lot_ids:
            return []
        with self._db.begin() as db:
            rows = db.scalars(select(LotRow).where(LotRow.id.in_(lot_ids)))
            found = {row.id: _model(row) for row in rows}
        return [found[i] for i in lot_ids if i in found]

    def ids(self) -> set[str]:
        with self._db.begin() as db:
            return set(db.scalars(select(LotRow.id)))

    def search(self, query: LotFilter) -> list[RegisteredLot]:
        statement = select(LotRow)
        conditions = [
            (query.scenario_id, LotRow.scenario_id),
            (query.shipper, LotRow.shipper_id),
            (query.product, LotRow.product_id),
            (query.destination, LotRow.destination),
            (query.status, LotRow.status),
        ]
        for value, column in conditions:
            if value is not None:
                statement = statement.where(column == value)
        if query.ready_from is not None:
            statement = statement.where(LotRow.ready_date >= query.ready_from)
        if query.ready_to is not None:
            statement = statement.where(LotRow.ready_date <= query.ready_to)
        statement = (
            statement.order_by(LotRow.created_at.desc(), LotRow.id)
            .limit(query.limit)
            .offset(query.offset)
        )
        with self._db.begin() as db:
            return [_model(row) for row in db.scalars(statement)]

    def update(
        self, lot_id: str, change: Callable[[RegisteredLot], dict[str, Any]]
    ) -> RegisteredLot | None:
        with self._lock, self._db.begin() as db:
            row = db.get(LotRow, lot_id, with_for_update=True)
            if row is None:
                return None
            for name, value in change(_model(row)).items():
                setattr(row, name, value)
            row.updated_at = self.clock()
            db.flush()
            return _model(row)

    def delete(self, lot_id: str) -> bool:
        with self._lock, self._db.begin() as db:
            return db.execute(delete(LotRow).where(LotRow.id == lot_id)).rowcount > 0

    def clear(self) -> int:
        with self._lock, self._db.begin() as db:
            return db.execute(delete(LotRow)).rowcount


lot_store = LotStore()
