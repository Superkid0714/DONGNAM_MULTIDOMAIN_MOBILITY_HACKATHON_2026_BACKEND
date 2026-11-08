from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from freshswitch_backend.config import get_settings
from freshswitch_backend.forecast.port_congestion import PortCongestion, build_indicator
from freshswitch_backend.integrations import (
    MissingServiceKey,
    MofVesselClient,
    MovementFeed,
    MovementProvider,
    MovementsUnavailable,
    SourcedMovementFeed,
    VesselMovement,
)
from freshswitch_backend.integrations.mof_provider import LIVE_REQUEST_TIMEOUT_SECONDS
from freshswitch_backend.integrations.mof_vessel import KST
from freshswitch_backend.integrations.ulsan_position import (
    LIVE_REQUEST_TIMEOUT_SECONDS as ULSAN_REQUEST_TIMEOUT_SECONDS,
)
from freshswitch_backend.integrations.ulsan_position import (
    MissingUlsanKey,
    PositionFeed,
    PositionProvider,
    PositionsUnavailable,
    UlsanPositionClient,
)

router = APIRouter(prefix="/vessels", tags=["vessels"])

VesselClientFactory = Callable[[], MofVesselClient]


def _default_factory() -> MofVesselClient:
    key = get_settings().data_go_kr_service_key
    if not key:
        raise MissingServiceKey()
    return MofVesselClient(key, timeout=LIVE_REQUEST_TIMEOUT_SECONDS)


def vessel_client_factory() -> VesselClientFactory:
    return _default_factory


def movement_provider(factory: VesselClientFactory) -> MovementProvider:
    return MovementProvider(factory, offline_mode=get_settings().offline_mode)


async def fetch_busan_sourced(
    factory: VesselClientFactory,
    start: date,
    end: date,
    direction: Literal["I", "O"] | None,
    demo: bool = False,
) -> SourcedMovementFeed:
    try:
        return await movement_provider(factory).get_sourced(start, end, direction, demo=demo)
    except MovementsUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"해수부 선박입출항 정보를 가져오지 못했습니다: {exc}"
        ) from exc


async def load_busan_feed(
    factory: VesselClientFactory,
    start: date,
    end: date,
    direction: Literal["I", "O"] | None,
    demo: bool = False,
) -> MovementFeed:
    return (await fetch_busan_sourced(factory, start, end, direction, demo=demo)).feed


async def fetch_busan_movements(
    factory: VesselClientFactory,
    start: date,
    end: date,
    direction: Literal["I", "O"] | None,
    demo: bool = False,
) -> list[VesselMovement]:
    return (await load_busan_feed(factory, start, end, direction, demo=demo)).movements


@router.get("/busan")
async def busan_movements(
    factory: Annotated[VesselClientFactory, Depends(vessel_client_factory)],
    date_str: Annotated[str | None, Query(alias="date", pattern=r"^\d{8}$")] = None,
    direction: Literal["I", "O"] | None = None,
    demo: bool = False,
) -> MovementFeed:
    try:
        day = datetime.strptime(date_str, "%Y%m%d").date() if date_str else datetime.now(KST).date()
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="date는 실제 존재하는 YYYYMMDD 날짜여야 합니다"
        ) from exc
    return await load_busan_feed(factory, day, day, direction, demo=demo)


UlsanClientFactory = Callable[[], UlsanPositionClient]


def _ulsan_factory() -> UlsanPositionClient:
    key = get_settings().data_go_kr_service_key
    if not key:
        raise MissingUlsanKey()
    return UlsanPositionClient(key, timeout=ULSAN_REQUEST_TIMEOUT_SECONDS)


def ulsan_client_factory() -> UlsanClientFactory:
    return _ulsan_factory


async def load_ulsan_feed(
    factory: UlsanClientFactory, call_sign: str | None = None
) -> PositionFeed:
    return await PositionProvider(factory, offline_mode=get_settings().offline_mode).get(call_sign)


async def load_ulsan_congestion(factory: UlsanClientFactory) -> PortCongestion:
    return build_indicator(await load_ulsan_feed(factory))


@router.get("/ulsan")
async def ulsan_positions(
    factory: Annotated[UlsanClientFactory, Depends(ulsan_client_factory)],
    call_sign: str | None = None,
) -> PositionFeed:
    try:
        return await load_ulsan_feed(factory, call_sign)
    except PositionsUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"울산항 선박위치 정보를 가져오지 못했습니다: {exc}"
        ) from exc


@router.get("/ulsan/congestion")
async def ulsan_congestion(
    factory: Annotated[UlsanClientFactory, Depends(ulsan_client_factory)],
    include_vessels: bool = True,
) -> PortCongestion:
    try:
        indicator = await load_ulsan_congestion(factory)
    except PositionsUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"울산항 혼잡도를 계산하지 못했습니다: {exc}"
        ) from exc
    return indicator if include_vessels else indicator.model_copy(update={"vessels": []})
