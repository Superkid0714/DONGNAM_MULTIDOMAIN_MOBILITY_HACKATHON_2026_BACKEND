import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from freshswitch_backend.integrations.errors import public_error
from freshswitch_backend.integrations.mof_snapshot import (
    MovementSnapshot,
    SnapshotMissing,
    SnapshotStore,
    demo_movements,
)
from freshswitch_backend.integrations.mof_vessel import (
    KST,
    Direction,
    MofVesselApiError,
    VesselMovement,
)

LIVE_TIMEOUT_SECONDS = 8.0
LIVE_REQUEST_TIMEOUT_SECONDS = 4.0

MovementSource = Literal["live", "snapshot"]
DataSource = Literal["live", "snapshot", "provided", "sandbox_mix"]
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


class MovementOrigin(BaseModel):
    source: MovementSource
    fetched_at: datetime | None = None
    snapshot_at: datetime | None = None
    snapshot_names: list[str] = []
    fallback_reason: str | None = None
    offline_mode: bool = False
    demo_count: int = 0


class SourcedMovementFeed(BaseModel):
    feed: MovementFeed
    origin: MovementOrigin


class MovementProvenance(BaseModel):
    data_source: DataSource
    fetched_at: datetime | None = None
    snapshot_at: datetime | None = None
    snapshot_name: str | None = None
    demo_mixed: bool = False
    source_note: str


def _kst_label(value: datetime) -> str:
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def provided_provenance(count: int) -> MovementProvenance:
    return MovementProvenance(
        data_source="provided",
        source_note=f"요청 본문으로 받은 선박 입출항 실적 {count}건을 썼습니다",
    )


def origin_provenance(origin: MovementOrigin) -> MovementProvenance:
    if origin.source == "live" and origin.fetched_at is not None:
        base = f"해수부 선박 입출항 실시간 조회 ({_kst_label(origin.fetched_at)})"
    else:
        stems = ", ".join(name.removesuffix(".json") for name in origin.snapshot_names)
        cause = (
            "오프라인 모드"
            if origin.offline_mode
            else f"실시간 조회 실패: {origin.fallback_reason or '원인 미상'}"
        )
        collected = f"수집 {_kst_label(origin.snapshot_at)}, " if origin.snapshot_at else ""
        base = f"저장 스냅샷 {stems} ({collected}{cause})"
    mixed = origin.demo_count > 0
    return MovementProvenance(
        data_source="sandbox_mix" if mixed else origin.source,
        fetched_at=origin.fetched_at,
        snapshot_at=origin.snapshot_at,
        snapshot_name=", ".join(origin.snapshot_names) or None,
        demo_mixed=mixed,
        source_note=(
            f"시연용 선박 실적 {origin.demo_count}건을 섞은 결과입니다 (기반: {base})"
            if mixed
            else base
        ),
    )


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
    ) -> list[MovementSnapshot]:
        try:
            snapshots = self._store.select(start, end, direction)
        except SnapshotMissing as exc:
            raise MovementsUnavailable(str(exc)) from exc
        if not snapshots:
            raise MovementsUnavailable("저장된 해수부 스냅샷이 없습니다")
        return snapshots

    async def get(
        self, start: date, end: date, direction: Direction | None = None, demo: bool = False
    ) -> MovementFeed:
        return (await self.get_sourced(start, end, direction, demo=demo)).feed

    async def get_sourced(
        self, start: date, end: date, direction: Direction | None = None, demo: bool = False
    ) -> SourcedMovementFeed:
        use_demo = demo or self._offline
        reason: str | None = None
        source: MovementSource
        snapshot_dates: list[date] = []
        snapshot_names: list[str] = []
        live_at: datetime | None = None
        snapshot_at: datetime | None = None
        if self._offline:
            reason = "오프라인 모드"
        else:
            try:
                movements = await self._live(start, end, direction)
                source, live_at = "live", datetime.now(UTC)
                fetched_at = live_at
            except LIVE_ERRORS as exc:
                reason = _reason(exc)
        if reason is not None:
            try:
                snapshots = self._snapshot(start, end, direction)
            except MovementsUnavailable as exc:
                raise MovementsUnavailable(f"{reason}, {exc}") from exc
            movements = [m for s in snapshots for m in s.movements]
            snapshot_at = fetched_at = max(s.fetched_at for s in snapshots)
            snapshot_dates = sorted({s.date for s in snapshots})
            snapshot_names = [self._store.path(s.date, s.direction).name for s in snapshots]
            source = "snapshot"
        extra = demo_movements(start, end, direction) if use_demo else []
        movements = [*movements, *extra]
        feed = MovementFeed(
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
        return SourcedMovementFeed(
            feed=feed,
            origin=MovementOrigin(
                source=source,
                fetched_at=live_at,
                snapshot_at=snapshot_at,
                snapshot_names=snapshot_names,
                fallback_reason=feed.fallback_reason,
                offline_mode=self._offline,
                demo_count=len(extra),
            ),
        )
