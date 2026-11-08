import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from freshswitch_backend.coldchain import ColdchainError, NotFound, coldchain_store
from freshswitch_backend.coldchain.models import BindingRequest, ReadingIn, SimulationRequest
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.lots import ANY_ORG, LotFilter, lot_store
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    ContainerRow,
    LotRow,
    OrganizationRow,
    PlanRow,
    RunRow,
)
from freshswitch_backend.planning.store import SessionStore, store
from freshswitch_backend.quality import QualityAccessDenied, quality_history

OTHER = "freshswitch"


@pytest.fixture(autouse=True)
def other_org() -> None:
    with database.begin() as db:
        db.execute(
            insert(OrganizationRow)
            .values(id=OTHER, type="platform", name="프레시스위치")
            .on_conflict_do_nothing(index_elements=[OrganizationRow.id])
        )


def _scenario():
    scenario = get_scenario("default")
    assert scenario is not None
    return scenario


def _register(lot_id: str, org_id: str):
    scenario = _scenario()
    lot = scenario.lots[0].model_copy(update={"id": lot_id})
    return lot_store.create(lambda _: (lot, scenario.id, None), org_id=org_id)


def _lot_status(lot_id: str) -> str:
    with database.begin() as db:
        return db.scalar(select(LotRow.status).where(LotRow.id == lot_id))


def test_plan_rows_record_org_and_lookups_stay_inside_org() -> None:
    demo_plan = store.create(_scenario())
    other_plan = store.create(_scenario(), org_id=OTHER)
    with database.begin() as db:
        orgs = dict(db.execute(select(PlanRow.plan_id, PlanRow.org_id)).all())
    assert orgs == {demo_plan.plan_id: DEMO_ORG, other_plan.plan_id: OTHER}

    assert store.get(other_plan.plan_id) is None
    assert store.get(demo_plan.plan_id, org_id=OTHER) is None
    assert store.get(other_plan.plan_id, org_id=OTHER).plan_id == other_plan.plan_id
    assert store.get(other_plan.plan_id, org_id=ANY_ORG).plan_id == other_plan.plan_id
    assert store.org_of(other_plan.plan_id) == OTHER

    assert [s.plan_id for s in store.summaries()] == [demo_plan.plan_id]
    assert [s.plan_id for s in store.summaries(org_id=OTHER)] == [other_plan.plan_id]
    assert store.count() == 1
    assert store.count(OTHER) == 1
    assert store.count(ANY_ORG) == 2

    assert store.orders(other_plan.plan_id) == []
    with pytest.raises(KeyError):
        store.order(other_plan.plan_id, "missing")
    assert store.idempotent(other_plan.plan_id, "k", "apply", "f") is None

    assert store.delete(other_plan.plan_id) is False
    assert store.get(other_plan.plan_id, org_id=OTHER) is not None
    assert store.delete(other_plan.plan_id, org_id=OTHER) is True
    assert store.get(demo_plan.plan_id) is not None


def test_plan_trim_counts_each_org_separately() -> None:
    small = SessionStore(max_sessions=2)
    small.clock = store.clock
    demo_plan = small.create(_scenario())
    other_ids = [small.create(_scenario(), org_id=OTHER).plan_id for _ in range(3)]
    assert small.get(demo_plan.plan_id) is not None
    assert [s.plan_id for s in small.summaries(org_id=OTHER)] == other_ids[:0:-1]


def test_lot_store_is_scoped_by_shipper_org() -> None:
    _register("LOT-DEMO-1", DEMO_ORG)
    _register("LOT-OTHER-1", OTHER)
    with database.begin() as db:
        orgs = dict(db.execute(select(LotRow.id, LotRow.shipper_org_id)).all())
    assert orgs == {"LOT-DEMO-1": DEMO_ORG, "LOT-OTHER-1": OTHER}

    assert lot_store.get("LOT-OTHER-1") is None
    assert lot_store.get("LOT-OTHER-1", org_id=OTHER).id == "LOT-OTHER-1"
    assert lot_store.org_of("LOT-OTHER-1") == OTHER
    assert [x.id for x in lot_store.get_many(["LOT-DEMO-1", "LOT-OTHER-1"])] == ["LOT-DEMO-1"]
    assert [x.id for x in lot_store.search(LotFilter())] == ["LOT-DEMO-1"]
    assert [x.id for x in lot_store.search(LotFilter(), org_id=OTHER)] == ["LOT-OTHER-1"]
    assert lot_store.ids() == {"LOT-DEMO-1"}
    assert lot_store.ids(ANY_ORG) == {"LOT-DEMO-1", "LOT-OTHER-1"}
    assert lot_store.reservation().taken == {"LOT-DEMO-1", "LOT-OTHER-1"}

    assert lot_store.update("LOT-OTHER-1", lambda _: {"pallets": 1}) is None
    assert lot_store.update("LOT-OTHER-1", lambda _: {"pallets": 1}, org_id=OTHER).pallets == 1
    assert lot_store.delete("LOT-OTHER-1") is False
    assert lot_store.clear() == 1
    assert lot_store.get("LOT-OTHER-1", org_id=OTHER) is not None
    assert lot_store.delete("LOT-OTHER-1", org_id=OTHER) is True


def test_coldchain_records_without_plan_use_org_key() -> None:
    demo_run = coldchain_store.simulate(SimulationRequest(preset="short_outage"))
    other_run = coldchain_store.simulate(SimulationRequest(preset="normal"), org_id=OTHER)
    with database.begin() as db:
        keys = dict(db.execute(select(ContainerRow.record_key, ContainerRow.org_id)).all())
        runs = dict(db.execute(select(RunRow.run_id, RunRow.org_id)).all())
    assert keys == {"-:CA-01": DEMO_ORG, f"-{OTHER}:CA-01": OTHER}
    assert runs == {demo_run.run_id: DEMO_ORG, other_run.run_id: OTHER}

    assert len(coldchain_store.summaries()) == 1
    assert len(coldchain_store.summaries(org_id=OTHER)) == 1
    assert coldchain_store.state("CA-01").reading_count == demo_run.total
    assert coldchain_store.state("CA-01", org_id=OTHER).reading_count == other_run.total
    demo_alerts = coldchain_store.alerts().alerts
    assert demo_alerts
    assert coldchain_store.alerts(org_id=OTHER).alerts == []

    with pytest.raises(NotFound):
        coldchain_store.run(other_run.run_id)
    assert coldchain_store.run(other_run.run_id, org_id=OTHER).done is True

    coldchain_store.reset_container("CA-01", org_id=OTHER)
    assert coldchain_store.summaries(org_id=OTHER) == []
    assert len(coldchain_store.summaries()) == 1


def test_coldchain_plan_lookup_passes_org() -> None:
    demo_plan = store.create(_scenario())
    other_plan = store.create(_scenario(), org_id=OTHER)

    with pytest.raises(NotFound):
        coldchain_store.simulate(SimulationRequest(plan_id=other_plan.plan_id))
    with pytest.raises(NotFound):
        coldchain_store.simulate(SimulationRequest(plan_id=demo_plan.plan_id), org_id=OTHER)

    run = coldchain_store.simulate(SimulationRequest(plan_id=other_plan.plan_id), org_id=OTHER)
    assert run.plan_id == other_plan.plan_id
    state = coldchain_store.state("CA-01", other_plan.plan_id, org_id=OTHER)
    assert state.plan_id == other_plan.plan_id
    assert state.loss.delta_krw > 0
    preview = coldchain_store.settlement_preview("CA-01", other_plan.plan_id, org_id=OTHER)
    assert preview.plan_id == other_plan.plan_id
    with pytest.raises(NotFound):
        coldchain_store.state("CA-01", other_plan.plan_id)
    assert coldchain_store.alerts(plan_id=other_plan.plan_id).alerts == []
    assert coldchain_store.alerts(plan_id=other_plan.plan_id, org_id=OTHER).alerts


def test_coldchain_bindings_are_scoped() -> None:
    coldchain_store.bind(BindingRequest(device_id="dev-other", container_id="CA-02"), OTHER)
    assert coldchain_store.bindings() == []
    assert [b.device_id for b in coldchain_store.bindings(OTHER)] == ["dev-other"]
    with pytest.raises(ColdchainError):
        coldchain_store.bind(BindingRequest(device_id="dev-other", container_id="CA-01"))

    result = coldchain_store.ingest([ReadingIn(device_id="dev-other", temp_c=1.0, power_on=True)])
    assert result.accepted == 0
    assert len(result.rejected) == 1
    accepted = coldchain_store.ingest(
        [ReadingIn(device_id="dev-other", temp_c=1.0, power_on=True)], org_id=OTHER
    )
    assert accepted.accepted == 1
    assert accepted.containers[0].container_id == "CA-02"


def test_demo_reset_only_clears_demo_org(client: TestClient, reset_admin_headers) -> None:
    demo_plan = store.create(_scenario())
    other_plan = store.create(_scenario(), org_id=OTHER)
    _register("LOT-DEMO-1", DEMO_ORG)
    _register("LOT-OTHER-1", OTHER)
    _register("LOT-OTHER-2", OTHER)
    with database.begin() as db:
        db.execute(update(LotRow).where(LotRow.id == "LOT-OTHER-2").values(status="planned"))
    coldchain_store.simulate(SimulationRequest(preset="normal"))
    coldchain_store.simulate(SimulationRequest(preset="normal"), org_id=OTHER)
    coldchain_store.bind(BindingRequest(device_id="dev-demo", container_id="CA-01"))
    coldchain_store.bind(BindingRequest(device_id="dev-other", container_id="CA-01"), OTHER)

    body = client.post(
        "/api/demo/reset", params={"include_lots": True}, headers=reset_admin_headers
    ).json()
    assert body["cleared_sessions"] == 1
    assert body["cleared_coldchain_containers"] == 1
    assert body["cleared_lots"] == 1

    assert store.get(demo_plan.plan_id) is None
    assert store.get(other_plan.plan_id, org_id=OTHER) is not None
    assert lot_store.ids(ANY_ORG) == {"LOT-OTHER-1", "LOT-OTHER-2"}
    assert _lot_status("LOT-OTHER-2") == "planned"
    assert coldchain_store.summaries() == []
    assert len(coldchain_store.summaries(org_id=OTHER)) == 1
    assert coldchain_store.bindings() == []
    assert [b.device_id for b in coldchain_store.bindings(OTHER)] == ["dev-other"]
    with database.begin() as db:
        assert set(db.scalars(select(RunRow.org_id))) == {OTHER}


def test_plan_clear_releases_only_own_org_lots() -> None:
    _register("LOT-DEMO-1", DEMO_ORG)
    _register("LOT-OTHER-1", OTHER)
    with database.begin() as db:
        db.execute(update(LotRow).values(status="planned"))
    store.clear(OTHER)
    assert _lot_status("LOT-OTHER-1") == "registered"
    assert _lot_status("LOT-DEMO-1") == "planned"
    store.clear()
    assert _lot_status("LOT-DEMO-1") == "registered"


def test_plan_create_marks_only_lots_of_lot_org() -> None:
    _register("LOT-DEMO-1", DEMO_ORG)
    _register("LOT-OTHER-1", OTHER)
    ids = ["LOT-DEMO-1", "LOT-OTHER-1"]
    store.create(_scenario(), registered_lot_ids=ids)
    assert _lot_status("LOT-DEMO-1") == "planned"
    assert _lot_status("LOT-OTHER-1") == "registered"
    store.create(_scenario(), registered_lot_ids=["LOT-OTHER-1"], org_id=DEMO_ORG, lot_org_id=OTHER)
    assert _lot_status("LOT-OTHER-1") == "planned"


def test_none_org_is_not_all_orgs() -> None:
    _register("LOT-OTHER-1", OTHER)
    other_plan = store.create(_scenario(), org_id=OTHER)
    assert store.get(other_plan.plan_id, org_id=None) is None
    assert store.count(None) == 0
    assert lot_store.get("LOT-OTHER-1", org_id=None) is None
    assert lot_store.ids(None) == set()


def test_quality_reset_accepts_caller_org_type() -> None:
    with pytest.raises(QualityAccessDenied):
        quality_history.reset(org_type="shipper")
    with pytest.raises(QualityAccessDenied):
        quality_history.delete("missing", org_type="demo")
    _, seeded = quality_history.reset(org_type="platform")
    assert seeded > 0
    assert quality_history.reset()[0] == seeded
