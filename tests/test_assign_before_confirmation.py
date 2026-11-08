from sqlalchemy import update
from test_saas_resource_invariants import PLANS, _forwarder, _org, _platform, _registered, _resource

from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PlanRow


def _assigned_plan():
    owner = _forwarder()
    shipper, lot_id = _registered()
    resource = _resource(owner)
    response = owner.post(PLANS, json={"lot_ids": [lot_id]})
    assert response.status_code == 201, response.text
    return owner, shipper, lot_id, resource, response.json()


def test_forwarder_resource_is_assigned_while_shipper_confirmation_is_pending():
    owner, _, _, resource, plan = _assigned_plan()
    plan_id = plan["plan_id"]
    assert plan["status"] == "pending_shipper_confirmation"
    listed = owner.get(PLANS)
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["forwarder"]["org_id"] == _org(owner)
    resources = owner.get("/api/saas/forwarder/resources").json()
    assert (
        next(item for item in resources if item["id"] == resource["id"])["allocated_plan_id"]
        == plan_id
    )
    admin = _platform()
    blocked = admin.put(
        f"/api/saas/platform/plans/{plan_id}/forwarder",
        json={"forwarder_org_id": _org(owner)},
    )
    assert blocked.status_code == 403, blocked.text


def test_forwarder_assignment_does_not_bypass_shipper_execution_consent():
    owner, shipper, lot_id, _, plan = _assigned_plan()
    plan_id = plan["plan_id"]
    path = f"/api/plans/{plan_id}/settlement/arrival"
    body = {"lot_id": lot_id, "inspected_loss_pct": 2}
    blocked = owner.post(path, json=body)
    assert blocked.status_code == 409, blocked.text
    assert "모든 화주가 배정을 수락" in blocked.json()["detail"]
    with database.begin() as db:
        db.execute(update(PlanRow).where(PlanRow.plan_id == plan_id).values(status="committed"))
    assert owner.post(path, json=body).status_code == 409
    accepted = shipper.post(
        f"/api/saas/shipper/plans/{plan_id}/confirmation", json={"accept": True}
    )
    assert accepted.status_code == 200, accepted.text
    allowed = owner.post(path, json=body)
    assert allowed.status_code == 200, allowed.text
