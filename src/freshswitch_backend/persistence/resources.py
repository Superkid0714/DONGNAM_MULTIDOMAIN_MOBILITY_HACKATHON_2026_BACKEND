import hashlib
import json
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.container_numbers import container_number
from freshswitch_backend.domain.models import Container, Plan, Scenario, Voyage
from freshswitch_backend.domain.resources import ResourceCreate, ResourceOut
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import same_sandbox
from freshswitch_backend.persistence.tables import (
    ForwarderResourceRow,
    MetaRow,
    OrganizationRow,
    PlanRow,
    SandboxWorldRow,
)


def _free_container_number(db: Session, seed: str, taken: set[str]) -> str:
    attempt = 0
    while True:
        number = container_number(seed, attempt)
        if number not in taken and db.get(ForwarderResourceRow, number) is None:
            taken.add(number)
            return number
        attempt += 1


def ensure_sandbox_resources(db: Session, org: OrganizationRow | None) -> None:
    if org is None or not org.sandbox or org.world_id is None:
        return
    db.scalar(select(SandboxWorldRow).where(SandboxWorldRow.id == org.world_id).with_for_update())
    key = f"forwarder_resources_seeded:{org.world_id}"
    if db.get(MetaRow, key) is not None:
        return
    owners = list(
        db.scalars(
            select(OrganizationRow)
            .where(
                OrganizationRow.type == "forwarder",
                OrganizationRow.world_id == org.world_id,
            )
            .order_by(OrganizationRow.id)
        )
    )
    scenario = get_scenario("default")
    if not owners or scenario is None:
        return
    owner = owners[0]
    exists = db.scalar(
        select(ForwarderResourceRow.id)
        .where(
            ForwarderResourceRow.org_id.in_([o.id for o in owners]),
        )
        .limit(1)
    )
    if exists is None:
        voyages = {v.id: v for v in scenario.voyages}
        taken: set[str] = set()
        for container in scenario.containers:
            voyage = voyages[container.voyage_id]
            rid = _free_container_number(db, f"{org.world_id}:{container.id}", taken)
            body = ResourceCreate(
                name=f"{container.id} (데모 예시)",
                kind=container.kind,
                capacity_pallets=container.capacity_pallets,
                temp_min_c=-30,
                temp_max_c=30,
                vessel_name=voyage.vessel_name,
                terminal=voyage.terminal,
                origin=voyage.origin,
                destination=voyage.destination,
                etd=voyage.etd,
                cy_cutoff=voyage.cy_cutoff,
                transit_days=voyage.transit_days,
            )
            db.add(
                ForwarderResourceRow(
                    id=rid,
                    org_id=owner.id,
                    data={
                        **body.model_dump(mode="json"),
                        "source": "데모 예시 · 제공 가능 공간·항차 가정, 실제 예약 별도",
                    },
                )
            )
    db.add(MetaRow(key=key, value={"world_id": org.world_id, "demo": True}))
    db.flush()


class ResourceUnavailable(Exception):
    def __init__(self, resource_ids: list[str]) -> None:
        self.resource_ids = resource_ids
        super().__init__("컨테이너 배정 가능 상태가 바뀌었습니다")


def sync_allocations(
    db: Session,
    plan_id: str,
    platform_org_id: str,
    snapshots: list[tuple[Scenario, Plan]],
    now: datetime,
) -> None:
    expected = {}
    pallets: dict[str, int] = {}
    for scenario, plan in snapshots:
        resources = {c.id: c for c in scenario.containers if c.resource_revision is not None}
        totals: dict[str, int] = {}
        for assignment in plan.assignments:
            cid = assignment.container_id
            if assignment.mode == "sea" and cid in resources:
                expected[cid] = resources[cid]
                totals[cid] = totals.get(cid, 0) + assignment.pallets
        for cid, used in totals.items():
            pallets[cid] = max(pallets.get(cid, 0), used)
    desired = set(expected)
    rows = list(
        db.scalars(
            select(ForwarderResourceRow)
            .where(
                or_(
                    ForwarderResourceRow.id.in_(desired),
                    ForwarderResourceRow.allocated_plan_id == plan_id,
                )
            )
            .order_by(ForwarderResourceRow.id)
            .with_for_update()
        )
    )
    found = {row.id: row for row in rows}
    missing = desired - set(found)
    if missing:
        raise ResourceUnavailable(sorted(missing))
    platform = db.get(OrganizationRow, platform_org_id)
    conflicts = []
    for cid in desired:
        row = found[cid]
        data = ResourceCreate.model_validate({k: v for k, v in row.data.items() if k != "source"})
        owner = db.get(OrganizationRow, row.org_id)
        same_world = (
            platform is not None
            and owner is not None
            and (platform.world_id == owner.world_id and platform.sandbox == owner.sandbox)
        )
        if (
            not same_world
            or row.org_id != expected[cid].forwarder_org_id
            or row.revision != expected[cid].resource_revision
            or row.allocated_plan_id not in {None, plan_id}
            or pallets[cid] > data.capacity_pallets
            or (row.allocated_plan_id is None and (not data.enabled or data.cy_cutoff < now))
        ):
            conflicts.append(cid)
    if conflicts:
        raise ResourceUnavailable(sorted(conflicts))
    for row in rows:
        if row.id in desired:
            row.allocated_plan_id = plan_id
        elif row.allocated_plan_id == plan_id:
            row.allocated_plan_id = None


def resource_query(viewer_org: str, own: bool = False):
    query = (
        select(ForwarderResourceRow, OrganizationRow.name)
        .join(OrganizationRow, OrganizationRow.id == ForwarderResourceRow.org_id)
        .where(OrganizationRow.type == "forwarder")
    )
    if own:
        query = query.where(ForwarderResourceRow.org_id == viewer_org)
    else:
        query = query.where(same_sandbox(OrganizationRow.sandbox, viewer_org))
    return query.order_by(ForwarderResourceRow.created_at, ForwarderResourceRow.id)


def resource_out(db: Session, row: ForwarderResourceRow, org_name: str) -> ResourceOut:
    data = ResourceCreate.model_validate({k: v for k, v in row.data.items() if k != "source"})
    allocated = 0
    if row.allocated_plan_id is not None:
        plan = db.get(PlanRow, row.allocated_plan_id)
        if plan is not None:
            for payload in (plan.payload, plan.committed):
                assigned = (payload.get("plan") or {}).get("assignments") or []
                allocated = max(
                    allocated,
                    sum(a["pallets"] for a in assigned if a.get("container_id") == row.id),
                )
    status = "available"
    if row.allocated_plan_id is not None:
        status = "allocated"
    elif not data.enabled or data.cy_cutoff < datetime.now(UTC):
        status = "disabled"
    return ResourceOut(
        **data.model_dump(),
        id=row.id,
        forwarder_org_id=row.org_id,
        forwarder_org_name=org_name,
        allocated_plan_id=row.allocated_plan_id,
        allocated_pallets=allocated,
        available_pallets=data.capacity_pallets if status == "available" else 0,
        status=status,
        source=row.data.get("source", "포워더 등록 · 실제 예약 별도"),
    )


def resource_scenario(
    scenario: Scenario,
    org_id: str,
    forwarder_org_id: str | None,
    now: datetime,
    require_owner: bool = True,
    require_available: bool = True,
) -> Scenario:
    with database.begin() as db:
        org = db.get(OrganizationRow, org_id)
        ensure_sandbox_resources(db, org)
        if org is not None and org.type == "forwarder":
            if forwarder_org_id is not None and forwarder_org_id != org_id:
                raise HTTPException(
                    status_code=403, detail="자기 조직의 컨테이너로만 계획을 만들 수 있습니다"
                )
            forwarder_org_id = org_id
        rows = db.execute(resource_query(org_id)).all()
        if not rows and org is not None and org.sandbox and forwarder_org_id is None:
            return scenario
        available = []
        for row, name in rows:
            data = ResourceCreate.model_validate(
                {k: v for k, v in row.data.items() if k != "source"}
            )
            if row.allocated_plan_id is None and data.enabled and data.cy_cutoff >= now:
                available.append((row, data))
        owners = {row.org_id for row, data in available}
        if require_owner and forwarder_org_id is None and len(owners) > 1:
            raise HTTPException(
                status_code=422, detail="가용 컨테이너를 제공할 포워더를 선택하세요"
            )
        if forwarder_org_id is not None:
            available = [(row, data) for row, data in available if row.org_id == forwarder_org_id]
        if require_available and not available:
            raise HTTPException(
                status_code=422,
                detail="가용 컨테이너가 없습니다. 포워더가 항차와 제공 가능 공간을 등록해야 합니다",
            )
        containers = []
        voyages = {}
        for row, data in available:
            schedule = data.model_dump(
                mode="json",
                include={
                    "vessel_name",
                    "terminal",
                    "origin",
                    "destination",
                    "etd",
                    "cy_cutoff",
                    "transit_days",
                },
            )
            fingerprint = json.dumps([row.org_id, schedule], sort_keys=True).encode()
            voyage_id = f"rv-{hashlib.sha256(fingerprint).hexdigest()[:24]}"
            source = row.data.get("source", "포워더 등록 · 실제 예약 별도")
            containers.append(
                Container(
                    id=row.id,
                    name=data.name,
                    voyage_id=voyage_id,
                    kind=data.kind,
                    capacity_pallets=data.capacity_pallets,
                    temp_min_c=data.temp_min_c,
                    temp_max_c=data.temp_max_c,
                    forwarder_org_id=row.org_id,
                    resource_revision=row.revision,
                    source=source,
                )
            )
            voyages[voyage_id] = Voyage(
                id=voyage_id,
                vessel_name=data.vessel_name,
                terminal=data.terminal,
                origin=data.origin,
                destination=data.destination,
                etd=data.etd,
                cy_cutoff=data.cy_cutoff,
                transit_days=data.transit_days,
                source=source,
            )
        return scenario.model_copy(
            update={"containers": containers, "voyages": list(voyages.values())}, deep=True
        )
