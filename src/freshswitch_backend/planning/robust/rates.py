"""Read-only versioned reference tables; no automatic applicability promotion."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from freshswitch_backend.planning.robust.calculator import BoundItem, FxRate, RobustRequest

RATE_ROOT = Path(__file__).resolve().parents[2] / "data" / "rates"


def table(name: str) -> list[dict]:
    if name not in {"public_rates", "fuel_surcharge", "fx"}:
        raise ValueError("Unknown rate table")
    return json.loads((RATE_ROOT / f"{name}.json").read_text(encoding="utf-8"))


def select_fuel(shipment: date, segment: str) -> tuple[dict | None, bool]:
    rows = [
        r
        for r in table("fuel_surcharge")
        if r["segment"] == segment
        and r["status"] in {"reference", "original_checked"}
        and r["amount"] is not None
    ]
    matching = [
        r
        for r in rows
        if date.fromisoformat(r["effective_from"])
        <= shipment
        <= date.fromisoformat(r["effective_to"])
    ]
    if matching:
        return min(matching, key=lambda r: Decimal(str(r["amount"]))), False
    return (min(rows, key=lambda r: Decimal(str(r["amount"]))), True) if rows else (None, False)


def seed_inputs(quote, segment: str | None = None) -> RobustRequest:
    """Unknown n/N and segment are not fabricated. Reference costs are not proof."""
    ocean = []
    for row in table("public_rates"):
        amount = row["amount"]
        if row["id"] == "pnc_storage":
            amount = None  # Actual dwell and progressive TEU tiers must be resolved.
        if row["id"] == "pnc_plug" and amount is not None:
            amount = Decimal(str(amount)) * 2
            if quote.shipment_date.month in row["seasonal_months"]:
                amount = (
                    amount * Decimal(row["seasonal_multiplier"])
                    if row.get("seasonal_status") == "reference"
                    else None
                )
        ocean.append(
            BoundItem(
                key=row["item"],
                supplier=row["supplier"],
                basis="per_shipper" if row["basis"] == "per_bl" else "per_container",
                grade="reference"
                if amount is not None and row["status"] in {"reference", "original_checked"}
                else "unknown",
                minimum=amount,
                maximum=amount,
                currency=row["currency"],
                source=row["source_url"],
                checked_at=row.get("verified_at"),
                exclude_reason=row.get("exclude_reason"),
            )
        )
    for key, basis in [
        ("sea_freight", "per_container"),
        ("ca_equipment", "per_container"),
        ("reefer_pickup", "per_shipper"),
        ("cfs_stuffing", "per_shipper"),
        ("cfs_storage", "per_shipper"),
        ("terminal_transfer", "per_container"),
        ("destination_additional", "per_shipper"),
    ]:
        ocean.append(BoundItem(key=key, supplier="unconfirmed", basis=basis, grade="unknown"))
    air = [
        BoundItem(key=key, supplier="unconfirmed", basis="per_shipper", grade="unknown")
        for key in ("air_freight", "air_inland", "air_terminal")
    ]
    flags = ["plug_days_default"]
    fuel, fallback = select_fuel(quote.shipment_date, segment) if segment else (None, False)
    if fuel:
        amount = Decimal(str(fuel["amount"])) * quote.cargo.gross_weight_kg
        air.append(
            BoundItem(
                key="fuel",
                supplier="KE",
                basis="per_shipper",
                grade="reference",
                minimum=amount,
                maximum=amount,
                source=fuel["source_url"],
                checked_at=fuel.get("verified_at"),
            )
        )
        if fallback:
            flags.append("fuel_min_fallback")
    else:
        air.append(BoundItem(key="fuel", supplier="KE", basis="per_shipper", grade="unknown"))
    return RobustRequest(
        shipper_pallets=quote.cargo.pallets,
        shipment_date=quote.shipment_date,
        actual_weight_kg=quote.cargo.gross_weight_kg,
        air=air,
        ocean=ocean,
        flags=flags,
        fx=[
            FxRate(
                currency=r["currency"],
                krw_per_unit=r["krw_per_unit"],
                source=r["source"],
                confirmed=False,
            )
            for r in table("fx")
        ],
        inventory_complete=False,
    )
