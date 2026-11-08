import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.api import auth
from freshswitch_backend.api.auth import (
    EmailFailureWindow,
    LoginFailureGuard,
    RateRule,
    SlidingWindowLimiter,
)
from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app

CSRF = {"X-Requested-With": "freshswitch"}
PROXY_PEER = ("127.0.0.1", 50000)
EMAIL = "lock-owner@example.com"
GHOST = "lock-ghost@example.com"
PASSWORD = "correct-horse-battery"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def saas_app():
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


@pytest.fixture
def clock(saas_app) -> FakeClock:
    clock = FakeClock()
    saas_app.state.login_failure_guard = LoginFailureGuard(clock=clock)
    saas_app.state.login_email_guard = EmailFailureWindow(clock=clock)
    return clock


def _client(app, ip: str = "8.8.8.8") -> TestClient:
    return TestClient(
        app,
        base_url="https://testserver",
        headers={**CSRF, "X-Forwarded-For": ip},
        client=PROXY_PEER,
    )


@pytest.fixture
def owner(saas_app) -> None:
    body = {
        "email": EMAIL,
        "password": PASSWORD,
        "name": "잠금 화주",
        "org_name": "잠금 영농조합",
        "org_type": "shipper",
    }
    assert _client(saas_app, "9.9.9.9").post("/api/auth/signup", json=body).status_code == 201


def _login(app, email: str, password: str, ip: str = "8.8.8.8"):
    return _client(app, ip).post("/api/auth/login", json={"email": email, "password": password})


def _fail(app, email: str, times: int, ip: str = "8.8.8.8") -> list[int]:
    return [_login(app, email, "wrong-password", ip).status_code for _ in range(times)]


def test_repeated_failures_lock_email_with_retry_after(saas_app, clock, owner) -> None:
    assert _fail(saas_app, EMAIL, auth.LOGIN_FAILURE_THRESHOLD) == [401] * 5
    locked = _login(saas_app, EMAIL, PASSWORD)
    assert locked.status_code == 429
    assert locked.headers["Retry-After"] == str(int(auth.LOGIN_LOCK_SECONDS))
    assert locked.json()["detail"] == auth.LOGIN_LOCKED
    assert _login(saas_app, EMAIL, PASSWORD, ip="7.7.7.7").status_code == 200
    assert _login(saas_app, EMAIL, PASSWORD).status_code == 429
    clock.now += auth.LOGIN_LOCK_SECONDS + 1
    assert _login(saas_app, EMAIL, PASSWORD).status_code == 200
    assert _fail(saas_app, EMAIL, 4) == [401] * 4
    assert _login(saas_app, EMAIL, PASSWORD).status_code == 200


def test_lock_grows_after_each_failure_past_threshold(saas_app, clock, owner) -> None:
    _fail(saas_app, EMAIL, 5)
    clock.now += 61
    assert _fail(saas_app, EMAIL, 1) == [401]
    second = _login(saas_app, EMAIL, PASSWORD)
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "120"
    clock.now += 121
    _fail(saas_app, EMAIL, 1)
    assert _login(saas_app, EMAIL, PASSWORD).headers["Retry-After"] == "240"
    for _ in range(10):
        clock.now += auth.LOGIN_MAX_LOCK_SECONDS + 1
        _fail(saas_app, EMAIL, 1)
    capped = _login(saas_app, EMAIL, PASSWORD)
    assert capped.headers["Retry-After"] == str(int(auth.LOGIN_MAX_LOCK_SECONDS))


def test_attacker_ip_lock_leaves_other_ips_usable(saas_app, clock, owner) -> None:
    for _ in range(6):
        assert _fail(saas_app, EMAIL, 1, ip="5.5.5.5") == [401]
        clock.now += auth.LOGIN_MAX_LOCK_SECONDS + 1
    assert _fail(saas_app, EMAIL, 1, ip="5.5.5.5") == [401]
    assert _login(saas_app, EMAIL, PASSWORD, ip="5.5.5.5").status_code == 429
    assert _login(saas_app, EMAIL, PASSWORD, ip="4.4.4.4").status_code == 200
    assert _login(saas_app, EMAIL, PASSWORD, ip="5.5.5.5").status_code == 429


def _spread(app, email: str, count: int) -> list[int]:
    return [
        _login(app, email, "wrong-password", ip=f"10.1.0.{n}").status_code for n in range(count)
    ]


def test_distributed_failures_lock_email_briefly(saas_app, clock, owner) -> None:
    assert _spread(saas_app, EMAIL, auth.LOGIN_EMAIL_THRESHOLD) == [401] * 20
    locked = _login(saas_app, EMAIL, PASSWORD, ip="10.1.0.0")
    assert locked.status_code == 429
    assert locked.headers["Retry-After"] == str(int(auth.LOGIN_EMAIL_LOCK_SECONDS))
    assert locked.json()["detail"] == auth.LOGIN_LOCKED
    assert _login(saas_app, EMAIL, PASSWORD, ip="3.3.3.3").status_code == 200
    assert _spread(saas_app, EMAIL, auth.LOGIN_EMAIL_THRESHOLD) == [401] * 20
    clock.now += auth.LOGIN_EMAIL_LOCK_SECONDS + 1
    assert _login(saas_app, EMAIL, "wrong-password", ip="10.1.0.1").status_code == 401
    assert _login(saas_app, EMAIL, PASSWORD, ip="10.1.0.2").status_code == 200


def test_distributed_email_lock_does_not_grow(saas_app, clock, owner) -> None:
    for _ in range(3):
        _spread(saas_app, EMAIL, auth.LOGIN_EMAIL_THRESHOLD)
        locked = _login(saas_app, EMAIL, PASSWORD, ip="10.1.0.5")
        assert locked.status_code == 429
        assert locked.headers["Retry-After"] == str(int(auth.LOGIN_EMAIL_LOCK_SECONDS))
        clock.now += auth.LOGIN_EMAIL_LOCK_SECONDS + 1
    _fail(saas_app, EMAIL, 1, ip="10.1.0.6")
    assert _login(saas_app, EMAIL, PASSWORD, ip="10.1.0.6").status_code == 200


def test_success_resets_failure_count(saas_app, clock, owner) -> None:
    for _ in range(3):
        assert _fail(saas_app, EMAIL, 4) == [401] * 4
        assert _login(saas_app, EMAIL, PASSWORD).status_code == 200


def test_unknown_and_real_emails_lock_identically(saas_app, clock, owner) -> None:
    real = [_login(saas_app, EMAIL, "wrong-password") for _ in range(7)]
    ghost = [_login(saas_app, GHOST, "wrong-password") for _ in range(7)]
    assert [r.status_code for r in real] == [r.status_code for r in ghost]
    assert [r.json()["detail"] for r in real] == [r.json()["detail"] for r in ghost]
    assert [r.headers.get("Retry-After") for r in real] == [
        r.headers.get("Retry-After") for r in ghost
    ]
    assert [r.status_code for r in real] == [401] * 5 + [429] * 2


def test_ip_rule_counts_only_failures(saas_app, clock, owner) -> None:
    rule = RateRule(per_client=3, window_seconds=300)
    rules = {**auth.AUTH_RATE_RULES, auth.LOGIN_ROUTE: rule}
    saas_app.state.auth_rate_limiter = SlidingWindowLimiter(rules)
    assert [_login(saas_app, EMAIL, PASSWORD).status_code for _ in range(10)] == [200] * 10
    spread = [_login(saas_app, f"guess-{n}@example.com", "x" * 8).status_code for n in range(4)]
    assert spread == [401, 401, 401, 429]
    blocked = _login(saas_app, EMAIL, PASSWORD)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert _login(saas_app, EMAIL, PASSWORD, ip="6.6.6.6").status_code == 200


def test_many_successful_logins_are_not_limited(saas_app, owner) -> None:
    codes = [_login(saas_app, EMAIL, PASSWORD).status_code for _ in range(40)]
    assert codes == [200] * 40


def test_default_login_rule_is_generous() -> None:
    rule = auth.AUTH_RATE_RULES[auth.LOGIN_ROUTE]
    assert rule.per_client >= 20
    assert auth.LOGIN_FAILURE_THRESHOLD >= 5
    assert auth.LOGIN_EMAIL_THRESHOLD >= 4 * auth.LOGIN_FAILURE_THRESHOLD
    assert auth.LOGIN_EMAIL_LOCK_SECONDS <= auth.LOGIN_LOCK_SECONDS
