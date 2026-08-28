"""Evaluation Harness — multi-prevalence evaluation and cost model."""
from .harness import (
    evaluate_at_prevalence, multi_prevalence_eval,
    full_report, PREVALENCES,
)
from .ood_matrix import build_ood_matrix, MECHANISM_TYPES
from .fidelity import (
    statistical_layer, behavioral_layer, adversarial_layer,
    build_fidelity_report,
)

__all__ = [
    'evaluate_at_prevalence', 'multi_prevalence_eval',
    'full_report', 'PREVALENCES',
    'build_ood_matrix', 'MECHANISM_TYPES',
    'statistical_layer', 'behavioral_layer', 'adversarial_layer',
    'build_fidelity_report',
]