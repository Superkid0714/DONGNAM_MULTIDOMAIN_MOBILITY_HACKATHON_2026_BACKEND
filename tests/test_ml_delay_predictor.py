from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from freshswitch_backend.config import Settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST, Voyage
from freshswitch_backend.forecast.ml.adapter import (
    MLDelayPredictorAdapter,
    clear_delay_model_cache,
    select_predictor,
)
from freshswitch_backend.forecast.ml.ml_delay_predictor import (
    HISTORY_COLS,
    MLDelayPredictor,
    VoyageInput,
)
from freshswitch_backend.forecast.predictor import DelayPredictor, DelayQuery
from freshswitch_backend.forecast.service import ForecastOptions, forecast_voyages
from freshswitch_backend.planning.service import build_plan

MODEL_DIR = Path(__file__).parents[1] / "src" / "freshswitch_backend" / "forecast" / "ml"


def _voyage_input(**changes) -> VoyageInput:
    values = {
        "clsgn": "5LOE3",
        "etryptDt": pd.Timestamp("2025-11-01 00:05"),
        "tkoffPrrrnDt": pd.Timestamp("2025-11-01 14:00"),
        "laidupFcltyNm": "신항 7부두 1선석",
        "satmntEntrpsNm": "만해항운한국(주)",
        "vsslNltyNm": "라이베리아",
        "prvsDpmprtPrtNm": "SAN ANTONIO",
        "nxlnptPrtNm": "QINGDAO",
        "nxlnptNatPrtCd": "CNTAO",
        "grtg": 68687,
        "ldadngTon": 19888,
        "landngFrghtTon": 1286,
        "trnpdtTon": 417,
        "tugYn": "Y",
        "piltgYn": "Y",
        "forecast_wsd_ms": 6.0,
    }
    values.update(changes)
    return VoyageInput(**values)


def _empty_history() -> pd.DataFrame:
    return pd.DataFrame(columns=[*HISTORY_COLS, "laidupFcltyNm"])


def test_model_load_and_predicts_numeric_p50_p90():
    predictor = MLDelayPredictor(MODEL_DIR)
    result = predictor.predict(_voyage_input(), _empty_history(), mode="ops")

    assert isinstance(result.p50_h, float)
    assert isinstance(result.p90_h, float)
    assert result.p90_h >= result.p50_h
    assert isinstance(result.eta_departure_p50, pd.Timestamp)
    assert isinstance(result.eta_departure_p90, pd.Timestamp)


def test_unseen_categories_are_handled_by_model_feature_mapping():
    predictor = MLDelayPredictor(MODEL_DIR)
    result = predictor.predict(
        _voyage_input(
            laidupFcltyNm="가상의 외항터미널 1선석",
            satmntEntrpsNm="미등록 대리점",
            vsslNltyNm="미등록 국적",
            prvsDpmprtPrtNm="UNKNOWN ORIGIN",
            nxlnptPrtNm="UNKNOWN DESTINATION",
            nxlnptNatPrtCd="ZZXXX",
        ),
        _empty_history(),
        mode="ops",
    )

    assert result.p90_h >= result.p50_h


def test_missing_model_falls_back_to_stat_predictor(tmp_path):
    clear_delay_model_cache()
    stat = DelayPredictor([])

    selected = select_predictor(
        stat,
        Settings(feature_p1_delay_ml=True, delay_ml_model_dir=tmp_path / "missing-model"),
    )

    assert selected is stat


def test_feature_switch_off_uses_stat_predictor():
    clear_delay_model_cache()
    stat = DelayPredictor([])

    selected = select_predictor(
        stat,
        Settings(feature_p1_delay_ml=False, delay_ml_model_dir=MODEL_DIR),
    )

    assert selected is stat


def test_adapter_returns_model_forecast_and_output_fields():
    predictor = MLDelayPredictor(MODEL_DIR)
    adapter = MLDelayPredictorAdapter(DelayPredictor([]), predictor)
    voyage = Voyage(
        id="test-voyage",
        vessel_name="WAN HAI 612",
        terminal="부산신항 7부두",
        origin="KRPUS",
        destination="HKHKG",
        etd=datetime(2025, 11, 1, 14, 0, tzinfo=KST),
        cy_cutoff=datetime(2025, 11, 1, 10, 0, tzinfo=KST),
        transit_days=3,
        call_sign="5LOE3",
    )
    details = adapter.predict_voyage(
        voyage,
        DelayQuery(vessel_name=voyage.vessel_name, terminal=voyage.terminal),
        "normal",
        "normal",
        _empty_history(),
        None,
        6.0,
    )

    assert details.source == "model"
    assert details.mode == "plan"
    assert details.delay_p90_h >= details.delay_p50_h
    assert details.departure_p90 == voyage.etd + timedelta(hours=details.delay_p90_h)


def test_forecast_service_exposes_p50_p90_and_source_fields():
    scenario = get_scenario("default")
    plan = build_plan(scenario).plan
    forecasts = forecast_voyages(
        DelayPredictor([]), scenario, plan, ForecastOptions(quantile="p90")
    )

    assert forecasts
    forecast = forecasts[0]
    assert forecast.source == "stat"
    assert forecast.mode in ("ops", "plan")
    assert forecast.delay_p90_h >= forecast.delay_p50_h
    assert forecast.predicted_etd >= forecast.planned_etd
    assert forecast.departure_p90.isoformat()
    assert forecast.assumptions
