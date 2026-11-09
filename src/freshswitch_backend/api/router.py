from copy import copy

from fastapi import APIRouter, Depends

from freshswitch_backend.api import (
    analysis,
    auth,
    coldchain,
    compare,
    demo,
    evidence_quote,
    forecast,
    health,
    intake,
    login_gate,
    lots,
    me,
    ops,
    operational_quotes,
    orgs,
    plans,
    provenance,
    quality,
    saas_admin,
    saas_catalog,
    saas_change_requests,
    saas_devices,
    saas_forwarder,
    saas_metrics,
    saas_operations,
    saas_plan_confirm,
    saas_platform,
    saas_products,
    saas_quantity_policy,
    saas_resources,
    saas_sandbox,
    saas_shipper,
    saas_shipper_profile,
    scenarios,
    settlement,
    showcase,
    vessels,
)
from freshswitch_backend.api.saas_roles import platform_operational_guard
from freshswitch_backend.planning.robust.api import router as robust_router

DEMO_ROUTERS = (
    robust_router,
    health.router,
    scenarios.router,
    plans.router,
    forecast.router,
    vessels.router,
    compare.router,
    evidence_quote.router,
    demo.router,
    analysis.router,
    intake.router,
    lots.router,
    settlement.router,
    coldchain.router,
    quality.router,
    provenance.router,
)

SAAS_ROUTERS = (
    auth.router,
    auth.invitation_router,
    me.router,
    saas_catalog.router,
    saas_admin.router,
    saas_shipper.router,
    saas_shipper_profile.router,
    saas_products.router,
    saas_resources.router,
    saas_platform.router,
    saas_metrics.router,
    saas_plan_confirm.router,
    saas_forwarder.router,
    saas_operations.router,
    orgs.router,
    saas_devices.router,
    saas_sandbox.router,
    saas_change_requests.router,
    saas_quantity_policy.router,
    showcase.router,
    ops.router,
    operational_quotes.router,
)

SAAS_ONLY_ENDPOINTS = frozenset(
    {
        coldchain.accept_action,
        coldchain.complete_action,
        coldchain.export_readings,
        coldchain.export_outages,
        coldchain.read_power_plan,
    }
)

RETIRED_IN_SAAS = frozenset(
    {
        auth.demo_login,
        saas_sandbox.reset_world,
        saas_plan_confirm.auto_accept_confirmation,
        saas_operations.auto_accept,
        *(route.endpoint for route in demo.router.routes),
    }
)


def router_for(*, saas_mode: bool) -> APIRouter:
    router = APIRouter(
        dependencies=[
            Depends(auth.csrf_guard),
            Depends(login_gate.require_login),
            Depends(platform_operational_guard),
        ]
    )
    sources = DEMO_ROUTERS + SAAS_ROUTERS if saas_mode else DEMO_ROUTERS
    excluded = RETIRED_IN_SAAS if saas_mode else SAAS_ONLY_ENDPOINTS
    for source in sources:
        included = copy(source)
        included.routes = [route for route in source.routes if route.endpoint not in excluded]
        router.include_router(included)
    return router
