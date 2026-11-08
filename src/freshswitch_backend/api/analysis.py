from typing import Literal

from fastapi import APIRouter, HTTPException, Query
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
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.planning.store import store

router = APIRouter(prefix="/analysis", tags=["analysis"])


class SensitivityRequest(BaseModel):
    scenario_id: str = "default"
    plan_id: str | None = None


class BenchmarkRequest(BaseModel):
    scenario_id: str = "default"


MAX_SURCHARGE_POINTS = 41
MAX_SURCHARGE_KRW = 20_000


class SurchargeSweep(BaseModel):
    start: float = Field(ge=0, le=MAX_SURCHARGE_KRW)
    stop: float = Field(ge=0, le=MAX_SURCHARGE_KRW)
    step: float = Field(gt=0)

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
    surcharges: list[float] | None = Field(default=None, max_length=MAX_SURCHARGE_POINTS)
    sweep: SurchargeSweep | None = None
    baseline_surcharge: float | None = Field(default=None, ge=0, le=MAX_SURCHARGE_KRW)
    find_thresholds: bool = True
    threshold_max: float = Field(default=DEFAULT_THRESHOLD_MAX, gt=0, le=MAX_SURCHARGE_KRW)

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


@router.post("/sensitivity")
def run_sensitivity(body: SensitivityRequest) -> SensitivityResult:
    if body.plan_id is not None:
        session = store.get(body.plan_id)
        if session is None:
            raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
        return sensitivity(session.scenario, plan_id=body.plan_id)
    return sensitivity(_scenario(body.scenario_id))


@router.post("/fuel-shock")
def run_fuel_shock(body: FuelShockRequest) -> FuelShockResult:
    if body.plan_id is not None:
        session = store.get(body.plan_id)
        if session is None:
            raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
        scenario = session.scenario
    else:
        scenario = _scenario(body.scenario_id)
    return fuel_shock(
        scenario,
        surcharges=body.values(),
        baseline_surcharge=body.baseline_surcharge,
        find_thresholds=body.find_thresholds,
        threshold_max=body.threshold_max,
        plan_id=body.plan_id,
    )


@router.post("/benchmark")
def run_benchmark(body: BenchmarkRequest) -> BenchmarkResult:
    return benchmark(_scenario(body.scenario_id))


@router.get("/business")
def run_business(
    scenario_id: str = "default",
    forwarder_share: float = Query(DEFAULT_FORWARDER_SHARE, ge=0, le=1),
    ops_cost_per_voyage: int = Query(DEFAULT_OPS_COST_PER_VOYAGE, ge=0),
    variable_cost_per_pallet: int = Query(0, ge=0),
) -> BusinessResult:
    return business(
        _scenario(scenario_id), forwarder_share, ops_cost_per_voyage, variable_cost_per_pallet
    )


@router.get("/business/matrix")
def run_business_matrix(
    scenario_id: str = "default",
    base_fee_mode: Literal["deducted", "additive"] = "deducted",
    grape_rule: Literal["usage_fee", "performance", "solo_baseline"] = "usage_fee",
    variable_cost_per_pallet: int = Query(0, ge=0),
) -> BusinessMatrix:
    result = business_matrix(
        _scenario(scenario_id), base_fee_mode, grape_rule, variable_cost_per_pallet
    )
    if result is None:
        raise HTTPException(
            status_code=422, detail="시즌 물량 품목(딸기·포도)이 없는 시나리오입니다"
        )
    return result
