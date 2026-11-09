"""Separate non-billable scenario estimates; robust algorithm is untouched."""

import json
from decimal import Decimal
from typing import Literal

from pydantic import Field

from freshswitch_backend.planning.robust.calculator import BoundItem, RobustRequest, RobustResult
from freshswitch_backend.planning.robust.rates import RATE_ROOT, table


class EstimateItem(BoundItem):
    grade: Literal["estimate"] = "estimate"


class EvaluateRequest(RobustRequest):
    estimate_plug_days: int = Field(default=2, ge=0, le=365)
    estimate_items: list[EstimateItem] = Field(default_factory=list, max_length=100)
    estimate_equipment: Literal["20ft", "40ft", "unknown"] = "unknown"


class EstimateResult(RobustResult):
    robust: RobustResult
    estimate: dict


def estimate(body: EvaluateRequest) -> dict:
    result = {
        "status": "not_computable",
        "settlement_eligible": False,
        "fee": None,
        "comparison_scope": "진주 출하 → 홍콩 공항/항만 도착(인도 전)",
        "flags": ["same_day_stuffing_assumed", "ca_included_in_sea_freight_estimate"],
        "out_of_scope": [],
        "zeroed_items": [],
        "filled_items": [],
        "unknown_items": [],
        "savings_per_pallet": None,
        "low": None,
        "high": None,
        "warnings": ["estimate는 과거 기사·가정 기반이며 청구·보장 근거가 아닙니다"],
    }
    if not all(
        (body.shipper_pallets, body.container_pallets, body.actual_weight_kg, body.shipment_date)
    ):
        return result
    seeds = json.loads((RATE_ROOT / "estimates.json").read_text(encoding="utf-8"))
    rank = {"confirmed": 4, "reference": 3, "range": 2, "estimate": 1, "unknown": 0}
    fx = {r.currency: r.krw_per_unit for r in body.fx}
    ratio = Decimal(body.shipper_pallets) / Decimal(body.container_pallets)
    totals = {}
    for side in ("air", "ocean"):
        candidates = {}
        original = getattr(body, side)
        aliases = {
            "air_base": "air_freight",
            "inland_container_20ft": "terminal_transfer",
            "reefer_truck_jinju_busan_cfs": "reefer_pickup",
            "cfs_to_terminal_shuttle": "terminal_transfer",
            "terminal_storage": "storage",
        }
        for row in seeds:
            if row["side"] != side:
                continue
            key = aliases.get(row["id"], row["id"])
            if row.get("equipment") and body.estimate_equipment != row["equipment"]:
                continue
            if row["minimum"] is not None:
                factor = body.actual_weight_kg if row["basis"] == "per_kg" else Decimal(1)
                if row.get("reference_pallets"):
                    factor *= Decimal(body.shipper_pallets) / Decimal(row["reference_pallets"])
                candidates[key] = [
                    {
                        "key": key,
                        "grade": "estimate",
                        "minimum": Decimal(row["minimum"]) * factor,
                        "maximum": Decimal(row["maximum"]) * factor,
                        "currency": row["currency"],
                        "basis": "per_shipper" if side == "air" else row["basis"],
                        "source": row["source"],
                        "note": row.get("note", ""),
                    }
                ]
        if side == "ocean":
            storage = next(r for r in table("public_rates") if r["id"] == "pnc_storage")
            days = body.estimate_plug_days
            # User-supplied estimate conditions, not confirmed tariff applicability.
            if days <= 3:
                storage_amount = Decimal(0)
            elif days <= 7:
                storage_amount = Decimal(6750) * 2 * (days - 3)
            else:
                storage_amount = None
            for key, amount, note in (
                (
                    "storage",
                    storage_amount,
                    "사용자 제공 PNC 수출리퍼 1~3일 무료, 4~7일6750/TEU/일×40ft2TEU; 8일 이후 미확인",
                ),
                ("cfs_storage", Decimal(0), "당일 적입 가정"),
                ("ca_equipment", Decimal(0), "sea_freight estimate 포함 가정"),
            ):
                if amount is not None:
                    candidates[key] = [
                        {
                            "key": key,
                            "grade": "estimate",
                            "minimum": amount,
                            "maximum": amount,
                            "currency": "KRW",
                            "basis": "per_container",
                            "source": storage["source_url"]
                            if key == "storage"
                            else "사용자 시나리오 가정",
                            "note": note,
                        }
                    ]
            result["flags"].append("terminal_storage_estimated_from_plug_days")
        for item in [*original, *body.estimate_items]:
            # Estimate overrides are ocean-only; air amounts use the explicit air list.
            if item in body.estimate_items and side != "ocean":
                continue
            if not item.exclude_reason and item.key != "pnc_basic_handling":
                if item.supplier in {"organization_quote_median", "engine_wait_days"} and side == "ocean":
                    # Historical general values replace seed estimates, not actual evidence.
                    candidate_key = aliases.get(item.key, item.key)
                    candidates[candidate_key] = [
                        r for r in candidates.get(candidate_key, []) if r["grade"] != "estimate"
                    ]
                if item.key in {
                    "destination_additional",
                    "destination_delivery",
                    "destination_customs",
                    "hong_kong_delivery",
                    "hong_kong_customs",
                }:
                    result["out_of_scope"].append(
                        {
                            "side": side,
                            "key": item.key,
                            "reason": "홍콩 인도·현지배송·통관은 비교 범위 밖",
                        }
                    )
                    continue
                candidates.setdefault(aliases.get(item.key, item.key), []).append(item.model_dump())
        low = high = Decimal(0)
        uses_article_air = False
        selected = []
        for key, rows in candidates.items():
            level = max(rank[r["grade"]] for r in rows)
            rows = [r for r in rows if rank[r["grade"]] == level]
            if level == 0:
                if side == "air":
                    result["zeroed_items"].append({"side": side, "key": key, "amount": "0"})
                    if "air_side_zero_conservative" not in result["flags"]:
                        result["flags"].append("air_side_zero_conservative")
                    continue
                result["unknown_items"].append({"side": side, "key": key})
                continue
            selected.append((key, rows))
            if side == "air" and key == "air_freight" and level == 1:
                uses_article_air = True
        for key, rows in selected:
            if uses_article_air and key == "fuel":
                result["warnings"].append(
                    "기사 항공 총운송비와 포함 범위 불명 fuel의 중복 가산을 피하려 fuel 제외"
                )
                continue
            values = []
            for row in rows:
                rate = Decimal(1) if row["currency"] == "KRW" else fx.get(row["currency"])
                if rate is None:
                    result["warnings"].append("외화 환율 없음")
                    return result
                share = ratio if side == "ocean" and row["basis"] == "per_container" else Decimal(1)
                values.append(
                    (Decimal(row["minimum"]) * rate * share, Decimal(row["maximum"]) * rate * share)
                )
                if row["grade"] == "estimate":
                    result["filled_items"].append(
                        {
                            "side": side,
                            "key": key,
                            "source": row["source"],
                            "grade": "estimate",
                            "note": row.get("note", ""),
                            "minimum": str(row["minimum"]),
                            "maximum": str(row["maximum"]),
                            "basis": row["basis"],
                        }
                    )
            low += min(v[0] for v in values)
            high += max(v[1] for v in values)
        totals[side] = (low, high)
    if not body.inventory_complete:
        result["flags"].append("estimate_scope_inventory_assumed")
        result["warnings"].append(
            "예상 범위의 비용 목록을 시나리오상 완전하다고 가정; 실제 누락비용 존재 가능"
        )
    # CFS never silently becomes free when absent from the input.
    a, o = totals["air"], totals["ocean"]
    low, high = a[0] - o[1], a[1] - o[0]
    if body.selected_baseline != "air":
        result["warnings"].append("estimate 단독 해상 대안은 미지원")
        return result
    result.update(
        status="partial" if result["unknown_items"] else "estimated",
        known_savings_range={"low": str(low), "high": str(high)},
        known_savings_per_pallet={
            "low": str(low / body.shipper_pallets),
            "high": str(high / body.shipper_pallets),
        },
        logistics_ranges={k: {"low": str(v[0]), "high": str(v[1])} for k, v in totals.items()},
    )
    if not result["unknown_items"]:
        result.update(
            low=str(low),
            high=str(high),
            savings_per_pallet={
                "low": str(low / body.shipper_pallets),
                "high": str(high / body.shipper_pallets),
            },
        )
    return result
