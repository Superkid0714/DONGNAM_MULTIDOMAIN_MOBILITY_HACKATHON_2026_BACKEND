import math
from collections.abc import Collection, Mapping
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from freshswitch_backend.domain.models import KST, Lot, Plan, Scenario, Voyage
from freshswitch_backend.forecast.ml.adapter import MLDelayPredictorAdapter, PredictionDetails
from freshswitch_backend.forecast.port_congestion import PortCongestionSummary
from freshswitch_backend.forecast.predictor import (
    CONGESTION_FACTOR,
    Congestion,
    DelayForecast,
    DelayPredictor,
    DelayQuery,
    Weather,
)
from freshswitch_backend.integrations.kma_forecast import (
    FORECAST_DATASET,
    HIGH_WAVE_M,
    STRONG_WIND_MS,
    WARNING_DATASET,
    WARNING_STANDARD_URL,
    WeatherAssessment,
    WeatherFeed,
    WeatherSource,
)
from freshswitch_backend.integrations.mof_vessel import VesselMovement
from freshswitch_backend.optimize.costs import CostModel, LotCost, haversine_km
from freshswitch_backend.optimize.feasibility import air_rejection
from freshswitch_backend.replan.engine import apply_event
from freshswitch_backend.replan.models import DelayEvent, ReplanError, VoyageDelay

Quantile = Literal["p50", "p90"]
Preset = Literal["typhoon"]
CongestionOption = Literal["auto", "normal", "elevated", "high"]
CongestionMode = Literal["auto", "manual", "preset"]
CongestionScope = Literal["manual", "ulsan_direct", "ulsan_proxy", "unavailable"]
WeatherOption = Literal["auto", "normal", "strong_wind", "typhoon"]
WeatherMode = Literal["auto", "manual", "preset"]
ULSAN_PORT_CODES = ("KRUSN", "KRONS")
PROXY_NOTE = (
    "부산항은 이 프로젝트에서 쓸 수 있는 공개 실시간 선박위치 API가 없어, "
    "울산항 묘박 대기 비율을 동남권 항만 혼잡의 대리 지표로 부산 항차에도 적용한다. "
    "울산 대기선은 유조선·케미컬선 비중이 커서 부산신항 컨테이너 선석 혼잡과 다를 수 있다"
)
PROXY_LABEL = "보정 신호(가정): 울산항 묘박 대기 비율을 부산 항차 혼잡의 대리 지표로 씀"
NOT_REPLANNED = "이번 재계획에 반영하지 않음"

GIMHAE_LAT = 35.1795
GIMHAE_LON = 128.9382
GIMHAE_STORAGE_DAYS = 0.5
GIMHAE_MAX_PALLETS = 6


class ForecastOptions(BaseModel):
    weather: WeatherOption = "normal"
    congestion: CongestionOption = "auto"
    quantile: Quantile = "p90"
    min_sell_days: int = Field(default=1, ge=0, le=14)
    preset: Preset | None = None

    def resolved(self) -> "ForecastOptions":
        if self.preset == "typhoon":
            return self.model_copy(
                update={"weather": "typhoon", "congestion": "high", "quantile": "p90"}
            )
        return self


class CongestionEffect(BaseModel):
    voyage_id: str
    scope: CongestionScope
    level: Congestion
    factor: float
    p50_added_hours: float
    p90_added_hours: float


class CongestionUse(BaseModel):
    mode: CongestionMode
    level: Congestion
    indicator: PortCongestionSummary | None = None
    unavailable_reason: str | None = None
    effects: list[CongestionEffect]
    note: str
    proxy_indicator: bool = False
    proxy_label: str | None = None


class WeatherUse(BaseModel):
    mode: WeatherMode
    level: Weather
    source: WeatherSource | None = None
    issued_at: datetime | None = None
    warning_checked_at: datetime | None = None
    grid: str | None = None
    covered: bool | None = None
    horizon_end: datetime | None = None
    max_wind_speed_ms: float | None = None
    max_wave_height_m: float | None = None
    active_warnings: list[str] = []
    reasons: list[str] = []
    fallback_reason: str | None = None
    note: str


class RemainingRisk(BaseModel):
    voyage_id: str
    vessel_name: str
    planned_etd: datetime
    predicted_etd: datetime
    delay_hours: float
    at_risk_lot_ids: list[str]
    at_risk_pallets: int
    reason: str = NOT_REPLANNED


class LotRisk(BaseModel):
    lot_id: str
    shipper_id: str
    product_id: str
    pallets: int
    container_id: str
    ready_date: date
    sell_by_date: date
    shelf_life_days: int
    planned_arrival: date
    predicted_arrival: date
    sell_days_left_planned: int
    sell_days_left_predicted: int
    at_risk: bool
    reason: str | None = None


class VoyageForecast(BaseModel):
    voyage_id: str
    vessel_name: str
    terminal: str
    planned_etd: datetime
    quantile: Quantile
    delay_hours: float
    predicted_etd: datetime
    congestion_scope: CongestionScope
    forecast: DelayForecast
    lots: list[LotRisk]
    delay_p50_h: float
    delay_p90_h: float
    departure_p50: datetime
    departure_p90: datetime
    strong_wind_rule: bool
    mode: Literal["ops", "plan"]
    source: Literal["model", "stat"]
    assumptions: list[str]


class AirportOption(BaseModel):
    airport: Literal["ICN", "PUS"]
    name: str
    trucking_km: float
    storage_days: float
    transit_days: float
    arrival_date: date
    sell_days_left: int
    feasible: bool
    reason: str | None = None
    freight: int
    collection: int
    storage: int
    loss_risk: int
    total: int
    source: str


class AirSwitchProposal(BaseModel):
    lot_id: str
    shipper_id: str
    pallets: int
    from_container_id: str
    reason: str
    sea_value_at_risk: int
    options: list[AirportOption]
    recommended_airport: Literal["ICN", "PUS"] | None
    replan_location: str | None = None


def _round_hours(hours: float) -> float:
    return float(math.ceil(hours)) if hours > 0 else 0.0


def _query(voyage: Voyage) -> DelayQuery:
    return DelayQuery(
        vessel_name=voyage.vessel_name,
        terminal=voyage.terminal,
        planned_departure=voyage.etd.astimezone(KST).date(),
    )


def _lot_risks(
    model: CostModel,
    voyage: Voyage,
    predicted: Voyage,
    plan: Plan,
    lots: dict[str, Lot],
    min_sell_days: int,
) -> list[LotRisk]:
    planned_arrival = model.sea_arrival_date(voyage)
    predicted_arrival = model.sea_arrival_date(predicted)
    risks = []
    for a in plan.assignments:
        if a.mode != "sea" or a.voyage_id != voyage.id or a.lot_id not in lots:
            continue
        lot = lots[a.lot_id]
        product = model.product(lot)
        effective_ready = lot.ready_date - timedelta(days=a.pull_forward_days)
        left = (lot.sell_by_date - predicted_arrival).days
        reason = None
        if predicted_arrival > lot.sell_by_date:
            reason = "예측 도착일이 판매기한 초과"
        elif (predicted_arrival - effective_ready).days > product.shelf_life_days:
            reason = "예측 도착일이 신선 유지 기간 초과"
        elif left < min_sell_days:
            reason = f"도착 후 판매 가능 기간 {left}일 < 기준 {min_sell_days}일"
        risks.append(
            LotRisk(
                lot_id=lot.id,
                shipper_id=lot.shipper_id,
                product_id=lot.product_id,
                pallets=lot.pallets,
                container_id=a.container_id or "",
                ready_date=lot.ready_date,
                sell_by_date=lot.sell_by_date,
                shelf_life_days=product.shelf_life_days,
                planned_arrival=planned_arrival,
                predicted_arrival=predicted_arrival,
                sell_days_left_planned=(lot.sell_by_date - planned_arrival).days,
                sell_days_left_predicted=left,
                at_risk=reason is not None,
                reason=reason,
            )
        )
    return risks


def calls_ulsan(voyage: Voyage) -> bool:
    return (
        voyage.origin in ULSAN_PORT_CODES or "울산" in voyage.terminal or "온산" in voyage.terminal
    )


def voyage_congestion(
    voyage: Voyage, options: ForecastOptions, port: PortCongestionSummary | None
) -> tuple[Congestion, CongestionScope]:
    if options.congestion != "auto":
        return options.congestion, "manual"
    if port is None:
        return "normal", "unavailable"
    return port.level, "ulsan_direct" if calls_ulsan(voyage) else "ulsan_proxy"


def congestion_use(
    options: ForecastOptions,
    voyages: list["VoyageForecast"],
    port: PortCongestionSummary | None,
    unavailable_reason: str | None = None,
) -> CongestionUse:
    options = options.resolved()
    mode: CongestionMode = (
        "preset" if options.preset else "auto" if options.congestion == "auto" else "manual"
    )
    effects = [
        CongestionEffect(
            voyage_id=v.voyage_id,
            scope=v.congestion_scope,
            level=v.forecast.congestion,
            factor=v.forecast.congestion_factor,
            p50_added_hours=v.forecast.congestion_added_p50_hours,
            p90_added_hours=v.forecast.congestion_added_p90_hours,
        )
        for v in voyages
    ]
    if mode != "auto":
        level: Congestion = options.congestion if options.congestion != "auto" else "normal"
        note = (
            f"혼잡도를 {'시연 프리셋' if mode == 'preset' else '입력값'}으로 고정"
            f"(배율 {CONGESTION_FACTOR[level]:g}, 가정). 울산항 선박위치는 조회하지 않음"
        )
        return CongestionUse(mode=mode, level=level, effects=effects, note=note)
    if port is None:
        return CongestionUse(
            mode=mode,
            level="normal",
            unavailable_reason=unavailable_reason,
            effects=effects,
            note="울산항 선박위치(실시간·스냅샷)를 못 읽어 평시(normal)로 계산",
        )
    proxy = "ulsan_proxy" in {e.scope for e in effects}
    note = port.basis
    if proxy:
        note += ". " + PROXY_NOTE
    return CongestionUse(
        mode=mode,
        level=port.level,
        indicator=port,
        effects=effects,
        note=note,
        proxy_indicator=proxy,
        proxy_label=PROXY_LABEL if proxy else None,
    )


def weather_mode(options: ForecastOptions) -> WeatherMode:
    if options.preset:
        return "preset"
    return "auto" if options.weather == "auto" else "manual"


def weather_use(
    options: ForecastOptions,
    feed: WeatherFeed | None = None,
    assessment: WeatherAssessment | None = None,
) -> WeatherUse:
    mode = weather_mode(options)
    resolved = options.resolved()
    if mode != "auto" or feed is None or assessment is None:
        level: Weather = resolved.weather if resolved.weather != "auto" else "normal"
        label = "시연 프리셋" if mode == "preset" else "입력값"
        return WeatherUse(
            mode=mode,
            level=level,
            note=f"기상을 {label}({level})으로 고정. 기상청 예보는 조회하지 않음",
        )
    basis = (
        f"{FORECAST_DATASET}(부산신항 격자 {feed.nx},{feed.ny}"
        + (f", {feed.issued_at.astimezone(KST):%Y-%m-%d %H:%M} 발표" if feed.issued_at else "")
        + f")·{WARNING_DATASET}(부산 {feed.stn_id})"
    )
    rule = (
        f"풍속 {STRONG_WIND_MS:g}m/s·파고 {HIGH_WAVE_M:g}m 이상 또는 풍랑·강풍 특보 → strong_wind, "
        f"태풍 특보 → typhoon (기상청 특보 기준 {WARNING_STANDARD_URL})"
    )
    if feed.source == "unavailable":
        note = "기상청 예보·스냅샷을 못 읽어 평시(normal)로 계산"
    elif assessment.horizon_end is None and not assessment.reasons:
        note = f"{basis} 기준. 단기예보 자료가 없어 평시(normal)로 계산"
    elif not assessment.covered and not assessment.reasons:
        note = f"{basis} 기준. 출항 예정이 단기예보 범위 밖이라 평시(normal)로 계산"
    else:
        found = "; ".join(assessment.reasons) or "기준 넘는 예보·특보 없음"
        source = "스냅샷" if feed.source == "snapshot" else "실시간"
        note = f"{basis} {source} 기준: {found} → {assessment.level}. {rule}"
    if assessment.warning_note:
        note += f". {assessment.warning_note}"
    return WeatherUse(
        mode=mode,
        level=assessment.level,
        source=feed.source,
        issued_at=feed.issued_at,
        warning_checked_at=feed.warning_checked_at,
        grid=f"{feed.nx},{feed.ny}",
        covered=assessment.covered,
        horizon_end=assessment.horizon_end,
        max_wind_speed_ms=assessment.max_wind_speed_ms,
        max_wave_height_m=assessment.max_wave_height_m,
        active_warnings=assessment.active_warnings,
        reasons=assessment.reasons,
        fallback_reason=feed.fallback_reason,
        note=note,
    )


def forecast_voyages(
    predictor: DelayPredictor,
    scenario: Scenario,
    plan: Plan,
    options: ForecastOptions,
    port: PortCongestionSummary | None = None,
    weather_by_voyage: Mapping[str, Weather] | None = None,
    *,
    ml_history=None,
    ml_movements: Mapping[str, VesselMovement] | None = None,
    forecast_wsd_by_voyage: Mapping[str, float] | None = None,
) -> list[VoyageForecast]:
    options = options.resolved()
    model = CostModel(scenario)
    lots = {lot.id: lot for lot in scenario.lots}
    used = {c.voyage_id for c in plan.containers if c.used}
    voyages = [v for v in scenario.voyages if v.id in used] or scenario.voyages
    result = []
    fixed: Weather = options.weather if options.weather != "auto" else "normal"
    for voyage in sorted(voyages, key=lambda v: v.etd):
        congestion, scope = voyage_congestion(voyage, options, port)
        weather = (weather_by_voyage or {}).get(voyage.id, fixed)
        query = _query(voyage)
        movement = (ml_movements or {}).get(voyage.id)
        mode: Literal["ops", "plan"] = (
            "ops" if movement is not None and movement.arrival_at is not None else "plan"
        )
        if isinstance(predictor, MLDelayPredictorAdapter):
            details = predictor.predict_voyage(
                voyage,
                query,
                weather,
                congestion,
                ml_history,
                movement,
                (forecast_wsd_by_voyage or {}).get(voyage.id),
            )
        else:
            forecast = predictor.predict(query, weather, congestion)
            p50 = float(forecast.p50_hours)
            p90 = max(p50, float(forecast.p90_hours))
            details = PredictionDetails(
                forecast=forecast,
                delay_p50_h=p50,
                delay_p90_h=p90,
                departure_p50=voyage.etd + timedelta(hours=p50),
                departure_p90=voyage.etd + timedelta(hours=p90),
                strong_wind_rule=False,
                mode=mode,
                source="stat",
                assumptions=[item.name for item in forecast.assumptions],
            )
        forecast = details.forecast
        raw = details.delay_p90_h if options.quantile == "p90" else details.delay_p50_h
        delay = _round_hours(raw)
        predicted = voyage.model_copy(update={"etd": voyage.etd + timedelta(hours=delay)})
        result.append(
            VoyageForecast(
                voyage_id=voyage.id,
                vessel_name=voyage.vessel_name,
                terminal=voyage.terminal,
                planned_etd=voyage.etd,
                quantile=options.quantile,
                delay_hours=delay,
                predicted_etd=predicted.etd,
                congestion_scope=scope,
                forecast=forecast,
                lots=_lot_risks(model, voyage, predicted, plan, lots, options.min_sell_days),
                delay_p50_h=details.delay_p50_h,
                delay_p90_h=details.delay_p90_h,
                departure_p50=details.departure_p50,
                departure_p90=details.departure_p90,
                strong_wind_rule=details.strong_wind_rule,
                mode=details.mode,
                source=details.source,
                assumptions=details.assumptions,
            )
        )
    return result


def _risky(forecasts: list[VoyageForecast]) -> list[VoyageForecast]:
    return [f for f in forecasts if any(lot.at_risk for lot in f.lots)]


def _severity(f: VoyageForecast) -> tuple[int, float]:
    return sum(lot.pallets for lot in f.lots if lot.at_risk), -f.planned_etd.timestamp()


def delay_event(forecasts: list[VoyageForecast], skip: Collection[str] = ()) -> DelayEvent | None:
    risky = [f for f in _risky(forecasts) if f.voyage_id not in skip]
    if not risky:
        return None
    worst = max(risky, key=_severity)
    others = sorted((f for f in risky if f is not worst), key=lambda f: f.planned_etd)
    return DelayEvent(
        voyage_id=worst.voyage_id,
        new_etd=worst.predicted_etd,
        delay_p90_h=max(0, worst.delay_p90_h),
        delay_forecast_source=worst.source,
        additional_voyages=[
            VoyageDelay(
                voyage_id=f.voyage_id,
                new_etd=f.predicted_etd,
                delay_p90_h=max(0, f.delay_p90_h),
                delay_forecast_source=f.source,
            )
            for f in others
        ],
        source="예측",
    )


def unreplannable(scenario: Scenario, forecasts: list[VoyageForecast]) -> dict[str, str]:
    reasons = {}
    for f in _risky(forecasts):
        try:
            apply_event(scenario, DelayEvent(voyage_id=f.voyage_id, new_etd=f.predicted_etd))
        except ReplanError as exc:
            reasons[f.voyage_id] = f"재계획에 넣을 수 없음: {exc}"
    return reasons


def remaining_risks(
    forecasts: list[VoyageForecast],
    event: DelayEvent | None,
    reasons: Mapping[str, str] | None = None,
) -> list[RemainingRisk]:
    handled = set(event.voyage_ids) if event is not None else set()
    return [
        RemainingRisk(
            voyage_id=f.voyage_id,
            vessel_name=f.vessel_name,
            planned_etd=f.planned_etd,
            predicted_etd=f.predicted_etd,
            delay_hours=f.delay_hours,
            at_risk_lot_ids=[lot.lot_id for lot in f.lots if lot.at_risk],
            at_risk_pallets=sum(lot.pallets for lot in f.lots if lot.at_risk),
            reason=(reasons or {}).get(f.voyage_id, NOT_REPLANNED),
        )
        for f in _risky(forecasts)
        if f.voyage_id not in handled
    ]


def _option(
    scenario: Scenario,
    lot: Lot,
    airport: Literal["ICN", "PUS"],
) -> AirportOption:
    params = scenario.cost_params
    if airport == "ICN":
        name = "인천공항"
        model = CostModel(scenario)
        source = "비용 파라미터: 산지→인천 트럭 km·인천 보관일·항공 운송일"
    else:
        name = "김해공항"
        base = CostModel(scenario)
        shipper = base.shippers.get(lot.shipper_id)
        packhouse_id = lot.packhouse_id or (shipper.packhouse_id if shipper else None)
        packhouse = base.packhouses.get(packhouse_id)
        km = (
            haversine_km(packhouse.lat, packhouse.lon, GIMHAE_LAT, GIMHAE_LON)
            * params.road_distance_factor.value
            if packhouse
            else params.air_trucking_km.value
        )
        changed = params.model_copy(
            update={
                "air_trucking_km": params.air_trucking_km.model_copy(
                    update={"value": round(km, 1), "source": "선과장→김해공항 직선거리×도로 배율"}
                ),
                "air_storage_days": params.air_storage_days.model_copy(
                    update={
                        "value": GIMHAE_STORAGE_DAYS,
                        "source": "가정: 냉장창고 없음, 당일 탑재",
                    }
                ),
            }
        )
        model = CostModel(scenario.model_copy(update={"cost_params": changed}))
        source = (
            f"가정: 김해 딸기 취급 항공사 1곳·냉장창고 없음(KNN 2023) → 당일 탑재, "
            f"하루 {GIMHAE_MAX_PALLETS}파렛트까지. 운임은 인천과 같은 kg당 단가"
        )
    cost: LotCost = model.air_lot_cost(lot)
    arrival = model.air_arrival_date(lot)
    reason = air_rejection(model, lot)
    if reason is None and airport == "PUS" and lot.pallets > GIMHAE_MAX_PALLETS:
        reason = f"김해 항공 여유 하루 {GIMHAE_MAX_PALLETS}파렛트(가정) 초과"
    return AirportOption(
        airport=airport,
        name=name,
        trucking_km=model.params.air_trucking_km.value,
        storage_days=model.params.air_storage_days.value,
        transit_days=model.params.air_transit_days.value,
        arrival_date=arrival,
        sell_days_left=(lot.sell_by_date - arrival).days,
        feasible=reason is None,
        reason=reason,
        freight=round(cost.freight),
        collection=round(cost.collection),
        storage=round(cost.storage),
        loss_risk=round(cost.loss_risk),
        total=round(cost.total),
        source=source,
    )


def air_switch_proposals(
    scenario: Scenario,
    forecasts: list[VoyageForecast],
    replan_locations: dict[str, str] | None = None,
) -> list[AirSwitchProposal]:
    lots = {lot.id: lot for lot in scenario.lots}
    model = CostModel(scenario)
    proposals = []
    gimhae_left = GIMHAE_MAX_PALLETS
    for f in forecasts:
        for risk in f.lots:
            if not risk.at_risk:
                continue
            lot = lots[risk.lot_id]
            options = [_option(scenario, lot, "PUS"), _option(scenario, lot, "ICN")]
            gimhae = options[0]
            if gimhae.feasible and lot.pallets > gimhae_left:
                gimhae.feasible = False
                gimhae.reason = f"김해 항공 여유 하루 {GIMHAE_MAX_PALLETS}파렛트(가정) 중 {gimhae_left}파렛트 남음"
            feasible = [o for o in options if o.feasible]
            best = min(feasible, key=lambda o: o.total, default=None)
            if best is not None and best.airport == "PUS":
                gimhae_left -= lot.pallets
            hard = risk.reason is not None and "초과" in risk.reason
            proposals.append(
                AirSwitchProposal(
                    lot_id=lot.id,
                    shipper_id=lot.shipper_id,
                    pallets=lot.pallets,
                    from_container_id=risk.container_id,
                    reason=f"{f.vessel_name} {f.quantile} 지연 {f.delay_hours:g}시간: {risk.reason}",
                    sea_value_at_risk=round(model.lot_value(lot)) if hard else 0,
                    options=options,
                    recommended_airport=best.airport if best else None,
                    replan_location=(replan_locations or {}).get(lot.id),
                )
            )
    return proposals
