"""Bind explicit evidence snapshots to solver assignments; no simulated prices."""

import math
from collections import defaultdict
from decimal import Decimal

from freshswitch_backend.planning.public_rates import table
from freshswitch_backend.planning.robust_savings import RobustRequest, robust_savings


def plan_robust(scenario, plan) -> dict[str, dict]:
    loads = defaultdict(int)
    shippers = defaultdict(int)
    voyages = {v.id: v for v in scenario.voyages}
    for a in plan.assignments:
        if a.mode == "sea":
            loads[a.container_id] += a.pallets
            shippers[(a.container_id, a.shipper_id)] += a.pallets
    output = {}
    plug = next(r for r in table("public_rates") if r["id"] == "pnc_plug")
    for (cid, sid), n in shippers.items():
        key = f"{cid}:{sid}"
        raw = scenario.robust_quote_inputs.get(key)
        if raw is None:
            continue
        body = RobustRequest.model_validate(raw).model_copy(deep=True)
        warnings = []
        if body.shipper_pallets not in (None, n) or body.container_pallets not in (
            None,
            loads[cid],
        ):
            warnings.append("입력 n/N과 CP-SAT 배정 물량 불일치: 배정 결과로 재계산")
        body.shipper_pallets, body.container_pallets = n, loads[cid]
        assignment = next(a for a in plan.assignments if a.container_id == cid)
        voyage = voyages[assignment.voyage_id]
        gate = scenario.container_gate_in.get(cid)
        if gate is not None and voyage.delay_p90_h is not None:
            hours = (voyage.etd - gate).total_seconds() / 3600
            if hours < 0:
                warnings.append("반입 시각이 계획 출항 이후여서 전원일수 기본값 사용")
                days = 2
                body.flags = list({*body.flags, "plug_days_default"})
            else:
                days = math.ceil((hours + voyage.delay_p90_h) / 24)
                body.flags = [f for f in body.flags if f != "plug_days_default"]
        else:
            days = 2
            body.flags = list({*body.flags, "plug_days_default"})
        for item in body.ocean:
            if (
                item.key == "plug"
                and item.supplier == "PNC"
                and item.grade != "confirmed"
                and plug["amount"] is not None
            ):
                amount = Decimal(str(plug["amount"])) * days
                if voyage.etd.month in plug["seasonal_months"]:
                    if plug.get("seasonal_status") != "reference":
                        item.minimum = item.maximum = None
                        item.grade = "unknown"
                        continue
                    amount *= Decimal(plug["seasonal_multiplier"])
                item.minimum = item.maximum = amount
                item.grade = "reference"
        result = robust_savings(body).model_dump(mode="json")
        result["warnings"].extend(warnings)
        result.update(
            shipper_pallets=n,
            container_pallets=loads[cid],
            plug_days=days,
            container_id=cid,
            shipper_id=sid,
        )
        output[key] = result
    return output


def robust_changes(before_scenario, before_plan, after_scenario, after_plan):
    before = plan_robust(before_scenario, before_plan)
    after = plan_robust(after_scenario, after_plan)
    return {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    }
