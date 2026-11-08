from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from freshswitch_backend.execution.quantity_policy import (
    QuantityPolicy,
    QuantityReservation,
    allocate_refill,
    amounts,
    change_quantity,
    confirm_quantity,
    distribute_credits,
    quote_quantity,
    vacancy_quantity,
)

CUTOFF = datetime(2026, 11, 17, 9, tzinfo=UTC)
MID = CUTOFF - timedelta(days=2, hours=12)
LATE = CUTOFF - timedelta(days=1)


def policy(**changes) -> QuantityPolicy:
    return QuantityPolicy(cutoff=CUTOFF, departure=CUTOFF + timedelta(days=1), **changes)


def reservation(quantity=5, **changes) -> QuantityReservation:
    return QuantityReservation(
        **{
            "reservation_id": "reserve-a",
            "lot_id": "lot-a",
            "shipper_id": "shipper-a",
            "shipper_org_id": "org-a",
            "container_id": "container-a",
            "current_quantity": quantity,
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("at", "quantity", "charged", "fee"),
    [
        (CUTOFF - timedelta(days=4), 0, 0, 0),
        (MID, 4, 0, 0),
        (MID, 3, 1, 49_800),
        (MID, 0, 4, 199_200),
        (LATE, 4, 0, 0),
        (LATE, 0, 4, 332_000),
    ],
)
def test_user_fee_examples(at, quantity, charged, fee):
    original = reservation()
    quoted = quote_quantity(original, policy(), quantity, at)
    changed = change_quantity(original, policy(), quantity, at, "change-1")
    assert quoted.newly_charged_quantity == charged
    assert quoted.fee_krw == fee
    assert amounts(changed) == (fee, 0, fee)
    assert original.current_quantity == 5
    assert original.events == []


@pytest.mark.parametrize(
    ("at", "quantity", "refill", "gross", "credit", "final"),
    [(MID, 3, 2, 49_800, 49_800, 0), (LATE, 0, 1, 332_000, 83_000, 249_000)],
)
def test_user_refill_examples(at, quantity, refill, gross, credit, final):
    changed = change_quantity(reservation(), policy(), quantity, at, "change-1")
    [filled] = allocate_refill([changed], "container-a", refill, "refill-1", at)
    assert amounts(filled) == (gross, credit, final)
    assert vacancy_quantity(filled) == 5 - quantity - refill
    assert changed.offsets == []


@pytest.mark.parametrize(
    ("at", "rate"),
    [
        (CUTOFF - timedelta(days=3, microseconds=1), Decimal(0)),
        (CUTOFF - timedelta(days=3), Decimal("0.3")),
        (CUTOFF - timedelta(days=2, microseconds=1), Decimal("0.3")),
        (CUTOFF - timedelta(days=2), Decimal("0.5")),
    ],
)
def test_rate_boundary_uses_exact_instant(at, rate):
    assert quote_quantity(reservation(), policy(), 0, at).rate == rate
    kst = timezone(timedelta(hours=9))
    assert quote_quantity(reservation(), policy(), 0, at.astimezone(kst)).rate == rate


@pytest.mark.parametrize("at", [CUTOFF, CUTOFF + timedelta(microseconds=1)])
def test_changes_close_exactly_at_cutoff(at):
    with pytest.raises(ValueError):
        change_quantity(reservation(), policy(), 0, at, "closed")


def test_one_free_pallet_is_cumulative_per_lot():
    first = change_quantity(reservation(), policy(), 4, MID, "first")
    second = change_quantity(first, policy(), 3, MID, "second")
    third = change_quantity(second, policy(), 2, LATE, "third")
    assert [event.charged_quantity for event in third.events] == [0, 1, 1]
    assert amounts(third) == (132_800, 0, 132_800)
    other = change_quantity(
        reservation(reservation_id="reserve-b", lot_id="lot-b"), policy(), 4, MID, "other"
    )
    assert amounts(other) == (0, 0, 0)


def test_preconfirmation_change_becomes_confirmation_baseline():
    held = change_quantity(reservation(), policy(), 3, CUTOFF - timedelta(days=4), "held-change")
    fixed = confirm_quantity(held, policy(), CUTOFF - timedelta(days=3))
    assert fixed.confirmed_quantity == 3
    assert fixed.confirmed_by == "system"
    changed = change_quantity(fixed, policy(), 1, MID, "confirmed-change")
    assert amounts(changed) == (49_800, 0, 49_800)
    assert held.confirmed_quantity is None


def test_confirmation_is_idempotent_and_retains_original_policy_snapshot():
    original = confirm_quantity(reservation(), policy(), MID, actor="shipper")
    again = confirm_quantity(original, policy(slot_rate_krw=999), LATE)
    assert again == original
    assert again.policy_snapshot.slot_rate_krw == 166_000


def test_confirmed_quote_uses_original_cutoff_after_config_changes():
    fixed = confirm_quantity(reservation(), policy(), MID)
    earlier = policy().model_copy(update={"cutoff": MID - timedelta(hours=1)})
    quoted = quote_quantity(fixed, earlier, 0, MID)
    assert quoted.fee_krw == 199_200


def test_refills_offset_highest_rate_first_without_repricing_history():
    first = change_quantity(reservation(), policy(), 3, MID, "mid-change")
    second = change_quantity(first, policy(), 1, LATE, "late-change")
    events_before = [event.model_dump() for event in second.events]
    [filled] = allocate_refill([second], "container-a", 1, "fill-high", LATE)
    assert amounts(filled) == (215_800, 83_000, 132_800)
    assert filled.offsets[0].reduction_event_id == "late-change"
    assert [event.model_dump() for event in filled.events] == events_before
    [filled_again] = allocate_refill([filled], "container-a", 2, "fill-rest", LATE)
    assert amounts(filled_again) == (215_800, 215_800, 0)
    assert vacancy_quantity(filled_again) == 1


def test_equal_rate_refill_priority_uses_original_event_time():
    first = change_quantity(reservation(), policy(), 3, MID, "z-first")
    second = change_quantity(first, policy(), 2, MID + timedelta(minutes=1), "a-second")
    [filled] = allocate_refill([second], "container-a", 1, "fill", LATE)
    assert filled.offsets[0].reduction_event_id == "z-first"


@pytest.mark.parametrize("exemption", ["pilot", "ai_air"])
def test_exempt_events_never_gain_charge_when_refilled(exemption):
    rules = policy(pilot=exemption == "pilot")
    changed = change_quantity(reservation(), rules, 0, LATE, "exempt", ai_air=exemption == "ai_air")
    [filled] = allocate_refill([changed], "container-a", 3, "fill", LATE)
    assert changed.events[0].exemption == exemption
    assert amounts(changed) == amounts(filled) == (0, 0, 0)


def test_event_is_immutable_and_holds_rate_and_slot_price():
    changed = change_quantity(reservation(), policy(), 0, MID, "change")
    event = changed.events[0]
    assert (event.at, event.rate, event.slot_rate_krw) == (MID, Decimal("0.3"), 166_000)
    with pytest.raises(ValidationError):
        event.rate = Decimal("0.5")
    quoted = quote_quantity(changed, policy(slot_rate_krw=9), 0, LATE)
    assert quoted.final_fee_krw == 199_200


def test_duplicate_reduction_is_safe_and_reused_key_cannot_change_quantity():
    changed = change_quantity(reservation(), policy(), 3, MID, "same-key")
    repeated = change_quantity(changed, policy(), 3, LATE, "same-key")
    assert repeated == changed
    with pytest.raises(ValueError):
        change_quantity(changed, policy(), 1, LATE, "same-key")


def test_refill_is_idempotent_and_cannot_exceed_actual_recorded_vacancy():
    changed = change_quantity(reservation(), policy(), 3, MID, "change")
    [filled] = allocate_refill([changed], "container-a", 1, "fill", LATE)
    assert allocate_refill([filled], "container-a", 1, "fill", CUTOFF) == [filled]
    with pytest.raises(ValueError):
        allocate_refill([filled], "container-a", 2, "fill", LATE)
    with pytest.raises(ValueError):
        allocate_refill([filled], "container-a", 2, "overfill", LATE)
    with pytest.raises(ValueError):
        allocate_refill([filled], "other-container", 1, "wrong-vacancy", LATE)


def test_reincrease_does_not_itself_grant_refill_credit_or_recharge_prior_assessment():
    first = change_quantity(reservation(), policy(), 3, MID, "reduce")
    increased = change_quantity(first, policy(), 5, MID, "increase")
    reduced = change_quantity(increased, policy(), 3, LATE, "reduce-again")
    assert amounts(first) == amounts(increased) == amounts(reduced) == (49_800, 0, 49_800)
    assert reduced.assessed_quantity == 1
    assert reduced.confirmed_quantity == 5


def test_compensation_matches_user_example_and_conserves_every_won():
    assert distribute_credits(332_000, {"a": 8, "b": 4}) == {"a": 221_333, "b": 110_667}
    for total in [0, 1, 2, 7, 49_800, 332_000, 10**18 + 1]:
        result = distribute_credits(total, {"a": 8, "b": 4, "c": 3, "zero": 0})
        assert sum(result.values()) == total
        assert all(isinstance(value, int) and value >= 0 for value in result.values())
        assert "zero" not in result


@pytest.mark.parametrize("slot_rate", [1, 3, 7, 101, 166_001])
def test_fractional_won_rounding_never_refunds_more_than_original_charge(slot_rate):
    changed = change_quantity(reservation(), policy(slot_rate_krw=slot_rate), 0, MID, "change")
    original = amounts(changed)[0]
    for count in range(1, 6):
        [changed] = allocate_refill([changed], "container-a", 1, f"fill-{count}", LATE)
        gross, credit, final = amounts(changed)
        assert gross == original
        assert 0 <= credit <= gross
        assert 0 <= final <= gross
        assert credit + final == gross
    assert amounts(changed) == (original, original, 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"hold_days": 2},
        {"confirm_days": 1},
        {"rate_mid": Decimal("0.6")},
        {"slot_rate_krw": -1},
        {"offer_timeout_minutes": 0},
    ],
)
def test_invalid_policy_is_rejected(changes):
    with pytest.raises(ValidationError):
        policy(**changes)
