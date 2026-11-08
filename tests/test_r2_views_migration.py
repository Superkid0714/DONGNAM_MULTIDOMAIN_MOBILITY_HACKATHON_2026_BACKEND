import json
from datetime import UTC, datetime

from alembic import command
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from saas_fixtures import PLATFORM_ORG_ID
from sqlalchemy import text
from test_deep_review_views import CANCEL, _apply, _renamed, _session
from test_saas_api import _assign, _platform_plan
from test_saas_e2e_round1 import _confirm_all

from freshswitch_backend.api import saas_views
from freshswitch_backend.execution.models import ExecutionOrder, ExternalRequest, OrderLogEntry
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.database import migration_config
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.planning.store import PlanSession, store

AT = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)
OTHER_NOTE = "메모 REQ-2 산청 딸기 수출농가 B 운임 3,200,000원 lot-sancheong-sb-1"
OWN_NOTE = "진주 건 메모"
PRE_0012_STATUSES = {"committed", "pending_confirmation"}


def _request(request_id: str, lot_ids: list[str]) -> ExternalRequest:
    return ExternalRequest(
        request_id=request_id,
        kind="air_cancel",
        counterparty_role="airline",
        counterparty="항공사",
        channel="email",
        deadline=AT,
        lot_ids=lot_ids,
        text=f"[항공 예약 취소 요청] {', '.join(lot_ids)}",
    )


def _reply(request_id: str, accepted: bool, note: str) -> OrderLogEntry:
    action = f"{request_id} 항공사 수락" if accepted else f"{request_id} 항공사 거절 → 재계획 필요"
    return OrderLogEntry(
        at=AT, actor="forwarder", action=action, status_after="forwarder_confirmed", note=note
    )


def _order_with_replies(
    platform_client: TestClient, saas_login
) -> tuple[PlanSession, ExecutionOrder]:
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    order_id = _apply(platform_client, plan_id, CANCEL)
    order = store.order(plan_id, order_id, org_id=PLATFORM_ORG_ID)
    confirmed = OrderLogEntry(
        at=AT,
        actor="forwarder",
        action="포워더 확정 → 계획 확정, 자동 조치 실행, 제3자 요청 발송",
        status_after="forwarder_confirmed",
    )
    order = order.model_copy(
        update={
            "requests": [
                _request("REQ-1", ["lot-jinju-sb-1"]),
                _request("REQ-2", ["lot-miryang-sb-2"]),
            ],
            "log": [
                *order.log,
                confirmed,
                _reply("REQ-1", True, OWN_NOTE),
                _reply("REQ-2", False, OTHER_NOTE),
            ],
        }
    )
    return _session(plan_id), order


def test_shipper_order_log_drops_replies_for_other_shippers_requests(
    platform_client: TestClient, saas_login
) -> None:
    session, order = _order_with_replies(platform_client, saas_login)

    view = saas_views.shipper_order_view(session, order, {"sh-jinju"})
    assert [r["request_id"] for r in view["requests"]] == ["REQ-1"]
    actions = [e["action"] for e in view["log"]]
    assert "REQ-1 항공사 수락" in actions
    assert "포워더 확정 → 계획 확정, 자동 조치 실행, 제3자 요청 발송" in actions
    dumped = json.dumps(view["log"], ensure_ascii=False)
    assert "REQ-2" not in dumped
    assert "3,200,000" not in dumped
    assert OWN_NOTE in dumped

    listed = saas_views.anonymized_orders(session, [order], {"sh-jinju"})
    assert "REQ-2" not in json.dumps(listed[0]["log"], ensure_ascii=False)

    miryang = saas_views.shipper_order_view(session, order, {"sh-miryang"})
    assert [r["request_id"] for r in miryang["requests"]] == ["REQ-2"]
    replies = [e for e in miryang["log"] if e["action"].startswith("REQ-")]
    assert [e["action"] for e in replies] == ["REQ-2 항공사 거절 → 재계획 필요"]
    assert "3,200,000원" in replies[0]["note"]
    assert "REQ-1" not in json.dumps(miryang["log"], ensure_ascii=False)


def test_real_forwarder_replies_for_other_shippers_requests_stay_hidden(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    _assign(plan_id)
    order_id = _apply(platform_client, plan_id, CANCEL)
    _confirm_all(platform_client, saas_login, forwarder_client, plan_id, order_id)
    order = store.order(plan_id, order_id, org_id=PLATFORM_ORG_ID)
    assert order.status == "forwarder_confirmed"
    jinju_lots = {"lot-jinju-sb-1"}
    own = next(r for r in order.requests if jinju_lots & set(r.lot_ids))
    foreign = [r for r in order.requests if not jinju_lots & set(r.lot_ids)]
    assert foreign
    notes = {r.request_id: f"{r.request_id} 비공개 회신 메모 운임 3,200,000원" for r in foreign}

    base = f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/requests"
    answered = forwarder_client.post(
        f"{base}/{own.request_id}", json={"accepted": True, "note": OWN_NOTE}
    )
    assert answered.status_code == 200, answered.text
    for index, request in enumerate(foreign):
        response = forwarder_client.post(
            f"{base}/{request.request_id}",
            json={"accepted": index % 2 == 0, "note": notes[request.request_id]},
        )
        assert response.status_code == 200, response.text

    stored = store.order(plan_id, order_id, org_id=PLATFORM_ORG_ID)
    replies = [e for e in stored.log if e.note in {OWN_NOTE, *notes.values()}]
    assert len(replies) == len(foreign) + 1
    for entry in replies:
        assert entry.actor == "forwarder"
        assert saas_views._reply_request_id(entry) in {own.request_id, *notes}
        assert entry.action.startswith(f"{saas_views._reply_request_id(entry)} ")

    jinju = saas_login(shipper_org_id("sh-jinju"))
    listed = jinju.get(f"/api/saas/shipper/plans/{plan_id}/orders")
    assert listed.status_code == 200, listed.text
    view = next(o for o in listed.json() if o["order_id"] == order_id)
    visible = {r["request_id"] for r in view["requests"]}
    assert own.request_id in visible
    assert not visible & set(notes)
    actions = [e["action"] for e in view["log"]]
    assert any(a.startswith(f"{own.request_id} ") for a in actions)
    for request_id in notes:
        assert not any(a.split(" ", 1)[0] == request_id for a in actions)
    dumped = json.dumps(view["log"], ensure_ascii=False)
    assert OWN_NOTE in dumped
    assert "비공개 회신 메모" not in dumped
    assert "3,200,000" not in dumped


def _short_ids_session(platform_client: TestClient, saas_login, own: str | None = None):
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    session = _renamed(_session(plan_id), "lot-sancheong-sb-1", "20231")
    session = _renamed(session, "lot-miryang-sb-2", "B7")
    if own is not None:
        session = _renamed(session, "lot-jinju-sb-1", own)
    return session


def test_short_and_numeric_lot_ids_are_anonymized_in_lot_lists(
    platform_client: TestClient, saas_login
) -> None:
    session = _short_ids_session(platform_client, saas_login)
    anonymizer = saas_views.shipper_anonymizer(session, {"sh-jinju"})
    numeric, short, long_ = (
        anonymizer.lot("20231"),
        anonymizer.lot("B7"),
        anonymizer.lot("lot-miryang-sb-1"),
    )
    assert numeric.startswith("anon-") and short.startswith("anon-")

    request = (
        "[SI 정정 + VGM 재제출 요청] 컨테이너 CA-01 적재를 20→18파렛트로 정정합니다. "
        "대상 출하: lot-jinju-sb-1, 20231, B7, lot-miryang-sb-1. 화주별 파렛트: 진주 8. "
        "회신 기한 11/12 18:00."
    )
    walked = anonymizer.walk(
        {"lot_ids": ["lot-jinju-sb-1", "20231", "B7", "lot-miryang-sb-1"], "text": request}
    )
    assert walked["lot_ids"] == ["lot-jinju-sb-1", numeric, short, long_]
    assert f"대상 출하: lot-jinju-sb-1, {numeric}, {short}, {long_}." in walked["text"]
    assert "20231" not in walked["text"]
    assert ", B7," not in walked["text"]

    assert anonymizer.text("로트 20231, B7: 무전원 예상") == f"로트 {numeric}, {short}: 무전원 예상"
    assert anonymizer.text("대상 출하: B7") == f"대상 출하: {short}"
    assert anonymizer.text("위험 로트 lot-jinju-sb-1, 20231(태풍)") == (
        f"위험 로트 lot-jinju-sb-1, {numeric}(태풍)"
    )
    moves = "이동: B7 2파렛트 CA-01→CA-03, 20231 5파렛트 CA-02→항공 / 적재: CA-03 10/20"
    assert anonymizer.text(moves) == (
        f"이동: {short} 2파렛트 CA-01→CA-03, {numeric} 5파렛트 CA-02→항공 / 적재: CA-03 10/20"
    )


def test_list_anonymization_keeps_ordinary_numbers_and_words(
    platform_client: TestClient, saas_login
) -> None:
    session = _short_ids_session(platform_client, saas_login)
    anonymizer = saas_views.shipper_anonymizer(session, {"sh-jinju"})
    for ordinary in (
        "화물 총중량 20231kg, VGM 20231 kg 기준",
        "B7 게이트 20231번 선석",
        "대상 출하: 없음. 회신 기한 20231",
        "대상 출하: B7x, 20231-1, xB7. 끝",
        "적재: B7 2파렛트",
        "슬로트 B7",
    ):
        assert anonymizer.text(ordinary) == ordinary


def test_own_lot_sharing_prefix_with_other_short_id_is_kept(
    platform_client: TestClient, saas_login
) -> None:
    session = _short_ids_session(platform_client, saas_login, own="B7.5")
    anonymizer = saas_views.shipper_anonymizer(session, {"sh-jinju"})
    short = anonymizer.lot("B7")
    assert anonymizer.lot("B7.5") == "B7.5"
    assert anonymizer.text("대상 출하: B7.5, B7.") == f"대상 출하: B7.5, {short}."


def _head() -> str | None:
    return ScriptDirectory.from_config(migration_config()).get_current_head()


def test_downgrade_0012_converts_shipper_confirmation_status(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    config = migration_config()
    with database.engine.connect() as conn:
        trans = conn.begin()
        try:
            row = conn.execute(
                text("SELECT status, payload->>'status' FROM plan_sessions WHERE plan_id = :id"),
                {"id": plan_id},
            ).one()
            assert tuple(row) == ("pending_shipper_confirmation", "pending_shipper_confirmation")
            conn.execute(
                text(
                    "INSERT INTO plan_sessions (plan_id, org_id, scenario_id, version, status, "
                    "lot_count, totals, created_at, updated_at, payload, committed) "
                    "SELECT 'pending-order-copy', org_id, scenario_id, version, "
                    "'pending_confirmation', lot_count, totals, created_at, updated_at, "
                    "jsonb_set(payload, '{status}', '\"pending_confirmation\"'::jsonb), "
                    "committed FROM plan_sessions WHERE plan_id = :id"
                ),
                {"id": plan_id},
            )
            conn.execute(
                text(
                    "UPDATE users SET world_id = NULL WHERE id IN ("
                    "SELECT user_id FROM memberships WHERE org_id IN ("
                    "SELECT org_id FROM plan_sessions))"
                )
            )
            conn.execute(
                text(
                    "UPDATE organizations SET world_id = NULL "
                    "WHERE id IN (SELECT org_id FROM plan_sessions)"
                )
            )
            config.attributes["connection"] = conn
            command.downgrade(config, "0011")
            assert MigrationContext.configure(conn).get_current_revision() == "0011"
            rows = {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    text("SELECT plan_id, status, payload->>'status' FROM plan_sessions")
                )
            }
            assert rows[plan_id] == ("committed", "committed")
            assert rows["pending-order-copy"] == ("pending_confirmation", "pending_confirmation")
            for status, payload_status in rows.values():
                assert status in PRE_0012_STATUSES
                assert payload_status in PRE_0012_STATUSES
            payload = conn.execute(
                text("SELECT payload FROM plan_sessions WHERE plan_id = :id"), {"id": plan_id}
            ).scalar_one()
            assert payload["plan"] and payload["scenario"]
        finally:
            trans.rollback()
    with database.engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == _head()
    assert store.get(plan_id, org_id=PLATFORM_ORG_ID) is not None
