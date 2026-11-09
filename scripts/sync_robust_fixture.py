"""Generate backend test snapshots from current seeds; no FE or DB dependency."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from freshswitch_backend.planning.public_rates import seed_inputs
from freshswitch_backend.planning.robust_savings import robust_savings

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[1] / "tests/quote_unify/fixtures/robust_savings.json"
)


def snapshots():
    from datetime import date
    from decimal import Decimal

    quote = SimpleNamespace(
        shipment_date=date(2026, 7, 20),
        cargo=SimpleNamespace(pallets=8, gross_weight_kg=Decimal(3320)),
    )
    result = {}
    for total in (17, 15):
        body = seed_inputs(quote, "medium")
        body.container_pallets = total
        row = robust_savings(body).model_dump(mode="json")
        row.update(shipper_pallets=8, container_pallets=total)
        result[str(total)] = {"CA-01:jinju": row}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path, nargs="?", default=DEFAULT_FIXTURE)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = snapshots()
    if args.check:
        assert json.loads(args.fixture.read_text(encoding="utf-8")) == expected, (
            "Seed/fixture drift"
        )
        print("Seed/fixture equality OK")
    else:
        args.fixture.parent.mkdir(parents=True, exist_ok=True)
        args.fixture.write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("Fixture generated from current seeds")
