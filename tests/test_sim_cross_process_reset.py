import time

import pytest
from saas_fixtures import PLATFORM_ORG_ID
from sqlalchemy import delete, func, select

from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.coldchain.models import SimulationRequest
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence import sandbox_reset as sandbox_reset_module
from freshswitch_backend.persistence.sandbox_world import SEED_ID, lock_world
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    AlertRow,
    BindingRow,
    ContainerRow,
    OrganizationRow,
    ReadingRow,
    RunRow,
)

INTERVAL = 0.05


@pytest.fixture(autouse=True)
def stop_runs():
    yield
    coldchain_store.stop_all()


def _counts(org_id: str) -> tuple[int, int, int, int]:
    with database.begin() as db:
        readings = db.scalar(select(func.count()).select_from(ReadingRow)) or 0
        alerts = db.scalar(select(func.count()).where(AlertRow.org_id == org_id)) or 0
        containers = db.scalar(select(func.count()).where(ContainerRow.org_id == org_id)) or 0
        runs = db.scalar(select(func.count()).where(RunRow.org_id == org_id)) or 0
    return readings, alerts, containers, runs


def _start(org_id: str):
    info = coldchain_store.simulate(
        SimulationRequest(preset="power_cut_restore", interval_seconds=INTERVAL), org_id
    )
    run = coldchain_store._runs[info.run_id]
    return info, run


def _wait_sent(run, count: int) -> None:
    deadline = time.monotonic() + 10
    while run.info.sent < count:
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_reset_from_other_process_stops_running_simulation(demo_tenants, monkeypatch) -> None:
    with database.begin() as db:
        org = db.get(OrganizationRow, PLATFORM_ORG_ID)
        assert org is not None and org.sandbox and org.world_id == SEED_ID
    info, run = _start(PLATFORM_ORG_ID)
    _wait_sent(run, 3)
    monkeypatch.setattr(sandbox_reset_module.coldchain_store, "clear", lambda org_id: 0)
    sandbox_reset_module.reset_sandbox()
    after_reset = _counts(PLATFORM_ORG_ID)
    sent_at_reset = run.info.sent
    run.thread.join(timeout=5)
    assert not run.thread.is_alive()
    time.sleep(INTERVAL * (info.total - sent_at_reset + 5))
    assert _counts(PLATFORM_ORG_ID) == after_reset
    assert after_reset[3] == 0
    with database.begin() as db:
        assert db.get(RunRow, info.run_id) is None
    assert run.cancelled
    assert run.info.sent < info.total
    assert info.run_id not in coldchain_store._runs


def test_reset_waits_for_tick_in_progress(demo_tenants, monkeypatch) -> None:
    _, run = _start(PLATFORM_ORG_ID)
    _wait_sent(run, 2)
    monkeypatch.setattr(sandbox_reset_module.coldchain_store, "clear", lambda org_id: 0)
    with lock_world(SEED_ID):
        held = run.info.sent
        time.sleep(INTERVAL * 6)
        assert run.info.sent == held
        assert run.thread.is_alive()
        with database.begin() as db:
            db.execute(delete(AlertRow).where(AlertRow.org_id == PLATFORM_ORG_ID))
            db.execute(delete(BindingRow).where(BindingRow.org_id == PLATFORM_ORG_ID))
            db.execute(delete(ContainerRow).where(ContainerRow.org_id == PLATFORM_ORG_ID))
            db.execute(delete(RunRow).where(RunRow.org_id == PLATFORM_ORG_ID))
    run.thread.join(timeout=5)
    assert not run.thread.is_alive()
    assert run.cancelled
    assert _counts(PLATFORM_ORG_ID)[1:] == (0, 0, 0)


def test_in_process_reset_still_stops_runs_quickly(demo_tenants) -> None:
    info, run = _start(PLATFORM_ORG_ID)
    _wait_sent(run, 2)
    started = time.monotonic()
    sandbox_reset_module.reset_sandbox()
    assert time.monotonic() - started < 4
    assert not run.thread.is_alive()
    with database.begin() as db:
        assert db.get(RunRow, info.run_id) is None


def test_demo_reset_lock_pauses_only_the_demo_run(demo_tenants) -> None:
    _, run = _start(DEMO_ORG)
    _wait_sent(run, 2)
    with sandbox_reset_module.demo_reset_lock():
        held = run.info.sent
        time.sleep(INTERVAL * 6)
        assert run.info.sent == held
        assert run.thread.is_alive()
    _wait_sent(run, held + 1)


def test_normal_stream_run_completes_and_persists_done(demo_tenants) -> None:
    info, run = _start(PLATFORM_ORG_ID)
    done = coldchain_store.wait(info.run_id, timeout=10, org_id=PLATFORM_ORG_ID)
    assert done.done and done.sent == done.total == info.total
    assert not run.cancelled
    with database.begin() as db:
        row = db.get(RunRow, info.run_id)
        assert row is not None
        assert row.done is True
        assert row.payload["done"] is True
        assert row.payload["sent"] == info.total
    assert coldchain_store.run(info.run_id, PLATFORM_ORG_ID).done is True


def test_stop_still_persists_stopped_run(demo_tenants) -> None:
    info, run = _start(PLATFORM_ORG_ID)
    _wait_sent(run, 2)
    stopped = coldchain_store.stop(info.run_id, PLATFORM_ORG_ID)
    assert stopped.done and stopped.sent < info.total
    with database.begin() as db:
        row = db.get(RunRow, info.run_id)
        assert row is not None and row.done is True
        assert row.payload["sent"] == stopped.sent


def test_streams_waiting_on_reset_do_not_hold_connections(demo_tenants, monkeypatch) -> None:
    runs = []
    for i in range(20):
        info = coldchain_store.simulate(
            SimulationRequest(
                preset="power_cut_restore",
                interval_seconds=INTERVAL,
                container_id=f"POOL-{i:02d}",
                device_id=f"sim-pool-{i:02d}",
            ),
            PLATFORM_ORG_ID,
        )
        runs.append(coldchain_store._runs[info.run_id])
    for run in runs:
        _wait_sent(run, 1)
    monkeypatch.setattr(sandbox_reset_module.coldchain_store, "clear", lambda org_id: 0)
    with lock_world(SEED_ID):
        held = [run.info.sent for run in runs]
        time.sleep(1)
        assert database.engine.pool.checkedout() <= 4
        with database.begin() as db:
            assert db.scalar(select(1)) == 1
        assert [run.info.sent for run in runs] == held
    for run in runs:
        _wait_sent(run, run.info.sent + 1)
    coldchain_store.stop_all()
    assert all(run.info.error is None for run in runs)
    assert not any(run.cancelled for run in runs)
