"""Sensitivity Engine — one build, three consumers.

Computes "what perturbation changes the output, and by how much."
Serves: (1) Attack Surface Map, (2) weakness-directed Red Team targeting,
(3) counterfactual explanations for the UI.
"""
from .shap_explainer import SHAPExplainer
from .gnn_ablation import GNNAblation
from .engine import SensitivityEngine, build_attack_surface_map, counterfactual_explanation

__all__ = [
    'SHAPExplainer', 'GNNAblation', 'SensitivityEngine',
    'build_attack_surface_map', 'counterfactual_explanation',
]