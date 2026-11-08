import json
import threading
import uuid
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from freshswitch_backend.coldchain import ColdchainError, NotFound, coldchain_store
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST, Scenario
from freshswitch_backend.execution.quality import ArrivalSettlementResult
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.persistence import Database, database, get_meta, set_meta
from freshswitch_backend.persistence.tables import ObservationRow
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


class QualityInputError(ValueError):
    pass


def _load_seed() -> tuple[str, list[SeedObservation]]:
    raw = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return raw["label"], [SeedObservation.model_validate(o) for o in raw["observations"]]


def _container_outage(plan_id: str, container_id: str) -> float | None:
    try:
        record = coldchain_store.get_record(container_id, plan_id)
        if record.plan_id != plan_id:
            return None
        return coldchain_store.state(container_id, plan_id, points=2).outage.cumulative_minutes
    except (NotFound, ColdchainError):
        return None


def _row(observation: Observation, inspection_revision: int | None = None) -> ObservationRow:
    return ObservationRow(
        id=observation.id,
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

    def reference_scenario(self) -> Scenario:
        scenario = get_scenario("default")
        if scenario is None:
            raise QualityInputError("기준 시나리오(default)가 없습니다")
        return scenario

    def _from_input(
        self, body: ObservationIn, origin: str, obs_id: str, scenario: Scenario
    ) -> Observation:
        if body.product_id not in {p.id for p in scenario.products}:
            raise QualityInputError(f"시나리오에 없는 품목입니다: {body.product_id}")
        uplift = condition_uplift(scenario, body.product_id, body.mode, body.wait_days)
        planned = (
            body.planned_loss_pct / 100
            if body.planned_loss_pct is not None
            else prior_model_rate(scenario, body.product_id, body.mode, body.wait_days)
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
            recorded_at=plan_store.clock(),
            note=body.note,
        )

    def _seed_rows(self, db: Session, scenario: Scenario) -> int:
        for item in self._seed:
            db.add(_row(self._from_input(item, "seed", item.id, scenario)))
        set_meta(db, SEEDED_KEY, True)
        return len(self._seed)

    def _ensure_seeded(self, db: Session) -> None:
        if get_meta(db, SEEDED_KEY):
            return
        self._seed_rows(db, self.reference_scenario())
        db.flush()

    def reset(self, seed: bool = True) -> tuple[int, int]:
        scenario = self.reference_scenario()
        with self._lock, self._db.begin() as db:
            cleared = db.scalar(select(func.count()).select_from(ObservationRow)) or 0
            db.execute(delete(ObservationRow))
            if seed:
                return cleared, self._seed_rows(db, scenario)
            set_meta(db, SEEDED_KEY, True)
            return cleared, 0

    def observations(self) -> list[Observation]:
        with self._lock, self._db.begin() as db:
            self._ensure_seeded(db)
            rows = db.scalars(select(ObservationRow).order_by(ObservationRow.seq))
            return [Observation.model_validate(row.payload) for row in rows]

    def get(self, obs_id: str) -> Observation | None:
        with self._lock, self._db.begin() as db:
            self._ensure_seeded(db)
            row = db.get(ObservationRow, obs_id)
            return Observation.model_validate(row.payload) if row is not None else None

    def delete(self, obs_id: str) -> bool:
        with self._lock, self._db.begin() as db:
            self._ensure_seeded(db)
            return (
                db.execute(delete(ObservationRow).where(ObservationRow.id == obs_id)).rowcount > 0
            )

    def _append(
        self, db: Session, observation: Observation, inspection_revision: int | None = None
    ) -> None:
        db.add(_row(observation, inspection_revision))
        db.flush()
        cutoff = db.scalar(
            select(ObservationRow.seq)
            .order_by(ObservationRow.seq.desc())
            .offset(MAX_OBSERVATIONS)
            .limit(1)
        )
        if cutoff is not None:
            db.execute(delete(ObservationRow).where(ObservationRow.seq <= cutoff))

    def add(self, body: ObservationIn) -> Observation:
        observation = self._from_input(
            body, "manual", f"obs-{uuid.uuid4().hex[:12]}", self.reference_scenario()
        )
        with self._lock, self._db.begin() as db:
            self._ensure_seeded(db)
            self._append(db, observation)
        return observation

    def record_arrival(
        self, plan_id: str, session: PlanSession, result: ArrivalSettlementResult
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
            outage = _container_outage(plan_id, container.id)
        else:
            baseline = model.air_loss_rate(lot)
        reference = prior_model_rate(prior, lot.product_id, assignment.mode, 0)
        uplift = baseline - reference
        actual = result.inspected_loss_rate
        now = plan_store.clock()
        with self._lock, self._db.begin() as db:
            self._ensure_seeded(db)
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
            if existing is not None:
                existing.product_id = observation.product_id
                existing.recorded_at = observation.recorded_at
                existing.inspection_revision = session.inspection_revision
                existing.payload = observation.model_dump(mode="json")
            else:
                self._append(db, observation, session.inspection_revision)
            return observation


quality_history = QualityHistory()
