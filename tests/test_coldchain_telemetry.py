import re
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import SAAS_CSRF
from sqlalchemy import select

from freshswitch_backend.coldchain import ColdchainParams
from freshswitch_backend.coldchain.models import Reading
from freshswitch_backend.coldchain.telemetry import (
    luhn_valid,
    reefer_device_id,
    reefer_trace,
    setpoint_for,
)
from freshswitch_backend.coldchain.twin import TwinContext, build_context, evaluate
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.container_numbers import (
    container_number,
    is_iso6346,
    iso6346_check_digit,
    printed_container_number,
)
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import BindingRow, DeviceRow, ForwarderResourceRow
from freshswitch_backend.planning.service import build_plan

END = datetime(2026, 11, 5, 3, 7, tzinfo=UTC)
PARAMS = ColdchainParams()
BANDS = {"strawberry": (0.0, 2.0), "grape_so2": (-0.5, 0.0), "point": (-0.5, 0.5)}
SEEDS = [f"w{index:012x}:SZLU{index:07d}" for index in range(12)]
FORBIDDEN = re.compile(r"synthetic|demo|e2e|test|sim-|fake", re.IGNORECASE)


def _trace(seed: str, band: tuple[float, float]):
    return reefer_trace(
        seed,
        device_id=reefer_device_id(seed),
        container_id="SZLU1234565",
        plan_id=None,
        band_min_c=band[0],
        band_max_c=band[1],
        end=END,
    )


def _evaluate(trace, band: tuple[float, float]):
    ctx = TwinContext(
        container_id="SZLU1234565", stage="terminal", band_min_c=band[0], band_max_c=band[1]
    )
    readings = [Reading(**r.model_dump(exclude={"plan_id", "stage"})) for r in trace.readings]
    return evaluate(readings, ctx, PARAMS)


def test_iso6346_check_digit_matches_the_standard_example() -> None:
    assert iso6346_check_digit("CSQU305438") == 3
    assert is_iso6346("CSQU3054383")
    assert not is_iso6346("CSQU3054384")
    assert printed_container_number("CSQU3054383") == "CSQU 305438 3"


def test_container_numbers_are_valid_reefer_numbers_and_stable() -> None:
    numbers = {container_number(seed) for seed in SEEDS}
    assert len(numbers) == len(SEEDS)
    assert all(is_iso6346(number) and number[3] == "U" for number in numbers)
    assert container_number(SEEDS[0]) == container_number(SEEDS[0])
    assert container_number(SEEDS[0], 1) != container_number(SEEDS[0])
    assert is_iso6346(container_number(SEEDS[0], 1))


def test_device_ids_look_like_modem_serials() -> None:
    ids = {reefer_device_id(seed) for seed in SEEDS}
    assert len(ids) == len(SEEDS)
    for device_id in ids:
        assert re.fullmatch(r"RCM-35\d{13}", device_id)
        assert luhn_valid(device_id[4:])
        assert not FORBIDDEN.search(device_id)


@pytest.mark.parametrize("product", sorted(BANDS))
@pytest.mark.parametrize("seed", SEEDS[:6])
def test_trace_shape_is_plausible(product: str, seed: str) -> None:
    band = BANDS[product]
    trace = _trace(seed, band)
    readings = trace.readings
    assert readings == _trace(seed, band).readings
    steps = {(b.ts - a.ts) for a, b in pairwise(readings)}
    assert steps == {timedelta(minutes=5)}
    assert readings[-1].ts == datetime(2026, 11, 5, 3, 5, tzinfo=UTC)
    span = readings[-1].ts - readings[0].ts
    assert timedelta(hours=26) <= span <= timedelta(hours=34)
    assert trace.setpoint_c == setpoint_for(*band)
    assert band[0] <= trace.setpoint_c < band[1]
    door = [r for r in readings if r.door_open]
    assert door and door[0] is readings[0] and len(door) <= 3
    off = [r for r in readings if not r.power_on]
    assert 20 <= trace.outage.minutes <= 30 and len(off) == trace.outage.minutes // 5
    assert all(trace.outage.covers(r.ts) for r in off)
    assert readings[-1].power_on
    assert len(trace.defrosts) >= 2
    gaps = [b.window.start - a.window.start for a, b in pairwise(trace.defrosts)]
    assert all(timedelta(hours=6) <= gap <= timedelta(hours=16) for gap in gaps)
    headroom = band[1] - trace.setpoint_c - 0.15
    assert all(
        0.4 * headroom - 0.01 <= d.amplitude_c <= 0.8 * headroom + 0.01
        and 20 <= d.window.minutes <= 30
        for d in trace.defrosts
    )
    temps = [r.temp_c for r in readings]
    assert min(temps) >= band[0] - 0.3
    assert max(temps) - band[1] < PARAMS.warning_excursion_c.value
    steady = [
        r.temp_c for r in readings[-12:] if not any(d.window.covers(r.ts) for d in trace.defrosts)
    ]
    assert steady and all(abs(t - trace.setpoint_c) <= 0.3 for t in steady)
    assert all(70 <= (r.humidity_pct or 0) <= 100 for r in readings)


@pytest.mark.parametrize("product", sorted(BANDS))
def test_twin_figures_follow_the_trace(product: str) -> None:
    band = BANDS[product]
    trace = _trace(SEEDS[3], band)
    ev = _evaluate(trace, band)
    assert ev.outage.episodes == 1
    assert ev.outage.cumulative_minutes == trace.outage.minutes
    assert ev.outage.longest_continuous_minutes == trace.outage.minutes
    assert ev.status == "normal"
    assert ev.excursion.max_temp_c == max(r.temp_c for r in trace.readings)
    above = [r for r in trace.readings if r.temp_c > band[1]]
    assert (ev.excursion.minutes_above_band > 0) == bool(above)
    assert ev.excursion.minutes_above_band <= 5 * (len(above) + 1) * 2
    kinds = [a.kind for a in ev.alerts]
    assert kinds.count("power_cut") == 1 and kinds.count("power_restored") == 1
    assert all(a.level != "critical" for a in ev.alerts)
    keys = {a.action.kind for a in ev.alerts if a.action is not None}
    assert "terminal_priority_plug" in keys
    assert "cold_storage_booking" not in keys and "insurance_evidence" not in keys


def test_point_band_widens_to_reefer_tolerance() -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    products = [
        p.model_copy(update={"temp_min_c": 0.0, "temp_max_c": 0.0}) if p.id == "strawberry" else p
        for p in scenario.products
    ]
    point = scenario.model_copy(update={"products": products})
    plan = build_plan(point).plan
    contexts = [
        build_context(c.container_id, "terminal", PARAMS, "p", point, plan)
        for c in plan.containers
        if c.used
    ]
    strawberry = [c for c in contexts if [p.product_id for p in c.products] == ["strawberry"]]
    assert strawberry
    assert all((c.band_min_c, c.band_max_c) == (-0.5, 0.5) for c in strawberry)
    original = build_plan(scenario).plan
    for c in original.containers:
        if c.used:
            ctx = build_context(c.container_id, "terminal", PARAMS, "p", scenario, original)
            assert (ctx.band_min_c, ctx.band_max_c) in {(0.0, 2.0), (-0.5, 0.0)}


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def test_visitor_plan_starts_with_realistic_telemetry(saas_app) -> None:
    client = TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF)
    me = client.post("/api/auth/demo-login", json={"new_world": True})
    assert me.status_code == 200, me.text
    world_id = me.json()["world"]["world_id"]
    owner = next(
        item["org_id"]
        for item in me.json()["memberships"]
        if item["org_id"].endswith("forwarder-dongnam")
    )
    assert client.put("/api/auth/active-org", json={"org_id": owner}).status_code == 200
    created = client.post("/api/plans", json={"include_registered": True})
    assert created.status_code == 201, created.text
    body = created.json()
    plan_id = body["plan_id"]
    used = sorted({a["container_id"] for a in body["plan"]["assignments"] if a["mode"] == "sea"})
    assert used
    assert all(is_iso6346(cid) for cid in used)
    summaries = client.get("/api/coldchain/containers", params={"plan_id": plan_id})
    assert summaries.status_code == 200, summaries.text
    rows = {row["container_id"]: row for row in summaries.json()}
    assert set(rows) == set(used)
    for container_id in used:
        state = client.get(
            f"/api/coldchain/containers/{container_id}", params={"plan_id": plan_id}
        ).json()
        assert state["has_data"] and state["status"] == "normal"
        assert 26 * 12 <= state["reading_count"] <= 34 * 12 + 1
        assert len(state["device_ids"]) == 1
        device_id = state["device_ids"][0]
        assert not FORBIDDEN.search(device_id) and luhn_valid(device_id[4:])
        assert device_id == reefer_device_id(f"{world_id}:{container_id}")
        band = state["excursion"]
        assert band["band_max_c"] > band["band_min_c"]
        assert 20 <= state["outage"]["cumulative_minutes"] <= 30
        assert state["outage"]["episodes"] == 1
        assert state["outage"]["current_continuous_minutes"] == 0
        assert state["current"]["power_on"] is True
        kinds = [a["kind"] for a in state["alerts"]]
        assert kinds.count("power_cut") == 1 and kinds.count("power_restored") == 1
        assert state["actions"] and all(a["status"] == "done" for a in state["actions"])
        loss = state["loss"]
        assert loss["sensor_loss_rate"] >= loss["no_outage_loss_rate"]
        assert loss["predicted_loss_rate"] >= loss["planned_loss_rate"]
        assert (
            rows[container_id]["cumulative_outage_minutes"] == state["outage"]["cumulative_minutes"]
        )
    with database.begin() as db:
        devices = db.scalars(select(DeviceRow).where(DeviceRow.org_id == owner)).all()
        names = [d.name or "" for d in devices]
        assert len(devices) == len(used)
        assert all(not FORBIDDEN.search(name) for name in names)
        bindings = db.scalars(select(BindingRow).where(BindingRow.org_id == owner)).all()
        assert {b.container_id for b in bindings} == set(used)
        resources = db.scalars(
            select(ForwarderResourceRow.id).where(
                ForwarderResourceRow.org_id.startswith(f"{world_id}-")
            )
        ).all()
        assert resources and all(is_iso6346(rid) for rid in resources)


def test_visitor_telemetry_is_deterministic_per_world() -> None:
    first = _trace("w0123456789ab:SZLU1234565", BANDS["strawberry"])
    second = _trace("w0123456789ab:SZLU1234565", BANDS["strawberry"])
    other = _trace("wba9876543210:SZLU1234565", BANDS["strawberry"])
    assert [r.temp_c for r in first.readings] == [r.temp_c for r in second.readings]
    assert [r.temp_c for r in first.readings] != [r.temp_c for r in other.readings]
