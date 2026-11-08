import argparse
import asyncio
import re
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from freshswitch_backend.config import get_settings
from freshswitch_backend.integrations.mof_snapshot import SNAPSHOT_ROOT, SnapshotStore
from freshswitch_backend.integrations.mof_vessel import (
    KST,
    MofVesselClient,
    VesselMovement,
    normalize_vessel_name,
)

HISTORY_PATH = SNAPSHOT_ROOT.parent / "history" / "mof-busan-departures.json"
MAX_STAY_DAYS = 10
MIN_DELAY_HOURS = -48.0
MAX_DELAY_HOURS = 120.0
OPERATOR_PREFIXES = (
    "CMA CGM",
    "MAERSK",
    "MSC",
    "HMM",
    "HYUNDAI",
    "EVER",
    "COSCO",
    "CSCL",
    "OOCL",
    "ONE",
    "YM",
    "SM",
    "KMTC",
    "SINOKOR",
    "HEUNG-A",
    "PANCON",
    "NAMSUNG",
    "DONGJIN",
    "WAN HAI",
    "ZIM",
    "APL",
    "X-PRESS",
    "TS",
    "SITC",
    "KOTA",
    "NYK",
    "MOL",
    "HAPAG",
    "CAPE",
    "STARSHIP",
)
_BERTH_SUFFIX = re.compile(r"\s*\d+\s*선석.*$")
MIN_GROSS_TONNAGE = 1000.0


class DepartureRecord(BaseModel):
    call_sign: str
    vessel_name: str | None = None
    vessel_kind: str | None = None
    purpose: str | None = None
    terminal: str | None = None
    berth: str | None = None
    gross_tonnage: float | None = None
    planned_departure_at: datetime
    departure_at: datetime
    delay_hours: float

    @property
    def operator(self) -> str | None:
        return operator_of(self.vessel_name)

    @property
    def weekday(self) -> int:
        return self.planned_departure_at.astimezone(KST).weekday()

    @property
    def is_container(self) -> bool:
        return "컨테이너" in (self.vessel_kind or "")

    @property
    def key(self) -> tuple[str, str]:
        return self.call_sign, self.planned_departure_at.isoformat()


class DepartureHistory(BaseModel):
    source: str = "해수부 선박입출항(VsslEtrynd5) 부산항 출항 신고: 출항예정일시 대비 출항일시"
    updated_at: datetime
    start: date | None = None
    end: date | None = None
    count: int
    records: list[DepartureRecord]


def operator_of(vessel_name: str | None) -> str | None:
    if not vessel_name:
        return None
    upper = " ".join(vessel_name.upper().split())
    for prefix in OPERATOR_PREFIXES:
        if upper == prefix or upper.startswith((prefix + " ", prefix + "-")):
            return prefix
    return None


def terminal_of(berth: str | None) -> str | None:
    if not berth:
        return None
    text = " ".join(berth.split())
    text = text.removeprefix("부산").strip()
    return _BERTH_SUFFIX.sub("", text) or None


def to_record(movement: VesselMovement) -> DepartureRecord | None:
    planned, actual = movement.planned_departure_at, movement.departure_at
    if planned is None or actual is None or not movement.call_sign:
        return None
    if movement.gross_tonnage is not None and movement.gross_tonnage < MIN_GROSS_TONNAGE:
        return None
    if movement.arrival_at is not None and actual - movement.arrival_at > timedelta(
        days=MAX_STAY_DAYS
    ):
        return None
    delay = (actual - planned).total_seconds() / 3600
    if not MIN_DELAY_HOURS <= delay <= MAX_DELAY_HOURS:
        return None
    return DepartureRecord(
        call_sign=movement.call_sign,
        vessel_name=movement.vessel_name,
        vessel_kind=movement.vessel_kind,
        purpose=movement.purpose,
        terminal=terminal_of(movement.departure_berth or movement.arrival_berth),
        berth=movement.departure_berth or movement.arrival_berth,
        gross_tonnage=movement.gross_tonnage,
        planned_departure_at=planned,
        departure_at=actual,
        delay_hours=round(delay, 2),
    )


def to_records(movements: Iterable[VesselMovement]) -> list[DepartureRecord]:
    return [r for m in movements if (r := to_record(m)) is not None]


def merge(
    existing: Iterable[DepartureRecord], new: Iterable[DepartureRecord]
) -> list[DepartureRecord]:
    merged = {r.key: r for r in existing}
    merged.update({r.key: r for r in new})
    return sorted(merged.values(), key=lambda r: (r.departure_at, r.call_sign))


def load_history(path: Path = HISTORY_PATH) -> DepartureHistory | None:
    if not path.is_file():
        return None
    return DepartureHistory.model_validate_json(path.read_text(encoding="utf-8"))


def save_history(records: list[DepartureRecord], path: Path = HISTORY_PATH) -> DepartureHistory:
    days = [r.departure_at.astimezone(KST).date() for r in records]
    history = DepartureHistory(
        updated_at=datetime.now(UTC),
        start=min(days, default=None),
        end=max(days, default=None),
        count=len(records),
        records=records,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(history.model_dump_json(exclude_none=True) + "\n", encoding="utf-8")
    return history


def snapshot_records(store: SnapshotStore | None = None) -> list[DepartureRecord]:
    store = store or SnapshotStore()
    records: list[DepartureRecord] = []
    for day, directions in store.index().items():
        if "O" in directions and (snapshot := store.load(day, "O")) is not None:
            records.extend(to_records(snapshot.movements))
    return records


def all_records(
    path: Path = HISTORY_PATH, store: SnapshotStore | None = None
) -> list[DepartureRecord]:
    history = load_history(path)
    return merge(history.records if history else [], snapshot_records(store))


def vessel_key(name: str | None, call_sign: str | None = None) -> str | None:
    if name:
        return normalize_vessel_name(name)
    return call_sign or None


async def fetch(start: date, end: date, client: MofVesselClient) -> list[DepartureRecord]:
    records: list[DepartureRecord] = []
    async with client:
        day = start
        while day <= end:
            movements = await client.fetch_movements(day, day, direction="O")
            got = to_records(movements)
            records.extend(got)
            print(f"{day:%Y-%m-%d} 출항 {len(movements):4d}척, 지연 표본 {len(got):4d}건")
            day += timedelta(days=1)
    return records


def _day(text: str) -> date:
    return datetime.strptime(text, "%Y%m%d").date()


def main(argv: list[str] | None = None) -> int:
    today = datetime.now(KST).date()
    parser = argparse.ArgumentParser(description="해수부 부산항 출항 지연 이력 누적")
    parser.add_argument("--start", type=_day, default=today - timedelta(days=30))
    parser.add_argument("--end", type=_day, default=today - timedelta(days=1))
    parser.add_argument("--snapshots-only", action="store_true")
    args = parser.parse_args(argv)
    history = load_history()
    existing = history.records if history else []
    new = snapshot_records()
    if not args.snapshots_only:
        key = get_settings().data_go_kr_service_key
        if not key:
            print("DATA_GO_KR_SERVICE_KEY가 비어 있습니다", file=sys.stderr)
            return 1
        new += asyncio.run(fetch(args.start, args.end, MofVesselClient(key, timeout=30)))
    saved = save_history(merge(existing, new))
    print(f"이력 {saved.count}건 ({saved.start} ~ {saved.end}) -> {HISTORY_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
