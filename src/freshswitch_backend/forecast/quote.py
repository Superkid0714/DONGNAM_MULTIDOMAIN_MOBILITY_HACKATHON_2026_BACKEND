"""Quote-time p90 snapshots. No live API request or authentication bypass here."""

from functools import lru_cache

from freshswitch_backend.config import get_settings
from freshswitch_backend.domain.models import Scenario, Voyage
from freshswitch_backend.forecast.ml.adapter import MLDelayPredictorAdapter, select_predictor
from freshswitch_backend.forecast.ml.history import HISTORY_CSV, _csv_history
from freshswitch_backend.forecast.predictor import DelayQuery, default_predictor


@lru_cache(maxsize=2)
def _history(stamp: int):
    return _csv_history()


@lru_cache(maxsize=256)
def _predict(serialized: str, model_dir: str, enabled: bool, stamp: int) -> tuple[float, str]:
    voyage = Voyage.model_validate_json(serialized)
    settings = get_settings().model_copy(
        update={"delay_ml_model_dir": model_dir, "feature_p1_delay_ml": enabled}
    )
    stat = default_predictor()
    predictor = select_predictor(stat, settings) if voyage.call_sign else stat
    query = DelayQuery(
        vessel_name=voyage.vessel_name,
        call_sign=voyage.call_sign,
        terminal=voyage.terminal,
        planned_departure=voyage.etd.date(),
    )
    if isinstance(predictor, MLDelayPredictorAdapter):
        history = _history(stamp)
        if not history.empty:
            result = predictor.predict_voyage(
                voyage, query, "normal", "normal", history, None, None
            )
            return max(0.0, result.delay_p90_h), result.source
        predictor = predictor.stat
    return max(0.0, predictor.predict(query).p90_hours), "stat"


def prepare_quote_scenario(scenario: Scenario) -> Scenario:
    """Use existing p90 snapshots, otherwise ML with CSV history, then statistics.

    The same snapshot is used for shared and dedicated sea quotes. Real measured
    power outages are not available: p90 minus the explicitly entered truck shift
    is only an exposure proxy, not an observed outage.
    """
    settings = get_settings()
    stamp = HISTORY_CSV.stat().st_mtime_ns if HISTORY_CSV.is_file() else 0
    voyages = []
    for voyage in scenario.voyages:
        if voyage.delay_p90_h is not None:
            voyages.append(voyage)
            continue
        p90, source = _predict(
            voyage.model_dump_json(),
            str(settings.delay_ml_model_dir),
            settings.feature_p1_delay_ml,
            stamp,
        )
        voyages.append(
            voyage.model_copy(
                update={
                    "delay_p90_h": p90,
                    "delay_forecast_source": source,
                }
            )
        )
    return scenario.model_copy(update={"voyages": voyages})
