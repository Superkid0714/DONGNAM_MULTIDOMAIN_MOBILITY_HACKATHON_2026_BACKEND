import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Self

import httpx
from pydantic import BaseModel

BASE_URL = "http://apis.data.go.kr/1192000/VsslEtrynd5"
INFO_PATH = "/Info5"
BUSAN_PORT_CODE = "020"
MAX_ROWS_PER_PAGE = 50
SOURCE_NAME = "해수부 선박입출항"
KST = timezone(timedelta(hours=9))
REQUEST_KIND_RANK = {"최초": 1, "변경": 2, "최종": 3}

ERROR_MESSAGES: dict[str, str] = {
    "01": "어플리케이션 에러",
    "10": "유효하지 않은 서비스키입니다",
    "11": "잘못된 요청 파라미터입니다",
    "12": "해당 오픈 API 서비스가 없거나 폐기되었습니다",
    "20": "서비스 접근이 거부되었습니다",
    "22": "서비스 요청 제한 횟수를 초과했습니다",
    "30": "등록되지 않은 서비스키입니다",
    "31": "기한이 만료된 서비스키입니다",
    "32": "등록되지 않은 IP입니다",
    "99": "기타 에러",
}

Direction = Literal["I", "O"]


class MofVesselApiError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        self.message = ERROR_MESSAGES.get(code, "알 수 없는 에러")
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"해수부 선박입출항 API 오류 [{code}] {self.message}{suffix}")


class VesselMovement(BaseModel):
    port_code: str
    port_name: str | None = None
    call_sign: str
    vessel_name: str | None = None
    entry_year: str | None = None
    entry_count: str | None = None
    nationality: str | None = None
    vessel_kind: str | None = None
    purpose: str | None = None
    previous_port: str | None = None
    next_port: str | None = None
    destination_port: str | None = None
    arrival_berth: str | None = None
    departure_berth: str | None = None
    arrival_at: datetime | None = None
    departure_at: datetime | None = None
    planned_departure_at: datetime | None = None
    destination_eta: datetime | None = None
    gross_tonnage: float | None = None

    @property
    def berth(self) -> str | None:
        return self.departure_berth or self.arrival_berth

    @property
    def observed_departure_at(self) -> datetime | None:
        return self.departure_at or self.planned_departure_at


class PlannedVoyage(BaseModel):
    voyage_id: str
    vessel_name: str
    etd: datetime
    call_sign: str | None = None


MatchedBy = Literal["call_sign", "vessel_name"]


class ScheduleChange(BaseModel):
    voyage_id: str
    vessel_name: str
    call_sign: str | None = None
    planned_etd: datetime
    observed_etd: datetime
    delta_hours: float
    matched_by: MatchedBy | None = None
    source: str = SOURCE_NAME


def parse_kst_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace(" ", "T", 1) if "T" not in text else text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _text(element: ET.Element | None, tag: str) -> str | None:
    if element is None:
        return None
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _pick_detail(details: list[ET.Element], kind: str) -> ET.Element | None:
    matches = [d for d in details if _text(d, "etryndNm") == kind]
    if not matches:
        return None
    return max(
        enumerate(matches),
        key=lambda pair: (REQUEST_KIND_RANK.get(_text(pair[1], "reqstSeNm") or "", 0), pair[0]),
    )[1]


def _parse_item(item: ET.Element) -> VesselMovement:
    details = item.findall("details/detail")
    arrival = _pick_detail(details, "입항")
    departure = _pick_detail(details, "출항")
    any_detail = departure if departure is not None else arrival
    return VesselMovement(
        port_code=_text(item, "prtAgCd") or "",
        port_name=_text(item, "prtAgNm"),
        call_sign=_text(item, "clsgn") or "",
        vessel_name=_text(item, "vsslNm"),
        entry_year=_text(item, "etryptYear"),
        entry_count=_text(item, "etryptCo"),
        nationality=_text(item, "vsslNltyNm"),
        vessel_kind=_text(item, "vsslKndNm"),
        purpose=_text(item, "etryptPurpsNm"),
        previous_port=_text(item, "prvsDpmprtPrtNm"),
        next_port=_text(item, "nxlnptPrtNm"),
        destination_port=_text(item, "dstnPrtNm"),
        arrival_berth=_text(arrival, "laidupFcltyNm"),
        departure_berth=_text(departure, "laidupFcltyNm"),
        arrival_at=parse_kst_datetime(_text(arrival, "etryptDt")),
        departure_at=parse_kst_datetime(_text(departure, "tkoffDt")),
        planned_departure_at=parse_kst_datetime(_text(arrival, "tkoffPrrrnDt")),
        destination_eta=parse_kst_datetime(_text(departure, "dstnEtryptDt")),
        gross_tonnage=_float(_text(any_detail, "grtg")),
    )


def _parse_root(payload: str | bytes) -> ET.Element:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise MofVesselApiError("99", "XML 파싱 실패") from exc
    if root.tag == "OpenAPI_ServiceResponse":
        code = _text(root, "cmmMsgHeader/returnReasonCode") or "99"
        raise MofVesselApiError(code, _text(root, "cmmMsgHeader/returnAuthMsg"))
    if root.tag != "response":
        raise MofVesselApiError("99", f"예상하지 못한 응답 루트 <{root.tag}>")
    code = _text(root, "header/resultCode")
    if code is not None and code != "00":
        raise MofVesselApiError(code, _text(root, "header/resultMsg"))
    return root


def parse_movements(payload: str | bytes) -> tuple[list[VesselMovement], int]:
    root = _parse_root(payload)
    items = [_parse_item(item) for item in root.findall("body/items/item")]
    total = int(_text(root, "body/totalCount") or len(items))
    return items, total


class MofVesselClient:
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
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_movements(
        self,
        start: date,
        end: date,
        port_code: str = BUSAN_PORT_CODE,
        direction: Direction | None = None,
        call_sign: str | None = None,
    ) -> list[VesselMovement]:
        params: dict[str, str | int] = {
            "serviceKey": self._service_key,
            "prtAgCd": port_code,
            "sde": start.strftime("%Y%m%d"),
            "ede": end.strftime("%Y%m%d"),
            "numOfRows": MAX_ROWS_PER_PAGE,
        }
        if direction is not None:
            params["deGb"] = direction
        if call_sign:
            params["clsgn"] = call_sign
        movements: list[VesselMovement] = []
        page = 1
        while True:
            response = await self._client.get(INFO_PATH, params={**params, "pageNo": page})
            if response.is_error and b"<" not in response.content[:200]:
                response.raise_for_status()
            items, total = parse_movements(response.content)
            movements.extend(items)
            if not items or len(movements) >= total:
                return movements
            page += 1


MAX_GAP_HOURS = 72.0
_PREFIX = re.compile(r"^(M/?V|M\.V\.?)\s+")


def normalize_vessel_name(name: str) -> str:
    collapsed = " ".join(name.upper().split())
    return _PREFIX.sub("", collapsed).replace(" ", "")


def normalize_call_sign(value: str) -> str:
    return "".join(value.upper().split())


def _as_voyage(entry: PlannedVoyage | tuple[str, str, datetime]) -> PlannedVoyage:
    if isinstance(entry, PlannedVoyage):
        return entry
    voyage_id, vessel_name, etd = entry
    return PlannedVoyage(voyage_id=voyage_id, vessel_name=vessel_name, etd=etd)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=KST)


class DepartureMatch(BaseModel):
    voyage: PlannedVoyage
    movement: VesselMovement
    matched_by: MatchedBy
    planned_etd: datetime
    observed_etd: datetime

    @property
    def delta_hours(self) -> float:
        return (self.observed_etd - self.planned_etd).total_seconds() / 3600


def _index(
    movements: Sequence[VesselMovement],
) -> tuple[dict[str, list[VesselMovement]], dict[str, list[VesselMovement]]]:
    by_call: dict[str, list[VesselMovement]] = {}
    by_name: dict[str, list[VesselMovement]] = {}
    for movement in movements:
        if movement.observed_departure_at is None:
            continue
        if movement.call_sign:
            by_call.setdefault(normalize_call_sign(movement.call_sign), []).append(movement)
        if movement.vessel_name:
            by_name.setdefault(normalize_vessel_name(movement.vessel_name), []).append(movement)
    return by_call, by_name


def _near(
    pool: list[VesselMovement], planned: datetime, taken: set[int], max_gap: timedelta
) -> list[tuple[timedelta, datetime, VesselMovement]]:
    aware = [
        (_aware(m.observed_departure_at), m)
        for m in pool
        if id(m) not in taken and m.observed_departure_at is not None
    ]
    return [
        (abs(observed - planned), observed, m)
        for observed, m in aware
        if abs(observed - planned) <= max_gap
    ]


def match_departures(
    voyages: Iterable[PlannedVoyage | tuple[str, str, datetime]],
    movements: Sequence[VesselMovement],
    max_gap_hours: float = MAX_GAP_HOURS,
) -> list[DepartureMatch]:
    by_call, by_name = _index(movements)
    taken: set[int] = set()
    max_gap = timedelta(hours=max_gap_hours)
    matches: list[DepartureMatch] = []
    for voyage in sorted(map(_as_voyage, voyages), key=lambda v: _aware(v.etd)):
        planned = _aware(voyage.etd)
        matched_by: MatchedBy = "call_sign"
        own = normalize_call_sign(voyage.call_sign) if voyage.call_sign else None
        pool = by_call.get(own, []) if own else []
        candidates = _near(pool, planned, taken, max_gap)
        if not candidates:
            matched_by = "vessel_name"
            pool = [
                m
                for m in by_name.get(normalize_vessel_name(voyage.vessel_name), [])
                if own is None or not m.call_sign or normalize_call_sign(m.call_sign) == own
            ]
            candidates = _near(pool, planned, taken, max_gap)
        if not candidates:
            continue
        _, observed, best = min(candidates, key=lambda c: c[0])
        taken.add(id(best))
        matches.append(
            DepartureMatch(
                voyage=voyage,
                movement=best,
                matched_by=matched_by,
                planned_etd=planned,
                observed_etd=observed,
            )
        )
    return matches


def detect_schedule_changes(
    voyages: Iterable[PlannedVoyage | tuple[str, str, datetime]],
    movements: Sequence[VesselMovement],
    threshold_hours: float = 3.0,
    max_gap_hours: float = MAX_GAP_HOURS,
) -> list[ScheduleChange]:
    return [
        ScheduleChange(
            voyage_id=match.voyage.voyage_id,
            vessel_name=match.voyage.vessel_name,
            call_sign=match.movement.call_sign or None,
            planned_etd=match.planned_etd,
            observed_etd=match.observed_etd,
            delta_hours=round(match.delta_hours, 2),
            matched_by=match.matched_by,
        )
        for match in match_departures(voyages, movements, max_gap_hours)
        if abs(match.delta_hours) >= threshold_hours
    ]
