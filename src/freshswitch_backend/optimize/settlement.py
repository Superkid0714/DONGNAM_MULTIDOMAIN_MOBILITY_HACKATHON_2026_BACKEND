from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from freshswitch_backend.domain.models import (
    Assignment,
    BillingRule,
    ContainerLoad,
    ContainerSettlement,
    ContainerShipperSettlement,
    CostBreakdown,
    CostParams,
    Lot,
    PalletTariff,
    Scenario,
    Settlement,
    SettlementBaselineKind,
    SettlementBasis,
    SettlementBillingRule,
    ShipperCost,
    TariffLine,
    TariffSheet,
)
from freshswitch_backend.optimize.costs import CostModel, LotCost
from freshswitch_backend.optimize.solo import quote_amounts, sea_solo_cost


def settle(savings: int, fee_rate: float) -> tuple[int, int]:
    if savings <= 0:
        return 0, savings
    fee = round(savings * fee_rate)
    return fee, savings - fee


BASELINE_KINDS: dict[SettlementBillingRule, SettlementBaselineKind] = {
    "performance": "air_solo",
    "usage_fee": "air_solo",
    "mixed": "air_solo",
}
BASELINE_LABELS: dict[SettlementBaselineKind, str] = {
    "air_solo": "단독 항공 대비",
    "sea_solo": "단독 해상 대비",
    "mixed": "품목별 기준선(단독 항공·단독 해상)",
}
SAVINGS_BASELINE_LABEL = "단독 항공 대비"


def uses_usage_fee(params: CostParams, product_id: str) -> bool:
    return product_id in params.billing.usage_fee_products


def billing_rule(params: CostParams, product_id: str) -> BillingRule:
    return "usage_fee" if uses_usage_fee(params, product_id) else "performance"


def usage_fee(params: CostParams, pallets: float) -> int:
    return round(pallets * params.billing.usage_fee_per_pallet_krw.value)


@dataclass(frozen=True)
class Charge:
    billing_rule: SettlementBillingRule
    performance_fee: int
    usage_fee: int
    fee: int
    shipper_gain: int
    baseline_kind: SettlementBaselineKind
    baseline_label: str
    savings_baseline_label: str
    pilot: bool

    def fields(self) -> dict:
        return asdict(self)


@dataclass
class _Billing:
    rules: set[BillingRule] = field(default_factory=set)
    performance_baseline: float = 0.0
    performance_plan: float = 0.0
    usage_pallets: int = 0

    def add(self, rule: BillingRule, pallets: int, sea: bool, baseline: float, plan: float) -> None:
        self.rules.add(rule)
        if rule == "usage_fee":
            self.usage_pallets += pallets if sea else 0
            return
        self.performance_baseline += baseline
        self.performance_plan += plan

    def rule(self) -> SettlementBillingRule:
        if len(self.rules) == 1:
            return next(iter(self.rules))
        return "mixed" if self.rules else "performance"

    def charge(self, params: CostParams, savings: int) -> Charge:
        rule = self.rule()
        performance_savings = savings
        if rule != "performance":
            performance_savings = round(self.performance_baseline) - round(self.performance_plan)
        return charge(params, rule, savings, performance_savings, self.usage_pallets)


def charge(
    params: CostParams,
    rule: SettlementBillingRule,
    savings: int,
    performance_savings: int,
    usage_pallets: int,
) -> Charge:
    performance_fee, _ = settle(performance_savings, params.platform_fee_rate.value)
    usage = usage_fee(params, usage_pallets)
    if performance_fee + usage > max(0, savings):
        performance_fee = usage = 0
    pilot = params.billing.pilot
    fee = 0 if pilot else performance_fee + usage
    kind = BASELINE_KINDS[rule]
    return Charge(
        billing_rule=rule,
        performance_fee=performance_fee,
        usage_fee=usage,
        fee=fee,
        shipper_gain=savings - fee,
        baseline_kind=kind,
        baseline_label=BASELINE_LABELS[kind],
        savings_baseline_label=SAVINGS_BASELINE_LABEL,
        pilot=pilot,
    )


PARTS = ("freight", "inland", "handling", "collection", "storage", "loss_risk")


def _add(acc: dict[str, float], cost: LotCost) -> None:
    for part in PARTS:
        acc[part] += getattr(cost, part)


def _breakdown(
    acc: dict[str, float], estimate: float | None = None, routed: bool = False
) -> CostBreakdown:
    parts = {k: round(acc[k]) for k in PARTS}
    return CostBreakdown(
        **parts,
        total=sum(parts.values()),
        collection_estimate=round(estimate) if estimate is not None else None,
        collection_route=parts["collection"] if routed else None,
    )


def container_loads(model: CostModel, assignments: list[Assignment]) -> list[ContainerLoad]:
    by_container: dict[str, list[Assignment]] = defaultdict(list)
    for a in assignments:
        if a.mode == "sea" and a.container_id:
            by_container[a.container_id].append(a)
    loads = []
    for container in model.scenario.containers:
        items = by_container.get(container.id, [])
        pallets = sum(a.pallets for a in items)
        extra_days = model.terminal_extra_days(model.voyage_of(container))
        loads.append(
            ContainerLoad(
                container_id=container.id,
                voyage_id=container.voyage_id,
                used=bool(items),
                pallets=pallets,
                capacity_pallets=container.capacity_pallets,
                utilization=round(pallets / container.capacity_pallets, 4),
                lot_ids=[a.lot_id for a in items],
                product_ids=sorted({a.product_id for a in items}),
                shipper_ids=sorted({a.shipper_id for a in items}),
                container_cost=round(model.container_fixed_cost) if items else 0,
                terminal_extra_days=round(extra_days, 2),
                terminal_plug_cost=round(model.container_plug_cost(container)) if items else 0,
            )
        )
    return loads


def lot_plan_cost(
    model: CostModel,
    lot: Lot,
    assignment: Assignment,
    container_pallets: dict[str, int],
    collection: float | None = None,
) -> LotCost:
    if assignment.mode == "air":
        return model.air_lot_cost(lot)
    container = model.containers[assignment.container_id]
    voyage = model.voyage_of(container)
    variable = model.sea_lot_cost(lot, voyage, assignment.pull_forward_days)
    share = lot.pallets / container_pallets[container.id]
    shipper_load = container_pallets.get(f"shipper:{lot.shipper_id}", lot.pallets)
    shipper_share = lot.pallets / shipper_load
    return LotCost(
        freight=model.container_freight * share,
        handling=model.container_handling * share
        + variable.handling
        + model.params.sea_shipper_document_krw.value * shipper_share,
        collection=(
            model.params.sea_shipper_pickup_krw.value * shipper_share
            if model.params.sea_fixed_pickup
            else variable.collection
            if collection is None
            else collection
        ),
        storage=variable.storage + model.container_plug_cost(container) * share,
        loss_risk=variable.loss_risk,
    )


def lot_settle_cost(
    model: CostModel,
    lot: Lot,
    assignment: Assignment,
    container_pallets: dict[str, int],
    collection: float | None = None,
) -> LotCost:
    return lot_plan_cost(model, lot, assignment, container_pallets, collection)


def _basis(kinds: set[bool]) -> SettlementBasis:
    if kinds == {True}:
        return "tariff"
    if kinds == {False}:
        return "operational"
    return "mixed"


def _fee_parts(total: int, rows: list[ContainerShipperSettlement], field: str) -> list[int]:
    weights = [getattr(row, field) for row in rows]
    if not sum(weights):
        weights = [row.pallets for row in rows]
    raw = [total * weight / sum(weights) for weight in weights]
    values = [int(value) for value in raw]
    for index in sorted(range(len(raw)), key=lambda i: (-(raw[i] - values[i]), i))[
        : total - sum(values)
    ]:
        values[index] += 1
    return values


@dataclass
class SettlementResult:
    costs: list[ShipperCost]
    settlements: list[Settlement]
    containers: list[ContainerSettlement]
    operational_baseline: int = 0
    operational_cost: int = 0
    collection_estimate: int = 0
    collection_route: int | None = None
    tariffs: list[TariffSheet] = field(default_factory=list)


def _assumed(source: str) -> bool:
    return source.startswith("가정")


def tariff_sheet(model: CostModel, product_id: str, tariff: PalletTariff) -> TariffSheet:
    product = model.products[product_id]
    rate_shift = model.air_rate_per_kg - tariff.air_rate_basis_per_kg.value
    air_freight = round(tariff.air_freight.value + rate_shift * product.net_pallet_kg)
    rows = (
        ("freight", "운임", tariff.air_freight, air_freight, tariff.sea_freight),
        ("inland", "내륙·장비·보관", tariff.air_inland, None, tariff.sea_inland),
        ("loss_risk", "품질손실 위험", tariff.air_loss_risk, None, tariff.sea_loss_risk),
    )
    lines = [
        TariffLine(
            key=key,
            label=label,
            air=value if value is not None else round(air.value),
            sea=round(sea.value),
            air_assumed=_assumed(air.source),
            sea_assumed=_assumed(sea.source),
            air_source=air.source,
            sea_source=sea.source,
        )
        for key, label, air, value, sea in rows
    ]
    air_total = sum(line.air for line in lines)
    sea_total = sum(line.sea for line in lines)
    savings = air_total - sea_total
    rule = billing_rule(model.params, product_id)
    usage = rule == "usage_fee"
    bill = charge(model.params, rule, savings, 0 if usage else savings, 1 if usage else 0)
    fee_rate = 0.0 if usage else model.params.platform_fee_rate.value
    short = round((tariff.sea_loss_risk_short_window or tariff.sea_loss_risk).value)
    short_total = sea_total - lines[2].sea + short
    assumed = [f"air.{line.key}" for line in lines if line.air_assumed]
    assumed += [f"sea.{line.key}" for line in lines if line.sea_assumed]
    if tariff.sea_loss_risk_short_window and _assumed(tariff.sea_loss_risk_short_window.source):
        assumed.append("sea.loss_risk_short_window")
    return TariffSheet(
        product_id=product_id,
        lines=lines,
        air_total=air_total,
        sea_total=sea_total,
        savings_per_pallet=savings,
        fee_rate=fee_rate,
        fee_per_pallet=bill.fee,
        shipper_share_per_pallet=bill.shipper_gain,
        billing_rule=rule,
        pilot=bill.pilot,
        sea_loss_risk_short_window=short,
        sea_total_short_window=short_total,
        short_window_rule=(
            f"판매 기간이 짧은 로트(sell_window=short)는 해상 품질손실 위험을 {short:,}원/파렛트로 "
            f"잡는다. 해상 {short_total:,}원 > 항공 {air_total:,}원이면 해상을 막고 항공으로 보낸다"
        ),
        assumed_items=assumed,
    )


def tariff_sheets(model: CostModel, scenario: Scenario) -> list[TariffSheet]:
    tariffs = model.params.pallet_tariffs
    return [tariff_sheet(model, p.id, tariffs[p.id]) for p in scenario.products if p.id in tariffs]


def shipper_costs_and_settlements(
    model: CostModel,
    lots: dict[str, Lot],
    assignments: list[Assignment],
    route_collection: Mapping[str, int] | None = None,
) -> SettlementResult:
    container_pallets: dict[str, int] = defaultdict(int)
    for a in assignments:
        if a.mode == "sea" and a.container_id:
            container_pallets[a.container_id] += a.pallets
            container_pallets[f"shipper:{a.shipper_id}"] += a.pallets
    plan_acc: dict[str, dict[str, float]] = {}
    base_acc: dict[str, dict[str, float]] = {}
    op_acc: dict[str, dict[str, float]] = {}
    op_base_acc: dict[str, dict[str, float]] = {}
    estimates: dict[str, float] = defaultdict(float)
    pallets: dict[str, list[int]] = {}
    kinds: dict[str, set[bool]] = defaultdict(set)
    row_kinds: dict[tuple[str, str], set[bool]] = defaultdict(set)
    rows: dict[tuple[str, str], list[float]] = {}
    billing: dict[str, _Billing] = defaultdict(_Billing)
    row_billing: dict[tuple[str, str], _Billing] = defaultdict(_Billing)
    params = model.params
    routed = route_collection is not None
    for a in assignments:
        lot = lots[a.lot_id]
        sid = lot.shipper_id
        for acc in (plan_acc, base_acc, op_acc, op_base_acc):
            acc.setdefault(sid, defaultdict(float))
        pallets.setdefault(sid, [0, 0])
        estimated = lot_plan_cost(model, lot, a, container_pallets)
        estimates[sid] += estimated.collection
        override = (route_collection or {}).get(a.lot_id) if a.mode == "sea" else None
        operational = lot_plan_cost(model, lot, a, container_pallets, override)
        _add(op_acc[sid], operational)
        _add(op_base_acc[sid], model.air_lot_cost(lot))
        settled = lot_settle_cost(model, lot, a, container_pallets, override)
        baseline = model.settle_air_cost(lot)
        _add(plan_acc[sid], settled)
        _add(base_acc[sid], baseline)
        kinds[sid].add(False)
        pallets[sid][0 if a.mode == "sea" else 1] += lot.pallets
        rule = billing_rule(params, lot.product_id)
        sea = a.mode == "sea"
        billing[sid].add(rule, lot.pallets, sea, baseline.total, settled.total)
        if sea and a.container_id:
            row = rows.setdefault((a.container_id, sid), [0, 0.0, 0.0])
            row[0] += lot.pallets
            row[1] += baseline.total
            row[2] += settled.total
            row_kinds[(a.container_id, sid)].add(False)
            row_billing[(a.container_id, sid)].add(
                rule, lot.pallets, sea, baseline.total, settled.total
            )
    costs: list[ShipperCost] = []
    settlements: list[Settlement] = []
    for shipper in model.scenario.shippers:
        sid = shipper.id
        if sid not in plan_acc:
            continue
        plan = _breakdown(plan_acc[sid])
        baseline = _breakdown(base_acc[sid])
        operational = _breakdown(op_acc[sid], estimates[sid], routed)
        plan = plan.model_copy(
            update={
                "collection_estimate": operational.collection_estimate,
                "collection_route": operational.collection_route,
            }
        )
        sea, air = pallets[sid]
        costs.append(
            ShipperCost(
                shipper_id=sid,
                pallets=sea + air,
                sea_pallets=sea,
                air_pallets=air,
                plan=plan,
                baseline=baseline,
                operational=operational,
                operational_baseline=_breakdown(op_base_acc[sid]),
            )
        )
        savings = baseline.total - plan.total
        included = [a.lot_id for a in assignments if a.shipper_id == sid]
        solo = sea_solo_cost(model.scenario, included) if model.compute_solo else None
        charge_fields = billing[sid].charge(params, savings).fields()
        risk = any(
            model.remaining_sell_days(lots[a.lot_id], model.voyages[a.voyage_id]) < 1
            for a in assignments
            if a.shipper_id == sid and a.mode == "sea"
        )
        settlements.append(
            Settlement(
                shipper_id=sid,
                baseline_cost=baseline.total,
                shared_cost=plan.total,
                savings=savings,
                sea_pallets=sea,
                air_pallets=air,
                basis=_basis(kinds[sid]),
                **charge_fields,
                **quote_amounts(baseline.total, solo, plan.total, charge_fields["fee"]),
                air_switch_candidate=risk
                or (sea > 0 and plan.total + charge_fields["fee"] > baseline.total),
                delay_inputs=[
                    {
                        "voyage_id": voyage.id,
                        "delay_p90_h": voyage.delay_p90_h or 0,
                        "truck_departure_adjustment_h": voyage.truck_departure_adjustment_h,
                        "unpowered_h": model.terminal_unpowered_hours(voyage),
                        "source": voyage.delay_forecast_source or "provided",
                    }
                    for voyage in model.scenario.voyages
                    if any(
                        a.shipper_id == sid and a.mode == "sea" and a.voyage_id == voyage.id
                        for a in assignments
                    )
                ],
                quote_assumptions=[
                    "운영 원가 기준; 단독 해상은 항공 선택 없이 전용 컨테이너·집하를 재계산",
                    "무전원 시간은 p90 지연에서 명시된 트럭 출발 조정 시간을 뺀 추정 노출시간이며 실측 단전이 아님",
                    *(
                        ["단독 해상은 일정·용량·집하 제약 또는 계산 제한으로 견적 불가"]
                        if model.compute_solo and solo is None
                        else []
                    ),
                ],
            )
        )
    containers = []
    shipper_order = {s.id: i for i, s in enumerate(model.scenario.shippers)}
    for container in model.scenario.containers:
        entries = sorted(
            ((sid, row) for (cid, sid), row in rows.items() if cid == container.id),
            key=lambda e: shipper_order.get(e[0], len(shipper_order)),
        )
        if not entries:
            continue
        shippers = []
        for sid, (count, base_total, plan_total) in entries:
            base_cost, shared = round(base_total), round(plan_total)
            bill = row_billing[(container.id, sid)].charge(params, base_cost - shared)
            included = [
                a.lot_id
                for a in assignments
                if a.container_id == container.id and a.shipper_id == sid
            ]
            solo = sea_solo_cost(model.scenario, included) if model.compute_solo else None
            shippers.append(
                ContainerShipperSettlement(
                    shipper_id=sid,
                    pallets=int(count),
                    baseline_cost=base_cost,
                    shared_cost=shared,
                    savings=base_cost - shared,
                    basis=_basis(row_kinds[(container.id, sid)]),
                    **bill.fields(),
                    **quote_amounts(base_cost, solo, shared, bill.fee),
                )
            )
        containers.append(
            ContainerSettlement(
                container_id=container.id,
                voyage_id=container.voyage_id,
                pallets=sum(x.pallets for x in shippers),
                capacity_pallets=container.capacity_pallets,
                shippers=shippers,
                total_savings=sum(x.savings for x in shippers),
                total_fee=sum(x.fee for x in shippers),
                total_shipper_gain=sum(x.shipper_gain for x in shippers),
            )
        )
    # A shipper may span several containers. Apply the fee cap once per shipper,
    # then distribute that exact fee so container totals cannot contradict it.
    for settlement in settlements:
        rows = [
            row
            for container in containers
            for row in container.shippers
            if row.shipper_id == settlement.shipper_id
        ]
        if not rows:
            continue
        performance = _fee_parts(settlement.performance_fee, rows, "performance_fee")
        usage = _fee_parts(settlement.usage_fee, rows, "usage_fee")
        for row, perf, use in zip(rows, performance, usage, strict=True):
            row.performance_fee, row.usage_fee = perf, use
            row.fee = 0 if settlement.pilot else perf + use
            row.shipper_gain = row.savings - row.fee
            row.air_switch_candidate = row.shared_cost + row.fee > row.baseline_cost
            for key, value in quote_amounts(
                row.baseline_cost, row.sea_solo_cost, row.shared_cost, row.fee
            ).items():
                setattr(row, key, value)
    for container in containers:
        container.total_fee = sum(row.fee for row in container.shippers)
        container.total_shipper_gain = sum(row.shipper_gain for row in container.shippers)
    return SettlementResult(
        costs=costs,
        settlements=settlements,
        containers=containers,
        operational_baseline=sum(c.operational_baseline.total for c in costs),
        operational_cost=sum(c.operational.total for c in costs),
        collection_estimate=sum(c.operational.collection_estimate or 0 for c in costs),
        collection_route=sum(c.operational.collection for c in costs) if routed else None,
        tariffs=[],
    )
