import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from freshswitch_backend.api.router import api_router
from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.config import get_settings
from freshswitch_backend.observability import RequestAuditMiddleware
from freshswitch_backend.persistence import database
from freshswitch_backend.replan import ReplanError

logger = logging.getLogger(__name__)


async def _replan_error(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def _startup() -> None:
    database.init()
    coldchain_store.finish_orphan_runs()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        await run_in_threadpool(_startup)
    except Exception:
        logger.exception("DB 초기화 실패. 첫 요청 때 다시 시도한다")
    yield
    coldchain_store.stop_all()


def create_app() -> FastAPI:
    app = FastAPI(
        title="FreshSwitch API",
        lifespan=lifespan,
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestAuditMiddleware)
    app.include_router(api_router)
    app.add_exception_handler(ReplanError, _replan_error)
    return app


app = create_app()
