from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from freshswitch_backend.analysis import (
    DEFAULT_FORWARDER_SHARE,
    DEFAULT_OPS_COST_PER_VOYAGE,
    DEFAULT_SURCHARGES,
    DEFAULT_THRESHOLD_MAX,
    BenchmarkResult,
    BusinessMatrix,
    BusinessResult,
    FuelShockResult,
    SensitivityResult,
    benchmark,
    business,
    business_matrix,
    fuel_shock,
    sensitivity,
)
from freshswitch_backend.analysis.business import (
    MAX_OPS_COST_PER_VOYAGE,
    MAX_VARIABLE_COST_PER_PALLET,
)
from freshswitch_backend.api.auth import OptionalPrincipal, app_settings
from freshswitch_backend.api.compute_limits import analysis_rate, analysis_slot
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.inputs import FiniteFloat
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.persistence.tables import DEMO_ORG
from freshswitch_backend.planning.store import PlanSession, store

router = APIRouter(prefix="/analysis", tags=["analysis"])

PLATFORM_ONLY = "계획 금액 분석은 계획을 소유한 포워더 조직이 사용할 수 있습니다"
PLAN_NOT_FOUND = "계획을 찾을 수 없습니다"
PLAN_REQUIRED = "계획이 필요합니다"


def platform_or_demo_org(request: Request, principal: OptionalPrincipal) -> str:
    if app_settings(request).saas_mode:
        if principal is None or principal.org_type != "forwarder":
            raise HTTPException(status_code=403, detail=PLATFORM_ONLY)
        return principal.org_id
    if principal is None:
        return DEMO_ORG
    if principal.org_type != "forwarder":
        raise HTTPException(status_code=403, detail=PLATFORM_ONLY)
    return principal.org_id


PlatformOrg = Annotated[str, Depends(platform_or_demo_org)]


def plan_session(plan_id: str, org_id: str) -> PlanSession:
    session = store.get(plan_id, org_id=org_id)
    if session is None:
        raise HTTPException(status_code=404, detail=PLAN_NOT_FOUND)
    return session


class SensitivityRequest(BaseModel):
    scenario_id: str = "default"
    plan_id: str | None = None


class BenchmarkRequest(BaseModel):
    scenario_id: str = "default"
    plan_id: str | None = None


MAX_SURCHARGE_POINTS = 41
MAX_SURCHARGE_KRW = 20_000
Surcharge = Annotated[FiniteFloat, Field(ge=0, le=MAX_SURCHARGE_KRW)]


class SurchargeSweep(BaseModel):
    start: FiniteFloat = Field(ge=0, le=MAX_SURCHARGE_KRW)
    stop: FiniteFloat = Field(ge=0, le=MAX_SURCHARGE_KRW)
    step: FiniteFloat = Field(gt=0, le=MAX_SURCHARGE_KRW)

    @model_validator(mode="after")
    def _check(self) -> "SurchargeSweep":
        if self.stop < self.start:
            raise ValueError("sweep.stop은 sweep.start보다 작을 수 없습니다")
        if (self.stop - self.start) / self.step + 1e-9 >= MAX_SURCHARGE_POINTS:
            raise ValueError(f"유류할증료 값은 최대 {MAX_SURCHARGE_POINTS}개입니다")
        return self

    def values(self) -> list[float]:
        count = int((self.stop - self.start) / self.step + 1e-9) + 1
        return [round(self.start + i * self.step, 3) for i in range(count)]


class FuelShockRequest(BaseModel):
    scenario_id: str = "fuel-shock"
    plan_id: str | None = None
    surcharges: list[Surcharge] | None = Field(default=None, max_length=MAX_SURCHARGE_POINTS)
    sweep: SurchargeSweep | None = None
    baseline_surcharge: Surcharge | None = None
    find_thresholds: bool = True
    threshold_max: FiniteFloat = Field(default=DEFAULT_THRESHOLD_MAX, gt=0, le=MAX_SURCHARGE_KRW)

    @model_validator(mode="after")
    def _check(self) -> "FuelShockRequest":
        values = self.values()
        if any(v < 0 or v > MAX_SURCHARGE_KRW for v in values):
            raise ValueError(f"유류할증료는 0~{MAX_SURCHARGE_KRW:,}원/kg 범위여야 합니다")
        if len(values) > MAX_SURCHARGE_POINTS:
            raise ValueError(f"유류할증료 값은 최대 {MAX_SURCHARGE_POINTS}개입니다")
        return self

    def values(self) -> list[float]:
        given = list(self.surcharges or [])
        swept = self.sweep.values() if self.sweep is not None else []
        if not given and not swept:
            return list(DEFAULT_SURCHARGES)
        return list(dict.fromkeys([*given, *swept]))


def _scenario(scenario_id: str) -> Scenario:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    return scenario


def _source(request: Request, scenario_id: str, plan_id: str | None, org_id: str) -> Scenario:
    if app_settings(request).saas_mode and plan_id is None:
        raise HTTPException(status_code=422, detail=PLAN_REQUIRED)
    if plan_id is not None:
        return plan_session(plan_id, org_id).scenario
    return _scenario(scenario_id)


ANALYSIS_LIMITS = [Depends(analysis_rate)]


@router.post("/sensitivity", dependencies=ANALYSIS_LIMITS)
def run_sensitivity(
    body: SensitivityRequest, request: Request, org_id: PlatformOrg
) -> SensitivityResult:
    if app_settings(request).saas_mode and body.plan_id is None:
        raise HTTPException(status_code=422, detail=PLAN_REQUIRED)
    if body.plan_id is not None:
        session = plan_session(body.plan_id, org_id)
        with analysis_slot():
            return sensitivity(session.scenario, plan_id=body.plan_id)
    scenario = _scenario(body.scenario_id)
    with analysis_slot():
        return sensitivity(scenario)


@router.post("/fuel-shock", dependencies=ANALYSIS_LIMITS)
def run_fuel_shock(
    body: FuelShockRequest, request: Request, org_id: PlatformOrg
) -> FuelShockResult:
    scenario = _source(request, body.scenario_id, body.plan_id, org_id)
    with analysis_slot():
        return fuel_shock(
            scenario,
            surcharges=body.values(),
            baseline_surcharge=body.baseline_surcharge,
            find_thresholds=body.find_thresholds,
            threshold_max=body.threshold_max,
            plan_id=body.plan_id,
        )


@router.post("/benchmark", dependencies=ANALYSIS_LIMITS)
def run_benchmark(body: BenchmarkRequest, request: Request, org_id: PlatformOrg) -> BenchmarkResult:
    if app_settings(request).saas_mode and body.plan_id is None:
        raise HTTPException(status_code=422, detail=PLAN_REQUIRED)
    if body.plan_id is not None:
        session = plan_session(body.plan_id, org_id)
        with analysis_slot():
            return benchmark(session.scenario, session.plan, session.air_locked_lot_ids)
    scenario = _scenario(body.scenario_id)
    with analysis_slot():
        return benchmark(scenario)


@router.get("/business", dependencies=ANALYSIS_LIMITS)
def run_business(
    request: Request,
    org_id: PlatformOrg,
    scenario_id: str = "default",
    plan_id: str | None = None,
    forwarder_share: float = Query(DEFAULT_FORWARDER_SHARE, ge=0, le=1),
    ops_cost_per_voyage: int = Query(DEFAULT_OPS_COST_PER_VOYAGE, ge=0, le=MAX_OPS_COST_PER_VOYAGE),
    variable_cost_per_pallet: int = Query(0, ge=0, le=MAX_VARIABLE_COST_PER_PALLET),
) -> BusinessResult:
    scenario = _source(request, scenario_id, plan_id, org_id)
    with analysis_slot():
        return business(scenario, forwarder_share, ops_cost_per_voyage, variable_cost_per_pallet)


@router.get("/business/matrix", dependencies=ANALYSIS_LIMITS)
def run_business_matrix(
    request: Request,
    org_id: PlatformOrg,
    scenario_id: str = "default",
    plan_id: str | None = None,
    base_fee_mode: Literal["deducted", "additive"] = "deducted",
    grape_rule: Literal["usage_fee", "performance", "solo_baseline"] = "usage_fee",
    variable_cost_per_pallet: int = Query(0, ge=0, le=MAX_VARIABLE_COST_PER_PALLET),
) -> BusinessMatrix:
    scenario = _source(request, scenario_id, plan_id, org_id)
    with analysis_slot():
        result = business_matrix(scenario, base_fee_mode, grape_rule, variable_cost_per_pallet)
    if result is None:
        raise HTTPException(
            status_code=422, detail="시즌 물량 품목(딸기·포도)이 없는 시나리오입니다"
        )
    return result
