from datetime import timedelta

import pytest

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.execution.cancellation import (
    annotate_cancellations,
    cancellation_charge,
    split_amount,
)
from freshswitch_backend.execution.params import ExecutionParams
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.replan import CancelEvent, ReduceEvent, ReplanResult, replan
from freshswitch_backend.replan.models import Alternative, CancellationCharge

CONTAINER_COST = 9_060_000
PLUG_PER_DAY = 50_000


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def plan(scenario: Scenario) -> Plan:
    return build_plan(scenario).plan


def _annotated(scenario: Scenario, plan: Plan, event: CancelEvent | ReduceEvent) -> ReplanResult:
    return annotate_cancellations(replan(scenario, plan, event), scenario, plan)


@pytest.fixture(scope="module")
def tariff_reduce(scenario: Scenario, plan: Plan) -> ReplanResult:
    return _annotated(scenario, plan, ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3))


@pytest.fixture(scope="module")
def operational_reduce(scenario: Scenario, plan: Plan) -> ReplanResult:
    return _annotated(scenario, plan, ReduceEvent(lot_id="lot-pohang-gr-1", new_pallets=3))


def _alt(result: ReplanResult, alt_id: str) -> Alternative:
    return next(a for a in result.alternatives if a.id == alt_id)


def _charge(result: ReplanResult, alt_id: str) -> CancellationCharge:
    charge = _alt(result, alt_id).diff.cancellation
    assert charge is not None
    return charge


def _adjustments(alt: Alternative) -> dict[str, int]:
    return {d.shipper_id: d.cancellation_adjustment for d in alt.diff.shippers}


def test_tariff_container_shippers_receive_nothing(tariff_reduce: ReplanResult) -> None:
    alt = _alt(tariff_reduce, "sail_as_is")
    charge = _charge(tariff_reduce, "sail_as_is")
    assert charge.container_id == "CA-01"
    assert charge.payer_shipper_id == "sh-sancheong"
    assert charge.fee == round(12 * (CONTAINER_COST / 15 - CONTAINER_COST / 17)) == 852_706
    assert charge.transfers == []
    adjustments = _adjustments(alt)
    assert adjustments["sh-sancheong"] == charge.fee
    assert adjustments["sh-jinju"] == 0
    assert adjustments["sh-miryang"] == 0
    shippers = {d.shipper_id: d for d in alt.diff.shippers}
    assert shippers["sh-jinju"].cost_delta == 0
    assert shippers["sh-jinju"].cost_delta + adjustments["sh-jinju"] == 0


def test_tariff_container_full_cancel_shippers_receive_nothing(
    scenario: Scenario, plan: Plan
) -> None:
    result = _annotated(scenario, plan, CancelEvent(lot_id="lot-sancheong-sb-1"))
    for alt_id in ("pull_forward", "sail_as_is"):
        charge = _charge(result, alt_id)
        assert charge.fee > 0
        assert charge.transfers == []
        adjustments = _adjustments(_alt(result, alt_id))
        assert adjustments["sh-jinju"] == 0
        assert adjustments["sh-miryang"] == 0
        assert sum(adjustments.values()) == charge.fee


def test_refilled_tariff_container_charges_nothing(tariff_reduce: ReplanResult) -> None:
    charge = _charge(tariff_reduce, "pull_forward")
    assert charge.dead_pallets == 0
    assert charge.fee == 0
    assert charge.transfers == []


def test_operational_container_burden_equals_compensation(
    operational_reduce: ReplanResult,
) -> None:
    for alt_id in ("pull_forward", "sail_as_is"):
        alt = _alt(operational_reduce, alt_id)
        charge = _charge(operational_reduce, alt_id)
        assert charge.container_id == "CA-02"
        assert charge.payer_shipper_id == "sh-pohang"
        assert charge.fee == round(13 * (CONTAINER_COST / 16 - CONTAINER_COST / 18)) == 817_917
        assert [(t.shipper_id, t.pallets, t.amount) for t in charge.transfers] == [
            ("sh-sangju", 13, charge.fee)
        ]
        assert sum(t.amount for t in charge.transfers) == charge.fee
        adjustments = _adjustments(alt)
        assert adjustments["sh-pohang"] == charge.fee
        assert adjustments["sh-sangju"] == -charge.fee
        assert sum(adjustments.values()) == 0


def test_operational_full_cancel_burden_equals_compensation(scenario: Scenario, plan: Plan) -> None:
    result = _annotated(scenario, plan, CancelEvent(lot_id="lot-pohang-gr-1"))
    charge = _charge(result, "sail_as_is")
    assert charge.fee == round(13 * (CONTAINER_COST / 13 - CONTAINER_COST / 18))
    assert sum(t.amount for t in charge.transfers) == charge.fee
    assert {t.shipper_id for t in charge.transfers} == {"sh-sangju"}


def test_same_container_without_tariff_compensates_remaining_shippers(
    scenario: Scenario, plan: Plan, tariff_reduce: ReplanResult
) -> None:
    after = tariff_reduce.scenario_after
    assert after is not None
    operational = after.model_copy(deep=True)
    operational.cost_params.pallet_tariffs = {}
    event = ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3)
    alt = _alt(tariff_reduce, "sail_as_is")
    charge = cancellation_charge(scenario, plan, operational, alt, event)
    assert charge is not None
    assert charge.fee == 852_706
    transfers = {t.shipper_id: t.amount for t in charge.transfers}
    assert set(transfers) == {"sh-jinju", "sh-miryang"}
    assert sum(transfers.values()) == charge.fee


def test_delayed_tariff_container_returns_only_plug_share(
    scenario: Scenario, plan: Plan, tariff_reduce: ReplanResult
) -> None:
    after = tariff_reduce.scenario_after
    assert after is not None
    delayed = after.model_copy(deep=True)
    voyage = next(v for v in delayed.voyages if v.id == "V-2611A")
    voyage.original_etd = voyage.etd
    voyage.etd = voyage.etd + timedelta(days=2)
    event = ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3)
    alt = _alt(tariff_reduce, "sail_as_is")
    charge = cancellation_charge(scenario, plan, delayed, alt, event)
    assert charge is not None
    plug = 2 * PLUG_PER_DAY
    step = 1 / 15 - 1 / 17
    assert charge.container_cost == CONTAINER_COST + plug
    assert charge.fee == round(12 * (CONTAINER_COST + plug) * step)
    transfers = {t.shipper_id: t for t in charge.transfers}
    assert set(transfers) == {"sh-jinju", "sh-miryang"}
    assert transfers["sh-jinju"].share_increase == round(8 * plug * step)
    assert transfers["sh-miryang"].share_increase == round(4 * plug * step)
    received = sum(t.amount for t in charge.transfers)
    assert received == round(12 * plug * step)
    assert charge.fee - received == round(12 * CONTAINER_COST * step)


def test_split_amount_keeps_total() -> None:
    assert split_amount(10, [1.0, 1.0, 1.0]) == [4, 3, 3]
    assert split_amount(7, [0.0, 2.0]) == [0, 7]
    assert split_amount(5, []) == []
    assert split_amount(5, [0.0, 0.0]) == [0, 0]


def test_mixed_container_splits_between_operational_shippers_and_consolidator(
    scenario: Scenario, plan: Plan, tariff_reduce: ReplanResult
) -> None:
    after = tariff_reduce.scenario_after
    assert after is not None
    mixed = after.model_copy(deep=True)
    miryang_lot = next(x for x in mixed.lots if x.id == "lot-miryang-sb-1")
    miryang_lot.product_id = "grape_so2"
    event = ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3)
    alt = _alt(tariff_reduce, "sail_as_is")
    charge = cancellation_charge(scenario, plan, mixed, alt, event)
    assert charge is not None
    step = 1 / 15 - 1 / 17
    assert charge.fee == round(12 * CONTAINER_COST * step) == 852_706
    transfers = {t.shipper_id: t for t in charge.transfers}
    assert set(transfers) == {"sh-miryang"}
    miryang = transfers["sh-miryang"]
    assert miryang.pallets == 4
    assert miryang.share_increase == round(4 * CONTAINER_COST * step)
    assert abs(miryang.amount - 4 * CONTAINER_COST * step) <= 1
    received = sum(t.amount for t in charge.transfers)
    consolidator_part = charge.fee - received
    assert abs(consolidator_part - 8 * CONTAINER_COST * step) <= 1
    assert received > 0 and consolidator_part > 0
    assert received + consolidator_part == charge.fee


def test_zero_fee_rate_keeps_operational_share_increase_listed(
    scenario: Scenario, plan: Plan, operational_reduce: ReplanResult
) -> None:
    after = operational_reduce.scenario_after
    assert after is not None
    params = ExecutionParams()
    params.cancellation_fee_rate = params.cancellation_fee_rate.model_copy(update={"value": 0.0})
    event = ReduceEvent(lot_id="lot-pohang-gr-1", new_pallets=3)
    alt = _alt(operational_reduce, "sail_as_is")
    charge = cancellation_charge(scenario, plan, after, alt, event, params)
    assert charge is not None
    assert charge.fee == 0
    assert [(t.shipper_id, t.amount) for t in charge.transfers] == [("sh-sangju", 0)]
    assert charge.transfers[0].share_increase == round(
        13 * (CONTAINER_COST / 16 - CONTAINER_COST / 18)
    )
