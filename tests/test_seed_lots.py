from datetime import UTC, date, datetime

import pytest
from saas_fixtures import PLATFORM_ORG_ID
from sqlalchemy import func, select

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import (
    SeedError,
    main,
    run_lots,
    seed_lots,
    shipper_org_id,
)
from freshswitch_backend.persistence.tables import (
    LotRow,
    OrganizationRow,
    PackhouseRow,
    PlanRow,
    ShipperProfileRow,
)

STAMP = datetime(2026, 11, 10, tzinfo=UTC)


def _lots() -> dict[str, tuple]:
    with database.begin() as db:
        return {
            row.id: (
                row.scenario_id,
                row.shipper_id,
                row.shipper_org_id,
                row.product_id,
                row.pallets,
                row.ready_date,
                row.sell_by_date,
                row.sell_window,
                row.packhouse,
                row.status,
            )
            for row in db.scalars(select(LotRow))
        }


def _containers(session: dict) -> dict[str, int]:
    return {c["container_id"]: c["pallets"] for c in session["plan"]["containers"] if c["used"]}


def test_seed_lots_mirror_default_scenario_per_sandbox_shipper(demo_tenants) -> None:
    assert run_lots() == 6
    scenario = get_scenario("default")
    lots = _lots()
    assert set(lots) == {lot.id for lot in scenario.lots}
    shippers = {s.id: s for s in scenario.shippers}
    for lot in scenario.lots:
        assert lots[lot.id] == (
            "default",
            lot.shipper_id,
            shipper_org_id(lot.shipper_id),
            lot.product_id,
            lot.pallets,
            lot.ready_date,
            lot.sell_by_date,
            lot.sell_window,
            shippers[lot.shipper_id].packhouse_id,
            "registered",
        )


def test_seed_lots_is_idempotent(demo_tenants) -> None:
    with database.begin() as db:
        assert seed_lots(db, now=STAMP) == 6
    first = _lots()
    with database.begin() as db:
        assert seed_lots(db, now=STAMP) == 0
    assert _lots() == first
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(LotRow)) == 6


def test_seed_lots_needs_sandbox_orgs() -> None:
    assert run_lots() == 0
    assert _lots() == {}


def test_seed_lots_unknown_scenario() -> None:
    with pytest.raises(SeedError), database.begin() as db:
        seed_lots(db, "no-such-scenario")


def test_seed_lots_skip_ids_taken_by_real_org(demo_tenants) -> None:
    with database.begin() as db:
        db.add(OrganizationRow(id="shipper-real", type="shipper", name="실제 화주"))
        db.flush()
        db.add(
            PackhouseRow(
                id="ph-real", org_id="shipper-real", name="실제 선과장", region="x", lat=35, lon=128
            )
        )
        db.flush()
        db.add(
            ShipperProfileRow(
                id="sh-real", org_id="shipper-real", name="실제 화주", packhouse_id="ph-real"
            )
        )
        db.add(
            LotRow(
                id="lot-jinju-sb-1",
                scenario_id="default",
                shipper_id="sh-real",
                shipper_org_id="shipper-real",
                product_id="strawberry",
                pallets=3,
                ready_date=date(2026, 11, 16),
                destination="HKHKG",
                sell_by_date=date(2026, 11, 23),
                source="등록",
                status="planned",
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        db.add(
            PlanRow(
                plan_id="plan-real",
                org_id="shipper-real",
                scenario_id="default",
                version=1,
                status="planned",
                lot_count=1,
                totals={},
                created_at=STAMP,
                updated_at=STAMP,
                payload={"registered_lot_ids": ["lot-pohang-gr-1"]},
                committed={},
            )
        )

    assert run_lots() == 4
    lots = _lots()
    assert lots["lot-jinju-sb-1"][2] == "shipper-real"
    assert lots["lot-jinju-sb-1"][4] == 3
    assert lots["lot-jinju-sb-1"][9] == "planned"
    assert "lot-pohang-gr-1" not in lots
    assert {lots[i][2] for i in lots if i != "lot-jinju-sb-1"} <= {
        shipper_org_id(s.id) for s in get_scenario("default").shippers
    }


def test_default_seed_cli_includes_lots(capsys) -> None:
    main(["--password", "cli-demo-password"])
    printed = capsys.readouterr().out.split()
    assert "shipper=5" in printed
    assert "lots=6" in printed
    main(["--password", "cli-demo-password"])
    assert "lots=0" in capsys.readouterr().out.split()
    assert len(_lots()) == 6


def test_seed_lots_cli(demo_tenants, capsys) -> None:
    main(["lots", "--scenario", "default"])
    assert capsys.readouterr().out.strip() == "lots=6"
    main(["lots", "--scenario", "default"])
    assert capsys.readouterr().out.strip() == "lots=0"


def test_seeded_pool_plans_into_reference_fill(demo_tenants, platform_client) -> None:
    run_lots()
    pool = platform_client.get("/api/saas/platform/lots", params={"status": "registered"})
    assert pool.status_code == 200
    assert len(pool.json()) == 6

    created = platform_client.post("/api/saas/platform/plans", json={"include_registered": True})
    assert created.status_code == 201, created.text
    session = created.json()
    assert _containers(session) == {"CA-01": 17, "CA-02": 18}
    capacities = {c.id: c.capacity_pallets for c in get_scenario("default").containers}
    assert capacities["CA-01"] == capacities["CA-02"] == 20
    with database.begin() as db:
        assert db.get(PlanRow, session["plan_id"]).org_id == PLATFORM_ORG_ID
        statuses = set(db.scalars(select(LotRow.status)))
    assert statuses == {"planned"}


def test_seed_lots_switch_to_fuel_shock_pool(demo_tenants, platform_client) -> None:
    assert run_lots() == 6
    fuel = get_scenario("fuel-shock")
    assert run_lots(scenario_id="fuel-shock") == len(fuel.lots)
    lots = _lots()
    assert set(lots) == {lot.id for lot in fuel.lots}
    assert {row[0] for row in lots.values()} == {"fuel-shock"}
    assert run_lots(scenario_id="fuel-shock") == 0

    pool = platform_client.get("/api/saas/platform/lots", params={"scenario_id": "fuel-shock"})
    assert pool.status_code == 200
    assert len(pool.json()) == len(fuel.lots)
    created = platform_client.post(
        "/api/saas/platform/plans",
        json={"scenario_id": "fuel-shock", "include_registered": True},
    )
    assert created.status_code == 201, created.text
    with database.begin() as db:
        assert db.get(PlanRow, created.json()["plan_id"]).lot_count == len(fuel.lots)

    assert run_lots() == 0
    assert {row[0] for row in _lots().values()} == {"fuel-shock"}


def test_seed_lots_cli_fuel_shock_replaces_default_seed(demo_tenants, capsys) -> None:
    main(["lots", "--scenario", "default"])
    capsys.readouterr()
    main(["lots", "--scenario", "fuel-shock"])
    assert capsys.readouterr().out.strip() == f"lots={len(get_scenario('fuel-shock').lots)}"
    main(["lots", "--scenario", "default"])
    assert capsys.readouterr().out.strip() == "lots=6"
    lots = _lots()
    assert set(lots) == {lot.id for lot in get_scenario("default").lots}
    assert {row[0] for row in lots.values()} == {"default"}
