from datetime import date

import pytest

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Lot, Plan, Scenario
from freshswitch_backend.optimize import AIR, products_compatible, settle, solve_plan

STRAWBERRY_CA01 = {"lot-jinju-sb-1", "lot-sancheong-sb-1", "lot-miryang-sb-1"}
GRAPE_LOTS = {"lot-sangju-gr-1", "lot-pohang-gr-1"}


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def default_plan(scenario: Scenario) -> Plan:
    return solve_plan(scenario)


def _container(plan: Plan, container_id: str):
    return next(c for c in plan.containers if c.container_id == container_id)


def _where(plan: Plan) -> dict[str, str | None]:
    return {a.lot_id: a.container_id or AIR for a in plan.assignments}


def _with_lots(scenario: Scenario, lots: list[Lot], **update) -> Scenario:
    return scenario.model_copy(update={"lots": lots, **update})


def test_default_plan_packs_strawberry_into_ca01(default_plan: Plan) -> None:
    assert default_plan.status == "OPTIMAL"
    ca01 = _container(default_plan, "CA-01")
    assert set(ca01.lot_ids) == STRAWBERRY_CA01
    assert ca01.pallets == 17
    assert ca01.utilization == pytest.approx(0.85)


def test_default_plan_puts_grapes_on_their_own_container(default_plan: Plan) -> None:
    ca02 = _container(default_plan, "CA-02")
    assert set(ca02.lot_ids) == GRAPE_LOTS
    assert ca02.pallets == 18
    assert ca02.product_ids == ["grape_so2"]


def test_default_plan_sends_late_miryang_lot_by_air(default_plan: Plan) -> None:
    assert _where(default_plan)["lot-miryang-sb-2"] == AIR
    assert not default_plan.unassigned
    assert not _container(default_plan, "CA-03").used


def test_default_plan_costs_add_up(default_plan: Plan) -> None:
    parts = ("freight", "inland", "handling", "collection", "storage", "loss_risk")
    for cost in default_plan.shipper_costs:
        for breakdown in (cost.plan, cost.baseline, cost.operational):
            assert sum(breakdown.model_dump(include=set(parts)).values()) == breakdown.total
    ca01 = _container(default_plan, "CA-01")
    jinju = next(c for c in default_plan.shipper_costs if c.shipper_id == "sh-jinju")
    share = jinju.operational.freight + jinju.operational.handling
    assert share == pytest.approx(ca01.container_cost * 8 / 17, abs=2)
    assert jinju.plan.model_dump(include=set(parts)) == {
        "freight": 8 * 166_000,
        "inland": 8 * 435_000,
        "handling": 0,
        "collection": 0,
        "storage": 0,
        "loss_risk": 8 * 119_000,
    }
    assert jinju.baseline.total == 8 * 969_000


def test_every_shipper_saves_and_fee_follows_billing_rule(default_plan: Plan) -> None:
    rules = {s.billing_rule for s in default_plan.settlements}
    assert rules == {"performance", "usage_fee"}
    for s in default_plan.settlements:
        assert s.savings == s.baseline_cost - s.shared_cost
        assert s.savings > 0
        if s.billing_rule == "performance":
            assert s.fee == s.performance_fee == round(s.savings * 0.25)
            assert s.usage_fee == 0
        else:
            assert s.fee == s.usage_fee > 0
            assert s.performance_fee == 0
            assert s.baseline_kind == "sea_solo"
        assert s.fee + s.shipper_gain == s.savings
    assert default_plan.total_fee == sum(s.fee for s in default_plan.settlements)


def test_settle_math() -> None:
    assert settle(1_000_000, 0.25) == (250_000, 750_000)
    assert settle(0, 0.25) == (0, 0)
    assert settle(-50_000, 0.25) == (0, -50_000)


def test_compatibility_rule(scenario: Scenario) -> None:
    products = {p.id: p for p in scenario.products}
    strawberry, grape = products["strawberry"], products["grape_so2"]
    plain_grape = grape.model_copy(update={"id": "grape_plain", "so2_treated": False})
    assert products_compatible(strawberry, strawberry)
    assert not products_compatible(strawberry, grape)
    assert not products_compatible(grape, plain_grape)
    other_ca = strawberry.model_copy(update={"id": "sb2", "temp_min_c": 1.0, "temp_max_c": 3.0})
    assert products_compatible(strawberry, other_ca)
    assert not products_compatible(strawberry, other_ca.model_copy(update={"co2_pct": 12.0}))


def test_so2_lots_never_share_a_container(scenario: Scenario) -> None:
    grape = next(p for p in scenario.products if p.id == "grape_so2")
    plain = grape.model_copy(update={"id": "grape_plain", "name": "포도", "so2_treated": False})
    lots = [
        Lot(
            id="so2",
            shipper_id="sh-sangju",
            product_id="grape_so2",
            pallets=8,
            ready_date=date(2026, 11, 15),
            destination="HKHKG",
            sell_by_date=date(2026, 12, 30),
        ),
        Lot(
            id="plain",
            shipper_id="sh-pohang",
            product_id="grape_plain",
            pallets=8,
            ready_date=date(2026, 11, 15),
            destination="HKHKG",
            sell_by_date=date(2026, 12, 30),
        ),
    ]
    crafted = _with_lots(
        scenario,
        lots,
        products=[*scenario.products, plain],
        containers=[c for c in scenario.containers if c.id == "CA-01"],
    )
    plan = solve_plan(crafted, allow_air=False, require_savings=False)
    on_ca01 = set(_container(plan, "CA-01").lot_ids)
    assert len(on_ca01) == 1
    assert len(plan.unassigned) == 1

    merged = crafted.model_copy(
        update={"products": [*scenario.products, plain.model_copy(update={"so2_treated": True})]}
    )
    plan = solve_plan(merged, allow_air=False, require_savings=False)
    assert set(_container(plan, "CA-01").lot_ids) == {"so2", "plain"}


def test_shelf_life_rejects_sea_and_falls_back(scenario: Scenario) -> None:
    short = Lot(
        id="short",
        shipper_id="sh-jinju",
        product_id="strawberry",
        pallets=4,
        ready_date=date(2026, 11, 16),
        destination="HKHKG",
        sell_by_date=date(2026, 11, 19),
    )
    crafted = _with_lots(scenario, [short])
    plan = solve_plan(crafted)
    assert _where(plan)["short"] == AIR

    plan = solve_plan(crafted, allow_air=False)
    assert not plan.assignments
    assert plan.unassigned[0].lot_id == "short"
    assert "판매기한" in plan.unassigned[0].reason


def test_fixed_assignment_is_respected(scenario: Scenario) -> None:
    plan = solve_plan(scenario, fixed={"lot-sangju-gr-1": "CA-03"})
    where = _where(plan)
    assert where["lot-sangju-gr-1"] == "CA-03"
    assert where["lot-pohang-gr-1"] == "CA-03"
    assert set(_container(plan, "CA-01").lot_ids) == STRAWBERRY_CA01


def test_fixed_air_and_forbidden(scenario: Scenario) -> None:
    plan = solve_plan(
        scenario,
        fixed={"lot-pohang-gr-1": AIR},
        forbidden=[("lot-jinju-sb-1", "CA-01")],
    )
    where = _where(plan)
    assert where["lot-pohang-gr-1"] == AIR
    assert where["lot-jinju-sb-1"] != "CA-01"


def test_cancellation_pulls_next_miryang_lot(scenario: Scenario) -> None:
    lots = [
        lot.model_copy(update={"pallets": 3}) if lot.id == "lot-sancheong-sb-1" else lot
        for lot in scenario.lots
    ]
    crafted = _with_lots(scenario, lots)
    without_pull = solve_plan(crafted)
    assert _container(without_pull, "CA-01").pallets == 15

    plan = solve_plan(crafted, max_pull_forward_days=1)
    ca01 = _container(plan, "CA-01")
    assert ca01.pallets == 17
    assert "lot-miryang-sb-2" in ca01.lot_ids
    pulled = next(a for a in plan.assignments if a.lot_id == "lot-miryang-sb-2")
    assert pulled.pull_forward_days == 1


def test_excluded_lot_is_dropped(scenario: Scenario) -> None:
    plan = solve_plan(scenario, exclude_lot_ids=["lot-pohang-gr-1"])
    ids = {a.lot_id for a in plan.assignments} | {u.lot_id for u in plan.unassigned}
    assert "lot-pohang-gr-1" not in ids
    assert set(_container(plan, "CA-02").lot_ids) == {"lot-sangju-gr-1"}


def test_solver_is_deterministic(scenario: Scenario, default_plan: Plan) -> None:
    again = solve_plan(scenario)
    assert again.objective == default_plan.objective
    assert _where(again) == _where(default_plan)
