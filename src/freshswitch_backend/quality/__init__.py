from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.quality.learning import loss_table, with_adjustments
from freshswitch_backend.quality.models import LossTable
from freshswitch_backend.quality.store import QualityHistory, QualityInputError, quality_history


def learned_table(scenario: Scenario, history: QualityHistory = quality_history) -> LossTable:
    return loss_table(history.observations(), scenario, history.params, history.seed_label)


def with_learned_loss(scenario: Scenario, history: QualityHistory = quality_history) -> Scenario:
    return with_adjustments(scenario, learned_table(scenario, history).adjustments)


__all__ = [
    "QualityHistory",
    "QualityInputError",
    "learned_table",
    "quality_history",
    "with_learned_loss",
]
