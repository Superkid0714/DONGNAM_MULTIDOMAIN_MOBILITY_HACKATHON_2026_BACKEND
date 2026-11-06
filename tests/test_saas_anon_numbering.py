from fastapi.testclient import TestClient
from test_saas_api import _lot_body, _scenario

from freshswitch_backend.persistence.seed import shipper_org_id

PARTICIPANTS = ("sh-jinju", "sh-sancheong", "sh-sangju")


def test_anonymous_numbers_count_only_plan_participants(
    forwarder_client: TestClient, saas_login
) -> None:
    ids = []
    for lot in _scenario().lots:
        if lot.shipper_id not in PARTICIPANTS:
            continue
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    created = forwarder_client.post("/api/saas/forwarder/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]

    sancheong = saas_login(shipper_org_id("sh-sancheong"))
    plan = sancheong.get(f"/api/saas/shipper/plans/{plan_id}").json()["plan"]
    others = {a["shipper_id"] for a in plan["assignments"] if a["shipper_id"] != "sh-sancheong"}
    assert others == {"anon-1", "anon-2"}
