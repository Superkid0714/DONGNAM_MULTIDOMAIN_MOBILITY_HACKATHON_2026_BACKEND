from datetime import datetime

from sqlalchemy import func, select

from freshswitch_backend.execution.card import EnteredNumbers
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import ExecutionNumbersRow

NUMBERS_LOCK = 7_310_211


class DuplicateContainerNo(Exception):
    def __init__(self, container_no: str, container_id: str) -> None:
        super().__init__(
            f"컨테이너 번호 {container_no}는 이 계획의 컨테이너 {container_id}에 이미 입력돼 있습니다"
        )


def _entered(row: ExecutionNumbersRow) -> EnteredNumbers:
    return EnteredNumbers(
        booking_no=row.booking_no,
        container_no=row.container_no,
        seal_no=row.seal_no,
        updated_at=row.updated_at,
    )


def entered_numbers(plan_id: str) -> dict[str, EnteredNumbers]:
    with database.begin() as db:
        rows = db.scalars(select(ExecutionNumbersRow).where(ExecutionNumbersRow.plan_id == plan_id))
        return {row.container_id: _entered(row) for row in rows}


def save_numbers(
    plan_id: str,
    container_id: str,
    changes: dict[str, str | None],
    active_container_ids: set[str],
    user_id: str | None,
    now: datetime,
) -> EnteredNumbers:
    with database.begin() as db:
        db.execute(select(func.pg_advisory_xact_lock(NUMBERS_LOCK, func.hashtext(plan_id))))
        number = changes.get("container_no")
        if number is not None:
            taken = db.scalar(
                select(ExecutionNumbersRow.container_id).where(
                    ExecutionNumbersRow.plan_id == plan_id,
                    ExecutionNumbersRow.container_no == number,
                    ExecutionNumbersRow.container_id != container_id,
                    ExecutionNumbersRow.container_id.in_(active_container_ids),
                )
            )
            if taken is not None:
                raise DuplicateContainerNo(number, taken)
        row = db.get(ExecutionNumbersRow, (plan_id, container_id))
        if row is None:
            row = ExecutionNumbersRow(plan_id=plan_id, container_id=container_id)
            db.add(row)
        for name, value in changes.items():
            setattr(row, name, value)
        row.updated_by = user_id
        row.updated_at = now
        db.flush()
        return _entered(row)
