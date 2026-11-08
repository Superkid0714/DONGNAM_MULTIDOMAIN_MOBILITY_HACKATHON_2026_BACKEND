from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.execution.models import ConfirmRequest
from freshswitch_backend.execution.quantity_policy import QuantityPolicy
from freshswitch_backend.lots.store import lot_store
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import (
    LotRow,
    OrganizationRow,
    PackhouseRow,
    PlanConfirmationRow,
    PlanForwarderRow,
    PlanRow,
    ShipperProfileRow,
)
from freshswitch_backend.planning.refill import (
    RefillState,
    RefillVacancy,
    _detour,
    advance_offers,
    build_quantity_increase,
    join_waitlist,
    preview_refill,
    reconcile_refills,
    respond_offer,
)
from freshswitch_backend.planning.store import SessionConflict, store
from freshswitch_backend.replan import IncreaseEvent, ReduceEvent, apply_event

NOW = datetime(2026, 11, 10, tzinfo=UTC)
OWNER = "refill-platform"
SHIPPER = "refill-shipper"


@pytest.fixture
def refill_plan():
    scenario = get_scenario("default").model_copy(deep=True)
    base = scenario.lots[0]
    scenario.lots = [base]
    scenario.containers = [scenario.containers[0]]
    scenario.voyages = [scenario.voyages[0]]
    scenario.cost_params.sea_freight_per_container_krw.value = 100000
    scenario.cost_params.sea_ancillary_per_container_krw.value = 100000
    with database.begin() as db:
        db.add_all(
            [
                OrganizationRow(id=OWNER, type="platform", name="리필 운영"),
                OrganizationRow(id=SHIPPER, type="shipper", name="리필 화주"),
            ]
        )
        db.flush()
        db.add(lot_store._row(base, scenario.id, None, SHIPPER))
        for index in range(3):
            lot = base.model_copy(update={"id": f"spare-{index}", "pallets": 5})
            row = lot_store._row(lot, scenario.id, None, SHIPPER)
            row.created_at = NOW + timedelta(seconds=index)
            db.add(row)
    session = store.create(scenario, [base.id], OWNER, lot_org_id=SHIPPER)
    assert session.plan.assignments[0].container_id == scenario.containers[0].id
    policy = QuantityPolicy(cutoff=scenario.voyages[0].cy_cutoff, departure=scenario.voyages[0].etd)
    vacancy = RefillVacancy(
        vacancy_id="cancellation-event-1",
        reservation_id="original-booking",
        container_id=scenario.containers[0].id,
        voyage_id=scenario.voyages[0].id,
        product_id=base.product_id,
        destination=base.destination,
        quantity=3,
    )
    return session.plan_id, policy, vacancy


def _row(db, plan_id):
    return db.get(PlanRow, plan_id, with_for_update=True)


def _waiting(db, row, policy, vacancy, count=2):
    state = RefillState()
    for index in range(count):
        state = join_waitlist(
            db,
            row,
            state,
            policy,
            vacancy.voyage_id,
            f"spare-{index}",
            SHIPPER,
            5,
            NOW + timedelta(seconds=index),
        )
    return state


def test_waitlist_fifo_expiration_survives_serialization_and_decline_advances(refill_plan):
    plan_id, policy, vacancy = refill_plan
    with database.begin() as db:
        row = _row(db, plan_id)
        state = _waiting(db, row, policy, vacancy, 3)
        state = advance_offers(db, row, state, [vacancy], policy, NOW)
        assert [o.lot_id for o in state.offers] == ["spare-0"]
        assert state.offers[0].expires_at == NOW + timedelta(minutes=30)
        assert state.offers[0].pickup_at < state.offers[0].port_eta <= policy.cutoff
        assert state.offers[0].destination == vacancy.destination
        state = RefillState.model_validate_json(state.model_dump_json())
        state = advance_offers(db, row, state, [vacancy], policy, NOW + timedelta(minutes=30))
        assert [o.status for o in state.offers] == ["expired", "pending"]
        assert state.offers[1].lot_id == "spare-1"
        state = respond_offer(
            db,
            row,
            state,
            [vacancy],
            policy,
            state.offers[1].offer_id,
            SHIPPER,
            False,
            None,
            NOW + timedelta(minutes=31),
        )
        assert [o.status for o in state.offers] == ["expired", "rejected", "pending"]
        assert state.offers[2].lot_id == "spare-2"


def test_registered_expansion_only_at_t3_and_after_waitlist(refill_plan):
    plan_id, policy, vacancy = refill_plan
    with database.begin() as db:
        row = _row(db, plan_id)
        early = advance_offers(
            db, row, RefillState(), [vacancy], policy, policy.confirms_at - timedelta(seconds=1)
        )
        assert not early.offers
        later = advance_offers(db, row, RefillState(), [vacancy], policy, policy.confirms_at)
        assert len(later.offers) == 1
        assert later.offers[0].source == "registered"
        assert later.offers[0].detour_minutes is not None
        assert later.offers[0].detour_minutes <= 30
        state = _waiting(db, row, policy, vacancy, 2)
        state.waitlist[0].status = "withdrawn"
        offered = advance_offers(db, row, state, [vacancy], policy, policy.confirms_at)
        assert offered.offers[0].lot_id == "spare-1"
        assert offered.offers[0].source == "waitlist"


def test_registered_expansion_prefers_shorter_real_detour_over_registration_age(refill_plan):
    plan_id, policy, vacancy = refill_plan
    session = store.get(plan_id, OWNER)
    original = session.scenario.lots[0]
    shipper = next(s for s in session.scenario.shippers if s.id == original.shipper_id)
    packhouse = next(p for p in session.scenario.packhouses if p.id == shipper.packhouse_id)
    with database.begin() as db:
        db.add(
            PackhouseRow(
                id="extra-stop",
                org_id=SHIPPER,
                name="추가 집하장",
                region=packhouse.region,
                lat=packhouse.lat,
                lon=packhouse.lon,
            )
        )
        db.get(LotRow, "spare-0").packhouse = "extra-stop"
        db.get(LotRow, "spare-2").status = "cancelled"
        db.flush()
        row = _row(db, plan_id)
        older = original.model_copy(
            update={"id": "spare-0", "packhouse_id": "extra-stop", "pallets": 3}
        )
        newer = original.model_copy(update={"id": "spare-1", "pallets": 3})
        _, farther = preview_refill(
            db, row, older, vacancy, policy, policy.confirms_at, require_detour=True
        )
        _, nearer = preview_refill(
            db, row, newer, vacancy, policy, policy.confirms_at, require_detour=True
        )
        assert farther is not None and nearer is not None
        assert 0 <= nearer < farther <= policy.detour_max_minutes
        offered = advance_offers(db, row, RefillState(), [vacancy], policy, policy.confirms_at)
        assert len(offered.offers) == 1
        assert offered.offers[0].lot_id == "spare-1"
        assert offered.offers[0].detour_minutes == nearer
        waiting = _waiting(db, row, policy, vacancy, 1)
        fifo = advance_offers(db, row, waiting, [vacancy], policy, policy.confirms_at)
        assert fifo.offers[0].lot_id == "spare-0"
        assert fifo.offers[0].source == "waitlist"


def test_partial_acceptance_is_atomic_and_counts_only_after_approval(refill_plan):
    plan_id, policy, vacancy = refill_plan
    with database.begin() as db:
        row = _row(db, plan_id)
        state = _waiting(db, row, policy, vacancy, 2)
        state = advance_offers(db, row, state, [vacancy], policy, NOW)
        offer_id = state.offers[0].offer_id
        state = respond_offer(db, row, state, [vacancy], policy, offer_id, SHIPPER, True, 2, NOW)
        offer = state.offers[0]
        assert offer.status == "pending_execution"
        assert offer.split_parent_lot_id == "spare-0"
        original = db.get(LotRow, "spare-0")
        child = db.get(LotRow, offer.accepted_lot_id)
        assert (original.pallets, child.pallets) == (3, 2)
        assert (original.status, child.status) == ("registered", "planned")
        state, offsets = reconcile_refills(db, row, state, NOW)
        assert offsets == []
        replay = respond_offer(db, row, state, [vacancy], policy, offer_id, SHIPPER, True, 2, NOW)
        assert len(replay.offers) == 1
    order = store.order(plan_id, offer.order_id, OWNER)
    for confirmation in order.shipper_confirmations:
        store.confirm(
            plan_id,
            order.order_id,
            ConfirmRequest(actor="shipper", shipper_id=confirmation.shipper_id, accept=True),
            OWNER,
        )
    store.confirm(plan_id, order.order_id, ConfirmRequest(actor="forwarder", accept=True), OWNER)
    with database.begin() as db:
        row = _row(db, plan_id)
        state, offsets = reconcile_refills(db, row, state, NOW)
        assert state.offers[0].status == "committed"
        assert len(offsets) == 1
        assert offsets[0].quantity == 2
        assert offsets[0].container_id == vacancy.container_id
        assert state.waitlist[0].fulfilled_quantity == 2
        assert reconcile_refills(db, row, state, NOW)[1] == []
        vacancy.refilled_quantity = 2
        state = advance_offers(db, row, state, [vacancy], policy, NOW)
        assert state.offers[-1].lot_id == "spare-1"
        assert state.offers[-1].offered_quantity == 1


def test_split_rolls_back_if_routing_fails(refill_plan):
    plan_id, policy, vacancy = refill_plan
    with database.begin() as db:
        row = _row(db, plan_id)
        state = advance_offers(
            db, row, _waiting(db, row, policy, vacancy, 1), [vacancy], policy, NOW
        )
    with pytest.raises(SessionConflict), database.begin() as db:
        row = _row(db, plan_id)
        impossible = vacancy.model_copy(update={"container_id": "missing-container"})
        respond_offer(
            db, row, state, [impossible], policy, state.offers[0].offer_id, SHIPPER, True, 2, NOW
        )
    with database.begin() as db:
        assert db.get(LotRow, "spare-0").pallets == 5
        assert not list(db.scalars(select(LotRow).where(LotRow.id.like("refill-%"))))


def test_claimed_lot_cannot_be_accepted_twice(refill_plan):
    plan_id, policy, vacancy = refill_plan
    with database.begin() as db:
        row = _row(db, plan_id)
        state = advance_offers(
            db, row, _waiting(db, row, policy, vacancy, 1), [vacancy], policy, NOW
        )
        db.get(LotRow, "spare-0").status = "planned"
    with pytest.raises(SessionConflict), database.begin() as db:
        respond_offer(
            db,
            _row(db, plan_id),
            state,
            [vacancy],
            policy,
            state.offers[0].offer_id,
            SHIPPER,
            True,
            2,
            NOW,
        )


def test_missing_detour_evidence_is_not_zero(refill_plan):
    plan_id, _, _ = refill_plan
    session = store.get(plan_id, OWNER)
    route = session.routes[0].pickup.routes[0]
    assert _detour([], route, "spare") is None


def test_registered_offer_requires_original_route_evidence(refill_plan):
    plan_id, policy, vacancy = refill_plan
    with database.begin() as db:
        row = _row(db, plan_id)
        row.payload = {**row.payload, "routes": []}
        db.flush()
        state = advance_offers(db, row, RefillState(), [vacancy], policy, policy.confirms_at)
        assert state.offers == []


def test_departed_route_cannot_accept_new_load(refill_plan):
    plan_id, policy, vacancy = refill_plan
    session = store.get(plan_id, OWNER)
    depart = session.routes[0].pickup.routes[0].earliest_departure
    with database.begin() as db:
        row = _row(db, plan_id)
        original = session.scenario.lots[0]
        extra = original.model_copy(update={"id": "spare-0", "pallets": 3})
        try:
            result, _ = preview_refill(
                db, row, extra, vacancy, policy, depart, require_detour=False
            )
        except SessionConflict:
            return
        before = session.routes[0].pickup.routes[0]
        after = next(
            r
            for vp in result.alternatives[0].routes
            for r in vp.pickup.routes
            if r.truck_id == before.truck_id and r.trip == before.trip
        )
        assert after == before
        assert "spare-0" not in {lid for stop in after.stops for lid in stop.lot_ids}


def test_increase_uses_real_capacity_and_routes_without_cancellation(refill_plan):
    plan_id, _, _ = refill_plan
    session = store.get(plan_id, OWNER)
    lot = session.scenario.lots[0]
    with database.begin() as db:
        row = _row(db, plan_id)
        result = build_quantity_increase(db, row, lot.id, lot.pallets + 2, NOW)
        assert isinstance(result.event, IncreaseEvent)
        assert result.event.type == "increase"
        assert result.diff.cancellation is None
        assert result.alternatives[0].plan.assignments[0].pallets == lot.pallets + 2
        assert result.alternatives[0].routes
        with pytest.raises(SessionConflict, match="용량"):
            build_quantity_increase(db, row, lot.id, 21, NOW)
        departure = session.routes[0].pickup.routes[0].earliest_departure
        with pytest.raises(SessionConflict, match="집하"):
            build_quantity_increase(db, row, lot.id, lot.pallets + 2, departure)


def test_increase_event_validation_keeps_reduce_semantics():
    scenario = get_scenario("default")
    lot = scenario.lots[0]
    after = apply_event(scenario, IncreaseEvent(lot_id=lot.id, new_pallets=lot.pallets + 1))
    assert after.lots[0].pallets == lot.pallets + 1
    with pytest.raises(ValueError, match="작아야"):
        apply_event(scenario, ReduceEvent(lot_id=lot.id, new_pallets=lot.pallets + 1))
    with pytest.raises(ValueError, match="커야"):
        apply_event(scenario, IncreaseEvent(lot_id=lot.id, new_pallets=lot.pallets - 1))


def test_rejected_partial_refill_releases_child_without_losing_inventory(refill_plan):
    plan_id, policy, vacancy = refill_plan
    with database.begin() as db:
        row = _row(db, plan_id)
        state = advance_offers(
            db, row, _waiting(db, row, policy, vacancy, 2), [vacancy], policy, NOW
        )
        state = respond_offer(
            db, row, state, [vacancy], policy, state.offers[0].offer_id, SHIPPER, True, 2, NOW
        )
    offer = state.offers[0]
    order = store.order(plan_id, offer.order_id, OWNER)
    if order.shipper_confirmations:
        body = ConfirmRequest(
            actor="shipper", shipper_id=order.shipper_confirmations[0].shipper_id, accept=False
        )
    else:
        body = ConfirmRequest(actor="forwarder", accept=False)
    store.confirm(plan_id, order.order_id, body, OWNER)
    with database.begin() as db:
        row = _row(db, plan_id)
        state, offsets = reconcile_refills(db, row, state, NOW)
        assert offsets == []
        assert state.offers[0].status == "failed"
        parent = db.get(LotRow, "spare-0")
        child = db.get(LotRow, offer.accepted_lot_id)
        assert parent.pallets + child.pallets == 5
        assert parent.status == child.status == "registered"
        state = advance_offers(db, row, state, [vacancy], policy, NOW)
        assert state.offers[-1].lot_id == "spare-1"


def test_one_pending_offer_per_container_pool(refill_plan):
    plan_id, policy, vacancy = refill_plan
    second = vacancy.model_copy(update={"vacancy_id": "other-cancellation", "quantity": 2})
    with database.begin() as db:
        row = _row(db, plan_id)
        state = _waiting(db, row, policy, vacancy, 3)
        state = advance_offers(db, row, state, [vacancy, second], policy, NOW)
        assert len(state.offers) == 1
        state = advance_offers(db, row, state, [vacancy, second], policy, NOW)
        assert len(state.offers) == 1
        with pytest.raises(SessionConflict, match="기한"):
            respond_offer(
                db,
                row,
                state,
                [vacancy, second],
                policy,
                state.offers[0].offer_id,
                SHIPPER,
                True,
                1,
                state.offers[0].expires_at,
            )


@pytest.mark.parametrize("forwarder_owned", [False, True])
def test_new_registered_identity_keeps_plan_and_execution_consent(refill_plan, forwarder_owned):
    plan_id, policy, vacancy = refill_plan
    session = store.get(plan_id, OWNER)
    original = session.scenario.lots[0]
    old_packhouse = session.scenario.packhouses[0]
    new_org = "new-refill-shipper"
    new_shipper = "new-refill-profile"
    new_lot = original.model_copy(
        update={"id": "new-refill-lot", "shipper_id": new_shipper, "pallets": 2}
    )
    with database.begin() as db:
        db.add(OrganizationRow(id=new_org, type="shipper", name="신규 화주"))
        db.add(OrganizationRow(id="refill-forwarder", type="forwarder", name="충원 포워더"))
        db.flush()
        db.add(
            PackhouseRow(
                id="new-refill-packhouse",
                org_id=new_org,
                name="신규 집하장",
                region=old_packhouse.region,
                lat=old_packhouse.lat,
                lon=old_packhouse.lon,
            )
        )
        db.flush()
        db.add(
            ShipperProfileRow(
                id=new_shipper,
                org_id=new_org,
                name="신규 화주",
                packhouse_id="new-refill-packhouse",
                product_ids=[original.product_id],
            )
        )
        db.add(lot_store._row(new_lot, session.scenario_id, None, new_org))
        if forwarder_owned:
            db.get(OrganizationRow, OWNER).type = "forwarder"
        db.add(
            PlanForwarderRow(
                plan_id=plan_id, forwarder_org_id=OWNER if forwarder_owned else "refill-forwarder"
            )
        )
    with database.begin() as db:
        row = _row(db, plan_id)
        state = join_waitlist(
            db, row, RefillState(), policy, vacancy.voyage_id, new_lot.id, new_org, 2, NOW
        )
        state = advance_offers(db, row, state, [vacancy], policy, NOW)
        assert len(state.offers) == 1
        state = respond_offer(
            db, row, state, [vacancy], policy, state.offers[0].offer_id, new_org, True, 2, NOW
        )
        confirmation = db.scalar(
            select(PlanConfirmationRow).where(
                PlanConfirmationRow.plan_id == plan_id,
                PlanConfirmationRow.shipper_id == new_shipper,
            )
        )
        assert confirmation.status == "pending"
        assert not confirmation.auto_accepted
        assert reconcile_refills(db, row, state, NOW)[1] == []
    order = store.order(plan_id, state.offers[0].order_id, OWNER)
    for confirmation in order.shipper_confirmations:
        store.confirm(
            plan_id,
            order.order_id,
            ConfirmRequest(actor="shipper", shipper_id=confirmation.shipper_id, accept=True),
            OWNER,
        )
    with pytest.raises(SessionConflict, match="모든 화주"):
        store.confirm(
            plan_id, order.order_id, ConfirmRequest(actor="forwarder", accept=True), OWNER
        )
    store.decide_confirmation(plan_id, new_org, True, shipper_id=new_shipper)
    store.confirm(plan_id, order.order_id, ConfirmRequest(actor="forwarder", accept=True), OWNER)
    with database.begin() as db:
        _, committed = reconcile_refills(db, _row(db, plan_id), state, NOW)
        assert len(committed) == 1
        assert committed[0].quantity == 2
