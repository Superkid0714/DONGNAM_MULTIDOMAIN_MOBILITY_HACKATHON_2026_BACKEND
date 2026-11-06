import json

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import saas_app
from sqlalchemy import update

from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import MembershipRow, OrganizationRow

CSRF = {"X-Requested-With": "freshswitch"}
PATH = "/api/saas/org"


def signup(email: str, org_type: str = "shipper") -> tuple[TestClient, dict]:
    client = TestClient(saas_app, base_url="https://testserver", headers=CSRF)
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
    anonymous = TestClient(saas_app, base_url="https://testserver")
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
