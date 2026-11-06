import contextvars
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException, Request, status

from freshswitch_backend.api.auth import (
    TOO_MANY_REQUESTS,
    OptionalPrincipal,
    Principal,
    RateRule,
    SlidingWindowLimiter,
    app_settings,
    client_address,
)

MAX_PLAN_LOTS = 60
PREVIEW_ROUTE = "plan-preview"
PREVIEW_ORG_ROUTE = "plan-preview-org"
PREVIEW_RATE = RateRule(per_client=20, window_seconds=60)
PREVIEW_ORG_RATE = RateRule(per_client=120, window_seconds=60)
REPLAN_ROUTE = "replan"
REPLAN_ORG_ROUTE = "replan-org"
REPLAN_RATE = RateRule(per_client=30, window_seconds=60)
REPLAN_ORG_RATE = RateRule(per_client=240, window_seconds=60)
ANALYSIS_ROUTE = "analysis"
ANALYSIS_ORG_ROUTE = "analysis-org"
ANALYSIS_RATE = RateRule(per_client=30, window_seconds=60)
ANALYSIS_ORG_RATE = RateRule(per_client=240, window_seconds=60)
COMPUTE_SLOTS = 2
ANALYSIS_SLOTS = 2
COMPUTE_WAIT_SECONDS = 2.0
ANALYSIS_WAIT_SECONDS = 10.0
PREVIEW_DEADLINE_SECONDS = 15.0
PREVIEW_CACHE_SECONDS = 30.0
PREVIEW_CACHE_SIZE = 128
RETRY_AFTER_SECONDS = 5
COMPUTE_BUSY = "계획 계산 요청이 몰려 있습니다. 잠시 후 다시 시도하세요"
PREVIEW_TIMEOUT = "미리보기 계산 시간이 초과되었습니다. 출하 수를 줄이거나 잠시 후 다시 시도하세요"


def too_many_lots(count: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"한 번에 계획할 수 있는 출하는 {MAX_PLAN_LOTS}건까지입니다(요청 {count}건)",
    )


def check_lot_count(count: int) -> None:
    if count > MAX_PLAN_LOTS:
        raise too_many_lots(count)


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


class ComputeGate:
    def __init__(
        self, slots: int = COMPUTE_SLOTS, wait_seconds: float = COMPUTE_WAIT_SECONDS
    ) -> None:
        self._slots = threading.BoundedSemaphore(slots)
        self._executor = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="plan-compute")
        self.wait_seconds = wait_seconds

    def _acquire(self, wait: float) -> None:
        if not self._slots.acquire(timeout=max(wait, 0.0)):
            raise _unavailable(COMPUTE_BUSY)

    @contextmanager
    def slot(self) -> Iterator[None]:
        self._acquire(self.wait_seconds)
        try:
            yield
        finally:
            self._slots.release()

    def submit(self, fn: Callable[..., Any], *args: Any, deadline: float, **kwargs: Any) -> Future:
        self._acquire(min(self.wait_seconds, deadline - time.perf_counter()))
        try:
            future = self._executor.submit(
                contextvars.copy_context().run, fn, *args, deadline=deadline, **kwargs
            )
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        return future

    def run(self, fn: Callable[..., Any], *args: Any, deadline: float, **kwargs: Any) -> Any:
        if deadline - time.perf_counter() <= 0:
            raise _unavailable(PREVIEW_TIMEOUT)
        future = self.submit(fn, *args, deadline=deadline, **kwargs)
        try:
            return future.result(timeout=max(deadline - time.perf_counter(), 0.0))
        except TimeoutError as exc:
            raise _unavailable(PREVIEW_TIMEOUT) from exc


gate = ComputeGate()
analysis_gate = ComputeGate(slots=ANALYSIS_SLOTS, wait_seconds=ANALYSIS_WAIT_SECONDS)


@contextmanager
def compute_slot() -> Iterator[None]:
    with gate.slot():
        yield


@contextmanager
def analysis_slot() -> Iterator[None]:
    with analysis_gate.slot():
        yield


def run_analysis(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    with analysis_slot():
        return fn(*args, **kwargs)


def run_bounded(fn: Callable[..., Any], *args: Any, deadline: float, **kwargs: Any) -> Any:
    return gate.run(fn, *args, deadline=deadline, **kwargs)


def preview_deadline() -> float:
    return time.perf_counter() + PREVIEW_DEADLINE_SECONDS


class TtlCache:
    def __init__(
        self,
        ttl_seconds: float,
        max_items: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_items
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        now = self._clock()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            if item[0] <= now:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return item[1]

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._items[key] = (self._clock() + self._ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)


def cache_key(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_state_lock = threading.Lock()


def _app_state(request: Request, name: str, factory: Callable[[], Any]) -> Any:
    state = request.app.state
    with _state_lock:
        value = getattr(state, name, None)
        if value is None:
            value = factory()
            setattr(state, name, value)
        return value


def preview_limiter(request: Request) -> SlidingWindowLimiter:
    return _app_state(
        request,
        "plan_preview_limiter",
        lambda: SlidingWindowLimiter(
            {PREVIEW_ROUTE: PREVIEW_RATE, PREVIEW_ORG_ROUTE: PREVIEW_ORG_RATE}
        ),
    )


def preview_cache(request: Request) -> TtlCache:
    return _app_state(
        request,
        "plan_preview_cache",
        lambda: TtlCache(PREVIEW_CACHE_SECONDS, PREVIEW_CACHE_SIZE),
    )


def replan_limiter(request: Request) -> SlidingWindowLimiter:
    return _app_state(
        request,
        "replan_limiter",
        lambda: SlidingWindowLimiter(
            {REPLAN_ROUTE: REPLAN_RATE, REPLAN_ORG_ROUTE: REPLAN_ORG_RATE}
        ),
    )


def analysis_limiter(request: Request) -> SlidingWindowLimiter:
    return _app_state(
        request,
        "analysis_limiter",
        lambda: SlidingWindowLimiter(
            {ANALYSIS_ROUTE: ANALYSIS_RATE, ANALYSIS_ORG_ROUTE: ANALYSIS_ORG_RATE}
        ),
    )


def _check_rate(
    limiter: SlidingWindowLimiter,
    routes: tuple[str, str],
    request: Request,
    principal: Principal,
) -> None:
    subject = principal.world_id or principal.org_id
    visitor = f"{subject}:{principal.user_id}:{client_address(request)}"
    retry = limiter.hit(routes[0], visitor)
    if retry is None:
        retry = limiter.hit(routes[1], subject)
    if retry is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=TOO_MANY_REQUESTS,
            headers={"Retry-After": str(max(1, math.ceil(retry)))},
        )


def check_preview_rate(request: Request, principal: Principal) -> None:
    _check_rate(preview_limiter(request), (PREVIEW_ROUTE, PREVIEW_ORG_ROUTE), request, principal)


def check_replan_rate(request: Request, principal: Principal) -> None:
    _check_rate(replan_limiter(request), (REPLAN_ROUTE, REPLAN_ORG_ROUTE), request, principal)


def replan_rate(request: Request, principal: OptionalPrincipal) -> None:
    if principal is None or not app_settings(request).saas_mode:
        return
    check_replan_rate(request, principal)


def analysis_rate(request: Request, principal: OptionalPrincipal) -> None:
    if principal is None or not app_settings(request).saas_mode:
        return
    _check_rate(analysis_limiter(request), (ANALYSIS_ROUTE, ANALYSIS_ORG_ROUTE), request, principal)
