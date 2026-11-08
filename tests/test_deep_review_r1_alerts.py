from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.coldchain.twin import evaluate
from freshswitch_backend.domain.models import KST

START = datetime(2026, 11, 17, 10, 0, tzinfo=KST)


def _reading(device: str, minute: int, power_on: bool, plan_id: str) -> dict:
    return {
        "device_id": device,
        "container_id": "CA-01",
        "plan_id": plan_id,
        "ts": (START + timedelta(minutes=minute)).isoformat(),
        "temp_c": 1.0 if power_on else 6.5,
        "power_on": power_on,
    }


def _status_alerts(client: TestClient, plan_id: str) -> list[dict]:
    feed = client.get("/api/coldchain/alerts", params={"plan_id": plan_id, "limit": 1000}).json()
    return [a for a in feed["alerts"] if a["kind"] == "status_change"]


@pytest.fixture
def plan_id(client: TestClient) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def _send(client: TestClient, items: list[dict]) -> None:
    for item in items:
        response = client.post("/api/coldchain/readings", json=item)
        assert response.status_code == 200, response.text


def test_coldchain4_late_or_second_device_does_not_repeat_status_alert(
    client: TestClient, plan_id: str
) -> None:
    _send(
        client,
        [
            _reading("dev-a", 0, True, plan_id),
            _reading("dev-a", 5, True, plan_id),
            _reading("dev-a", 10, False, plan_id),
            _reading("dev-a", 15, False, plan_id),
        ],
    )
    before = _status_alerts(client, plan_id)
    assert len(before) == 1
    _send(client, [_reading("dev-b", 7, False, plan_id)])
    _send(client, [_reading("dev-a", 12, False, plan_id)])
    assert _status_alerts(client, plan_id) == before


def test_coldchain4_separate_earlier_episode_still_alerts(client: TestClient, plan_id: str) -> None:
    _send(
        client,
        [
            _reading("dev-a", 0, True, plan_id),
            _reading("dev-a", 60, False, plan_id),
            _reading("dev-a", 65, False, plan_id),
        ],
    )
    _send(client, [_reading("dev-b", 10, False, plan_id), _reading("dev-b", 20, True, plan_id)])
    statuses = [a["key"].split(":")[1] for a in _status_alerts(client, plan_id)]
    assert len(statuses) == 3
    assert statuses.count("normal") == 1


@pytest.mark.parametrize("preset", ["power_cut_restore", "short_outage", "normal"])
def test_coldchain4_demo_simulation_alerts_match_one_shot_evaluation(
    client: TestClient, plan_id: str, preset: str
) -> None:
    run = client.post(
        "/api/coldchain/simulate",
        json={"preset": preset, "plan_id": plan_id, "container_id": "CA-01"},
    )
    assert run.status_code == 200, run.text
    record = coldchain_store.get_record("CA-01", plan_id)
    ctx, _ = coldchain_store._evaluate(record)
    expected = [
        a.key for a in evaluate(record.sorted_readings(), ctx, coldchain_store.params).alerts
    ]
    feed = client.get("/api/coldchain/alerts", params={"plan_id": plan_id, "limit": 1000}).json()
    assert [a["key"] for a in feed["alerts"]] == expected
