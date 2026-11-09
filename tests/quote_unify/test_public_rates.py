from datetime import date

from freshswitch_backend.planning.public_rates import select_fuel, table


def test_fuel_verified_period_and_fallback():
    row, fallback = select_fuel(date(2026, 7, 20), "medium")
    assert row["amount"] == 980 and not fallback
    row, fallback = select_fuel(date(2026, 11, 20), "medium")
    assert row["amount"] == 980 and fallback
    assert select_fuel(date(2026, 7, 20), "unclassified") == (None, False)


def test_unverified_seed_is_disabled():
    for row in table("public_rates"):
        if row["id"] != "pnc_storage":
            assert row["status"] == "reference"
            assert row["amount"] is not None
            assert row["verified_by"] == "안성준(수동 확인)"
            assert row["applicability"] == "unconfirmed"
        else:
            assert row["status"] == "unknown" and row["amount"] is None
    assert table("fx")[0]["confirmed"] is False
