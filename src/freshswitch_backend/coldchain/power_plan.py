from collections.abc import Iterable
from datetime import datetime, timedelta

from pydantic import BaseModel

from freshswitch_backend.coldchain.models import (
    GensetCandidate,
    LotPowerPlan,
    PowerPlan,
    PowerSegment,
)
from freshswitch_backend.domain.models import Quantity
from freshswitch_backend.planning.store import PlanSession
from freshswitch_backend.routing.models import GateStatus, StopVisit, TruckRoute

BATTERY_GENSET = "배터리 젯셋(Thermo King E-COOLPAC)"
POWER_RULE = (
    "로트마다 집하 경로의 항만 도착 시각에서 게이트 대기(혼잡 시간대면 혼잡 대기)와 "
    "게이트 통과 뒤 플러그 연결까지를 무전원 예상 구간으로 더한다. 트럭 운행 중에는 냉장트럭 "
    "리퍼가 돈다고 본다. 무전원 예상 분이 사전 배정 기준 이상이면 같은 트럭 회차 로트를 묶어 "
    "배터리 젯셋 사전 배정 후보로 낸다. 실행 지시로 바꾸지 않는다"
)


def _q(value: float, unit: str, source: str) -> Quantity:
    return Quantity(value=value, unit=unit, source=source)


class PowerPlanParams(BaseModel):
    gate_wait_off_peak_minutes: Quantity = _q(
        20, "분", "가정: 비혼잡 시간 터미널 게이트 대기. 대기 중 트럭 시동을 끄면 리퍼도 멈춘다"
    )
    gate_wait_peak_minutes: Quantity = _q(
        60,
        "분",
        "가정: 항만 게이트 혼잡 시간대(시나리오 gate_peak_windows)에 도착하면 게이트 대기",
    )
    plug_wait_minutes: Quantity = _q(
        30, "분", "가정: 게이트 통과 → 적입·장치장 이동 → 리퍼 플러그 연결까지 무전원"
    )
    pre_assign_threshold_minutes: Quantity = _q(
        60,
        "분",
        "가정: 계획 시점 무전원 예상이 60분 이상이면 배터리 젯셋 사전 배정 후보. "
        "실행 중 젯셋 배정 기준(연속 단전 30분)의 2배로 잡아 비혼잡 도착은 거른다",
    )


TRUCK_BASIS = "계획 집하 경로(VRP): 산지 상차 도착 → 항만 도착. 가정: 냉장트럭 리퍼 가동"
NO_ROUTE_BASIS = "가정: 집하 경로가 없어 도착 시각을 모른다. 비혼잡 게이트 대기로 본다"


def _route_of(session: PlanSession, lot_id: str) -> tuple[TruckRoute, StopVisit] | None:
    for pickup in session.routes:
        for route in pickup.pickup.routes:
            for stop in route.stops:
                if lot_id in stop.lot_ids:
                    return route, stop
    return None


def _minutes(start: datetime, end: datetime) -> float:
    return round(max(0.0, (end - start).total_seconds() / 60), 1)


def _gate_basis(status: GateStatus | None, params: PowerPlanParams) -> tuple[float, str]:
    if status == "peak":
        return params.gate_wait_peak_minutes.value, params.gate_wait_peak_minutes.source
    if status == "avoided":
        return (
            params.gate_wait_off_peak_minutes.value,
            f"{params.gate_wait_off_peak_minutes.source}. 경로가 혼잡 시간대를 피해 출발을 옮겼다",
        )
    if status is None:
        return params.gate_wait_off_peak_minutes.value, NO_ROUTE_BASIS
    return params.gate_wait_off_peak_minutes.value, params.gate_wait_off_peak_minutes.source


def _segments(
    found: tuple[TruckRoute, StopVisit] | None, params: PowerPlanParams
) -> list[PowerSegment]:
    route, stop = found if found else (None, None)
    gate_minutes, gate_basis = _gate_basis(route.gate_status if route else None, params)
    plug_minutes = params.plug_wait_minutes.value
    arrival = route.port_eta if route else None
    gate_end = arrival + timedelta(minutes=gate_minutes) if arrival else None
    plug_end = gate_end + timedelta(minutes=plug_minutes) if gate_end else None
    trucking = PowerSegment(
        stage="trucking",
        start=stop.eta if stop else None,
        end=arrival,
        minutes=_minutes(stop.eta, arrival) if stop and arrival else 0.0,
        powered=True,
        basis=TRUCK_BASIS if route else NO_ROUTE_BASIS,
    )
    return [
        trucking,
        PowerSegment(
            stage="gate",
            start=arrival,
            end=gate_end,
            minutes=gate_minutes,
            powered=False,
            basis=gate_basis,
        ),
        PowerSegment(
            stage="plug",
            start=gate_end,
            end=plug_end,
            minutes=plug_minutes,
            powered=False,
            basis=params.plug_wait_minutes.source,
        ),
    ]


def lot_power_plans(
    session: PlanSession, params: PowerPlanParams | None = None
) -> list[LotPowerPlan]:
    params = params or PowerPlanParams()
    threshold = params.pre_assign_threshold_minutes.value
    out: list[LotPowerPlan] = []
    for a in session.plan.assignments:
        if a.mode != "sea" or a.container_id is None or a.voyage_id is None:
            continue
        found = _route_of(session, a.lot_id)
        segments = _segments(found, params)
        unpowered = round(sum(s.minutes for s in segments if not s.powered), 1)
        route = found[0] if found else None
        out.append(
            LotPowerPlan(
                lot_id=a.lot_id,
                shipper_id=a.shipper_id,
                container_id=a.container_id,
                voyage_id=a.voyage_id,
                truck_id=route.truck_id if route else None,
                trip=route.trip if route else None,
                gate_status=route.gate_status if route else None,
                segments=segments,
                unpowered_minutes=unpowered,
                threshold_minutes=threshold,
                exceeds=unpowered >= threshold,
            )
        )
    return out


def _window(lots: list[LotPowerPlan]) -> tuple[datetime | None, datetime | None]:
    starts = [s.start for lot in lots for s in lot.segments if not s.powered and s.start]
    ends = [s.end for lot in lots for s in lot.segments if not s.powered and s.end]
    return (min(starts) if starts else None, max(ends) if ends else None)


def genset_candidates(lots: Iterable[LotPowerPlan]) -> list[GensetCandidate]:
    groups: dict[tuple[str, str, int | None], list[LotPowerPlan]] = {}
    for lot in lots:
        if not lot.exceeds:
            continue
        if lot.truck_id is not None:
            group = (lot.voyage_id, lot.truck_id, lot.trip)
        else:
            group = (lot.voyage_id, f"container:{lot.container_id}", None)
        groups.setdefault(group, []).append(lot)
    out: list[GensetCandidate] = []
    for (voyage_id, unit, trip), members in groups.items():
        truck_id = None if unit.startswith("container:") else unit
        start, end = _window(members)
        minutes = max(m.unpowered_minutes for m in members)
        peak = any(m.gate_status == "peak" for m in members)
        where = f"{truck_id} {trip}회차" if truck_id else members[0].container_id
        reason = (
            f"{where} 무전원 예상 {minutes:.0f}분(기준 {members[0].threshold_minutes:.0f}분)"
            + (". 게이트 혼잡 시간대 도착" if peak else "")
        )
        key = f"genset:{voyage_id}:{unit}" + (f":{trip}" if trip is not None else "")
        out.append(
            GensetCandidate(
                key=key,
                equipment=BATTERY_GENSET,
                voyage_id=voyage_id,
                container_ids=sorted({m.container_id for m in members}),
                truck_id=truck_id,
                trip=trip,
                lot_ids=[m.lot_id for m in members],
                window_start=start,
                window_end=end,
                unpowered_minutes=minutes,
                threshold_minutes=members[0].threshold_minutes,
                reason=reason,
                gate_peak=peak,
            )
        )
    return out


def power_plan(
    session: PlanSession,
    params: PowerPlanParams | None = None,
    shipper_ids: Iterable[str] | None = None,
) -> PowerPlan:
    params = params or PowerPlanParams()
    lots = lot_power_plans(session, params)
    if shipper_ids is not None:
        mine = set(shipper_ids)
        lots = [lot for lot in lots if lot.shipper_id in mine]
    return PowerPlan(
        plan_id=session.plan_id,
        lots=lots,
        candidates=genset_candidates(lots),
        params=dict(params),
        rule=POWER_RULE,
    )
