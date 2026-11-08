import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from freshswitch_backend.domain.models import KST, Lot, Plan, Scenario
from freshswitch_backend.execution.models import (
    AutomaticAction,
    ConfirmationReason,
    Deadlines,
    ExecutionOrder,
    ExternalRequest,
    OrderLogEntry,
    ShipperConfirmation,
)
from freshswitch_backend.execution.params import ExecutionParams
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.planning.service import VoyagePickup
from freshswitch_backend.replan.engine import locations
from freshswitch_backend.replan.models import (
    AddLotEvent,
    AirSwitchEvent,
    Alternative,
    CancelEvent,
    DelayEvent,
    Event,
    ReduceEvent,
)

AIR = "AIR"
UNASSIGNED = "UNASSIGNED"
CARRIER_CHANNEL = "포워더 → 선사 (부킹 명의는 포워더)"
TERMINAL_CHANNEL = "포워더 → 선사 → 터미널 (터미널은 화주·플랫폼 요청을 직접 받지 않는다)"
AIRLINE_CHANNEL = "포워더 → 항공사·GSA"

REASON_TEXT: dict[ConfirmationReason, str] = {
    "cost_increase": "총비용이 늘어난다",
    "pull_forward": "출하(수확)를 앞당겨야 한다",
    "mode_change": "운송수단이 바뀐다",
    "voyage_change": "다른 배로 옮겨진다",
    "cancellation_fee": "취소·감량 부담금을 낸다",
}


def fmt(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%m/%d %H:%M")


def day_start(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=KST)


def short_name(name: str) -> str:
    return name.split()[0] if name else name


def event_lot_id(event: Event) -> str | None:
    if isinstance(event, CancelEvent | ReduceEvent | AirSwitchEvent):
        return event.lot_id
    if isinstance(event, AddLotEvent):
        return event.lot.id
    return None


def _category(location: str | None) -> str | None:
    if location is None:
        return None
    if location in (AIR, UNASSIGNED):
        return location
    return "sea"


@dataclass
class _Ctx:
    scenario_before: Scenario
    plan_before: Plan
    routes_before: list[VoyagePickup]
    scenario_after: Scenario
    alternative: Alternative
    event: Event
    params: ExecutionParams

    def __post_init__(self) -> None:
        self.model_before = CostModel(self.scenario_before)
        self.model_after = CostModel(self.scenario_after)
        self.lots_before = {lot.id: lot for lot in self.scenario_before.lots}
        self.lots_after = {lot.id: lot for lot in self.scenario_after.lots}
        self.loc_before = locations(self.plan_before)
        self.loc_after = locations(self.alternative.plan)
        self.pull_before = {a.lot_id: a.pull_forward_days for a in self.plan_before.assignments}
        self.pull_after = {a.lot_id: a.pull_forward_days for a in self.alternative.plan.assignments}
        self.shippers = {s.id: s for s in self.scenario_before.shippers}
        for shipper in self.scenario_after.shippers:
            self.shippers.setdefault(shipper.id, shipper)
        self.ports = {p.id: p.name for p in self.scenario_after.ports}
        self.products = {p.id: p for p in self.scenario_after.products}

    def lot(self, lot_id: str) -> Lot | None:
        return self.lots_after.get(lot_id) or self.lots_before.get(lot_id)

    def shipper_name(self, shipper_id: str) -> str:
        shipper = self.shippers.get(shipper_id)
        return shipper.name if shipper else shipper_id

    def kg(self, lot_id: str, pallets: int) -> int:
        lot = self.lot(lot_id)
        product = self.products.get(lot.product_id) if lot else None
        return round(pallets * (product.pallet_kg if product else 415))


def _composition(plan: Plan, container_id: str) -> dict[str, int]:
    return {
        a.lot_id: a.pallets
        for a in plan.assignments
        if a.mode == "sea" and a.container_id == container_id
    }


def _routes_by_lot(routes: list[VoyagePickup]) -> dict[str, tuple[str, str, datetime, datetime]]:
    found: dict[str, tuple[str, str, datetime, datetime]] = {}
    for voyage_pickup in routes:
        for route in voyage_pickup.pickup.routes:
            for stop in route.stops:
                for lot_id in stop.lot_ids:
                    found[lot_id] = (
                        voyage_pickup.voyage_id,
                        route.truck_id,
                        route.latest_departure,
                        route.port_eta,
                    )
    return found


def _route_signature(routes: list[VoyagePickup]) -> dict[tuple[str, str], tuple]:
    signature: dict[tuple[str, str], tuple] = {}
    for voyage_pickup in routes:
        for route in voyage_pickup.pickup.routes:
            key = (voyage_pickup.voyage_id, route.truck_id)
            lots = tuple(tuple(stop.lot_ids) for stop in route.stops)
            signature[key] = signature.get(key, ()) + ((lots, route.port_eta),)
    return signature


class _RequestBuilder:
    def __init__(self) -> None:
        self.items: list[ExternalRequest] = []

    def add(self, **fields) -> None:
        self.items.append(ExternalRequest(request_id=f"REQ-{len(self.items) + 1}", **fields))


def _composition_text(ctx: _Ctx, before: dict[str, int], after: dict[str, int]) -> str:
    totals_before: dict[str, int] = defaultdict(int)
    totals_after: dict[str, int] = defaultdict(int)
    for lot_id, pallets in before.items():
        lot = ctx.lot(lot_id)
        totals_before[lot.shipper_id if lot else lot_id] += pallets
    for lot_id, pallets in after.items():
        lot = ctx.lot(lot_id)
        totals_after[lot.shipper_id if lot else lot_id] += pallets
    parts = []
    order = {sid: i for i, sid in enumerate(ctx.shippers)}
    for sid in sorted(set(totals_before) | set(totals_after), key=lambda x: (order.get(x, 99), x)):
        parts.append(
            f"{short_name(ctx.shipper_name(sid))} {totals_before.get(sid, 0)}→{totals_after.get(sid, 0)}"
        )
    return ", ".join(parts)


def _ca_setting(ctx: _Ctx, lot_ids: list[str]) -> str:
    products = {ctx.lot(x).product_id for x in lot_ids if ctx.lot(x)}
    texts = []
    for pid in sorted(products):
        product = ctx.products.get(pid)
        if product is None:
            continue
        gas = ""
        if product.o2_pct is not None or product.co2_pct is not None:
            gas = f", O2 {product.o2_pct:g}%·CO2 {product.co2_pct:g}%"
        texts.append(f"{product.name} {product.temp_min_c:g}~{product.temp_max_c:g}°C{gas}")
    return "; ".join(texts)


def build_order(
    *,
    plan_id: str,
    replan_id: str,
    base_version: int,
    target_version: int,
    rollback_version: int,
    scenario_before: Scenario,
    plan_before: Plan,
    routes_before: list[VoyagePickup],
    scenario_after: Scenario,
    alternative: Alternative,
    event: Event,
    now: datetime,
    params: ExecutionParams | None = None,
) -> ExecutionOrder:
    ctx = _Ctx(
        scenario_before,
        plan_before,
        routes_before,
        scenario_after,
        alternative,
        event,
        params or ExecutionParams(),
    )
    p = ctx.params
    voyages_after = ctx.model_after.voyages
    voyages_before = ctx.model_before.voyages
    container_ids = [c.id for c in scenario_after.containers]
    changed: list[tuple[str, dict[str, int], dict[str, int]]] = []
    for cid in container_ids:
        before = _composition(plan_before, cid)
        after = _composition(alternative.plan, cid)
        if before != after:
            changed.append((cid, before, after))
    affected_voyages = {ctx.model_after.containers[cid].voyage_id for cid, _, _ in changed}
    rescheduled = {
        vid
        for vid, v in voyages_after.items()
        if vid in voyages_before
        and (v.etd, v.cy_cutoff) != (voyages_before[vid].etd, voyages_before[vid].cy_cutoff)
    }
    affected_voyages |= rescheduled
    if isinstance(event, DelayEvent):
        affected_voyages.add(event.voyage_id)
    cutoffs = [voyages_after[v].cy_cutoff for v in affected_voyages if v in voyages_after]
    if not cutoffs:
        cutoffs = [v.cy_cutoff for v in scenario_after.voyages]
    cy_cutoff = min(cutoffs)
    booking_deadline = cy_cutoff - timedelta(hours=p.booking_amendment_lead_hours.value)

    routes_after_by_lot = _routes_by_lot(alternative.routes)
    routes_before_by_lot = _routes_by_lot(routes_before)
    affected_lots = {lot_id for _, _, after in changed for lot_id in after}
    affected_lots |= {m.lot_id for m in alternative.diff.moved_lots}
    departures = [
        routes_after_by_lot[lot_id][2]
        for lot_id in affected_lots
        if lot_id in routes_after_by_lot and routes_after_by_lot[lot_id][0] in affected_voyages
    ]
    if departures:
        dispatch = min(departures)
        dispatch_basis = (
            f"집하 트럭 최종 출발 {fmt(dispatch)} "
            f"(CY 마감 {fmt(cy_cutoff)} − 집하 리드 "
            f"{(cy_cutoff - dispatch).total_seconds() / 3600:.1f}시간)"
        )
    else:
        dispatch = cy_cutoff - timedelta(hours=p.fallback_pickup_lead_hours.value)
        dispatch_basis = (
            f"집하 경로 없음 → CY 마감 {fmt(cy_cutoff)} − "
            f"{p.fallback_pickup_lead_hours.value:g}시간(가정)"
        )

    requests = _RequestBuilder()
    for cid, before, after in changed:
        container = ctx.model_after.containers[cid]
        voyage = voyages_after[container.voyage_id]
        pallets_before = sum(before.values())
        pallets_after = sum(after.values())
        kg_before = sum(ctx.kg(x, n) for x, n in before.items())
        kg_after = sum(ctx.kg(x, n) for x, n in after.items())
        head = (
            f"{voyage.vessel_name}({voyage.id}, {voyage.terminal}, ETD {fmt(voyage.etd)}) "
            f"{ctx.ports.get(voyage.origin, voyage.origin)}→"
            f"{ctx.ports.get(voyage.destination, voyage.destination)}"
        )
        composition = _composition_text(ctx, before, after)
        lot_ids = sorted(set(before) | set(after))
        common = {
            "voyage_id": voyage.id,
            "container_id": cid,
            "lot_ids": lot_ids,
            "pallets_before": pallets_before,
            "pallets_after": pallets_after,
        }
        if before and after:
            requests.add(
                kind="booking_amendment",
                counterparty_role="carrier",
                counterparty="선사",
                channel=CARRIER_CHANNEL,
                deadline=booking_deadline,
                text=(
                    f"[부킹 변경 요청] {head} 부킹의 컨테이너 {cid}({container.kind}) 적재를 "
                    f"{pallets_before}→{pallets_after}파렛트({kg_before:,}→{kg_after:,}kg)로 "
                    f"변경합니다. 화주별 파렛트: {composition}. 선적지시(SI)·VGM 수정 반영 "
                    f"부탁드립니다. CY 반입 마감 {fmt(voyage.cy_cutoff)} 유지. "
                    f"회신 기한 {fmt(booking_deadline)}."
                ),
                **common,
            )
        elif before:
            requests.add(
                kind="booking_cancel",
                counterparty_role="carrier",
                counterparty="선사",
                channel=CARRIER_CHANNEL,
                deadline=booking_deadline,
                text=(
                    f"[부킹 취소 요청] {head} 부킹의 컨테이너 {cid}({container.kind}, "
                    f"{pallets_before}파렛트·{kg_before:,}kg)를 취소합니다. 화주별 파렛트: "
                    f"{composition}. CA 장비 반납 일정과 취소료 조건 회신 부탁드립니다. "
                    f"회신 기한 {fmt(booking_deadline)}."
                ),
                **common,
            )
        else:
            setting = _ca_setting(ctx, list(after))
            requests.add(
                kind="booking_new",
                counterparty_role="carrier",
                counterparty="선사",
                channel=CARRIER_CHANNEL,
                deadline=booking_deadline,
                text=(
                    f"[신규 부킹 요청] {head} {container.kind} 1대({cid}), "
                    f"{pallets_after}파렛트·{kg_after:,}kg. 화주별 파렛트: {composition}. "
                    f"설정: {setting}. 장비 PTI·공컨테이너 반출 일정 회신 부탁드립니다. "
                    f"CY 반입 마감 {fmt(voyage.cy_cutoff)}. 회신 기한 {fmt(booking_deadline)}."
                ),
                **common,
            )
            requests.add(
                kind="reefer_plug",
                counterparty_role="terminal",
                counterparty=voyage.terminal,
                channel=TERMINAL_CHANNEL,
                deadline=booking_deadline,
                text=(
                    f"[리퍼 전원 연결 요청] {voyage.terminal} {voyage.vessel_name}({voyage.id}) "
                    f"선적 예정 {cid}({container.kind}, {pallets_after}파렛트) 반입 즉시 전원 연결·"
                    f"모니터링 부탁드립니다. 설정: {setting}. ETD {fmt(voyage.etd)}."
                ),
                **common,
            )
        if after:
            new_etas = sorted(
                {
                    (routes_after_by_lot[x][3], routes_after_by_lot[x][1])
                    for x in after
                    if x in routes_after_by_lot
                }
            )
            old_etas = sorted(
                {
                    (routes_before_by_lot[x][3], routes_before_by_lot[x][1])
                    for x in before
                    if x in routes_before_by_lot
                }
            )
            if new_etas and (not before or new_etas != old_etas):
                arrivals = ", ".join(f"{truck} {fmt(eta)}" for eta, truck in new_etas)
                previous = (
                    ", ".join(f"{truck} {fmt(eta)}" for eta, truck in old_etas)
                    if old_etas
                    else "없음"
                )
                requests.add(
                    kind="cy_entry",
                    counterparty_role="terminal",
                    counterparty=voyage.terminal,
                    channel=TERMINAL_CHANNEL,
                    deadline=booking_deadline,
                    text=(
                        f"[CY 반입 예약 변경] {voyage.terminal} {voyage.vessel_name}({voyage.id}) "
                        f"{cid} 반입분 {pallets_before}→{pallets_after}파렛트. 트럭 도착 예정: "
                        f"{arrivals} (이전: {previous}). 반입 마감 {fmt(voyage.cy_cutoff)}."
                    ),
                    **common,
                )
    for voyage_id in sorted(rescheduled):
        voyage = voyages_after[voyage_id]
        extra = CostModel.terminal_extra_days(voyage)
        if extra > CostModel.terminal_extra_days(voyages_before[voyage_id]):
            for load in alternative.plan.containers:
                if load.voyage_id != voyage.id or not load.used:
                    continue
                requests.add(
                    kind="reefer_plug",
                    counterparty_role="terminal",
                    counterparty=voyage.terminal,
                    channel=TERMINAL_CHANNEL,
                    deadline=voyage.cy_cutoff,
                    voyage_id=voyage.id,
                    container_id=load.container_id,
                    lot_ids=load.lot_ids,
                    pallets_before=load.pallets,
                    pallets_after=load.pallets,
                    text=(
                        f"[리퍼 전원 연결 연장 요청] {voyage.terminal} {voyage.vessel_name}"
                        f"({voyage.id}) ETD {fmt(voyage.original_etd or voyage.etd)}→"
                        f"{fmt(voyage.etd)} 지연으로 {load.container_id}({load.pallets}파렛트) "
                        f"터미널 대기가 {extra:.1f}일로 늘어납니다. 출항까지 전원 연결 유지·"
                        f"온도 이탈 시 통보 부탁드립니다. CY 반입 마감 {fmt(voyage.cy_cutoff)}."
                    ),
                )

    event_lot = event_lot_id(event)
    for lot_id in sorted(set(ctx.loc_before) | set(ctx.loc_after)):
        before_loc = ctx.loc_before.get(lot_id)
        after_loc = ctx.loc_after.get(lot_id)
        lot = ctx.lot(lot_id)
        if lot is None:
            continue
        pallets_before = ctx.lots_before[lot_id].pallets if lot_id in ctx.lots_before else 0
        pallets_after = ctx.lots_after[lot_id].pallets if lot_id in ctx.lots_after else 0
        air_deadline = day_start(lot.ready_date) - timedelta(hours=p.air_booking_lead_hours.value)
        product = ctx.products.get(lot.product_id)
        product_name = product.name if product else lot.product_id
        who = ctx.shipper_name(lot.shipper_id)
        if after_loc == AIR and (before_loc != AIR or pallets_before != pallets_after):
            verb = "변경" if before_loc == AIR else "신규"
            requests.add(
                kind="air_booking",
                counterparty_role="airline",
                counterparty="항공사",
                channel=AIRLINE_CHANNEL,
                deadline=air_deadline,
                lot_ids=[lot_id],
                pallets_before=pallets_before if before_loc == AIR else 0,
                pallets_after=pallets_after,
                text=(
                    f"[항공 예약 {verb} 요청] {lot_id} {who} {product_name} "
                    f"{pallets_after}파렛트({ctx.kg(lot_id, pallets_after):,}kg), 출하 "
                    f"{lot.ready_date:%m/%d}, 목적지 {ctx.ports.get(lot.destination, lot.destination)}, 판매기한 "
                    f"{lot.sell_by_date:%m/%d}. 냉장 보관·탑재 가능 편 회신 부탁드립니다. "
                    f"회신 기한 {fmt(air_deadline)}."
                ),
            )
        elif before_loc == AIR and after_loc != AIR:
            reason = (
                f"해상 {after_loc} 배정"
                if _category(after_loc) == "sea"
                else ("출하 취소" if after_loc is None else "미배정")
            )
            requests.add(
                kind="air_cancel",
                counterparty_role="airline",
                counterparty="항공사",
                channel=AIRLINE_CHANNEL,
                deadline=air_deadline,
                lot_ids=[lot_id],
                pallets_before=pallets_before,
                pallets_after=0,
                text=(
                    f"[항공 예약 취소 요청] {lot_id} {who} {product_name} "
                    f"{pallets_before}파렛트({ctx.kg(lot_id, pallets_before):,}kg), 출하 "
                    f"{lot.ready_date:%m/%d} 항공 예약을 취소합니다. 사유: {reason}. "
                    f"취소 기한 {fmt(air_deadline)}."
                ),
            )

    request_deadlines = [r.deadline for r in requests.items]
    forwarder = min([dispatch, *request_deadlines])
    shipper_deadline = forwarder - timedelta(hours=p.forwarder_processing_hours.value)
    basis = (
        f"화주 확인 = 포워더 확정 기한 − 포워더 처리 {p.forwarder_processing_hours.value:g}시간(가정). "
        f"포워더 확정 = min(집하 출발 기한, 제3자 요청 회신 기한). {dispatch_basis}. "
        f"선사 부킹 변경 회신 = CY 마감 − {p.booking_amendment_lead_hours.value:g}시간(가정)"
    )

    initiator = None
    if event_lot:
        lot = ctx.lot(event_lot)
        initiator = lot.shipper_id if lot else None
    charge = alternative.diff.cancellation
    moved_by_shipper: dict[str, list] = defaultdict(list)
    for move in alternative.diff.moved_lots:
        moved_by_shipper[move.shipper_id].append(move)
    confirmations: list[ShipperConfirmation] = []
    notified: list[str] = []
    deltas = {d.shipper_id: d for d in alternative.diff.shippers}
    for sid in sorted(set(deltas) | set(moved_by_shipper)):
        delta = deltas.get(sid)
        reasons: list[ConfirmationReason] = []
        cost_delta = delta.cost_delta if delta else 0
        adjustment = delta.cancellation_adjustment if delta else 0
        if cost_delta + adjustment > 0 and sid != initiator:
            reasons.append("cost_increase")
        pull_days = 0
        pulled_dates: list[date] = []
        for move in moved_by_shipper.get(sid, []):
            before_cat = _category(move.from_location)
            after_cat = _category(move.to_location)
            gained = ctx.pull_after.get(move.lot_id, 0) - ctx.pull_before.get(move.lot_id, 0)
            if after_cat == "sea" and gained > 0:
                pulled = ctx.pull_after.get(move.lot_id, 0)
                pull_days = max(pull_days, pulled)
                lot = ctx.lot(move.lot_id)
                if lot:
                    pulled_dates.append(lot.ready_date - timedelta(days=pulled))
                if "pull_forward" not in reasons:
                    reasons.append("pull_forward")
            if move.lot_id == event_lot:
                continue
            if (
                before_cat
                and after_cat
                and before_cat != after_cat
                and "mode_change" not in reasons
            ):
                reasons.append("mode_change")
            if before_cat == "sea" and after_cat == "sea":
                vb = ctx.model_before.containers.get(move.from_location)
                va = ctx.model_after.containers.get(move.to_location)
                if vb and va and vb.voyage_id != va.voyage_id and "voyage_change" not in reasons:
                    reasons.append("voyage_change")
        if charge and charge.fee > 0 and charge.payer_shipper_id == sid:
            reasons.append("cancellation_fee")
        if not reasons:
            if delta and (cost_delta != 0 or adjustment != 0) or sid in moved_by_shipper:
                notified.append(sid)
            continue
        deadline = shipper_deadline
        for ready in pulled_dates:
            harvest = day_start(ready) - timedelta(hours=p.harvest_notice_hours.value)
            deadline = min(deadline, harvest)
        lot_ids = sorted(
            {m.lot_id for m in moved_by_shipper.get(sid, [])}
            | {
                a.lot_id
                for a in alternative.plan.assignments
                if a.shipper_id == sid and a.lot_id in affected_lots
            }
        )
        confirmations.append(
            ShipperConfirmation(
                shipper_id=sid,
                shipper_name=ctx.shipper_name(sid),
                lot_ids=lot_ids,
                reasons=reasons,
                reason_text=[REASON_TEXT[r] for r in reasons],
                cost_before=delta.cost_before if delta else 0,
                cost_after=delta.cost_after if delta else 0,
                cost_delta=cost_delta,
                cancellation_adjustment=adjustment,
                net_cost_delta=cost_delta + adjustment,
                pull_forward_days=pull_days,
                deadline=deadline,
            )
        )
    if confirmations:
        shipper_deadline = min(c.deadline for c in confirmations)

    automatic = _automatic_actions(ctx, changed)
    status = "proposed" if confirmations else "shipper_confirmed"
    log = [
        OrderLogEntry(
            at=now,
            actor="system",
            action=f"대안 {alternative.id} 적용 → 실행 지시 생성",
            status_after="proposed",
        )
    ]
    if not confirmations:
        log.append(
            OrderLogEntry(
                at=now,
                actor="system",
                action="확인이 필요한 화주 없음 → 포워더 확정 대기",
                status_after="shipper_confirmed",
            )
        )
    return ExecutionOrder(
        order_id=str(uuid.uuid4()),
        plan_id=plan_id,
        replan_id=replan_id,
        alternative_id=alternative.id,
        event=event,
        base_version=base_version,
        target_version=target_version,
        rollback_version=rollback_version,
        status=status,
        created_at=now,
        updated_at=now,
        deadlines=Deadlines(
            shipper=shipper_deadline,
            forwarder=forwarder,
            dispatch=dispatch,
            cy_cutoff=cy_cutoff,
            basis=basis,
        ),
        shipper_confirmations=confirmations,
        notified_shipper_ids=notified,
        automatic=automatic,
        requests=requests.items,
        cancellation=charge,
        log=log,
    )


def _automatic_actions(
    ctx: _Ctx, changed: list[tuple[str, dict[str, int], dict[str, int]]]
) -> list[AutomaticAction]:
    actions: list[AutomaticAction] = []
    names = {"AIR": "항공", "UNASSIGNED": "미배정", None: "없음"}
    moved_ids = set()
    for move in ctx.alternative.diff.moved_lots:
        moved_ids.add(move.lot_id)
        pull = f", {move.pull_forward_days}일 당김" if move.pull_forward_days else ""
        actions.append(
            AutomaticAction(
                kind="slot_reassignment",
                description=(
                    f"{move.lot_id}({short_name(ctx.shipper_name(move.shipper_id))} "
                    f"{move.pallets}파렛트): {names.get(move.from_location, move.from_location)} → "
                    f"{names.get(move.to_location, move.to_location)}{pull}"
                ),
                lot_ids=[move.lot_id],
                container_id=move.to_location
                if _category(move.to_location) == "sea"
                else move.from_location,
            )
        )
    for cid, before, after in changed:
        if set(before) | set(after) <= moved_ids:
            continue
        actions.append(
            AutomaticAction(
                kind="slot_reassignment",
                description=(
                    f"{cid} 적재 {sum(before.values())}→{sum(after.values())}파렛트 "
                    f"({_composition_text(ctx, before, after)})"
                ),
                lot_ids=sorted(set(before) | set(after)),
                container_id=cid,
            )
        )
    before_sig = _route_signature(ctx.routes_before)
    after_sig = _route_signature(ctx.alternative.routes)
    packhouses = {
        p.id: p.name for p in [*ctx.scenario_before.packhouses, *ctx.scenario_after.packhouses]
    }
    for voyage_pickup in ctx.alternative.routes:
        for route in voyage_pickup.pickup.routes:
            key = (voyage_pickup.voyage_id, route.truck_id)
            if before_sig.get(key) == after_sig.get(key):
                continue
            stops = [packhouses.get(stop.packhouse_id, stop.packhouse_id) for stop in route.stops]
            lot_ids = [x for stop in route.stops for x in stop.lot_ids]
            actions.append(
                AutomaticAction(
                    kind="truck_dispatch",
                    description=(
                        f"{route.truck_id} {voyage_pickup.voyage_id}: {' → '.join(stops)} → 부산신항, "
                        f"{route.load_pallets}파렛트, 출발 {fmt(route.earliest_departure)}~"
                        f"{fmt(route.latest_departure)}, 항만 도착 예정 {fmt(route.port_eta)}"
                    ),
                    lot_ids=lot_ids,
                    truck_id=route.truck_id,
                )
            )
    for key in sorted(set(before_sig) - set(after_sig)):
        actions.append(
            AutomaticAction(
                kind="truck_dispatch",
                description=f"{key[1]} {key[0]} 배차 취소",
                truck_id=key[1],
            )
        )
    plan_before, plan_after = ctx.plan_before, ctx.alternative.plan
    text = (
        f"정산 재계산: 화주 총비용 {plan_before.total_shared_cost:,}→"
        f"{plan_after.total_shared_cost:,}원, 플랫폼 성과보수 {plan_before.total_fee:,}→"
        f"{plan_after.total_fee:,}원"
    )
    charge = ctx.alternative.diff.cancellation
    if charge and charge.fee > 0:
        transfers = ", ".join(
            f"{short_name(ctx.shipper_name(t.shipper_id))} {t.amount:,}원" for t in charge.transfers
        )
        text += (
            f". 취소 부담금 {short_name(ctx.shipper_name(charge.payer_shipper_id))} "
            f"{charge.fee:,}원 → {transfers}"
        )
    actions.append(AutomaticAction(kind="settlement_recompute", description=text))
    return actions
