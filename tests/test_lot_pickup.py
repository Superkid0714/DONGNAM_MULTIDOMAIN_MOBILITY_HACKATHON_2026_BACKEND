import pytest
from test_onboarding import PACKHOUSE, _lot, _onboarded

from freshswitch_backend.api.plans import CreatePlanRequest, tenant_plan_scenario
from freshswitch_backend.api.saas_views import Anonymizer
from freshswitch_backend.intake.tenants import load_catalog
from freshswitch_backend.lots import lot_store
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import OrganizationRow
from freshswitch_backend.routing import build_pickup_request


def second_site(client):
    response = client.post(
        "/api/saas/shipper/packhouses",
        json={**PACKHOUSE, "name": "다른 출하 장소", "region": "부산", "lat": 35.15, "lon": 129.05},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_selected_pickup_preview_register_and_edits_preserve_default_and_old_lots():
    client, default = _onboarded("pickup-owner@example.com", "집하 업체")
    selected = second_site(client)
    before = client.get("/api/saas/shipper/profile").json()
    old = client.post("/api/saas/shipper/lots", json=_lot()).json()["registered"]
    shipper_id = before["profile"]["shipper_id"]
    payload = _lot(packhouse_id=selected["packhouse_id"])
    preview = client.post("/api/intake/lots", json={**payload, "shipper": shipper_id})
    assert preview.status_code == 200, preview.text
    assert preview.json()["resolved"]["packhouse_id"] == selected["packhouse_id"]
    response = client.post("/api/saas/shipper/lots", json=payload)
    assert response.status_code == 201, response.text
    registered = response.json()["registered"]
    assert registered["packhouse"] == selected["packhouse_id"]
    assert response.json()["lot"]["packhouse_id"] == selected["packhouse_id"]
    assert client.get("/api/saas/shipper/profile").json()["profile"] == before["profile"]
    lots = {lot["id"]: lot for lot in client.get("/api/saas/shipper/lots").json()}
    assert lots[old["id"]]["packhouse"] == default["packhouse_id"]
    assert lots[registered["id"]]["packhouse"] == selected["packhouse_id"]
    changed = client.patch(
        f"/api/saas/shipper/lots/{registered['id']}", json={"destination": "HKHKG"}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["packhouse"] == selected["packhouse_id"]
    following = client.post("/api/saas/shipper/lots", json=_lot(packhouse_id=None))
    assert following.status_code == 201, following.text
    assert following.json()["registered"]["packhouse"] == default["packhouse_id"]


@pytest.mark.parametrize("invalid", ["foreign", "missing", "", "bad\x00id"])
def test_preview_and_create_reject_unowned_or_invalid_pickup_without_saving(invalid):
    client, _ = _onboarded("pickup-owner@example.com", "집하 업체")
    _, foreign = _onboarded("pickup-other@example.com", "다른 업체")
    site_id = foreign["packhouse_id"] if invalid == "foreign" else invalid
    before = client.get("/api/saas/shipper/profile").json()
    payload = _lot(packhouse_id=site_id)
    response = client.post(
        "/api/intake/lots",
        json={**payload, "shipper": before["profile"]["shipper_id"], "save": True},
    )
    assert response.status_code == 422, response.text
    response = client.post("/api/saas/shipper/lots", json=payload)
    assert response.status_code == 422, response.text
    assert client.get("/api/saas/shipper/lots").json() == []
    assert client.get("/api/saas/shipper/profile").json() == before


def test_plan_routing_and_cost_use_each_lots_selected_coordinates():
    client, default = _onboarded("pickup-routing@example.com", "경로 업체")
    selected = second_site(client)
    me = client.get("/api/me").json()
    org_id = me["active_org"]["org_id"]
    with database.begin() as db:
        db.add(OrganizationRow(id="pickup-platform", type="platform", name="운영 업체"))
    first = client.post("/api/saas/shipper/lots", json=_lot(pallets=1)).json()["registered"]
    second = client.post(
        "/api/saas/shipper/lots", json=_lot(pallets=1, packhouse_id=selected["packhouse_id"])
    ).json()["registered"]
    scenario, ids = tenant_plan_scenario(
        CreatePlanRequest(lot_ids=[first["id"], second["id"]]), "pickup-platform"
    )
    request = build_pickup_request(scenario, ids, "V-2611A")
    stops = {stop.packhouse_id: stop for stop in request.stops}
    assert set(stops) == {default["packhouse_id"], selected["packhouse_id"]}
    assert stops[selected["packhouse_id"]].lot_ids == [second["id"]]
    assert (stops[selected["packhouse_id"]].lat, stops[selected["packhouse_id"]].lon) == (
        35.15,
        129.05,
    )
    model = CostModel(scenario)
    voyage = next(v for v in scenario.voyages if v.id == "V-2611A")
    lots = {lot.id: lot for lot in scenario.lots}
    assert model.collection_km(lots[first["id"]], voyage) != model.collection_km(
        lots[second["id"]], voyage
    )
    assert (
        lot_store.get_many([second["id"]], org_id=org_id)[0].to_lot().packhouse_id
        == selected["packhouse_id"]
    )
    assert {p.id for p in load_catalog(org_id).packhouses} == set(stops)
    anonymizer = Anonymizer(scenario, [])
    assert anonymizer.text(selected["packhouse_id"]) != selected["packhouse_id"]
    assert anonymizer.text(selected["name"]) != selected["name"]
