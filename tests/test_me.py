import json

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.config import get_settings
from freshswitch_backend.main import app, create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import demo_orgs
from freshswitch_backend.persistence.tables import UserRow

CSRF = {"X-Requested-With": "freshswitch"}


def signup(client: TestClient, email: str) -> dict:
    response = client.post(
        "/api/auth/signup",
        json={
            "email": email,
            "password": "profile-test-password",
            "name": "원래 이름",
            "org_name": "원래 조직",
            "org_type": "shipper",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def profile_client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def test_profile_update_persists_and_preserves_other_users_and_memberships(profile_client):
    before = signup(profile_client, "owner@example.com")
    other = TestClient(app, base_url="https://testserver")
    other_before = signup(other, "other@example.com")
    response = profile_client.patch("/api/me", json={"name": "  수정한 이름  "}, headers=CSRF)
    assert response.status_code == 200, response.text
    expected = {**before["user"], "name": "수정한 이름"}
    assert response.json() == expected
    assert profile_client.get("/api/me").json() == {**before, "user": expected}
    assert other.get("/api/me").json() == other_before
    with database.begin() as db:
        assert db.get(UserRow, before["user"]["id"]).name == "수정한 이름"
        assert db.get(UserRow, other_before["user"]["id"]).name == "원래 이름"


def test_profile_update_requires_session_and_csrf(profile_client):
    assert profile_client.patch("/api/me", json={"name": "변경"}).status_code == 401
    before = signup(profile_client, "owner@example.com")
    for headers in ({}, {"X-Requested-With": "XMLHttpRequest"}):
        response = profile_client.patch("/api/me", json={"name": "변경"}, headers=headers)
        assert response.status_code == 403
    assert profile_client.get("/api/me").json() == before
    assert profile_client.post("/api/auth/logout", headers=CSRF).status_code == 204
    assert profile_client.patch("/api/me", json={"name": "변경"}, headers=CSRF).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": " \t\n "},
        {"name": "가" * 201},
        {"name": None},
        {"name": 123},
        {"name": ["변경"]},
        {"name": "bad\x00name"},
        {"name": "bad\ud800name"},
        {"name": "변경", "email": "attacker@example.com"},
        {"name": "변경", "id": "another-user"},
        {"name": "변경", "org_name": "다른 조직"},
    ],
)
def test_profile_update_rejects_invalid_input_without_mutation(profile_client, body):
    before = signup(profile_client, "owner@example.com")
    response = profile_client.patch(
        "/api/me",
        content=json.dumps(body),
        headers={**CSRF, "Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert profile_client.get("/api/me").json() == before


def test_profile_update_accepts_maximum_trimmed_name(profile_client):
    signup(profile_client, "owner@example.com")
    name = "가" * 200
    response = profile_client.patch("/api/me", json={"name": f" {name} "}, headers=CSRF)
    assert response.status_code == 200, response.text
    assert profile_client.get("/api/me").json()["user"]["name"] == name


def test_visitor_profile_update_is_isolated_from_seed_and_other_visitors(demo_tenants):
    settings = get_settings().model_copy(update={"saas_mode": True})
    visitor_app = create_app(settings)
    visitor = TestClient(visitor_app, base_url="https://testserver", headers=CSRF)
    other = TestClient(visitor_app, base_url="https://testserver", headers=CSRF)
    before = visitor.post("/api/auth/demo-login", json={"new_world": True}).json()
    other_before = other.post("/api/auth/demo-login", json={"new_world": True}).json()
    with database.begin() as db:
        seeds_before = {o.user_id: db.get(UserRow, o.user_id).name for o in demo_orgs()}
    response = visitor.patch("/api/me", json={"name": "내 데모 이름"})
    assert response.status_code == 200, response.text
    assert response.json() == {**before["user"], "name": "내 데모 이름"}
    assert other.get("/api/me").json() == other_before
    with database.begin() as db:
        assert {o.user_id: db.get(UserRow, o.user_id).name for o in demo_orgs()} == seeds_before


def test_shared_seed_profile_cannot_be_edited(profile_client, demo_tenants):
    response = profile_client.post("/api/auth/demo-login", json={"org_type": "shipper"})
    assert response.status_code == 200, response.text
    before = response.json()
    assert profile_client.patch("/api/me", json={"name": "변경"}, headers=CSRF).status_code == 403
    assert profile_client.get("/api/me").json() == before
