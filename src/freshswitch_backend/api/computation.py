import asyncio
import time
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from freshswitch_backend.config import get_settings
from freshswitch_backend.observability import record_event
from freshswitch_backend.planning.budget import remaining


def request_deadline() -> float:
    return time.perf_counter() + get_settings().replan_timeout_seconds


async def compute_replan(fn: Callable, *args: Any, deadline: float, **kwargs: Any):
    started = time.perf_counter()
    event_type = getattr(args[2], "type", None) if len(args) > 2 else None
    try:
        timeout = remaining(deadline, get_settings().replan_timeout_seconds)
        result = await asyncio.wait_for(
            asyncio.to_thread(fn, *args, deadline=deadline, **kwargs),
            timeout=timeout,
        )
        record_event(
            "ai.replan",
            event_type=event_type,
            status="completed",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return result
    except TimeoutError as exc:
        record_event(
            "ai.replan",
            event_type=event_type,
            status="timeout",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "replan_timeout", "message": "계산 시간 초과. 기존 계획은 유지됩니다"},
        ) from exc


def explanation_budget(deadline: float) -> float:
    return max(0.0, min(get_settings().explain_timeout_seconds, deadline - time.perf_counter()))
