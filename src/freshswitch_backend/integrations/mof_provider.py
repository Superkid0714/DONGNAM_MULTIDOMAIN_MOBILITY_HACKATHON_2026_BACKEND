import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from freshswitch_backend.integrations.errors import public_error
from freshswitch_backend.integrations.mof_snapshot import (
    SnapshotMissing,
    SnapshotStore,
    demo_movements,
)
from freshswitch_backend.integrations.mof_vessel import Direction, MofVesselApiError, VesselMovement

LIVE_TIMEOUT_SECONDS = 8.0
LIVE_REQUEST_TIMEOUT_SECONDS = 4.0

MovementSource = Literal["live", "snapshot"]
ClientFactory = Callable[[], Any]


class MovementsUnavailable(Exception):
    pass


class MissingServiceKey(Exception):
    def __init__(self) -> None:
        super().__init__("해수부 API 키(DATA_GO_KR_SERVICE_KEY)가 설정되지 않았습니다")


LIVE_ERRORS = (
    MissingServiceKey,
    MofVesselApiError,
    httpx.HTTPError,
    TimeoutError,
    OSError,
    ValueError,
)


class MovementFeed(BaseModel):
    source: MovementSource
    fetched_at: datetime
    start: date
    end: date
    direction: Direction | None
    offline_mode: bool
    demo: bool
    snapshot_dates: list[date] = []
    fallback_reason: str | None = None
    movement_count: int
    movements: list[VesselMovement]


def _reason(exc: BaseException) -> str:
    if isinstance(exc, MissingServiceKey):
        return "해수부 API 키가 설정되지 않았습니다"
    return public_error(exc, "해수부 API")


class MovementProvider:
    def __init__(
        self,
        client_factory: ClientFactory | None,
        offline_mode: bool = False,
        store: SnapshotStore | None = None,
        live_timeout: float = LIVE_TIMEOUT_SECONDS,
    ) -> None:
        self._factory = client_factory
        self._offline = offline_mode
        self._store = store or SnapshotStore()
        self._timeout = live_timeout

    async def _live(
        self, start: date, end: date, direction: Direction | None
    ) -> list[VesselMovement]:
        if self._factory is None:
            raise MissingServiceKey()
        client = self._factory()
        async with client:
            return await asyncio.wait_for(
                client.fetch_movements(start, end, direction=direction), self._timeout
            )

    def _snapshot(
        self, start: date, end: date, direction: Direction | None
    ) -> tuple[list[VesselMovement], datetime, list[date]]:
        try:
            snapshots = self._store.select(start, end, direction)
        except SnapshotMissing as exc:
            raise MovementsUnavailable(str(exc)) from exc
        if not snapshots:
            raise MovementsUnavailable("저장된 해수부 스냅샷이 없습니다")
        movements = [m for s in snapshots for m in s.movements]
        fetched_at = max(s.fetched_at for s in snapshots)
        return movements, fetched_at, sorted({s.date for s in snapshots})

    async def get(
        self, start: date, end: date, direction: Direction | None = None, demo: bool = False
    ) -> MovementFeed:
        use_demo = demo or self._offline
        reason: str | None = None
        source: MovementSource
        snapshot_dates: list[date] = []
        if self._offline:
            reason = "오프라인 모드"
        else:
            try:
                movements = await self._live(start, end, direction)
                source, fetched_at = "live", datetime.now(UTC)
            except LIVE_ERRORS as exc:
                reason = _reason(exc)
        if reason is not None:
            try:
                movements, fetched_at, snapshot_dates = self._snapshot(start, end, direction)
            except MovementsUnavailable as exc:
                raise MovementsUnavailable(f"{reason}, {exc}") from exc
            source = "snapshot"
        if use_demo:
            movements = [*movements, *demo_movements(start, end, direction)]
        return MovementFeed(
            source=source,
            fetched_at=fetched_at,
            start=start,
            end=end,
            direction=direction,
            offline_mode=self._offline,
            demo=use_demo,
            snapshot_dates=snapshot_dates,
            fallback_reason=None if self._offline else reason,
            movement_count=len(movements),
            movements=movements,
        )
