from datetime import datetime

from freshswitch_backend.config import Settings, get_settings
from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.explain import (
    AlternativeInput,
    ContainerFill,
    ExplainContext,
    Explanation,
    ShipperImpact,
    explain,
    render_template,
)
from freshswitch_backend.explain.service import LLM_TIMEOUT_SECONDS
from freshswitch_backend.observability import record_event
from freshswitch_backend.optimize import AIR
from freshswitch_backend.replan.engine import UNASSIGNED, apply_event, locations
from freshswitch_backend.replan.models import (
    AddLotEvent,
    AirSwitchEvent,
    Alternative,
    CancelEvent,
    DelayEvent,
    Event,
    IncreaseEvent,
    ReduceEvent,
    ReplanError,
    ReplanResult,
    VoyageDelay,
)

PLACE = {AIR: "항공", UNASSIGNED: "미배정"}


def _when(value) -> str:
    return value.strftime("%m/%d %H:%M")


def event_summary(scenario: Scenario, event: Event) -> str:
    lots = {lot.id: lot for lot in scenario.lots}
    shippers = {s.id: s.name for s in scenario.shippers}

    def owner(lot_id: str) -> str:
        return shippers.get(lots[lot_id].shipper_id, lots[lot_id].shipper_id)

    match event:
        case CancelEvent(lot_id=lot_id):
            return f"{owner(lot_id)} {lots[lot_id].pallets}파렛트 전량 취소"
        case IncreaseEvent(lot_id=lot_id, new_pallets=new):
            return f"{owner(lot_id)} {lots[lot_id].pallets}→{new}파렛트 증량"
        case ReduceEvent(lot_id=lot_id, new_pallets=new):
            return f"{owner(lot_id)} {lots[lot_id].pallets}→{new}파렛트 감량"
        case AirSwitchEvent(lot_id=lot_id):
            return f"{owner(lot_id)} {lots[lot_id].pallets}파렛트 항공 전환"
        case AddLotEvent(lot=lot):
            return f"{shippers.get(lot.shipper_id, lot.shipper_id)} {lot.pallets}파렛트 신규 출하"
        case DelayEvent():
            cutoffs = _cutoffs_after(scenario, event)
            return ", ".join(_delay_summary(scenario, d, cutoffs) for d in event.delays())
    return "일정 변경"


def _delay_summary(
    scenario: Scenario, delay: VoyageDelay, cutoffs: dict[str, datetime | None]
) -> str:
    voyage = next(v for v in scenario.voyages if v.id == delay.voyage_id)
    cutoff = f"CY 마감 {_when(voyage.cy_cutoff)} 유지"
    applied = cutoffs.get(delay.voyage_id)
    if applied is not None and applied != voyage.cy_cutoff:
        cutoff = f"CY 마감 {_when(voyage.cy_cutoff)}→{_when(applied)} 변경"
    return f"{voyage.vessel_name} 출항 {_when(voyage.etd)}→{_when(delay.new_etd)} 변경({cutoff})"


def _cutoffs_after(scenario: Scenario, event: DelayEvent) -> dict[str, datetime | None]:
    try:
        after = apply_event(scenario, event)
    except ReplanError:
        return {d.voyage_id: d.new_cy_cutoff for d in event.delays()}
    return {v.id: v.cy_cutoff for v in after.voyages if v.id in event.voyage_ids}


def _lot_ids(alt: Alternative) -> set[str]:
    plan = alt.plan
    ids = {a.lot_id for a in plan.assignments} | {u.lot_id for u in plan.unassigned}
    return ids | {m.lot_id for m in alt.diff.moved_lots}


def _tail(reason: str, lot_ids: set[str]) -> str:
    head, sep, tail = reason.partition(": ")
    return tail if sep and head in lot_ids else reason


def _reason(alt: Alternative) -> str | None:
    if alt.feasible:
        return None
    lot_ids = _lot_ids(alt)
    tails = [_tail(r, lot_ids) for r in alt.infeasible_reasons]
    return "; ".join(dict.fromkeys(tails)) or None


def _involved_containers(current: Plan, event: Event, alt: Alternative) -> set[str]:
    where = locations(current)
    involved = {loc for m in alt.diff.moved_lots for loc in (m.from_location, m.to_location)}
    lot_id = getattr(event, "lot_id", None)
    if lot_id:
        involved.add(where.get(lot_id))
    if isinstance(event, DelayEvent):
        voyages = set(event.voyage_ids)
        involved |= {c.container_id for c in alt.diff.containers if c.voyage_id in voyages}
    return {c for c in involved if c}


def _alternative_input(current: Plan, event: Event, alt: Alternative) -> AlternativeInput:
    involved = _involved_containers(current, event, alt)
    fills = [
        ContainerFill(
            container_id=c.container_id,
            before=c.pallets_before,
            after=c.pallets_after,
            capacity=c.capacity_pallets,
        )
        for c in alt.diff.containers
        if (c.pallets_before or c.pallets_after)
        and (c.container_id in involved or c.pallets_before != c.pallets_after)
    ]
    return AlternativeInput(
        id=alt.id,
        label=alt.label,
        feasible=alt.feasible,
        reason=_reason(alt),
        total_cost_delta_krw=alt.cost_delta,
        container_fill=fills,
    )


def _shipper_impacts(scenario: Scenario, alt: Alternative | None) -> list[ShipperImpact]:
    if alt is None:
        return []
    names = {s.id: s.name for s in scenario.shippers}
    impacts = []
    for delta in alt.diff.shippers:
        moves = [m for m in alt.diff.moved_lots if m.shipper_id == delta.shipper_id]
        targets = [PLACE.get(m.to_location, m.to_location) for m in moves if m.to_location]
        if delta.cost_delta == 0 and not moves and not delta.cancellation_adjustment:
            continue
        impacts.append(
            ShipperImpact(
                shipper_id=delta.shipper_id,
                shipper_name=names.get(delta.shipper_id, delta.shipper_id),
                cost_before_krw=delta.cost_before,
                cost_after_krw=delta.cost_after,
                delta_krw=delta.cost_delta,
                moved=", ".join(dict.fromkeys(targets)) or None,
                pull_forward_days=max((m.pull_forward_days for m in moves), default=0),
                cancellation_adjustment_krw=delta.cancellation_adjustment,
            )
        )
    return impacts


def build_explain_context(
    scenario_before: Scenario, current: Plan, result: ReplanResult
) -> ExplainContext:
    recommended = next((a for a in result.alternatives if a.id == result.recommended_id), None)
    return ExplainContext(
        event_type=result.event.type,
        event_summary=event_summary(scenario_before, result.event),
        recommended_alternative_id=result.recommended_id,
        alternatives=[_alternative_input(current, result.event, a) for a in result.alternatives],
        shippers=_shipper_impacts(scenario_before, recommended),
        warnings=list(result.warnings),
    )


def llm_enabled(requested: bool | None, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not settings.explain_use_llm or not settings.llm_api_key or not settings.llm_model_name:
        return False
    return requested is not False


async def explain_replan(
    scenario_before: Scenario,
    current: Plan,
    result: ReplanResult,
    *,
    use_llm: bool = False,
    timeout_s: float = LLM_TIMEOUT_SECONDS,
) -> Explanation:
    ctx = build_explain_context(scenario_before, current, result)
    if use_llm:
        return await explain(ctx, timeout_s=timeout_s)
    record_event(
        "ai.explanation",
        source="template",
        event_type=ctx.event_type,
        fallback_reason="disabled",
        shipper_count=len(ctx.shippers),
    )
    return render_template(ctx)
