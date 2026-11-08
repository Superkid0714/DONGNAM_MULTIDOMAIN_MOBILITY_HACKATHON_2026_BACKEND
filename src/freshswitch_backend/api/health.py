import os
from functools import lru_cache

from alembic.script import ScriptDirectory
from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from freshswitch_backend.api.ops import seed_completed
from freshswitch_backend.config import get_settings
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.database import migration_config

router = APIRouter(tags=["health"])

COMMIT_ENV = "GIT_COMMIT"


@lru_cache
def migration_head() -> str | None:
    return ScriptDirectory.from_config(migration_config()).get_current_head()


def deploy_commit() -> str | None:
    return os.environ.get(COMMIT_ENV, "").strip() or None


def _db_state() -> tuple[bool, str | None, bool | None]:
    if not database.ping():
        return False, None, None
    try:
        with database.begin() as db:
            version = db.scalar(text("SELECT version_num FROM alembic_version"))
            return True, version, seed_completed(db)
    except SQLAlchemyError:
        return True, None, None


@router.get("/health")
async def health() -> JSONResponse:
    settings = get_settings()
    db_ok, migration_version, seeded = await run_in_threadpool(_db_state)
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "degraded",
            "db": "ok" if db_ok else "error",
            "data_go_kr_configured": bool(settings.data_go_kr_service_key),
            "llm_configured": bool(settings.llm_api_key and settings.llm_model_name),
            "commit": deploy_commit(),
            "migration_version": migration_version,
            "migration_head": migration_head(),
            "offline_mode": settings.offline_mode,
            "explain_use_llm": settings.explain_use_llm,
            "seed_completed": seeded,
        },
    )
