from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from freshswitch_backend.api.auth import _token_hash
from freshswitch_backend.config import Settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import AuthSessionRow

PREFIX = "/test/api"
CSRF = {"X-Requested-With": "freshswitch"}
PASSWORD = "migration-test-password"


def client(*, path="/", saas=True):
    app = create_app(Settings(saas_mode=saas, api_prefix=PREFIX, session_cookie_path=path))
    return TestClient(app, base_url="https://testserver", headers=CSRF)


def signup(http, email="customer@example.com"):
    return http.post(
        PREFIX + "/auth/signup",
        json={
            "email": email,
            "password": PASSWORD,
            "name": "고객",
            "org_name": "일반 업체",
            "org_type": "shipper",
        },
    )


def cookies(response):
    result = []
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        result.extend(parsed.values())
    return result


def test_signup_expires_legacy_cookie_before_setting_root_cookie():
    http = client()
    http.cookies.set("fs_session", "stale-legacy", path="/test", domain="testserver.local")
    response = signup(http)
    assert response.status_code == 201, response.text
    values = cookies(response)
    assert [value["path"] for value in values] == ["/test", "/"]
    assert values[0]["max-age"] == "0"
    assert int(values[1]["max-age"]) > 0
    assert all(
        value["secure"] and value["httponly"] and value["samesite"] == "lax" for value in values
    )
    assert {cookie.path for cookie in http.cookies.jar if cookie.name == "fs_session"} == {"/"}
    assert http.get(PREFIX + "/me").status_code == 200


def test_existing_legacy_session_still_works_and_login_migrates_cookie():
    legacy = client(path="/test")
    created = signup(legacy)
    assert created.status_code == 201
    old_token = legacy.cookies.get("fs_session")
    http = client()
    http.cookies.update(legacy.cookies)
    assert http.get(PREFIX + "/me").json()["user"] == created.json()["user"]
    with database.begin() as db:
        assert db.get(AuthSessionRow, _token_hash(old_token)) is not None
    response = http.post(
        PREFIX + "/auth/login", json={"email": "customer@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert [value["path"] for value in cookies(response)] == ["/test", "/"]
    assert {cookie.path for cookie in http.cookies.jar if cookie.name == "fs_session"} == {"/"}
    assert http.get(PREFIX + "/me").json()["user"] == created.json()["user"]


def test_logout_clears_root_and_legacy_cookies_without_resurrection():
    http = client()
    assert signup(http).status_code == 201
    token = http.cookies.get("fs_session")
    http.cookies.set("fs_session", token, path="/test", domain="testserver.local")
    response = http.post(PREFIX + "/auth/logout")
    assert response.status_code == 204
    values = cookies(response)
    assert [value["path"] for value in values] == ["/test", "/"]
    assert all(value["max-age"] == "0" for value in values)
    assert not list(http.cookies.jar)
    assert http.get(PREFIX + "/me").status_code == 401
    with database.begin() as db:
        assert db.get(AuthSessionRow, _token_hash(token)) is None


def test_stale_session_401_clears_both_cookie_paths():
    http = client()
    for path in ("/", "/test"):
        http.cookies.set("fs_session", "expired", path=path, domain="testserver.local")
    response = http.get(PREFIX + "/me")
    assert response.status_code == 401
    values = cookies(response)
    assert [value["path"] for value in values] == ["/test", "/"]
    assert all(value["max-age"] == "0" for value in values)
    assert not list(http.cookies.jar)


@pytest.mark.parametrize(("saas", "path"), [(False, "/"), (False, "/test"), (True, "/test")])
def test_legacy_cleanup_is_limited_to_root_saas_sessions(saas, path):
    http = client(path=path, saas=saas)
    response = signup(http)
    assert response.status_code == 201
    assert [value["path"] for value in cookies(response)] == [path]
    response = http.post(PREFIX + "/auth/logout")
    assert [value["path"] for value in cookies(response)] == [path]


def test_cookie_migration_preserves_other_account_sessions():
    first = client(path="/test")
    assert signup(first).status_code == 201
    first_token = first.cookies.get("fs_session")
    second = client(path="/test")
    assert signup(second, email="other@example.com").status_code == 201
    with database.begin() as db:
        before = set(db.scalars(select(AuthSessionRow.token_hash)))
    http = client()
    http.cookies.update(first.cookies)
    assert http.get(PREFIX + "/me").status_code == 200
    with database.begin() as db:
        assert set(db.scalars(select(AuthSessionRow.token_hash))) == before
        assert db.get(AuthSessionRow, _token_hash(first_token)) is not None
