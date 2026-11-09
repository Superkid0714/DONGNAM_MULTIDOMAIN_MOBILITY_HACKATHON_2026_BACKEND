from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException

from freshswitch_backend.api.auth import OptionalPrincipal
from freshswitch_backend.api.saas_access import plan_for
from freshswitch_backend.planning.robust.calculator import (
    RobustRequest,
    robust_savings,
)
from freshswitch_backend.planning.robust.estimate import EstimateResult, EvaluateRequest, estimate
from freshswitch_backend.planning.robust.quotes import router as quote_router
from freshswitch_backend.planning.robust.rates import seed_inputs, table

router = APIRouter(prefix="/v1/robust-savings", tags=["robust-savings"])
# Existing application router filtering requires concrete routes, not nested routers.
for quote_route in quote_router.routes:
    router.add_api_route(
        quote_route.path,
        quote_route.endpoint,
        methods=list(quote_route.methods),
        response_model=quote_route.response_model,
        status_code=quote_route.status_code,
        tags=quote_route.tags,
    )


def require_login(principal):
    if principal is None:
        raise HTTPException(401, "로그인이 필요합니다")


def calculate_container(access, container_id):
    session = access.session
    assignments = [
        a for a in session.plan.assignments if a.mode == "sea" and a.container_id == container_id
    ]
    if not assignments:
        raise HTTPException(404, "해상 배정 컨테이너를 찾을 수 없습니다")
    n = defaultdict(int)
    kg = defaultdict(float)
    lots = {lot.id: lot for lot in session.scenario.lots}
    products = {p.id: p for p in session.scenario.products}
    for a in assignments:
        n[a.shipper_id] += a.pallets
        kg[a.shipper_id] += a.pallets * products[lots[a.lot_id].product_id].pallet_kg
    total = sum(n.values())
    results = {}
    for sid in n:
        if access.viewer.org_type == "shipper" and sid not in access.viewer.shipper_ids:
            continue
        shipper_lots = [lots[a.lot_id] for a in assignments if a.shipper_id == sid]
        quote = SimpleNamespace(
            shipment_date=max(l.ready_date for l in shipper_lots),
            cargo=SimpleNamespace(pallets=n[sid], gross_weight_kg=Decimal(str(kg[sid]))),
        )
        # Only the manually verified HKG segment mapping may be selected automatically.
        segment = "medium" if all(l.destination == "HKHKG" for l in shipper_lots) else None
        body = seed_inputs(quote, segment)
        body.container_pallets = total
        row = robust_savings(body).model_dump(mode="json")
        row.update(shipper_pallets=n[sid], container_pallets=total)
        row["warnings"].append("중량은 저장 품목 파렛트 중량 환산값이며 실계근값이 아닙니다")
        results[sid] = row
    return {"container_id": container_id, "shippers": results, "settlement_eligible": False}


@router.post("/evaluate")
def evaluate(body: EvaluateRequest, principal: OptionalPrincipal) -> EstimateResult:
    require_login(principal)
    robust = robust_savings(
        RobustRequest.model_validate(
            body.model_dump(exclude={"estimate_items", "estimate_equipment", "estimate_plug_days"})
        )
    )
    return EstimateResult(**robust.model_dump(), robust=robust, estimate=estimate(body))


@router.get("/rates")
def rates(principal: OptionalPrincipal):
    require_login(principal)
    return {name: table(name) for name in ("public_rates", "fuel_surcharge", "fx")}


@router.get("/plans/{plan_id}/containers/{container_id}")
def container(plan_id: str, container_id: str, principal: OptionalPrincipal):
    require_login(principal)
    return calculate_container(plan_for(plan_id, principal), container_id)


@router.get("/compare")
def compare(before: str, after: str, principal: OptionalPrincipal):
    require_login(principal)
    output = {}
    for key, plan_id in (("before", before), ("after", after)):
        access = plan_for(plan_id, principal)
        ids = sorted({a.container_id for a in access.session.plan.assignments if a.mode == "sea"})
        output[key] = [calculate_container(access, cid) for cid in ids]
    return output
