from fastapi.testclient import TestClient
from test_api_plans import FakeClient, fake_vessels  # noqa: F401
from test_saas_api import REDUCE, _platform_plan

from freshswitch_backend.api import plans as plans_api
from freshswitch_backend.integrations import VesselMovement
from freshswitch_backend.notifications.store import list_for_org
from freshswitch_backend.persistence.seed import shipper_org_id

LATE = VesselMovement(
    port_code="020",
    call_sign="ABC",
    vessel_name="FRESH HARMONY",
    departure_at="2026-11-19T06:00:00+09:00",
)


def _capture(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def record(org_id, voyages, movements, *, trusted, plan_id=None, max_gap_hours=0):
        calls.append({"org_id": org_id, "trusted": trusted, "plan_id": plan_id})
        return 0

    monkeypatch.setattr(plans_api, "record_schedule_check", record)
    return calls


def test_body_movements_are_never_trusted(platform_client: TestClient, saas_login, monkeypatch):
    calls = _capture(monkeypatch)
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    response = platform_client.post(
        f"/api/plans/{plan_id}/schedule-check",
        json={"movements": [LATE.model_dump(mode="json")]},
    )
    assert response.status_code == 200, response.text
    assert calls == [{"org_id": calls[0]["org_id"], "trusted": False, "plan_id": plan_id}]


def test_fetched_movements_are_trusted_and_demo_fixture_is_not(
    platform_client: TestClient,
    saas_login,
    monkeypatch,
    fake_vessels,  # noqa: F811
):
    calls = _capture(monkeypatch)
    fake_vessels(FakeClient([LATE]))
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    fetched = platform_client.post(f"/api/plans/{plan_id}/schedule-check")
    assert fetched.status_code == 200, fetched.text
    demo = platform_client.post(f"/api/plans/{plan_id}/schedule-check", params={"demo": "true"})
    assert demo.status_code == 200, demo.text
    assert [c["trusted"] for c in calls] == [True, False]


def test_apply_notifies_shippers_who_must_confirm(platform_client: TestClient, saas_login):
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    event = platform_client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE})
    assert event.status_code == 200, event.text
    applied = platform_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"}
    )
    assert applied.status_code == 200, applied.text
    order_id = applied.json()["active_order_id"]

    def order_notices():
        sent = list_for_org(shipper_org_id("sh-miryang"), plan_id=plan_id)
        return [n for n in sent if n.kind == "order_confirmation_requested"]

    assert [n.order_id for n in order_notices()] == [order_id]
    again = platform_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"}
    )
    assert again.status_code in (200, 409)
    assert len(order_notices()) == 1


def test_demo_apply_records_no_notifications(client: TestClient):
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE})
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert applied.status_code == 200, applied.text
    assert list_for_org("demo", plan_id=plan_id) == []
