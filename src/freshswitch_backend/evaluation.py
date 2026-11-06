"""Offline evaluation: uv run python -m freshswitch_backend.evaluation --output report.json."""

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from fastapi.testclient import TestClient

from freshswitch_backend.analysis.benchmark import violations
from freshswitch_backend.config import get_settings
from freshswitch_backend.domain.models import Plan
from freshswitch_backend.forecast.history import KST, DepartureRecord, all_records
from freshswitch_backend.forecast.predictor import DelayPredictor, DelayQuery, quantile
from freshswitch_backend.main import create_app
from freshswitch_backend.planning.store import store
from freshswitch_backend.replan import apply_event, locations
from freshswitch_backend.replan.explanation import llm_enabled
from freshswitch_backend.replan.models import ReplanResult


def forecast_backtest(records: list[DepartureRecord], train_fraction: float = 0.8) -> dict:
    """One chronological holdout; labels must already exist at the split instant."""
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between 0 and 1")
    cohort = sorted(
        {r.key: r for r in records if r.is_container}.values(),
        key=lambda r: r.planned_departure_at,
    )
    if len(cohort) < 5:
        raise ValueError("At least five container departures are required")
    cutoff = cohort[min(len(cohort) - 1, int(len(cohort) * train_fraction))].planned_departure_at
    train = [r for r in cohort if r.planned_departure_at < cutoff and r.departure_at < cutoff]
    test = [r for r in cohort if r.planned_departure_at >= cutoff]
    if not train or not test:
        raise ValueError("Chronological split has no training or evaluation samples")
    predictor = DelayPredictor(train)
    baseline50 = quantile([r.delay_hours for r in train], 0.5)
    baseline90 = quantile([r.delay_hours for r in train], 0.9)
    rows = []
    for record in test:
        prediction = predictor.predict(
            DelayQuery(
                vessel_name=record.vessel_name,
                call_sign=record.call_sign,
                terminal=record.terminal,
                planned_departure=record.planned_departure_at.astimezone(KST).date(),
            )
        )
        rows.append(
            {
                "actual": record.delay_hours,
                "p50": prediction.p50_hours,
                "p90": prediction.p90_hours,
                "confidence": prediction.confidence,
            }
        )

    def metrics(pairs: list[tuple[float, float, float]]) -> dict:
        n = len(pairs)
        return {
            "p50_mae_hours": round(sum(abs(y - p50) for y, p50, _ in pairs) / n, 4),
            "p90_coverage": round(sum(y <= p90 for y, _, p90 in pairs) / n, 4),
            "p90_pinball_hours": round(
                sum(max(0.9 * (y - p90), -0.1 * (y - p90)) for y, _, p90 in pairs) / n, 4
            ),
        }

    model = metrics([(r["actual"], r["p50"], r["p90"]) for r in rows])
    baseline = metrics([(r["actual"], baseline50, baseline90) for r in rows])
    return {
        "target": "actual departure minus planned departure in hours; not berthing delay",
        "cohort": "container ships only; duplicated voyage keys removed",
        "method": "fixed chronological holdout; no weather/congestion assumptions applied",
        "cutoff": cutoff.isoformat(),
        "train_count": len(train),
        "test_count": len(test),
        "unavailable_training_labels_excluded": sum(
            r.planned_departure_at < cutoff and r.departure_at >= cutoff for r in cohort
        ),
        "model": model,
        "global_quantile_baseline": baseline,
        "baseline_quantiles_hours": {"p50": baseline50, "p90": baseline90},
        "p50_mae_improvement_hours": round(baseline["p50_mae_hours"] - model["p50_mae_hours"], 4),
        "confidence_counts": {
            level: sum(r["confidence"] == level for r in rows)
            for level in ("high", "medium", "low")
        },
        "limitations": [
            "A single historical split is not proof of future operational accuracy.",
            "Terminal/operator fields are historical reported values, not an as-of feature store.",
            "Quality-loss seed observations are simulated and are not evaluated as real labels.",
        ],
    }


def plan_checks(scenario, plan: Plan, routes, fixed: dict[str, str]) -> list[str]:
    errors = violations(scenario, plan)
    where = locations(plan)
    ids = [a.lot_id for a in plan.assignments] + [u.lot_id for u in plan.unassigned]
    if len(ids) != len(set(ids)) or set(ids) != {lot.id for lot in scenario.lots}:
        errors.append("lot conservation: duplicate or missing assignment")
    errors += [
        f"fixed assignment changed: {lid}" for lid, loc in fixed.items() if where.get(lid) != loc
    ]
    expected = {a.lot_id for a in plan.assignments if a.mode == "sea"}
    visited = []
    truck_intervals = {}
    for voyage in routes:
        errors += [f"pickup dropped: {lid}" for lid in voyage.dropped_lot_ids]
        for route in voyage.pickup.routes:
            truck_intervals.setdefault(route.truck_id, []).append(
                (route.earliest_departure, route.port_eta)
            )
            if route.port_eta > voyage.pickup.deadline:
                errors.append(f"pickup cutoff missed: {route.truck_id}")
            if route.load_pallets > route.capacity_pallets:
                errors.append(f"truck capacity exceeded: {route.truck_id}")
            for stop in route.stops:
                visited.extend(stop.lot_ids)
                if stop.eta < stop.ready_at or stop.eta > stop.latest_arrival:
                    errors.append(f"pickup time window missed: {stop.stop_id}")
    if set(visited) != expected or len(visited) != len(set(visited)):
        errors.append("pickup coverage: duplicate or missing sea lot")
    for truck_id, intervals in truck_intervals.items():
        intervals.sort()
        if any(start < previous_end for (_, previous_end), (start, _) in pairwise(intervals)):
            errors.append(f"truck routes overlap: {truck_id}")
    return errors


def optimization_evaluation(repeats: int = 1, use_llm: bool = False) -> dict:
    if repeats < 1:
        raise ValueError("repeats must be positive")
    if use_llm and not llm_enabled(True):
        raise ValueError("Live evaluation requires EXPLAIN_USE_LLM, LLM_API_KEY and LLM_MODEL_NAME")
    rows = []
    with TestClient(create_app()) as client:
        started = time.perf_counter()
        response = client.post("/api/plans", json={"scenario_id": "default"})
        response.raise_for_status()
        creation_ms = round((time.perf_counter() - started) * 1000)
        session = store.get(response.json()["plan_id"])
        base = locations(session.plan)
        lot = next(lot for lot in session.scenario.lots if lot.id == "lot-sancheong-sb-1")
        voyage = session.scenario.voyages[0]
        cases = [
            {"type": "reduce", "lot_id": lot.id, "new_pallets": lot.pallets - 2},
            {"type": "cancel", "lot_id": lot.id},
            {"type": "air_switch", "lot_id": lot.id},
            {
                "type": "delay",
                "voyage_id": voyage.id,
                "new_etd": (voyage.etd + timedelta(hours=12)).isoformat(),
            },
            {
                "type": "add_lot",
                "lot": lot.model_copy(
                    update={
                        "id": "evaluation-added-lot",
                        "pallets": 2,
                    }
                ).model_dump(mode="json"),
            },
        ]
        for iteration in range(repeats):
            for event in cases:
                started = time.perf_counter()
                response = client.post(
                    f"/api/plans/{session.plan_id}/events",
                    params={"llm": str(use_llm).lower()},
                    json={"event": event},
                )
                elapsed = (time.perf_counter() - started) * 1000
                row = {
                    "event": event["type"],
                    "iteration": iteration + 1,
                    "elapsed_ms": round(elapsed),
                    "http_status": response.status_code,
                    "within_10_seconds": elapsed <= 10_000,
                }
                if response.status_code != 200:
                    row.update(passed=False, error=response.json())
                    rows.append(row)
                    continue
                result = ReplanResult.model_validate(response.json())
                after = apply_event(session.scenario, result.event)
                fixed = {lid: base[lid] for lid in result.fixed_lot_ids}
                candidates = []
                for alt in result.alternatives:
                    errors = plan_checks(after, alt.plan, alt.routes, fixed)
                    candidates.append(
                        {
                            "id": alt.id,
                            "feasible": alt.feasible,
                            "violations": errors,
                            "cost_krw": alt.total_cost,
                            "unassigned_lots": len(alt.plan.unassigned),
                            "fill": {
                                c.container_id: c.utilization for c in alt.plan.containers if c.used
                            },
                        }
                    )
                recommended = next(
                    (a for a in candidates if a["id"] == result.recommended_id), None
                )
                manual = next(a for a in candidates if a["id"] == "sail_as_is")
                cheapest = min((a["cost_krw"] for a in candidates if a["feasible"]), default=None)
                row.update(
                    recommended_id=result.recommended_id,
                    alternatives=candidates,
                    passed=bool(
                        recommended
                        and recommended["cost_krw"] == cheapest
                        and not recommended["violations"]
                        and not recommended["unassigned_lots"]
                        and all(not a["violations"] for a in candidates if a["feasible"])
                        and row["within_10_seconds"]
                    ),
                    explanation_source=result.explanation.generated_by,
                    fallback_reason=result.explanation.fallback_reason,
                    savings_vs_as_is_krw=(manual["cost_krw"] - recommended["cost_krw"])
                    if recommended and manual["feasible"]
                    else None,
                )
                rows.append(row)
    times = [r["elapsed_ms"] for r in rows]
    return {
        "scope": "in-process HTTP requests including explanation; excludes internet/proxy latency",
        "live_llm_requested": use_llm,
        "initial_plan_ms": creation_ms,
        "timeout_seconds": get_settings().replan_timeout_seconds,
        "p50_ms": quantile(times, 0.5),
        "p95_ms": quantile(times, 0.95),
        "explanation_counts": {
            source: sum(r.get("explanation_source") == source for r in rows)
            for source in ("llm", "template")
        },
        "llm_acceptance_rate": sum(r.get("explanation_source") == "llm" for r in rows) / len(rows)
        if use_llm
        else None,
        "passed": all(r["passed"] for r in rows),
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--live-llm", action="store_true", help="Opt in to paid external LLM calls")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "forecast": forecast_backtest(all_records()),
        "optimization": optimization_evaluation(args.repeats, args.live_llm),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Evaluation written to {args.output}")
    return 0 if report["optimization"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
