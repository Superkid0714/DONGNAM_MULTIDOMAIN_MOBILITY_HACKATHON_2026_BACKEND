import json
import re
from datetime import timedelta

from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID
from test_saas_api import REDUCE, _assign, _platform_plan

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Assignment, ContainerLoad, Plan
from freshswitch_backend.execution.card import (
    build_execution_card,
    iso6346_check_digit,
    mock_container_no,
)
from freshswitch_backend.execution.models import ExternalRequest
from freshswitch_backend.execution.orders import saas_request_text
from freshswitch_backend.execution.params import ExecutionParams

MONEY_WORDS = ("cost", "fee", "krw", "price", "amount", "saving")
STRAWBERRY_LOTS = ["lot-jinju-sb-1", "lot-miryang-sb-1", "lot-sancheong-sb-1"]


def _scenario():
    scenario = get_scenario("default")
    assert scenario is not None
    return scenario


def _request(kind: str, before: int, after: int) -> ExternalRequest:
    scenario = _scenario()
    voyage = next(v for v in scenario.voyages if v.id == "V-2611A")
    return ExternalRequest(
        request_id="REQ-1",
        kind=kind,
        counterparty_role="carrier",
        counterparty="선사",
        channel="포워더 → 선사",
        deadline=voyage.cy_cutoff - timedelta(hours=24),
        voyage_id="V-2611A",
        container_id="CA-01",
        lot_ids=STRAWBERRY_LOTS,
        pallets_before=before,
        pallets_after=after,
        text="원문",
    )


def test_gross_weight_adds_pallet_and_packaging() -> None:
    params = ExecutionParams()
    per_pallet = 415 + params.pallet_tare_kg.value + params.packaging_kg_per_pallet.value
    assert per_pallet == 475
    text = saas_request_text("plan-x", _request("booking_amendment", 17, 15), _scenario())
    gross_before = round(17 * per_pallet)
    gross_after = round(15 * per_pallet)
    assert f"{gross_before:,}→{gross_after:,}kg" in text
    tare = params.container_tare_kg.value
    assert f"VGM {round(gross_before + tare):,}→{round(gross_after + tare):,}kg" in text
    assert "7,055" not in text and "6,225" not in text
    assert "pallet_tare_kg" in ExecutionParams.model_fields
    assert params.pallet_tare_kg.source.startswith("가정")
    assert params.packaging_kg_per_pallet.source.startswith("가정")


def test_pallet_only_change_is_si_and_vgm_correction() -> None:
    text = saas_request_text("plan-x", _request("booking_amendment", 17, 15), _scenario())
    assert text.startswith("[SI 정정 + VGM 재제출 요청]")
    assert "부킹 변경" not in text
    assert "SI 마감 11/16 18:00" in text and "VGM 마감 11/17 06:00" in text


def test_container_count_change_is_booking_change() -> None:
    added = saas_request_text("plan-x", _request("booking_new", 0, 5), _scenario())
    removed = saas_request_text("plan-x", _request("booking_cancel", 5, 0), _scenario())
    assert added.startswith("[부킹 변경 요청: 컨테이너 1대 추가]")
    assert removed.startswith("[부킹 변경 요청: 컨테이너 1대 취소]")
    assert f"{5 * 475:,}kg" in added and f"{5 * 475:,}kg" in removed
    assert "SI 정정" not in added


def test_other_requests_keep_text() -> None:
    request = _request("reefer_plug", 5, 5)
    assert saas_request_text("plan-x", request, _scenario()) == "원문"


def test_iso6346_container_number() -> None:
    assert iso6346_check_digit("CSQU305438") == 3
    number = mock_container_no("plan-x", "CA-01")
    assert re.fullmatch(r"[A-Z]{3}U\d{7}", number)
    assert int(number[-1]) == iso6346_check_digit(number[:10])
    assert mock_container_no("plan-x", "CA-01") == number
    assert mock_container_no("plan-y", "CA-01") != number


def test_demo_order_text_unchanged(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    event = client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE})
    assert event.status_code == 200, event.text
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert applied.status_code == 200, applied.text
    order = client.get(f"/api/plans/{plan_id}/orders").json()[-1]
    amendment = next(r for r in order["requests"] if r["kind"] == "booking_amendment")
    assert amendment["text"].startswith("[부킹 변경 요청]")
    assert "17→17파렛트(7,055→7,055kg)" in amendment["text"]
    assert "SI 정정 + VGM" not in json.dumps(order, ensure_ascii=False)


def _saas_plan(platform_client: TestClient, saas_login) -> str:
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    _assign(plan_id, FORWARDER_ORG_ID)
    return plan_id


def test_forwarder_orders_use_saas_wording(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _saas_plan(platform_client, saas_login)
    assert (
        platform_client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE}).status_code
        == 200
    )
    applied = platform_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"}
    )
    assert applied.status_code == 200, applied.text
    orders = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    amendment = next(r for r in orders[0]["requests"] if r["kind"] == "booking_amendment")
    assert amendment["text"].startswith("[SI 정정 + VGM 재제출 요청]")
    assert "17→17파렛트, 화물 총중량 8,075→8,075kg" in amendment["text"]
    assert "부킹 변경" not in amendment["text"]


def test_forwarder_plan_detail_has_execution_card(
    platform_client: TestClient,
    saas_login,
    forwarder_client: TestClient,
    other_forwarder_client: TestClient,
) -> None:
    plan_id = _saas_plan(platform_client, saas_login)
    detail = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}").json()
    card = detail["execution_card"]
    assert card["mock"] is True and card["plan_id"] == plan_id
    assert card["containers"]
    scenario = _scenario()
    voyages = {v.id: v for v in scenario.voyages}
    for row in card["containers"]:
        assert row["mock"] is True
        assert re.fullmatch(r"FRSU\d{7}", row["container_no"])
        assert int(row["container_no"][-1]) == iso6346_check_digit(row["container_no"][:10])
        assert row["booking_no"].startswith("FSBK") and row["seal_no"].startswith("FS")
        weight = row["weight"]
        assert weight["net_kg"] == weight["pallets"] * 415
        assert weight["gross_cargo_kg"] == weight["pallets"] * 475
        assert weight["vgm_kg"] == weight["gross_cargo_kg"] + 4800
        setpoint = row["setpoint"]
        assert setpoint["assumed"] is True
        assert setpoint["temp_min_c"] <= setpoint["set_temp_c"] <= setpoint["temp_max_c"]
        if "strawberry" in setpoint["product_ids"]:
            assert setpoint["o2_pct"] == 10.0 and setpoint["co2_pct"] == 15.0
            assert setpoint["ca_controlled"] is True
        assert row["empty_pickup"]["assumed"] is True
        cutoffs = row["cutoffs"]
        voyage = voyages[row["voyage_id"]]
        assert cutoffs["cy_cutoff"] == voyage.cy_cutoff.isoformat()
        assert cutoffs["si_cutoff"] == (voyage.cy_cutoff - timedelta(hours=24)).isoformat()
        assert "ETD" in cutoffs["basis"]
    keys = json.dumps(
        [list(_keys(row)) for row in card["containers"]] + [list(card)], ensure_ascii=False
    )
    for word in MONEY_WORDS:
        assert word not in keys
    endpoint = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/execution-card")
    assert endpoint.status_code == 200
    assert endpoint.json() == card
    assert (
        other_forwarder_client.get(
            f"/api/saas/forwarder/plans/{plan_id}/execution-card"
        ).status_code
        == 404
    )


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _mixed_scenario():
    scenario = _scenario()
    products = [
        p.model_copy(update={"pallet_kg": 600.0}) if p.id == "grape_so2" else p
        for p in scenario.products
    ]
    return scenario.model_copy(update={"products": products})


def _mixed_plan(scenario, composition: dict[str, int]) -> Plan:
    lots = {lot.id: lot for lot in scenario.lots}
    assignments = [
        Assignment(
            lot_id=lot_id,
            shipper_id=lots[lot_id].shipper_id,
            product_id=lots[lot_id].product_id,
            pallets=n,
            mode="sea",
            container_id="CA-01",
            voyage_id="V-2611A",
        )
        for lot_id, n in composition.items()
    ]
    load = ContainerLoad(
        container_id="CA-01",
        voyage_id="V-2611A",
        used=True,
        pallets=sum(composition.values()),
        capacity_pallets=20,
        utilization=0.75,
        lot_ids=list(composition),
        product_ids=sorted({lots[x].product_id for x in composition}),
        shipper_ids=sorted({lots[x].shipper_id for x in composition}),
        container_cost=0,
    )
    return Plan(
        scenario_id="default",
        status="optimal",
        objective=0,
        solve_ms=0,
        assignments=assignments,
        unassigned=[],
        containers=[load],
        shipper_costs=[],
        settlements=[],
        total_baseline_cost=0,
        total_shared_cost=0,
        total_savings=0,
        total_fee=0,
    )


def test_gross_weight_follows_lot_composition() -> None:
    scenario = _mixed_scenario()
    before = {"lot-jinju-sb-1": 10, "lot-sangju-gr-1": 5}
    after = {"lot-jinju-sb-1": 5, "lot-sangju-gr-1": 10}
    request = _request("booking_amendment", 15, 15).model_copy(
        update={
            "lot_ids": sorted(before),
            "lot_pallets_before": before,
            "lot_pallets_after": after,
        }
    )
    text = saas_request_text("plan-x", request, scenario)
    gross = [
        build_execution_card("plan-x", 1, scenario, _mixed_plan(scenario, c))
        .containers[0]
        .weight.gross_cargo_kg
        for c in (before, after)
    ]
    assert gross[0] != gross[1]
    assert f"화물 총중량 {gross[0]:,}→{gross[1]:,}kg" in text


def test_saas_texts_keep_shipper_breakdown() -> None:
    breakdown = "진주 8→6, 산청 5→3"
    for kind, before, after in (
        ("booking_amendment", 13, 9),
        ("booking_cancel", 13, 0),
        ("booking_new", 0, 9),
    ):
        request = _request(kind, before, after).model_copy(
            update={"text": f"[원문] 화주별 파렛트: {breakdown}. 회신 부탁드립니다."}
        )
        text = saas_request_text("plan-x", request, _scenario())
        assert f"화주별 파렛트: {breakdown}." in text


def test_shipper_and_forwarder_see_same_saas_wording(
    platform_client: TestClient,
    saas_login,
    forwarder_client: TestClient,
    shipper_client: TestClient,
) -> None:
    plan_id = _saas_plan(platform_client, saas_login)
    assert (
        platform_client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE}).status_code
        == 200
    )
    applied = platform_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"}
    )
    assert applied.status_code == 200, applied.text
    forwarder = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    shipper = shipper_client.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    sent = next(r for r in forwarder[0]["requests"] if r["kind"] == "booking_amendment")
    seen = next(r for r in shipper[0]["requests"] if r["request_id"] == sent["request_id"])
    assert seen["text"].startswith("[SI 정정 + VGM 재제출 요청]")
    assert "화주별 파렛트:" in seen["text"] and "화주별 파렛트:" in sent["text"]
    head = sent["text"].split(" 대상 출하:")[0]
    assert seen["text"].split(" 대상 출하:")[0] == head
