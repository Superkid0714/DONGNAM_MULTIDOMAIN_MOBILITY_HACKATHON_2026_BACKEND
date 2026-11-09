import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from freshswitch_backend.api.analysis import PlatformOrg
from freshswitch_backend.api.computation import (
    compute_replan,
    explanation_budget,
    request_deadline,
)
from freshswitch_backend.api.compute_limits import replan_rate
from freshswitch_backend.api.plans import replan_inputs
from freshswitch_backend.api.vessels import (
    UlsanClientFactory,
    VesselClientFactory,
    load_ulsan_congestion,
    ulsan_client_factory,
    vessel_client_factory,
)
from freshswitch_backend.config import get_settings
from freshswitch_backend.forecast.history import KST, DepartureRecord, merge
from freshswitch_backend.forecast.ml.adapter import MLDelayPredictorAdapter, select_predictor
from freshswitch_backend.forecast.ml.history import load_delay_model_data, match_voyage_movements
from freshswitch_backend.forecast.observations import OWN_SOURCE, own_records
from freshswitch_backend.forecast.port_congestion import PortCongestionSummary
from freshswitch_backend.forecast.predictor import DelayPredictor, default_predictor
from freshswitch_backend.forecast.service import (
    AirSwitchProposal,
    CongestionUse,
    ForecastOptions,
    RemainingRisk,
    VoyageForecast,
    WeatherUse,
    air_switch_proposals,
    congestion_use,
    delay_event,
    forecast_voyages,
    remaining_risks,
    unreplannable,
    weather_mode,
    weather_use,
)
from freshswitch_backend.integrations.kma_forecast import (
    LIVE_REQUEST_TIMEOUT_SECONDS as KMA_REQUEST_TIMEOUT_SECONDS,
)
from freshswitch_backend.integrations.kma_forecast import (
    KmaForecastClient,
    MissingKmaKey,
    Weather,
    WeatherFeed,
    WeatherProvider,
    assess_each,
    combine,
)
from freshswitch_backend.integrations.ulsan_position import PositionsUnavailable
from freshswitch_backend.planning.store import store
from freshswitch_backend.replan import DelayEvent, ReplanResult, locations, replan
from freshswitch_backend.replan.explanation import explain_replan, llm_enabled

router = APIRouter(prefix="/plans", tags=["forecast"])
logger = logging.getLogger(__name__)

KmaClientFactory = Callable[[], KmaForecastClient]


def _kma_factory() -> KmaForecastClient:
    key = get_settings().data_go_kr_service_key
    if not key:
        raise MissingKmaKey()
    return KmaForecastClient(key, timeout=KMA_REQUEST_TIMEOUT_SECONDS)


def kma_client_factory() -> KmaClientFactory:
    return _kma_factory


def _utc_now() -> datetime:
    return datetime.now(UTC)


clock: Callable[[], datetime] = _utc_now


class HistoryInfo(BaseModel):
    record_count: int
    start: date | None
    end: date | None
    source: str = "해수부 선박입출항 출항 신고(부산항): 출항예정일시 대비 출항일시"
    own_observation_count: int = 0
    own_observation_source: str | None = None


class DelayForecastReplan(ReplanResult):
    plan_id: str
    replan_id: str
    base_version: int


class DelayForecastResponse(BaseModel):
    plan_id: str
    options: ForecastOptions
    history: HistoryInfo
    port_congestion: CongestionUse
    weather: WeatherUse
    voyages: list[VoyageForecast]
    at_risk_lot_ids: list[str]
    triggered: bool
    event: DelayEvent | None
    remaining_risk_voyages: list[RemainingRisk] = []
    air_switch: list[AirSwitchProposal]
    air_lot_ids: list[str]
    replan: DelayForecastReplan | None
    summary: str


def _predictor(
    own: list[DepartureRecord],
) -> tuple[DelayPredictor | MLDelayPredictorAdapter, int]:
    base = default_predictor()
    if own:
        known = {r.key for r in base.records}
        added = sum(1 for r in own if r.key not in known)
        base = DelayPredictor(merge(base.records, own))
    else:
        added = 0
    return select_predictor(base, get_settings()), added


def _history(predictor: DelayPredictor, own: int) -> HistoryInfo:
    records = predictor.records
    days = [r.departure_at.astimezone(KST).date() for r in records]
    return HistoryInfo(
        record_count=len(records),
        start=min(days, default=None),
        end=max(days, default=None),
        own_observation_count=own,
        own_observation_source=OWN_SOURCE if own else None,
    )


def _summary(
    voyages: list[VoyageForecast],
    proposals: list[AirSwitchProposal],
    air: list[str],
    remaining: list[RemainingRisk],
    own: int,
    event: DelayEvent | None = None,
) -> str:
    parts = [
        f"{v.vessel_name}({v.voyage_id}) {v.quantile} 지연 {v.delay_hours:g}시간" for v in voyages
    ]
    head = ", ".join(parts)
    if own:
        head += f" (자체 관측 {own}건 반영)"
    if not proposals:
        return f"{head}. 판매기한·신선 유지 기간을 넘는 로트 없음"
    lots = ", ".join(p.lot_id for p in proposals)
    tail = ""
    if event is not None and event.additional_voyages:
        tail += f" → 위험 항차 {len(event.voyage_ids)}곳({', '.join(event.voyage_ids)}) 함께 재계획"
    if air:
        tail += f" → 재계획 추천안에서 항공 전환: {', '.join(air)}"
    if remaining:
        names = ", ".join(
            f"{r.vessel_name}({r.voyage_id}) {', '.join(r.at_risk_lot_ids)}({r.reason})"
            for r in remaining
        )
        tail += f". 이번 재계획에 반영하지 않은 위험 항차: {names} — 따로 재계획 필요"
    return f"{head}. 위험 로트 {lots}{tail}"


async def _weather(
    factory: KmaClientFactory,
    options: ForecastOptions,
    etds: dict[str, datetime],
    *,
    need_feed: bool = False,
) -> tuple[WeatherUse, dict[str, Weather] | None, WeatherFeed | None]:
    if weather_mode(options) != "auto" and not need_feed:
        return weather_use(options), None, None
    now = clock()
    provider = WeatherProvider(factory, offline_mode=get_settings().offline_mode)
    feed = await provider.get(now)
    if weather_mode(options) != "auto":
        return weather_use(options), None, feed
    each = assess_each(feed, etds, now)
    levels = {voyage_id: a.level for voyage_id, a in each.items()}
    return weather_use(options, feed, combine(each)), levels, feed


def _forecast_wsd(feed: WeatherFeed | None, etds: dict[str, datetime]) -> dict[str, float]:
    if feed is None or feed.source == "unavailable":
        return {}
    values: dict[str, float] = {}
    for voyage_id, etd in etds.items():
        local_day = etd.astimezone(KST).date()
        wind = [
            point.wind_speed_ms
            for point in feed.points
            if point.at.astimezone(KST).date() == local_day and point.wind_speed_ms is not None
        ]
        if wind:
            values[voyage_id] = max(wind)
    return values


@router.post("/{plan_id}/delay-forecast", dependencies=[Depends(replan_rate)])
async def delay_forecast(
    plan_id: str,
    factory: Annotated[UlsanClientFactory, Depends(ulsan_client_factory)],
    mof_factory: Annotated[VesselClientFactory, Depends(vessel_client_factory)],
    kma_factory: Annotated[KmaClientFactory, Depends(kma_client_factory)],
    org_id: PlatformOrg,
    body: ForecastOptions | None = None,
    llm: Annotated[bool | None, Query()] = None,
) -> DelayForecastResponse:
    deadline = request_deadline()
    session = await run_in_threadpool(store.get, plan_id, org_id)
    if session is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    options = (body or ForecastOptions()).resolved()
    port: PortCongestionSummary | None = None
    unavailable: str | None = None
    if options.congestion == "auto":
        try:
            port = (await load_ulsan_congestion(factory)).summary()
        except PositionsUnavailable as exc:
            unavailable = str(exc)
    used = {c.voyage_id for c in session.plan.containers if c.used}
    sailing = [v for v in session.scenario.voyages if v.id in used] or session.scenario.voyages
    settings = get_settings()
    own = await run_in_threadpool(own_records, org_id)
    predictor, own_count = _predictor(own)
    weather, levels, weather_feed = await _weather(
        kma_factory,
        options,
        {v.id: v.etd for v in sailing},
        need_feed=isinstance(predictor, MLDelayPredictorAdapter),
    )
    ml_data = None
    ml_movements = {}
    wsd_by_voyage: dict[str, float] = {}
    if isinstance(predictor, MLDelayPredictorAdapter):
        ml_data = await load_delay_model_data(
            mof_factory,
            offline_mode=settings.offline_mode,
            now=clock(),
        )
        if ml_data.source == "unavailable":
            logger.warning("ML 이력 CSV와 최근 MOF 데이터가 없어 통계 예측기로 대체합니다")
            predictor = predictor.stat
        else:
            ml_movements = match_voyage_movements(sailing, ml_data.movements)
            wsd_by_voyage = _forecast_wsd(weather_feed, {v.id: v.etd for v in sailing})
    effective = options.model_copy(update={"weather": weather.level})
    voyages = await run_in_threadpool(
        forecast_voyages,
        predictor,
        session.scenario,
        session.plan,
        effective,
        port,
        weather_by_voyage=levels,
        ml_history=ml_data.history if ml_data is not None else None,
        ml_movements=ml_movements,
        forecast_wsd_by_voyage=wsd_by_voyage,
    )
    skipped = unreplannable(session.scenario, voyages)
    event = delay_event(voyages, skip=skipped)
    remaining = remaining_risks(voyages, event, skipped)
    replan_response = None
    where: dict[str, str] = {}
    air: list[str] = []
    if event is not None:
        inputs = await run_in_threadpool(replan_inputs, session, org_id)
        result = await compute_replan(
            replan,
            session.scenario,
            session.plan,
            event,
            air_locked=session.air_locked_lot_ids,
            routes=session.routes,
            now=store.clock(),
            deadline=deadline,
            **inputs,
        )
        from freshswitch_backend.planning.quantity_policy import annotate_for_policy

        annotate_for_policy(session, result)
        result.explanation = await explain_replan(
            session.scenario,
            session.plan,
            result,
            use_llm=llm_enabled(llm),
            timeout_s=explanation_budget(deadline),
        )
        pending = await run_in_threadpool(store.add_pending, session, result, org_id)
        replan_response = DelayForecastReplan(
            **{name: getattr(result, name) for name in ReplanResult.model_fields},
            plan_id=plan_id,
            replan_id=pending.replan_id,
            base_version=pending.base_version,
        )
        chosen = next((a for a in result.alternatives if a.id == result.recommended_id), None)
        if chosen is not None:
            before = locations(session.plan)
            where = locations(chosen.plan)
            air = sorted(
                lid for lid, loc in where.items() if loc == "AIR" and before.get(lid) != "AIR"
            )
    proposals = air_switch_proposals(session.scenario, voyages, where)
    from freshswitch_backend.planning.quantity_policy import record_air_recommendations

    await run_in_threadpool(
        record_air_recommendations,
        plan_id,
        session.version,
        sorted(set(air) | {p.lot_id for p in proposals if p.recommended_airport is not None}),
        store.clock(),
    )
    return DelayForecastResponse(
        plan_id=plan_id,
        options=options,
        history=_history(predictor, own_count),
        port_congestion=congestion_use(options, voyages, port, unavailable),
        weather=weather,
        voyages=voyages,
        at_risk_lot_ids=[p.lot_id for p in proposals],
        triggered=event is not None,
        event=event,
        remaining_risk_voyages=remaining,
        air_switch=proposals,
        air_lot_ids=air,
        replan=replan_response,
        summary=_summary(voyages, proposals, air, remaining, own_count, event),
    )
