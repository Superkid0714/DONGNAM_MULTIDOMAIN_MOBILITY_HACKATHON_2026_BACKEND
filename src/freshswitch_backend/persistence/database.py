import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from freshswitch_backend.config import get_settings
from freshswitch_backend.persistence.tables import ALERT_SEQ, Base, MetaRow

MIGRATIONS = "freshswitch_backend:migrations"
MIGRATION_LOCK_KEY = 7_402_115_301


class DatabaseNotConfigured(RuntimeError):
    pass


def migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    return config


def upgrade_schema(engine: Engine) -> None:
    config = migration_config()
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        config.attributes["connection"] = conn
        command.upgrade(config, "head")


def normalize_url(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url.removeprefix(prefix)
    return url


class Database:
    def __init__(self, url: str | None = None) -> None:
        self._url = url
        self._engine: Engine | None = None
        self._ready = False
        self._lock = threading.Lock()

    def configure(self, url: str | None) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
            self._url = url
            self._engine = None
            self._ready = False

    @property
    def engine(self) -> Engine:
        with self._lock:
            if self._engine is None:
                url = self._url or get_settings().resolved_database_url()
                if not url:
                    raise DatabaseNotConfigured("DATABASE_URL이 설정되지 않았습니다")
                self._engine = create_engine(
                    normalize_url(url),
                    pool_size=5,
                    max_overflow=10,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                )
            return self._engine

    def init(self) -> None:
        if self._ready:
            return
        engine = self.engine
        with self._lock:
            if not self._ready:
                upgrade_schema(engine)
                self._ready = True

    @contextmanager
    def begin(self) -> Iterator[Session]:
        self.init()
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            yield session

    def ping(self) -> bool:
        try:
            self.init()
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except (SQLAlchemyError, DatabaseNotConfigured, OSError):
            return False

    def wipe(self) -> None:
        self.init()
        with self.engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
            conn.execute(text(f"ALTER SEQUENCE {ALERT_SEQ.name} RESTART"))


def get_meta(db: Session, key: str) -> Any | None:
    row = db.get(MetaRow, key)
    return row.value.get("v") if row is not None else None


def set_meta(db: Session, key: str, value: Any) -> None:
    row = db.get(MetaRow, key)
    if row is None:
        db.add(MetaRow(key=key, value={"v": value}))
    else:
        row.value = {"v": value}


database = Database()
