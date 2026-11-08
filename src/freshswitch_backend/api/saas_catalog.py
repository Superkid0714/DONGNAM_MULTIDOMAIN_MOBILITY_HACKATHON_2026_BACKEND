from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.intake.tenants import (
    eligible_cargo_scenario,
    load_catalog,
    tenant_scenario,
)
from freshswitch_backend.lots import ANY_ORG
from freshswitch_backend.persistence.resources import resource_scenario
from freshswitch_backend.planning.store import store

router = APIRouter(prefix="/saas", tags=["saas"])

CatalogPrincipal = Annotated[Principal, Depends(require_role("platform", "shipper", "forwarder"))]


def org_scenario(principal: Principal, scenario_id: str = "default") -> Scenario:
    base = get_scenario(scenario_id)
    if base is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    if principal.org_type in {"platform", "forwarder"}:
        scenario = tenant_scenario(base, load_catalog(ANY_ORG, base, peer_of=principal.org_id))
    else:
        scenario = tenant_scenario(base, load_catalog(principal.org_id, base))
    if principal.org_type == "forwarder":
        scenario = eligible_cargo_scenario(scenario, principal.org_id)
    scenario = resource_scenario(
        scenario,
        principal.org_id,
        None,
        store.clock(),
        require_owner=False,
        require_available=False,
    )
    return scenario.model_copy(update={"description": ""})


@router.get("/catalog")
def read_catalog(principal: CatalogPrincipal, scenario_id: str = "default") -> Scenario:
    return org_scenario(principal, scenario_id)
