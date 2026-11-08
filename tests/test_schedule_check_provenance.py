from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.api import vessels
from freshswitch_backend.config import Settings
from freshswitch_backend.integrations import (
    MovementProvider,
    MovementSnapshot,
    SnapshotStore,
    VesselMovement,
)
from freshswitch_backend.integrations.mof_vessel import KST
from freshswitch_backend.main import app

SNAPSHOT_AT = datetime(2026, 11, 17, 0, 30, tzinfo=UTC)
LATE = VesselMovement(
    port_code="020",
    call_sign="ABC",
    vessel_name="FRESH HARMONY",
    departure_at="2026-11-19T06:00:00+09:00",
)


class FakeClient:
    def __init__(self, movements=None, error: Exception | None = None) -> None:
        self.movements = movements or []
        self.error = error
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def fetch_movements(self, start: date, end: date, direction=None, **_):
        self.calls += 1
        if self.error:
            raise self.error
        return self.movements


@pytest.fixture
def snapshot_store(tmp_path, monkeypatch) -> dict[str, bool]:
    store = SnapshotStore(tmp_path)
    store.save(
        MovementSnapshot(
            date=date(2026, 11, 18),
            direction="O",
            fetched_at=SNAPSHOT_AT,
            count=1,
            movements=[LATE],
        )
    )
    settings = {"offline": False}
    monkeypatch.setattr(
        vessels,
        "movement_provider",
        lambda factory: MovementProvider(factory, offline_mode=settings["offline"], store=store),
    )
    return settings


@pytest.fixture
def use_factory():
    def use(factory) -> None:
        app.dependency_overrides[vessels.vessel_client_factory] = lambda: factory

    yield use
    app.dependency_overrides.pop(vessels.vessel_client_factory, None)


def _plan(client: TestClient) -> str:
    response = client.post("/api/plans", json={"scenario_id": "default"})
    assert response.status_code == 201
    return response.json()["plan_id"]


def _check(client: TestClient, plan_id: str, **kwargs) -> dict:
    response = client.post(f"/api/plans/{plan_id}/schedule-check", **kwargs)
    assert response.status_code == 200
    return response.json()


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_provided_movements(client: TestClient, use_factory, snapshot_store) -> None:
    fake = FakeClient([LATE])
    use_factory(lambda: fake)
    body = _check(client, _plan(client), json={"movements": [LATE.model_dump(mode="json")]})
    assert fake.calls == 0
    assert body["source"] == "provided"
    assert body["data_source"] == "provided"
    assert body["fetched_at"] is None
    assert body["snapshot_at"] is None
    assert body["snapshot_name"] is None
    assert body["demo_mixed"] is False
    assert body["source_note"] == "요청 본문으로 받은 선박 입출항 실적 1건을 썼습니다"


def test_live_fetch(client: TestClient, use_factory, snapshot_store) -> None:
    fake = FakeClient([LATE])
    use_factory(lambda: fake)
    before = datetime.now(UTC)
    body = _check(client, _plan(client))
    assert fake.calls == 1
    assert body["source"] == "해수부"
    assert body["data_source"] == "live"
    fetched = _at(body["fetched_at"])
    assert before <= fetched <= datetime.now(UTC)
    assert body["snapshot_at"] is None
    assert body["snapshot_name"] is None
    assert body["demo_mixed"] is False
    label = fetched.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    assert body["source_note"] == f"해수부 선박 입출항 실시간 조회 ({label})"
    assert body["changes"][0]["delta_hours"] == 12.0


@pytest.mark.parametrize(
    ("factory", "cause"),
    [
        (None, "해수부 API 키가 설정되지 않았습니다"),
        (lambda: FakeClient(error=httpx.ConnectError("down")), None),
    ],
    ids=["no-key", "live-error"],
)
def test_snapshot_fallback(client: TestClient, use_factory, snapshot_store, factory, cause) -> None:
    use_factory(factory)
    body = _check(client, _plan(client))
    assert body["source"] == "해수부"
    assert body["data_source"] == "snapshot"
    assert body["fetched_at"] is None
    assert _at(body["snapshot_at"]) == SNAPSHOT_AT
    assert body["snapshot_name"] == "20261118-O.json"
    assert body["demo_mixed"] is False
    note = body["source_note"]
    assert note.startswith("저장 스냅샷 20261118-O (수집 2026-11-17 09:30 KST, 실시간 조회 실패: ")
    if cause:
        assert note.endswith(f"{cause})")
    assert body["changes"][0]["delta_hours"] == 12.0


def test_demo_mixes_sandbox_movements(client: TestClient, use_factory, snapshot_store) -> None:
    use_factory(lambda: FakeClient([]))
    body = _check(client, _plan(client), params={"demo": "true"})
    assert body["source"] == "해수부"
    assert body["data_source"] == "sandbox_mix"
    assert body["demo_mixed"] is True
    assert body["fetched_at"] is not None
    assert body["snapshot_at"] is None
    assert body["snapshot_name"] is None
    assert body["source_note"].startswith(
        "시연용 선박 실적 2건을 섞은 결과입니다 (기반: 해수부 선박 입출항 실시간 조회 ("
    )
    assert [e["voyage_id"] for e in body["events"]] == ["V-2611A"]


def test_offline_demo_mix_keeps_snapshot_meta(
    client: TestClient, use_factory, snapshot_store, monkeypatch
) -> None:
    snapshot_store["offline"] = True
    monkeypatch.setattr(vessels, "get_settings", lambda: Settings(offline_mode=True))
    fake = FakeClient([LATE])
    use_factory(lambda: fake)
    body = _check(client, _plan(client), params={"demo": "true"})
    assert fake.calls == 0
    assert body["source"] == "해수부"
    assert body["data_source"] == "sandbox_mix"
    assert body["demo_mixed"] is True
    assert body["fetched_at"] is None
    assert _at(body["snapshot_at"]) == SNAPSHOT_AT
    assert body["snapshot_name"] == "20261118-O.json"
    assert body["source_note"] == (
        "시연용 선박 실적 2건을 섞은 결과입니다 "
        "(기반: 저장 스냅샷 20261118-O (수집 2026-11-17 09:30 KST, 오프라인 모드))"
    )
