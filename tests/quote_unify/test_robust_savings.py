from decimal import Decimal

import pytest

from freshswitch_backend.planning.robust_savings import RobustRequest, robust_savings


def request(unknown=False, grade="reference"):
    def item(key, value, basis):
        return {
            "key": key,
            "supplier": "fixture",
            "basis": basis,
            "grade": grade,
            "minimum": value,
            "maximum": value,
            "source": "test only",
            "applicability_confirmed": grade == "confirmed",
        }

    ocean = [item("freight", 1000000, "per_container")]
    if unknown:
        ocean.append(
            {"key": "cfs", "supplier": "fixture", "basis": "per_shipper", "grade": "unknown"}
        )
    return RobustRequest(
        shipper_pallets=8,
        container_pallets=20,
        actual_weight_kg=3320,
        shipment_date="2026-11-20",
        inventory_complete=True,
        air=[item("air", 800000, "per_shipper")],
        ocean=ocean,
    )


def test_reference_bound():
    result = robust_savings(request())
    assert result.status == "savings_reference"
    assert result.savings_lower_bound == 400000
    assert result.shipper_net_savings_lower_bound == 320000
    assert result.solo_savings == 600000
    assert not result.settlement_eligible


def test_unknown_threshold():
    result = robust_savings(request(True))
    assert result.status == "conditional"
    assert result.break_even_threshold_container == 1000000
    assert result.ocean_upper is None
    assert "shipper_unknowns_present" in result.flags


def test_confirmed_and_assumption_fx():
    body = request(grade="confirmed")
    assert robust_savings(body).status == "savings_confirmed"
    body.ocean[0].currency = "HKD"
    body.ocean[0].minimum = body.ocean[0].maximum = Decimal(100)
    assert robust_savings(body).status == "not_computable"
    body = RobustRequest.model_validate(
        {
            **body.model_dump(),
            "fx": [{"currency": "HKD", "krw_per_unit": 180, "source": "assumption"}],
        }
    )
    assert robust_savings(body).status == "savings_reference"


@pytest.mark.parametrize("flag", ["plug_days_default", "fuel_min_fallback"])
def test_fallback_never_confirmed(flag):
    body = request(grade="confirmed")
    body.flags = [flag]
    assert robust_savings(body).status == "savings_reference"


def test_missing_inventory_is_unknown():
    body = request()
    body.inventory_complete = False
    assert robust_savings(body).status == "conditional"


def test_negative_margin():
    body = request()
    body.air[0].minimum = body.air[0].maximum = Decimal(100)
    result = robust_savings(body)
    assert result.status == "not_saving"
    assert result.air_switch_candidate


def test_max_supplier_and_range():
    body = request()
    item = body.ocean[0].model_copy(
        update={"minimum": Decimal(1100000), "maximum": Decimal(1200000), "grade": "range"}
    )
    body.ocean.append(item)
    assert robust_savings(body).ocean_known == 480000
