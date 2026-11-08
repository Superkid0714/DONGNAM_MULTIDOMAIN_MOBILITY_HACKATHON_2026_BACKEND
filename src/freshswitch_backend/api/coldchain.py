import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from freshswitch_backend.coldchain import ColdchainError, NotFound, coldchain_store
from freshswitch_backend.coldchain.models import (
    AlertFeed,
    Binding,
    BindingRequest,
    IngestResult,
    PresetInfo,
    ReadingIn,
    SettlementPreview,
    SimulationRequest,
    SimulationRun,
    TwinState,
    TwinSummary,
)
from freshswitch_backend.coldchain.simulator import PRESETS
from freshswitch_backend.domain.models import Quantity

router = APIRouter(prefix="/coldchain", tags=["coldchain"])


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/params")
def read_params() -> dict[str, Quantity]:
    return dict(coldchain_store.params)


@router.post("/bindings", status_code=status.HTTP_201_CREATED)
def bind(body: BindingRequest) -> Binding:
    try:
        return coldchain_store.bind(body)
    except (NotFound, ColdchainError) as exc:
        raise _http(exc) from exc


@router.get("/bindings")
def list_bindings() -> list[Binding]:
    return coldchain_store.bindings()


@router.post("/readings")
def ingest(body: list[ReadingIn] | ReadingIn) -> IngestResult:
    items = body if isinstance(body, list) else [body]
    if not items:
        raise HTTPException(status_code=422, detail="측정값이 없습니다")
    if len(items) > 2000:
        raise HTTPException(status_code=422, detail="한 번에 2,000건까지 보낼 수 있습니다")
    result = coldchain_store.ingest(items)
    if result.accepted == 0 and result.duplicates == 0:
        raise HTTPException(status_code=422, detail=result.rejected[0].reason)
    return result


@router.get("/containers")
def list_containers(plan_id: str | None = None) -> list[TwinSummary]:
    return coldchain_store.summaries(plan_id)


@router.get("/containers/{container_id}")
def read_twin(
    container_id: str,
    plan_id: str | None = None,
    points: int = Query(720, ge=0, le=5000),
) -> TwinState:
    try:
        return coldchain_store.state(container_id, plan_id, points)
    except (NotFound, ColdchainError) as exc:
        raise _http(exc) from exc


@router.delete("/containers/{container_id}", status_code=status.HTTP_204_NO_CONTENT)
def reset_container(container_id: str, plan_id: str | None = None) -> None:
    try:
        coldchain_store.reset_container(container_id, plan_id)
    except ColdchainError as exc:
        raise _http(exc) from exc


@router.get("/containers/{container_id}/settlement-preview")
def settlement_preview(container_id: str, plan_id: str | None = None) -> SettlementPreview:
    try:
        return coldchain_store.settlement_preview(container_id, plan_id)
    except (NotFound, ColdchainError) as exc:
        raise _http(exc) from exc


@router.get("/alerts")
def read_alerts(
    since: int = Query(0, ge=0),
    container_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    plan_id: str | None = None,
) -> AlertFeed:
    return coldchain_store.alerts(since, container_id, limit, plan_id)


@router.get("/alerts/stream")
async def stream_alerts(
    since: int = Query(0, ge=0),
    container_id: str | None = None,
    timeout: float = Query(300, ge=0, le=3600),
    plan_id: str | None = None,
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        cursor = since
        deadline = time.monotonic() + timeout
        while True:
            feed = await run_in_threadpool(
                coldchain_store.alerts, cursor, container_id, 1000, plan_id
            )
            for alert in feed.alerts:
                data = json.dumps(alert.model_dump(mode="json"), ensure_ascii=False)
                yield f"id: {alert.seq}\nevent: alert\ndata: {data}\n\n"
            cursor = feed.next_since
            if time.monotonic() >= deadline:
                break
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/simulate/presets")
def list_presets() -> list[PresetInfo]:
    return [p.info() for p in PRESETS.values()]


@router.post("/simulate")
def simulate(body: SimulationRequest) -> SimulationRun:
    try:
        return coldchain_store.simulate(body)
    except (NotFound, ColdchainError) as exc:
        raise _http(exc) from exc


@router.get("/simulate/{run_id}")
def read_run(run_id: str) -> SimulationRun:
    try:
        return coldchain_store.run(run_id)
    except NotFound as exc:
        raise _http(exc) from exc


@router.delete("/simulate/{run_id}")
def stop_run(run_id: str) -> SimulationRun:
    try:
        return coldchain_store.stop(run_id)
    except NotFound as exc:
        raise _http(exc) from exc
