import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from freshswitch_backend.config import get_settings
from freshswitch_backend.main import app, create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import MembershipRow, OrganizationRow, ShipperProfileRow

CSRF = {"X-Requested-With": "freshswitch"}
PATH = "/api/saas/org"


def signup(email: str, org_type: str = "shipper") -> tuple[TestClient, dict]:
    client = TestClient(app, base_url="https://testserver", headers=CSRF)
    response = client.post(
        "/api/auth/signup",
        json={
            "email": email,
            "password": "company-test-password",
            "name": "개인 이름",
            "org_name": "원래 업체",
            "org_type": org_type,
        },
    )
    assert response.status_code == 201, response.text
    return client, response.json()


@pytest.mark.parametrize("org_type", ["shipper", "forwarder"])
def test_rename_persists_for_current_company_only(org_type):
    client, before = signup("owner@example.com", org_type)
    other, other_before = signup("other@example.com", org_type)
    org_id = before["active_org"]["org_id"]
    response = client.patch(PATH, json={"name": "  변경한 업체  "})
    assert response.status_code == 200, response.text
    assert response.json() == {"org_id": org_id, "org_type": org_type, "org_name": "변경한 업체"}
    after = client.get("/api/me").json()
    assert after["user"] == before["user"]
    assert after["active_org"] == {**before["active_org"], "org_name": "변경한 업체"}
    assert after["memberships"] == [after["active_org"]]
    assert other.get("/api/me").json() == other_before
    with database.begin() as db:
        assert db.get(OrganizationRow, org_id).name == "변경한 업체"


def test_rename_requires_authentication_csrf_and_admin():
    anonymous = TestClient(app, base_url="https://testserver")
    assert anonymous.patch(PATH, json={"name": "수정"}).status_code == 401
    client, before = signup("owner@example.com")
    for header in ("", "XMLHttpRequest"):
        assert (
            client.patch(
                PATH, json={"name": "수정"}, headers={"X-Requested-With": header}
            ).status_code
            == 403
        )
    with database.begin() as db:
        db.execute(
            update(MembershipRow)
            .where(MembershipRow.user_id == before["user"]["id"])
            .values(role="member")
        )
    assert client.patch(PATH, json={"name": "수정"}).status_code == 403
    assert client.get("/api/me").json()["active_org"]["org_name"] == "원래 업체"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": " \t\n"},
        {"name": "가" * 201},
        {"name": None},
        {"name": 42},
        {"name": "bad\x00name"},
        {"name": "bad\ud800name"},
        {"name": "수정", "org_id": "other"},
        {"name": "수정", "org_type": "platform"},
    ],
)
def test_rename_rejects_invalid_or_extra_fields(body):
    client, before = signup("owner@example.com")
    response = client.patch(
        PATH, content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    assert client.get("/api/me").json() == before


def test_org_header_cannot_select_unrelated_company():
    client, before = signup("owner@example.com")
    _, other = signup("other@example.com")
    assert (
        client.patch(
            PATH, json={"name": "수정"}, headers={"X-Org-Id": other["active_org"]["org_id"]}
        ).status_code
        == 403
    )
    assert client.get("/api/me").json() == before


def test_visitor_company_rename_syncs_shipper_profile_and_preserves_other_worlds(demo_tenants):
    visitor_app = create_app(get_settings().model_copy(update={"saas_mode": True}))
    visitor = TestClient(visitor_app, base_url="https://testserver", headers=CSRF)
    other = TestClient(visitor_app, base_url="https://testserver", headers=CSRF)
    before = visitor.post("/api/auth/demo-login", json={"org_type": "shipper"}).json()
    other_before = other.post("/api/auth/demo-login", json={"org_type": "shipper"}).json()
    org_id = before["active_org"]["org_id"]
    with database.begin() as db:
        previous_names = dict(
            db.execute(select(ShipperProfileRow.id, ShipperProfileRow.name)).all()
        )
        own_ids = set(
            db.scalars(select(ShipperProfileRow.id).where(ShipperProfileRow.org_id == org_id))
        )
    assert own_ids
    response = visitor.patch(PATH, json={"name": "우리 업체"})
    assert response.status_code == 200, response.text
    profile = visitor.get("/api/saas/shipper/profile").json()
    assert profile["org_name"] == profile["profile"]["name"] == "우리 업체"
    assert other.get("/api/me").json() == other_before
    with database.begin() as db:
        after_names = dict(db.execute(select(ShipperProfileRow.id, ShipperProfileRow.name)).all())
    assert after_names == {
        key: "우리 업체" if key in own_ids else name for key, name in previous_names.items()
    }


def test_shared_seed_company_rename_is_forbidden(demo_tenants):
    client = TestClient(app, base_url="https://testserver", headers=CSRF)
    response = client.post("/api/auth/demo-login", json={"org_type": "shipper"})
    assert response.status_code == 200, response.text
    before = response.json()
    assert client.patch(PATH, json={"name": "수정"}).status_code == 403
    assert client.get("/api/me").json() == before
