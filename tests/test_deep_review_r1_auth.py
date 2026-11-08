import json

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app

CSRF = {"X-Requested-With": "freshswitch"}


@pytest.fixture
def saas(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _post(client: TestClient, path: str, body: dict) -> int:
    headers = {**CSRF, "Content-Type": "application/json"}
    return client.post(path, content=json.dumps(body), headers=headers).status_code


SIGNUP = {
    "email": "owner@example.com",
    "password": "correct-horse-battery",
    "name": "김화주",
    "org_name": "진주 딸기 영농조합",
    "org_type": "shipper",
}


@pytest.mark.parametrize("bad", ["a\x00b", "a\ud800b"])
@pytest.mark.parametrize("field", ["email", "password", "name", "org_name"])
def test_auth1_signup_rejects_unstorable_text(saas, field: str, bad: str) -> None:
    client = TestClient(saas, base_url="https://testserver")
    value = f"{bad}@example.com" if field == "email" else f"abcdefgh{bad}"
    assert _post(client, "/api/auth/signup", {**SIGNUP, field: value}) == 422


@pytest.mark.parametrize("bad", ["a\x00@example.com", "a\ud800@example.com"])
@pytest.mark.parametrize("field", ["email", "password"])
def test_auth1_login_rejects_unstorable_text(saas, field: str, bad: str) -> None:
    client = TestClient(saas, base_url="https://testserver")
    body = {"email": "owner@example.com", "password": "correct-horse-battery", field: bad}
    assert _post(client, "/api/auth/login", body) == 422


@pytest.mark.parametrize("bad", ["freshswitch\x00", "fresh\ud800"])
def test_auth1_demo_login_rejects_unstorable_org_id(saas, bad: str) -> None:
    client = TestClient(saas, base_url="https://testserver")
    body = {"org_type": "platform", "org_id": bad}
    assert _post(client, "/api/auth/demo-login", body) == 422
