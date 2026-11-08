from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Literal

from freshswitch_backend.config import Settings
from freshswitch_backend.domain.models import Voyage
from freshswitch_backend.forecast.predictor import (
    Congestion,
    DelayForecast,
    DelayPredictor,
    DelayQuery,
    Weather,
)
from freshswitch_backend.integrations.mof_vessel import KST, VesselMovement

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PredictionDetails:
    forecast: DelayForecast
    delay_p50_h: float
    delay_p90_h: float
    departure_p50: datetime
    departure_p90: datetime
    strong_wind_rule: bool
    mode: Literal["ops", "plan"]
    source: Literal["model", "stat"]
    assumptions: list[str]


@dataclass(frozen=True)
class _ModelLoad:
    model: Any | None


@lru_cache(maxsize=8)
def _load_model_once(model_dir: str) -> _ModelLoad:
    try:
        from freshswitch_backend.forecast.ml.ml_delay_predictor import MLDelayPredictor

        return _ModelLoad(MLDelayPredictor(model_dir))
    except Exception:
        logger.exception("지연 ML 모델 로드 실패. 통계 예측기를 사용합니다")
        return _ModelLoad(None)


def initialize_delay_model(settings: Settings) -> bool:
    """Warm the model cache during application startup when the feature is enabled."""
    if not settings.feature_p1_delay_ml:
        return False
    return _load_model_once(str(settings.delay_ml_model_dir)).model is not None


def clear_delay_model_cache() -> None:
    _load_model_once.cache_clear()


def select_predictor(
    stat: DelayPredictor, settings: Settings
) -> DelayPredictor | MLDelayPredictorAdapter:
    if not settings.feature_p1_delay_ml:
        return stat
    loaded = _load_model_once(str(settings.delay_ml_model_dir)).model
    if loaded is None:
        return stat
    return MLDelayPredictorAdapter(stat, loaded)


class MLDelayPredictorAdapter:
    """Wrap the trained model while retaining the statistical predictor as fallback."""

    def __init__(self, stat: DelayPredictor, model: Any) -> None:
        self.stat = stat
        self.model = model
        self.records = stat.records

    def predict(
        self,
        query: DelayQuery,
        weather: Weather = "normal",
        congestion: Congestion = "normal",
    ) -> DelayForecast:
        return self.stat.predict(query, weather, congestion)

    def predict_voyage(
        self,
        voyage: Voyage,
        query: DelayQuery,
        weather: Weather,
        congestion: Congestion,
        history: Any,
        movement: VesselMovement | None,
        forecast_wsd_ms: float | None,
    ) -> PredictionDetails:
        matched_arrival = movement.arrival_at if movement is not None else None
        mode: Literal["ops", "plan"] = "ops" if matched_arrival is not None else "plan"
        try:
            if not voyage.call_sign:
                raise ValueError("항차 호출부호가 없어 ML 입력을 구성할 수 없습니다")
            import pandas as pd

            from freshswitch_backend.forecast.ml.ml_delay_predictor import (
                GUST_FACTOR_FROM_WSD,
                VoyageInput,
            )

            berth = (
                (movement.departure_berth or movement.arrival_berth)
                if movement is not None
                else None
            )
            input_voyage = VoyageInput(
                clsgn=voyage.call_sign,
                tkoffPrrrnDt=pd.Timestamp(voyage.etd.astimezone(KST).replace(tzinfo=None)),
                etryptDt=(
                    pd.Timestamp(matched_arrival.astimezone(KST).replace(tzinfo=None))
                    if matched_arrival
                    else None
                ),
                laidupFcltyNm=berth or (voyage.terminal or None),
                satmntEntrpsNm=None,
                vsslNltyNm=movement.nationality if movement is not None else None,
                prvsDpmprtPrtNm=(movement.previous_port if movement else None),
                nxlnptPrtNm=(movement.next_port if movement else None),
                nxlnptNatPrtCd=voyage.destination or None,
                grtg=movement.gross_tonnage if movement is not None else None,
                forecast_wsd_ms=forecast_wsd_ms,
            )
            p50, p90, strong_wind_rule, assumptions = self._predict_nullable_features(
                input_voyage, history, mode, pd, GUST_FACTOR_FROM_WSD
            )
            p90 = max(p90, p50)
            assumptions.append("모델 입력에 없는 항차·입항 피처는 None으로 유지")
            assumptions.append(
                "울산 혼잡 배율 및 수동 기상 가산은 학습 모델 출력에 별도 합산하지 않음"
            )
            if forecast_wsd_ms is None:
                assumptions.append("출항 예정일 WSD 예보 없음: 강풍 규칙 미적용")
            legacy = self.stat.predict(query, weather, congestion).model_copy(
                update={
                    "p50_hours": round(p50, 2),
                    "p90_hours": round(p90, 2),
                    "base_p50_hours": round(p50, 2),
                    "base_p90_hours": round(p90, 2),
                    "sample_size": len(history),
                    "method": "LightGBM·CatBoost 출항 지연 모델",
                    "summary": f"{mode} ML 모델 기준 지연 p50 {p50:.1f}시간, p90 {p90:.1f}시간",
                }
            )
            sched = voyage.etd
            return PredictionDetails(
                forecast=legacy,
                delay_p50_h=p50,
                delay_p90_h=p90,
                departure_p50=sched + timedelta(hours=p50),
                departure_p90=sched + timedelta(hours=p90),
                strong_wind_rule=strong_wind_rule,
                mode=mode,
                source="model",
                assumptions=assumptions,
            )
        except Exception:
            logger.exception("항차 ML 예측 실패. 해당 항차는 통계 예측기로 대체합니다")
            return self._stat_details(voyage, query, weather, congestion, mode)

    def _predict_nullable_features(
        self, voyage_input, history, mode: str, pd, gust_factor: float
    ) -> tuple[float, float, bool, list[str]]:
        """Keep missing values missing while giving LightGBM numeric NaNs."""
        features = self.model.build_features(voyage_input, history, mode)
        for column in features.columns:
            if column not in self.model.cat_cols and features[column].dtype == object:
                features[column] = pd.to_numeric(features[column], errors="coerce")

        models = self.model.models[mode]
        lightgbm_p50 = float(models["lgb50"].predict(features)[0])
        catboost_features = features.copy()
        for column in self.model.cat_cols:
            if column in catboost_features:
                catboost_features[column] = catboost_features[column].astype(str)
        catboost_p50 = float(models["cb50"].predict(catboost_features)[0])
        p50 = (lightgbm_p50 + catboost_p50) / 2
        p90 = max(p50, float(models["lgb90"].predict(features)[0])) + self.model.q90_adj

        assumptions = [f"p90 보정 +{self.model.q90_adj:.2f}h"]
        gust = voyage_input.forecast_gust_ms
        if gust is None and voyage_input.forecast_wsd_ms is not None:
            gust = voyage_input.forecast_wsd_ms * gust_factor
            assumptions.append(f"순간풍속 = 단기예보 WSD x {gust_factor} (가정)")
        strong_wind = gust is not None and gust >= self.model.wind_threshold
        if strong_wind:
            p90 += self.model.wind_add
            assumptions.append(f"강풍 규칙 적용 (+{self.model.wind_add:.1f}h)")
        if mode == "plan":
            assumptions.append("계획용: 입항 신고 정보 없이 선사·항로·이력으로 추정")
        return p50, p90, strong_wind, assumptions

    def _stat_details(
        self,
        voyage: Voyage,
        query: DelayQuery,
        weather: Weather,
        congestion: Congestion,
        mode: Literal["ops", "plan"],
    ) -> PredictionDetails:
        forecast = self.stat.predict(query, weather, congestion)
        p50 = float(forecast.p50_hours)
        p90 = max(p50, float(forecast.p90_hours))
        assumptions = [item.name for item in forecast.assumptions]
        assumptions.append("ML 모델을 사용할 수 없어 기존 계층형 통계 예측기로 계산")
        return PredictionDetails(
            forecast=forecast,
            delay_p50_h=p50,
            delay_p90_h=p90,
            departure_p50=voyage.etd + timedelta(hours=p50),
            departure_p90=voyage.etd + timedelta(hours=p90),
            strong_wind_rule=False,
            mode=mode,
            source="stat",
            assumptions=assumptions,
        )
