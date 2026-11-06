from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID
from test_plan_confirm import _lot_body, _plan
from test_r2_plan_state import (
    _accept_others,
    _apply,
    _confirm_order,
    _event,
    _reject_pohang,
    _rejected_candidate,
)
from test_r2_tenancy import _accept_all, _go_real
from test_r2_tenancy import _assign as _assign_real
from test_saas_api import _scenario

from freshswitch_backend.persistence.seed import shipper_org_id


def _parties(payload: dict) -> tuple[set[str], set[str], set[str]]:
    scenario = payload["scenario"]
    owners = {lot["shipper_id"] for lot in scenario["lots"]}
    return (
        owners,
        {s["id"] for s in scenario["shippers"]},
        {p["id"] for p in scenario["packhouses"]},
    )


def test_forwarder_and_shipper_see_only_shippers_in_the_plan(
    forwarder_client: TestClient, platform_client: TestClient, saas_login
) -> None:
    pohang = next(lot for lot in _scenario().lots if lot.shipper_id == "sh-pohang")
    registered = saas_login(shipper_org_id("sh-pohang")).post(
        "/api/saas/shipper/lots", json=_lot_body(pohang)
    )
    assert registered.status_code == 201, registered.text
    plan_id = _plan(forwarder_client, saas_login, skip=("sh-pohang",))["plan_id"]
    real = _go_real(saas_login)
    _accept_all(real, real(FORWARDER_ORG_ID), plan_id)
    assert _assign_real(platform_client, plan_id).status_code == 403

    full = platform_client.get(f"/api/plans/{plan_id}").json()
    assert "sh-pohang" in {s["id"] for s in full["scenario"]["shippers"]}

    seen = real(FORWARDER_ORG_ID).get(f"/api/saas/forwarder/plans/{plan_id}")
    assert seen.status_code == 200, seen.text
    owners, shippers, packhouses = _parties(seen.json())
    assert "sh-pohang" not in owners
    assert shippers == owners
    sites = {s["packhouse_id"] for s in seen.json()["scenario"]["shippers"]}
    assert packhouses == sites
    assert "포항" not in seen.text

    jinju = real(shipper_org_id("sh-jinju")).get(f"/api/saas/shipper/plans/{plan_id}")
    assert jinju.status_code == 200, jinju.text
    assert len(jinju.json()["scenario"]["shippers"]) == len(owners)
    assert "포항" not in jinju.text


def test_rejected_shipper_dropped_by_cancel_candidate_loses_plan_access(
    forwarder_client: TestClient, saas_login
) -> None:
    plan_id = _plan(forwarder_client, saas_login)["plan_id"]
    _reject_pohang(saas_login, plan_id)
    _accept_others(saas_login, forwarder_client, plan_id)
    candidate = _rejected_candidate(forwarder_client, plan_id)
    applied = _apply(
        forwarder_client, plan_id, _event(forwarder_client, plan_id, candidate["events"][0])
    )
    _confirm_order(
        forwarder_client, saas_login, forwarder_client, plan_id, applied["active_order_id"]
    )

    pohang = saas_login(shipper_org_id("sh-pohang"))
    assert pohang.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 404
    listed = pohang.get("/api/saas/shipper/plans").json()
    assert plan_id not in {p["plan_id"] for p in listed}
    assert pohang.get(f"/api/saas/shipper/plans/{plan_id}/orders").status_code in (200, 404)
    orders = pohang.get(f"/api/saas/shipper/plans/{plan_id}/orders")
    if orders.status_code == 200:
        assert orders.json() == []
    lots = pohang.get("/api/saas/shipper/lots").json()
    assert {lot["status"] for lot in lots} == {"cancelled"}

    jinju = saas_login(shipper_org_id("sh-jinju"))
    assert jinju.get(f"/api/saas/shipper/plans/{plan_id}").status_code == 200
