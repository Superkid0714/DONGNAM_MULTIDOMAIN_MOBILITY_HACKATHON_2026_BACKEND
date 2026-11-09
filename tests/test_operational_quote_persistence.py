from datetime import date

from freshswitch_backend.persistence.operational_quotes import read_quote, save_quote
from freshswitch_backend.planning.operational_quote import Cargo, Dimension, OperationalQuote


def test_quote_persists_with_org_isolation():
    quote = OperationalQuote(
        provider="fixture",
        provider_type="forwarder",
        mode="ca_sea",
        origin="BUS",
        destination="HKG",
        shipment_date=date(2026, 11, 20),
        issued_on=date(2026, 11, 9),
        valid_from=date(2026, 11, 9),
        valid_until=date(2026, 11, 30),
        includes={"fuel": None, "inland": None, "customs": None, "destination": None},
        cargo=Cargo(
            product="딸기",
            packaging="상자",
            pallets=1,
            gross_weight_kg=415,
            dimensions=[Dimension(length_cm=100, width_cm=100, height_cm=100, pieces=1)],
        ),
    )
    record = save_quote("demo", "fixture-user", quote)
    assert read_quote("demo", record["id"])["quote"] == quote.model_dump(mode="json")
    assert read_quote("other-org", record["id"]) is None
