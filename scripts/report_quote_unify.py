"""Reproduce the requested 8P shipper / 17P shared-container comparison.

Run: uv run python scripts/report_quote_unify.py
Only reads local data and models; never writes a DB or calls a live provider.
"""

import json

from freshswitch_backend.analysis.fuel_shock import with_surcharge
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.forecast.quote import prepare_quote_scenario
from freshswitch_backend.planning.compare import compare_air_sea
from freshswitch_backend.planning.service import build_plan


def report() -> dict:
    base = get_scenario("default").model_copy(deep=True)
    base.lots = base.lots[:3]  # Jinju 8P + Sancheong 5P + Miryang 4P.
    base = prepare_quote_scenario(base)
    rows = []
    for exposure in ("forecast_p90", "no_outage_sensitivity"):
        scenario = base.model_copy(deep=True)
        if exposure == "no_outage_sensitivity":
            for voyage in scenario.voyages:
                voyage.delay_p90_h = 0
                voyage.delay_forecast_source = "provided"
        for surcharge in (470, 2060):
            priced = with_surcharge(scenario, surcharge)
            plan = build_plan(
                priced, fixed={lot.id: "CA-01" for lot in priced.lots}, require_savings=False
            ).plan
            quote = compare_air_sea(priced, plan, "lot-jinju-sb-1")
            rows.append(
                {
                    "exposure": exposure,
                    "surcharge_per_kg": surcharge,
                    "shipper_pallets": 8,
                    "container_pallets": 17,
                    "capacity_pallets": 20,
                    **{
                        key: getattr(quote, key)
                        for key in (
                            "air_cost",
                            "sea_solo_cost",
                            "shared_cost",
                            "fee",
                            "shipper_net_saving",
                            "savings_vs_air_pct",
                            "savings_vs_solo_pct",
                            "air_switch_candidate",
                        )
                    },
                    "shared_plus_fee": quote.shared_cost + quote.fee,
                    "delay_inputs": [value.model_dump() for value in quote.delay_inputs],
                }
            )
    return {
        "scope": "Conditional sea quote at 17/20P; optimizer may recommend air instead. Actual forecast and explicitly zero-outage sensitivity are separate.",
        "rows": rows,
    }


if __name__ == "__main__":
    print(json.dumps(report(), ensure_ascii=False, indent=2))
