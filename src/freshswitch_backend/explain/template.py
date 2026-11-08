from freshswitch_backend.explain.formatting import (
    change_verb,
    format_amount_krw,
    format_fill,
    format_krw,
)
from freshswitch_backend.explain.models import (
    AlternativeInput,
    ExplainContext,
    Explanation,
    ShipperImpact,
    ShipperMessage,
)


def _recommended(ctx: ExplainContext) -> AlternativeInput | None:
    return next(
        (alt for alt in ctx.alternatives if alt.id == ctx.recommended_alternative_id),
        None,
    )


def _shipper_message(ctx: ExplainContext, shipper: ShipperImpact) -> str:
    parts = [f"{shipper.shipper_name}님, {ctx.event_summary} 건으로 적재 계획이 조정되었습니다."]
    if shipper.delta_krw == 0:
        parts.append(f"부담 비용은 {format_krw(shipper.cost_after_krw)}으로 변동 없습니다.")
    else:
        parts.append(
            f"부담 비용이 {format_krw(shipper.cost_before_krw)}에서 "
            f"{format_krw(shipper.cost_after_krw)}으로 {format_amount_krw(shipper.delta_krw)} "
            f"{change_verb(shipper.delta_krw)}."
        )
    if shipper.moved:
        parts.append(f"화물은 {shipper.moved}(으)로 옮겨집니다.")
    if shipper.pull_forward_days:
        parts.append(f"출하를 {shipper.pull_forward_days}일 앞당겨 주셔야 합니다.")
    return " ".join(parts)


def _cost_sentence(delta: int) -> str:
    if delta == 0:
        return "총 비용은 변동 없습니다."
    return f"총 비용은 {format_amount_krw(delta)} {change_verb(delta)}."


def _operator_summary(ctx: ExplainContext) -> str:
    parts = [f"{ctx.event_summary}."]
    recommended = _recommended(ctx)
    if recommended:
        fills = ", ".join(
            f"{fill.container_id} {format_fill(fill.before, fill.after, fill.capacity)}"
            for fill in recommended.container_fill
        )
        line = (
            f"추천안은 '{recommended.label}'이며 {_cost_sentence(recommended.total_cost_delta_krw)}"
        )
        parts.append(line + (f" 적재: {fills}." if fills else ""))
    else:
        parts.append("실행 가능한 대안이 없습니다.")
    infeasible = [alt for alt in ctx.alternatives if not alt.feasible]
    for alt in infeasible:
        reason = alt.reason or "사유 미기재"
        parts.append(f"'{alt.label}' 불가: {reason}.")
    affected = [s.shipper_name for s in ctx.shippers if s.delta_krw != 0 or s.moved]
    if affected:
        parts.append(f"영향 화주: {', '.join(affected)}.")
    return " ".join(parts)


def render_template(ctx: ExplainContext) -> Explanation:
    return Explanation(
        operator_summary=_operator_summary(ctx),
        shipper_messages=[
            ShipperMessage(shipper_id=s.shipper_id, message=_shipper_message(ctx, s))
            for s in ctx.shippers
        ],
        generated_by="template",
        model=None,
    )
