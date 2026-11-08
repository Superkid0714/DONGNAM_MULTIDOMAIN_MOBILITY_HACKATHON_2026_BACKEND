from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from freshswitch_backend.config import get_settings
from freshswitch_backend.persistence import database

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> JSONResponse:
    settings = get_settings()
    db_ok = await run_in_threadpool(database.ping)
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "degraded",
            "db": "ok" if db_ok else "error",
            "data_go_kr_configured": bool(settings.data_go_kr_service_key),
            "llm_configured": bool(settings.llm_api_key and settings.llm_model_name),
        },
    )
