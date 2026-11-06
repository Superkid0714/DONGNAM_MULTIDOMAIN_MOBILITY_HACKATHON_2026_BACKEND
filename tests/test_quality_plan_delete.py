import pytest
from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, PLATFORM_ORG_ID, accept_plan, seed_client
from sqlalchemy import select

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.execution.quality import ArrivalRequest
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.sandbox_reset import reset_sandbox
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import ObservationRow
from freshswitch_backend.planning.store import store
from freshswitch_backend.quality import quality_history
from freshswitch_backend.quality.store import ScopeRef

LOT = "lot-sancheong-sb-1"
CELL = ("strawberry", "HKHKG", "sea")


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _cell_n(client: TestClient) -> int:
    response = client.get("/api/quality/loss-rates")
    assert response.status_code == 200, response.text
    cell = next(
        c
        for c in response.json()["cells"]
        if (c["product_id"], c["destination"], c["mode"]) == CELL
    )
    return cell["observations"]


def _observation_ids(client: TestClient) -> set[str]:
    response = client.get("/api/quality/observations")
    assert response.status_code == 200, response.text
    return {o["id"] for o in response.json()["observations"]}


def _arrival(client: TestClient, plan_id: str, lot: str = LOT, pct: float = 40) -> None:
    response = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": lot, "inspected_loss_pct": pct},
    )
    assert response.status_code == 200, response.text


def _arrival_ids(scope: str, plan_id: str) -> set[str]:
    ref = ScopeRef(scope, "seed") if scope == "sandbox" else scope
    return {
        o.id
        for o in quality_history.observations(ref)
        if o.origin == "arrival" and o.plan_id == plan_id
    }


def _stored_arrivals(scope: str) -> list[str]:
    with database.begin() as db:
        return list(
            db.scalars(
                select(ObservationRow.id).where(
                    ObservationRow.scope == scope, ObservationRow.origin != "seed"
                )
            )
        )


def _lot_body(lot) -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "sell_window": lot.sell_window,
        "shipper": lot.shipper_id,
        "lot_id": lot.id,
    }


def _sandbox_plan(saas_app) -> tuple[TestClient, TestClient, str]:
    scenario = get_scenario("default")
    assert scenario is not None
    ids = []
    for lot in scenario.lots:
        shipper = seed_client(shipper_org_id(lot.shipper_id), saas_app)
        response = shipper.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    forwarder = seed_client(FORWARDER_ORG_ID, saas_app)
    created = forwarder.post("/api/saas/forwarder/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    accept_plan(lambda org_id: seed_client(org_id, saas_app), plan_id)
    return seed_client(PLATFORM_ORG_ID, saas_app), forwarder, plan_id


def test_demo_plan_delete_drops_its_arrival_observations(client: TestClient) -> None:
    n_before = _cell_n(client)
    ids_before = _observation_ids(client)
    kept = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _arrival(client, kept, "lot-jinju-sb-1", 9)
    n_kept = _cell_n(client)
    assert n_kept == n_before + 1

    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _arrival(client, plan_id)
    _arrival(client, plan_id, "lot-miryang-sb-1", 20)
    assert _cell_n(client) == n_kept + 2
    dropped = _arrival_ids("demo", plan_id)
    assert len(dropped) == 2

    assert client.delete(f"/api/plans/{plan_id}").status_code == 204
    assert _cell_n(client) == n_kept
    remaining = _observation_ids(client)
    assert not dropped & remaining
    assert ids_before < remaining
    assert len(_arrival_ids("demo", kept)) == 1
    seeds = [o for o in quality_history.observations() if o.origin == "seed"]
    assert len(seeds) == len(quality_history._seed)


def test_delete_of_missing_plan_keeps_observations(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _arrival(client, plan_id)
    before = _observation_ids(client)
    assert client.delete("/api/plans/no-such-plan").status_code == 404
    assert _observation_ids(client) == before


def test_arrival_racing_plan_delete_is_not_learned(client: TestClient, monkeypatch) -> None:
    n_before = _cell_n(client)
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    original = store.record_arrival_snapshot

    def interleaved(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        assert store.delete(plan_id)
        return snapshot

    monkeypatch.setattr(store, "record_arrival_snapshot", interleaved)
    _arrival(client, plan_id)
    assert _arrival_ids("demo", plan_id) == set()
    assert _cell_n(client) == n_before


def test_retention_trimmed_plan_drops_observations(client: TestClient, monkeypatch) -> None:
    n_before = _cell_n(client)
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _arrival(client, plan_id)
    assert _cell_n(client) == n_before + 1
    trimmed = _arrival_ids("demo", plan_id)
    monkeypatch.setattr(store, "_max", 1)
    assert client.post("/api/plans", json={"scenario_id": "default"}).status_code == 201
    assert client.get(f"/api/plans/{plan_id}").status_code == 404
    assert _arrival_ids("demo", plan_id) == set()
    assert not trimmed & _observation_ids(client)
    assert all(client.get(f"/api/quality/observations/{i}").status_code == 404 for i in trimmed)
    assert _cell_n(client) == n_before

    survivor = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _arrival(client, survivor, "lot-jinju-sb-1", 9)
    assert _stored_arrivals("demo") == list(_arrival_ids("demo", survivor))


def test_failed_cleanup_is_finished_by_retried_delete(client: TestClient, monkeypatch) -> None:
    n_before = _cell_n(client)
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _arrival(client, plan_id)
    original = quality_history.prune_orphans

    def broken() -> int:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(quality_history, "prune_orphans", broken)
    with pytest.raises(RuntimeError):
        client.delete(f"/api/plans/{plan_id}")
    assert _stored_arrivals("demo") == []
    assert _cell_n(client) == n_before
    assert _arrival_ids("demo", plan_id) == set()

    monkeypatch.setattr(quality_history, "prune_orphans", original)
    assert client.delete(f"/api/plans/{plan_id}").status_code == 404
    assert _stored_arrivals("demo") == []


def test_arrival_racing_demo_reset_leaves_no_row(
    client: TestClient, reset_admin_headers, monkeypatch
) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    result, snapshot = store.record_arrival_snapshot(
        plan_id, ArrivalRequest(lot_id=LOT, inspected_loss_pct=40)
    )
    original = quality_history.reset

    def reset_then_arrive(*args, **kwargs):
        cleared = original(*args, **kwargs)
        quality_history.record_arrival(plan_id, snapshot, result)
        return cleared

    monkeypatch.setattr(quality_history, "reset", reset_then_arrive)
    response = client.post("/api/demo/reset", headers=reset_admin_headers)
    assert response.status_code == 200, response.text
    assert _stored_arrivals("demo") == []
    seeds = [o for o in quality_history.observations() if o.origin == "seed"]
    assert len(seeds) == len(quality_history._seed)


def test_sandbox_plan_delete_drops_its_arrival_observations(saas_app, client: TestClient) -> None:
    demo_n = _cell_n(client)
    platform, forwarder, plan_id = _sandbox_plan(saas_app)
    n_before = _cell_n(platform)
    ids_before = _observation_ids(platform)
    _arrival(forwarder, plan_id)
    assert _cell_n(platform) == n_before + 1
    dropped = _arrival_ids("sandbox", plan_id)
    assert len(dropped) == 1 and dropped <= _observation_ids(platform)

    assert forwarder.delete(f"/api/plans/{plan_id}").status_code == 204
    assert _cell_n(platform) == n_before
    assert _observation_ids(platform) == ids_before
    assert _cell_n(client) == demo_n


def test_other_org_cannot_drop_observations_by_deleting(saas_app, client: TestClient) -> None:
    _, forwarder, plan_id = _sandbox_plan(saas_app)
    _arrival(forwarder, plan_id)
    assert client.delete(f"/api/plans/{plan_id}").status_code == 404
    assert len(_arrival_ids("sandbox", plan_id)) == 1


def test_sandbox_reset_drops_arrival_observations(saas_app) -> None:
    platform, forwarder, plan_id = _sandbox_plan(saas_app)
    n_before = _cell_n(platform)
    _arrival(forwarder, plan_id)
    assert _cell_n(platform) == n_before + 1

    reset_sandbox()
    platform = seed_client(PLATFORM_ORG_ID, saas_app)
    assert _cell_n(platform) == n_before
    assert _arrival_ids("sandbox", plan_id) == set()
    sandbox = quality_history.observations(ScopeRef("sandbox", "seed"))
    assert sorted(o.id for o in sandbox) == sorted(
        f"sandbox-{item.id}" for item in quality_history._seed
    )
