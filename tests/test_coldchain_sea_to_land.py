from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.coldchain import ColdchainParams, coldchain_store
from freshswitch_backend.coldchain.models import Reading, ReadingIn
from freshswitch_backend.coldchain.simulator import PRESETS, preset_readings
from freshswitch_backend.coldchain.twin import build_context, evaluate, exposure_index
from freshswitch_backend.domain.models import KST

START = datetime(2026, 11, 17, 9, 0, tzinfo=KST)
PARAMS = ColdchainParams()
OUTAGE_START = 30


@pytest.fixture(autouse=True)
def clean_coldchain():
    coldchain_store.clear()
    yield
    coldchain_store.clear()


def _plan(client: TestClient) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def _context():
    return build_context("CA-01", "terminal", PARAMS)


def _scenario(off_minutes: int | None = None) -> list[Reading]:
    readings: list[Reading] = []
    for item in preset_readings(
        PRESETS["sea_to_land"],
        device_id="d",
        container_id="CA-01",
        plan_id=None,
        band_min_c=0.0,
        band_max_c=2.0,
    ):
        readings.append(Reading(**item.model_dump(exclude={"plan_id", "stage"})))
    if off_minutes is None:
        return readings
    return [
        r.model_copy(update={"power_on": True})
        if not r.power_on and _elapsed(r) >= off_minutes
        else r
        for r in readings
    ]


def _elapsed(reading: Reading) -> float:
    return (reading.ts - START).total_seconds() / 60 - OUTAGE_START


def _upto(minute_off: int) -> list[Reading]:
    return [r for r in _scenario() if _elapsed(r) <= minute_off]


def _alert_titles(readings: list[Reading]) -> list[str]:
    return [a.title for a in evaluate(readings, _context(), PARAMS).alerts]


def test_preset_shape() -> None:
    preset = PRESETS["sea_to_land"]
    assert preset.outage_minutes == 45
    assert preset.total_minutes == 150
    assert preset.readings == 31
    assert [p.handover_step for p in preset.phases] == [1, 2, 3, 4, 5, 6, 7]
    assert [p.power_on for p in preset.phases] == [True, False, False, False, True, True, True]


def test_thresholds_are_sourced_as_assumption() -> None:
    assert PARAMS.planned_outage_warning_minutes.value == 20
    assert PARAMS.planned_outage_allowance_minutes.value == 30
    assert PARAMS.planned_outage_warning_minutes.source.startswith("가정")
    assert PARAMS.planned_outage_allowance_minutes.source.startswith("가정")


def test_no_alert_before_twenty_minutes() -> None:
    ev = evaluate(_upto(15), _context(), PARAMS)
    assert ev.alerts == []
    assert ev.status == "normal"
    assert ev.handover is not None
    assert ev.handover.outage_kind == "planned"
    assert ev.handover.current_step == 3
    assert ev.handover.continuous_off_minutes == 15


def test_single_warning_at_twenty_minutes() -> None:
    ev = evaluate(_upto(30), _context(), PARAMS)
    assert ev.status == "warning"
    warnings = [a for a in ev.alerts if a.level == "warning"]
    assert len(ev.alerts) == 1
    assert len(warnings) == 1
    assert warnings[0].at == START + timedelta(minutes=OUTAGE_START + 20)
    assert "계획 단전 주의" in warnings[0].title
    assert ev.handover is not None
    assert ev.handover.outage_kind == "planned_warning"
    assert not any(a.action for a in ev.alerts)


def test_critical_and_priority_plug_past_thirty_minutes() -> None:
    ev = evaluate(_upto(35), _context(), PARAMS)
    assert ev.status == "critical"
    critical = [a for a in ev.alerts if a.level == "critical"]
    assert len(critical) == 1
    assert critical[0].at == START + timedelta(minutes=OUTAGE_START + 35)
    assert "이상 단전" in critical[0].title
    actions = [a.action for a in ev.alerts if a.action]
    assert [a.kind for a in actions] == ["terminal_priority_plug"]
    assert actions[0].request is not None
    assert actions[0].request.counterparty_role == "terminal"
    assert ev.handover is not None
    assert ev.handover.outage_kind == "abnormal"
    assert ev.handover.current_step == 4


def test_full_run_tracks_seven_steps_and_excess() -> None:
    ev = evaluate(_scenario(), _context(), PARAMS)
    assert [p.handover_step for p in ev.timeline][::5] == [1, 1, 4, 5, 6, 7, 7]
    assert sorted({p.handover_step for p in ev.timeline}) == [1, 2, 3, 4, 5, 6, 7]
    assert ev.outage.cumulative_minutes == 45
    assert ev.handover is not None
    assert ev.handover.current_step == 7
    assert ev.handover.outage_kind == "none"
    assert ev.handover.cumulative_off_minutes == 45
    assert ev.handover.excess_minutes == 15
    assert ev.handover.allowance.value == 30
    assert [s.step for s in ev.handover.steps] == [1, 2, 3, 4, 5, 6, 7]
    kinds = [a.action.kind for a in ev.alerts if a.action]
    assert kinds == ["terminal_priority_plug"]


def test_loss_only_on_excess_minutes() -> None:
    ctx = _context()
    full = evaluate(_scenario(), ctx, PARAMS)
    untouched = exposure_index(45, full.excursion.degree_minutes, PARAMS)
    assert full.exposure < untouched
    assert full.exposure == pytest.approx(
        exposure_index(15, full.carry.excess_degree_minutes, PARAMS)
    )
    assert full.carry.excess_minutes == 15
    assert full.carry.excess_degree_minutes < full.excursion.degree_minutes

    inside_allowance = evaluate(_scenario(off_minutes=30), ctx, PARAMS)
    assert inside_allowance.outage.cumulative_minutes == 30
    assert inside_allowance.carry.excess_minutes == 0
    assert inside_allowance.exposure == 0


def test_real_readings_keep_default_alerts() -> None:
    plain = [r.model_copy(update={"handover_step": None}) for r in _upto(35)]
    ev = evaluate(plain, _context(), PARAMS)
    assert ev.handover is None
    assert ev.status == "warning"
    assert any(a.kind == "power_cut" for a in ev.alerts)
    actions = {a.action.kind for a in ev.alerts if a.action}
    assert actions == {"terminal_priority_plug", "genset_dispatch"}
    assert ev.exposure == pytest.approx(
        exposure_index(35, ev.excursion.degree_minutes, PARAMS), abs=1e-4
    )


def test_ingest_payload_cannot_mark_handover(client: TestClient) -> None:
    plan_id = _plan(client)
    payload = {
        "device_id": "dev-real",
        "container_id": "CA-01",
        "plan_id": plan_id,
        "ts": datetime.now(KST).isoformat(),
        "temp_c": 1.0,
        "power_on": False,
        "handover_step": 2,
    }
    assert "handover_step" not in ReadingIn.model_fields
    assert client.post("/api/coldchain/readings", json=payload).status_code < 300
    state = client.get("/api/coldchain/containers/CA-01", params={"plan_id": plan_id}).json()
    assert state["handover"] is None
    assert any(a["kind"] == "power_cut" for a in state["alerts"])


def test_simulate_sea_to_land_via_api(client: TestClient) -> None:
    plan_id = _plan(client)
    assert "sea_to_land" in {p["id"] for p in client.get("/api/coldchain/simulate/presets").json()}
    run = client.post(
        "/api/coldchain/simulate",
        json={"preset": "sea_to_land", "plan_id": plan_id, "stage": "terminal"},
    ).json()
    assert run["done"] is True
    assert run["total"] == 31
    state = client.get("/api/coldchain/containers/CA-01", params={"plan_id": plan_id}).json()
    assert state["handover"]["current_step"] == 7
    assert state["handover"]["excess_minutes"] == 15
    assert state["handover"]["cumulative_off_minutes"] == 45
    assert state["handover"]["allowance"]["source"].startswith("가정")
    assert state["timeline"][0]["handover_step"] == 1
    assert state["outage"]["cumulative_minutes"] == 45
    loss = state["loss"]
    assert loss["planned_loss_rate"] == 0.033
    assert loss["predicted_loss_rate"] > loss["planned_loss_rate"]
    assert loss["delta_krw"] > 0
    titles = [a["title"] for a in state["alerts"]]
    assert not any("리퍼 전원 차단" in t for t in titles)
    assert sum("계획 단전 주의" in t for t in titles) == 1
    assert sum("이상 단전 전환" in t for t in titles) == 1


@pytest.mark.parametrize(
    ("preset", "outage", "readings", "kinds"),
    [
        (
            "power_cut_restore",
            240,
            85,
            {
                "terminal_priority_plug",
                "genset_dispatch",
                "entry_time_adjust",
                "cold_storage_booking",
            },
        ),
        ("short_outage", 45, 34, {"terminal_priority_plug", "genset_dispatch"}),
        ("normal", 0, 25, set()),
    ],
)
def test_existing_presets_unchanged(preset, outage, readings, kinds) -> None:
    item = PRESETS[preset]
    assert item.outage_minutes == outage
    assert item.readings == readings
    built = preset_readings(
        item,
        device_id="d",
        container_id="CA-01",
        plan_id=None,
        band_min_c=0.0,
        band_max_c=2.0,
    )
    assert all(type(r) is ReadingIn for r in built)
    series = [Reading(**r.model_dump(exclude={"plan_id", "stage"})) for r in built]
    ev = evaluate(series, _context(), PARAMS)
    assert ev.handover is None
    assert {a.action.kind for a in ev.alerts if a.action} == kinds
    assert ev.exposure == pytest.approx(
        exposure_index(ev.outage.cumulative_minutes, ev.excursion.degree_minutes, PARAMS)
    )
    assert all(p.handover_step is None for p in ev.timeline)
