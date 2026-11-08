from collections.abc import Iterable

from pydantic import BaseModel

from freshswitch_backend.domain.models import DEFAULT_PALLET_KG, Plan, Scenario
from freshswitch_backend.optimize.settlement import uses_usage_fee

FACTOR_SOURCE_URL = (
    "https://transportgeography.org/contents/chapter4/"
    "transportation-and-environment/co2-emissions-passenger-freight-transport-mode"
)
FACTOR_SOURCE = (
    "IEA 운송수단별 CO₂ 배출계수(g CO₂/톤·km): 항공 602, 도로 62, 해상 8. "
    "The Geography of Transport Systems 인용값이며 IEA 원문 표는 확인 필요"
)
AIR_G_PER_TKM = 602.0
ROAD_G_PER_TKM = 62.0
SEA_G_PER_TKM = 8.0
AIR_KM = {"HKHKG": 2_060.0}
SEA_KM = {"HKHKG": 2_130.0}
SEA_TRUCKING_KM = 100.0
DISTANCE_SOURCE = (
    "가정: 인천–홍콩 항공 대권거리 약 2,060km, 부산신항–홍콩 해상 약 2,130km(1,150해리), "
    "선과장→부산신항 육송 평균 100km. 항공 쪽 육송은 cost_params.air_trucking_km"
)


class EmissionLine(BaseModel):
    destination: str
    pallets: int
    tonnes_cargo: float
    air_kg_co2: float
    sea_kg_co2: float
    saved_t_co2: float


class EmissionResult(BaseModel):
    saved_t_co2: float
    air_avoided_pallets: int
    lines: list[EmissionLine]
    skipped_pallets: int
    skipped_reason: str | None
    air_g_per_tkm: float = AIR_G_PER_TKM
    road_g_per_tkm: float = ROAD_G_PER_TKM
    sea_g_per_tkm: float = SEA_G_PER_TKM
    factor_source: str = FACTOR_SOURCE
    factor_source_url: str = FACTOR_SOURCE_URL
    distance_source: str = DISTANCE_SOURCE


def air_avoided(scenario: Scenario, plan: Plan) -> list[tuple[str, str, int]]:
    lots = {lot.id: lot for lot in scenario.lots}
    params = scenario.cost_params
    return [
        (a.lot_id, lots[a.lot_id].destination if a.lot_id in lots else "", a.pallets)
        for a in plan.assignments
        if a.mode == "sea" and not uses_usage_fee(params, a.product_id)
    ]


def avoided_emissions(plans: Iterable[tuple[Scenario, Plan]]) -> EmissionResult:
    totals: dict[str, list[float]] = {}
    skipped = 0
    for scenario, plan in plans:
        products = {p.id: p for p in scenario.products}
        product_of = {a.lot_id: a.product_id for a in plan.assignments}
        air_truck_km = scenario.cost_params.air_trucking_km.value
        for lot_id, destination, pallets in air_avoided(scenario, plan):
            if destination not in AIR_KM or destination not in SEA_KM:
                skipped += pallets
                continue
            product = products.get(product_of[lot_id])
            tonnes = pallets * (product.net_pallet_kg if product else DEFAULT_PALLET_KG) / 1000
            air = tonnes * (AIR_KM[destination] * AIR_G_PER_TKM + air_truck_km * ROAD_G_PER_TKM)
            sea = tonnes * (SEA_KM[destination] * SEA_G_PER_TKM + SEA_TRUCKING_KM * ROAD_G_PER_TKM)
            row = totals.setdefault(destination, [0, 0.0, 0.0, 0.0])
            row[0] += pallets
            row[1] += tonnes
            row[2] += air / 1000
            row[3] += sea / 1000
    lines = [
        EmissionLine(
            destination=destination,
            pallets=int(pallets),
            tonnes_cargo=round(tonnes, 3),
            air_kg_co2=round(air, 1),
            sea_kg_co2=round(sea, 1),
            saved_t_co2=round((air - sea) / 1000, 3),
        )
        for destination, (pallets, tonnes, air, sea) in sorted(totals.items())
    ]
    return EmissionResult(
        saved_t_co2=round(sum(line.saved_t_co2 for line in lines), 3),
        air_avoided_pallets=sum(line.pallets for line in lines) + skipped,
        lines=lines,
        skipped_pallets=skipped,
        skipped_reason="거리 가정이 없는 목적지는 CO₂ 계산에서 뺐다" if skipped else None,
    )
