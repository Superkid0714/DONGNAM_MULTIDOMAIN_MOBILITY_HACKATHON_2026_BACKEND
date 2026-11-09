from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.coldchain import ColdchainParams, coldchain_store
from freshswitch_backend.coldchain.models import Reading
from freshswitch_backend.coldchain.simulator import PRESETS, preset_readings
from freshswitch_backend.coldchain.twin import (
    build_context,
    downsample,
    evaluate,
    exposure_index,
    sensor_rate,
)
from freshswitch_backend.domain.models import KST

START = datetime(2026, 11, 17, 9, 0, tzinfo=KST)
PARAMS = ColdchainParams()


@pytest.fixture(autouse=True)
def clean_coldchain():
    coldchain_store.clear()
    yield
    coldchain_store.clear()


def _plan(client: TestClient) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def _reading(minute: int, temp: float, power_on: bool, device: str = "dev-1") -> dict:
    return {
        "device_id": device,
        "container_id": "CA-01",
        "ts": (START + timedelta(minutes=minute)).isoformat(),
        "temp_c": temp,
        "power_on": power_on,
    }


def _series(points: list[tuple[int, float, bool]]) -> list[Reading]:
    return [
        Reading(
            device_id="d",
            container_id="CA-01",
            ts=START + timedelta(minutes=m),
            temp_c=t,
            power_on=p,
        )
        for m, t, p in points
    ]


def test_loss_rate_interpolates_jlst_range() -> None:
    assert exposure_index(0, 0, PARAMS) == 0
    assert sensor_rate(0.015, 0.08, 0, PARAMS) == pytest.approx(0.015)
    reference = exposure_index(356, 356 * 3.0, PARAMS)
    assert reference == pytest.approx(1.0)
    assert sensor_rate(0.015, 0.08, reference, PARAMS) == pytest.approx(0.07)
    assert sensor_rate(0.015, 0.08, 5.0, PARAMS) == pytest.approx(0.08)
    half = sensor_rate(0.015, 0.08, 0.5, PARAMS)
    assert 0.04 < half < 0.045


def test_thermal_exposure_counts_without_outage() -> None:
    hot = exposure_index(0, 28 * 120, PARAMS)
    assert sensor_rate(0.015, 0.08, hot, PARAMS) > 0.06


def test_status_thresholds() -> None:
    ctx = build_context("CA-01", "terminal", PARAMS)
    assert (ctx.band_min_c, ctx.band_max_c) == (0.0, 2.0)
    assert evaluate([], ctx, PARAMS).status == "no_data"
    assert evaluate(_series([(0, 1.0, True)]), ctx, PARAMS).status == "normal"
    assert evaluate(_series([(0, 3.9, True)]), ctx, PARAMS).status == "normal"
    assert evaluate(_series([(0, 4.0, True)]), ctx, PARAMS).status == "warning"
    assert evaluate(_series([(0, 1.0, False)]), ctx, PARAMS).status == "warning"
    assert evaluate(_series([(0, 6.0, True)]), ctx, PARAMS).status == "critical"
    assert evaluate(_series([(0, -2.5, True)]), ctx, PARAMS).status == "warning"
    long_cut = _series([(0, 1.0, False), (60, 1.5, False), (120, 1.9, False)])
    ev = evaluate(long_cut, ctx, PARAMS)
    assert ev.status == "critical"
    assert ev.outage.current_continuous_minutes == 120


def test_outage_and_degree_minutes() -> None:
    ctx = build_context("CA-01", "terminal", PARAMS)
    ev = evaluate(
        _series([(0, 2.0, True), (10, 2.0, False), (40, 5.0, False), (70, 2.0, True)]),
        ctx,
        PARAMS,
    )
    assert ev.outage.cumulative_minutes == 60
    assert ev.outage.longest_continuous_minutes == 60
    assert ev.outage.episodes == 1
    assert ev.excursion.degree_minutes == pytest.approx(90.0)
    assert ev.excursion.max_temp_c == 5.0


def test_gap_is_capped() -> None:
    ctx = build_context("CA-01", "terminal", PARAMS)
    ev = evaluate(_series([(0, 1.0, False), (600, 1.0, False)]), ctx, PARAMS)
    assert ev.outage.cumulative_minutes == PARAMS.max_gap_minutes.value


def test_actions_follow_thresholds() -> None:
    ctx = build_context("CA-01", "terminal", PARAMS)
    minutes = list(range(0, 61, 5))
    ev = evaluate(_series([(m, 1.0, True) for m in minutes]), ctx, PARAMS)
    assert [a for a in ev.alerts if a.kind == "action"] == []
    cut = _series([(m, 1.0, False) for m in minutes])
    kinds = [(a.action.kind, a.at) for a in evaluate(cut, ctx, PARAMS).alerts if a.action]
    assert kinds == [
        ("terminal_priority_plug", START + timedelta(minutes=10)),
        ("genset_dispatch", START + timedelta(minutes=30)),
        ("entry_time_adjust", START + timedelta(minutes=60)),
    ]
    vessel = build_context("CA-01", "vessel", PARAMS)
    assert not [a for a in evaluate(cut, vessel, PARAMS).alerts if a.action]


def test_long_outage_books_cold_storage_and_insurance() -> None:
    ctx = build_context("CA-01", "terminal", PARAMS)
    ev = evaluate(_series([(m, 1.0, False) for m in range(0, 481, 30)]), ctx, PARAMS)
    kinds = [a.action.kind for a in ev.alerts if a.action]
    assert "cold_storage_booking" in kinds
    assert "insurance_evidence" in kinds
    assert ev.outage.insurance_eligible


def test_ingest_single_and_batch(client: TestClient) -> None:
    single = client.post("/api/coldchain/readings", json=_reading(0, 1.0, True))
    assert single.status_code == 200
    assert single.json()["accepted"] == 1
    batch = client.post(
        "/api/coldchain/readings", json=[_reading(5, 1.1, True), _reading(10, 1.2, False)]
    )
    body = batch.json()
    assert body["accepted"] == 2
    assert body["containers"][0]["status"] == "warning"
    assert body["new_alerts"][0]["kind"] == "power_cut"
    again = client.post("/api/coldchain/readings", json=[_reading(5, 1.1, True)]).json()
    assert again["accepted"] == 0
    assert again["duplicates"] == 1
    state = client.get("/api/coldchain/containers/CA-01").json()
    assert state["reading_count"] == 3
    assert state["plan_id"] is None
    assert state["loss"]["lots"] == []


def test_ingest_requires_container_or_binding(client: TestClient) -> None:
    orphan = {"device_id": "x", "temp_c": 1.0, "power_on": True}
    assert client.post("/api/coldchain/readings", json=orphan).status_code == 422
    plan_id = _plan(client)
    bind = client.post(
        "/api/coldchain/bindings",
        json={"device_id": "x", "container_id": "CA-01", "plan_id": plan_id, "stage": "gate"},
    )
    assert bind.status_code == 201
    response = client.post("/api/coldchain/readings", json=orphan)
    assert response.status_code == 200
    assert response.json()["containers"][0]["plan_id"] == plan_id
    state = client.get("/api/coldchain/containers/CA-01").json()
    assert state["stage"] == "gate"
    assert state["voyage_id"] == "V-2611A"
    assert [p["product_id"] for p in state["products"]] == ["strawberry"]
    assert client.get("/api/coldchain/bindings").json()[0]["device_id"] == "x"


def test_bad_plan_or_container(client: TestClient) -> None:
    plan_id = _plan(client)
    missing = client.post(
        "/api/coldchain/bindings", json={"device_id": "a", "container_id": "CA-01", "plan_id": "no"}
    )
    assert missing.status_code == 404
    wrong = client.post(
        "/api/coldchain/bindings",
        json={"device_id": "a", "container_id": "CA-99", "plan_id": plan_id},
    )
    assert wrong.status_code == 422
    empty = client.get("/api/coldchain/containers/CA-77")
    assert empty.status_code == 200
    twin = empty.json()
    assert twin["has_data"] is False
    assert twin["reading_count"] == 0
    assert twin["timeline"] == []
    assert twin["loss"]["delta_krw"] is None
    assert twin["status"] == "no_data"
    assert client.get("/api/coldchain/containers").json() == []
    with_plan = client.get("/api/coldchain/containers/CA-01", params={"plan_id": plan_id}).json()
    assert with_plan["has_data"] is False
    assert with_plan["plan_id"] == plan_id
    assert with_plan["loss"]["delta_krw"] == 0
    assert with_plan["outage"]["cumulative_minutes"] == 0


def test_actions_are_idempotent(client: TestClient) -> None:
    first = [_reading(m, 1.0 + m / 20, False) for m in range(0, 41, 5)]
    r1 = client.post("/api/coldchain/readings", json=first).json()
    kinds = [a["kind"] for a in r1["new_alerts"]]
    assert kinds.count("action") == 2
    r2 = client.post("/api/coldchain/readings", json=first).json()
    assert r2["new_alerts"] == []
    r3 = client.post("/api/coldchain/readings", json=[_reading(45, 3.5, False)]).json()
    assert [a["kind"] for a in r3["new_alerts"]] == []
    client.post("/api/coldchain/readings", json=[_reading(50, 2.0, True)])
    second = [_reading(m, 1.5, False) for m in range(60, 81, 5)]
    r4 = client.post("/api/coldchain/readings", json=second).json()
    actions = [a["action"]["kind"] for a in r4["new_alerts"] if a["kind"] == "action"]
    assert actions == ["terminal_priority_plug", "entry_time_adjust"]
    state = client.get("/api/coldchain/containers/CA-01").json()
    assert len(state["actions"]) == 4
    assert len({a["key"] for a in state["actions"]}) == 4


def test_simulator_preset_is_deterministic() -> None:
    preset = PRESETS["power_cut_restore"]
    a = preset_readings(
        preset, device_id="s", container_id="CA-01", plan_id=None, band_min_c=0, band_max_c=2
    )
    b = preset_readings(
        preset, device_id="s", container_id="CA-01", plan_id=None, band_min_c=0, band_max_c=2
    )
    assert a == b
    assert len(a) == preset.readings == 85
    assert sum(1 for r in a if not r.power_on) == 48
    assert a[0].ts == START


def test_simulate_instant_with_plan(client: TestClient) -> None:
    plan_id = _plan(client)
    presets = client.get("/api/coldchain/simulate/presets").json()
    assert {p["id"] for p in presets} == {
        "power_cut_restore",
        "short_outage",
        "normal",
        "sea_to_land",
    }
    run = client.post(
        "/api/coldchain/simulate", json={"preset": "power_cut_restore", "plan_id": plan_id}
    ).json()
    assert run["done"] is True
    assert run["sent"] == 85
    state = client.get("/api/coldchain/containers/CA-01", params={"points": 20}).json()
    assert len(state["timeline"]) == 20
    assert state["timeline"][-1]["ts"] == run["end"]
    assert state["outage"]["cumulative_minutes"] == 240
    loss = state["loss"]
    assert loss["no_outage_loss_rate"] == 0.015
    assert 0.06 <= loss["sensor_loss_rate"] <= 0.08
    assert loss["planned_loss_rate"] == 0.033
    assert loss["delta_krw"] > 0
    assert sum(s["delta_krw"] for s in loss["shippers"]) == loss["delta_krw"]
    kinds = [a["kind"] for a in state["actions"]]
    assert kinds == [
        "terminal_priority_plug",
        "genset_dispatch",
        "entry_time_adjust",
        "cold_storage_booking",
    ]
    texts = [a["request"]["text"] for a in state["actions"]]
    assert texts[0].startswith("[리퍼 우선 연결 요청] 부산신항 2부두 MV FRESH HARMONY")
    assert "E-COOLPAC" in texts[1]
    assert texts[3].startswith("[현지 냉장창고 예약 요청] 홍콩항행")
    again = client.post(
        "/api/coldchain/simulate", json={"preset": "power_cut_restore", "plan_id": plan_id}
    ).json()
    assert again["state"] == run["state"]


def test_short_outage_stays_within_plan(client: TestClient) -> None:
    plan_id = _plan(client)
    client.post("/api/coldchain/simulate", json={"preset": "short_outage", "plan_id": plan_id})
    state = client.get("/api/coldchain/containers/CA-01").json()
    assert state["loss"]["sensor_loss_rate"] < 0.033
    assert state["loss"]["delta_krw"] == 0
    assert [a["kind"] for a in state["actions"]] == ["terminal_priority_plug", "genset_dispatch"]


def test_simulate_stream_mode(client: TestClient) -> None:
    run = client.post(
        "/api/coldchain/simulate", json={"preset": "normal", "interval_seconds": 0.001}
    ).json()
    assert run["mode"] == "stream"
    done = coldchain_store.wait(run["run_id"])
    assert done.done and done.sent == done.total == 25
    assert client.get(f"/api/coldchain/simulate/{run['run_id']}").json()["done"] is True
    assert client.get("/api/coldchain/containers/CA-01").json()["status"] == "normal"


def test_alert_feed_cursor_and_sse(client: TestClient) -> None:
    client.post("/api/coldchain/simulate", json={"preset": "short_outage"})
    feed = client.get("/api/coldchain/alerts").json()
    assert len(feed["alerts"]) == 6
    cursor = feed["alerts"][2]["seq"]
    rest = client.get("/api/coldchain/alerts", params={"since": cursor}).json()
    assert [a["seq"] for a in rest["alerts"]] == [a["seq"] for a in feed["alerts"][3:]]
    assert rest["next_since"] == feed["next_since"]
    empty = client.get("/api/coldchain/alerts", params={"since": feed["next_since"]}).json()
    assert empty == {"alerts": [], "next_since": feed["next_since"]}
    stream = client.get("/api/coldchain/alerts/stream", params={"since": cursor, "timeout": 0})
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.text.count("event: alert") == 3


def test_settlement_preview(client: TestClient) -> None:
    plan_id = _plan(client)
    client.post("/api/coldchain/simulate", json={"plan_id": plan_id})
    preview = client.get("/api/coldchain/containers/CA-01/settlement-preview").json()
    assert len(preview["lots"]) == 3
    assert preview["total_refund_change"] > 0
    assert all(x["predicted_loss_rate"] > x["planned_loss_rate"] for x in preview["lots"])
    settlement = client.get(f"/api/plans/{plan_id}/settlement").json()
    assert settlement["inspections"] == []


def test_reset_container_and_demo_reset(client: TestClient, reset_admin_headers) -> None:
    client.post("/api/coldchain/simulate", json={"preset": "normal"})
    assert client.delete("/api/coldchain/containers/CA-01").status_code == 204
    assert client.get("/api/coldchain/containers").json() == []
    client.post("/api/coldchain/simulate", json={"preset": "normal"})
    body = client.post("/api/demo/reset", headers=reset_admin_headers).json()
    assert body["cleared_coldchain_containers"] == 1


def test_downsample_keeps_ends() -> None:
    ctx = build_context("CA-01", "terminal", PARAMS)
    ev = evaluate(_series([(m, 1.0, True) for m in range(100)]), ctx, PARAMS)
    points = downsample(ev.timeline, 10)
    assert len(points) == 10
    assert points[0].ts == ev.timeline[0].ts and points[-1].ts == ev.timeline[-1].ts
