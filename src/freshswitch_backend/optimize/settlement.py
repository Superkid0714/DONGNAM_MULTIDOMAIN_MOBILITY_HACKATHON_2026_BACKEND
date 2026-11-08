from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from freshswitch_backend.domain.models import (
    Assignment,
    ContainerLoad,
    ContainerSettlement,
    ContainerShipperSettlement,
    CostBreakdown,
    Lot,
    PalletTariff,
    Scenario,
    Settlement,
    SettlementBasis,
    ShipperCost,
    TariffLine,
    TariffSheet,
)
from freshswitch_backend.optimize.costs import CostModel, LotCost


def settle(savings: int, fee_rate: float) -> tuple[int, int]:
    if savings <= 0:
        return 0, savings
    fee = round(savings * fee_rate)
    return fee, savings - fee


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
    return LotCost(
        freight=model.container_freight * share,
        handling=model.container_handling * share,
        collection=variable.collection if collection is None else collection,
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
    if model.tariff(lot) is None:
        return lot_plan_cost(model, lot, assignment, container_pallets, collection)
    if assignment.mode == "air":
        return model.tariff_air_cost(lot)
    container = model.containers[assignment.container_id]
    share = lot.pallets / container_pallets[container.id]
    return model.tariff_sea_cost(
        lot,
        model.voyage_of(container),
        assignment.pull_forward_days,
        model.container_plug_cost(container) * share,
    )


def _basis(kinds: set[bool]) -> SettlementBasis:
    if kinds == {True}:
        return "tariff"
    if kinds == {False}:
        return "operational"
    return "mixed"


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
    air_freight = round(tariff.air_freight.value + rate_shift * product.pallet_kg)
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
    fee_rate = model.params.platform_fee_rate.value
    fee, gain = settle(savings, fee_rate)
    short = round(tariff.sea_loss_risk_short_window.value)
    short_total = sea_total - lines[2].sea + short
    assumed = [f"air.{line.key}" for line in lines if line.air_assumed]
    assumed += [f"sea.{line.key}" for line in lines if line.sea_assumed]
    if _assumed(tariff.sea_loss_risk_short_window.source):
        assumed.append("sea.loss_risk_short_window")
    return TariffSheet(
        product_id=product_id,
        lines=lines,
        air_total=air_total,
        sea_total=sea_total,
        savings_per_pallet=savings,
        fee_rate=fee_rate,
        fee_per_pallet=fee,
        shipper_share_per_pallet=gain,
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
    plan_acc: dict[str, dict[str, float]] = {}
    base_acc: dict[str, dict[str, float]] = {}
    op_acc: dict[str, dict[str, float]] = {}
    op_base_acc: dict[str, dict[str, float]] = {}
    estimates: dict[str, float] = defaultdict(float)
    pallets: dict[str, list[int]] = {}
    kinds: dict[str, set[bool]] = defaultdict(set)
    rows: dict[tuple[str, str], list[float]] = {}
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
        kinds[sid].add(model.tariff(lot) is not None)
        pallets[sid][0 if a.mode == "sea" else 1] += lot.pallets
        if a.mode == "sea" and a.container_id:
            row = rows.setdefault((a.container_id, sid), [0, 0.0, 0.0])
            row[0] += lot.pallets
            row[1] += baseline.total
            row[2] += settled.total
    fee_rate = model.params.platform_fee_rate.value
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
        fee, gain = settle(savings, fee_rate)
        settlements.append(
            Settlement(
                shipper_id=sid,
                baseline_cost=baseline.total,
                shared_cost=plan.total,
                savings=savings,
                fee=fee,
                shipper_gain=gain,
                sea_pallets=sea,
                air_pallets=air,
                basis=_basis(kinds[sid]),
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
            fee, gain = settle(base_cost - shared, fee_rate)
            shippers.append(
                ContainerShipperSettlement(
                    shipper_id=sid,
                    pallets=int(count),
                    baseline_cost=base_cost,
                    shared_cost=shared,
                    savings=base_cost - shared,
                    fee=fee,
                    shipper_gain=gain,
                    basis=_basis(kinds[sid]),
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
    return SettlementResult(
        costs=costs,
        settlements=settlements,
        containers=containers,
        operational_baseline=sum(c.operational_baseline.total for c in costs),
        operational_cost=sum(c.operational.total for c in costs),
        collection_estimate=sum(c.operational.collection_estimate or 0 for c in costs),
        collection_route=sum(c.operational.collection for c in costs) if routed else None,
        tariffs=tariff_sheets(model, model.scenario),
    )
