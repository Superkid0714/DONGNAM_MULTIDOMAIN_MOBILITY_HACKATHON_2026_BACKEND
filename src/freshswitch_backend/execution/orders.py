import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from freshswitch_backend.domain.models import DEFAULT_PALLET_KG, KST, Lot, Plan, Scenario
from freshswitch_backend.execution.card import (
    EnteredNumbers,
    ExecutionCard,
    GensetInstruction,
    cutoff_timeline,
    gross_cargo_kg,
    mock_booking_no,
    vgm_kg,
)
from freshswitch_backend.execution.models import (
    AutomaticAction,
    ConfirmationReason,
    Deadlines,
    DocumentAction,
    ExecutionOrder,
    ExternalRequest,
    OrderLogEntry,
    RequestBasis,
    ShipperConfirmation,
)
from freshswitch_backend.execution.params import ExecutionParams

if TYPE_CHECKING:
    from freshswitch_backend.coldchain.models import GensetCandidate

CONSOLIDATOR_LABEL = {"platform": "플랫폼(혼재 주관)", "forwarder": "포워더(혼재 주관)"}
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
from freshswitch_backend.routing.models import TruckRoute

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

    def net_kg(self, composition: dict[str, int]) -> float:
        total = 0.0
        for lot_id, pallets in composition.items():
            lot = self.lot(lot_id)
            product = self.products.get(lot.product_id) if lot else None
            total += pallets * (product.net_pallet_kg if product else DEFAULT_PALLET_KG)
        return total

    def kg(self, lot_id: str, pallets: int) -> int:
        lot = self.lot(lot_id)
        product = self.products.get(lot.product_id) if lot else None
        return round(pallets * (product.net_pallet_kg if product else DEFAULT_PALLET_KG))


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


def _add_cy_entry(
    requests: _RequestBuilder,
    ctx: _Ctx,
    cid: str,
    before: dict[str, int],
    after: dict[str, int],
    routes_before_by_lot: dict[str, tuple[str, str, datetime, datetime]],
    routes_after_by_lot: dict[str, tuple[str, str, datetime, datetime]],
    deadline: datetime,
) -> None:
    if not after:
        return
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
    if not new_etas or (before and new_etas == old_etas):
        return
    voyage = ctx.model_after.voyages[ctx.model_after.containers[cid].voyage_id]
    pallets_before = sum(before.values())
    pallets_after = sum(after.values())
    arrivals = ", ".join(f"{truck} {fmt(eta)}" for eta, truck in new_etas)
    previous = ", ".join(f"{truck} {fmt(eta)}" for eta, truck in old_etas) if old_etas else "없음"
    requests.add(
        kind="cy_entry",
        counterparty_role="terminal",
        counterparty=voyage.terminal,
        channel=TERMINAL_CHANNEL,
        deadline=deadline,
        text=(
            f"[CY 반입 예약 변경] {voyage.terminal} {voyage.vessel_name}({voyage.id}) "
            f"{cid} 반입분 {pallets_before}→{pallets_after}파렛트. 트럭 도착 예정: "
            f"{arrivals} (이전: {previous}). 반입 마감 {fmt(voyage.cy_cutoff)}."
        ),
        voyage_id=voyage.id,
        container_id=cid,
        lot_ids=sorted(set(before) | set(after)),
        pallets_before=pallets_before,
        pallets_after=pallets_after,
        lot_pallets_before=dict(before),
        lot_pallets_after=dict(after),
    )


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
    initiator_shipper_id: str | None = None,
    infer_event_initiator: bool = True,
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
    gated_cutoffs = [
        voyages_before[v].cy_cutoff
        for v in affected_voyages
        if v in voyages_before and voyages_before[v].cy_cutoff <= now
    ]
    dispatch_bound = True
    if departures:
        dispatch = min(departures)
        dispatch_basis = (
            f"집하 트럭 최종 출발 {fmt(dispatch)} "
            f"(CY 마감 {fmt(cy_cutoff)} − 집하 리드 "
            f"{(cy_cutoff - dispatch).total_seconds() / 3600:.1f}시간)"
        )
    elif gated_cutoffs:
        dispatch = cy_cutoff
        dispatch_bound = False
        dispatch_basis = (
            f"집하 경로 없음, CY 반입 마감 {fmt(min(gated_cutoffs))}이 지나 컨테이너가 이미 "
            "터미널에 반입됨 → 집하 출발 기한 없음"
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
        timeline = cutoff_timeline(voyage, p)
        basis = RequestBasis(
            head=head,
            container_kind=container.kind,
            si_cutoff=timeline.si_cutoff,
            vgm_cutoff=timeline.vgm_cutoff,
            cy_cutoff=timeline.cy_cutoff,
            net_kg_before=ctx.net_kg(before),
            net_kg_after=ctx.net_kg(after),
            setting=_scenario_setting(scenario_after, lot_ids),
        )
        common = {
            "voyage_id": voyage.id,
            "container_id": cid,
            "lot_ids": lot_ids,
            "pallets_before": pallets_before,
            "pallets_after": pallets_after,
            "lot_pallets_before": dict(before),
            "lot_pallets_after": dict(after),
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
                saas_basis=basis,
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
                saas_basis=basis,
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
                saas_basis=basis,
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
        _add_cy_entry(
            requests,
            ctx,
            cid,
            before,
            after,
            routes_before_by_lot,
            routes_after_by_lot,
            booking_deadline,
        )
    changed_ids = {cid for cid, _, _ in changed}
    for cid in container_ids:
        if cid in changed_ids:
            continue
        composition = _composition(alternative.plan, cid)
        if composition:
            _add_cy_entry(
                requests,
                ctx,
                cid,
                composition,
                composition,
                routes_before_by_lot,
                routes_after_by_lot,
                booking_deadline,
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
    candidates = [dispatch, *request_deadlines] if dispatch_bound else request_deadlines
    forwarder = min(candidates, default=dispatch)
    shipper_deadline = forwarder - timedelta(hours=p.forwarder_processing_hours.value)
    basis = (
        f"화주 확인 = 포워더 확정 기한 − 포워더 처리 {p.forwarder_processing_hours.value:g}시간(가정). "
        f"포워더 확정 = min(집하 출발 기한, 제3자 요청 회신 기한). {dispatch_basis}. "
        f"선사 부킹 변경 회신 = CY 마감 − {p.booking_amendment_lead_hours.value:g}시간(가정)"
    )

    initiator = initiator_shipper_id
    if infer_event_initiator and event_lot:
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
            if move.lot_id == event_lot and sid == initiator:
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
            note="; ".join(alternative.warnings) or None,
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


SAAS_REQUEST_KINDS = ("booking_amendment", "booking_cancel", "booking_new")
CONTAINER_COUNT_DELTA = {"booking_new": 1, "booking_cancel": -1}
DOCUMENT_ACTION_LABEL: dict[DocumentAction, str] = {
    "si_vgm_correction": "SI·VGM 정정",
    "booking_change": "부킹 변경",
}
PLAIN_GLOSSARY: tuple[tuple[str, str], ...] = (
    ("부킹", "배 자리 예약"),
    ("SI(선적지시서)", "배에 싣는 짐 목록 서류"),
    ("VGM", "컨테이너째 잰 총무게 신고"),
    ("SI·VGM 정정", "배 예약은 그대로 두고 짐 목록·무게 신고만 고침"),
    ("부킹 변경", "배에 싣는 컨테이너 대수를 늘리거나 줄임"),
    ("CY 반입", "항구 컨테이너 장치장에 트럭으로 들여놓기"),
    ("리퍼 전원 연결", "냉장 컨테이너에 전기를 꽂아 냉장 유지"),
    ("항공 예약", "비행기 화물칸 자리 예약"),
    ("배터리 젯셋", "전기가 없는 동안 냉장을 이어 주는 배터리 냉각 장치"),
)
PLAIN_TEXT: dict[str, str] = {
    "si_vgm_correction": (
        "배에 싣는 짐 목록과 무게 신고만 고칩니다. 배 자리 예약과 컨테이너 대수는 그대로입니다."
    ),
    "booking_new": (
        "배에 싣는 냉장 컨테이너를 1대 늘립니다. 선사가 받아 주면 늘어난 컨테이너에 실어 보냅니다."
    ),
    "booking_cancel": (
        "배에 싣던 냉장 컨테이너 1대를 뺍니다. 짐이 어디로 옮겨 가는지는 변경 내용에서 확인하세요."
    ),
    "reefer_plug": "항구에 들어온 냉장 컨테이너에 바로 전기를 꽂아 달라고 터미널에 요청합니다.",
    "reefer_plug_extension": (
        "배가 늦게 떠서 항구에서 기다리는 동안 냉장 컨테이너 전기를 계속 켜 두도록 요청합니다."
    ),
    "cy_entry": "트럭이 항구 장치장에 짐을 들여놓는 예약 시간을 바꿉니다.",
    "air_booking": "이 출하를 비행기로 보내려고 항공사에 화물칸 자리를 예약합니다.",
    "air_cancel": "잡아 둔 비행기 화물칸 자리를 취소합니다. 이 출하는 비행기로 가지 않습니다.",
    "genset": (
        "항구 게이트에서 전기가 끊기는 시간이 길어 보여 배터리 냉각 장치를 미리 붙입니다. "
        "냉장은 그대로 이어집니다."
    ),
}


def container_count_delta(request: ExternalRequest) -> int:
    return CONTAINER_COUNT_DELTA.get(request.kind, 0)


def container_swaps(
    requests: list[ExternalRequest], scenario: Scenario
) -> dict[int, tuple[str, str]]:
    kinds = {c.id: c.kind for c in scenario.containers}
    groups: dict[tuple[str | None, str | None], list[int]] = defaultdict(list)
    for i, r in enumerate(requests):
        if r.kind in SAAS_REQUEST_KINDS:
            kind = r.saas_basis.container_kind if r.saas_basis else kinds.get(r.container_id or "")
            groups[(r.voyage_id, kind)].append(i)
    swaps: dict[int, tuple[str, str]] = {}
    for (_, kind), members in groups.items():
        if kind is None or sum(container_count_delta(requests[i]) for i in members) != 0:
            continue
        cancels = [i for i in members if requests[i].kind == "booking_cancel"]
        news = [i for i in members if requests[i].kind == "booking_new"]
        for old, new in zip(cancels, news, strict=True):
            pair = (requests[old].container_id or "", requests[new].container_id or "")
            swaps[old] = pair
            swaps[new] = pair
    return swaps


def document_action(
    request: ExternalRequest, swap: tuple[str, str] | None = None
) -> DocumentAction | None:
    if request.kind not in SAAS_REQUEST_KINDS:
        return None
    if swap is not None or container_count_delta(request) == 0:
        return "si_vgm_correction"
    return "booking_change"


def plain_text(request: ExternalRequest, swap: tuple[str, str] | None = None) -> str:
    if document_action(request, swap) == "si_vgm_correction":
        return PLAIN_TEXT["si_vgm_correction"]
    if request.kind == "reefer_plug" and request.text.startswith("[리퍼 전원 연결 연장"):
        return PLAIN_TEXT["reefer_plug_extension"]
    return PLAIN_TEXT[request.kind]


def _net_per_pallet(scenario: Scenario, lot_ids: list[str]) -> float:
    products = {p.id: p for p in scenario.products}
    weighted = 0.0
    pallets = 0
    for lot in scenario.lots:
        if lot.id not in lot_ids:
            continue
        product = products.get(lot.product_id)
        weighted += lot.pallets * (product.net_pallet_kg if product else DEFAULT_PALLET_KG)
        pallets += lot.pallets
    return weighted / pallets if pallets else DEFAULT_PALLET_KG


def _net_kg(scenario: Scenario, composition: dict[str, int]) -> float:
    products = {p.id: p for p in scenario.products}
    lots = {lot.id: lot for lot in scenario.lots}
    total = 0.0
    for lot_id, pallets in composition.items():
        lot = lots.get(lot_id)
        product = products.get(lot.product_id) if lot else None
        total += pallets * (product.net_pallet_kg if product else DEFAULT_PALLET_KG)
    return total


def _scenario_setting(scenario: Scenario, lot_ids: list[str]) -> str:
    products = {p.id: p for p in scenario.products}
    found = sorted({lot.product_id for lot in scenario.lots if lot.id in lot_ids})
    texts = []
    for pid in found:
        product = products.get(pid)
        if product is None:
            continue
        gas = ""
        if product.o2_pct is not None or product.co2_pct is not None:
            gas = f", O2 {product.o2_pct:g}%·CO2 {product.co2_pct:g}%"
        texts.append(f"{product.name} {product.temp_min_c:g}~{product.temp_max_c:g}°C{gas}")
    return "; ".join(texts) or "품목 조건 없음(선사 기본 설정)"


def _scenario_basis(
    request: ExternalRequest, scenario: Scenario, params: ExecutionParams
) -> RequestBasis | None:
    voyage = next((v for v in scenario.voyages if v.id == request.voyage_id), None)
    container = next((c for c in scenario.containers if c.id == request.container_id), None)
    if voyage is None or container is None:
        return None
    ports = {port.id: port.name for port in scenario.ports}
    cutoffs = cutoff_timeline(voyage, params)
    if request.lot_pallets_before or request.lot_pallets_after:
        net_before = _net_kg(scenario, request.lot_pallets_before)
        net_after = _net_kg(scenario, request.lot_pallets_after)
    else:
        per_pallet = _net_per_pallet(scenario, request.lot_ids)
        net_before = per_pallet * request.pallets_before
        net_after = per_pallet * request.pallets_after
    return RequestBasis(
        head=(
            f"{voyage.vessel_name}({voyage.id}, {voyage.terminal}, ETD {fmt(voyage.etd)}) "
            f"{ports.get(voyage.origin, voyage.origin)}→"
            f"{ports.get(voyage.destination, voyage.destination)}"
        ),
        container_kind=container.kind,
        si_cutoff=cutoffs.si_cutoff,
        vgm_cutoff=cutoffs.vgm_cutoff,
        cy_cutoff=cutoffs.cy_cutoff,
        net_kg_before=net_before,
        net_kg_after=net_after,
        setting=_scenario_setting(scenario, request.lot_ids),
    )


def _booking_label(
    plan_id: str, container_id: str, numbers: Mapping[str, EnteredNumbers] | None
) -> str:
    entered = (numbers or {}).get(container_id)
    if entered is not None and entered.booking_no:
        return entered.booking_no
    return f"{mock_booking_no(plan_id, container_id)}(모의)"


def saas_request_text(
    plan_id: str,
    request: ExternalRequest,
    scenario: Scenario,
    params: ExecutionParams | None = None,
    swap: tuple[str, str] | None = None,
    numbers: Mapping[str, EnteredNumbers] | None = None,
) -> str:
    p = params or ExecutionParams()
    action = document_action(request, swap)
    if action is None:
        return request.text
    basis = request.saas_basis or _scenario_basis(request, scenario, p)
    if basis is None:
        return request.text
    pb, pa = request.pallets_before, request.pallets_after
    gb = gross_cargo_kg(basis.net_kg_before, pb, p)
    ga = gross_cargo_kg(basis.net_kg_after, pa, p)
    tare = p.container_tare_kg.value
    cid = request.container_id or ""
    booking = _booking_label(plan_id, cid, numbers)
    head = basis.head
    kind = basis.container_kind
    lots = ", ".join(request.lot_ids) or "없음"
    found = re.search(r"화주별 파렛트: (.+?)\. ", request.text)
    if found:
        lots = f"{lots}. 화주별 파렛트: {found.group(1)}"
    timeline = (
        f"SI 마감 {fmt(basis.si_cutoff)}, VGM 마감 {fmt(basis.vgm_cutoff)}, "
        f"CY 반입 마감 {fmt(basis.cy_cutoff)}"
    )
    if swap is not None:
        source, target = swap
        return (
            f"[SI 정정 + VGM 재제출 요청] {head} 부킹 {_booking_label(plan_id, source, numbers)} "
            f"컨테이너 {source}→{target} 교체({kind}), 컨테이너 수·종류가 같아 "
            f"부킹은 그대로 둡니다. 이 요청 대상 {cid} 적재 {pb}→{pa}파렛트, 화물 총중량 "
            f"{gb:,}→{ga:,}kg(순중량 + 파렛트·포장)으로 SI 정정하고 VGM "
            f"{vgm_kg(gb, p) if pb else 0:,}→{vgm_kg(ga, p) if pa else 0:,}kg"
            f"(컨테이너 자중 {tare:,.0f}kg 포함)으로 재제출합니다. 대상 출하: {lots}. "
            f"{timeline}. 회신 기한 {fmt(request.deadline)}."
        )
    if action == "si_vgm_correction":
        return (
            f"[SI 정정 + VGM 재제출 요청] {head} 부킹 {booking}의 컨테이너 {cid}"
            f"({kind}) 적재를 {pb}→{pa}파렛트, 화물 총중량 {gb:,}→{ga:,}kg"
            f"(순중량 + 파렛트·포장)으로 SI 정정하고 VGM {vgm_kg(gb, p):,}→{vgm_kg(ga, p):,}kg"
            f"(컨테이너 자중 {tare:,.0f}kg 포함)으로 재제출합니다. 컨테이너 수·종류가 같아 "
            f"부킹은 그대로 둡니다. 대상 출하: {lots}. {timeline}. "
            f"회신 기한 {fmt(request.deadline)}."
        )
    if container_count_delta(request) < 0:
        return (
            f"[부킹 변경 요청: 컨테이너 1대 취소] {head} 부킹 {booking}의 컨테이너 {cid}"
            f"({kind}, {pb}파렛트·화물 총중량 {gb:,}kg)를 취소합니다. "
            f"대상 출하: {lots}. CA 장비 반납 일정과 취소 조건 회신 부탁드립니다. "
            f"회신 기한 {fmt(request.deadline)}."
        )
    return (
        f"[부킹 변경 요청: 컨테이너 1대 추가] {head} {kind} 1대({cid}), "
        f"{pa}파렛트·화물 총중량 {ga:,}kg(VGM {vgm_kg(ga, p):,}kg, 컨테이너 자중 "
        f"{tare:,.0f}kg 포함). 대상 출하: {lots}. 설정: {basis.setting}. 장비 PTI·공컨테이너 "
        f"반출 일정 회신 부탁드립니다. {timeline}. 회신 기한 {fmt(request.deadline)}."
    )


def saas_requests(
    plan_id: str,
    requests: list[ExternalRequest],
    scenario: Scenario,
    params: ExecutionParams | None = None,
    numbers: Mapping[str, EnteredNumbers] | None = None,
) -> list[ExternalRequest]:
    swaps = container_swaps(requests, scenario)
    out = []
    for i, r in enumerate(requests):
        swap = swaps.get(i)
        action = document_action(r, swap)
        out.append(
            r.model_copy(
                update={
                    "text": saas_request_text(plan_id, r, scenario, params, swap, numbers),
                    "document_action": action,
                    "document_action_label": DOCUMENT_ACTION_LABEL[action] if action else None,
                    "plain_text": plain_text(r, swap),
                    "saas_basis": None,
                }
            )
        )
    return out


def without_saas_basis(order: ExecutionOrder) -> ExecutionOrder:
    if all(r.saas_basis is None for r in order.requests):
        return order
    requests = [r.model_copy(update={"saas_basis": None}) for r in order.requests]
    return order.model_copy(update={"requests": requests})


def with_object_particle(word: str) -> str:
    base = re.sub(r"(\s*\([^()]*\))+\s*$", "", word)
    last = base[-1:] if base else ""
    if not "가" <= last <= "힣":
        return f"{word}을(를)"
    has_final = (ord(last) - ord("가")) % 28 != 0
    return f"{word}{'을' if has_final else '를'}"


def genset_instructions(
    candidates: Iterable["GensetCandidate"],
    card: ExecutionCard,
    visible_lot_ids: set[str] | None = None,
) -> list[GensetInstruction]:
    numbers = {c.container_id: c.container_no for c in card.containers}
    out: list[GensetInstruction] = []
    for candidate in candidates:
        lot_ids = [x for x in candidate.lot_ids if visible_lot_ids is None or x in visible_lot_ids]
        if not lot_ids:
            continue
        nos = [numbers.get(cid, cid) for cid in candidate.container_ids]
        where = f"{candidate.truck_id} {candidate.trip}회차" if candidate.truck_id else "트럭 미정"
        window = (
            f"{fmt(candidate.window_start)}~{fmt(candidate.window_end)}"
            if candidate.window_start and candidate.window_end
            else "시각 미정"
        )
        units = ", ".join(
            f"{cid}({no})" for cid, no in zip(candidate.container_ids, nos, strict=True)
        )
        out.append(
            GensetInstruction(
                key=candidate.key,
                equipment=candidate.equipment,
                voyage_id=candidate.voyage_id,
                container_ids=candidate.container_ids,
                container_nos=nos,
                truck_id=candidate.truck_id,
                trip=candidate.trip,
                lot_ids=lot_ids,
                window_start=candidate.window_start,
                window_end=candidate.window_end,
                unpowered_minutes=candidate.unpowered_minutes,
                threshold_minutes=candidate.threshold_minutes,
                text=(
                    f"[배터리 젯셋 사전 배정] {candidate.voyage_id} {where} {units} "
                    f"로트 {', '.join(lot_ids)}: 무전원 예상 {window} "
                    f"{candidate.unpowered_minutes:.0f}분(기준 {candidate.threshold_minutes:.0f}분). "
                    f"{with_object_particle(candidate.equipment)} 게이트 도착 전에 배정·연결합니다. "
                    + ("게이트 혼잡 시간대 도착." if candidate.gate_peak else "")
                ).rstrip(),
                plain_text=PLAIN_TEXT["genset"],
            )
        )
    return out


def _trips(
    routes: list[VoyagePickup],
) -> dict[tuple[str, datetime], list[tuple[str, TruckRoute]]]:
    trips: dict[tuple[str, datetime], list[tuple[str, TruckRoute]]] = {}
    for voyage_pickup in routes:
        for route in voyage_pickup.pickup.routes:
            trips.setdefault((route.truck_id, route.earliest_departure), []).append(
                (voyage_pickup.voyage_id, route)
            )
    return trips


def _trip_key(truck_id: str, parts: list[tuple[str, TruckRoute]]) -> tuple[str, str]:
    return "·".join(dict.fromkeys(voyage_id for voyage_id, _ in parts)), truck_id


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
    trips_before = _trips(ctx.routes_before)
    trips_after = _trips(ctx.alternative.routes)
    keys_before = {_trip_key(truck_id, parts) for (truck_id, _), parts in trips_before.items()}
    keys_after = {_trip_key(truck_id, parts) for (truck_id, _), parts in trips_after.items()}
    cancelled = sorted(keys_before - keys_after)
    released = {
        (voyage_id, truck_id) for voyages, truck_id in cancelled for voyage_id in voyages.split("·")
    }
    emit = {
        _trip_key(truck_id, parts)
        for (truck_id, _), parts in trips_after.items()
        if _trip_key(truck_id, parts) not in keys_before
        or any(
            before_sig.get(key) != after_sig.get(key) or key in released
            for key in ((voyage_id, truck_id) for voyage_id, _ in parts)
        )
    }
    for (truck_id, earliest), parts in trips_after.items():
        voyages, _ = _trip_key(truck_id, parts)
        if (voyages, truck_id) not in emit:
            continue
        visits = [stop for _, route in parts for stop in route.stops]
        if len(parts) > 1:
            unique = {stop.stop_id: stop for stop in reversed(visits)}
            visits = sorted(unique.values(), key=lambda stop: (stop.sequence, stop.eta))
        stops = [packhouses.get(stop.packhouse_id, stop.packhouse_id) for stop in visits]
        lot_ids = list(
            dict.fromkeys(x for _, route in parts for stop in route.stops for x in stop.lot_ids)
        )
        pallets = sum(route.load_pallets for _, route in parts)
        latest = min(route.latest_departure for _, route in parts)
        port_eta = max(route.port_eta for _, route in parts)
        actions.append(
            AutomaticAction(
                kind="truck_dispatch",
                description=(
                    f"{truck_id} {voyages}: {' → '.join(stops)} → 부산신항, "
                    f"{pallets}파렛트, 출발 {fmt(earliest)}~"
                    f"{fmt(latest)}, 항만 도착 예정 {fmt(port_eta)}"
                ),
                lot_ids=lot_ids,
                truck_id=truck_id,
            )
        )
    for voyages, truck_id in cancelled:
        actions.append(
            AutomaticAction(
                kind="truck_dispatch",
                description=f"{truck_id} {voyages} 배차 취소",
                truck_id=truck_id,
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
        recipients = [
            f"{short_name(ctx.shipper_name(t.shipper_id))} {t.amount:,}원" for t in charge.transfers
        ]
        if charge.consolidator_amount > 0:
            recipients.append(
                f"{CONSOLIDATOR_LABEL[charge.consolidator]} {charge.consolidator_amount:,}원"
            )
        text += (
            f". 취소 부담금 {short_name(ctx.shipper_name(charge.payer_shipper_id))} "
            f"{charge.fee:,}원 → {', '.join(recipients)}"
        )
    actions.append(AutomaticAction(kind="settlement_recompute", description=text))
    return actions
