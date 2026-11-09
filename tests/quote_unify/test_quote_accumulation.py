from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from freshswitch_backend.config import Settings
from freshswitch_backend.planning.robust.quotes import (
    AccumulatedEvaluation,
    QuoteInput,
    evaluate_accumulated,
    recent_medians,
)


def row(amount=1000000, **changes):
    return QuoteInput.model_validate(
        {
            "item": "sea_freight",
            "origin": "JINJU",
            "destination": "HKG",
            "equipment": "40ft",
            "quoted_on": "2026-11-09",
            "minimum": amount,
            "maximum": amount,
            "basis": "per_container",
            "source": "fixture",
            **changes,
        }
    )


def evaluation():
    return AccumulatedEvaluation.model_validate(
        {
            "origin": "JINJU",
            "destination": "HKG",
            "equipment": "40ft",
            "evaluated_on": "2026-11-09",
            "evaluation": {
                "shipper_pallets": 8,
                "container_pallets": 17,
                "actual_weight_kg": 3320,
                "shipment_date": "2026-11-20",
                "estimate_equipment": "40ft",
            },
        }
    )


@pytest.mark.parametrize("count", [0, 2, 3])
def test_zero_two_three_estimate_source(count):
    rows = [row(value) for value in (1000000, 2000000, 9000000)[:count]]
    result = evaluate_accumulated(evaluation(), rows)
    filled = next(r for r in result.estimate["filled_items"] if r["key"] == "sea_freight")
    if count < 3:
        assert result.estimate["general_value_source"] == "시드 추정값"
        assert Decimal(filled["minimum"]) == 2822000
    else:
        assert filled["source"] == "최근 견적 3건 중앙값"
        assert Decimal(filled["minimum"]) == 2000000
    assert result.settlement_eligible is False
    assert result.estimate["fee"] is None


def test_demo_old_future_and_other_route_excluded():
    rows = [
        row(),
        row(2000000),
        row(9000000, is_demo=True),
        row(9000000, quoted_on="2026-08-11"),
        row(9000000, quoted_on="2026-11-10"),
        row(9000000, destination="SIN"),
    ]
    groups = recent_medians(rows, date(2026, 11, 9), "JINJU", "HKG", "40ft")
    assert groups[0]["count"] == 2
    assert (
        evaluate_accumulated(evaluation(), rows).estimate["general_value_source"] == "시드 추정값"
    )


def test_range_endpoints_median():
    rows = [row(100, maximum=200), row(300, maximum=500), row(700, maximum=900)]
    result = recent_medians(rows, date(2026, 11, 9), "JINJU", "HKG", "40ft")[0]
    assert result["minimum"] == 300 and result["maximum"] == 500


def test_custom_minimum_and_inclusive_window():
    settings = Settings(_env_file=None, robust_quote_min_count=2, robust_quote_lookback_days=7)
    rows = [
        row(1000000, quoted_on="2026-11-03"),
        row(3000000),
        row(9000000, quoted_on="2026-11-02"),
    ]
    groups = recent_medians(rows, date(2026, 11, 9), "JINJU", "HKG", "40ft", settings)
    assert groups[0]["count"] == 2
    assert groups[0]["estimate_source"] == "최근 견적 2건 중앙값"
    result = evaluate_accumulated(evaluation(), rows, settings)
    filled = next(r for r in result.estimate["filled_items"] if r["key"] == "sea_freight")
    assert Decimal(filled["minimum"]) == 2000000
    settings.robust_quote_min_count = 3
    assert (
        evaluate_accumulated(evaluation(), rows, settings).estimate["general_value_source"]
        == "시드 추정값"
    )


def test_settings_environment(monkeypatch):
    monkeypatch.setenv("ROBUST_QUOTE_MIN_COUNT", "5")
    monkeypatch.setenv("ROBUST_QUOTE_LOOKBACK_DAYS", "30")
    settings = Settings(_env_file=None)
    assert settings.robust_quote_min_count == 5
    assert settings.robust_quote_lookback_days == 30


@pytest.mark.parametrize("field", ["robust_quote_min_count", "robust_quote_lookback_days"])
def test_invalid_quote_settings(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})
