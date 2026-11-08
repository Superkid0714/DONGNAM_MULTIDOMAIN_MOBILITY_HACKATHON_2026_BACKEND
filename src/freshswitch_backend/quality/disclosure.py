from collections.abc import Iterable

from freshswitch_backend.domain.models import Mode, Scenario
from freshswitch_backend.quality.learning import estimate_cell
from freshswitch_backend.quality.models import ExpectedLoss, Observation
from freshswitch_backend.quality.params import QualityParams
from freshswitch_backend.quality.store import LearningScope, QualityHistory, quality_history

LEARNED_BASIS = "도착 검수 학습"
DEFAULT_BASIS = "기본값(가정)"


def expected_loss(
    scenario: Scenario,
    product_id: str,
    destination: str,
    observations: Iterable[Observation],
    params: QualityParams,
    mode: Mode = "sea",
) -> ExpectedLoss:
    key = (product_id, destination, mode)
    group = [o for o in observations if (o.product_id, o.destination, o.mode) == key]
    cell = estimate_cell(scenario, product_id, destination, mode, group, params)
    if not cell.observations:
        return ExpectedLoss(
            rate=cell.posterior_rate,
            mode=mode,
            basis=DEFAULT_BASIS,
            n_observations=0,
            source=cell.prior_source,
        )
    return ExpectedLoss(
        rate=cell.posterior_rate,
        mode=mode,
        basis=LEARNED_BASIS,
        n_observations=cell.observations,
        source=cell.basis,
    )


class LossDisclosure:
    def __init__(self, scope: LearningScope, history: QualityHistory = quality_history) -> None:
        self._observations = history.observations(scope)
        self._params = history.params

    def for_lot(self, scenario: Scenario, product_id: str, destination: str) -> ExpectedLoss:
        return expected_loss(scenario, product_id, destination, self._observations, self._params)


def lot_expected_loss(
    scenario: Scenario,
    product_id: str,
    destination: str,
    scope: LearningScope,
    history: QualityHistory = quality_history,
) -> ExpectedLoss:
    return LossDisclosure(scope, history).for_lot(scenario, product_id, destination)
