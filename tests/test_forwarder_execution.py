import json
import re

from fastapi.testclient import TestClient
from test_coldchain_power_plan import _with_peak
from test_forwarder_execution_card import _mixed_plan, _request, _saas_plan, _scenario
from test_saas_api import REDUCE

from freshswitch_backend.api.saas_forwarder import _card
from freshswitch_backend.coldchain.power_plan import power_plan
from freshswitch_backend.domain.models import Product, Scenario
from freshswitch_backend.execution.card import build_execution_card
from freshswitch_backend.execution.orders import (
    PLAIN_GLOSSARY,
    PLAIN_TEXT,
    document_action,
    genset_instructions,
    plain_text,
    saas_requests,
)
from freshswitch_backend.execution.params import ExecutionParams
from freshswitch_backend.planning.store import store

MONEY = re.compile(r"\d[\d,]*\s*원|₩|KRW|운임|요금|비용")
ALL_KINDS = (
    ("booking_amendment", 17, 15),
    ("booking_new", 0, 5),
    ("booking_cancel", 5, 0),
    ("reefer_plug", 5, 5),
    ("cy_entry", 5, 5),
    ("air_booking", 0, 3),
    ("air_cancel", 3, 0),
)


def test_document_action_follows_container_count() -> None:
    same = saas_requests("plan-x", [_request("booking_amendment", 17, 15)], _scenario())[0]
    assert same.document_action == "si_vgm_correction"
    assert same.document_action_label == "SI·VGM 정정"
    assert same.text.startswith("[SI 정정 + VGM 재제출 요청]")
    for kind, before, after in (("booking_new", 0, 5), ("booking_cancel", 5, 0)):
        changed = saas_requests("plan-x", [_request(kind, before, after)], _scenario())[0]
        assert changed.document_action == "booking_change"
        assert changed.document_action_label == "부킹 변경"
        assert changed.text.startswith("[부킹 변경 요청")
    for kind in ("reefer_plug", "cy_entry", "air_booking", "air_cancel"):
        assert document_action(_request(kind, 5, 5)) is None


def test_vgm_items_add_up_with_sources() -> None:
    scenario = _scenario()
    plan = _mixed_plan(scenario, {"lot-jinju-sb-1": 8, "lot-sancheong-sb-1": 5})
    weight = build_execution_card("plan-x", 1, scenario, plan).containers[0].weight
    params = ExecutionParams()
    names = [item.name for item in weight.items]
    assert names == ["화물 순중량", "파렛트", "포장", "컨테이너 자중"]
    assert sum(item.kg for item in weight.items) == weight.vgm_kg
    assert weight.net_kg == 13 * 415
    assert weight.pallet_tare_kg == 13 * params.pallet_tare_kg.value
    assert weight.packaging_kg == 13 * params.packaging_kg_per_pallet.value
    assert weight.pallet_tare_kg + weight.packaging_kg == weight.pallet_packaging_kg
    assert weight.gross_cargo_kg + weight.container_tare_kg == weight.vgm_kg
    assumed = {item.name: item.assumed for item in weight.items}
    assert assumed == {"화물 순중량": False, "파렛트": True, "포장": True, "컨테이너 자중": True}
    assert all(item.source for item in weight.items)
    assert weight.calculation.startswith(f"VGM {weight.vgm_kg:,}kg = 화물 순중량 5,395kg")
    assert "컨테이너 자중 4,800kg(가정)" in weight.calculation


def test_plain_text_for_every_request_kind() -> None:
    names = [s.name for s in _scenario().shippers]
    for kind, before, after in ALL_KINDS:
        request = _request(kind, before, after).model_copy(
            update={"text": "[원문] 화주별 파렛트: 진주 8→6, 산청 5→3. 회신 부탁드립니다."}
        )
        filled = saas_requests("plan-x", [request], _scenario())[0]
        assert filled.plain_text and filled.plain_text == plain_text(request)
        assert 1 <= filled.plain_text.count(".") <= 2
        assert not MONEY.search(filled.plain_text)
        assert not any(name.split()[0] in filled.plain_text for name in names)
        assert "lot-" not in filled.plain_text
    extension = _request("reefer_plug", 5, 5).model_copy(
        update={"text": "[리퍼 전원 연결 연장 요청] ..."}
    )
    assert plain_text(extension) == PLAIN_TEXT["reefer_plug_extension"]
    assert all(term and meaning for term, meaning in PLAIN_GLOSSARY)


def test_shipper_orders_have_plain_text_without_others(
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
    sent = next(r for r in forwarder[0]["requests"] if r["kind"] == "booking_amendment")
    assert sent["document_action"] == "si_vgm_correction"
    shipper = shipper_client.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    requests = shipper[0]["requests"]
    assert requests
    others = [s.name for s in _scenario().shippers if s.id != "sh-jinju"]
    for request in requests:
        assert request["plain_text"]
        assert not MONEY.search(request["plain_text"])
        assert not any(name.split()[0] in request["plain_text"] for name in others)
    seen = next(r for r in requests if r["request_id"] == sent["request_id"])
    assert seen["document_action"] == "si_vgm_correction"
    assert seen["plain_text"] == PLAIN_TEXT["si_vgm_correction"]
    assert "젯셋" not in json.dumps(shipper, ensure_ascii=False)


def test_demo_orders_have_no_saas_fields(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    assert client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE}).status_code == 200
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert applied.status_code == 200, applied.text
    order = client.get(f"/api/plans/{plan_id}/orders").json()[-1]
    for request in order["requests"]:
        assert request["document_action"] is None and request["plain_text"] is None


def test_default_card_has_no_genset_lines(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _saas_plan(platform_client, saas_login)
    card = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/execution-card").json()
    assert card["genset_instructions"] == []
    detail = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}").json()
    assert detail["execution_card"]["genset_instructions"] == []


def test_peak_gate_makes_genset_line(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    session = store.get(plan_id)
    assert session is not None
    assert _card(session).genset_instructions == []
    truck_id = session.routes[0].pickup.routes[0].truck_id
    peak = _with_peak(session, truck_id)
    candidates = power_plan(peak).candidates
    assert candidates
    card = _card(peak)
    lines = card.genset_instructions
    assert [line.key for line in lines] == [c.key for c in candidates]
    numbers = {c.container_id: c.container_no for c in card.containers}
    for line, candidate in zip(lines, candidates, strict=True):
        assert line.text.startswith("[배터리 젯셋 사전 배정]")
        assert line.lot_ids == candidate.lot_ids
        assert line.container_nos == [numbers[c] for c in candidate.container_ids]
        assert all(lot_id in line.text for lot_id in line.lot_ids)
        assert line.plain_text == PLAIN_TEXT["genset"]
        assert not MONEY.search(line.text)
    lots = {lot.id: lot.shipper_id for lot in peak.scenario.lots}
    shippers = {lots[x] for line in lines for x in line.lot_ids}
    assert len(shippers) > 1
    owner = lots[lines[0].lot_ids[0]]
    mine = {x for x, sid in lots.items() if sid == owner}
    scoped = genset_instructions(candidates, card, visible_lot_ids=mine)
    assert scoped
    assert all(set(line.lot_ids) <= mine for line in scoped)
    others = {x for x, sid in lots.items() if sid != owner}
    assert not any(x in line.text for line in scoped for x in others)


def _swap_requests():
    cancel = _request("booking_cancel", 17, 0).model_copy(
        update={"request_id": "REQ-A", "container_id": "CA-01"}
    )
    new = _request("booking_new", 0, 17).model_copy(
        update={"request_id": "REQ-B", "container_id": "CA-02"}
    )
    return [cancel, new]


def test_container_swap_with_same_count_is_si_vgm_correction() -> None:
    swapped = saas_requests("plan-x", _swap_requests(), _scenario())
    for request in swapped:
        assert request.document_action == "si_vgm_correction"
        assert request.document_action_label == "SI·VGM 정정"
        assert request.plain_text == PLAIN_TEXT["si_vgm_correction"]
        assert request.text.startswith("[SI 정정 + VGM 재제출 요청]")
        assert "컨테이너 CA-01→CA-02 교체" in request.text
        assert "부킹 변경" not in request.text


def test_container_swap_with_different_kind_is_booking_change() -> None:
    scenario = _scenario()
    containers = [
        c.model_copy(update={"kind": "40RH"}) if c.id == "CA-02" else c for c in scenario.containers
    ]
    scenario = scenario.model_copy(update={"containers": containers})
    swapped = saas_requests("plan-x", _swap_requests(), scenario)
    for request in swapped:
        assert request.document_action == "booking_change"
        assert request.text.startswith("[부킹 변경 요청")
        assert request.plain_text == PLAIN_TEXT[request.kind]


def test_genset_line_does_not_repeat_reason(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    session = store.get(plan_id)
    assert session is not None
    peak = _with_peak(session, session.routes[0].pickup.routes[0].truck_id)
    candidates = power_plan(peak).candidates
    assert any(c.gate_peak for c in candidates)
    lines = genset_instructions(candidates, _card(peak))
    for line, candidate in zip(lines, candidates, strict=True):
        assert "근거" not in line.text
        assert line.text.count("무전원 예상") == 1
        assert line.text.endswith("게이트 혼잡 시간대 도착.") == candidate.gate_peak
        assert line.text == line.text.rstrip()


def test_missing_pallet_kg_stays_assumed_after_round_trip() -> None:
    scenario = _scenario()
    products = [
        Product.model_validate(
            {k: v for k, v in p.model_dump().items() if k not in ("pallet_kg", "pallet_kg_assumed")}
        )
        for p in scenario.products
    ]
    scenario = scenario.model_copy(update={"products": products})
    loaded = Scenario.model_validate(scenario.model_dump(mode="json"))
    plan = _mixed_plan(loaded, {"lot-jinju-sb-1": 8, "lot-sancheong-sb-1": 5})
    weight = build_execution_card("plan-x", 1, loaded, plan).containers[0].weight
    net = next(item for item in weight.items if item.name == "화물 순중량")
    assert net.assumed is True
    assert net.kg == 13 * 415
    assert all(p.pallet_kg == 415 and p.pallet_kg_assumed for p in loaded.products)
    explicit = Product.model_validate(_scenario().products[0].model_dump(mode="json"))
    assert explicit.pallet_kg_assumed is False
