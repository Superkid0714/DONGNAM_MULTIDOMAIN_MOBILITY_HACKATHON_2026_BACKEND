from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from freshswitch_backend.api import settlement
from freshswitch_backend.api.auth import current_org_id, current_principal
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Plan
from freshswitch_backend.planning.service import build_plan


def test_settlement_legacy_fields_unchanged_when_robust_added(monkeypatch):
    scenario = get_scenario("default").model_copy(deep=True)
    scenario.lots = scenario.lots[:3]
    for voyage in scenario.voyages:
        voyage.delay_p90_h = 0
    plan = build_plan(scenario).plan
    session = SimpleNamespace(scenario=scenario, plan=plan, version=1, arrival_inspections=[])
    monkeypatch.setattr(settlement.store, "get", lambda *args, **kwargs: session)
    app = FastAPI()
    app.include_router(settlement.router)
    app.dependency_overrides[current_principal] = lambda: None
    app.dependency_overrides[current_org_id] = lambda: "test-org"
    client = TestClient(app)
    before = client.get("/plans/p/settlement")
    assert before.status_code == 200
    body = before.json()
    assert "robust_savings" not in body
    assert "simulation" not in body
    new = body
    for key in ("settlements", "inspections", "containers", "tariffs", "assumed_items"):
        assert isinstance(new[key], list)
    for key in ("plan_id", "version", "total_fee", "total_savings", "shared_cost", "fee"):
        assert key in new


def test_old_plan_payload_loads_without_robust_fields():
    scenario = get_scenario("default").model_copy(deep=True)
    scenario.lots = scenario.lots[:1]
    payload = build_plan(scenario).plan.model_dump(mode="json")
    assert "robust_savings" not in payload
    restored = Plan.model_validate(payload)
    assert "robust_savings" not in restored.model_dump()
    assert {
        k: v for k, v in restored.model_dump(mode="json").items() if k != "robust_savings"
    } == payload
