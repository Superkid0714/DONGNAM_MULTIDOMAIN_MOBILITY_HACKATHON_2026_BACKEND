import json
import threading
import uuid
from datetime import datetime, time
from pathlib import Path
from typing import Literal, NamedTuple

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.orm import Session

from freshswitch_backend.coldchain import ColdchainError, NotFound, coldchain_store
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST, Scenario
from freshswitch_backend.execution.quality import ArrivalSettlementResult
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.persistence import Database, database, get_meta, set_meta
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    DEMO_SCOPE,
    ObservationRow,
    OrganizationRow,
    PlanRow,
    SandboxWorldRow,
)
from freshswitch_backend.planning.store import PlanSession
from freshswitch_backend.planning.store import store as plan_store
from freshswitch_backend.quality.learning import condition_uplift, outage_bucket, prior_model_rate
from freshswitch_backend.quality.models import (
    MixKind,
    Observation,
    ObservationIn,
    SeedObservation,
)
from freshswitch_backend.quality.params import QualityParams

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "quality" / "seed-observations.json"
MAX_OBSERVATIONS = 5000
SEEDED_KEY = "quality_seeded"
RESET_ORG_TYPES = frozenset({"platform"})
LearningScope = Literal["demo", "sandbox", "live"]
SEEDED_SCOPES = frozenset({"demo", "sandbox"})
LIVE_LABEL = "실측: 이 범위의 도착 검수·수동 관측만 학습한다(시드 관측 없음)"


class ScopeRef(NamedTuple):
    kind: str
    world_id: str | None


def scope_ref(scope: ScopeRef | str, world_id: str | None = None) -> ScopeRef:
    if isinstance(scope, ScopeRef):
        if world_id is not None and scope.world_id is None:
            return ScopeRef(scope.kind, world_id)
        return scope
    return ScopeRef(scope, world_id)


def _kind(scope: ScopeRef | str) -> str:
    return scope.kind if isinstance(scope, ScopeRef) else scope


def learning_scope(org_id: str | None) -> ScopeRef:
    if org_id is None or org_id == DEMO_ORG:
        return ScopeRef(DEMO_SCOPE, None)
    with database.begin() as db:
        org = db.get(OrganizationRow, org_id)
    if org is None or org.type == "demo":
        return ScopeRef(DEMO_SCOPE, None)
    if org.world_id and org.sandbox:
        return ScopeRef("sandbox", org.world_id)
    if org.world_id is None and not org.sandbox:
        return ScopeRef("live", None)
    return ScopeRef(DEMO_SCOPE, None)


def _seeded_key(scope: str) -> str:
    return SEEDED_KEY if scope == DEMO_SCOPE else f"{SEEDED_KEY}:{scope}"


def _seed_id(item_id: str, scope: str) -> str:
    return item_id if scope == DEMO_SCOPE else f"{scope}-{item_id}"


def _in_scope(scope: ScopeRef):
    kind = ObservationRow.scope == scope.kind
    if scope.world_id is None:
        return and_(kind, ObservationRow.world_id.is_(None))
    return and_(kind, ObservationRow.world_id == scope.world_id)


class QualityInputError(ValueError):
    pass


class QualityAccessDenied(PermissionError):
    pass


def _check_reset(org_type: str | None) -> None:
    if org_type is not None and org_type not in RESET_ORG_TYPES:
        raise QualityAccessDenied("품질 관측 초기화·삭제는 플랫폼 조직만 할 수 있습니다")


def _seed_recorded_at(item: SeedObservation) -> datetime | None:
    if item.observed_on is None:
        return None
    return datetime.combine(item.observed_on, time.min, tzinfo=KST)


def _load_seed() -> tuple[str, list[SeedObservation]]:
    raw = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return raw["label"], [SeedObservation.model_validate(o) for o in raw["observations"]]


def _container_outage(plan_id: str, container_id: str, org_id: str = DEMO_ORG) -> float | None:
    try:
        record = coldchain_store.get_record(container_id, plan_id, org_id)
        if record.plan_id != plan_id:
            return None
        state = coldchain_store.state(container_id, plan_id, points=2, org_id=org_id)
        return state.outage.cumulative_minutes
    except (NotFound, ColdchainError):
        return None


def _plan_alive():
    return exists().where(PlanRow.plan_id == ObservationRow.plan_id)


def _orphan_arrival():
    return (ObservationRow.origin == "arrival") & ~_plan_alive()


def _counted():
    return or_(ObservationRow.origin != "arrival", _plan_alive())


def _row(
    observation: Observation, scope: ScopeRef | str, inspection_revision: int | None = None
) -> ObservationRow:
    ref = scope_ref(scope)
    return ObservationRow(
        id=observation.id,
        scope=ref.kind,
        world_id=ref.world_id,
        origin=observation.origin,
        product_id=observation.product_id,
        plan_id=observation.plan_id,
        lot_id=observation.lot_id,
        recorded_at=observation.recorded_at,
        inspection_revision=inspection_revision,
        payload=observation.model_dump(mode="json"),
    )


class QualityHistory:
    def __init__(self, params: QualityParams | None = None, db: Database | None = None) -> None:
        self.params = params or QualityParams()
        self._db = db or database
        self._lock = threading.RLock()
        self.seed_label, self._seed = _load_seed()

    def seed_label_for(self, scope: ScopeRef | str = "demo") -> str:
        return self.seed_label if _kind(scope) in SEEDED_SCOPES else LIVE_LABEL

    def seed_world(self, db: Session, world_id: str, *, prefixed: bool) -> int:
        scenario = self.reference_scenario()
        ref = ScopeRef("sandbox", world_id)
        count = 0
        for item in self._seed:
            obs_id = f"{world_id}-{item.id}" if prefixed else _seed_id(item.id, "sandbox")
            observation = self._from_input(item, "seed", obs_id, scenario, _seed_recorded_at(item))
            db.add(_row(observation, ref))
            count += 1
        world = db.get(SandboxWorldRow, world_id)
        if world is not None:
            world.quality_seeded = True
        return count

    def reference_scenario(self) -> Scenario:
        scenario = get_scenario("default")
        if scenario is None:
            raise QualityInputError("기준 시나리오(default)가 없습니다")
        return scenario

    def _from_input(
        self,
        body: ObservationIn,
        origin: str,
        obs_id: str,
        scenario: Scenario,
        recorded_at: datetime | None = None,
    ) -> Observation:
        if body.product_id not in {p.id for p in scenario.products}:
            raise QualityInputError(f"시나리오에 없는 품목입니다: {body.product_id}")
        hours = (body.outage_minutes or 0) / 60
        uplift = condition_uplift(
            scenario, body.product_id, body.mode, body.wait_days, outage_hours=hours
        )
        planned = (
            body.planned_loss_pct / 100
            if body.planned_loss_pct is not None
            else prior_model_rate(
                scenario, body.product_id, body.mode, body.wait_days, outage_hours=hours
            )
        )
        actual = body.actual_loss_pct / 100
        return Observation(
            id=obs_id,
            origin=origin,
            source=body.source,
            product_id=body.product_id,
            destination=body.destination,
            mode=body.mode,
            pallets=body.pallets,
            wait_days=body.wait_days,
            outage_minutes=body.outage_minutes,
            outage_bucket=outage_bucket(body.outage_minutes, self.params),
            mix=body.mix,
            so2_in_container=body.so2_in_container,
            co_loaded_product_ids=body.co_loaded_product_ids,
            planned_loss_rate=round(planned, 6),
            actual_loss_rate=round(actual, 6),
            condition_uplift=round(uplift, 6),
            adjusted_loss_rate=round(max(0.0, actual - uplift), 6),
            observed_on=body.observed_on or plan_store.clock().astimezone(KST).date(),
            recorded_at=recorded_at or plan_store.clock(),
            note=body.note,
        )

    def _seed_observations(self, scenario: Scenario, scope: LearningScope) -> list[Observation]:
        if scope not in SEEDED_SCOPES:
            return []
        return [
            self._from_input(
                item, "seed", _seed_id(item.id, scope), scenario, _seed_recorded_at(item)
            )
            for item in self._seed
        ]

    def _seed_rows(self, db: Session, scenario: Scenario, scope: LearningScope) -> int:
        set_meta(db, _seeded_key(scope), True)
        seeds = self._seed_observations(scenario, scope)
        for observation in seeds:
            db.add(_row(observation, scope))
        return len(seeds)

    def _ensure_seeded(self, db: Session, scope: ScopeRef | str) -> None:
        ref = scope_ref(scope)
        if ref.kind != DEMO_SCOPE:
            return
        if get_meta(db, _seeded_key(ref.kind)):
            return
        self._seed_rows(db, self.reference_scenario(), ref.kind)
        db.flush()

    def _pending_seeds(self, db: Session, scope: ScopeRef | str) -> list[Observation]:
        ref = scope_ref(scope)
        if ref.kind != DEMO_SCOPE or get_meta(db, _seeded_key(ref.kind)):
            return []
        return self._seed_observations(self.reference_scenario(), ref.kind)

    def ensure_seeded(self, scopes: frozenset[str] = SEEDED_SCOPES) -> None:
        with self._lock, self._db.begin() as db:
            for scope in sorted(scopes):
                if scope == "sandbox":
                    continue
                self._ensure_seeded(db, scope)

    def reset(
        self,
        seed: bool = True,
        org_type: str | None = None,
        scope: ScopeRef | str = "demo",
        world_id: str | None = None,
    ) -> tuple[int, int]:
        _check_reset(org_type)
        ref = scope_ref(scope, world_id)
        if ref.kind == "sandbox" and not ref.world_id:
            raise QualityInputError("world_id가 필요합니다")
        scenario = self.reference_scenario()
        in_scope = _in_scope(ref)
        with self._lock, self._db.begin() as db:
            cleared = db.scalar(select(func.count()).select_from(ObservationRow).where(in_scope))
            db.execute(delete(ObservationRow).where(in_scope))
            if not seed:
                if ref.kind != "sandbox":
                    set_meta(db, _seeded_key(ref.kind), True)
                return cleared or 0, 0
            if ref.kind == "sandbox" and ref.world_id is not None:
                return cleared or 0, self.seed_world(
                    db, ref.world_id, prefixed=ref.world_id != "seed"
                )
            return cleared or 0, self._seed_rows(db, scenario, ref.kind)

    def observations(self, scope: ScopeRef | str = "demo") -> list[Observation]:
        ref = scope_ref(scope)
        with self._lock, self._db.begin() as db:
            rows = db.scalars(
                select(ObservationRow)
                .where(_in_scope(ref), _counted())
                .order_by(ObservationRow.seq)
            )
            stored = [Observation.model_validate(row.payload) for row in rows]
            return [*stored, *self._pending_seeds(db, ref)]

    def get(self, obs_id: str, scope: ScopeRef | str = "demo") -> Observation | None:
        ref = scope_ref(scope)
        with self._lock, self._db.begin() as db:
            row = db.scalars(
                select(ObservationRow).where(ObservationRow.id == obs_id, _counted())
            ).first()
            if row is not None:
                if row.scope != ref.kind or row.world_id != ref.world_id:
                    return None
                return Observation.model_validate(row.payload)
            return next((o for o in self._pending_seeds(db, ref) if o.id == obs_id), None)

    def delete(
        self, obs_id: str, org_type: str | None = None, scope: ScopeRef | str = "demo"
    ) -> bool:
        _check_reset(org_type)
        ref = scope_ref(scope)
        with self._lock, self._db.begin() as db:
            self._ensure_seeded(db, ref)
            target = delete(ObservationRow).where(ObservationRow.id == obs_id, _in_scope(ref))
            return db.execute(target).rowcount > 0

    def prune_orphans(self) -> int:
        with self._lock, self._db.begin() as db:
            return db.execute(delete(ObservationRow).where(_orphan_arrival())).rowcount

    def _append(
        self,
        db: Session,
        observation: Observation,
        scope: ScopeRef | str,
        inspection_revision: int | None = None,
    ) -> None:
        ref = scope_ref(scope)
        in_scope = _in_scope(ref)
        db.execute(delete(ObservationRow).where(in_scope, _orphan_arrival()))
        db.add(_row(observation, ref, inspection_revision))
        db.flush()
        cutoff = db.scalar(
            select(ObservationRow.seq)
            .where(in_scope)
            .order_by(ObservationRow.seq.desc())
            .offset(MAX_OBSERVATIONS)
            .limit(1)
        )
        if cutoff is not None:
            db.execute(delete(ObservationRow).where(in_scope, ObservationRow.seq <= cutoff))

    def add(self, body: ObservationIn, scope: ScopeRef | str = "demo") -> Observation:
        observation = self._from_input(
            body, "manual", f"obs-{uuid.uuid4().hex[:12]}", self.reference_scenario()
        )
        with self._lock, self._db.begin() as db:
            self._ensure_seeded(db, scope)
            self._append(db, observation, scope)
        return observation

    def record_arrival(
        self,
        plan_id: str,
        session: PlanSession,
        result: ArrivalSettlementResult,
        org_id: str = DEMO_ORG,
    ) -> Observation:
        scenario = session.scenario
        plan = session.plan
        lot = next(x for x in scenario.lots if x.id == result.lot_id)
        assignment = next(a for a in plan.assignments if a.lot_id == result.lot_id)
        prior = scenario.model_copy(
            update={"cost_params": scenario.cost_params.model_copy(update={"loss_adjustments": []})}
        )
        model = CostModel(prior)
        products = {p.id: p for p in scenario.products}
        outage: float | None = None
        mix: MixKind = "solo"
        so2 = products[lot.product_id].so2_treated if lot.product_id in products else False
        co_loaded: list[str] = []
        wait = pull = 0
        voyage_id = None
        if assignment.mode == "sea" and assignment.container_id:
            container = model.containers[assignment.container_id]
            voyage = model.voyage_of(container)
            voyage_id = voyage.id
            wait = model.sea_wait_days(lot, voyage, assignment.pull_forward_days)
            pull = assignment.pull_forward_days
            baseline = model.sea_loss_rate(lot, voyage, pull)
            load = next((c for c in plan.containers if c.container_id == container.id), None)
            if load is not None:
                co_loaded = [p for p in load.product_ids if p != lot.product_id]
                so2 = any(products[p].so2_treated for p in load.product_ids if p in products)
                if co_loaded:
                    mix = "mixed_products"
                elif len(load.shipper_ids) > 1:
                    mix = "shared_same_product"
            outage = _container_outage(plan_id, container.id, org_id)
        else:
            baseline = model.air_loss_rate(lot)
        reference = prior_model_rate(prior, lot.product_id, assignment.mode, 0)
        uplift = baseline - reference
        actual = result.inspected_loss_rate
        now = plan_store.clock()
        scope = learning_scope(org_id)
        with self._lock, self._db.begin() as db:
            self._ensure_seeded(db, scope)
            alive = db.scalar(
                select(PlanRow.plan_id).where(PlanRow.plan_id == plan_id).with_for_update(read=True)
            )
            existing = db.scalars(
                select(ObservationRow).where(
                    ObservationRow.origin == "arrival",
                    ObservationRow.plan_id == plan_id,
                    ObservationRow.lot_id == result.lot_id,
                )
            ).first()
            obs_id = existing.id if existing is not None else f"arr-{uuid.uuid4().hex[:12]}"
            if (
                existing is not None
                and existing.inspection_revision is not None
                and session.inspection_revision < existing.inspection_revision
            ):
                return Observation.model_validate(existing.payload)
            observation = Observation(
                id=obs_id,
                origin="arrival",
                source=f"도착 검수(계획 {plan_id[:8]}, {result.lot_id})",
                product_id=lot.product_id,
                destination=lot.destination,
                mode=assignment.mode,
                pallets=lot.pallets,
                wait_days=wait,
                pull_forward_days=pull,
                outage_minutes=outage,
                outage_bucket=outage_bucket(outage, self.params),
                mix=mix,
                so2_in_container=so2,
                co_loaded_product_ids=co_loaded,
                planned_loss_rate=result.planned_loss_rate,
                actual_loss_rate=actual,
                condition_uplift=round(uplift, 6),
                adjusted_loss_rate=round(max(0.0, actual - uplift), 6),
                observed_on=now.astimezone(KST).date(),
                recorded_at=now,
                plan_id=plan_id,
                lot_id=lot.id,
                shipper_id=lot.shipper_id,
                container_id=assignment.container_id,
                voyage_id=voyage_id,
                note=result.note,
            )
            if alive is None:
                return observation
            if existing is not None:
                ref = scope_ref(scope)
                existing.scope = ref.kind
                existing.world_id = ref.world_id
                existing.product_id = observation.product_id
                existing.recorded_at = observation.recorded_at
                existing.inspection_revision = session.inspection_revision
                existing.payload = observation.model_dump(mode="json")
            else:
                self._append(db, observation, scope, session.inspection_revision)
            return observation


quality_history = QualityHistory()
