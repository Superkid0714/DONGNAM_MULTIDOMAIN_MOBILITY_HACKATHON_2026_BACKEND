from datetime import datetime, timedelta

from freshswitch_backend.coldchain import ColdchainParams
from freshswitch_backend.coldchain.models import Reading
from freshswitch_backend.coldchain.twin import build_context, evaluate
from freshswitch_backend.domain.models import KST

START = datetime(2026, 11, 17, 9, 0, tzinfo=KST)
PARAMS = ColdchainParams()


def _series(points: list[tuple[int, float, bool]]) -> list[Reading]:
    return [
        Reading(
            device_id="d",
            container_id="CA-01",
            ts=START + timedelta(minutes=m),
            temp_c=t,
            power_on=p,
        )
        for m, t, p in points
    ]


def _alerts(points: list[tuple[int, float, bool]], kind: str) -> list[str]:
    ctx = build_context("CA-01", "terminal", PARAMS)
    return [a.message for a in evaluate(_series(points), ctx, PARAMS).alerts if a.kind == kind]


def test_first_power_cut_alert_has_no_zero_cumulative() -> None:
    messages = _alerts([(0, 1.0, True), (10, 1.0, False), (20, 1.0, False)], "power_cut")
    assert len(messages) == 1
    assert "전원 차단 감지" in messages[0]
    assert "누적 단전" not in messages[0]


def test_second_power_cut_alert_shows_previous_cumulative() -> None:
    messages = _alerts(
        [(0, 1.0, True), (10, 1.0, False), (40, 1.0, True), (50, 1.0, False), (55, 1.0, False)],
        "power_cut",
    )
    assert len(messages) == 2
    assert "누적 단전" not in messages[0]
    assert "이전 누적 단전 30분" in messages[1]


def test_status_alert_at_first_cut_skips_zero_cumulative() -> None:
    messages = _alerts([(0, 1.0, True), (10, 1.0, False), (20, 1.0, False)], "status_change")
    assert messages
    assert all("누적 단전 0분" not in m for m in messages)
    restored = _alerts([(0, 1.0, True), (10, 1.0, False), (40, 1.0, True)], "power_restored")
    assert "누적 단전 30분" in restored[0]
