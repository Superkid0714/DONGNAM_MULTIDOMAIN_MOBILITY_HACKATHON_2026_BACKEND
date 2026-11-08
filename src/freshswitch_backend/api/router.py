from fastapi import APIRouter

from freshswitch_backend.api import (
    analysis,
    coldchain,
    compare,
    demo,
    forecast,
    health,
    intake,
    lots,
    plans,
    provenance,
    quality,
    scenarios,
    settlement,
    vessels,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(scenarios.router)
api_router.include_router(plans.router)
api_router.include_router(forecast.router)
api_router.include_router(vessels.router)
api_router.include_router(compare.router)
api_router.include_router(demo.router)
api_router.include_router(analysis.router)
api_router.include_router(intake.router)
api_router.include_router(lots.router)
api_router.include_router(settlement.router)
api_router.include_router(coldchain.router)
api_router.include_router(quality.router)
api_router.include_router(provenance.router)
