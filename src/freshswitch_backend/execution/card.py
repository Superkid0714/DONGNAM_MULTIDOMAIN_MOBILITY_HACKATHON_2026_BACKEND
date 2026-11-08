import hashlib
import re
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator

from freshswitch_backend.domain.models import (
    DEFAULT_PALLET_KG,
    KST,
    Plan,
    Product,
    Scenario,
    Voyage,
)
from freshswitch_backend.execution.params import ExecutionParams

MOCK_OWNER_CODE = "FRSU"
MOCK_NOTE = (
    "모의값: 부킹·컨테이너·씰 번호는 실제 선사 발급 전이라 계획 id·컨테이너 id로 결정적으로 만든 값이다. "
    "컨테이너 번호는 ISO 6346 형식(소유자 코드 + U + 일련번호 6자리 + 검사 숫자)"
)
CONTAINER_NO_PATTERN = re.compile(r"[A-Z]{3}[UJZ]\d{7}")
BOOKING_NO_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9-]{3,29}")
SEAL_NO_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9-]{3,19}")
ISO6346_LETTERS = {
    letter: value
    for letter, value in zip(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        [v for v in range(10, 39) if v % 11 != 0],
        strict=True,
    )
}


class VgmItem(BaseModel):
    name: str
    kg: int
    assumed: bool
    source: str


class VgmWeight(BaseModel):
    pallets: int
    net_kg: int
    pallet_packaging_kg: int
    gross_cargo_kg: int
    container_tare_kg: int
    vgm_kg: int
    basis: str
    pallet_tare_kg: int = 0
    packaging_kg: int = 0
    items: list[VgmItem] = Field(default_factory=list)
    calculation: str = ""


class GensetInstruction(BaseModel):
    key: str
    equipment: str
    voyage_id: str
    container_ids: list[str]
    container_nos: list[str]
    truck_id: str | None
    trip: int | None
    lot_ids: list[str]
    window_start: datetime | None
    window_end: datetime | None
    unpowered_minutes: float
    threshold_minutes: float
    text: str
    plain_text: str


class Setpoint(BaseModel):
    product_ids: list[str]
    set_temp_c: float | None
    temp_min_c: float | None
    temp_max_c: float | None
    ventilation: str
    o2_pct: float | None
    co2_pct: float | None
    ca_controlled: bool
    assumed: bool
    assumptions: list[str]
    source: str


class EmptyPickup(BaseModel):
    location: str
    assumed: bool
    basis: str


class CutoffTimeline(BaseModel):
    si_cutoff: datetime
    vgm_cutoff: datetime
    cy_cutoff: datetime
    etd: datetime
    basis: str


class ContainerExecution(BaseModel):
    container_id: str
    voyage_id: str
    vessel_name: str
    terminal: str
    kind: str
    lot_ids: list[str]
    booking_no: str
    container_no: str
    seal_no: str
    mock: bool
    booking_no_mock: bool = True
    container_no_mock: bool = True
    seal_no_mock: bool = True
    entered_at: datetime | None = None
    weight: VgmWeight
    setpoint: Setpoint
    empty_pickup: EmptyPickup
    cutoffs: CutoffTimeline


class ExecutionCard(BaseModel):
    plan_id: str
    version: int
    mock: bool
    mock_note: str
    containers: list[ContainerExecution]
    genset_instructions: list[GensetInstruction] = Field(default_factory=list)


class EnteredNumbers(BaseModel):
    booking_no: str | None = None
    container_no: str | None = None
    seal_no: str | None = None
    updated_at: datetime | None = None


def _normalized(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", "", value).upper()
    return cleaned or None


class ExecutionNumbersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    booking_no: str | None = None
    container_no: str | None = None
    seal_no: str | None = None

    @field_validator("booking_no")
    @classmethod
    def _booking(cls, value: str | None) -> str | None:
        cleaned = _normalized(value)
        if cleaned is not None and not BOOKING_NO_PATTERN.fullmatch(cleaned):
            raise ValueError("부킹 번호는 영문 대문자·숫자·하이픈 4~30자여야 합니다")
        return cleaned

    @field_validator("seal_no")
    @classmethod
    def _seal(cls, value: str | None) -> str | None:
        cleaned = _normalized(value)
        if cleaned is not None and not SEAL_NO_PATTERN.fullmatch(cleaned):
            raise ValueError("씰 번호는 영문 대문자·숫자·하이픈 4~20자여야 합니다")
        return cleaned

    @field_validator("container_no")
    @classmethod
    def _container(cls, value: str | None) -> str | None:
        cleaned = _normalized(value)
        if cleaned is None:
            return None
        error = container_no_error(cleaned)
        if error:
            raise ValueError(error)
        return cleaned


def container_no_error(number: str) -> str | None:
    if not CONTAINER_NO_PATTERN.fullmatch(number):
        return (
            "컨테이너 번호는 ISO 6346 형식(소유자 코드 영문 3자 + 장비 구분 U/J/Z + "
            "일련번호 6자리 + 체크디지트 1자리, 예: CSQU3054383)이어야 합니다"
        )
    expected = iso6346_check_digit(number[:10])
    if int(number[10]) != expected:
        return (
            f"컨테이너 번호 체크디지트가 틀립니다: {number[:10]}의 체크디지트는 {expected}입니다"
            f"(올바른 번호 {number[:10]}{expected})"
        )
    return None


def iso6346_check_digit(code: str) -> int:
    total = sum(
        (ISO6346_LETTERS[ch] if ch.isalpha() else int(ch)) * 2**i for i, ch in enumerate(code)
    )
    return total % 11 % 10


def _digits(plan_id: str, container_id: str, salt: str, width: int) -> str:
    digest = hashlib.sha256(f"{salt}:{plan_id}:{container_id}".encode()).hexdigest()
    return str(int(digest, 16) % 10**width).zfill(width)


def mock_container_no(plan_id: str, container_id: str) -> str:
    code = MOCK_OWNER_CODE + _digits(plan_id, container_id, "container", 6)
    return f"{code}{iso6346_check_digit(code)}"


def mock_booking_no(plan_id: str, container_id: str) -> str:
    return "FSBK" + _digits(plan_id, container_id, "booking", 8)


def mock_seal_no(plan_id: str, container_id: str) -> str:
    return "FS" + _digits(plan_id, container_id, "seal", 7)


def fmt_kst(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%m/%d %H:%M")


def packaging_per_pallet(params: ExecutionParams) -> float:
    return params.pallet_tare_kg.value + params.packaging_kg_per_pallet.value


def gross_cargo_kg(net_kg: float, pallets: int, params: ExecutionParams) -> int:
    return round(net_kg + pallets * packaging_per_pallet(params))


def vgm_kg(gross_kg: int, params: ExecutionParams) -> int:
    return round(gross_kg + params.container_tare_kg.value)


def weight_basis(params: ExecutionParams) -> str:
    return (
        f"화물 총중량 = 화물 순중량(품목 파렛트당 순중량 × 파렛트) + 파렛트당 "
        f"(파렛트 {params.pallet_tare_kg.value:g}kg + 포장 {params.packaging_kg_per_pallet.value:g}kg). "
        f"VGM = 화물 총중량 + 컨테이너 자중 {params.container_tare_kg.value:,.0f}kg(별도 항목). "
        f"{params.pallet_tare_kg.source} / {params.packaging_kg_per_pallet.source} / "
        f"{params.container_tare_kg.source}"
    )


NET_SOURCE = "품목 데이터 파렛트당 순중량(pallet_kg) × 로트별 파렛트"
NET_ASSUMED_SOURCE = (
    "가정: 품목 데이터에 파렛트당 순중량이 없어 기본 {kg:g}kg/파렛트로 본다. 출하 실측 확인 필요"
)


def vgm_weight(
    net_kg: float, pallets: int, params: ExecutionParams, net_assumed: bool = True
) -> VgmWeight:
    net = round(net_kg)
    gross = gross_cargo_kg(net_kg, pallets, params)
    pallet_tare = round(pallets * params.pallet_tare_kg.value)
    packaging = gross - net - pallet_tare
    tare = round(params.container_tare_kg.value)
    total = vgm_kg(gross, params)
    net_source = NET_ASSUMED_SOURCE.format(kg=DEFAULT_PALLET_KG) if net_assumed else NET_SOURCE
    items = [
        VgmItem(name="화물 순중량", kg=net, assumed=net_assumed, source=net_source),
        VgmItem(
            name="파렛트",
            kg=pallet_tare,
            assumed=params.pallet_tare_kg.source.startswith("가정"),
            source=params.pallet_tare_kg.source,
        ),
        VgmItem(
            name="포장",
            kg=packaging,
            assumed=params.packaging_kg_per_pallet.source.startswith("가정"),
            source=params.packaging_kg_per_pallet.source,
        ),
        VgmItem(
            name="컨테이너 자중",
            kg=tare,
            assumed=params.container_tare_kg.source.startswith("가정"),
            source=params.container_tare_kg.source,
        ),
    ]
    marks = " + ".join(
        f"{item.name} {item.kg:,}kg" + ("(가정)" if item.assumed else "") for item in items
    )
    return VgmWeight(
        pallets=pallets,
        net_kg=net,
        pallet_packaging_kg=gross - net,
        gross_cargo_kg=gross,
        container_tare_kg=tare,
        vgm_kg=total,
        basis=weight_basis(params),
        pallet_tare_kg=pallet_tare,
        packaging_kg=packaging,
        items=items,
        calculation=(
            f"VGM {total:,}kg = {marks}. 파렛트 {pallets}개 × (파렛트 "
            f"{params.pallet_tare_kg.value:g}kg + 포장 {params.packaging_kg_per_pallet.value:g}kg)"
        ),
    )


def cutoff_timeline(voyage: Voyage, params: ExecutionParams) -> CutoffTimeline:
    si_lead = params.booking_amendment_lead_hours
    vgm_lead = params.vgm_cutoff_lead_hours
    return CutoffTimeline(
        si_cutoff=voyage.cy_cutoff - timedelta(hours=si_lead.value),
        vgm_cutoff=voyage.cy_cutoff - timedelta(hours=vgm_lead.value),
        cy_cutoff=voyage.cy_cutoff,
        etd=voyage.etd,
        basis=(
            f"항차 {voyage.id} ETD {fmt_kst(voyage.etd)}·CY 마감 {fmt_kst(voyage.cy_cutoff)} 기준. "
            f"SI 마감 = CY 마감 − {si_lead.value:g}시간({si_lead.source}). "
            f"VGM 마감 = CY 마감 − {vgm_lead.value:g}시간({vgm_lead.source})"
        ),
    )


def setpoint(products: list[Product], product_ids: list[str]) -> Setpoint:
    if not products:
        return Setpoint(
            product_ids=product_ids,
            set_temp_c=None,
            temp_min_c=None,
            temp_max_c=None,
            ventilation="가정: 선사 기본 설정(품목 조건 없음)",
            o2_pct=None,
            co2_pct=None,
            ca_controlled=False,
            assumed=True,
            assumptions=["가정: 시나리오에 품목 CA 조건이 없어 선사 기본 설정을 따른다"],
            source="가정",
        )
    low = max(p.temp_min_c for p in products)
    high = min(p.temp_max_c for p in products)
    assumptions = ["가정: 설정 온도 = 품목 허용 온도 범위 중앙값"]
    if len(products) > 1:
        assumptions.append("가정: 혼적 품목의 허용 온도 범위 교집합을 쓴다")
    if low > high:
        low, high = products[0].temp_min_c, products[0].temp_max_c
        assumptions.append(f"가정: 혼적 품목 온도 범위가 겹치지 않아 {products[0].name} 기준")
    gas = next((p for p in products if p.o2_pct is not None or p.co2_pct is not None), None)
    ca_controlled = gas is not None
    if ca_controlled:
        ventilation = "닫힘(CA 장치가 O₂·CO₂ 제어)"
        assumptions.append(
            "가정: CA 제어 중에는 외기 환기구를 닫는다(품목 데이터에 환기 항목 없음)"
        )
    else:
        ventilation = "최소(0~5 CBM/h)"
        assumptions.append(
            "가정: 기체 제어 없는 품목은 환기 최소(품목 데이터에 환기 항목 없음, SO2 처리 포도는 "
            "SO2 유지를 위해 최소)"
        )
    return Setpoint(
        product_ids=product_ids,
        set_temp_c=round((low + high) / 2, 1),
        temp_min_c=low,
        temp_max_c=high,
        ventilation=ventilation,
        o2_pct=gas.o2_pct if gas else None,
        co2_pct=gas.co2_pct if gas else None,
        ca_controlled=ca_controlled,
        assumed=True,
        assumptions=assumptions,
        source=" / ".join(f"{p.name}: {p.source}" for p in products),
    )


def empty_pickup(voyage: Voyage) -> EmptyPickup:
    return EmptyPickup(
        location=f"{voyage.terminal} 선사 지정 공컨테이너 장치장",
        assumed=True,
        basis="가정: 시나리오에 공컨테이너 반출지 데이터가 없어 선적 터미널의 선사 지정 장치장으로 둔다. "
        "부킹 확인서의 반출지(Empty pick-up) 확인 필요",
    )


def build_execution_card(
    plan_id: str,
    version: int,
    scenario: Scenario,
    plan: Plan,
    params: ExecutionParams | None = None,
    entered: dict[str, EnteredNumbers] | None = None,
) -> ExecutionCard:
    p = params or ExecutionParams()
    known = entered or {}
    lots = {lot.id: lot for lot in scenario.lots}
    products = {product.id: product for product in scenario.products}
    voyages = {voyage.id: voyage for voyage in scenario.voyages}
    kinds = {container.id: container.kind for container in scenario.containers}
    cards: list[ContainerExecution] = []
    for load in plan.containers:
        voyage = voyages.get(load.voyage_id)
        if not load.used or voyage is None:
            continue
        assignments = [
            a for a in plan.assignments if a.mode == "sea" and a.container_id == load.container_id
        ]
        net = 0.0
        net_assumed = False
        for a in assignments:
            lot = lots.get(a.lot_id)
            product = products.get(lot.product_id) if lot else None
            net += a.pallets * (product.net_pallet_kg if product else DEFAULT_PALLET_KG)
            if product is None or product.pallet_kg_assumed:
                net_assumed = True
        pallets = sum(a.pallets for a in assignments)
        found = [products[pid] for pid in load.product_ids if pid in products]
        actual = known.get(load.container_id, EnteredNumbers())
        booking = actual.booking_no or mock_booking_no(plan_id, load.container_id)
        container_no = actual.container_no or mock_container_no(plan_id, load.container_id)
        seal = actual.seal_no or mock_seal_no(plan_id, load.container_id)
        cards.append(
            ContainerExecution(
                container_id=load.container_id,
                voyage_id=voyage.id,
                vessel_name=voyage.vessel_name,
                terminal=voyage.terminal,
                kind=kinds.get(load.container_id, "40RH-CA"),
                lot_ids=sorted(a.lot_id for a in assignments),
                booking_no=booking,
                container_no=container_no,
                seal_no=seal,
                mock=None in (actual.booking_no, actual.container_no, actual.seal_no),
                booking_no_mock=actual.booking_no is None,
                container_no_mock=actual.container_no is None,
                seal_no_mock=actual.seal_no is None,
                entered_at=actual.updated_at,
                weight=vgm_weight(net, pallets, p, net_assumed),
                setpoint=setpoint(found, sorted(load.product_ids)),
                empty_pickup=empty_pickup(voyage),
                cutoffs=cutoff_timeline(voyage, p),
            )
        )
    return ExecutionCard(
        plan_id=plan_id,
        version=version,
        mock=any(card.mock for card in cards) or not cards,
        mock_note=MOCK_NOTE,
        containers=cards,
    )
