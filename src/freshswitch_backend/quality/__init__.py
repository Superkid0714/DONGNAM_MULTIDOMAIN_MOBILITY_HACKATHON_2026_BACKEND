from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.quality.learning import loss_table, with_adjustments
from freshswitch_backend.quality.models import LossTable
from freshswitch_backend.quality.store import (
    LearningScope,
    QualityAccessDenied,
    QualityHistory,
    QualityInputError,
    learning_scope,
    quality_history,
)


def learned_table(
    scenario: Scenario, history: QualityHistory = quality_history, scope: LearningScope = "demo"
) -> LossTable:
    return loss_table(
        history.observations(scope), scenario, history.params, history.seed_label_for(scope)
    )


def with_learned_loss(
    scenario: Scenario, history: QualityHistory = quality_history, scope: LearningScope = "demo"
) -> Scenario:
    return with_adjustments(scenario, learned_table(scenario, history, scope).adjustments)


__all__ = [
    "LearningScope",
    "QualityAccessDenied",
    "QualityHistory",
    "QualityInputError",
    "learned_table",
    "learning_scope",
    "quality_history",
    "with_learned_loss",
]
