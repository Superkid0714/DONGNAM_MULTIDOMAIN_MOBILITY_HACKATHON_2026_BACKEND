import asyncio
import threading
import time
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from freshswitch_backend.api import lots as lots_api
from freshswitch_backend.coldchain import ColdchainStore, coldchain_store
from freshswitch_backend.config import Settings
from freshswitch_backend.domain.models import KST
from freshswitch_backend.execution.quality import ArrivalRequest
from freshswitch_backend.main import app
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.tables import RunRow
from freshswitch_backend.planning.store import store
from freshswitch_backend.quality.store import QualityHistory

FORM = {
    "product": "딸기",
    "pallets": 2,
    "ready_date": "2026-11-17",
    "shipper": "산청",
    "destination": "홍콩",
}
START = datetime(2026, 11, 17, 9, 0, tzinfo=KST)


def _lot_ids(plan: dict) -> set[str]:
    return {lot["id"] for lot in plan["scenario"]["lots"]}


def _status(client: TestClient, lot_id: str) -> str:
    return client.get(f"/api/lots/{lot_id}").json()["status"]


def test_lot_ids_reject_other_scenario(client: TestClient) -> None:
    created = client.post("/api/lots", json={**FORM, "lot_id": "lot-jinju-sb-2"})
    assert created.status_code == 201
    response = client.post(
        "/api/plans", json={"scenario_id": "fuel-shock", "lot_ids": ["lot-jinju-sb-2"]}
    )
    assert response.status_code == 422
    assert "lot-jinju-sb-2" in str(response.json()["detail"])
    assert _status(client, "lot-jinju-sb-2") == "registered"


def test_inline_lot_colliding_with_scenario_lot_rejected(client: TestClient) -> None:
    base = client.post("/api/plans", json={}).json()
    first = base["scenario"]["lots"][0]
    response = client.post("/api/plans", json={"lots": [{**first, "pallets": 1}]})
    assert response.status_code == 422


def test_inline_lot_colliding_with_registered_lot_rejected(client: TestClient) -> None:
    lot = client.post("/api/lots", json=FORM).json()["registered"]
    base = client.post("/api/plans", json={}).json()
    inline = {**base["scenario"]["lots"][0], "id": lot["id"], "pallets": 9}
    response = client.post("/api/plans", json={"lots": [inline], "lot_ids": [lot["id"]]})
    assert response.status_code == 422
    assert _status(client, lot["id"]) == "registered"
    with_all = client.post("/api/plans", json={"lots": [inline], "include_registered": True})
    assert with_all.status_code == 422


def test_lot_status_reverts_when_plans_go_away(client: TestClient, reset_admin_headers) -> None:
    lot = client.post("/api/lots", json=FORM).json()["registered"]
    first = client.post("/api/plans", json={"lot_ids": [lot["id"]]}).json()
    second = client.post("/api/plans", json={"lot_ids": [lot["id"]]}).json()
    assert first["registered_lot_ids"] == [lot["id"]]
    assert second["registered_lot_ids"] == [lot["id"]]
    assert _status(client, lot["id"]) == "planned"
    assert client.delete(f"/api/plans/{first['plan_id']}").status_code == 204
    assert _status(client, lot["id"]) == "planned"
    assert client.delete(f"/api/plans/{second['plan_id']}").status_code == 204
    assert _status(client, lot["id"]) == "registered"

    client.post("/api/plans", json={"lot_ids": [lot["id"]]})
    assert _status(client, lot["id"]) == "planned"
    client.post("/api/demo/reset", headers=reset_admin_headers)
    assert _status(client, lot["id"]) == "registered"


def test_include_registered_skips_lots_planned_elsewhere(client: TestClient) -> None:
    taken = client.post("/api/lots", json=FORM).json()["registered"]
    free = client.post("/api/lots", json={**FORM, "pallets": 3}).json()["registered"]
    first = client.post("/api/plans", json={"lot_ids": [taken["id"]]}).json()
    assert _status(client, taken["id"]) == "planned"

    second = client.post("/api/plans", json={"include_registered": True}).json()
    assert second["registered_lot_ids"] == [free["id"]]
    assert taken["id"] not in _lot_ids(second)
    assert free["id"] in _lot_ids(second)

    assert client.delete(f"/api/plans/{first['plan_id']}").status_code == 204
    assert _status(client, taken["id"]) == "registered"
    assert _status(client, free["id"]) == "planned"


def test_plan_delete_unbinds_coldchain_device(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={}).json()["plan_id"]
    bound = client.post(
        "/api/coldchain/bindings",
        json={"device_id": "d1", "container_id": "CA-01", "plan_id": plan_id},
    )
    assert bound.status_code == 201
    reading = {"device_id": "d1", "ts": START.isoformat(), "temp_c": 1.0, "power_on": True}
    assert client.post("/api/coldchain/readings", json=[reading]).json()["accepted"] == 1
    assert client.delete(f"/api/plans/{plan_id}").status_code == 204
    bindings = client.get("/api/coldchain/bindings").json()
    assert bindings[0]["plan_id"] is None
    later = {**reading, "ts": (START + timedelta(minutes=5)).isoformat()}
    response = client.post("/api/coldchain/readings", json=[later])
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_ingest_tolerates_binding_to_missing_plan(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={}).json()["plan_id"]
    client.post(
        "/api/coldchain/bindings",
        json={"device_id": "d1", "container_id": "CA-01", "plan_id": plan_id},
    )
    store.clear()
    reading = {"device_id": "d1", "ts": START.isoformat(), "temp_c": 1.0, "power_on": True}
    response = client.post("/api/coldchain/readings", json=[reading])
    assert response.json()["accepted"] == 1


def test_long_lot_id_rejected(client: TestClient) -> None:
    response = client.post("/api/lots", json={**FORM, "lot_id": "x" * 130})
    assert response.status_code == 422
    preview = client.post("/api/intake/lots", json={**FORM, "lot_id": "x" * 130, "save": True})
    assert preview.status_code == 422


def test_concurrent_registration_gets_distinct_ids() -> None:
    async def run() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await asyncio.gather(*(http.post("/api/lots", json=FORM) for _ in range(6)))

    responses = asyncio.run(run())
    assert [r.status_code for r in responses] == [201] * 6
    ids = {r.json()["registered"]["id"] for r in responses}
    assert len(ids) == 6


def test_auto_lot_id_skips_ids_embedded_in_plans(client: TestClient) -> None:
    lot = client.post("/api/lots", json=FORM).json()["registered"]
    client.post("/api/plans", json={"lot_ids": [lot["id"]]})
    assert client.delete(f"/api/lots/{lot['id']}").status_code == 204
    again = client.post("/api/lots", json=FORM).json()["registered"]
    assert again["id"] != lot["id"]
    preview = client.post("/api/intake/lots", json=FORM).json()
    assert preview["lot"]["id"] not in {lot["id"], again["id"]}


def test_stream_thread_failure_marks_run_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = coldchain_store.ingest
    calls = {"n": 0}

    def flaky(items):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OperationalError("SELECT 1", {}, Exception("connection lost"))
        return original(items)

    monkeypatch.setattr(coldchain_store, "ingest", flaky)
    run = client.post(
        "/api/coldchain/simulate", json={"preset": "normal", "interval_seconds": 0.01}
    ).json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = client.get(f"/api/coldchain/simulate/{run['run_id']}").json()
        if state["done"]:
            break
        time.sleep(0.05)
    assert state["done"] is True
    assert state["error"]
    with database.begin() as db:
        row = db.get(RunRow, run["run_id"])
        assert row.done is True


def test_demo_reset_during_stream_does_not_resurrect_run(
    client: TestClient, reset_admin_headers
) -> None:
    client.post("/api/coldchain/simulate", json={"preset": "normal", "interval_seconds": 0.3})
    time.sleep(0.4)
    client.post("/api/demo/reset", headers=reset_admin_headers)
    time.sleep(0.8)
    with database.begin() as db:
        assert db.scalars(select(RunRow)).all() == []


def test_concurrent_patches_cannot_break_dates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    lot = client.post("/api/lots", json={**FORM, "sell_by_date": "2026-11-20"}).json()["registered"]
    original = lots_api._validate_patch

    def slow(current, patch):
        values = original(current, patch)
        time.sleep(0.3)
        return values

    monkeypatch.setattr(lots_api, "_validate_patch", slow)
    results: list[int] = []

    def patch(body: dict) -> None:
        results.append(client.patch(f"/api/lots/{lot['id']}", json=body).status_code)

    threads = [
        threading.Thread(target=patch, args=({"ready_date": "2026-11-19"},)),
        threading.Thread(target=patch, args=({"sell_by_date": "2026-11-18"},)),
    ]
    for thread in threads:
        thread.start()
        time.sleep(0.05)
    for thread in threads:
        thread.join()
    stored = client.get(f"/api/lots/{lot['id']}").json()
    assert stored["sell_by_date"] >= stored["ready_date"]
    assert sorted(results) == [200, 422]


def test_post_event_runs_store_io_off_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = client.post("/api/plans", json={}).json()
    voyage = plan["scenario"]["voyages"][0]
    on_loop: list[str] = []

    def guard(name, fn):
        def wrapped(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                on_loop.append(name)
            except RuntimeError:
                pass
            return fn(*args, **kwargs)

        return wrapped

    for name in ("get", "add_pending", "idempotent", "remember"):
        monkeypatch.setattr(store, name, guard(name, getattr(store, name)))
    new_etd = (datetime.fromisoformat(voyage["etd"]) + timedelta(hours=30)).isoformat()
    response = client.post(
        f"/api/plans/{plan['plan_id']}/events",
        json={
            "event": {"type": "delay", "voyage_id": voyage["id"], "new_etd": new_etd},
            "idempotency_key": "k1",
        },
    )
    assert response.status_code == 200
    client.post(f"/api/plans/{plan['plan_id']}/schedule-check", json={"movements": []})
    assert on_loop == []


def test_database_url_built_from_postgres_parts() -> None:
    password = "p@ss:w/rd%40#?"
    settings = Settings(
        _env_file=None,
        database_url="",
        postgres_host="db",
        postgres_user="fresh",
        postgres_password=password,
        postgres_db="fs",
    )
    url = make_url(settings.resolved_database_url())
    assert url.password == password
    assert url.host == "db"
    assert url.username == "fresh"
    assert url.database == "fs"
    assert url.port == 5432
    override = Settings(_env_file=None, database_url="postgresql://a:b@h/x", postgres_host="db")
    assert override.resolved_database_url() == "postgresql://a:b@h/x"
    assert Settings(_env_file=None, database_url="").resolved_database_url() == ""


def test_lifespan_repairs_orphan_runs() -> None:
    with database.begin() as db:
        db.add(
            RunRow(
                run_id="orphan-1",
                container_id="CA-01",
                done=False,
                payload={"run_id": "orphan-1", "done": False},
            )
        )
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
    with database.begin() as db:
        row = db.get(RunRow, "orphan-1")
        assert row.done is True
        assert row.payload["done"] is True


def test_plan_scoped_sensor_records_survive_restart_and_follow_plan_delete(
    client: TestClient,
) -> None:
    first = client.post("/api/plans", json={}).json()["plan_id"]
    second = client.post("/api/plans", json={}).json()["plan_id"]
    for plan_id, power in ((first, False), (second, True)):
        readings = [
            {
                "device_id": f"dev-{plan_id[:4]}",
                "container_id": "CA-01",
                "plan_id": plan_id,
                "ts": (START + timedelta(minutes=m)).isoformat(),
                "temp_c": 1.0,
                "power_on": power,
            }
            for m in (0, 30)
        ]
        assert client.post("/api/coldchain/readings", json=readings).json()["accepted"] == 2

    fresh = ColdchainStore(db=Database(database._url))
    assert fresh.state("CA-01", first).outage.cumulative_minutes == 30
    assert fresh.state("CA-01", second).outage.cumulative_minutes == 0
    assert {s.plan_id for s in fresh.summaries()} == {first, second}
    first_alerts = fresh.alerts(plan_id=first).alerts
    assert first_alerts and all(a.plan_id == first for a in first_alerts)
    assert fresh.alerts(plan_id=second).alerts == []

    assert client.delete(f"/api/plans/{first}").status_code == 204
    assert [s.plan_id for s in fresh.summaries()] == [second]
    assert fresh.alerts(plan_id=first).alerts == []
    assert client.get("/api/coldchain/containers/CA-01").json()["plan_id"] == second


def test_learning_revision_guard_survives_restart(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={}).json()["plan_id"]
    older, old_snapshot = store.record_arrival_snapshot(
        plan_id, ArrivalRequest(lot_id="lot-jinju-sb-1", inspected_loss_pct=5)
    )
    newer, new_snapshot = store.record_arrival_snapshot(
        plan_id, ArrivalRequest(lot_id="lot-jinju-sb-1", inspected_loss_pct=9)
    )
    QualityHistory().record_arrival(plan_id, new_snapshot, newer)
    QualityHistory().record_arrival(plan_id, old_snapshot, older)
    saved = [o for o in QualityHistory().observations() if o.plan_id == plan_id]
    assert len(saved) == 1 and saved[0].actual_loss_rate == 0.09
