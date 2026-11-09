import math
from collections.abc import Callable, Iterable

from freshswitch_backend.domain.models import LossAdjustment, Scenario
from freshswitch_backend.quality.models import (
    BreakdownRow,
    LossCell,
    LossTable,
    Observation,
    OutageBucket,
)
from freshswitch_backend.quality.params import QualityParams

MODES = ("sea", "air")
METHOD = (
    "칸(품목·목적항·운송수단)마다 사후 손실률 = (사전 가중 w × 사전 손실률 + Σ 파렛트 × 보정 실측) "
    "÷ (w + Σ 파렛트). 보정 실측 = 실측 손실률 − 무전원 시간·조기출하 효과(같은 조건의 사전 모델 손실률 − 사전 "
    "기본 손실률). 범위는 사후값 ± z × 표준오차, 표준오차는 사전(상대 표준편차 가정)과 관측의 파렛트 가중 분산을 (건수 + 1)로 나눈 근사. 단전 구간·혼적 구분은 분포로만 보여주고 "
    "추천에는 칸 전체 평균을 쓴다"
)
OUTAGE_LABELS: dict[str, str] = {
    "none": "단전 없음",
    "short": "짧은 단전",
    "long": "긴 단전",
    "unknown": "센서 없음",
}
MIX_LABELS: dict[str, str] = {
    "solo": "단독 적재",
    "shared_same_product": "같은 품목 공동적재",
    "mixed_products": "다품목 혼적",
}


def outage_bucket(minutes: float | None, params: QualityParams) -> OutageBucket:
    if minutes is None:
        return "unknown"
    if minutes < params.short_outage_minutes.value:
        return "none"
    if minutes < params.long_outage_minutes.value:
        return "short"
    return "long"


def prior_rate(scenario: Scenario, product_id: str, mode: str) -> tuple[float, str]:
    pc = scenario.cost_params.for_product(product_id)
    quantity = pc.sea_base_loss_rate if mode == "sea" else pc.air_loss_rate
    return quantity.value, quantity.source


def prior_model_rate(
    scenario: Scenario,
    product_id: str,
    mode: str,
    wait_days: int,
    pull_forward_days: int = 0,
    outage_hours: float = 0,
) -> float:
    pc = scenario.cost_params.for_product(product_id)
    if mode == "air":
        return pc.air_loss_rate.value
    base, maximum = pc.sea_base_loss_rate.value, pc.max_sea_loss_rate.value
    rate = base + min(max(outage_hours, 0) / 5.5, 1) * (maximum - base)
    return min(maximum, max(0, rate + pc.early_harvest_loss_rate_per_day.value * pull_forward_days))


def condition_uplift(
    scenario: Scenario,
    product_id: str,
    mode: str,
    wait_days: int,
    pull_forward_days: int = 0,
    outage_hours: float = 0,
) -> float:
    base, _ = prior_rate(scenario, product_id, mode)
    return (
        prior_model_rate(scenario, product_id, mode, wait_days, pull_forward_days, outage_hours)
        - base
    )


def _weighted(observations: list[Observation], value: Callable[[Observation], float]) -> float:
    pallets = sum(o.pallets for o in observations)
    return sum(o.pallets * value(o) for o in observations) / pallets


def _breakdown(
    observations: list[Observation],
    key: Callable[[Observation], str],
    labels: dict[str, str],
) -> list[BreakdownRow]:
    total = sum(o.pallets for o in observations)
    rows = []
    for name, label in labels.items():
        group = [o for o in observations if key(o) == name]
        if not group:
            continue
        outages = [o for o in group if o.outage_minutes is not None]
        rows.append(
            BreakdownRow(
                key=name,
                label=label,
                observations=len(group),
                pallets=sum(o.pallets for o in group),
                pallet_share=round(sum(o.pallets for o in group) / total, 4),
                mean_outage_minutes=round(_weighted(outages, lambda o: o.outage_minutes or 0), 1)
                if outages
                else None,
                mean_actual_rate=round(_weighted(group, lambda o: o.actual_loss_rate), 6),
                mean_adjusted_rate=round(_weighted(group, lambda o: o.adjusted_loss_rate), 6),
            )
        )
    return rows


def _pct(rate: float) -> str:
    return f"{rate * 100:.2f}%"


def estimate_cell(
    scenario: Scenario,
    product_id: str,
    destination: str,
    mode: str,
    observations: list[Observation],
    params: QualityParams,
) -> LossCell:
    prior, source = prior_rate(scenario, product_id, mode)
    w = params.prior_weight_pallets.value
    pallets = sum(o.pallets for o in observations)
    weighted = sum(o.pallets * o.adjusted_loss_rate for o in observations)
    posterior = (w * prior + weighted) / (w + pallets)
    tau = params.prior_relative_sd.value * prior
    variance = (
        w * tau**2 + sum(o.pallets * (o.adjusted_loss_rate - posterior) ** 2 for o in observations)
    ) / (w + pallets)
    spread = params.range_z.value * math.sqrt(variance / (len(observations) + 1))
    names = {p.id: p.name for p in scenario.products}
    if observations:
        mean_adjusted = weighted / pallets
        basis = (
            f"사전 {_pct(prior)}(가중 {w:g}파렛트) + 관측 {len(observations)}건·{pallets}파렛트 "
            f"보정 평균 {_pct(mean_adjusted)} → {_pct(posterior)} "
            f"({(posterior - prior) * 100:+.2f}%p)"
        )
    else:
        mean_adjusted = None
        basis = f"관측 없음. 사전 {_pct(prior)} 그대로"
    origins: dict[str, int] = {}
    for o in observations:
        origins[o.origin] = origins.get(o.origin, 0) + 1
    return LossCell(
        product_id=product_id,
        product_name=names.get(product_id, product_id),
        destination=destination,
        mode=mode,
        prior_rate=prior,
        prior_source=source,
        prior_weight_pallets=w,
        observations=len(observations),
        pallets=pallets,
        origins=origins,
        mean_actual_rate=round(_weighted(observations, lambda o: o.actual_loss_rate), 6)
        if observations
        else None,
        mean_adjusted_rate=round(mean_adjusted, 6) if mean_adjusted is not None else None,
        posterior_rate=round(posterior, 6),
        delta=round(posterior - prior, 6),
        range_low=round(max(0.0, posterior - spread), 6),
        range_high=round(min(1.0, posterior + spread), 6),
        outage_breakdown=_breakdown(observations, lambda o: o.outage_bucket, OUTAGE_LABELS),
        mix_breakdown=_breakdown(observations, lambda o: o.mix, MIX_LABELS),
        basis=basis,
    )


def adjustments_from(cells: Iterable[LossCell]) -> list[LossAdjustment]:
    return [
        LossAdjustment(
            product_id=c.product_id,
            destination=c.destination,
            mode=c.mode,
            delta=c.delta,
            source=f"도착 검수 학습: {c.basis}",
        )
        for c in cells
        if c.observations and abs(c.delta) >= 1e-6
    ]


def loss_table(
    observations: list[Observation], scenario: Scenario, params: QualityParams, seed_label: str
) -> LossTable:
    products = [p.id for p in scenario.products]
    destinations = list(dict.fromkeys(lot.destination for lot in scenario.lots))
    for o in observations:
        if o.product_id in products and o.destination not in destinations:
            destinations.append(o.destination)
    cells = []
    for product_id in products:
        for destination in destinations:
            for mode in MODES:
                group = [
                    o
                    for o in observations
                    if (o.product_id, o.destination, o.mode) == (product_id, destination, mode)
                ]
                cells.append(estimate_cell(scenario, product_id, destination, mode, group, params))
    return LossTable(
        scenario_id=scenario.id,
        method=METHOD,
        seed_label=seed_label,
        prior_weight_pallets=params.prior_weight_pallets.value,
        range_z=params.range_z.value,
        outage_thresholds_minutes={
            "short": params.short_outage_minutes.value,
            "long": params.long_outage_minutes.value,
        },
        total_observations=len(observations),
        cells=cells,
        adjustments=adjustments_from(cells),
    )


def with_adjustments(scenario: Scenario, adjustments: list[LossAdjustment]) -> Scenario:
    params = scenario.cost_params.model_copy(update={"loss_adjustments": adjustments})
    return scenario.model_copy(update={"cost_params": params})
