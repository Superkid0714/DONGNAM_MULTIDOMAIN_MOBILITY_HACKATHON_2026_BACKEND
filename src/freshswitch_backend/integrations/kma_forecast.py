import asyncio
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

import httpx
from pydantic import BaseModel

from freshswitch_backend.config import get_settings
from freshswitch_backend.integrations.errors import public_error
from freshswitch_backend.integrations.mof_snapshot import SNAPSHOT_ROOT
from freshswitch_backend.integrations.mof_vessel import KST

BASE_URL = "https://apis.data.go.kr/1360000"
FORECAST_PATH = "/VilageFcstInfoService_2.0/getVilageFcst"
WARNING_PATH = "/WthrWrnInfoService/getWthrWrnList"
FORECAST_DATASET = "기상청_단기예보 조회서비스"
FORECAST_DATASET_URL = "https://www.data.go.kr/data/15084084/openapi.do"
WARNING_DATASET = "기상청_기상특보 조회서비스"
WARNING_DATASET_URL = "https://www.data.go.kr/data/15000415/openapi.do"
WARNING_STANDARD_URL = "https://www.weather.go.kr/w/weather/warning/standard.do"
NEWPORT_LAT = 35.0756
NEWPORT_LON = 128.8222
NEWPORT_NX = 93
NEWPORT_NY = 73
BUSAN_STN_ID = "159"
BASE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
PUBLISH_DELAY = timedelta(minutes=15)
FORECAST_ROWS = 1000
WARNING_ROWS = 100
WARNING_LOOKBACK = timedelta(days=3)
WARNING_HORIZON = timedelta(hours=48)
STRONG_WIND_MS = 14.0
HIGH_WAVE_M = 3.0
WINDOW_BEFORE = timedelta(hours=24)
WINDOW_AFTER = timedelta(hours=6)
LIVE_TIMEOUT_SECONDS = 8.0
LIVE_REQUEST_TIMEOUT_SECONDS = 4.0
NO_DATA = "03"
KMA_SNAPSHOT_PATH = SNAPSHOT_ROOT / "kma" / "busan-newport.json"

Weather = Literal["normal", "strong_wind", "typhoon"]
WarningKind = Literal["typhoon", "wind_wave", "other"]
WeatherSource = Literal["live", "snapshot", "unavailable"]
SEVERITY: dict[Weather, int] = {"normal": 0, "strong_wind": 1, "typhoon": 2}
_SEGMENT = re.compile(r"[·,ㆍ]")
_ARROW = re.compile(r"→|->")
_DECOR = re.compile(r"[()*]")
_ACTION = re.compile(r"\s*(발표|해제|변경|연장).*$")
_GRADE = re.compile(r"(주의보|경보)$")


class KmaApiError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"기상청 API 오류 [{code}]{suffix}")


class MissingKmaKey(Exception):
    def __init__(self) -> None:
        super().__init__("공공데이터포털 키(DATA_GO_KR_SERVICE_KEY)가 설정되지 않았습니다")


class ForecastPoint(BaseModel):
    at: datetime
    wind_speed_ms: float | None = None
    wave_height_m: float | None = None


class WeatherWarning(BaseModel):
    title: str
    issued_at: datetime
    kind: WarningKind
    name: str
    lifted: bool

    @property
    def phenomenon(self) -> str:
        return _GRADE.sub("", self.name)


class KmaSnapshot(BaseModel):
    fetched_at: datetime
    issued_at: datetime | None = None
    warning_checked_at: datetime | None = None
    nx: int = NEWPORT_NX
    ny: int = NEWPORT_NY
    stn_id: str = BUSAN_STN_ID
    points: list[ForecastPoint] = []
    warnings: list[WeatherWarning] = []


class WeatherFeed(KmaSnapshot):
    source: WeatherSource
    offline_mode: bool
    fallback_reason: str | None = None


class WeatherAssessment(BaseModel):
    level: Weather
    covered: bool
    horizon_end: datetime | None = None
    max_wind_speed_ms: float | None = None
    max_wave_height_m: float | None = None
    active_warnings: list[str] = []
    reasons: list[str] = []
    warning_note: str | None = None


def base_time(now: datetime) -> datetime:
    local = (now - PUBLISH_DELAY).astimezone(KST)
    for hour in reversed(BASE_HOURS):
        if local.hour >= hour:
            return local.replace(hour=hour, minute=0, second=0, microsecond=0)
    previous = local - timedelta(days=1)
    return previous.replace(hour=BASE_HOURS[-1], minute=0, second=0, microsecond=0)


def _kst(day: str, hhmm: str) -> datetime:
    return datetime.strptime(f"{day}{hhmm[:4]}", "%Y%m%d%H%M").replace(tzinfo=KST)


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _items(payload: str | bytes) -> list[dict[str, Any]]:
    raw = payload.encode() if isinstance(payload, str) else payload
    text = raw.lstrip()
    if text.startswith(b"<"):
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise KmaApiError("99", "응답 파싱 실패") from exc
        code = (
            root.findtext("cmmMsgHeader/returnReasonCode")
            or root.findtext("header/resultCode")
            or "99"
        )
        if code == NO_DATA:
            return []
        if code not in ("00", "0"):
            raise KmaApiError(
                code,
                root.findtext("cmmMsgHeader/returnAuthMsg") or root.findtext("header/resultMsg"),
            )
        return [{child.tag: child.text for child in item} for item in root.iter("item")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise KmaApiError("99", "응답 파싱 실패") from exc
    if not isinstance(data, dict):
        raise KmaApiError("99", "응답 파싱 실패")
    root = data.get("response", data)
    if not isinstance(root, dict):
        raise KmaApiError("99", "응답 파싱 실패")
    header = root.get("header")
    header = header if isinstance(header, dict) else {}
    code = str(header.get("resultCode", "00"))
    if code == NO_DATA:
        return []
    if code not in ("00", "0"):
        raise KmaApiError(code, header.get("resultMsg"))
    body = root.get("body")
    body = body if isinstance(body, dict) else {}
    items = body.get("items") or []
    if isinstance(items, dict):
        items = items.get("item") or []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def parse_forecast(payload: str | bytes) -> tuple[list[ForecastPoint], datetime | None]:
    points: dict[datetime, ForecastPoint] = {}
    issued: datetime | None = None
    for item in _items(payload):
        category = item.get("category")
        if category not in ("WSD", "WAV"):
            continue
        try:
            at = _kst(str(item["fcstDate"]), str(item["fcstTime"]).zfill(4))
            if item.get("baseDate") and item.get("baseTime"):
                issued = _kst(str(item["baseDate"]), str(item["baseTime"]).zfill(4))
        except (KeyError, ValueError):
            continue
        point = points.setdefault(at, ForecastPoint(at=at))
        value = _number(item.get("fcstValue"))
        if category == "WSD":
            point.wind_speed_ms = value
        else:
            point.wave_height_m = value
    return sorted(points.values(), key=lambda p: p.at), issued


def _warning_kind(name: str) -> WarningKind:
    if "태풍" in name:
        return "typhoon"
    if "풍랑" in name or "강풍" in name:
        return "wind_wave"
    return "other"


def parse_warnings(payload: str | bytes) -> list[WeatherWarning]:
    warnings: list[WeatherWarning] = []
    for item in _items(payload):
        title = str(item.get("title") or "").strip()
        stamp = str(item.get("tmFc") or "").strip()
        if not title or len(stamp) < 12:
            continue
        try:
            issued = _kst(stamp[:8], stamp[8:12])
        except ValueError:
            continue
        body = title.split("/", 1)[-1]
        for segment in _SEGMENT.split(body):
            text = _ARROW.split(segment)[-1]
            lifted = "해제" in text
            name = _ACTION.sub("", _DECOR.sub("", text).strip()).strip()
            if not name.endswith(("주의보", "경보")):
                continue
            warnings.append(
                WeatherWarning(
                    title=title,
                    issued_at=issued,
                    kind=_warning_kind(name),
                    name=name,
                    lifted=lifted,
                )
            )
    return sorted(warnings, key=lambda w: w.issued_at)


def active_warnings(warnings: Iterable[WeatherWarning]) -> list[WeatherWarning]:
    latest: dict[str, WeatherWarning] = {}
    for warning in sorted(warnings, key=lambda w: w.issued_at):
        latest[warning.phenomenon] = warning
    return [w for w in latest.values() if not w.lifted and w.kind != "other"]


class KmaForecastClient:
    def __init__(
        self,
        service_key: str,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._service_key = service_key
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, str | int]) -> bytes:
        response = await self._client.get(
            path, params={"serviceKey": self._service_key, "dataType": "JSON", **params}
        )
        head = response.content.lstrip()[:1]
        if response.is_error and head not in (b"{", b"<"):
            response.raise_for_status()
        return response.content

    async def fetch_forecast(
        self, issued: datetime, nx: int = NEWPORT_NX, ny: int = NEWPORT_NY
    ) -> tuple[list[ForecastPoint], datetime | None]:
        local = issued.astimezone(KST)
        payload = await self._get(
            FORECAST_PATH,
            {
                "pageNo": 1,
                "numOfRows": FORECAST_ROWS,
                "base_date": local.strftime("%Y%m%d"),
                "base_time": local.strftime("%H00"),
                "nx": nx,
                "ny": ny,
            },
        )
        return parse_forecast(payload)

    async def fetch_warnings(
        self, start: datetime, end: datetime, stn_id: str = BUSAN_STN_ID
    ) -> list[WeatherWarning]:
        payload = await self._get(
            WARNING_PATH,
            {
                "pageNo": 1,
                "numOfRows": WARNING_ROWS,
                "stnId": stn_id,
                "fromTmFc": start.astimezone(KST).strftime("%Y%m%d"),
                "toTmFc": end.astimezone(KST).strftime("%Y%m%d"),
            },
        )
        return parse_warnings(payload)

    async def fetch(self, now: datetime) -> KmaSnapshot:
        issued = base_time(now)
        points, reported = await self.fetch_forecast(issued)
        if not points:
            points, reported = await self.fetch_forecast(base_time(issued - timedelta(minutes=1)))
        if not points:
            raise KmaApiError(NO_DATA, "단기예보 자료 없음")
        warnings = await self.fetch_warnings(now - WARNING_LOOKBACK, now)
        return KmaSnapshot(
            fetched_at=datetime.now(UTC),
            issued_at=reported,
            warning_checked_at=now,
            points=points,
            warnings=warnings,
        )


def load_snapshot(path: Path = KMA_SNAPSHOT_PATH) -> KmaSnapshot | None:
    if not path.is_file():
        return None
    return KmaSnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def save_snapshot(snapshot: KmaSnapshot, path: Path = KMA_SNAPSHOT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=1, exclude_none=True) + "\n", encoding="utf-8")
    return path


LIVE_ERRORS = (MissingKmaKey, KmaApiError, httpx.HTTPError, TimeoutError, OSError, ValueError)


def _reason(exc: BaseException) -> str:
    if isinstance(exc, MissingKmaKey):
        return "기상청 API 키가 설정되지 않았습니다"
    return public_error(exc, "기상청 API")


class WeatherProvider:
    def __init__(
        self,
        client_factory: Callable[[], KmaForecastClient] | None,
        offline_mode: bool = False,
        snapshot_path: Path = KMA_SNAPSHOT_PATH,
        live_timeout: float = LIVE_TIMEOUT_SECONDS,
    ) -> None:
        self._factory = client_factory
        self._offline = offline_mode
        self._path = snapshot_path
        self._timeout = live_timeout

    async def _live(self, now: datetime) -> KmaSnapshot:
        if self._factory is None:
            raise MissingKmaKey()
        async with self._factory() as client:
            return await asyncio.wait_for(client.fetch(now), self._timeout)

    async def get(self, now: datetime) -> WeatherFeed:
        reason = "오프라인 모드" if self._offline else None
        if reason is None:
            try:
                live = await self._live(now)
                return WeatherFeed(**live.model_dump(), source="live", offline_mode=False)
            except LIVE_ERRORS as exc:
                reason = _reason(exc)
        try:
            snapshot = load_snapshot(self._path)
        except (OSError, ValueError):
            snapshot = None
        fallback = None if self._offline else reason
        if snapshot is None:
            return WeatherFeed(
                fetched_at=now,
                source="unavailable",
                offline_mode=self._offline,
                fallback_reason=f"{reason}, 저장된 기상청 스냅샷이 없습니다",
            )
        return WeatherFeed(
            **snapshot.model_dump(),
            source="snapshot",
            offline_mode=self._offline,
            fallback_reason=fallback,
        )


def _recent_warnings(feed: KmaSnapshot, now: datetime) -> tuple[list[WeatherWarning], str | None]:
    checked = feed.warning_checked_at or feed.fetched_at
    if now - checked > WARNING_LOOKBACK:
        stale = active_warnings(feed.warnings)
        if not stale:
            return [], None
        names = ", ".join(w.name for w in stale)
        return [], f"스냅샷 특보({names}, {checked.astimezone(KST):%m-%d} 확인) 만료로 미반영"
    recent = [w for w in feed.warnings if now - WARNING_LOOKBACK <= w.issued_at <= now]
    kept = active_warnings(recent)
    expired = [w for w in active_warnings(feed.warnings) if w not in kept]
    if not expired:
        return recent, None
    names = ", ".join(f"{w.name}({w.issued_at.astimezone(KST):%m-%d} 발표)" for w in expired)
    return recent, f"발표 {WARNING_LOOKBACK.days}일 지난 특보 {names} 미반영"


def assess(feed: KmaSnapshot, etds: Iterable[datetime], now: datetime) -> WeatherAssessment:
    times = sorted(etds)
    horizon = max((p.at for p in feed.points), default=None)
    if not times:
        return WeatherAssessment(level="normal", covered=False, horizon_end=horizon)
    window = [
        p for p in feed.points if any(t - WINDOW_BEFORE <= p.at <= t + WINDOW_AFTER for t in times)
    ]
    winds = [p.wind_speed_ms for p in window if p.wind_speed_ms is not None]
    waves = [p.wave_height_m for p in window if p.wave_height_m is not None]
    level: Weather = "normal"
    reasons: list[str] = []

    def raise_to(value: Weather, reason: str) -> None:
        nonlocal level
        reasons.append(reason)
        if SEVERITY[value] > SEVERITY[level]:
            level = value

    wind = max(winds, default=None)
    wave = max(waves, default=None)
    if wind is not None and wind >= STRONG_WIND_MS:
        raise_to("strong_wind", f"예보 풍속 최대 {wind:g}m/s ≥ {STRONG_WIND_MS:g}m/s")
    if wave is not None and wave >= HIGH_WAVE_M:
        raise_to("strong_wind", f"예보 파고 최대 {wave:g}m ≥ {HIGH_WAVE_M:g}m")
    recent, warning_note = _recent_warnings(feed, now)
    active = active_warnings(recent)
    if times[0] - now <= WARNING_HORIZON:
        for warning in active:
            value: Weather = "typhoon" if warning.kind == "typhoon" else "strong_wind"
            raise_to(value, f"{warning.name} 발효 중({warning.issued_at:%m-%d %H:%M} 발표)")
    return WeatherAssessment(
        level=level,
        covered=bool(window),
        horizon_end=horizon,
        max_wind_speed_ms=wind,
        max_wave_height_m=wave,
        active_warnings=[w.name for w in active],
        reasons=reasons,
        warning_note=warning_note,
    )


def assess_each(
    feed: KmaSnapshot, etds: Mapping[str, datetime], now: datetime
) -> dict[str, WeatherAssessment]:
    return {voyage_id: assess(feed, [etd], now) for voyage_id, etd in etds.items()}


def combine(assessments: Mapping[str, WeatherAssessment]) -> WeatherAssessment:
    items = list(assessments.items())
    if not items:
        return WeatherAssessment(level="normal", covered=False)
    level = max((a.level for _, a in items), key=lambda w: SEVERITY[w])
    winds = [a.max_wind_speed_ms for _, a in items if a.max_wind_speed_ms is not None]
    waves = [a.max_wave_height_m for _, a in items if a.max_wave_height_m is not None]
    names = list(dict.fromkeys(n for _, a in items for n in a.active_warnings))
    many = len(items) > 1
    reasons = [f"{vid}: {r}" if many else r for vid, a in items for r in a.reasons]
    return WeatherAssessment(
        level=level,
        covered=any(a.covered for _, a in items),
        horizon_end=items[0][1].horizon_end,
        max_wind_speed_ms=max(winds, default=None),
        max_wave_height_m=max(waves, default=None),
        active_warnings=names,
        reasons=reasons,
        warning_note=next((a.warning_note for _, a in items if a.warning_note), None),
    )


def main() -> int:
    key = get_settings().data_go_kr_service_key
    if not key:
        print("DATA_GO_KR_SERVICE_KEY가 비어 있습니다", file=sys.stderr)
        return 1

    async def run() -> KmaSnapshot:
        async with KmaForecastClient(key, timeout=30) as client:
            return await client.fetch(datetime.now(UTC))

    try:
        snapshot = asyncio.run(run())
    except KmaApiError as exc:
        print(
            f"{exc}\n활용신청: {FORECAST_DATASET} {FORECAST_DATASET_URL}, "
            f"{WARNING_DATASET} {WARNING_DATASET_URL}",
            file=sys.stderr,
        )
        return 2
    path = save_snapshot(snapshot)
    print(f"기상청 단기예보 {len(snapshot.points)}시점·특보 {len(snapshot.warnings)}건 -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
