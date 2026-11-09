from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime

from freshswitch_backend.coldchain.actions import ActionContext, build_action
from freshswitch_backend.coldchain.models import (
    ActionKind,
    Alert,
    CurrentReading,
    ExcursionSummary,
    HandoverOutageKind,
    HandoverState,
    LossSummary,
    LotLoss,
    OutageSummary,
    ProductBand,
    Reading,
    ShipperLoss,
    Stage,
    TimelinePoint,
    TwinStatus,
)
from freshswitch_backend.coldchain.params import ColdchainParams
from freshswitch_backend.coldchain.simulator import handover_steps
from freshswitch_backend.domain.models import Plan, Scenario, Voyage
from freshswitch_backend.execution.orders import fmt
from freshswitch_backend.optimize.costs import CostModel

LOSS_RULE = (
    "노출 지수 = 단전 몫 × 누적 단전 ÷ 5시간 56분 + 온도 몫 × 적정 상한 초과 °C·분 ÷ (5시간 56분 × 3°C). "
    "센서 손실률 = 기본 손실률 + (상한 − 기본) × min(1, 노출 지수 × 0.846). "
    "지수 1이면 딸기 1.5% → 7%. 로트 예상 손실률 = max(계획 대기 손실, 센서 손실률) + 조기 수확 손실. "
    "계획의 대기일 손실은 단전 위험의 사전 추정이라 센서 값과 겹쳐 더하지 않는다"
)


@dataclass
class LotContext:
    lot_id: str
    shipper_id: str
    shipper_name: str
    product_id: str
    pallets: int
    value: float
    base_rate: float
    max_rate: float
    storage_rate: float
    early_rate: float

    @property
    def planned_rate(self) -> float:
        return self.storage_rate + self.early_rate


@dataclass
class TwinContext:
    container_id: str
    stage: Stage
    plan_id: str | None = None
    scenario_id: str | None = None
    voyage: Voyage | None = None
    container_kind: str = "40RH-CA"
    destination: str | None = None
    products: list[ProductBand] = field(default_factory=list)
    band_min_c: float = 0.0
    band_max_c: float = 2.0
    lots: list[LotContext] = field(default_factory=list)

    def action_context(self) -> ActionContext:
        return ActionContext(
            container_id=self.container_id,
            stage=self.stage,
            voyage=self.voyage,
            container_kind=self.container_kind,
            destination=self.destination,
            products=self.products,
            band_min_c=self.band_min_c,
            band_max_c=self.band_max_c,
            lot_ids=[x.lot_id for x in self.lots],
            pallets=sum(x.pallets for x in self.lots),
        )


def build_context(
    container_id: str,
    stage: Stage,
    params: ColdchainParams,
    plan_id: str | None = None,
    scenario: Scenario | None = None,
    plan: Plan | None = None,
) -> TwinContext:
    ctx = TwinContext(
        container_id=container_id,
        stage=stage,
        band_min_c=params.default_temp_min_c.value,
        band_max_c=params.default_temp_max_c.value,
    )
    if scenario is None or plan is None:
        return ctx
    ctx.plan_id = plan_id
    ctx.scenario_id = scenario.id
    model = CostModel(scenario)
    container = model.containers.get(container_id)
    if container is None:
        return ctx
    voyage = model.voyage_of(container)
    ctx.voyage = voyage
    ctx.container_kind = container.kind
    port = model.ports.get(voyage.destination)
    ctx.destination = port.name if port else voyage.destination
    lots = {x.id: x for x in scenario.lots}
    shippers = {x.id: x.name for x in scenario.shippers}
    product_ids: list[str] = []
    for a in plan.assignments:
        lot = lots.get(a.lot_id)
        if a.mode != "sea" or a.container_id != container_id or lot is None:
            continue
        pc = model.params.for_product(lot.product_id)
        wait = model.sea_wait_days(lot, voyage, a.pull_forward_days)
        storage = min(
            pc.max_sea_loss_rate.value,
            pc.sea_base_loss_rate.value + pc.loss_rate_per_wait_day.value * wait,
        )
        ctx.lots.append(
            LotContext(
                lot_id=lot.id,
                shipper_id=lot.shipper_id,
                shipper_name=shippers.get(lot.shipper_id, lot.shipper_id),
                product_id=lot.product_id,
                pallets=lot.pallets,
                value=model.lot_value(lot),
                base_rate=pc.sea_base_loss_rate.value,
                max_rate=pc.max_sea_loss_rate.value,
                storage_rate=storage,
                early_rate=pc.early_harvest_loss_rate_per_day.value * a.pull_forward_days,
            )
        )
        if lot.product_id not in product_ids:
            product_ids.append(lot.product_id)
    products = {p.id: p for p in scenario.products}
    ctx.products = [
        ProductBand(product_id=p.id, name=p.name, temp_min_c=p.temp_min_c, temp_max_c=p.temp_max_c)
        for p in (products[x] for x in product_ids if x in products)
    ]
    if ctx.products:
        ctx.band_max_c = min(p.temp_max_c for p in ctx.products)
        ctx.band_min_c = min(max(p.temp_min_c for p in ctx.products), ctx.band_max_c)
        if ctx.band_max_c <= ctx.band_min_c:
            tolerance = params.point_band_tolerance_c.value
            point = ctx.band_max_c
            ctx.band_min_c = round(point - tolerance, 2)
            ctx.band_max_c = round(point + tolerance, 2)
    return ctx


def exposure_index(outage_minutes: float, degree_minutes: float, params: ColdchainParams) -> float:
    w = params.outage_weight.value
    outage = outage_minutes / params.reference_outage_minutes.value
    thermal = degree_minutes / params.reference_degree_minutes
    return w * outage + (1 - w) * thermal


def sensor_rate(base: float, cap: float, exposure: float, params: ColdchainParams) -> float:
    return base + max(0.0, cap - base) * min(1.0, exposure * params.reference_fraction)


def _lot_rates(
    lot: LotContext, exposure: float, params: ColdchainParams, additive: bool = False
) -> tuple[float, float]:
    sensor = sensor_rate(lot.base_rate, lot.max_rate, exposure, params)
    if additive:
        extra = sensor - lot.base_rate
        return sensor, min(lot.max_rate, lot.storage_rate + extra) + lot.early_rate
    predicted = min(lot.max_rate, max(lot.storage_rate, sensor)) + lot.early_rate
    return sensor, predicted


def _weighted(pairs: list[tuple[float, float]]) -> float:
    total = sum(v for v, _ in pairs)
    if total <= 0:
        return 0.0
    return sum(v * r for v, r in pairs) / total


def container_rates(
    ctx: TwinContext, exposure: float, params: ColdchainParams, additive: bool = False
) -> tuple[float, float | None]:
    if not ctx.lots:
        reference = sensor_rate(
            params.reference_no_outage_loss_rate.value,
            params.reference_max_loss_rate.value,
            exposure,
            params,
        )
        return reference, None
    rates = [(lot.value, _lot_rates(lot, exposure, params, additive)) for lot in ctx.lots]
    sensor = _weighted([(v, r[0]) for v, r in rates])
    predicted = _weighted([(v, r[1]) for v, r in rates])
    return sensor, predicted


def no_outage_rate(ctx: TwinContext, params: ColdchainParams) -> float:
    if not ctx.lots:
        return params.reference_no_outage_loss_rate.value
    return _weighted([(lot.value, lot.base_rate) for lot in ctx.lots])


def loss_summary(
    ctx: TwinContext, exposure: float, params: ColdchainParams, additive: bool = False
) -> LossSummary:
    sensor, predicted = container_rates(ctx, exposure, params, additive)
    summary = LossSummary(
        exposure_index=round(exposure, 4),
        sensor_loss_rate=round(sensor, 6),
        no_outage_loss_rate=round(no_outage_rate(ctx, params), 6),
        reference_outage_loss_rate=params.reference_outage_loss_rate.value,
        rule=LOSS_RULE,
    )
    if not ctx.lots:
        return summary
    lots: list[LotLoss] = []
    for lot in ctx.lots:
        lot_sensor, lot_predicted = _lot_rates(lot, exposure, params, additive)
        planned_krw = round(lot.value * lot.planned_rate)
        predicted_krw = round(lot.value * lot_predicted)
        lots.append(
            LotLoss(
                lot_id=lot.lot_id,
                shipper_id=lot.shipper_id,
                product_id=lot.product_id,
                pallets=lot.pallets,
                lot_value=round(lot.value),
                planned_loss_rate=round(lot.planned_rate, 6),
                sensor_loss_rate=round(lot_sensor, 6),
                predicted_loss_rate=round(lot_predicted, 6),
                planned_loss_krw=planned_krw,
                predicted_loss_krw=predicted_krw,
                delta_krw=predicted_krw - planned_krw,
            )
        )
    shippers: dict[str, ShipperLoss] = {}
    names = {lot.shipper_id: lot.shipper_name for lot in ctx.lots}
    for item in lots:
        entry = shippers.setdefault(
            item.shipper_id,
            ShipperLoss(
                shipper_id=item.shipper_id,
                shipper_name=names[item.shipper_id],
                lot_ids=[],
                lot_value=0,
                planned_loss_krw=0,
                predicted_loss_krw=0,
                delta_krw=0,
            ),
        )
        entry.lot_ids.append(item.lot_id)
        entry.lot_value += item.lot_value
        entry.planned_loss_krw += item.planned_loss_krw
        entry.predicted_loss_krw += item.predicted_loss_krw
        entry.delta_krw += item.delta_krw
    value = sum(x.lot_value for x in lots)
    planned_krw = sum(x.planned_loss_krw for x in lots)
    predicted_krw = sum(x.predicted_loss_krw for x in lots)
    summary.planned_loss_rate = round(_weighted([(x.value, x.planned_rate) for x in ctx.lots]), 6)
    summary.predicted_loss_rate = round(predicted or 0.0, 6)
    summary.cargo_value = value
    summary.planned_loss_krw = planned_krw
    summary.predicted_loss_krw = predicted_krw
    summary.delta_krw = predicted_krw - planned_krw
    summary.lots = lots
    summary.shippers = sorted(shippers.values(), key=lambda s: s.shipper_id)
    return summary


def excursion_of(temp_c: float, ctx: TwinContext) -> float:
    if temp_c > ctx.band_max_c:
        return temp_c - ctx.band_max_c
    if temp_c < ctx.band_min_c:
        return temp_c - ctx.band_min_c
    return 0.0


def _status(
    excursion: float, power_on: bool, continuous: float, params: ColdchainParams
) -> TwinStatus:
    if (
        abs(excursion) >= params.critical_excursion_c.value
        or continuous >= params.critical_continuous_minutes.value
    ):
        return "critical"
    if not power_on or abs(excursion) >= params.warning_excursion_c.value:
        return "warning"
    return "normal"


def _handover_kind(
    power_on: bool, continuous: float, params: ColdchainParams
) -> HandoverOutageKind:
    if power_on:
        return "none"
    if continuous > params.planned_outage_allowance_minutes.value:
        return "abnormal"
    if continuous >= params.planned_outage_warning_minutes.value:
        return "planned_warning"
    return "planned"


def _handover_status(
    excursion: float, power_on: bool, continuous: float, params: ColdchainParams
) -> TwinStatus:
    base = _status(excursion, True, 0.0, params)
    kind = _handover_kind(power_on, continuous, params)
    if kind == "abnormal":
        return "critical"
    if kind == "planned_warning" and base == "normal":
        return "warning"
    return base


def _handover_rule(params: ColdchainParams) -> str:
    allowance = f"{params.planned_outage_allowance_minutes.value:g}"
    return (
        f"계획 단전 허용 {allowance}분(가정) 안의 단전과 그 구간 온도 이탈은 손실에 넣지 않는다. "
        f"연속 단전이 {allowance}분을 넘은 시간과 그 시간의 온도 이탈만 노출 지수에 반영하고, "
        "그만큼 오른 손실률을 계획 손실률에 더한다"
    )


STATUS_LEVEL = {"normal": "info", "warning": "warning", "critical": "critical"}
STATUS_TEXT = {"normal": "경고 해제", "warning": "온도·전원 경고", "critical": "온도·전원 위험"}


@dataclass
class Carry:
    prev: Reading | None = None
    count: int = 0
    cumulative: float = 0.0
    degree_minutes: float = 0.0
    above: float = 0.0
    longest: float = 0.0
    episodes: int = 0
    episode_start: datetime | None = None
    episode_minutes: float = 0.0
    max_temp: float | None = None
    status: TwinStatus | None = None
    cold_storage_done: bool = False
    excess_minutes: float = 0.0
    excess_degree_minutes: float = 0.0


@dataclass
class Evaluation:
    status: TwinStatus
    current: CurrentReading | None
    outage: OutageSummary
    excursion: ExcursionSummary
    exposure: float
    timeline: list[TimelinePoint]
    alerts: list[Alert]
    carry: Carry = field(default_factory=Carry)
    anchors: list[datetime] = field(default_factory=list)
    handover: HandoverState | None = None


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def evaluate(
    readings: list[Reading],
    ctx: TwinContext,
    params: ColdchainParams,
    carry: Carry | None = None,
    anchors: Iterable[datetime] = (),
) -> Evaluation:
    max_gap = params.max_gap_minutes.value
    actx = ctx.action_context()
    alerts: list[Alert] = []
    timeline: list[TimelinePoint] = []
    base = carry or Carry()
    cumulative = base.cumulative
    degree_minutes = base.degree_minutes
    above = base.above
    longest = base.longest
    episodes = base.episodes
    episode_start = base.episode_start
    episode_minutes = base.episode_minutes
    max_temp = base.max_temp
    status = base.status
    cold_storage_done = base.cold_storage_done
    excess_minutes = base.excess_minutes
    excess_degree_minutes = base.excess_degree_minutes
    allowance = params.planned_outage_allowance_minutes.value
    prev = base.prev
    cid = ctx.container_id
    known = sorted(set(anchors))
    used: list[datetime] = []
    pending: list[tuple[int, str]] = []

    emitted: set[str] = set()

    def track(alert: Alert, prefix: str) -> None:
        pending.append((len(alerts), prefix))
        alerts.append(alert)

    def anchor_episode(end: datetime) -> datetime:
        start = episode_start or end
        anchor = next((a for a in known if start <= a <= end), start)
        for index, prefix in pending:
            key = f"{prefix}:{anchor.isoformat()}"
            alert = alerts[index]
            action = alert.action.model_copy(update={"key": key}) if alert.action else None
            alerts[index] = alert.model_copy(update={"key": key, "action": action})
        pending.clear()
        used.append(anchor)
        return anchor

    def emit_action(
        kind: ActionKind, key: str, at: datetime, metrics: dict, prefix: str | None = None
    ) -> None:
        if key in emitted:
            return
        emitted.add(key)
        action = build_action(kind, key, actx, at, metrics)
        alert = Alert(
            key=key,
            container_id=cid,
            level=action.severity,
            kind="action",
            title=action.title,
            message=action.description,
            at=at,
            action=action,
        )
        if prefix is None:
            alerts.append(alert)
        else:
            track(alert, prefix)

    for r in readings:
        if prev is not None:
            dt = min(max(0.0, (r.ts - prev.ts).total_seconds() / 60), max_gap)
            e0 = max(0.0, prev.temp_c - ctx.band_max_c)
            e1 = max(0.0, r.temp_c - ctx.band_max_c)
            if not prev.power_on:
                if prev.handover_step is not None:
                    overrun = max(0.0, episode_minutes + dt - allowance) - max(
                        0.0, episode_minutes - allowance
                    )
                    excess_minutes += overrun
                    excess_degree_minutes += (e0 + e1) / 2 * overrun
                cumulative += dt
                episode_minutes += dt
            degree_minutes += (e0 + e1) / 2 * dt
            if e0 > 0 and e1 > 0:
                above += dt
            elif e0 > 0 or e1 > 0:
                above += dt / 2
        max_temp = r.temp_c if max_temp is None else max(max_temp, r.temp_c)
        handover = r.handover_step is not None
        exposure = (
            exposure_index(excess_minutes, excess_degree_minutes, params)
            if handover
            else exposure_index(cumulative, degree_minutes, params)
        )
        sensor, predicted = container_rates(ctx, exposure, params, handover)
        shown = predicted if predicted is not None else sensor
        if not r.power_on and episode_start is None:
            episode_start = r.ts
            episodes += 1
            episode_minutes = 0.0
            if not handover:
                track(
                    Alert(
                        key=f"power_cut:{r.ts.isoformat()}",
                        container_id=cid,
                        level="warning",
                        kind="power_cut",
                        title=f"{cid} 리퍼 전원 차단",
                        message=(
                            f"{fmt(r.ts)} 전원 차단 감지. 품온 {r.temp_c:.1f}°C "
                            f"(적정 {ctx.band_min_c:g}~{ctx.band_max_c:g}°C)"
                            + (f", 이전 누적 단전 {cumulative:.0f}분" if round(cumulative) else "")
                        ),
                        at=r.ts,
                    ),
                    "power_cut",
                )
        elif r.power_on and episode_start is not None:
            longest = max(longest, episode_minutes)
            anchor = anchor_episode(r.ts)
            alerts.append(
                Alert(
                    key=f"power_restored:{anchor.isoformat()}",
                    container_id=cid,
                    level="info",
                    kind="power_restored",
                    title=f"{cid} 복전 확인" if handover else f"{cid} 전원 복구",
                    message=(
                        f"단전 {episode_minutes:.0f}분 만에 복전. 허용 {allowance:g}분(가정) 초과 "
                        f"{excess_minutes:.0f}분만 손실에 반영, 품온 {r.temp_c:.1f}°C, "
                        f"예상 손실률 {_pct(shown)}"
                        if handover
                        else f"단전 {episode_minutes:.0f}분 만에 복구. "
                        f"누적 단전 {cumulative:.0f}분, "
                        f"품온 {r.temp_c:.1f}°C, 예상 손실률 {_pct(shown)}"
                    ),
                    at=r.ts,
                )
            )
            episode_start = None
            episode_minutes = 0.0
        continuous = episode_minutes if episode_start is not None else 0.0
        longest = max(longest, continuous)
        excursion = excursion_of(r.temp_c, ctx)
        current_status = (
            _handover_status(excursion, r.power_on, continuous, params)
            if handover
            else _status(excursion, r.power_on, continuous, params)
        )
        if current_status != status and not (status is None and current_status == "normal"):
            handover_text = (
                {
                    "warning": (
                        "계획 단전 주의",
                        (
                            f"계획 단전 {continuous:.0f}분 경과, 허용 {allowance:g}분(가정)까지 "
                            f"{allowance - continuous:.0f}분 남음. "
                        ),
                    ),
                    "critical": (
                        "이상 단전 전환",
                        f"연속 단전 {continuous:.0f}분으로 허용 {allowance:g}분(가정) 초과. ",
                    ),
                }.get(current_status)
                if handover and not r.power_on
                else None
            )
            alerts.append(
                Alert(
                    key=f"status:{current_status}:{r.ts.isoformat()}",
                    container_id=cid,
                    level=STATUS_LEVEL[current_status],
                    kind="status_change",
                    title=f"{cid} {handover_text[0] if handover_text else STATUS_TEXT[current_status]}",
                    message=(
                        (handover_text[1] if handover_text else "")
                        + f"품온 {r.temp_c:.1f}°C(적정 {ctx.band_min_c:g}~{ctx.band_max_c:g}°C, "
                        f"이탈 {excursion:+.1f}°C), 전원 {'켜짐' if r.power_on else '꺼짐'}, "
                        + (f"누적 단전 {cumulative:.0f}분, " if round(cumulative) else "")
                        + f"예상 손실률 {_pct(shown)}"
                    ),
                    at=r.ts,
                )
            )
        status = current_status
        metrics = {
            "temp_c": r.temp_c,
            "continuous": continuous,
            "cumulative": cumulative,
            "rate": shown,
            "readings": base.count + len(timeline) + 1,
        }
        if handover:
            if episode_start is not None and continuous > allowance:
                kind = "terminal_priority_plug"
                key = f"{kind}:{episode_start.isoformat()}"
                emit_action(kind, key, r.ts, metrics, kind)
        elif episode_start is not None and ctx.stage != "vessel":
            episode_key = episode_start.isoformat()
            for kind, reached in (
                ("terminal_priority_plug", continuous >= params.plug_request_after_minutes.value),
                ("genset_dispatch", continuous >= params.genset_after_minutes.value),
                ("entry_time_adjust", cumulative >= params.entry_adjust_after_minutes.value),
            ):
                if reached:
                    emit_action(kind, f"{kind}:{episode_key}", r.ts, metrics, kind)
        if (
            not handover
            and not cold_storage_done
            and (
                cumulative >= params.cold_storage_cumulative_minutes.value
                or sensor >= params.cold_storage_loss_rate.value
            )
        ):
            cold_storage_done = True
            emit_action("cold_storage_booking", "cold_storage_booking", r.ts, metrics)
        if (
            not handover
            and episode_start is not None
            and continuous >= params.insurance_continuous_minutes.value
        ):
            emit_action(
                "insurance_evidence",
                f"insurance_evidence:{episode_start.isoformat()}",
                r.ts,
                metrics,
                "insurance_evidence",
            )
        timeline.append(
            TimelinePoint(
                ts=r.ts,
                temp_c=r.temp_c,
                power_on=r.power_on,
                humidity_pct=r.humidity_pct,
                door_open=r.door_open,
                status=current_status,
                cumulative_outage_minutes=round(cumulative, 2),
                degree_minutes=round(degree_minutes, 2),
                sensor_loss_rate=round(sensor, 6),
                predicted_loss_rate=None if predicted is None else round(predicted, 6),
                handover_step=r.handover_step,
                handover_kind=_handover_kind(r.power_on, continuous, params) if handover else None,
                handover_excess_minutes=round(excess_minutes, 2) if handover else None,
            )
        )
        prev = r
    if episode_start is not None and prev is not None:
        anchor_episode(prev.ts)
    seen: set[str] = set()
    unique = []
    for alert in alerts:
        if alert.key not in seen:
            seen.add(alert.key)
            unique.append(alert)
    last = readings[-1] if readings else base.prev
    continuous = episode_minutes if episode_start is not None else 0.0
    current = None
    if last is not None:
        current = CurrentReading(
            ts=last.ts,
            device_id=last.device_id,
            temp_c=last.temp_c,
            power_on=last.power_on,
            humidity_pct=last.humidity_pct,
            door_open=last.door_open,
            excursion_c=round(excursion_of(last.temp_c, ctx), 2),
        )
    insurance = params.insurance_continuous_minutes.value
    last_step = last.handover_step if last is not None else None
    handover_state = (
        None
        if last is None or last_step is None
        else HandoverState(
            current_step=last_step,
            outage_kind=_handover_kind(last.power_on, continuous, params),
            continuous_off_minutes=round(continuous, 2),
            cumulative_off_minutes=round(cumulative, 2),
            excess_minutes=round(excess_minutes, 2),
            warning=params.planned_outage_warning_minutes,
            allowance=params.planned_outage_allowance_minutes,
            steps=handover_steps(params),
            rule=_handover_rule(params),
        )
    )
    return Evaluation(
        status=status or "no_data",
        current=current,
        outage=OutageSummary(
            cumulative_minutes=round(cumulative, 2),
            current_continuous_minutes=round(continuous, 2),
            longest_continuous_minutes=round(longest, 2),
            episodes=episodes,
            reference_minutes=params.reference_outage_minutes.value,
            insurance_threshold_minutes=insurance,
            insurance_eligible=longest >= insurance,
        ),
        excursion=ExcursionSummary(
            band_min_c=ctx.band_min_c,
            band_max_c=ctx.band_max_c,
            degree_minutes=round(degree_minutes, 2),
            minutes_above_band=round(above, 2),
            max_temp_c=max_temp,
        ),
        exposure=(
            exposure_index(excess_minutes, excess_degree_minutes, params)
            if last_step is not None
            else exposure_index(cumulative, degree_minutes, params)
        ),
        timeline=timeline,
        alerts=unique,
        carry=replace(
            base,
            prev=prev,
            count=base.count + len(readings),
            cumulative=cumulative,
            degree_minutes=degree_minutes,
            above=above,
            longest=longest,
            episodes=episodes,
            episode_start=episode_start,
            episode_minutes=episode_minutes,
            max_temp=max_temp,
            status=status,
            cold_storage_done=cold_storage_done,
            excess_minutes=excess_minutes,
            excess_degree_minutes=excess_degree_minutes,
        ),
        anchors=used,
        handover=handover_state,
    )


def downsample(points: list[TimelinePoint], limit: int) -> list[TimelinePoint]:
    if limit <= 0 or len(points) <= limit:
        return points
    if limit == 1:
        return [points[-1]]
    step = (len(points) - 1) / (limit - 1)
    indices = sorted({round(i * step) for i in range(limit)})
    return [points[i] for i in indices]
