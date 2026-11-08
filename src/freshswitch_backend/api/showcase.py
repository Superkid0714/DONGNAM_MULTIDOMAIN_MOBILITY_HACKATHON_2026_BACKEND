import threading
from functools import cache
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.planning.service import PlanWithRoutes, build_plan
from freshswitch_backend.routing.models import TruckRoute

router = APIRouter(prefix="/public", tags=["public"])

DEFAULT_SCENARIO_ID = "default"
ShowcaseBasis = Literal["default_scenario"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShowcasePlace(_Strict):
    id: str
    name: str
    lat: float
    lon: float


class ShowcasePort(_Strict):
    id: str
    name: str
    lat: float
    lon: float


class ShowcaseProduct(_Strict):
    id: str
    name: str
    so2_treated: bool


class ShowcaseVoyage(_Strict):
    id: str
    vessel_name: str
    origin: str
    destination: str
    transit_days: int


class ShowcaseContainer(_Strict):
    container_id: str
    voyage_id: str
    product_ids: list[str]
    pallets: int
    capacity_pallets: int


class ShowcaseAir(_Strict):
    packhouse_id: str
    pallets: int


class ShowcaseRoute(_Strict):
    voyage_id: str
    truck_id: str
    trip: int
    cargo_group: str | None
    load_pallets: int
    stops: list[str]
    stop_pallets: list[int]


class Showcase(_Strict):
    basis: ShowcaseBasis
    saving_rate: float
    packhouse_count: int
    total_pallets: int
    containers: list[ShowcaseContainer]
    air: list[ShowcaseAir]
    packhouses: list[ShowcasePlace]
    ports: list[ShowcasePort]
    products: list[ShowcaseProduct]
    voyages: list[ShowcaseVoyage]
    routes: list[ShowcaseRoute]


_lock = threading.Lock()


def _default_scenario() -> Scenario:
    scenario = get_scenario(DEFAULT_SCENARIO_ID)
    if scenario is None:
        raise RuntimeError(f"시나리오 {DEFAULT_SCENARIO_ID}를 찾을 수 없습니다")
    return scenario.model_copy(deep=True)


def _summarize(scenario: Scenario, built: PlanWithRoutes) -> Showcase:
    plan, routes = built.plan, built.routes
    packhouse_of = {s.id: s.packhouse_id for s in scenario.shippers}
    used = [c for c in plan.containers if c.used]
    used_voyages = {c.voyage_id for c in used}
    voyages = sorted(
        (v for v in scenario.voyages if v.id in used_voyages), key=lambda v: (v.etd, v.id)
    )
    port_ids = {v.origin for v in voyages} | {v.destination for v in voyages}
    route_packhouses = [
        stop.packhouse_id
        for voyage in routes
        for truck in voyage.pickup.routes
        for stop in sorted(truck.stops, key=lambda s: s.sequence)
    ]
    assigned_packhouses = {
        packhouse_of[a.shipper_id] for a in plan.assignments if a.shipper_id in packhouse_of
    }
    shown = assigned_packhouses | set(route_packhouses)
    air: dict[str, int] = {}
    for a in plan.assignments:
        if a.mode == "air" and a.shipper_id in packhouse_of:
            key = packhouse_of[a.shipper_id]
            air[key] = air.get(key, 0) + a.pallets
    product_ids = {a.product_id for a in plan.assignments}
    return Showcase(
        basis="default_scenario",
        saving_rate=(
            round(plan.total_savings / plan.total_baseline_cost, 3)
            if plan.total_baseline_cost
            else 0.0
        ),
        packhouse_count=len(assigned_packhouses),
        total_pallets=sum(a.pallets for a in plan.assignments),
        containers=[
            ShowcaseContainer(
                container_id=c.container_id,
                voyage_id=c.voyage_id,
                product_ids=list(c.product_ids),
                pallets=c.pallets,
                capacity_pallets=c.capacity_pallets,
            )
            for c in used
        ],
        air=[ShowcaseAir(packhouse_id=key, pallets=pallets) for key, pallets in air.items()],
        packhouses=[
            ShowcasePlace(id=p.id, name=p.name, lat=p.lat, lon=p.lon)
            for p in scenario.packhouses
            if p.id in shown
        ],
        ports=[
            ShowcasePort(id=p.id, name=p.name, lat=p.lat, lon=p.lon)
            for p in scenario.ports
            if p.id in port_ids
        ],
        products=[
            ShowcaseProduct(id=p.id, name=p.name, so2_treated=p.so2_treated)
            for p in scenario.products
            if p.id in product_ids
        ],
        voyages=[
            ShowcaseVoyage(
                id=v.id,
                vessel_name=v.vessel_name,
                origin=v.origin,
                destination=v.destination,
                transit_days=v.transit_days,
            )
            for v in voyages
        ],
        routes=[
            _route(voyage.voyage_id, truck) for voyage in routes for truck in voyage.pickup.routes
        ],
    )


def _route(voyage_id: str, truck: TruckRoute) -> ShowcaseRoute:
    stops = sorted(truck.stops, key=lambda s: s.sequence)
    return ShowcaseRoute(
        voyage_id=voyage_id,
        truck_id=truck.truck_id,
        trip=truck.trip,
        cargo_group=truck.cargo_group,
        load_pallets=truck.load_pallets,
        stops=[s.packhouse_id for s in stops],
        stop_pallets=[s.pallets for s in stops],
    )


@cache
def _cached_showcase() -> Showcase:
    scenario = _default_scenario()
    return _summarize(scenario, build_plan(scenario))


def showcase_snapshot() -> Showcase:
    with _lock:
        return _cached_showcase()


@router.get("/showcase")
def read_showcase() -> Showcase:
    return showcase_snapshot()
