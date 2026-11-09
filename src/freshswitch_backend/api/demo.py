import json
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from freshswitch_backend.api.access import require_demo_reset
from freshswitch_backend.api.saas_sandbox import SandboxResetResponse, sandbox_reset_response
from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import load_scenarios
from freshswitch_backend.integrations import SnapshotDate, SnapshotStore
from freshswitch_backend.integrations.mof_snapshot import load_demo_fixture
from freshswitch_backend.lots import lot_store
from freshswitch_backend.persistence.sandbox_reset import demo_reset_lock, reset_sandbox
from freshswitch_backend.persistence.sandbox_world import WorldNotFound
from freshswitch_backend.persistence.tables import DEMO_ORG
from freshswitch_backend.planning.store import store
from freshswitch_backend.quality import quality_history

router = APIRouter(prefix="/demo", tags=["demo"])

DEFAULT_CURL_BASE = "https://freshswitch.ssalhub.xyz/api"


class ResetResponse(BaseModel):
    cleared_sessions: int
    cleared_coldchain_containers: int = 0
    cleared_quality_observations: int = 0
    cleared_lots: int = 0


class DemoStatus(BaseModel):
    offline_mode: bool
    explain_use_llm: bool
    llm_configured: bool
    mof_key_configured: bool
    snapshot_dates: list[SnapshotDate]
    demo_fixture: bool
    scenario_ids: list[str]
    session_count: int


class DemoCapture(BaseModel):
    name: str
    path: str
    kind: Literal["string", "json"] = "string"


class DemoCheck(BaseModel):
    path: str
    op: Literal["eq", "contains", "len", "gt"] = "eq"
    value: Any


class DemoStep(BaseModel):
    step: int
    id: str
    title: str
    description: str
    method: Literal["GET", "POST"]
    path: str
    body: dict[str, Any] | None = None
    body_from: str | None = None
    capture: list[DemoCapture] = Field(default_factory=list)
    checks: list[DemoCheck] = Field(default_factory=list)
    expect: str


def _confirm_shipper(
    step_id: str, shipper_id: str, label: str, order_var: str, status: str, stage: str
) -> dict[str, Any]:
    return {
        "id": step_id,
        "title": f"{label} 화주 수락 ({stage})",
        "description": f"{label} 화주가 실행 지시의 비용·일정 변경을 수락한다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/orders/{" + order_var + "}/confirm",
        "body": {"actor": "shipper", "shipper_id": shipper_id, "accept": True},
        "body_from": f"경로의 {{{order_var}}}는 앞 실행 지시 조회 응답 마지막 항목의 order_id",
        "checks": [{"path": ".status", "value": status}],
        "expect": f"200, status {status}",
    }


def _confirm_forwarder(step_id: str, order_var: str, stage: str) -> dict[str, Any]:
    return {
        "id": step_id,
        "title": f"포워더 확정 ({stage})",
        "description": "화주 확인이 끝난 지시를 포워더가 확정한다. 계획이 committed가 되고 자동 조치가 실행된다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/orders/{" + order_var + "}/confirm",
        "body": {"actor": "forwarder", "accept": True},
        "body_from": f"경로의 {{{order_var}}}는 앞 실행 지시 조회 응답 마지막 항목의 order_id",
        "checks": [
            {"path": ".status", "value": "forwarder_confirmed"},
            {"path": ".automatic[0].done", "value": True},
        ],
        "expect": "200, status forwarder_confirmed, automatic[].done = true",
    }


DELAY_SHIPPERS = [
    ("sh-jinju", "진주"),
    ("sh-miryang", "밀양"),
    ("sh-pohang", "포항"),
    ("sh-sancheong", "산청"),
    ("sh-sangju", "상주"),
]

_STEPS: list[dict[str, Any]] = [
    {
        "id": "reset",
        "title": "시연 초기화",
        "description": "메모리의 계획 세션을 모두 지우고 처음부터 시작한다",
        "method": "POST",
        "path": "/api/demo/reset",
        "expect": "200, cleared_sessions",
    },
    {
        "id": "intake_strawberry",
        "title": "간편 출하 등록: 산청 딸기 2파렛트",
        "description": "품목·수량·출하일·선과장·목적지 5개만 넣으면 판매기한·로트 ID를 채우고 "
        "해상 컨테이너별·항공 가능 여부를 바로 알려준다(미리보기, 세션은 안 바뀜)",
        "method": "POST",
        "path": "/api/intake/lots",
        "body": {
            "product": "딸기",
            "pallets": 2,
            "ready_date": "2026-11-17",
            "shipper": "산청",
            "destination": "홍콩",
        },
        "checks": [
            {"path": ".resolved.shipper_id", "value": "sh-sancheong"},
            {"path": ".defaults_applied", "op": "len", "value": 2},
            {"path": ".hints.earliest_feasible.container_id", "value": "CA-01"},
            {"path": ".hints.earliest_feasible.arrival_date", "value": "2026-11-21"},
            {"path": ".hints.air.feasible", "value": True},
            {"path": ".hints.warnings", "op": "len", "value": 0},
            {"path": ".event.type", "value": "add_lot"},
        ],
        "expect": "sell_by_date·lot_id 자동 채움, 가장 빠른 해상 CA-01(V-2611A, 11/21 도착), "
        "항공도 가능, 경고 없음",
    },
    {
        "id": "intake_grape_so2",
        "title": "간편 출하 등록: 포항 샤인머스캣(SO2) 3파렛트",
        "description": "SO2 처리 포도는 딸기와 같은 컨테이너에 못 싣는다는 경고를 미리보기에서 보여준다",
        "method": "POST",
        "path": "/api/intake/lots",
        "body": {
            "product": "샤인머스캣",
            "pallets": 3,
            "ready_date": "2026-11-17",
            "shipper": "포항",
            "destination": "홍콩",
        },
        "checks": [
            {"path": ".resolved.product_id", "value": "grape_so2"},
            {"path": ".hints.warnings[0]", "op": "contains", "value": "별도 CA 컨테이너"},
        ],
        "expect": "warnings[0]: SO2 처리 품목은 ... 별도 CA 컨테이너가 필요합니다",
    },
    {
        "id": "create_plan",
        "title": "공동적재 계획 생성",
        "description": "기본 시나리오(딸기 3화주 + 포도 2화주)를 최적화해 컨테이너 배정·집하 경로·정산을 만든다",
        "method": "POST",
        "path": "/api/plans",
        "body": {"scenario_id": "default"},
        "capture": [{"name": "plan_id", "path": ".plan_id"}],
        "checks": [
            {"path": ".version", "value": 1},
            {"path": ".status", "value": "committed"},
            {"path": ".plan.total_savings", "value": 10318577},
            {"path": ".plan.container_settlements[0].container_id", "value": "CA-01"},
            {"path": ".plan.container_settlements[0].pallets", "value": 17},
            {"path": ".plan.container_settlements[0].total_savings", "value": 4233000},
        ],
        "expect": "201, 절감 10,318,577원(딸기 정산표 423.3만 + 포도 608.6만). "
        "plan_id를 이후 단계 {plan_id}에 넣는다",
    },
    {
        "id": "settlement_ca01",
        "title": "CA-01 화주별 정산표",
        "description": "딸기 1파렛트 정산표(항공 96.9만 / 해상 72.0만 원, 절감 24.9만 원)로 CA-01 화주별 "
        "절감·성과보수 20%·화주 몫 80%를 낸다. 해상 내륙·장비·보관과 품질손실 위험은 가정 표시",
        "method": "GET",
        "path": "/api/plans/{plan_id}/settlement",
        "checks": [
            {"path": ".tariffs[0].air_total", "value": 969000},
            {"path": ".tariffs[0].sea_total", "value": 720000},
            {"path": ".tariffs[0].savings_per_pallet", "value": 249000},
            {"path": ".containers[0].shippers[0].savings", "value": 1992000},
            {"path": ".containers[0].shippers[1].fee", "value": 249000},
            {"path": ".containers[0].shippers[2].shipper_gain", "value": 796800},
            {"path": ".containers[0].total_fee", "value": 846600},
            {"path": ".containers[0].total_shipper_gain", "value": 3386400},
            {"path": ".assumed_items", "op": "contains", "value": "strawberry.sea.inland"},
        ],
        "expect": "진주 8·산청 5·밀양 4 = 17파렛트, 절감 199.2·124.5·99.6만 원(계 423.3만), "
        "성과보수 39.8·24.9·19.9만 원(계 84.7만), 화주 몫 계 338.6만 원",
    },
    {
        "id": "air_switch_short_window",
        "title": "판매 기간 짧은 화주 → 항공",
        "description": "산청 로트의 판매 기간이 짧아지면 해상 품질손실 위험을 119.1만 원으로 잡는다. "
        "해상 179.2만 원 > 항공 96.9만 원이라 엔진이 산청을 항공으로 돌린다(미리보기, 적용 안 함)",
        "method": "POST",
        "path": "/api/plans/{plan_id}/events?llm=false",
        "body": {"event": {"type": "air_switch", "lot_id": "lot-sancheong-sb-1"}},
        "checks": [
            {"path": ".recommended_id", "value": "pull_forward"},
            {"path": ".alternatives[0].plan.assignments[1].lot_id", "value": "lot-sancheong-sb-1"},
            {"path": ".alternatives[0].plan.assignments[1].mode", "value": "air"},
            {
                "path": ".alternatives[0].plan.settlement_tariffs[0].sea_total_short_window",
                "value": 1792000,
            },
        ],
        "expect": "산청 해상 179.2만 > 항공 96.9만 → 산청 항공, 빈자리에 밀양 2차 당겨 싣기",
    },
    {
        "id": "reduce_sancheong",
        "title": "산청 딸기 감축 5→3 팔레트",
        "description": "산청 화주가 출하량을 줄였다. 재최적화 대안 3개(당겨 채우기·다음 항차·그대로 출항)를 "
        "받는다. idempotency_key로 같은 요청을 다시 보내도 결과가 같다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/events?llm=false",
        "body": {
            "event": {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3},
            "idempotency_key": "demo-reduce-sancheong",
        },
        "checks": [
            {"path": ".alternatives", "op": "len", "value": 3},
            {"path": ".recommended_id", "value": "pull_forward"},
            {"path": ".alternatives[1].id", "value": "next_voyage"},
            {"path": ".alternatives[1].feasible", "value": False},
            {"path": ".alternatives[2].id", "value": "sail_as_is"},
            {
                "path": ".alternatives[2].diff.cancellation.payer_shipper_id",
                "value": "sh-sancheong",
            },
            {"path": ".alternatives[2].diff.cancellation.fee", "value": 852706},
            {"path": ".alternatives[0].diff.cancellation.fee", "value": 0},
            {"path": ".explanation.generated_by", "value": "template"},
        ],
        "expect": "대안 3개, 추천 pull_forward, next_voyage는 판매기한 초과로 불가, "
        "그대로 출항이면 산청 취소 부담금 852,706원(당겨 채우면 0원), 템플릿 설명문",
    },
    {
        "id": "apply_pull_forward",
        "title": "당겨 채우기 적용",
        "description": "빈 자리에 밀양 2차 로트를 하루 당겨 실어 항공 대신 해상으로 보낸다. 실행 지시가 생긴다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/apply",
        "body": {"alternative_id": "pull_forward"},
        "checks": [
            {"path": ".version", "value": 2},
            {"path": ".status", "value": "pending_confirmation"},
        ],
        "expect": "200, version 2, status pending_confirmation",
    },
    {
        "id": "orders_pull_forward",
        "title": "실행 지시 확인",
        "description": "확인할 화주(밀양. 진주는 정산표 단가라 비용 변화 없음), 자동 조치, "
        "포워더가 선사·터미널·항공사에 보낼 요청 문장",
        "method": "GET",
        "path": "/api/plans/{plan_id}/orders",
        "capture": [{"name": "order_id", "path": ".[-1].order_id"}],
        "checks": [
            {"path": ".[-1].status", "value": "proposed"},
            {"path": ".[-1].shipper_confirmations", "op": "len", "value": 1},
            {"path": ".[-1].requests[0].kind", "value": "booking_amendment"},
            {"path": ".[-1].requests[0].text", "op": "contains", "value": "[부킹 변경 요청]"},
            {"path": ".[-1].requests[1].kind", "value": "cy_entry"},
            {"path": ".[-1].requests[2].kind", "value": "air_cancel"},
            {"path": ".[-1].requests[2].text", "op": "contains", "value": "[항공 예약 취소 요청]"},
        ],
        "expect": "requests: 부킹 변경(선사)·CY 반입(터미널)·항공 취소(항공사). order_id를 이후 {order_id}에",
    },
    _confirm_shipper(
        "confirm_miryang", "sh-miryang", "밀양", "order_id", "shipper_confirmed", "감축"
    ),
    _confirm_forwarder("confirm_forwarder", "order_id", "감축"),
    {
        "id": "plan_committed",
        "title": "계획 확정 확인",
        "description": "포워더 확정 뒤 세션이 committed, 확정 버전 2가 된다",
        "method": "GET",
        "path": "/api/plans/{plan_id}",
        "checks": [
            {"path": ".status", "value": "committed"},
            {"path": ".committed_version", "value": 2},
        ],
        "expect": "status committed, committed_version 2",
    },
    {
        "id": "fuel_shock",
        "title": "운임 쇼크: 유류할증료 470 → 2,060원",
        "description": "항공 유류할증료를 kg당 470원에서 2,060원(2026.4)으로 올려 다시 푼다. "
        "다음 배 후속 딸기 9파렛트가 항공에서 CA-03 공동적재로 바뀌고 화주별 절감이 늘어난다",
        "method": "POST",
        "path": "/api/analysis/fuel-shock",
        "body": {"scenario_id": "fuel-shock", "surcharges": [470, 2060]},
        "checks": [
            {"path": ".cases[0].plan_changed", "value": False},
            {"path": ".cases[1].plan_changed", "value": True},
            {"path": ".cases[1].air_freight_per_pallet", "value": 1552515},
            {"path": ".cases[1].switches", "op": "len", "value": 3},
            {"path": ".cases[1].switches[0].direction", "value": "air_to_sea"},
            {"path": ".cases[1].savings_delta", "value": 31274400},
            {"path": ".thresholds[1].surcharge_per_kg", "value": 1046},
        ],
        "expect": "2,060원에서 항공→해상 3로트 9파렛트, CA-03 9/20, 총 절감 +31,274,400원. "
        "배정이 바뀌는 할증료 약 1,046원/kg",
    },
    {
        "id": "coldchain_bind",
        "title": "아두이노 센서 ↔ CA-01 연결",
        "description": "소형 냉각 박스의 아두이노(arduino-01)를 계획의 딸기 컨테이너 CA-01에 연결한다. "
        "이후 측정값은 container_id 없이 보내도 CA-01로 들어간다",
        "method": "POST",
        "path": "/api/coldchain/bindings",
        "body": {
            "device_id": "arduino-01",
            "container_id": "CA-01",
            "plan_id": "{plan_id}",
            "stage": "terminal",
        },
        "checks": [{"path": ".container_id", "value": "CA-01"}],
        "expect": "201, arduino-01 → CA-01(터미널 장치장)",
    },
    {
        "id": "coldchain_power_cut",
        "title": "단전 복구 시뮬레이션",
        "description": "실물 아두이노 대신 같은 수집 경로로 정상 1시간 → 터미널 단전 4시간 → 복구 2시간을 "
        "재생한다(5분 간격 85건). interval_seconds를 주면 화면에서 실시간으로 움직인다",
        "method": "POST",
        "path": "/api/coldchain/simulate",
        "body": {
            "preset": "power_cut_restore",
            "container_id": "CA-01",
            "plan_id": "{plan_id}",
            "device_id": "arduino-01",
        },
        "checks": [
            {"path": ".done", "value": True},
            {"path": ".total", "value": 85},
            {"path": ".state.cumulative_outage_minutes", "value": 240.0},
            {"path": ".state.open_actions", "value": 4},
        ],
        "expect": "누적 단전 240분, 조치 4건",
    },
    {
        "id": "coldchain_twin",
        "title": "손실 디지털 트윈",
        "description": "센서 손실률이 단전 없음 1.5%에서 6.6%로 오르고, 화주별 예상 손실액이 계획보다 "
        "늘어난다. 우선 플러그·젯셋·반입 조정·현지 냉장창고 요청 문장이 붙는다",
        "method": "GET",
        "path": "/api/coldchain/containers/CA-01",
        "checks": [
            {"path": ".loss.no_outage_loss_rate", "value": 0.015},
            {"path": ".loss.sensor_loss_rate", "value": 0.066015},
            {"path": ".loss.delta_krw", "value": 4327077},
            {"path": ".actions", "op": "len", "value": 4},
            {"path": ".actions[0].kind", "value": "terminal_priority_plug"},
            {"path": ".actions[1].kind", "value": "genset_dispatch"},
            {"path": ".actions[2].kind", "value": "entry_time_adjust"},
            {"path": ".actions[3].kind", "value": "cold_storage_booking"},
            {
                "path": ".actions[0].request.text",
                "op": "contains",
                "value": "[리퍼 우선 연결 요청]",
            },
        ],
        "expect": "센서 손실률 1.5% → 6.6%, 계획 대비 예상 손실 +4,327,077원, 조치 4건",
    },
    {
        "id": "coldchain_alerts",
        "title": "폰 알림 피드",
        "description": "프런트는 since 커서로 폴링한다(SSE는 /api/coldchain/alerts/stream)",
        "method": "GET",
        "path": "/api/coldchain/alerts?container_id=CA-01",
        "checks": [
            {"path": ".alerts", "op": "len", "value": 10},
            {"path": ".alerts[0].kind", "value": "power_cut"},
        ],
        "expect": "전원 차단·경고·위험·조치 4건·복구·경고 해제 등 10건",
    },
    {
        "id": "detect_delay",
        "title": "해수부 출항 지연 감지",
        "description": "해수부 입출항 정보(오프라인이면 저장 스냅샷)와 시연 선박 실적을 예정 ETD와 비교한다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/schedule-check?demo=true",
        "capture": [{"name": "delay_event", "path": ".events[0]", "kind": "json"}],
        "checks": [
            {"path": ".changes[0].voyage_id", "value": "V-2611A"},
            {"path": ".changes[0].vessel_name", "value": "MV FRESH HARMONY"},
            {"path": ".changes[0].delta_hours", "value": 12},
            {"path": ".events[0].type", "value": "delay"},
        ],
        "expect": "changes[0]: V-2611A MV FRESH HARMONY 12시간 지연, events[0]에 delay 이벤트",
    },
    {
        "id": "replan_delay",
        "title": "지연 반영 재최적화",
        "description": "감지된 지연 이벤트를 그대로 보내 대안별 해상 슬롯과 집하 경로를 다시 계산한다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/events?llm=false",
        "body": {"event": "{delay_event}"},
        "body_from": "{delay_event}는 앞 단계 응답 events[0] 객체 그대로",
        "capture": [{"name": "delay_alternative", "path": ".recommended_id"}],
        "checks": [
            {"path": ".alternatives", "op": "len", "value": 3},
            {"path": ".recommended_id", "value": "pull_forward"},
            {"path": ".alternatives[1].feasible", "value": False},
        ],
        "expect": "next_voyage는 판매기한 초과로 실행 불가, 추천 pull_forward",
    },
    {
        "id": "apply_delay_plan",
        "title": "지연 추천안 적용",
        "description": "재최적화 추천안을 계획에 반영한다. 새 실행 지시가 생긴다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/apply",
        "body": {"alternative_id": "{delay_alternative}"},
        "body_from": "{delay_alternative}는 앞 단계 응답 recommended_id",
        "checks": [
            {"path": ".version", "value": 3},
            {"path": ".status", "value": "pending_confirmation"},
        ],
        "expect": "200, version 3, history 2건",
    },
    {
        "id": "orders_delay",
        "title": "지연 실행 지시 확인",
        "description": "지연으로 보관비가 늘어 5개 화주 모두 확인 대상, 터미널에 리퍼 전원 연장 요청",
        "method": "GET",
        "path": "/api/plans/{plan_id}/orders",
        "capture": [{"name": "delay_order_id", "path": ".[-1].order_id"}],
        "checks": [
            {"path": ".[-1].status", "value": "proposed"},
            {"path": ".[-1].shipper_confirmations", "op": "len", "value": len(DELAY_SHIPPERS)},
            {"path": ".[-1].requests[0].kind", "value": "reefer_plug"},
            {
                "path": ".[-1].requests[0].text",
                "op": "contains",
                "value": "[리퍼 전원 연결 연장 요청]",
            },
        ],
        "expect": "화주 확인 5건(cost_increase), 요청 reefer_plug 2건(CA-01·CA-02). "
        "order_id를 이후 {delay_order_id}에",
    },
    *[
        _confirm_shipper(
            f"confirm_delay_{shipper_id.removeprefix('sh-')}",
            shipper_id,
            label,
            "delay_order_id",
            "shipper_confirmed" if i == len(DELAY_SHIPPERS) - 1 else "proposed",
            "지연",
        )
        for i, (shipper_id, label) in enumerate(DELAY_SHIPPERS)
    ],
    _confirm_forwarder("confirm_delay_forwarder", "delay_order_id", "지연"),
    {
        "id": "compare_air",
        "title": "항공 전환 비교",
        "description": "지연 이후에도 밀양 2차 로트를 해상으로 보내는 게 항공보다 싼지 비교한다",
        "method": "POST",
        "path": "/api/compare/air-vs-sea",
        "body": {"scenario_id": "default", "lot_id": "lot-miryang-sb-2", "plan_id": "{plan_id}"},
        "checks": [
            {"path": ".sea.in_plan", "value": True},
            {"path": ".cheaper", "value": "sea"},
            {"path": ".sea_savings", "value": 55799},
        ],
        "expect": "cheaper sea, 해상이 55,799원 싸다(하루 당김·지연 하루 손실 반영)",
    },
    {
        "id": "ulsan_congestion",
        "title": "울산항 실시간 혼잡도",
        "description": "울산항만공사 선박위치(AIS)에서 소형선을 빼고 묘박 대기·접안·항행을 나눠 "
        "대기 비율과 혼잡 단계를 낸다. 지연 예측의 congestion=auto가 이 값을 쓴다",
        "method": "GET",
        "path": "/api/vessels/ulsan/congestion?include_vessels=false",
        "checks": [
            {"path": ".dataset_url", "op": "contains", "value": "15156631"},
            {"path": ".counts.berthed", "op": "gt", "value": 0},
        ],
        "expect": "source live(실패 시 snapshot), 대기·접안·항행 척수, level normal/elevated/high",
    },
    {
        "id": "forecast_typhoon_air",
        "title": "태풍 접안 지연 예측 → 항공 전환 제안",
        "description": "해수부 출항 이력으로 V-2611A 지연을 예측하고 태풍·혼잡 가정(p90)을 얹는다. "
        "딸기 도착이 판매기한을 넘으면 지연 이벤트로 재계획해 김해·인천 항공을 제안한다(적용은 하지 않음)",
        "method": "POST",
        "path": "/api/plans/{plan_id}/delay-forecast?llm=false",
        "body": {"preset": "typhoon"},
        "checks": [
            {"path": ".triggered", "value": True},
            {"path": ".event.voyage_id", "value": "V-2611A"},
            {"path": ".air_lot_ids", "op": "len", "value": 4},
            {"path": ".air_switch[0].options", "op": "len", "value": 2},
            {"path": ".replan.recommended_id", "value": "pull_forward"},
        ],
        "expect": "triggered true, CA-01 딸기 4로트(진주·산청·밀양 1·2차) 항공 전환 제안"
        "(김해·인천 비용), 추천 pull_forward",
    },
    {
        "id": "benchmark",
        "title": "수작업 규칙 대비 최적화",
        "description": "선착순 채우기 등 수작업 규칙과 최적화의 컨테이너 수·절감액을 같은 비용 기준으로 비교한다",
        "method": "POST",
        "path": "/api/analysis/benchmark",
        "body": {"scenario_id": "default"},
        "checks": [
            {"path": ".methods[0].method", "value": "optimizer"},
            {"path": ".methods[0].containers_used", "value": 2},
            {"path": ".methods[0].total_operational_savings", "value": 9987059},
            {"path": ".methods[1].method", "value": "fcfs"},
            {"path": ".methods[1].containers_used", "value": 3},
            {"path": ".methods[1].total_operational_savings", "value": 2154542},
        ],
        "expect": "운영비 기준 최적화 컨테이너 2대·절감 9,987,059원 vs 선착순 3대·2,154,542원 "
        "(정산표 기준 절감은 빈 컨테이너 비용을 반영하지 않아 비교에 쓰지 않는다)",
    },
    {
        "id": "business_matrix",
        "title": "BM 시나리오 매트릭스",
        "description": "성과보수율·기본 이용료·포워더 배분·고정비·항공 운임 조합 96개의 1년 차 손익",
        "method": "GET",
        "path": "/api/analysis/business/matrix?scenario_id=default",
        "checks": [
            {"path": ".rows", "op": "len", "value": 96},
            {"path": ".break_even_rows", "value": 48},
            {"path": ".highlights[0].summary", "op": "contains", "value": "흑자"},
        ],
        "expect": "96행 중 흑자 48행, highlights[0]: MVP 현금 고정비면 흑자",
    },
    {
        "id": "arrival_settlement",
        "title": "도착 검수 품질 사후정산",
        "description": "산청 로트 홍콩 도착 검수 손실률이 계획 가정(정산표 1.59% + 지연 하루 0.9%p "
        "= 2.49%)을 넘으면 "
        "넘은 손실액만큼 성과보수를 환급한다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/settlement/arrival",
        "body": {
            "lot_id": "lot-sancheong-sb-1",
            "inspected_loss_pct": 4.5,
            "note": "홍콩 도착 검수",
        },
        "checks": [
            {"path": ".planned_loss_rate", "value": 0.02493},
            {"path": ".refund_after", "op": "gt", "value": 0},
            {"path": ".capped", "value": True},
            {"path": ".refund_after", "value": 106380},
            {"path": ".uncovered_loss", "value": 343389},
        ],
        "expect": "계획 2.49% 대비 2.01%p 초과 손실 449,769원 중 산청 성과보수 전액 "
        "106,380원 환급(한도), 나머지 343,389원은 적하보험 영역",
    },
    {
        "id": "arrival_poor_jinju",
        "title": "도착 검수 학습 1: 진주 딸기 손실 9%",
        "description": "단전 240분을 겪은 CA-01의 진주 로트가 9% 손실로 도착했다. 검수 정산과 함께 "
        "품질 이력에 관측 1건(홍콩·해상·딸기, 긴 단전)이 자동으로 쌓인다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/settlement/arrival",
        "body": {
            "lot_id": "lot-jinju-sb-1",
            "inspected_loss_pct": 9.0,
            "note": "홍콩 도착 검수(긴 단전)",
        },
        "checks": [{"path": ".refund_after", "op": "gt", "value": 0}],
        "expect": "계획 대비 초과 손실만큼 진주 성과보수 환급",
    },
    {
        "id": "arrival_poor_miryang",
        "title": "도착 검수 학습 2: 밀양 딸기 손실 9.5%",
        "description": "같은 컨테이너의 밀양 로트도 9.5% 손실로 도착했다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/settlement/arrival",
        "body": {
            "lot_id": "lot-miryang-sb-1",
            "inspected_loss_pct": 9.5,
            "note": "홍콩 도착 검수(긴 단전)",
        },
        "checks": [{"path": ".refund_after", "op": "gt", "value": 0}],
        "expect": "계획 대비 초과 손실만큼 밀양 성과보수 환급",
    },
    {
        "id": "quality_loss_rates",
        "title": "학습 손실률표",
        "description": "시드 3건(JLST 가정)에 도착 검수 3건이 더해져 홍콩 해상 딸기 손실률이 사전 1.5%에서 "
        "올라간다. 단전 구간별 분포와 근거 문장을 함께 준다",
        "method": "GET",
        "path": "/api/quality/loss-rates",
        "checks": [
            {"path": ".cells[0].observations", "value": 6},
            {"path": ".cells[0].origins.arrival", "value": 3},
            {"path": ".cells[0].posterior_rate", "value": 0.030889},
            {"path": ".cells[0].outage_breakdown[1].key", "value": "long"},
            {"path": ".adjustments", "op": "len", "value": 1},
        ],
        "expect": "홍콩 해상 딸기 1.50% → 3.09%(+1.59%p), 긴 단전 17파렛트 보정 평균 5.71%",
    },
    {
        "id": "quality_impact",
        "title": "학습 손실률을 넣은 추천 비교",
        "description": "같은 기본 시나리오를 사전 손실률과 학습 손실률로 각각 풀어 화주별 절감액·"
        "성과보수 변화와 배정 변화를 보여준다. 새 계획에 쓰려면 POST /api/plans에 "
        "use_learned_loss: true",
        "method": "GET",
        "path": "/api/quality/impact",
        "checks": [
            {"path": ".total_savings_prior", "value": 10318577},
            {"path": ".total_savings_learned", "value": 8300833},
            {"path": ".shippers[0].savings_change", "value": -949527},
            {"path": ".shippers[3].savings_change", "value": 0},
            {"path": ".lot_shifts", "op": "len", "value": 0},
        ],
        "expect": "총 절감 1,031.9만 → 830.1만 원, 진주 −94.9만 원, 포도 화주 그대로, 배정 변화 없음",
    },
    {
        "id": "quality_observation_manual",
        "title": "다른 항차 검수 기록 추가",
        "description": "같은 노선 다른 항차(포워더 보고)에서 딸기 20파렛트가 긴 단전 356분 뒤 9% 손실로 "
        "도착했다는 기록을 수동으로 넣는다",
        "method": "POST",
        "path": "/api/quality/observations",
        "body": {
            "product_id": "strawberry",
            "destination": "HKHKG",
            "mode": "sea",
            "pallets": 20,
            "actual_loss_pct": 9.0,
            "outage_minutes": 356,
            "mix": "shared_same_product",
            "source": "포워더 보고(시연용 가정)",
        },
        "checks": [{"path": ".outage_bucket", "value": "long"}],
        "expect": "201, 긴 단전 관측 1건 추가",
    },
    {
        "id": "quality_impact_shift",
        "title": "학습 손실률이 추천을 바꾼다",
        "description": "홍콩 해상 딸기 학습 손실률이 더 오르면 해상 공동적재가 항공보다 비싸져 딸기 "
        "로트가 항공으로 바뀐다. 포도는 그대로 해상",
        "method": "GET",
        "path": "/api/quality/impact",
        "checks": [
            {"path": ".lot_shifts", "op": "len", "value": 3},
            {"path": ".lot_shifts[0].after", "value": "항공"},
            {"path": ".shippers[3].savings_change", "value": 0},
        ],
        "expect": "진주·산청·밀양 딸기 해상 → 항공, 포도 해상 유지",
    },
]

DEMO_STEPS: list[DemoStep] = [DemoStep(step=i, **s) for i, s in enumerate(_STEPS)]


def _sq(text: str) -> str:
    return text.replace("'", "'\\''")


def _shell_body(body: dict[str, Any], kinds: dict[str, str]) -> str:
    text = _sq(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
    for name, kind in kinds.items():
        if kind == "json":
            text = text.replace(f'"{{{name}}}"', f"'\"${{{name}}}\"'")
        else:
            text = text.replace(f"{{{name}}}", f"'\"${{{name}}}\"'")
    return f"'{text}'"


def _shell_path(path: str) -> str:
    path = path.removeprefix("/api")
    return '"' + re.sub(r"\{(\w+)\}", r"${\1}", path) + '"'


CURL_HEADER = """#!/usr/bin/env bash
set -euo pipefail
BASE=${BASE:-%s}
command -v jq >/dev/null || { echo "jq가 필요합니다" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl이 필요합니다" >&2; exit 1; }
RESP=""
MISMATCH=0

call() {
  local method=$1 path=$2 body=${3-} out code
  local -a auth=()
  if [ "$path" = "/demo/reset" ]; then
    : "${DEMO_RESET_TOKEN:?전체 초기화에는 관리자 토큰이 필요합니다}"
    auth=(-H "Authorization: Bearer $DEMO_RESET_TOKEN")
  fi
  if [ -n "$body" ]; then
    out=$(curl -sS -X "$method" "$BASE$path" "${auth[@]}" -H 'content-type: application/json' -d "$body" -w '\\n%%{http_code}')
  else
    out=$(curl -sS -X "$method" "$BASE$path" "${auth[@]}" -w '\\n%%{http_code}')
  fi
  code=${out##*$'\\n'}
  RESP=${out%%$'\\n'*}
  if [ "${code:0:1}" != "2" ]; then
    echo "  실패 HTTP $code: $RESP" >&2
    exit 1
  fi
  echo "  HTTP $code"
}

capture() {
  local value
  value=$(jq "$2" "$3" <<<"$RESP")
  if [ -z "$value" ] || [ "$value" = "null" ]; then
    echo "  값을 못 찾음: $3" >&2
    exit 1
  fi
  printf -v "$1" '%%s' "$value"
  echo "  $1 = $value"
}

check() {
  local path=$1 op=$2 want=$3 expr
  case $op in
    eq) expr="$path == \\$v" ;;
    contains) expr="($path) as \\$x | if (\\$x|type) == \\"array\\" then (\\$x|index(\\$v)) != null else (\\$x|tostring|contains(\\$v)) end" ;;
    len) expr="($path|length) == \\$v" ;;
    gt) expr="$path > \\$v" ;;
  esac
  if jq -e --argjson v "$want" "$expr" <<<"$RESP" >/dev/null; then
    echo "  ok $path $op $want"
  else
    echo "  불일치 $path $op $want (실제 $(jq -c "$path" <<<"$RESP"))"
    MISMATCH=$((MISMATCH + 1))
  fi
}
"""


def curl_script(base: str = DEFAULT_CURL_BASE) -> str:
    kinds: dict[str, str] = {}
    lines = [CURL_HEADER % base]
    for step in DEMO_STEPS:
        lines.append(f"echo '== [{step.step}] {_sq(step.title)}'")
        call = f"call {step.method} {_shell_path(step.path)}"
        if step.body is not None:
            call += " " + _shell_body(step.body, kinds)
        lines.append(call)
        for cap in step.capture:
            flag = "-r" if cap.kind == "string" else "-c"
            lines.append(f"capture {cap.name} {flag} '{cap.path}'")
            kinds[cap.name] = cap.kind
        for chk in step.checks:
            want = _sq(json.dumps(chk.value, ensure_ascii=False))
            lines.append(f"check '{chk.path}' {chk.op} '{want}'")
        lines.append("")
    lines.append('echo "== 끝: 불일치 $MISMATCH건"')
    lines.append('[ "$MISMATCH" -eq 0 ]')
    return "\n".join(lines) + "\n"


@router.post("/reset", dependencies=[Depends(require_demo_reset)])
def reset(
    include_lots: bool = Query(False),
    scope: Literal["demo", "sandbox"] = Query("demo"),
    world_id: str | None = None,
    all_worlds: bool = Query(False, alias="all"),
) -> ResetResponse | SandboxResetResponse:
    if scope == "sandbox":
        if not world_id and not all_worlds:
            raise HTTPException(status_code=422, detail="world_id 또는 all=true가 필요합니다")
        try:
            result = reset_sandbox(world_id=world_id, all_worlds=all_worlds)
        except WorldNotFound as exc:
            raise HTTPException(status_code=404, detail="세계를 찾을 수 없습니다") from exc
        return sandbox_reset_response(result)
    with demo_reset_lock():
        cleared_sessions = store.clear(DEMO_ORG)
        cleared_quality, _ = quality_history.reset()
        cleared_coldchain = coldchain_store.clear(DEMO_ORG)
        cleared_lots = lot_store.clear(DEMO_ORG) if include_lots else 0
    return ResetResponse(
        cleared_sessions=cleared_sessions,
        cleared_coldchain_containers=cleared_coldchain,
        cleared_quality_observations=cleared_quality,
        cleared_lots=cleared_lots,
    )


@router.get("/status")
def status() -> DemoStatus:
    settings = get_settings()
    return DemoStatus(
        offline_mode=settings.offline_mode,
        explain_use_llm=settings.explain_use_llm,
        llm_configured=bool(settings.llm_api_key and settings.llm_model_name),
        mof_key_configured=bool(settings.data_go_kr_service_key),
        snapshot_dates=SnapshotStore().dates(),
        demo_fixture=load_demo_fixture() is not None,
        scenario_ids=sorted(load_scenarios()),
        session_count=store.count(DEMO_ORG),
    )


@router.get("/script", response_model=list[DemoStep])
def script(format: Literal["json", "curl"] = Query("json")) -> Any:
    if format == "curl":
        return PlainTextResponse(curl_script(), media_type="text/x-shellscript; charset=utf-8")
    return DEMO_STEPS
