from freshswitch_backend.planning.robust.calculator import RobustRequest, robust_savings
from freshswitch_backend.planning.robust.estimate import EvaluateRequest, estimate


def body():
    return EvaluateRequest(
        shipper_pallets=8,
        container_pallets=17,
        actual_weight_kg=3320,
        shipment_date="2026-07-20",
        inventory_complete=True,
        ocean=[
            {
                "key": "sea_freight",
                "supplier": "unknown",
                "basis": "per_container",
                "grade": "unknown",
            }
        ],
    )


def test_partial_never_claims_complete_savings():
    b = body()
    b.ocean.append(
        b.ocean[0].model_validate(
            {
                "key": "unclassified_charge",
                "supplier": "unknown",
                "basis": "per_shipper",
                "grade": "unknown",
            }
        )
    )
    result = estimate(b)
    assert result["status"] == "partial"
    assert result["savings_per_pallet"] is None
    assert result["fee"] is None and not result["settlement_eligible"]
    assert {i["key"] for i in result["filled_items"]} == {
        "air_freight",
        "origin_warehouse",
        "sea_freight",
        "reefer_pickup",
        "cfs_stuffing",
        "storage",
        "cfs_storage",
        "ca_equipment",
    }


def test_cfs_scales_but_truck_is_one_vehicle_assumption():
    b = body()
    b.shipper_pallets = 4
    rows = {i["key"]: i for i in estimate(b)["filled_items"]}
    assert rows["cfs_stuffing"]["minimum"] == "57000.0"
    assert rows["cfs_stuffing"]["maximum"] == "171500.0"
    assert rows["reefer_pickup"]["minimum"] == "200000"


def test_confirmed_overrides_estimate_and_cfs_range():
    b = body()
    b.ocean[0] = b.ocean[0].model_validate(
        {
            "key": "sea_freight",
            "supplier": "actual",
            "basis": "per_container",
            "grade": "confirmed",
            "minimum": 1000000,
            "maximum": 1000000,
            "source": "contract",
            "applicability_confirmed": True,
        }
    )
    b = EvaluateRequest.model_validate(
        {
            **b.model_dump(),
            "estimate_items": [
                {
                    "key": "cfs_stuffing",
                    "supplier": "scenario",
                    "basis": "per_shipper",
                    "minimum": 100000,
                    "maximum": 200000,
                    "source": "scenario",
                    "grade": "estimate",
                }
            ],
        }
    )
    result = estimate(b)
    assert result["status"] == "estimated"
    assert all(i["key"] != "sea_freight" for i in result["filled_items"])
    assert result["savings_per_pallet"]["low"] != result["savings_per_pallet"]["high"]
    robust = robust_savings(
        RobustRequest.model_validate(
            b.model_dump(exclude={"estimate_items", "estimate_equipment", "estimate_plug_days"})
        )
    )
    assert robust.settlement_eligible is False


def test_20ft_estimate_requires_matching_equipment():
    b = body()
    assert all(i["key"] != "terminal_transfer" for i in estimate(b)["filled_items"])
    b.estimate_equipment = "20ft"
    assert any(i["key"] == "terminal_transfer" for i in estimate(b)["filled_items"])


def test_scope_and_unknown_air_rules():
    b = body()
    b.estimate_equipment = "40ft"
    b.air = [
        b.ocean[0].model_validate(
            {
                "key": "air_terminal",
                "supplier": "unknown",
                "basis": "per_shipper",
                "grade": "unknown",
            }
        )
    ]
    b.ocean.append(
        b.ocean[0].model_validate(
            {
                "key": "destination_additional",
                "supplier": "unknown",
                "basis": "per_shipper",
                "grade": "unknown",
            }
        )
    )
    result = estimate(b)
    assert result["status"] == "estimated"
    assert "air_side_zero_conservative" in result["flags"]
    assert result["out_of_scope"][0]["key"] == "destination_additional"
    assert result["savings_per_pallet"] is not None


def test_storage_tiers_and_unconfirmed_later_days():
    b = body()
    b.estimate_plug_days = 5
    rows = {r["key"]: r for r in estimate(b)["filled_items"]}
    assert rows["storage"]["maximum"] == "27000"
    b.estimate_plug_days = 8
    b.ocean.append(
        b.ocean[0].model_validate(
            {"key": "storage", "supplier": "unknown", "basis": "per_container", "grade": "unknown"}
        )
    )
    assert estimate(b)["status"] == "partial"
