from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from freshswitch_backend.api.access import require_demo_reset
from freshswitch_backend.data.loader import get_scenario, load_scenarios
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.sandbox_world import expected_world_lots
from freshswitch_backend.persistence.seed import (
    SCENARIO_ID,
    DemoOrg,
    demo_orgs,
)
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    BindingRow,
    ContainerRow,
    DeviceRow,
    LotRow,
    MembershipRow,
    ObservationRow,
    OrderRow,
    OrganizationRow,
    PackhouseRow,
    PlanForwarderRow,
    PlanRow,
    SandboxWorldRow,
    ShipperProfileRow,
    UserRow,
)

router = APIRouter(prefix="/ops", tags=["ops"])

SEEDED_SCOPES = ("demo", "sandbox")
SEED_ORIGIN = "seed"
SEED_LOT_FIELDS = (
    "scenario_id",
    "shipper_id",
    "shipper_org_id",
    "product_id",
    "pallets",
    "ready_date",
    "destination",
    "sell_by_date",
    "sell_window",
    "packhouse",
    "source",
    "status",
)

COUNT_KEYS = (
    "lots",
    "missing_seed_lots",
    "plans",
    "orders",
    "forwarder_assignments",
    "coldchain_containers",
    "devices",
    "bindings",
    "members",
    "packhouses",
    "shipper_profiles",
)
COUNT_PROPERTIES = {name: {"type": "integer"} for name in COUNT_KEYS}


class OrgDrift(BaseModel):
    org_id: str
    org_type: str
    sandbox: bool
    seeded: bool
    counts: dict[str, int] = Field(
        json_schema_extra={"properties": COUNT_PROPERTIES, "required": list(COUNT_KEYS)}
    )
    baseline: dict[str, int] = Field(
        json_schema_extra={"properties": COUNT_PROPERTIES, "required": list(COUNT_KEYS)}
    )
    drift: dict[str, int] = Field(json_schema_extra={"properties": COUNT_PROPERTIES})
    non_demo_members: int


class QualityDrift(BaseModel):
    scope: str
    world_id: str | None = None
    seed_rows: int
    added_rows: int


class Contamination(BaseModel):
    contaminated: bool
    seed_completed: bool
    missing_orgs: list[str]
    unexpected_sandbox_orgs: list[str]
    orgs: list[OrgDrift]
    quality: list[QualityDrift]


def seed_completed(db: Session) -> bool:
    return db.get(OrganizationRow, DEMO_ORG) is not None and get_scenario("default") is not None


def _baseline(seed: DemoOrg | None) -> dict[str, int]:
    counts = dict.fromkeys(COUNT_KEYS, 0)
    if seed is not None:
        counts.update(
            members=1, packhouses=len(seed.shipper_ids), shipper_profiles=len(seed.shipper_ids)
        )
    return counts


def _grouped(
    db: Session, column: Any, ids: list[str], join: tuple[Any, Any] | None = None, *where: Any
) -> Counter:
    query = select(column, func.count()).where(column.in_(ids), *where)
    if join is not None:
        query = query.join(*join)
    return Counter(dict(db.execute(query.group_by(column)).all()))


def _seed_lot_ids() -> set[str]:
    return {lot.id for scenario in load_scenarios().values() for lot in scenario.lots}


def _seeded_scenario(rows: list[Any]) -> str:
    seeded = _seed_lot_ids()
    present = Counter(row.scenario_id for row in rows if row.id in seeded)
    return present.most_common(1)[0][0] if present else SCENARIO_ID


def _slug(org_id: str, world_id: str | None) -> str:
    prefix = f"{world_id}-" if world_id and world_id != "seed" else ""
    if prefix and org_id.startswith(prefix):
        return org_id[len(prefix) :]
    return org_id


def _lot_counts(db: Session, ids: list[str]) -> tuple[Counter, Counter]:
    columns = [getattr(LotRow, name) for name in ("id", *SEED_LOT_FIELDS)]
    rows = db.execute(select(*columns).where(LotRow.shipper_org_id.in_(ids))).all()
    wanted = set(ids)
    seed = {
        value["id"]: value for value in expected_world_lots(db) if value["shipper_org_id"] in wanted
    }
    changed: Counter = Counter()
    for row in rows:
        expected = seed.get(row.id)
        if expected is None or any(
            expected[name] != getattr(row, name) for name in SEED_LOT_FIELDS
        ):
            changed[row.shipper_org_id] += 1
    present = {row.id for row in rows}
    missing = Counter(v["shipper_org_id"] for lot_id, v in seed.items() if lot_id not in present)
    return changed, missing


def _counts(db: Session, ids: list[str]) -> dict[str, Counter]:
    changed, missing = _lot_counts(db, ids)
    return {
        "lots": changed,
        "missing_seed_lots": missing,
        "plans": _grouped(db, PlanRow.org_id, ids),
        "orders": _grouped(
            db, PlanRow.org_id, ids, (OrderRow, OrderRow.plan_id == PlanRow.plan_id)
        ),
        "forwarder_assignments": _grouped(db, PlanForwarderRow.forwarder_org_id, ids),
        "coldchain_containers": _grouped(db, ContainerRow.org_id, ids),
        "devices": _grouped(db, DeviceRow.org_id, ids),
        "bindings": _grouped(db, BindingRow.org_id, ids),
        "members": _grouped(db, MembershipRow.org_id, ids),
        "packhouses": _grouped(db, PackhouseRow.org_id, ids),
        "shipper_profiles": _grouped(db, ShipperProfileRow.org_id, ids),
    }


def _quality(db: Session) -> list[QualityDrift]:
    rows = db.execute(
        select(
            ObservationRow.scope,
            ObservationRow.world_id,
            ObservationRow.origin,
            func.count(),
        )
        .where(ObservationRow.scope.in_(SEEDED_SCOPES))
        .group_by(ObservationRow.scope, ObservationRow.world_id, ObservationRow.origin)
    ).all()
    tally: Counter = Counter()
    for scope, world_id, origin, count in rows:
        tally[(scope, world_id, origin == SEED_ORIGIN)] += count
    worlds = list(db.scalars(select(SandboxWorldRow.id).order_by(SandboxWorldRow.id)))
    report = [
        QualityDrift(
            scope="demo",
            world_id=None,
            seed_rows=tally.get(("demo", None, True), 0),
            added_rows=tally.get(("demo", None, False), 0),
        )
    ]
    report.extend(
        QualityDrift(
            scope="sandbox",
            world_id=world_id,
            seed_rows=tally.get(("sandbox", world_id, True), 0),
            added_rows=tally.get(("sandbox", world_id, False), 0),
        )
        for world_id in worlds
    )
    return report


def contamination_report(db: Session) -> Contamination:
    seeds = {org.id: org for org in demo_orgs()}
    orgs = db.execute(
        select(
            OrganizationRow.id,
            OrganizationRow.type,
            OrganizationRow.sandbox,
            OrganizationRow.world_id,
        )
        .where(or_(OrganizationRow.sandbox.is_(True), OrganizationRow.id == DEMO_ORG))
        .order_by(OrganizationRow.type, OrganizationRow.id)
    ).all()
    ids = [org_id for org_id, _, _, _ in orgs]
    counts = _counts(db, ids)
    outsiders = _grouped(
        db,
        MembershipRow.org_id,
        ids,
        (UserRow, UserRow.id == MembershipRow.user_id),
        UserRow.is_demo.is_(False),
    )
    rows = []
    for org_id, org_type, sandbox, world_id in orgs:
        slug = _slug(org_id, world_id)
        baseline = _baseline(seeds.get(slug))
        current = {name: counts[name][org_id] for name in baseline}
        rows.append(
            OrgDrift(
                org_id=org_id,
                org_type=org_type,
                sandbox=sandbox,
                seeded=slug in seeds,
                counts=current,
                baseline=baseline,
                drift={
                    name: current[name] - baseline[name]
                    for name in baseline
                    if current[name] != baseline[name]
                },
                non_demo_members=outsiders[org_id],
            )
        )
    quality = _quality(db)
    unexpected = [r.org_id for r in rows if r.sandbox and not r.seeded]
    present_ids = {org_id for org_id, _, _, _ in orgs}
    missing = sorted(set(seeds) - present_ids)
    contaminated = bool(
        unexpected
        or missing
        or any(r.drift or r.non_demo_members or (r.seeded and not r.sandbox) for r in rows)
        or any(q.added_rows for q in quality)
    )
    return Contamination(
        contaminated=contaminated,
        seed_completed=seed_completed(db),
        missing_orgs=missing,
        unexpected_sandbox_orgs=unexpected,
        orgs=rows,
        quality=quality,
    )


@router.get("/demo-contamination", dependencies=[Depends(require_demo_reset)])
def demo_contamination() -> Contamination:
    with database.begin() as db:
        return contamination_report(db)
