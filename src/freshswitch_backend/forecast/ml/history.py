from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from freshswitch_backend.integrations.mof_vessel import (
    KST,
    VesselMovement,
    normalize_call_sign,
)

logger = logging.getLogger(__name__)
HISTORY_CSV = (
    Path(__file__).resolve().parents[2] / "data" / "history" / "busan_container_voyages.csv"
)
LIVE_LOOKBACK_DAYS = 30
LIVE_FETCH_TIMEOUT_SECONDS = 8.0
MAX_VOYAGE_MATCH_GAP = timedelta(hours=72)


@dataclass(frozen=True)
class DelayModelData:
    history: Any
    movements: list[VesselMovement]
    source: str


def _pandas():
    import pandas as pd

    return pd


def _empty_history():
    return _pandas().DataFrame(
        columns=[
            "clsgn",
            "etryptDt",
            "tkoffDt",
            "delay_h",
            "satmntEntrpsNm",
            "laidupFcltyNm",
        ]
    )


def _csv_history(path: Path = HISTORY_CSV):
    if not path.is_file():
        logger.warning("출항 ML 이력 CSV가 없습니다: %s", path)
        return _empty_history()
    try:
        frame = _pandas().read_csv(path, low_memory=False)
    except Exception:
        logger.exception("출항 ML 이력 CSV를 읽을 수 없습니다: %s", path)
        return _empty_history()
    required = {"clsgn", "etryptDt", "tkoffDt", "delay_h", "satmntEntrpsNm"}
    missing = sorted(required - set(frame.columns))
    if missing:
        logger.error("출항 ML 이력 CSV 필수 컬럼 누락: %s", ", ".join(missing))
        return _empty_history()
    frame["etryptDt"] = _pandas().to_datetime(frame["etryptDt"], errors="coerce")
    frame["tkoffDt"] = _pandas().to_datetime(frame["tkoffDt"], errors="coerce")
    return frame


def _naive_kst(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(KST).replace(tzinfo=None)


def movements_to_history(movements: list[VesselMovement]):
    pd = _pandas()
    rows = []
    for movement in movements:
        if not movement.vessel_kind or "컨테이너" not in movement.vessel_kind:
            continue
        planned = movement.planned_departure_at
        actual = movement.departure_at
        delay_h = (
            (actual - planned).total_seconds() / 3600
            if actual is not None and planned is not None
            else None
        )
        rows.append(
            {
                "clsgn": movement.call_sign or None,
                "etryptDt": _naive_kst(movement.arrival_at),
                "tkoffDt": _naive_kst(actual),
                "delay_h": delay_h,
                "satmntEntrpsNm": None,
                "laidupFcltyNm": movement.departure_berth or movement.arrival_berth,
                "vsslNltyNm": movement.nationality,
                "prvsDpmprtPrtNm": movement.previous_port,
                "nxlnptPrtNm": movement.next_port,
                "grtg": movement.gross_tonnage,
                "ldadngTon": None,
                "landngFrghtTon": None,
                "trnpdtTon": None,
                "tugYn": None,
                "piltgYn": None,
            }
        )
    return pd.DataFrame(rows)


def merge_history(csv_history, movements: list[VesselMovement]):
    pd = _pandas()
    live = movements_to_history(movements)
    if live.empty:
        return csv_history
    merged = pd.concat([csv_history, live], ignore_index=True, sort=False)
    if {"clsgn", "etryptDt"}.issubset(merged.columns):
        merged = merged.drop_duplicates(subset=["clsgn", "etryptDt"], keep="last")
    return merged.reset_index(drop=True)


async def load_delay_model_data(
    client_factory,
    *,
    offline_mode: bool,
    now: datetime,
    csv_path: Path = HISTORY_CSV,
) -> DelayModelData:
    history = await asyncio.to_thread(_csv_history, csv_path)
    has_csv = csv_path.is_file() and not history.empty
    if offline_mode or client_factory is None:
        return DelayModelData(history, [], "csv" if has_csv else "unavailable")

    local_now = now.astimezone(KST)
    start = local_now.date() - timedelta(days=LIVE_LOOKBACK_DAYS)
    try:
        async with client_factory() as client:
            movements = await asyncio.wait_for(
                client.fetch_movements(start, local_now.date(), direction=None),
                timeout=LIVE_FETCH_TIMEOUT_SECONDS,
            )
        merged = merge_history(history, movements)
        source = "csv+live" if has_csv else ("live" if not merged.empty else "unavailable")
        return DelayModelData(merged, movements, source)
    except Exception:
        logger.exception("최근 부산항 ML 이력 조회 실패. CSV 이력으로 대체합니다")
        return DelayModelData(history, [], "csv" if has_csv else "unavailable")


def match_voyage_movements(voyages, movements: list[VesselMovement]) -> dict[str, VesselMovement]:
    by_call_sign: dict[str, list[VesselMovement]] = {}
    for movement in movements:
        if (
            movement.call_sign
            and movement.planned_departure_at
            and movement.vessel_kind
            and "컨테이너" in movement.vessel_kind
        ):
            key = normalize_call_sign(movement.call_sign)
            by_call_sign.setdefault(key, []).append(movement)

    matched: dict[str, VesselMovement] = {}
    for voyage in voyages:
        if not voyage.call_sign:
            continue
        candidates = by_call_sign.get(normalize_call_sign(voyage.call_sign), [])
        nearby = [
            movement
            for movement in candidates
            if abs(movement.planned_departure_at - voyage.etd) <= MAX_VOYAGE_MATCH_GAP
        ]
        if nearby:
            matched[voyage.id] = min(
                nearby,
                key=lambda movement: abs(movement.planned_departure_at - voyage.etd),
            )
    return matched
