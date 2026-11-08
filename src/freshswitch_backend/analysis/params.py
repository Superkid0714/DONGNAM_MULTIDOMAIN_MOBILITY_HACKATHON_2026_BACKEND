from dataclasses import dataclass

from freshswitch_backend.domain.models import Quantity, Scenario

AIR_PALLET_FREIGHT_2026_04_BEFORE = 892_750
AIR_PALLET_FREIGHT_2026_04_AFTER = 1_552_600
AIR_PALLET_FREIGHT_2024_05 = 830_000
PALLET_KG = 415
ANCHOR_PRODUCT = "strawberry"


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    unit: str
    field: str
    per_product: bool
    low_factor: float
    high_factor: float
    source: str
    affects_plan: bool = True
    extra_fields: tuple[str, ...] = ()
    tariff_fields: tuple[str, ...] = ()


SPECS: tuple[ParamSpec, ...] = (
    ParamSpec(
        key="sea_base_loss_rate",
        label="해상 기본 손실률",
        unit="비율",
        field="sea_base_loss_rate",
        per_product=True,
        low_factor=0.01 / 0.015,
        high_factor=0.02 / 0.015,
        source="JLST 6권 2호(2025.6) 단전 없음 1~2%(모의) 범위 양 끝. 포도 등 다른 품목도 같은 배율. "
        "딸기 정산표 해상 품질손실 위험도 같은 배율",
        tariff_fields=("sea_loss_risk", "sea_loss_risk_short_window"),
    ),
    ParamSpec(
        key="loss_rate_per_wait_day",
        label="대기 하루당 손실 증가",
        unit="비율/일",
        field="loss_rate_per_wait_day",
        per_product=True,
        low_factor=((0.06 - 0.02) / 6) / 0.009,
        high_factor=((0.08 - 0.01) / 6) / 0.009,
        source="JLST 단전 있음 6~8%와 없음 1~2%의 최소 차이 4%p·최대 차이 7%p를 문전 6일로 나눔",
    ),
    ParamSpec(
        key="early_harvest_loss_rate_per_day",
        label="조기 수확 하루당 손실",
        unit="비율/일",
        field="early_harvest_loss_rate_per_day",
        per_product=True,
        low_factor=0.5,
        high_factor=1.5,
        source="가정 ±50%",
    ),
    ParamSpec(
        key="sea_ancillary_per_container_krw",
        label="해상 부대비(컨테이너당)",
        unit="원/컨테이너",
        field="sea_ancillary_per_container_krw",
        per_product=False,
        low_factor=0.7,
        high_factor=1.3,
        source="가정 ±30%. 기준 450만 원은 제안서 파렛트당 절감 24.9만 원에 맞춘 보정값. "
        "딸기 정산표 해상 내륙·장비·보관도 같은 배율",
        tariff_fields=("sea_inland",),
    ),
    ParamSpec(
        key="sea_freight_per_container_krw",
        label="해상 운임(컨테이너당)",
        unit="원/컨테이너",
        field="sea_freight_per_container_krw",
        per_product=False,
        low_factor=0.2 * AIR_PALLET_FREIGHT_2026_04_BEFORE * 20 / 4_560_000,
        high_factor=0.3 * AIR_PALLET_FREIGHT_2026_04_BEFORE * 20 / 4_560_000,
        source="원예원 CA 해상 물류비 = 항공의 20~30%(이데일리 2024.5, 콜드체인뉴스) × 항공 89.3만 원 × 20파렛트. "
        "딸기 정산표 해상 운임도 같은 배율",
        tariff_fields=("sea_freight",),
    ),
    ParamSpec(
        key="air_freight_per_kg_krw",
        label="항공 운임(kg당)",
        unit="원/kg",
        field="air_freight_per_kg_krw",
        per_product=False,
        low_factor=(AIR_PALLET_FREIGHT_2024_05 / PALLET_KG) / 2151,
        high_factor=(AIR_PALLET_FREIGHT_2026_04_AFTER / PALLET_KG) / 2151,
        source="하한: 원예원 2024.5 항공 t당 약 200만 원(2,000원/kg). "
        "상한: 2026.4.16 할증 후 파렛트 155.26만 원(한국농어민신문, 3,741원/kg). "
        "기본 운임과 유류할증료를 같은 배율로 바꾼다",
        extra_fields=("air_fuel_surcharge_per_kg_krw",),
    ),
    ParamSpec(
        key="reefer_truck_cost_per_km_krw",
        label="냉장트럭 km당 비용(집하)",
        unit="원/km",
        field="reefer_truck_cost_per_km_krw",
        per_product=False,
        low_factor=0.7,
        high_factor=1.3,
        source="가정 ±30%. 해상 집하와 항공 인천공항 운송에 같이 적용",
    ),
    ParamSpec(
        key="storage_per_pallet_day_krw",
        label="보관비(파렛트·일)",
        unit="원/파렛트·일",
        field="storage_per_pallet_day_krw",
        per_product=False,
        low_factor=0.5,
        high_factor=2.0,
        source="가정 0.5~2배(냉장창고 요율 미확인)",
    ),
    ParamSpec(
        key="platform_fee_rate",
        label="성과보수율",
        unit="비율",
        field="platform_fee_rate",
        per_product=False,
        low_factor=0.2 / 0.25,
        high_factor=0.4 / 0.25,
        source="기획서 8-1: 절감액의 20~40%(기본 25%)",
        affects_plan=False,
    ),
)

SPEC_BY_KEY = {spec.key: spec for spec in SPECS}


def _quantities(scenario: Scenario, spec: ParamSpec) -> list[Quantity]:
    params = scenario.cost_params
    if not spec.per_product:
        return [getattr(params, name) for name in (spec.field, *spec.extra_fields)]
    groups = [*params.product_costs.values(), params.default_product_cost]
    return [getattr(group, spec.field) for group in groups]


def anchor_value(scenario: Scenario, spec: ParamSpec) -> float:
    params = scenario.cost_params
    if not spec.per_product:
        return sum(getattr(params, name).value for name in (spec.field, *spec.extra_fields))
    product_ids = [p.id for p in scenario.products]
    anchor = ANCHOR_PRODUCT if ANCHOR_PRODUCT in product_ids else (product_ids or [""])[0]
    return getattr(params.for_product(anchor), spec.field).value


def scaled(scenario: Scenario, spec: ParamSpec, factor: float) -> Scenario:
    copy = scenario.model_copy(deep=True)
    for quantity in _quantities(copy, spec):
        quantity.value = quantity.value * factor
    for tariff in copy.cost_params.pallet_tariffs.values():
        for name in spec.tariff_fields:
            getattr(tariff, name).value *= factor
    return copy
