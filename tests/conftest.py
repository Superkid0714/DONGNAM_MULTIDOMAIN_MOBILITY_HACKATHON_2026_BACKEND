import os
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from freshswitch_backend.api.vessels import ulsan_client_factory
from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.lots import lot_store
from freshswitch_backend.main import app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import ensure_demo_org
from freshswitch_backend.persistence.seed import run as seed_demo_tenants
from freshswitch_backend.planning.store import store

pytest_plugins = ["saas_fixtures"]

FROZEN_NOW = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
POSTGRES_IMAGE = "postgres:17-alpine"
STARTUP_TIMEOUT = 60
LABEL = "freshswitch-pytest"
PID_LABEL = f"{LABEL}.pid"


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


def _wait_ready(url: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while True:
        try:
            with psycopg.connect(url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep_stale() -> None:
    try:
        listed = _docker(
            "ps",
            "-a",
            "--filter",
            f"label={LABEL}",
            "--format",
            f'{{{{.Names}}}}\t{{{{.Label "{PID_LABEL}"}}}}',
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return
    for line in listed.splitlines():
        name, _, pid = line.partition("\t")
        if not pid.isdigit() or not _alive(int(pid)):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _start_container(name: str) -> str:
    _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--label",
        LABEL,
        "--label",
        f"{PID_LABEL}={os.getpid()}",
        "-e",
        "POSTGRES_USER=test",
        "-e",
        "POSTGRES_PASSWORD=test",
        "-e",
        "POSTGRES_DB=test",
        "-p",
        "127.0.0.1::5432",
        "--tmpfs",
        "/var/lib/postgresql/data",
        POSTGRES_IMAGE,
        "-c",
        "fsync=off",
        "-c",
        "synchronous_commit=off",
        "-c",
        "full_page_writes=off",
    )
    port = _docker("port", name, "5432/tcp").splitlines()[0].rsplit(":", 1)[1]
    return f"postgresql://test:test@127.0.0.1:{port}/test"


@pytest.fixture(scope="session", autouse=True)
def postgres() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    name = None
    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        if not url:
            if shutil.which("docker") is None:
                pytest.exit(
                    "테스트에 PostgreSQL이 필요합니다. docker를 설치하거나 "
                    "TEST_DATABASE_URL을 지정하세요",
                    2,
                )
            _sweep_stale()
            name = f"{LABEL}-{uuid.uuid4().hex[:8]}"
            try:
                url = _start_container(name)
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                OSError,
                IndexError,
            ) as exc:
                detail = getattr(exc, "stderr", "") or str(exc)
                pytest.exit(f"테스트용 PostgreSQL 컨테이너를 띄우지 못했습니다: {detail}", 2)
        _wait_ready(url)
        database.configure(url)
        database.init()
        yield url
    finally:
        coldchain_store.stop_all()
        database.configure(None)
        if name is not None:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        signal.signal(signal.SIGTERM, previous)


@pytest.fixture(autouse=True)
def clean_database(postgres: str) -> Iterator[None]:
    coldchain_store.stop_all()
    database.wipe()
    ensure_demo_org()
    yield
    coldchain_store.stop_all()


@pytest.fixture(autouse=True)
def frozen_clock():
    original = store.clock
    original_lots = lot_store.clock
    store.clock = lambda: FROZEN_NOW
    lot_store.clock = lambda: FROZEN_NOW
    yield
    store.clock = original
    lot_store.clock = original_lots


@pytest.fixture(autouse=True)
def fresh_request_limits():
    for name in (
        "auth_rate_limiter",
        "login_failure_guard",
        "login_email_guard",
        "replan_limiter",
        "analysis_limiter",
    ):
        setattr(app.state, name, None)
    yield


@pytest.fixture(autouse=True)
def no_live_ulsan():
    app.dependency_overrides[ulsan_client_factory] = lambda: None
    yield
    app.dependency_overrides.pop(ulsan_client_factory, None)


@pytest.fixture
def demo_tenants() -> dict[str, int]:
    return seed_demo_tenants(password="test-demo-password")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def reset_admin_headers(monkeypatch):
    from freshswitch_backend.api import access

    settings = access.get_settings().model_copy(
        update={"demo_reset_token": SecretStr("test-reset-token")}
    )
    monkeypatch.setattr(access, "get_settings", lambda: settings)
    return {"Authorization": "Bearer test-reset-token"}
