import asyncio
import os
from datetime import date, datetime, timedelta

import httpx
import pytest

from freshswitch_backend.integrations import (
    MofVesselApiError,
    MofVesselClient,
    PlannedVoyage,
    SnapshotStore,
    VesselMovement,
    detect_schedule_changes,
    match_departures,
    normalize_vessel_name,
    parse_movements,
)
from freshswitch_backend.integrations.mof_vessel import KST

PDF_ITEM = """
<item>
  <prtAgCd>020</prtAgCd>
  <prtAgNm>부산</prtAgNm>
  <etryptYear>2017</etryptYear>
  <etryptCo>010</etryptCo>
  <clsgn>C6UM3</clsgn>
  <vsslNm>PERUVIAN REEFER</vsslNm>
  <vsslNltyCd>BS</vsslNltyCd>
  <vsslNltyNm>바하마</vsslNltyNm>
  <vsslKndCd>21</vsslKndCd>
  <vsslKndNm>산물선(벌크선)</vsslKndNm>
  <etryptPurpsCd>03</etryptPurpsCd>
  <etryptPurpsNm>적하</etryptPurpsNm>
  <prvsDpmprtPrtNm>DALIAN</prvsDpmprtPrtNm>
  <nxlnptPrtNm>DUTCHHARBOR</nxlnptPrtNm>
  <dstnPrtNm>DUTCHHARBOR</dstnPrtNm>
  <details>
    <detail>
      <reqstSeNm>최종</reqstSeNm>
      <etryndNm>입항</etryndNm>
      <etryptDt>2017-12-27T01:00:00+09:00</etryptDt>
      <laidupFcltyNm>남외항 N-3 박지</laidupFcltyNm>
      <grtg>7944</grtg>
      <tkoffPrrrnDt>2018-01-05 23:00:00</tkoffPrrrnDt>
    </detail>
    <detail>
      <reqstSeNm>최종</reqstSeNm>
      <etryndNm>출항</etryndNm>
      <tkoffDt>2018-01-05T19:00:00+09:00</tkoffDt>
      <laidupFcltyNm>감천 6 부두 3 선석</laidupFcltyNm>
      <grtg>7944</grtg>
      <dstnEtryptDt>2018-01-07 15:30:00</dstnEtryptDt>
    </detail>
  </details>
</item>
"""

LIVE_ITEM = """
<item>
  <prtAgCd>020</prtAgCd>
  <prtAgNm>부산</prtAgNm>
  <etryptYear>2026</etryptYear>
  <etryptCo>001</etryptCo>
  <clsgn>6KAQ</clsgn>
  <vsslNm>다누리</vsslNm>
  <vsslNltyNm>대한민국</vsslNltyNm>
  <vsslKndNm>원양 어선</vsslKndNm>
  <etryptPurpsNm>선박수리</etryptPurpsNm>
  <prvsDpmprtPrtNm>목포</prvsDpmprtPrtNm>
  <nxlnptPrtNm>OCEAN DISTRICT</nxlnptPrtNm>
  <dstnPrtNm>부산</dstnPrtNm>
  <details>
    <detail>
      <reqstSeNm>최초</reqstSeNm>
      <etryndNm>입항</etryndNm>
      <etryptDt>2026-09-27T08:00:00+09:00</etryptDt>
      <laidupFcltyNm>감천 3원양부두 1선석</laidupFcltyNm>
      <grtg>1911</grtg>
      <mrNum>26MMRRM006I</mrNum>
      <tkoffPrrrnDt>2026-09-30 15:00:00</tkoffPrrrnDt>
    </detail>
    <detail>
      <reqstSeNm>변경</reqstSeNm>
      <etryndNm>출항</etryndNm>
      <tkoffDt>2026-10-29T15:00:00+09:00</tkoffDt>
      <laidupFcltyNm>다대부두 1선석</laidupFcltyNm>
      <grtg>1911</grtg>
      <mrNum>26MMRRM006E</mrNum>
      <dstnEtryptDt></dstnEtryptDt>
    </detail>
  </details>
</item>
"""

ERROR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse>
<cmmMsgHeader>
  <errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg>
  <returnAuthMsg>등록되지 않은 서비스키</returnAuthMsg>
  <returnReasonCode>30</returnReasonCode>
</cmmMsgHeader>
</OpenAPI_ServiceResponse>"""


def response_xml(items: list[str], total: int, page: int = 1, rows: int = 50) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<response>
  <header><resultCode>00</resultCode><resultMsg>NORMAL_SERVICE</resultMsg></header>
  <body>
    <items>{"".join(items)}</items>
    <numOfRows>{rows}</numOfRows><pageNo>{page}</pageNo><totalCount>{total}</totalCount>
  </body>
</response>"""


def run(coro):
    return asyncio.run(coro)


def test_parse_pdf_example():
    movements, total = parse_movements(response_xml([PDF_ITEM], 1))
    assert total == 1
    m = movements[0]
    assert m.vessel_name == "PERUVIAN REEFER"
    assert m.call_sign == "C6UM3"
    assert m.arrival_berth == "남외항 N-3 박지"
    assert m.departure_berth == "감천 6 부두 3 선석"
    assert m.berth == "감천 6 부두 3 선석"
    assert m.arrival_at == datetime(2017, 12, 27, 1, 0, tzinfo=KST)
    assert m.departure_at == datetime(2018, 1, 5, 19, 0, tzinfo=KST)
    assert m.planned_departure_at == datetime(2018, 1, 5, 23, 0, tzinfo=KST)
    assert m.planned_departure_at.utcoffset().total_seconds() == 9 * 3600
    assert m.destination_eta == datetime(2018, 1, 7, 15, 30, tzinfo=KST)
    assert m.gross_tonnage == 7944.0


def test_parse_live_shape_with_empty_tags():
    movements, _ = parse_movements(response_xml([LIVE_ITEM], 1))
    m = movements[0]
    assert m.vessel_name == "다누리"
    assert m.destination_eta is None
    assert m.planned_departure_at == datetime(2026, 9, 30, 15, 0, tzinfo=KST)
    assert m.observed_departure_at == datetime(2026, 10, 29, 15, 0, tzinfo=KST)


def test_pagination_requests_all_pages():
    calls: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params)
        page = int(request.url.params["pageNo"])
        items = [PDF_ITEM] * 50 if page == 1 else [LIVE_ITEM] * 20
        return httpx.Response(200, text=response_xml(items, 70, page))

    async def go():
        async with MofVesselClient("dec+key/==", transport=httpx.MockTransport(handler)) as client:
            return await client.fetch_movements(
                start=date(2026, 10, 29), end=date(2026, 10, 30), direction="O"
            )

    movements = run(go())
    assert len(movements) == 70
    assert [p["pageNo"] for p in calls] == ["1", "2"]
    first = calls[0]
    assert first["serviceKey"] == "dec+key/=="
    assert first["prtAgCd"] == "020"
    assert first["sde"] == "20261029"
    assert first["ede"] == "20261030"
    assert first["deGb"] == "O"
    assert first["numOfRows"] == "50"
    assert "clsgn" not in first


def test_service_key_is_encoded_once():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=response_xml([], 0))

    async def go():
        async with MofVesselClient("a+b/c==", transport=httpx.MockTransport(handler)) as client:
            await client.fetch_movements(date(2026, 10, 29), date(2026, 10, 29))

    run(go())
    assert "serviceKey=a%2Bb%2Fc%3D%3D" in seen[0]


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (403, ERROR_XML, "30"),
        (
            200,
            (
                "<response><header><resultCode>22</resultCode>"
                "<resultMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</resultMsg>"
                "</header></response>"
            ),
            "22",
        ),
        (200, "not xml", "99"),
    ],
)
def test_error_codes_raise_typed_exception(status: int, body: str, code: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    async def go():
        async with MofVesselClient("k", transport=httpx.MockTransport(handler)) as client:
            await client.fetch_movements(date(2026, 10, 29), date(2026, 10, 29))

    with pytest.raises(MofVesselApiError) as info:
        run(go())
    assert info.value.code == code
    assert "해수부 선박입출항 API 오류" in str(info.value)


def test_normalize_vessel_name():
    assert normalize_vessel_name("MV  Peruvian Reefer") == "PERUVIANREEFER"
    assert normalize_vessel_name("m/v peruvian   reefer") == "PERUVIANREEFER"
    assert normalize_vessel_name("다누리") == "다누리"


def movement(name: str, departure: datetime | None, planned: datetime | None = None):
    return VesselMovement(
        port_code="020",
        call_sign="X1",
        vessel_name=name,
        departure_at=departure,
        planned_departure_at=planned,
    )


def test_detect_schedule_changes():
    movements = [
        movement("PERUVIAN REEFER", datetime(2026, 10, 30, 18, 0, tzinfo=KST)),
        movement("PERUVIAN REEFER", datetime(2026, 11, 20, 18, 0, tzinfo=KST)),
        movement("다누리", None, datetime(2026, 10, 30, 13, 0, tzinfo=KST)),
        movement("UNRELATED", datetime(2026, 10, 30, 0, 0, tzinfo=KST)),
    ]
    voyages = [
        ("V1", "MV Peruvian Reefer", datetime(2026, 10, 30, 12, 0, tzinfo=KST)),
        PlannedVoyage(voyage_id="V2", vessel_name="다누리", etd=datetime(2026, 10, 30, 11, 0)),
        ("V3", "GHOST SHIP", datetime(2026, 10, 30, 12, 0, tzinfo=KST)),
    ]
    changes = detect_schedule_changes(voyages, movements)
    assert [c.voyage_id for c in changes] == ["V1"]
    change = changes[0]
    assert change.delta_hours == 6.0
    assert change.observed_etd == datetime(2026, 10, 30, 18, 0, tzinfo=KST)
    assert change.source == "해수부 선박입출항"

    loose = detect_schedule_changes(voyages, movements, threshold_hours=2)
    assert {c.voyage_id: c.delta_hours for c in loose} == {"V1": 6.0, "V2": 2.0}


def test_detect_negative_delta():
    movements = [movement("다누리", datetime(2026, 10, 30, 6, 0, tzinfo=KST))]
    changes = detect_schedule_changes(
        [("V9", "다누리", datetime(2026, 10, 30, 12, 0, tzinfo=KST))], movements
    )
    assert changes[0].delta_hours == -6.0


def _real_movements() -> list[VesselMovement]:
    snapshot = SnapshotStore().load(date(2026, 10, 29), "O")
    assert snapshot is not None
    return snapshot.movements


def test_detect_matches_real_snapshot_record_by_call_sign():
    movements = _real_movements()
    real = next(m for m in movements if m.call_sign == "5LYK5")
    assert real.vessel_name == "MSC SALERNO" and real.departure_at is not None
    planned = datetime(2026, 10, 28, 18, 0, tzinfo=KST)
    voyage = PlannedVoyage(
        voyage_id="V-REAL", vessel_name="MV FRESH HARMONY", etd=planned, call_sign="5lyk5"
    )
    changes = detect_schedule_changes([voyage], movements)
    assert len(changes) == 1
    change = changes[0]
    assert change.matched_by == "call_sign" and change.call_sign == "5LYK5"
    assert change.observed_etd == real.departure_at
    assert change.delta_hours == round((real.departure_at - planned).total_seconds() / 3600, 2)
    unnamed = voyage.model_copy(update={"call_sign": None})
    assert detect_schedule_changes([unnamed], movements) == []


def test_call_sign_wins_over_name_and_name_is_fallback():
    when = datetime(2026, 11, 18, 18, 0, tzinfo=KST)
    movements = [
        VesselMovement(
            port_code="020",
            call_sign="AAA1",
            vessel_name="SAME NAME",
            departure_at=when + timedelta(hours=10),
        ),
        VesselMovement(
            port_code="020",
            call_sign="BBB2",
            vessel_name="SAME NAME",
            departure_at=when + timedelta(hours=4),
        ),
    ]
    by_call = match_departures(
        [PlannedVoyage(voyage_id="V1", vessel_name="SAME NAME", etd=when, call_sign="AAA1")],
        movements,
    )
    assert [(m.matched_by, m.movement.call_sign) for m in by_call] == [("call_sign", "AAA1")]
    other_ship = match_departures(
        [PlannedVoyage(voyage_id="V1", vessel_name="SAME NAME", etd=when, call_sign="ZZZ9")],
        movements,
    )
    assert other_ship == []
    unsigned = VesselMovement(
        port_code="020",
        call_sign="",
        vessel_name="SAME NAME",
        departure_at=when + timedelta(hours=6),
    )
    fallback = match_departures(
        [PlannedVoyage(voyage_id="V1", vessel_name="SAME NAME", etd=when, call_sign="ZZZ9")],
        [*movements, unsigned],
    )
    assert [(m.matched_by, m.movement.call_sign) for m in fallback] == [("vessel_name", "")]
    no_sign = match_departures(
        [PlannedVoyage(voyage_id="V1", vessel_name="SAME NAME", etd=when)], movements
    )
    assert [(m.matched_by, m.movement.call_sign) for m in no_sign] == [("vessel_name", "BBB2")]


@pytest.mark.skipif(
    not os.environ.get("DATA_GO_KR_SERVICE_KEY"), reason="DATA_GO_KR_SERVICE_KEY 미설정"
)
def test_live_fetch_busan_departures():
    async def go():
        async with MofVesselClient(os.environ["DATA_GO_KR_SERVICE_KEY"], timeout=30) as client:
            today = datetime.now(KST).date()
            return await client.fetch_movements(today, today, direction="O")

    movements = run(go())
    assert all(m.port_code == "020" for m in movements)
    assert all(m.departure_at is None or m.departure_at.tzinfo for m in movements)
