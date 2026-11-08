from typing import Literal

from pydantic import BaseModel


class ParamRange(BaseModel):
    key: str
    label: str
    unit: str
    base: float
    low: float
    high: float
    source: str


class LotChange(BaseModel):
    lot_id: str
    base: str
    case: str


class SensitivityCase(BaseModel):
    value: float
    factor: float
    status: str
    total_savings: int
    savings_delta: int
    total_fee: int
    containers_used: int
    shipper_savings: dict[str, int]
    mode_changed: bool
    changed_lots: list[LotChange]


class ParamSensitivity(BaseModel):
    param: ParamRange
    low: SensitivityCase
    high: SensitivityCase
    conservative: Literal["low", "high"]
    optimistic: Literal["low", "high"]
    savings_impact: int
    fee_impact: int


class TornadoBar(BaseModel):
    key: str
    label: str
    base_savings: int
    savings_low: int
    savings_high: int
    savings_impact: int
    fee_low: int
    fee_high: int


class Threshold(BaseModel):
    key: str
    label: str
    unit: str
    direction: Literal["down", "up"]
    base: float
    threshold: float | None
    factor: float | None
    searched_to: float
    iterations: int
    base_outcome: str
    flipped_outcome: str | None
    changed_lots: list[LotChange]


class AlternativeSummary(BaseModel):
    id: str
    label: str
    feasible: bool
    total_cost: int
    total_savings: int
    total_fee: int
    container_fill: dict[str, int]


class ReplanSensitivity(BaseModel):
    event: dict
    base_outcome: str
    alternatives: list[AlternativeSummary]
    thresholds: list[Threshold]


class SensitivityResult(BaseModel):
    scenario_id: str
    plan_id: str | None
    base_total_savings: int
    base_total_fee: int
    base_shipper_savings: dict[str, int]
    base_routes: dict[str, str]
    params: list[ParamSensitivity]
    tornado: list[TornadoBar]
    thresholds: list[Threshold]
    replan: ReplanSensitivity | None
    collection_basis: Literal["estimate"] = "estimate"
    route_base_total_savings: int
    route_base_total_fee: int
    solve_count: int
    solve_ms: int


class MethodResult(BaseModel):
    method: str
    label: str
    description: str
    containers_used: int
    avg_fill: float
    sea_pallets: int
    air_pallets: int
    unassigned_lots: int
    unassigned_pallets: int
    total_baseline_cost: int
    total_shipper_cost: int
    total_savings: int
    total_fee: int
    total_operational_baseline_cost: int = 0
    total_operational_cost: int = 0
    total_operational_savings: int = 0
    collection_basis: Literal["route"] = "route"
    total_collection_estimate: int = 0
    total_collection_route: int = 0
    shippers_with_loss: int
    constraint_violations: int
    violation_details: list[str]
    solve_ms: int
    locations: dict[str, str]


class ReplanBenchmarkRow(BaseModel):
    id: str
    label: str
    role: Literal["manual", "recommended", "other"]
    feasible: bool
    total_cost: int
    cost_vs_manual: int
    total_savings: int
    total_fee: int
    container_fill: dict[str, int]
    summary: str


class ReplanBenchmark(BaseModel):
    event: dict
    recommended_id: str | None
    rows: list[ReplanBenchmarkRow]
    improvement_vs_manual: int


class BenchmarkResult(BaseModel):
    scenario_id: str
    methods: list[MethodResult]
    replan: ReplanBenchmark | None
    solve_ms: int


class ContainerEconomics(BaseModel):
    container_id: str
    pallets: int
    capacity_pallets: int
    fill: float
    container_cost: int
    savings: int
    margin_per_pallet: int
    breakeven_savings_pallets: float | None
    breakeven_platform_pallets: float | None
    breakeven_platform_fill: float | None


class VoyageEconomics(BaseModel):
    voyage_id: str
    vessel_name: str
    etd: str
    containers_used: int
    pallets: int
    capacity_pallets: int
    fill: float
    savings: int
    fee_revenue: int
    forwarder_share: int
    ops_cost: int
    platform_net: int
    variable_cost: int = 0
    contribution_margin: int = 0
    fixed_cost_allocation: int = 0
    usage_fee_rule_revenue: int
    breakeven_containers: float | None
    containers: list[ContainerEconomics]


class SeasonLine(BaseModel):
    product_id: str
    tonnes: float
    pallets: int
    containers: float
    savings_per_pallet: int
    revenue_rule: str
    revenue: int
    source: str


class SeasonProjection(BaseModel):
    lines: list[SeasonLine]
    revenue: int
    forwarder_share: int
    fixed_cost: int
    platform_net: int
    breakeven_pallets: int | None
    source: str


class Assumption(BaseModel):
    key: str
    value: float
    unit: str
    source: str


class BasisLine(BaseModel):
    product_id: str
    pallets: int
    savings_per_pallet: int


class BasisComparison(BaseModel):
    basis: Literal["freight_difference", "total_cost"]
    label: str
    source: str
    lines: list[BasisLine]
    fee_rate: float
    season_revenue_performance: int
    season_revenue_proposal_rule: int
    breakeven_pallets: int | None


class BusinessResult(BaseModel):
    scenario_id: str
    assumptions: list[Assumption]
    voyages: list[VoyageEconomics]
    total_fee_revenue: int
    total_platform_net: int
    total_contribution_margin: int = 0
    total_fixed_cost_allocation: int = 0
    season: SeasonProjection | None
    basis: list[BasisComparison] = []


class AirRegime(BaseModel):
    key: Literal["normal", "surcharge"]
    label: str
    air_freight_per_kg_krw: float
    source: str
    savings_per_pallet: dict[str, int]
    grape_solo_savings_per_pallet: int | None


class FixedLevel(BaseModel):
    key: Literal["full", "mvp_cash"]
    label: str
    annual_cost: int
    source: str


class MatrixRow(BaseModel):
    fee_rate: float
    base_fee_per_pallet: int
    forwarder_share: float
    fixed_level: Literal["full", "mvp_cash"]
    air_regime: Literal["normal", "surcharge"]
    pallets: int
    revenue: int
    forwarder_cost: int
    variable_cost: int
    contribution: int
    fixed_cost: int
    net: int
    breakeven_pallets: int | None
    breaks_even: bool
    needs: list[str]


class MatrixHighlight(BaseModel):
    summary: str
    needs: list[str]
    row: MatrixRow


class BusinessMatrix(BaseModel):
    scenario_id: str
    base_fee_mode: Literal["deducted", "additive"]
    grape_rule: Literal["usage_fee", "performance", "solo_baseline"]
    variable_cost_per_pallet: int
    season_pallets: dict[str, int]
    baseline: dict[str, float | int | str]
    air_regimes: list[AirRegime]
    fixed_levels: list[FixedLevel]
    rules: list[str]
    rows: list[MatrixRow]
    highlights: list[MatrixHighlight]
    break_even_rows: int
    solve_ms: int


class ShockLot(BaseModel):
    lot_id: str
    shipper_id: str
    product_id: str
    pallets: int
    mode: Literal["sea", "air", "unassigned"]
    location: str
    voyage_id: str | None


class ShockContainer(BaseModel):
    container_id: str
    voyage_id: str
    used: bool
    pallets: int
    capacity_pallets: int
    utilization: float
    lot_ids: list[str]


class ShockShipper(BaseModel):
    shipper_id: str
    pallets: int
    sea_pallets: int
    air_pallets: int
    baseline_cost: int
    plan_cost: int
    savings: int
    fee: int
    shipper_gain: int
    savings_delta: int


class ModeSwitch(BaseModel):
    lot_id: str
    shipper_id: str
    pallets: int
    base: str
    case: str
    direction: Literal["air_to_sea", "sea_to_air", "sea_to_sea", "to_unassigned", "from_unassigned"]


class FuelShockCase(BaseModel):
    surcharge_per_kg: float
    air_rate_per_kg: float
    air_freight_per_pallet: int
    is_baseline: bool
    status: str
    lots: list[ShockLot]
    containers: list[ShockContainer]
    shippers: list[ShockShipper]
    containers_used: int
    sea_pallets: int
    air_pallets: int
    unassigned_pallets: int
    total_baseline_cost: int
    total_shared_cost: int
    total_savings: int
    total_fee: int
    plan_changed: bool
    switches: list[ModeSwitch]
    savings_delta: int
    fee_delta: int
    shared_cost_delta: int
    summary: str


class SurchargeThreshold(BaseModel):
    direction: Literal["down", "up"]
    searched_from: float
    searched_to: float
    surcharge_per_kg: float | None
    iterations: int
    switches: list[ModeSwitch]


class FuelShockResult(BaseModel):
    scenario_id: str
    plan_id: str | None
    base_freight_per_kg: float
    baseline_surcharge_per_kg: float
    pallet_kg: float
    sources: dict[str, str]
    cases: list[FuelShockCase]
    thresholds: list[SurchargeThreshold]
    collection_basis: Literal["route"] = "route"
    solve_count: int
    solve_ms: int
