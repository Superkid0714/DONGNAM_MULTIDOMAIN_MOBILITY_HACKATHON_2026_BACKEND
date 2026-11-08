import json
import logging
import math
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import unquote_plus

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.errors import UntranslatableCharacter
from sqlalchemy.exc import DataError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from freshswitch_backend.api.router import router_for
from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.config import Settings, get_settings
from freshswitch_backend.domain.inputs import UNSTORABLE_TEXT, storable
from freshswitch_backend.observability import RequestAuditMiddleware
from freshswitch_backend.persistence import database
from freshswitch_backend.replan import ReplanError

logger = logging.getLogger(__name__)


async def _replan_error(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def _encodable(value: object) -> object:
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, list):
        return [_encodable(item) for item in value]
    if isinstance(value, dict):
        return {_encodable(key): _encodable(item) for key, item in value.items()}
    return value


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422, content={"detail": _encodable(jsonable_encoder(exc.errors()))}
    )


async def _data_error(_: Request, exc: Exception) -> JSONResponse:
    orig = getattr(exc, "orig", None)
    if isinstance(orig, UntranslatableCharacter) or "cannot contain NUL" in str(orig):
        return JSONResponse(status_code=422, content={"detail": UNSTORABLE_TEXT})
    raise exc


def _unstorable(text: str) -> bool:
    try:
        storable(text)
    except ValueError:
        return True
    return False


def _has_unstorable(value: object) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _unstorable(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _json_body(scope: Scope) -> bool:
    content_type = next(
        (v for k, v in scope.get("headers", []) if k.lower() == b"content-type"), None
    )
    if content_type is None:
        return True
    media = content_type.split(b";", 1)[0].strip().lower()
    return media == b"application/json" or (
        media.startswith(b"application/") and media.endswith(b"+json")
    )


class UnstorableTextGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        query = unquote_plus(scope.get("query_string", b"").decode("latin-1"))
        if "\x00" in scope["path"]:
            await _reject("path")(scope, receive, send)
            return
        if "\x00" in query:
            await _reject("query")(scope, receive, send)
            return
        if not _json_body(scope):
            await self.app(scope, receive, send)
            return
        messages: list[Message] = []
        body = b""
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        if body and _body_unstorable(body):
            await _reject("body")(scope, receive, send)
            return

        async def replay() -> Message:
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, replay, send)


def _body_unstorable(body: bytes) -> bool:
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return False
    return _has_unstorable(parsed)


def _reject(where: str) -> JSONResponse:
    detail = [{"type": "value_error", "loc": [where], "msg": UNSTORABLE_TEXT}]
    return JSONResponse(status_code=422, content={"detail": detail})


def _startup(settings: Settings) -> None:
    from freshswitch_backend.persistence.sandbox_world import purge_expired_worlds

    database.init()
    coldchain_store.finish_orphan_runs()
    if not settings.saas_mode:
        purge_expired_worlds()


def _quantity_forever() -> None:
    from freshswitch_backend.planning.quantity_policy import tick_policies

    while True:
        try:
            tick_policies()
        except Exception:
            logger.exception("수량 확정·충원 제안 갱신 실패")
        time.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app.state.settings
    if settings.feature_p1_delay_ml:
        try:
            from freshswitch_backend.forecast.ml.adapter import initialize_delay_model

            loaded = await run_in_threadpool(initialize_delay_model, settings)
            if not loaded:
                logger.warning("지연 ML 모델을 사용할 수 없어 기존 통계 예측기를 사용합니다")
        except Exception:
            logger.exception("지연 ML 초기화 실패. 기존 통계 예측기를 사용합니다")
    try:
        await run_in_threadpool(_startup, settings)
    except Exception:
        logger.exception("DB 초기화 실패. 첫 요청 때 다시 시도한다")
    if settings.saas_mode and not os.environ.get("PYTEST_CURRENT_TEST"):
        threading.Thread(target=_quantity_forever, daemon=True, name="quantity-policy").start()
    yield
    coldchain_store.stop_all()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    prefix = settings.api_prefix
    app = FastAPI(
        title="FreshSwitch API",
        lifespan=lifespan,
        version="0.1.0",
        docs_url=f"{prefix}/docs",
        redoc_url=f"{prefix}/redoc",
        openapi_url=f"{prefix}/openapi.json",
    )
    app.state.settings = settings
    app.add_middleware(UnstorableTextGuard)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestAuditMiddleware)
    app.include_router(router_for(saas_mode=settings.saas_mode), prefix=prefix)
    app.add_exception_handler(ReplanError, _replan_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(DataError, _data_error)
    return app


app = create_app()
