"""Conservative checks for factual Korean explanations, not an LLM-based judge."""

import re
from decimal import Decimal

from freshswitch_backend.explain.formatting import (
    extract_numbers,
    format_amount_krw,
    format_krw,
)
from freshswitch_backend.explain.models import ExplainContext, ShipperImpact
from freshswitch_backend.explain.template import render_template

MONEY = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?\s*(?:만\s*)?원")
INCREASE = re.compile(r"증가|늘어|늘었|늘어나|오릅|올랐|상승|비싸")
DECREASE = re.compile(r"감소|줄어|줄었|줄어드|내립|내려|하락|절감|저렴")
UNCHANGED = re.compile(r"변동\s*없|변화\s*없|동일|그대로")
CONTAINER = re.compile(r"\b[A-Z][A-Z0-9]*-\d[A-Z0-9-]*")


def _money(text: str) -> Decimal:
    value = Decimal(re.search(r"\d[\d,]*(?:\.\d+)?", text)[0].replace(",", ""))
    return value * (10_000 if "만" in text else 1)


def shipper_error(ctx: ExplainContext, shipper: ShipperImpact, text: str) -> str | None:
    # Currency is scoped to this shipper, never the union of all shippers' numbers.
    amounts = {
        _money(format_krw(shipper.cost_before_krw)),
        _money(format_krw(shipper.cost_after_krw)),
        _money(format_amount_krw(shipper.delta_krw)),
        _money(format_krw(abs(shipper.cancellation_adjustment_krw))),
    }
    if any(_money(m[0]) not in amounts for m in MONEY.finditer(text)):
        return "다른 화주의 금액 또는 입력에 없는 금액"
    own = render_template(ctx.model_copy(update={"shippers": [shipper]})).shipper_messages[0]
    facts = own.message.split("조정되었습니다.", 1)[-1].strip()
    normalize = lambda value: " ".join(value.split())
    if normalize(text) == normalize(own.message):
        return None
    prefixes = (
        "",
        "안녕하세요.",
        f"{shipper.shipper_name}님,",
        f"안녕하세요, {shipper.shipper_name}님.",
        f"안녕하세요. {shipper.shipper_name}님,",
    )
    suffixes = ("", "감사합니다.", "확인 부탁드립니다.", "확인해 주세요.")
    if not any(
        normalize(text) == normalize(f"{prefix} {facts} {suffix}")
        for prefix in prefixes
        for suffix in suffixes
    ):
        return "검증된 사실과 허용된 인사 외의 내용"
    cost_fact = facts.split("다.", 1)[0] + "다."
    if cost_fact not in text:
        return "검증된 변경 전후 비용·증감 문구 누락"
    if extract_numbers(text) - extract_numbers(own.message):
        return "화주에게 해당하지 않는 숫자"
    for other in ctx.shippers:
        if other.shipper_id != shipper.shipper_id and other.shipper_name in text:
            return "다른 화주의 이름"
    targets = set(CONTAINER.findall(shipper.moved or ""))
    if set(CONTAINER.findall(text)) - targets:
        return "화주에게 해당하지 않는 컨테이너"
    if "항공" in text and "항공" not in (shipper.moved or ""):
        return "확인되지 않은 항공 이동"
    if shipper.moved and f"화물은 {shipper.moved}(으)로 옮겨집니다." not in text:
        return "화물 이동 정보 누락"
    if shipper.pull_forward_days:
        if f"출하를 {shipper.pull_forward_days}일 앞당겨 주셔야 합니다." not in text:
            return "출하 당김 정보 누락"
    elif "앞당" in text:
        return "확인되지 않은 출하 당김"
    if shipper.delta_krw == 0:
        if not UNCHANGED.search(text) or INCREASE.search(text) or DECREASE.search(text):
            return "비용 변동 없음과 설명 불일치"
        return None
    change = _money(format_amount_krw(shipper.delta_krw))
    expected = INCREASE if shipper.delta_krw > 0 else DECREASE
    opposite = DECREASE if shipper.delta_krw > 0 else INCREASE
    # Check the cost sentence, not words such as '감량' in the event description.
    sentences = re.split(r"(?<=[다요])\s*[.!?]\s*|\n", text)
    cost_sentences = [s for s in sentences if MONEY.search(s)]
    claims = [s for s in cost_sentences if any(_money(m[0]) == change for m in MONEY.finditer(s))]
    if not claims or not any(expected.search(s) for s in claims):
        return "비용 변화량 또는 방향 누락"
    if any(opposite.search(s) or UNCHANGED.search(s) for s in cost_sentences):
        return "비용 증감 방향 불일치"
    for sentence in cost_sentences:
        if "에서" in sentence:
            values = [_money(m[0]) for m in MONEY.finditer(sentence)]
            if len(values) >= 2 and values[:2] != [shipper.cost_before_krw, shipper.cost_after_krw]:
                return "변경 전후 비용 순서 불일치"
    return None


def summary_error(ctx: ExplainContext, text: str) -> str | None:
    canonical = render_template(ctx).operator_summary
    # Never let generated prose change the recommendation, totals or feasibility.
    if text.strip() != canonical:
        return "사건·추천안·총비용·실행 불가 정보 불일치"
    return None
