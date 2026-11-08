from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.compute_limits import (
    cache_key,
    check_lot_count,
    check_preview_rate,
    compute_slot,
    preview_cache,
    preview_deadline,
    replan_rate,
    run_bounded,
)
from freshswitch_backend.api.plans import (
    CreatePlanRequest,
    EventRequest,
    ReplanResponse,
    create_tenant_plan,
    replan_event,
    tenant_plan_scenario,
)
from freshswitch_backend.api.saas_access import visible_plans
from freshswitch_backend.api.saas_plan_confirm import require_sandbox
from freshswitch_backend.api.saas_views import (
    PlanListItem,
    PlatformSettlement,
    forwarders_of,
    plan_item,
    platform_settlement,
)
from freshswitch_backend.domain.inputs import StorableStr
from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.intake.tenants import load_catalog
from freshswitch_backend.lots import ANY_ORG, LotStatus, LotUnavailable, RegisteredLot
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import same_sandbox
from freshswitch_backend.persistence.tables import LotRow, OrganizationRow, PlanRow
from freshswitch_backend.planning.service import VoyagePickup, build_plan
from freshswitch_backend.planning.store import ContainerBooked, PlanSession, store
from freshswitch_backend.replan import AddLotEvent

router = APIRouter(prefix="/saas/platform", tags=["saas-platform"])

PlatformPrincipal = Annotated[Principal, Depends(require_role("platform"))]


class PlatformLot(RegisteredLot):
    shipper_org_id: str
    shipper_org_name: str


class PlatformPlanRequest(BaseModel):
    scenario_id: str = "default"
    use_learned_loss: bool = False
    lot_ids: list[StorableStr] = Field(default_factory=list)
    include_registered: bool = False
    auto_accept: Literal[False] = False
    forwarder_org_id: StorableStr | None = Field(default=None, min_length=1, max_length=64)


class AddLotRequest(BaseModel):
    lot_id: StorableStr = Field(min_length=1, max_length=120)
    idempotency_key: StorableStr | None = Field(default=None, min_length=1, max_length=200)


class PlanPreview(BaseModel):
    scenario: Scenario
    plan: Plan
    routes: list[VoyagePickup]


@router.get("/lots")
def list_lots(
    principal: PlatformPrincipal,
    status: LotStatus | None = None,
    scenario_id: Annotated[StorableStr | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlatformLot]:
    query = (
        select(LotRow, OrganizationRow.name)
        .join(OrganizationRow, OrganizationRow.id == LotRow.shipper_org_id)
        .where(
            OrganizationRow.type == "shipper",
            same_sandbox(OrganizationRow.sandbox, principal.org_id),
        )
        .order_by(LotRow.created_at.desc(), LotRow.id)
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        query = query.where(LotRow.status == status)
    if scenario_id is not None:
        query = query.where(LotRow.scenario_id == scenario_id)
    with database.begin() as db:
        return [
            PlatformLot(
                **RegisteredLot.model_validate(row, from_attributes=True).model_dump(),
                shipper_org_id=row.shipper_org_id,
                shipper_org_name=name,
            )
            for row, name in db.execute(query).all()
        ]


@router.post("/plans", status_code=status.HTTP_201_CREATED)
def create_plan(body: PlatformPlanRequest, principal: PlatformPrincipal) -> PlanSession:
    if body.auto_accept:
        require_sandbox(principal)
    request = CreatePlanRequest(**body.model_dump(exclude={"auto_accept"}))
    if not body.auto_accept:
        return create_tenant_plan(request, principal.org_id)
    scenario, registered_ids = tenant_plan_scenario(request, principal.org_id)
    try:
        with compute_slot():
            return store.create(
                scenario,
                registered_ids,
                org_id=principal.org_id,
                lot_org_id=ANY_ORG,
                exclusive=True,
                auto_accept=True,
                quantity_policy_v1=True,
            )
    except LotUnavailable as exc:
        raise HTTPException(
            status_code=409, detail=f"이미 계획에 들어갔거나 그사이 바뀐 출하입니다: {exc}"
        ) from exc
    except ContainerBooked as exc:
        raise HTTPException(
            status_code=409,
            detail=f"그사이 다른 계획이 컨테이너를 배정했습니다: {', '.join(exc.container_ids)}",
        ) from exc


def _registered_lot(lot_id: str, principal: Principal) -> RegisteredLot | None:
    with database.begin() as db:
        row = db.scalars(
            select(LotRow)
            .join(OrganizationRow, OrganizationRow.id == LotRow.shipper_org_id)
            .where(
                LotRow.id == lot_id,
                OrganizationRow.type == "shipper",
                same_sandbox(OrganizationRow.sandbox, principal.org_id),
            )
        ).first()
        return RegisteredLot.model_validate(row, from_attributes=True) if row else None


def _addable(session: PlanSession, body: AddLotRequest, principal: Principal) -> AddLotEvent:
    row = _registered_lot(body.lot_id, principal)
    if row is None:
        raise HTTPException(status_code=404, detail="등록된 출하를 찾을 수 없습니다")
    if any(lot.id == row.id for lot in session.scenario.lots):
        raise HTTPException(status_code=409, detail="이미 이 계획에 들어 있는 출하입니다")
    if row.status != "registered":
        raise HTTPException(status_code=409, detail="이미 다른 계획에 들어갔거나 취소된 출하입니다")
    if row.scenario_id != session.scenario_id:
        raise HTTPException(status_code=422, detail="다른 시나리오에 등록된 출하입니다")
    lot = row.to_lot()
    scenario = session.scenario
    if lot.shipper_id not in {s.id for s in scenario.shippers}:
        raise HTTPException(
            status_code=422,
            detail="계획을 만든 뒤 가입한 화주의 출하입니다. 새 계획으로 묶으세요",
        )
    if lot.product_id not in {p.id for p in scenario.products} or lot.destination not in {
        p.id for p in scenario.ports
    }:
        raise HTTPException(status_code=422, detail="이 계획 시나리오에 없는 품목·항구입니다")
    check_lot_count(len(scenario.lots) + 1)
    return AddLotEvent(lot=lot)


@router.post("/plans/{plan_id}/add-lot", dependencies=[Depends(replan_rate)])
async def add_registered_lot(
    plan_id: str,
    body: AddLotRequest,
    principal: PlatformPrincipal,
    llm: Annotated[bool | None, Query()] = None,
) -> ReplanResponse:
    session = await run_in_threadpool(store.get, plan_id, principal.org_id)
    if session is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    event = await run_in_threadpool(_addable, session, body, principal)
    request = EventRequest(event=event, idempotency_key=body.idempotency_key)
    response = await replan_event(plan_id, request, org_id=principal.org_id, llm=llm)
    await run_in_threadpool(
        store.mark_registered_add, plan_id, response.replan_id, event.lot.id, principal.org_id
    )
    return response


def _compute_preview(scenario: Scenario, deadline: float) -> PlanPreview:
    working = scenario.model_copy(deep=True)
    built = build_plan(working, deadline=deadline)
    return PlanPreview(scenario=working, plan=built.plan, routes=built.routes)


@router.post("/plans/preview")
def preview_plan(
    body: PlatformPlanRequest, request: Request, principal: PlatformPrincipal
) -> PlanPreview:
    deadline = preview_deadline()
    check_lot_count(len(dict.fromkeys(body.lot_ids)))
    scenario, registered_ids = tenant_plan_scenario(
        CreatePlanRequest(**body.model_dump(exclude={"auto_accept"})), principal.org_id
    )
    key = cache_key(
        {
            "org_id": principal.org_id,
            "lot_ids": sorted(registered_ids),
            "options": body.model_dump(exclude={"lot_ids", "auto_accept"}),
            "scenario": scenario.model_dump(mode="json"),
        }
    )
    cache = preview_cache(request)
    cached = cache.get(key)
    if cached is not None:
        return cached
    check_preview_rate(request, principal)
    preview = run_bounded(_compute_preview, scenario, deadline=deadline)
    cache.put(key, preview)
    return preview


def _sessions(principal: Principal, limit: int, offset: int) -> list[PlanSession]:
    store.expire_due(PlanRow.org_id == principal.org_id)
    with database.begin() as db:
        rows = db.scalars(
            select(PlanRow)
            .where(visible_plans(principal))
            .order_by(PlanRow.created_at.desc(), PlanRow.seq.desc())
            .limit(limit)
            .offset(offset)
        )
        return [PlanSession.model_validate(row.payload) for row in rows]


@router.get("/plans")
def list_plans(
    principal: PlatformPrincipal,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlanListItem]:
    sessions = _sessions(principal, limit, offset)
    forwarders = forwarders_of([s.plan_id for s in sessions])
    return [plan_item(s, forwarders.get(s.plan_id)) for s in sessions]


@router.get("/settlements")
def list_settlements(
    principal: PlatformPrincipal,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlatformSettlement]:
    names = load_catalog(ANY_ORG, peer_of=principal.org_id).org_names()
    return [platform_settlement(s, names) for s in _sessions(principal, limit, offset)]
