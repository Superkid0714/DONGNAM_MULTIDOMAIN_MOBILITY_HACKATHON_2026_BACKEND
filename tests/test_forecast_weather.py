import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest

from freshswitch_backend.api import forecast as forecast_api
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.forecast.predictor import default_predictor
from freshswitch_backend.forecast.service import ForecastOptions, forecast_voyages, weather_use
from freshswitch_backend.integrations import kma_forecast
from freshswitch_backend.integrations.kma_forecast import (
    ForecastPoint,
    KmaApiError,
    KmaForecastClient,
    KmaSnapshot,
    MissingKmaKey,
    WeatherFeed,
    WeatherProvider,
    WeatherWarning,
    active_warnings,
    assess,
    assess_each,
    base_time,
    combine,
    parse_forecast,
    parse_warnings,
)
from freshswitch_backend.integrations.mof_vessel import KST
from freshswitch_backend.main import app
from freshswitch_backend.planning.service import build_plan

ETD = datetime(2026, 11, 18, 18, 0, tzinfo=KST)
NOW = datetime(2026, 11, 17, 6, 0, tzinfo=KST)
ISSUED = datetime(2026, 11, 17, 5, 0, tzinfo=KST)


def forecast_payload(wind: float, wave: float, base: datetime = ISSUED) -> dict:
    items = []
    for hours in range(0, 60, 3):
        at = base + timedelta(hours=hours + 1)
        for category, value in (("WSD", wind), ("WAV", wave), ("TMP", 12)):
            items.append(
                {
                    "baseDate": f"{base:%Y%m%d}",
                    "baseTime": f"{base:%H%M}",
                    "category": category,
                    "fcstDate": f"{at:%Y%m%d}",
                    "fcstTime": f"{at:%H%M}",
                    "fcstValue": str(value),
                    "nx": 93,
                    "ny": 73,
                }
            )
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {"dataType": "JSON", "items": {"item": items}, "totalCount": len(items)},
        }
    }


def warning_payload(*titles: tuple[str, str]) -> dict:
    if not titles:
        return {"response": {"header": {"resultCode": "03", "resultMsg": "NO_DATA"}}}
    items = [{"stnId": "159", "title": t, "tmFc": int(tm), "tmSeq": 1} for tm, t in titles]
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {"items": {"item": items}, "totalCount": len(items)},
        }
    }


def mock_client(forecast: dict, warnings: dict, seen: list | None = None) -> KmaForecastClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = forecast if "VilageFcst" in request.url.path else warnings
        return httpx.Response(200, content=json.dumps(body).encode())

    return KmaForecastClient("test-key", transport=httpx.MockTransport(handler))


@pytest.fixture
def kma(monkeypatch):
    def install(factory):
        app.dependency_overrides[forecast_api.kma_client_factory] = lambda: factory
        monkeypatch.setattr(forecast_api, "clock", lambda: NOW)

    yield install
    app.dependency_overrides.pop(forecast_api.kma_client_factory, None)


def test_base_time_picks_latest_published_slot():
    assert base_time(datetime(2026, 11, 17, 5, 20, tzinfo=KST)) == ISSUED
    assert base_time(datetime(2026, 11, 17, 5, 5, tzinfo=KST)) == datetime(
        2026, 11, 17, 2, 0, tzinfo=KST
    )
    assert base_time(datetime(2026, 11, 17, 1, 0, tzinfo=KST)) == datetime(
        2026, 11, 16, 23, 0, tzinfo=KST
    )


def test_parse_forecast_and_warnings():
    points, issued = parse_forecast(json.dumps(forecast_payload(15.2, 1.0)))
    assert issued == ISSUED
    assert points[0].wind_speed_ms == 15.2 and points[0].wave_height_m == 1.0
    warnings = parse_warnings(
        json.dumps(
            warning_payload(
                ("202611161000", "[특보] 제11-3호 : 2026.11.16.10:00 / 풍랑주의보 발표(*)"),
                ("202611170400", "[특보] 제11-4호 : 2026.11.17.04:00 / 풍랑주의보 해제"),
                (
                    "202611170500",
                    "[특보] 제11-5호 : 2026.11.17.05:00 / 태풍주의보 발표·건조주의보 발표",
                ),
            )
        )
    )
    assert [(w.name, w.kind, w.lifted) for w in warnings] == [
        ("풍랑주의보", "wind_wave", False),
        ("풍랑주의보", "wind_wave", True),
        ("태풍주의보", "typhoon", False),
        ("건조주의보", "other", False),
    ]
    assert [w.name for w in active_warnings(warnings)] == ["태풍주의보"]
    assert parse_warnings(json.dumps(warning_payload())) == []
    error = "<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode>"
    error += "<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
    error += "</cmmMsgHeader></OpenAPI_ServiceResponse>"
    with pytest.raises(KmaApiError) as exc:
        parse_forecast(error)
    assert exc.value.code == "30"


def test_client_requests_newport_grid_and_busan_warnings():
    seen: list[httpx.Request] = []

    async def go():
        async with mock_client(forecast_payload(5, 0.5), warning_payload(), seen) as client:
            return await client.fetch(NOW)

    snapshot = asyncio.run(go())
    assert snapshot.issued_at == ISSUED and snapshot.warnings == []
    forecast, warnings = seen
    assert forecast.url.params["nx"] == "93" and forecast.url.params["ny"] == "73"
    assert forecast.url.params["base_date"] == "20261117"
    assert forecast.url.params["base_time"] == "0500"
    assert warnings.url.params["stnId"] == "159"


def test_assess_levels_and_horizon():
    calm = KmaSnapshot(
        fetched_at=NOW,
        issued_at=ISSUED,
        points=[ForecastPoint(at=ETD, wind_speed_ms=6, wave_height_m=1)],
    )
    assert assess(calm, [ETD], NOW).level == "normal"
    windy = calm.model_copy(
        update={"points": [ForecastPoint(at=ETD - timedelta(hours=3), wave_height_m=3.5)]}
    )
    result = assess(windy, [ETD], NOW)
    assert result.level == "strong_wind" and result.max_wave_height_m == 3.5
    typhoon = calm.model_copy(
        update={
            "warnings": [
                WeatherWarning(
                    title="태풍주의보 발표",
                    issued_at=NOW,
                    kind="typhoon",
                    name="태풍주의보",
                    lifted=False,
                )
            ]
        }
    )
    assert assess(typhoon, [ETD], NOW).level == "typhoon"
    far = assess(typhoon, [ETD + timedelta(days=10)], NOW)
    assert far.level == "normal" and far.covered is False


def test_provider_falls_back_to_snapshot_then_unavailable(tmp_path):
    def missing():
        raise MissingKmaKey()

    path = tmp_path / "kma.json"
    none = asyncio.run(WeatherProvider(missing, snapshot_path=path).get(NOW))
    assert none.source == "unavailable" and "키" in none.fallback_reason
    path.write_text(
        KmaSnapshot(
            fetched_at=NOW,
            issued_at=ISSUED,
            points=[ForecastPoint(at=ETD, wind_speed_ms=16)],
        ).model_dump_json(),
        encoding="utf-8",
    )
    snap = asyncio.run(WeatherProvider(missing, snapshot_path=path).get(NOW))
    assert snap.source == "snapshot" and snap.issued_at == ISSUED and snap.fallback_reason
    offline = asyncio.run(WeatherProvider(missing, offline_mode=True, snapshot_path=path).get(NOW))
    assert offline.source == "snapshot" and offline.fallback_reason is None


def _plan(client) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def test_delay_forecast_weather_auto_strong_wind(client, kma):
    kma(lambda: mock_client(forecast_payload(16, 2.0), warning_payload()))
    plan_id = _plan(client)
    body = client.post(
        f"/api/plans/{plan_id}/delay-forecast?llm=false", json={"weather": "auto"}
    ).json()
    weather = body["weather"]
    assert body["options"]["weather"] == "auto"
    assert weather["mode"] == "auto" and weather["level"] == "strong_wind"
    assert weather["source"] == "live" and weather["grid"] == "93,73"
    assert datetime.fromisoformat(weather["issued_at"]) == ISSUED
    assert "2026-11-17 05:00 발표" in weather["note"]
    assert weather["max_wind_speed_ms"] == 16
    assert all(v["forecast"]["weather"] == "strong_wind" for v in body["voyages"])


def test_delay_forecast_weather_auto_typhoon_warning(client, kma):
    warnings = warning_payload(
        ("202611170500", "[특보] 제11-5호 : 2026.11.17.05:00 / 태풍주의보 발표(*)")
    )
    kma(lambda: mock_client(forecast_payload(8, 1.0), warnings))
    plan_id = _plan(client)
    body = client.post(
        f"/api/plans/{plan_id}/delay-forecast?llm=false", json={"weather": "auto"}
    ).json()
    assert body["weather"]["level"] == "typhoon"
    assert body["weather"]["active_warnings"] == ["태풍주의보"]
    assert body["voyages"][0]["forecast"]["weather"] == "typhoon"


def test_delay_forecast_weather_auto_falls_back_to_normal(client, kma, monkeypatch):
    def missing():
        raise MissingKmaKey()

    kma(missing)
    monkeypatch.setattr(kma_forecast, "load_snapshot", lambda path: None)
    plan_id = _plan(client)
    body = client.post(
        f"/api/plans/{plan_id}/delay-forecast?llm=false", json={"weather": "auto"}
    ).json()
    weather = body["weather"]
    assert weather["level"] == "normal" and weather["source"] == "unavailable"
    assert "키" in weather["fallback_reason"]
    assert body["voyages"][0]["forecast"]["weather"] == "normal"


def test_delay_forecast_default_weather_is_manual_and_proxy_is_labeled(client, kma):
    def boom():
        raise AssertionError("기상청을 부르면 안 됨")

    kma(boom)
    plan_id = _plan(client)
    body = client.post(f"/api/plans/{plan_id}/delay-forecast?llm=false", json={}).json()
    assert body["weather"]["mode"] == "manual" and body["weather"]["level"] == "normal"
    assert body["remaining_risk_voyages"] == []
    congestion = body["port_congestion"]
    assert congestion["proxy_indicator"] is True
    assert congestion["proxy_label"].startswith("보정 신호(가정)")


def _warnings(*titles: tuple[str, str]) -> list[WeatherWarning]:
    return parse_warnings(json.dumps(warning_payload(*titles)))


def test_non_object_json_is_a_kma_error():
    for payload in (b"[]", b'"x"', b'{"response": null}', b"[1, 2]"):
        with pytest.raises(KmaApiError):
            parse_forecast(payload)
    with pytest.raises(KmaApiError):
        parse_warnings(b'{"response": null}')
    assert parse_forecast(b'{"response": {"header": "x", "body": "y"}}') == ([], None)


def _raw_client(content: bytes, seen: list | None = None) -> KmaForecastClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, content=content)

    return KmaForecastClient("test-key", transport=httpx.MockTransport(handler))


def test_provider_falls_back_on_malformed_response(tmp_path):
    path = tmp_path / "kma.json"
    none = asyncio.run(WeatherProvider(lambda: _raw_client(b"[]"), snapshot_path=path).get(NOW))
    assert none.source == "unavailable" and none.fallback_reason
    path.write_text(KmaSnapshot(fetched_at=NOW, issued_at=ISSUED).model_dump_json())
    snap = asyncio.run(WeatherProvider(lambda: _raw_client(b"[]"), snapshot_path=path).get(NOW))
    assert snap.source == "snapshot" and snap.fallback_reason


def test_delay_forecast_weather_auto_survives_malformed_response(client, kma, monkeypatch):
    kma(lambda: _raw_client(b"[]"))
    monkeypatch.setattr(kma_forecast, "load_snapshot", lambda path: None)
    response = client.post(
        f"/api/plans/{_plan(client)}/delay-forecast?llm=false", json={"weather": "auto"}
    )
    assert response.status_code == 200
    assert response.json()["weather"]["level"] == "normal"


def test_warning_state_follows_phenomenon():
    upgrade = _warnings(
        ("202611160100", "[특보] 제11-1호 : 2026.11.16.01:00 / 풍랑주의보 발표(*)"),
        ("202611160400", "[특보] 제11-2호 : 2026.11.16.04:00 / 풍랑경보 발표"),
        ("202611160900", "[특보] 제11-3호 : 2026.11.16.09:00 / 풍랑경보 해제"),
    )
    assert active_warnings(upgrade) == []
    changed = _warnings(
        ("202611160100", "[특보] 제11-1호 : 2026.11.16.01:00 / 풍랑주의보 발표"),
        ("202611160400", "[특보] 제11-2호 : 2026.11.16.04:00 / 풍랑주의보 → 풍랑경보 변경"),
        ("202611160900", "[특보] 제11-3호 : 2026.11.16.09:00 / 풍랑경보 해제"),
    )
    assert [w.name for w in changed] == ["풍랑주의보", "풍랑경보", "풍랑경보"]
    assert active_warnings(changed) == []
    downgrade = _warnings(
        ("202611160100", "[특보] 제11-1호 : 2026.11.16.01:00 / 풍랑경보 발표"),
        ("202611160400", "[특보] 제11-2호 : 2026.11.16.04:00 / 풍랑경보→풍랑주의보 변경"),
    )
    assert [w.name for w in active_warnings(downgrade)] == ["풍랑주의보"]
    bracket = _warnings(
        ("202611160100", "[특보] 제11-1호 : 2026.11.16.01:00 / 풍랑경보 변경(풍랑주의보→풍랑경보)"),
        ("202611160100", "[특보] 제11-1호 : 2026.11.16.01:00 / 강풍주의보 발표"),
    )
    assert [w.name for w in active_warnings(bracket)] == ["풍랑경보", "강풍주의보"]
    same_title = _warnings(
        ("202611160100", "[특보] 제11-1호 : 2026.11.16.01:00 / 풍랑경보 해제·풍랑주의보 발표"),
    )
    assert [w.name for w in active_warnings(same_title)] == ["풍랑주의보"]


def test_weather_is_judged_per_voyage():
    later = ETD + timedelta(days=3)
    between = KmaSnapshot(
        fetched_at=NOW,
        issued_at=ISSUED,
        points=[
            ForecastPoint(at=ETD, wind_speed_ms=5),
            ForecastPoint(at=ETD + timedelta(hours=36), wind_speed_ms=20),
            ForecastPoint(at=later, wind_speed_ms=5),
        ],
    )
    each = assess_each(between, {"A": ETD, "B": later}, NOW)
    assert {k: a.level for k, a in each.items()} == {"A": "normal", "B": "normal"}
    assert assess(between, [ETD, later], NOW).level == "normal"
    near_a = between.model_copy(
        update={
            "points": [
                ForecastPoint(at=ETD - timedelta(hours=3), wind_speed_ms=18),
                ForecastPoint(at=later, wind_speed_ms=5),
            ]
        }
    )
    each = assess_each(near_a, {"A": ETD, "B": later}, NOW)
    assert {k: a.level for k, a in each.items()} == {"A": "strong_wind", "B": "normal"}
    summary = combine(each)
    assert summary.level == "strong_wind" and summary.reasons[0].startswith("A: ")
    scenario = get_scenario("default")
    plan = build_plan(scenario).plan
    voyages = forecast_voyages(
        default_predictor(),
        scenario,
        plan,
        ForecastOptions(weather="auto"),
        weather_by_voyage={scenario.voyages[0].id: "strong_wind"},
    )
    levels = {v.voyage_id: v.forecast.weather for v in voyages}
    assert levels[scenario.voyages[0].id] == "strong_wind"
    assert all(level == "normal" for vid, level in levels.items() if vid != scenario.voyages[0].id)


def test_stale_snapshot_warnings_are_ignored():
    typhoon = WeatherWarning(
        title="태풍주의보 발표",
        issued_at=NOW - timedelta(days=10),
        kind="typhoon",
        name="태풍주의보",
        lifted=False,
    )
    stale = KmaSnapshot(
        fetched_at=NOW - timedelta(days=10),
        warning_checked_at=NOW - timedelta(days=10),
        points=[ForecastPoint(at=ETD, wind_speed_ms=5)],
        warnings=[typhoon],
    )
    result = assess(stale, [ETD], NOW)
    assert result.level == "normal" and result.active_warnings == []
    assert result.reasons == [] and "만료" in result.warning_note
    recent_check = stale.model_copy(update={"warning_checked_at": NOW - timedelta(hours=1)})
    result = assess(recent_check, [ETD], NOW)
    assert result.level == "normal" and result.active_warnings == []
    assert "미반영" in result.warning_note


def test_empty_forecast_retries_then_falls_back(tmp_path):
    seen: list[httpx.Request] = []
    empty = json.dumps(warning_payload()).encode()
    path = tmp_path / "kma.json"
    feed = asyncio.run(
        WeatherProvider(lambda: _raw_client(empty, seen), snapshot_path=path).get(NOW)
    )
    times = [r.url.params["base_time"] for r in seen if "VilageFcst" in r.url.path]
    assert times == ["0500", "0200"]
    assert feed.source == "unavailable" and "[03]" in feed.fallback_reason


def test_empty_snapshot_points_are_not_out_of_range():
    feed = WeatherFeed(fetched_at=NOW, source="snapshot", offline_mode=True)
    use = weather_use(ForecastOptions(weather="auto"), feed, assess(feed, [ETD], NOW))
    assert "범위 밖" not in use.note and "자료가 없어" in use.note


def test_delay_forecast_weather_auto_uses_snapshot_on_live_error(client, kma, monkeypatch):
    error = b"<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode>"
    error += b"<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
    error += b"</cmmMsgHeader></OpenAPI_ServiceResponse>"
    kma(lambda: _raw_client(error))
    snapshot = KmaSnapshot(
        fetched_at=NOW,
        issued_at=ISSUED,
        warning_checked_at=NOW,
        points=[ForecastPoint(at=ETD, wind_speed_ms=16)],
    )
    monkeypatch.setattr(kma_forecast, "load_snapshot", lambda path: snapshot)
    body = client.post(
        f"/api/plans/{_plan(client)}/delay-forecast?llm=false", json={"weather": "auto"}
    ).json()
    weather = body["weather"]
    assert weather["source"] == "snapshot" and weather["level"] == "strong_wind"
    assert "[30]" in weather["fallback_reason"] and "스냅샷" in weather["note"]
