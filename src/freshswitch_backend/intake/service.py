from collections import defaultdict
from datetime import timedelta

from freshswitch_backend.domain.models import Lot, Plan, Scenario
from freshswitch_backend.intake.models import (
    AirHint,
    FeasibilityHints,
    FieldError,
    IntakePreview,
    IntakeRequest,
    Resolved,
    SeaHint,
)
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.optimize.feasibility import air_rejection, products_compatible, sea_option
from freshswitch_backend.optimize.settlement import tariff_sheet
from freshswitch_backend.replan.models import AddLotEvent

MAX_PULL_FORWARD_DAYS = 2
SO2_WARNING = (
    "SO2 처리 품목은 딸기 등 비처리 품목과 같은 컨테이너에 실을 수 없어 별도 CA 컨테이너가 "
    "필요합니다(WFLO, 제안서 나-1)"
)


class IntakeError(ValueError):
    def __init__(self, errors: list[FieldError]) -> None:
        super().__init__("; ".join(f"{e.field}: {e.message}" for e in errors))
        self.errors = errors


def _match(query: str, candidates: list[tuple[str, list[str]]]) -> list[str]:
    q = query.strip().lower()
    exact = {key for key, names in candidates if any(q == n.lower() for n in names if n)}
    if exact:
        return sorted(exact)
    return sorted({key for key, names in candidates if any(q in n.lower() for n in names if n)})


def _resolve(scenario: Scenario, body: IntakeRequest) -> tuple[Resolved | None, list[FieldError]]:
    errors: list[FieldError] = []
    products = _match(body.product, [(p.id, [p.id, p.name]) for p in scenario.products])
    if len(products) != 1:
        options = ", ".join(f"{p.name}({p.id})" for p in scenario.products)
        reason = "찾을 수 없음" if not products else "여러 품목과 일치"
        errors.append(
            FieldError(field="product", message=f"{reason}: {body.product}. 가능: {options}")
        )
    packhouses = {p.id: p for p in scenario.packhouses}
    shippers = _match(
        body.shipper,
        [
            (
                s.id,
                [
                    s.id,
                    s.name,
                    s.packhouse_id,
                    packhouses[s.packhouse_id].name if s.packhouse_id in packhouses else "",
                ],
            )
            for s in scenario.shippers
        ],
    )
    if len(shippers) != 1:
        options = ", ".join(f"{s.name}({s.id})" for s in scenario.shippers)
        reason = "찾을 수 없음" if not shippers else "여러 화주와 일치"
        errors.append(
            FieldError(field="shipper", message=f"{reason}: {body.shipper}. 가능: {options}")
        )
    ports = _match(body.destination, [(p.id, [p.id, p.unlocode, p.name]) for p in scenario.ports])
    if len(ports) != 1:
        options = ", ".join(f"{p.name}({p.id})" for p in scenario.ports)
        reason = "찾을 수 없음" if not ports else "여러 항구와 일치"
        errors.append(
            FieldError(
                field="destination", message=f"{reason}: {body.destination}. 가능: {options}"
            )
        )
    if errors:
        return None, errors
    product = next(p for p in scenario.products if p.id == products[0])
    shipper = next(s for s in scenario.shippers if s.id == shippers[0])
    port = next(p for p in scenario.ports if p.id == ports[0])
    packhouse = packhouses.get(shipper.packhouse_id)
    return (
        Resolved(
            product_id=product.id,
            product_name=product.name,
            shipper_id=shipper.id,
            shipper_name=shipper.name,
            packhouse_id=shipper.packhouse_id,
            packhouse_name=packhouse.name if packhouse else shipper.packhouse_id,
            destination_port_id=port.id,
            destination_name=port.name,
        ),
        [],
    )


def _lot_id(taken: set[str], resolved: Resolved) -> str:
    short = resolved.shipper_id.removeprefix("sh-")
    n = 1
    while f"lot-{short}-{resolved.product_id}-{n}" in taken:
        n += 1
    return f"lot-{short}-{resolved.product_id}-{n}"


def _loads(plan: Plan | None) -> tuple[dict[str, int], dict[str, set[str]]]:
    pallets: dict[str, int] = defaultdict(int)
    products: dict[str, set[str]] = defaultdict(set)
    if plan is None:
        return pallets, products
    for a in plan.assignments:
        if a.mode == "sea" and a.container_id:
            pallets[a.container_id] += a.pallets
            products[a.container_id].add(a.product_id)
    return pallets, products


def resolve_names(scenario: Scenario, body: IntakeRequest) -> Resolved:
    resolved, errors = _resolve(scenario, body)
    if errors or resolved is None:
        raise IntakeError(errors)
    return resolved


def preview_lot(
    scenario: Scenario,
    body: IntakeRequest,
    plan: Plan | None = None,
    taken_ids: set[str] | None = None,
    reserved_ids: set[str] | None = None,
) -> IntakePreview:
    taken = {lot.id for lot in scenario.lots} | (taken_ids or set())
    resolved, errors = _resolve(scenario, body)
    max_capacity = max((c.capacity_pallets for c in scenario.containers), default=20)
    if body.pallets <= 0:
        errors.append(FieldError(field="pallets", message="파렛트 수는 1 이상이어야 합니다"))
    elif body.pallets > max_capacity:
        errors.append(
            FieldError(
                field="pallets",
                message=f"컨테이너 1대 용량({max_capacity}파렛트)을 넘습니다. 나눠서 등록하세요",
            )
        )
    if body.lot_id and body.lot_id in taken:
        errors.append(FieldError(field="lot_id", message=f"이미 있는 로트 ID입니다: {body.lot_id}"))
    if body.sell_by_date and body.sell_by_date < body.ready_date:
        errors.append(
            FieldError(field="sell_by_date", message="판매기한이 출하 가능일보다 빠릅니다")
        )
    if errors or resolved is None:
        raise IntakeError(errors)
    products = {p.id: p for p in scenario.products}
    product = products[resolved.product_id]
    defaults: list[str] = []
    sell_by = body.sell_by_date
    if sell_by is None:
        sell_by = body.ready_date + timedelta(days=product.shelf_life_days)
        defaults.append(
            f"sell_by_date = 출하 가능일 + {product.name} 신선 유지 기간 "
            f"{product.shelf_life_days}일 = {sell_by.isoformat()} (출처: {product.source})"
        )
    lot_id = body.lot_id or _lot_id(taken | (reserved_ids or set()), resolved)
    if body.lot_id is None:
        defaults.append(f"lot_id 자동 생성: {lot_id}")
    lot = Lot(
        id=lot_id,
        shipper_id=resolved.shipper_id,
        product_id=resolved.product_id,
        pallets=body.pallets,
        ready_date=body.ready_date,
        destination=resolved.destination_port_id,
        sell_by_date=sell_by,
        sell_window=body.sell_window,
        source="화주 간편 등록",
    )
    hints = _hints(scenario, lot, plan)
    return IntakePreview(
        lot=lot,
        event=AddLotEvent(lot=lot),
        resolved=resolved,
        defaults_applied=defaults,
        hints=hints,
        next_step=(
            "plan_id가 있으면 event를 그대로 POST /api/plans/{plan_id}/events 에 "
            '{"event": ...}로 보내 재최적화한다'
        ),
    )


def _hints(scenario: Scenario, lot: Lot, plan: Plan | None) -> FeasibilityHints:
    model = CostModel(scenario)
    product = model.product(lot)
    pallets, loaded = _loads(plan)
    products = {p.id: p for p in scenario.products}
    options: list[SeaHint] = []
    warnings: list[str] = []
    if product.so2_treated:
        warnings.append(SO2_WARNING)
    for container in sorted(scenario.containers, key=lambda c: (model.voyage_of(c).etd, c.id)):
        voyage = model.voyage_of(container)
        option, reason = sea_option(model, lot, container, MAX_PULL_FORWARD_DAYS)
        reasons = [reason] if reason else []
        current = loaded.get(container.id, set())
        clash = [
            products[pid].name
            for pid in sorted(current)
            if pid in products and not products_compatible(product, products[pid])
        ]
        if clash:
            so2 = product.so2_treated or any(products[pid].so2_treated for pid in current)
            label = "SO2 처리 여부가 다른 품목" if so2 else "CA 조건이 다른 품목"
            reasons.append(f"{label}({', '.join(clash)})이 실린 컨테이너라 혼적 불가")
        free = None
        if plan is not None:
            free = container.capacity_pallets - pallets.get(container.id, 0)
            if lot.pallets > free:
                reasons.append(f"남은 자리 {free}파렛트 < {lot.pallets}파렛트")
        options.append(
            SeaHint(
                container_id=container.id,
                voyage_id=voyage.id,
                vessel_name=voyage.vessel_name,
                etd=voyage.etd,
                cy_cutoff=voyage.cy_cutoff,
                arrival_date=model.sea_arrival_date(voyage),
                feasible=not reasons,
                reasons=reasons,
                pull_forward_days=option.pull_forward_days if option else 0,
                free_pallets=free,
                loaded_product_ids=sorted(current),
            )
        )
    feasible = [o for o in options if o.feasible]
    earliest = feasible[0] if feasible else None
    if not any(model.voyage_of(c).destination == lot.destination for c in scenario.containers):
        warnings.append("이 목적지로 가는 항차가 없습니다. 항공만 가능합니다")
    if earliest and earliest.pull_forward_days:
        warnings.append(
            f"가장 빠른 해상 {earliest.container_id}({earliest.voyage_id})는 선적 마감 때문에 "
            f"출하를 {earliest.pull_forward_days}일 앞당겨야 합니다"
        )
    if not feasible:
        warnings.append(
            "지금 계획에서 실을 수 있는 컨테이너가 없습니다. 항공 또는 새 컨테이너가 필요합니다"
        )
    tariff = model.tariff(lot)
    if tariff is not None and lot.sell_window == "short":
        sea = tariff_sheet(model, lot.product_id, tariff)
        if sea.sea_total_short_window >= sea.air_total:
            warnings.append(
                f"판매 기간이 짧아 해상 품질손실 위험을 {sea.sea_loss_risk_short_window:,}원/파렛트로 "
                f"잡습니다. 해상 {sea.sea_total_short_window:,}원 > 항공 {sea.air_total:,}원이라 "
                "항공으로 배정됩니다"
            )
    air_reason = air_rejection(model, lot)
    return FeasibilityHints(
        based_on="plan" if plan is not None else "scenario",
        sea_options=options,
        earliest_feasible=earliest,
        air=AirHint(
            feasible=air_reason is None,
            arrival_date=model.air_arrival_date(lot),
            reason=air_reason,
        ),
        warnings=warnings,
    )
