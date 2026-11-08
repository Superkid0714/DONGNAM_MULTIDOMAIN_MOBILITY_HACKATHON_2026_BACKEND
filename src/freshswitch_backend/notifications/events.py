from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from freshswitch_backend.domain.models import KST
from freshswitch_backend.execution.models import ExecutionOrder
from freshswitch_backend.notifications.models import Notification
from freshswitch_backend.notifications.store import Draft, lot_shipper, record, shipper_orgs
from freshswitch_backend.planning.store import PlanSession
from freshswitch_backend.replan.models import (
    AddLotEvent,
    AirSwitchEvent,
    CancelEvent,
    DelayEvent,
    ReduceEvent,
)

SHIPPER_PLAN_PATH = "/app/shipper/plans/detail"
SENDER = "[프레시스위치]"
OTHER_CHANGE = {"reduce": "감량", "increase": "증량", "cancel": "취소", "air_switch": "항공 전환"}


@dataclass(frozen=True)
class ChangeRequestNotice:
    request_id: str
    plan_id: str
    shipper_org_id: str
    shipper_id: str
    lot_id: str
    type: str
    new_pallets: int | None
    order_id: str | None = None
    note: str | None = None


def shipper_link(plan_id: str, order_id: str | None = None) -> str:
    query = {"plan_id": plan_id}
    if order_id is not None:
        query["order_id"] = order_id
    return f"{SHIPPER_PLAN_PATH}?{urlencode(query)}"


def voyage_label(session: PlanSession) -> str:
    used = {c.voyage_id for c in session.plan.containers if c.used}
    voyages = sorted((v for v in session.scenario.voyages if v.id in used), key=lambda v: v.etd)
    if not voyages:
        return "공동적재 계획"
    first = voyages[0]
    return f"{first.vessel_name}({first.etd.astimezone(KST):%m/%d} 출항)"


def change_phrase(change_type: str, lot_id: str, new_pallets: int | None) -> str:
    if change_type == "increase":
        return f"출하 {lot_id} {new_pallets}파렛트로 증량"
    if change_type == "reduce":
        return f"출하 {lot_id} {new_pallets}파렛트로 감량"
    if change_type == "cancel":
        return f"출하 {lot_id} 취소"
    return f"출하 {lot_id} 항공 전환"


def _event_shipper(session: PlanSession, order: ExecutionOrder) -> str | None:
    event = order.event
    if isinstance(event, AddLotEvent):
        return event.lot.shipper_id
    lot_id = getattr(event, "lot_id", None)
    lot = next((x for x in session.scenario.lots if x.id == lot_id), None)
    if lot is not None:
        return lot.shipper_id
    if order.cancellation is not None and order.cancellation.lot_id == lot_id:
        return order.cancellation.payer_shipper_id
    confirmed = next(
        (c.shipper_id for c in order.shipper_confirmations if lot_id in c.lot_ids), None
    )
    if confirmed is not None:
        return confirmed
    return lot_shipper(lot_id)


def _event_phrase(session: PlanSession, order: ExecutionOrder, shipper_id: str) -> str:
    event = order.event
    if isinstance(event, DelayEvent):
        return "출항 일정 변경"
    own = _event_shipper(session, order) == shipper_id
    if isinstance(event, AddLotEvent):
        return f"출하 {event.lot.id} 추가" if own else "다른 화주 출하 추가"
    if not isinstance(event, ReduceEvent | CancelEvent | AirSwitchEvent):
        return "계획 변경"
    if not own:
        return f"같은 계획 다른 화주 출하 {OTHER_CHANGE[event.type]}"
    return change_phrase(event.type, event.lot_id, getattr(event, "new_pallets", None))


def _order_shippers(order: ExecutionOrder) -> list[str]:
    ids = [c.shipper_id for c in order.shipper_confirmations]
    return list(dict.fromkeys([*ids, *order.notified_shipper_ids]))


def notify_order_confirmation(
    session: PlanSession, order: ExecutionOrder, owner_org_id: str, now: datetime
) -> list[Notification]:
    pending = [c.shipper_id for c in order.shipper_confirmations if c.status == "pending"]
    confirming = {c.shipper_id for c in order.shipper_confirmations}
    notified = [s for s in order.notified_shipper_ids if s not in confirming]
    orgs = shipper_orgs([*pending, *notified], owner_org_id)
    deadline = order.deadlines.shipper.astimezone(KST)
    label = voyage_label(session)
    link = shipper_link(session.plan_id, order.order_id)
    drafts = [
        Draft(
            org_id=orgs[shipper_id],
            shipper_id=shipper_id,
            kind="order_confirmation_requested",
            title="계획 변경 확인 요청",
            body=(
                f"{SENDER} {label} 공동적재 계획이 바뀌었습니다"
                f"({_event_phrase(session, order, shipper_id)}). "
                f"{deadline:%m/%d %H:%M}까지 변경 내용을 확인하고 수락 또는 거절해 주세요."
            ),
            link=link,
            dedupe_key=f"order_confirmation_requested:{order.order_id}:{shipper_id}",
            plan_id=session.plan_id,
            order_id=order.order_id,
        )
        for shipper_id in pending
        if shipper_id in orgs
    ]
    drafts += [
        Draft(
            org_id=orgs[shipper_id],
            shipper_id=shipper_id,
            kind="order_notified",
            title="계획 변경 안내",
            body=(
                f"{SENDER} {label} 공동적재 계획이 바뀌었습니다"
                f"({_event_phrase(session, order, shipper_id)}). 확인 절차는 필요 없습니다."
            ),
            link=link,
            dedupe_key=f"order_notified:{order.order_id}:{shipper_id}",
            plan_id=session.plan_id,
            order_id=order.order_id,
        )
        for shipper_id in notified
        if shipper_id in orgs
    ]
    return record(drafts, now)


def notify_order_committed(
    session: PlanSession, order: ExecutionOrder, owner_org_id: str, now: datetime
) -> list[Notification]:
    if order.status not in ("forwarder_confirmed", "executed"):
        return []
    shippers = _order_shippers(order)
    orgs = shipper_orgs(shippers, owner_org_id)
    label = voyage_label(session)
    drafts = [
        Draft(
            org_id=orgs[shipper_id],
            shipper_id=shipper_id,
            kind="order_committed",
            title="실행 지시 확정",
            body=(
                f"{SENDER} {label} 실행 지시가 포워더 확정으로 계획에 반영되었습니다"
                f"({_event_phrase(session, order, shipper_id)}). 집하·선적 일정을 확인해 주세요."
            ),
            link=shipper_link(session.plan_id, order.order_id),
            dedupe_key=f"order_committed:{order.order_id}:{shipper_id}",
            plan_id=session.plan_id,
            order_id=order.order_id,
        )
        for shipper_id in shippers
        if shipper_id in orgs
    ]
    return record(drafts, now)


def notify_change_request_decision(
    notice: ChangeRequestNotice, applied: bool, now: datetime, confirmation_sent: bool = False
) -> list[Notification]:
    phrase = change_phrase(notice.type, notice.lot_id, notice.new_pallets)
    if applied:
        kind = "change_request_applied"
        title = "변경 요청 반영"
        follow_up = (
            "실행 지시 확인 요청을 함께 보냈습니다."
            if confirmation_sent
            else "포워더 확정 뒤 집하·선적 일정이 확정됩니다."
        )
        body = f"{SENDER} 요청하신 {phrase}이(가) 계획에 반영되었습니다. {follow_up}"
    else:
        kind = "change_request_rejected"
        title = "변경 요청 반려"
        body = f"{SENDER} 요청하신 {phrase}이(가) 반려되었습니다."
        if notice.note:
            body += f" 사유: {notice.note}"
    draft = Draft(
        org_id=notice.shipper_org_id,
        shipper_id=notice.shipper_id,
        kind=kind,
        title=title,
        body=body,
        link=shipper_link(notice.plan_id, notice.order_id),
        dedupe_key=f"{kind}:{notice.request_id}",
        plan_id=notice.plan_id,
        order_id=notice.order_id,
        change_request_id=notice.request_id,
    )
    return record([draft], now)


def notify_plan_confirmation(
    session: PlanSession,
    owner_org_id: str,
    now: datetime,
    shipper_ids: Iterable[str] | None = None,
) -> list[Notification]:
    planned = {a.shipper_id for a in session.plan.assignments}
    shippers = sorted(planned if shipper_ids is None else set(shipper_ids))
    orgs = shipper_orgs(shippers, owner_org_id)
    label = voyage_label(session)
    lots_by_shipper = {
        shipper_id: sum(1 for a in session.plan.assignments if a.shipper_id == shipper_id)
        for shipper_id in shippers
    }
    drafts = [
        Draft(
            org_id=orgs[shipper_id],
            shipper_id=shipper_id,
            kind="plan_confirmation_requested",
            title="새 계획 확인 요청",
            body=(
                f"{SENDER} 새 공동적재 계획 {label}에 출하 {lots_by_shipper[shipper_id]}건이 "
                "들어갔습니다. 확인 후 수락 또는 거절해 주세요."
            ),
            link=shipper_link(session.plan_id),
            dedupe_key=(
                f"plan_confirmation_requested:{session.plan_id}:{session.version}:{shipper_id}"
            ),
            plan_id=session.plan_id,
        )
        for shipper_id in shippers
        if shipper_id in orgs
    ]
    return record(drafts, now)
