from fastapi.testclient import TestClient
from test_plan_confirm import REDUCE, _register
from test_r2_plan_state import _apply, _event, _reject_pohang
from test_r2_tenancy import _go_real

from freshswitch_backend.planning.store import APPLY_BEFORE_CONFIRMATION


def test_real_org_can_drop_rejecting_shipper_while_others_still_confirm(
    forwarder_client: TestClient, saas_login
) -> None:
    created = forwarder_client.post(
        "/api/saas/forwarder/plans", json={"lot_ids": _register(saas_login)}
    )
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    real = _go_real(saas_login)
    _reject_pohang(real, plan_id)

    other = _event(forwarder_client, plan_id, REDUCE)
    blocked = forwarder_client.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": other["recommended_id"], "replan_id": other["replan_id"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == APPLY_BEFORE_CONFIRMATION

    state = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/confirmation")
    assert state.status_code == 200, state.text
    assert state.json()["needs_replan"] is True
    candidate = next(
        item for item in state.json()["replan_candidates"] if item["source"] == "shipper_rejected"
    )
    applied = _apply(
        forwarder_client, plan_id, _event(forwarder_client, plan_id, candidate["events"][0])
    )
    assert all(lot["shipper_id"] != "sh-pohang" for lot in applied["scenario"]["lots"])
