from datetime import date, datetime, time, timedelta

from saas_fixtures import PLATFORM_ORG_ID as SANDBOX
from saas_fixtures import create_real_platform
from sqlalchemy import select

from freshswitch_backend.api import forecast as forecast_api
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Voyage
from freshswitch_backend.forecast.observations import (
    clear_observations,
    clear_org_observations,
    own_records,
    planned_voyages,
    record_matches,
    record_schedule_check,
)
from freshswitch_backend.forecast.predictor import DelayPredictor, default_predictor
from freshswitch_backend.forecast.service import (
    ForecastOptions,
    delay_event,
    forecast_voyages,
    remaining_risks,
)
from freshswitch_backend.integrations import SnapshotStore, VesselMovement
from freshswitch_backend.integrations.mof_snapshot import demo_movements
from freshswitch_backend.integrations.mof_vessel import KST, match_departures
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import DelayObservationRow
from freshswitch_backend.planning.service import build_plan


def _voyage(etd: datetime, name: str = "MV FRESH HARMONY", call_sign: str | None = None) -> Voyage:
    return Voyage(
        id=f"V-{etd:%m%d}",
        vessel_name=name,
        terminal="부산신항 2부두",
        origin="KRPUS",
        destination="HKHKG",
        etd=etd,
        cy_cutoff=etd - timedelta(days=1),
        transit_days=3,
        call_sign=call_sign,
    )


def _departure(name: str, at: datetime, call_sign: str = "FH01") -> VesselMovement:
    return VesselMovement(
        port_code="020",
        call_sign=call_sign,
        vessel_name=name,
        vessel_kind="풀컨테이너선",
        departure_berth="신항 2부두 1선석",
        departure_at=at,
    )


def _past_voyages(weeks: int = 3) -> tuple[list[Voyage], list[VesselMovement]]:
    base = datetime(2026, 10, 7, 18, 0, tzinfo=KST)
    voyages = [_voyage(base + timedelta(weeks=i)) for i in range(weeks)]
    movements = [_departure(v.vessel_name, v.etd + timedelta(hours=10)) for v in voyages]
    return voyages, movements


def test_planned_voyages_carry_call_sign():
    voyage = _voyage(datetime(2026, 11, 18, 18, 0, tzinfo=KST), call_sign="5LYK5")
    assert planned_voyages([voyage])[0].call_sign == "5LYK5"


def test_real_snapshot_departure_is_recorded_by_call_sign(demo_tenants):
    snapshot = SnapshotStore().load(date(2026, 10, 29), "O")
    assert snapshot is not None
    real = next(m for m in snapshot.movements if m.call_sign == "5LYK5")
    voyage = _voyage(datetime(2026, 10, 28, 18, 0, tzinfo=KST), call_sign="5LYK5")
    assert (
        record_schedule_check(SANDBOX, [voyage], snapshot.movements, trusted=True, plan_id="p1")
        == 1
    )
    with database.begin() as db:
        row = db.scalars(select(DelayObservationRow)).one()
    assert row.matched_by == "call_sign" and row.call_sign == "5LYK5"
    assert row.scope == "sandbox" and row.plan_id == "p1" and row.terminal == "신항 2부두"
    assert row.departure_at == real.departure_at
    assert row.delay_hours == round((real.departure_at - voyage.etd).total_seconds() / 3600, 2)


def test_planned_only_movement_is_not_an_observation(demo_tenants):
    voyage = _voyage(datetime(2026, 10, 28, 18, 0, tzinfo=KST))
    movement = VesselMovement(
        port_code="020",
        call_sign="FH01",
        vessel_name=voyage.vessel_name,
        planned_departure_at=voyage.etd + timedelta(hours=5),
    )
    assert record_schedule_check(SANDBOX, [voyage], [movement], trusted=True) == 0
    assert own_records(SANDBOX) == []


def test_untrusted_movements_are_not_recorded(demo_tenants):
    voyages, movements = _past_voyages()
    assert record_schedule_check(SANDBOX, voyages, movements, trusted=False) == 0
    assert own_records(SANDBOX) == []


def test_demo_scope_is_never_recorded(demo_tenants):
    voyages, movements = _past_voyages()
    assert record_schedule_check("demo", voyages, movements, trusted=True) == 0
    matches = match_departures(planned_voyages(voyages), movements)
    assert record_matches("demo", matches) == 0
    with database.begin() as db:
        assert db.scalars(select(DelayObservationRow)).all() == []


def test_demo_fixture_movements_are_dropped(demo_tenants):
    voyages = get_scenario("default").voyages
    synthetic = demo_movements(date(2026, 11, 18), date(2026, 11, 22), "O")
    assert synthetic
    assert record_schedule_check(SANDBOX, voyages, synthetic, trusted=True) == 0
    assert own_records(SANDBOX) == []


def test_observations_accumulate_once_per_voyage_and_stay_in_scope(demo_tenants):
    live = create_real_platform()
    voyages, movements = _past_voyages()
    assert record_schedule_check(SANDBOX, voyages, movements, trusted=True) == 3
    assert record_schedule_check(SANDBOX, voyages, movements, trusted=True) == 3
    assert len(own_records(SANDBOX)) == 3
    assert all(r.delay_hours == 10 for r in own_records(SANDBOX))
    assert own_records(live) == []
    assert own_records("demo") == []
    record_schedule_check(live, voyages[:1], movements[:1], trusted=True)
    assert len(own_records(live)) == 1
    assert len(own_records(SANDBOX)) == 3


def test_clear_observations(demo_tenants):
    live = create_real_platform()
    voyages, movements = _past_voyages()
    record_schedule_check(SANDBOX, voyages, movements, trusted=True)
    record_schedule_check(live, voyages, movements, trusted=True)
    assert clear_observations("sandbox", world_id="seed") == 3
    assert own_records(SANDBOX) == []
    assert len(own_records(live)) == 3
    assert clear_org_observations(live) == 3
    assert own_records(live) == []


def test_duplicate_vessel_and_etd_is_stored_once(demo_tenants):
    etd = datetime(2026, 10, 7, 18, 0, tzinfo=KST)
    first = _voyage(etd)
    second = first.model_copy(update={"id": "V-TWIN"})
    movements = [
        _departure(first.vessel_name, etd + timedelta(hours=10)),
        _departure(first.vessel_name, etd + timedelta(hours=4)),
    ]
    assert record_schedule_check(SANDBOX, [first, second], movements, trusted=True) == 1
    assert [r.delay_hours for r in own_records(SANDBOX)] == [4]


def test_delay_bounds_limit_what_is_stored(demo_tenants):
    etd = datetime(2026, 10, 7, 18, 0, tzinfo=KST)
    voyage = _voyage(etd)
    early = [_departure(voyage.vessel_name, etd - timedelta(hours=60))]
    assert len(match_departures(planned_voyages([voyage]), early)) == 1
    assert record_schedule_check(SANDBOX, [voyage], early, trusted=True) == 0
    assert own_records(SANDBOX) == []
    edge = [_departure(voyage.vessel_name, etd - timedelta(hours=48))]
    assert record_schedule_check(SANDBOX, [voyage], edge, trusted=True) == 1
    assert [r.delay_hours for r in own_records(SANDBOX)] == [-48]
    later = _voyage(etd + timedelta(weeks=2))
    late = [_departure(later.vessel_name, later.etd + timedelta(hours=130))]
    assert record_schedule_check(SANDBOX, [later], late, trusted=True, max_gap_hours=200) == 0


def test_own_records_use_kst_and_dedupe_against_history(demo_tenants):
    base = default_predictor().records
    known = next(r for r in base if r.call_sign == "5LYK5")
    voyage = _voyage(known.planned_departure_at, call_sign="5LYK5")
    movement = _departure(voyage.vessel_name, known.departure_at, call_sign="5LYK5")
    assert record_schedule_check(SANDBOX, [voyage], [movement], trusted=True) == 1
    own = own_records(SANDBOX)
    assert own[0].planned_departure_at.utcoffset() == timedelta(hours=9)
    assert own[0].key == known.key
    predictor, added = forecast_api._predictor(own)
    assert added == 0 and len(predictor.records) == len(base)


def test_history_end_is_kst_date(demo_tenants):
    last = max(r.departure_at for r in default_predictor().records)
    etd = datetime.combine(last.date() + timedelta(days=2), time(2, 0), tzinfo=KST)
    voyage = _voyage(etd, call_sign="ZZ01")
    movement = _departure(voyage.vessel_name, etd + timedelta(hours=1), call_sign="ZZ01")
    record_schedule_check(SANDBOX, [voyage], [movement], trusted=True)
    predictor, added = forecast_api._predictor(own_records(SANDBOX))
    assert added == 1
    assert forecast_api._history(predictor, added).end == etd.date()


def test_delay_forecast_adds_own_observations(client, demo_tenants, monkeypatch):
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    url = f"/api/plans/{plan_id}/delay-forecast?llm=false"
    before = client.post(url, json={}).json()
    assert before["history"]["own_observation_count"] == 0
    assert "자체 관측" not in before["summary"]
    voyages, movements = _past_voyages()
    record_schedule_check(SANDBOX, voyages, movements, trusted=True, plan_id=plan_id)
    demo = client.post(url, json={}).json()
    assert demo["history"] == before["history"] and demo["summary"] == before["summary"]
    monkeypatch.setattr(forecast_api, "own_records", lambda org_id: own_records(SANDBOX))
    after = client.post(url, json={}).json()
    history = after["history"]
    assert history["own_observation_count"] == 3
    assert history["record_count"] == before["history"]["record_count"] + 3
    assert history["own_observation_source"].startswith("자체 관측")
    assert "자체 관측 3건 반영" in after["summary"]
    top = after["voyages"][0]["forecast"]["basis"][-1]
    assert top["level"] == "vessel" and top["sample_size"] == 3


def test_remaining_risk_voyages_are_listed():
    scenario = get_scenario("default")
    plan = build_plan(scenario).plan
    storm = forecast_voyages(
        DelayPredictor(default_predictor().records),
        scenario,
        plan,
        ForecastOptions(preset="typhoon"),
    )
    first = storm[0]
    second = first.model_copy(
        update={
            "voyage_id": "V-OTHER",
            "lots": [lot.model_copy(update={"pallets": 1}) for lot in first.lots],
        }
    )
    forecasts = [first, second]
    event = delay_event(forecasts)
    assert event is not None and event.voyage_id == first.voyage_id
    assert event.voyage_ids == [first.voyage_id, "V-OTHER"]
    assert remaining_risks(forecasts, event) == []
    single = delay_event(forecasts, skip={"V-OTHER"})
    remaining = remaining_risks(forecasts, single)
    assert [r.voyage_id for r in remaining] == ["V-OTHER"]
    assert remaining[0].at_risk_lot_ids == [lot.lot_id for lot in first.lots if lot.at_risk]
    assert remaining[0].reason == "이번 재계획에 반영하지 않음"
    assert remaining_risks(storm, delay_event(storm)) == []
