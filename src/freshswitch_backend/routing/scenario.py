from datetime import datetime, time
from itertools import combinations

from freshswitch_backend.domain.models import KST, Lot, Port, Product, Scenario
from freshswitch_backend.optimize.feasibility import products_compatible
from freshswitch_backend.routing.models import (
    Depot,
    GateWindow,
    PickupRequest,
    PickupStop,
    PickupTruck,
)

DEFAULT_READY_TIME = time(8, 0)


def cargo_groups(products: list[Product]) -> tuple[dict[str, str], list[tuple[str, str]]]:
    members: list[list[Product]] = []
    for product in products:
        for group in members:
            if all(products_compatible(product, other) for other in group):
                group.append(product)
                break
        else:
            members.append([product])
    labels = ["+".join(p.id for p in group) for group in members]
    group_of = {p.id: label for label, group in zip(labels, members, strict=True) for p in group}
    conflicts = [
        (labels[i], labels[j])
        for i, j in combinations(range(len(members)), 2)
        if any(not products_compatible(a, b) for a in members[i] for b in members[j])
    ]
    return group_of, conflicts


def gate_windows(port: Port) -> list[GateWindow]:
    return [
        GateWindow(start=w.start, end=w.end, label=w.label, source=w.source)
        for w in port.gate_peak_windows
    ]


def _truckloads(group: list[Lot], max_cap: int) -> list[list[Lot]]:
    if sum(lot.pallets for lot in group) <= max_cap or len(group) == 1:
        return [group]
    loads: list[list[Lot]] = []
    for lot in sorted(group, key=lambda x: (-x.pallets, x.id)):
        target = next(
            (b for b in loads if sum(x.pallets for x in b) + lot.pallets <= max_cap), None
        )
        if target is None:
            loads.append([lot])
        else:
            target.append(lot)
    return loads


def build_pickup_request(
    scenario: Scenario,
    lot_ids: list[str],
    voyage_id: str,
    ready_time: time = DEFAULT_READY_TIME,
    **options,
) -> PickupRequest:
    voyages = {v.id: v for v in scenario.voyages}
    if voyage_id not in voyages:
        raise KeyError(f"unknown voyage: {voyage_id}")
    voyage = voyages[voyage_id]
    ports = {p.id: p for p in scenario.ports}
    port = ports[voyage.origin]
    lots = {lot.id: lot for lot in scenario.lots}
    shippers = {s.id: s for s in scenario.shippers}
    packhouses = {p.id: p for p in scenario.packhouses}
    group_of, conflicts = cargo_groups(scenario.products)

    grouped: dict[tuple[str, str], list] = {}
    for lot_id in lot_ids:
        if lot_id not in lots:
            raise KeyError(f"unknown lot: {lot_id}")
        lot = lots[lot_id]
        key = (shippers[lot.shipper_id].packhouse_id, group_of.get(lot.product_id, lot.product_id))
        grouped.setdefault(key, []).append(lot)
    split = {ph for ph, _ in grouped if sum(1 for other, _ in grouped if other == ph) > 1}

    trucks = [
        PickupTruck(id=t.id, capacity_pallets=t.capacity_pallets)
        for t in scenario.trucks
        if t.depot_id == port.id
    ]
    max_cap = max((t.capacity_pallets for t in trucks), default=0)

    stops = []
    for (packhouse_id, group_id), group in grouped.items():
        ph = packhouses[packhouse_id]
        base = (
            f"stop-{packhouse_id}-{group_id}" if packhouse_id in split else f"stop-{packhouse_id}"
        )
        loads = _truckloads(group, max_cap)
        for k, load in enumerate(loads, start=1):
            ready = max(datetime.combine(lot.ready_date, ready_time, tzinfo=KST) for lot in load)
            stops.append(
                PickupStop(
                    id=base if len(loads) == 1 else f"{base}-{k}",
                    packhouse_id=packhouse_id,
                    lat=ph.lat,
                    lon=ph.lon,
                    pallets=sum(lot.pallets for lot in load),
                    ready_at=ready,
                    lot_ids=[lot.id for lot in load],
                    cargo_group=group_id,
                )
            )

    used = {stop.cargo_group for stop in stops}
    return PickupRequest(
        depot=Depot(id=port.id, lat=port.lat, lon=port.lon),
        stops=stops,
        trucks=trucks,
        deadline=voyage.cy_cutoff,
        incompatible_groups=[(a, b) for a, b in conflicts if a in used and b in used],
        **{"gate_windows": gate_windows(port), **options},
    )
