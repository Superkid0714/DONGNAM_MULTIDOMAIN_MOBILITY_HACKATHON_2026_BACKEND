from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from freshswitch_backend.api import forecast as forecast_api
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Lot, Scenario
from freshswitch_backend.forecast.predictor import default_predictor
from freshswitch_backend.forecast.service import (
    ForecastOptions,
    delay_event,
    forecast_voyages,
    remaining_risks,
)
from freshswitch_backend.optimize import AIR
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.replan import (
    DelayEvent,
    ReplanError,
    VoyageDelay,
    apply_event,
    locations,
    replan,
)
from freshswitch_backend.replan.explanation import event_summary

A_ETD = datetime.fromisoformat("2026-11-22T15:00+09:00")
B_ETD = datetime.fromisoformat("2026-11-25T04:00+09:00")
LATE_LOT = {
    "id": "lot-late-sb",
    "shipper_id": "sh-sangju",
    "product_id": "strawberry",
    "pallets": 14,
    "ready_date": "2026-11-19",
    "destination": "HKHKG",
    "sell_by_date": "2026-11-26",
}
A_LOTS = {"lot-jinju-sb-1", "lot-sancheong-sb-1", "lot-miryang-sb-1"}
B_LOTS = {"lot-miryang-sb-2", "lot-late-sb"}


def _both() -> DelayEvent:
    return DelayEvent(
        voyage_id="V-2611A",
        new_etd=A_ETD,
        additional_voyages=[VoyageDelay(voyage_id="V-2611B", new_etd=B_ETD)],
        source="예측",
    )


@pytest.fixture(scope="module")
def two_voyages() -> Scenario:
    base = get_scenario("default")
    assert base is not None
    late = Lot.model_validate(LATE_LOT)
    return base.model_copy(update={"lots": [*base.lots, late]})


def _plan_with_late_lot(client: TestClient) -> str:
    response = client.post("/api/plans", json={"scenario_id": "default", "lots": [LATE_LOT]})
    assert response.status_code == 201, response.text
    return response.json()["plan_id"]


def test_single_voyage_event_serializes_as_before() -> None:
    event = DelayEvent(voyage_id="V-2611A", new_etd=A_ETD)
    assert "additional_voyages" not in event.model_dump()
    assert event.voyage_ids == ["V-2611A"]
    assert DelayEvent.model_validate(event.model_dump()) == event


def test_duplicate_voyage_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DelayEvent(
            voyage_id="V-2611A",
            new_etd=A_ETD,
            additional_voyages=[VoyageDelay(voyage_id="V-2611A", new_etd=B_ETD)],
        )


def test_multi_voyage_event_moves_every_voyage(two_voyages: Scenario) -> None:
    after = apply_event(two_voyages, _both())
    etd = {v.id: v.etd for v in after.voyages}
    assert etd == {"V-2611A": A_ETD, "V-2611B": B_ETD}
    unknown = _both().model_copy(
        update={"additional_voyages": [VoyageDelay(voyage_id="V-NOPE", new_etd=B_ETD)]}
    )
    with pytest.raises(ReplanError, match="V-NOPE"):
        apply_event(two_voyages, unknown)


def test_one_replan_handles_both_voyages(two_voyages: Scenario) -> None:
    plan = build_plan(two_voyages).plan
    where = locations(plan)
    assert {where[lid] for lid in A_LOTS} == {"CA-01"}
    assert {where[lid] for lid in B_LOTS} == {"CA-03"}
    result = replan(two_voyages, plan, _both())
    assert A_LOTS | B_LOTS <= set(result.free_lot_ids)
    as_is = next(a for a in result.alternatives if a.id == "sail_as_is")
    assert not as_is.feasible
    flagged = {r.split(":")[0] for r in as_is.infeasible_reasons}
    assert A_LOTS | B_LOTS <= flagged
    pull = next(a for a in result.alternatives if a.id == "pull_forward")
    assert result.recommended_id == "pull_forward" and pull.feasible
    moved = locations(pull.plan)
    assert all(moved[lid] == AIR for lid in A_LOTS | B_LOTS)
    nxt = next(a for a in result.alternatives if a.id == "next_voyage")
    assert not nxt.feasible
    assert any(r.startswith("lot-late-sb:") for r in nxt.infeasible_reasons)
    assert not any("이후 항차 없음" in r for r in nxt.infeasible_reasons)
    reasons = {r.split(":")[0]: r for r in nxt.infeasible_reasons}
    assert all("V-2611B/CA-03" in reasons[lid] for lid in A_LOTS)
    assert all("V-2611A/" in reasons[lid] for lid in B_LOTS)
    placed = locations(nxt.plan)
    assert placed["lot-sangju-gr-1"] == placed["lot-pohang-gr-1"] == "CA-03"


@pytest.mark.parametrize(
    ("a_etd", "b_etd"),
    [
        ("2026-11-19T18:00+09:00", "2026-11-21T19:00+09:00"),
        ("2026-11-26T15:00+09:00", "2026-11-22T06:00+09:00"),
    ],
)
def test_next_voyage_may_use_another_delayed_voyage(a_etd: str, b_etd: str) -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    plan = build_plan(scenario).plan
    single = DelayEvent(voyage_id="V-2611A", new_etd=datetime.fromisoformat(a_etd))
    joint = single.model_copy(
        update={
            "additional_voyages": [
                VoyageDelay(voyage_id="V-2611B", new_etd=datetime.fromisoformat(b_etd))
            ]
        }
    )
    for event in (single, joint):
        nxt = next(a for a in replan(scenario, plan, event).alternatives if a.id == "next_voyage")
        assert not any("이후 항차 없음" in r for r in nxt.infeasible_reasons)
        assert all("V-2611B/CA-03" in r for r in nxt.infeasible_reasons)
        placed = locations(nxt.plan)
        assert placed["lot-sangju-gr-1"] == placed["lot-pohang-gr-1"] == "CA-03"


def test_explanation_mentions_every_voyage(two_voyages: Scenario) -> None:
    text = event_summary(two_voyages, _both())
    assert "MV FRESH HARMONY 출항 11/18 18:00→11/22 15:00" in text
    assert "MV COLD BRIDGE 출항 11/21 18:00→11/25 04:00" in text


def test_delay_event_carries_all_at_risk_voyages(two_voyages: Scenario) -> None:
    plan = build_plan(two_voyages).plan
    storm = forecast_voyages(
        default_predictor(), two_voyages, plan, ForecastOptions(preset="typhoon")
    )
    event = delay_event(storm)
    assert event is not None and event.voyage_id == "V-2611A"
    assert event.voyage_ids == ["V-2611A", "V-2611B"]
    assert remaining_risks(storm, event) == []
    single = delay_event(storm, skip={"V-2611B"})
    assert single is not None and single.voyage_ids == ["V-2611A"]
    left = remaining_risks(storm, single, {"V-2611B": "재계획에 넣을 수 없음: 테스트"})
    assert [(r.voyage_id, r.reason) for r in left] == [("V-2611B", "재계획에 넣을 수 없음: 테스트")]
    assert set(left[0].at_risk_lot_ids) == B_LOTS


def test_delay_forecast_replans_both_voyages_at_once(client: TestClient) -> None:
    plan_id = _plan_with_late_lot(client)
    response = client.post(
        f"/api/plans/{plan_id}/delay-forecast?llm=false", json={"preset": "typhoon"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["triggered"] is True
    assert body["remaining_risk_voyages"] == []
    event = body["event"]
    assert event["voyage_id"] == "V-2611A"
    assert [v["voyage_id"] for v in event["additional_voyages"]] == ["V-2611B"]
    replan_body = body["replan"]
    assert replan_body["event"] == event
    assert A_LOTS | B_LOTS <= set(replan_body["free_lot_ids"])
    assert set(body["air_lot_ids"]) == A_LOTS | B_LOTS
    assert "위험 항차 2곳(V-2611A, V-2611B) 함께 재계획" in body["summary"]
    assert "반영하지 않은" not in body["summary"]
    summary = replan_body["explanation"]["operator_summary"]
    assert "MV FRESH HARMONY" in summary and "MV COLD BRIDGE" in summary
    applied = client.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": "pull_forward", "replan_id": replan_body["replan_id"]},
    )
    assert applied.status_code == 200, applied.text
    session = applied.json()
    etd = {v["id"]: v["etd"] for v in session["scenario"]["voyages"]}
    assert etd == {"V-2611A": "2026-11-22T15:00:00+09:00", "V-2611B": "2026-11-25T04:00:00+09:00"}
    modes = {a["lot_id"]: a["mode"] for a in session["plan"]["assignments"]}
    assert all(modes[lid] == "air" for lid in A_LOTS | B_LOTS)


def test_unreplannable_voyage_stays_listed_with_reason(client: TestClient, monkeypatch) -> None:
    plan_id = _plan_with_late_lot(client)
    monkeypatch.setattr(
        forecast_api,
        "unreplannable",
        lambda scenario, voyages: {"V-2611B": "재계획에 넣을 수 없음: 테스트"},
    )
    body = client.post(
        f"/api/plans/{plan_id}/delay-forecast?llm=false", json={"preset": "typhoon"}
    ).json()
    assert "additional_voyages" not in body["event"]
    remaining = body["remaining_risk_voyages"]
    assert [(r["voyage_id"], r["reason"]) for r in remaining] == [
        ("V-2611B", "재계획에 넣을 수 없음: 테스트")
    ]
    assert "이번 재계획에 반영하지 않은 위험 항차" in body["summary"]
    assert "재계획에 넣을 수 없음: 테스트" in body["summary"]


def test_multi_voyage_event_is_idempotent(client: TestClient) -> None:
    plan_id = _plan_with_late_lot(client)
    event = _both().model_dump(mode="json")
    url = f"/api/plans/{plan_id}/events?llm=false"
    first = client.post(url, json={"event": event, "idempotency_key": "multi-1"})
    assert first.status_code == 200, first.text
    second = client.post(url, json={"event": event, "idempotency_key": "multi-1"})
    assert second.status_code == 200
    assert second.json() == first.json()
    assert [v["voyage_id"] for v in first.json()["event"]["additional_voyages"]] == ["V-2611B"]
    fewer = {k: v for k, v in event.items() if k != "additional_voyages"}
    mismatch = client.post(url, json={"event": fewer, "idempotency_key": "multi-1"})
    assert mismatch.status_code == 422


def test_single_voyage_event_api_shape_is_unchanged(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    event = {"type": "delay", "voyage_id": "V-2611A", "new_etd": "2026-11-21T06:00:00+09:00"}
    url = f"/api/plans/{plan_id}/events?llm=false"
    old = client.post(url, json={"event": event}).json()
    explicit = client.post(url, json={"event": {**event, "additional_voyages": []}}).json()
    assert old["event"] == {**event, "new_cy_cutoff": None, "source": "manual"}
    assert explicit["event"] == old["event"]
    assert [a["id"] for a in old["alternatives"]] == [a["id"] for a in explicit["alternatives"]]
    assert old["recommended_id"] == explicit["recommended_id"] == "pull_forward"
    old_plan = {key: value for key, value in old["alternatives"][0]["plan"].items() if key != "solve_ms"}
    explicit_plan = {
        key: value for key, value in explicit["alternatives"][0]["plan"].items() if key != "solve_ms"
    }
    assert old_plan == explicit_plan
    duplicate = client.post(
        url,
        json={
            "event": {
                **event,
                "additional_voyages": [{"voyage_id": "V-2611A", "new_etd": event["new_etd"]}],
            }
        },
    )
    assert duplicate.status_code == 422
