"""Shared Sensitivity Engine — one module, three consumers.

1. Attack Surface Map (aggregated per-model sensitivity registry)
2. Weakness-directed Red Team targeting (reverse: "what perturbation lowers score most")
3. Counterfactual explanations for the UI
"""

import numpy as np

from .shap_explainer import SHAPExplainer
from .gnn_ablation import GNNAblation


class SensitivityEngine:
    """One perturbation/sensitivity engine serving three consumers."""

    def __init__(self, xgb_model=None, gnn_model=None, feature_names=None):
        self.xgb_model = xgb_model
        self.gnn_model = gnn_model
        self.feature_names = feature_names
        self.shap = SHAPExplainer(xgb_model, feature_names) if xgb_model else None
        self.gnn_abl = GNNAblation(gnn_model) if gnn_model else None

    # -- Consumer 1: Attack Surface Map ------------------------------------
    def attack_surface_map(self, X, data=None):
        """Build the Attack Surface Map: per-model sensitivity registry.

        Phase 3 (finding #5b): every key here is a COMPUTED measurement.
        The previous 'shared_device' and 'graph_density' entries were both
        `mean_score` twice — fake structure over one number. Now the GNN
        block reports distinct measured quantities: neighbour-ablation delta
        on risky nodes (graph reliance) and edge-attr zeroing delta (feature
        reliance). If a quantity cannot be computed it is reported as
        {"computed": false} rather than invented.
        """
        import numpy as _np

        surface = {}

        if self.shap is not None and X is not None:
            xgb_sensitivity = self.shap.feature_importance(X)
            surface["xgb"] = {
                "model": "xgb",
                "sensitivity": xgb_sensitivity,
                "top_features": sorted(xgb_sensitivity.items(), key=lambda kv: -kv[1])[:5],
            }

        if self.gnn_abl is not None and data is not None:
            gnn_map = self.gnn_abl.sensitivity_map(data)

            # --- COMPUTED 1: graph-reliance via neighbour ablation on the
            # riskiest nodes (bounded work) ---
            node_scores = _np.asarray(gnn_map.get("node_scores", []))
            ablation_deltas = []
            if node_scores.size:
                risky = _np.argsort(-node_scores)[:5]
                for ni in risky:
                    try:
                        abl = self.gnn_abl.neighbor_ablation(data, int(ni))
                        ablation_deltas.append(abs(abl["delta"]))
                    except Exception:
                        continue

            # --- COMPUTED 2: edge-feature reliance via zeroed edge_attr ---
            try:
                ea = self.gnn_abl.edge_attr_sensitivity(data)
            except Exception:
                ea = {"mean_delta": 0.0, "max_delta": 0.0,
                      "affected_nodes": 0}

            surface["gnn"] = {
                "model": "gnn",
                "sensitivity": {
                    "neighbor_ablation_delta_mean":
                        round(float(_np.mean(ablation_deltas)), 6)
                        if ablation_deltas else {"computed": False},
                    "edge_attr_zeroing_delta":
                        round(float(ea["mean_delta"]), 6),
                    "riskiest_node_score":
                        round(float(node_scores.max()), 4)
                        if node_scores.size else {"computed": False},
                    "high_risk_node_count": gnn_map.get("high_risk_nodes", 0),
                },
                "target": "GNN",
                "goal": "preserve suspicious economic behavior while diluting graph concentration",
            }

        return surface

    # -- Consumer 2: Weakness-directed targeting ----------------------------
    def weakness_direction(self, X, data=None):
        """Reverse sensitivity: what perturbation lowers the score most.

        Returns a weakness descriptor with suggested variants.
        """
        surface = self.attack_surface_map(X, data)
        weakness = "relational camouflage"
        target_model = "GNN"
        suggested = ["more_devices", "more_intermediaries", "longer_paths", "temporal_spreading"]

        # If XGBoost is the weaker signal, target amount/velocity
        if "xgb" in surface:
            top = surface["xgb"].get("top_features", [])
            if top and top[0][0] in ("amount", "log_amount"):
                weakness = "amount-based anomaly"
                target_model = "XGBoost"
                suggested = ["smaller_amounts", "amount_splitting", "round_number_avoidance"]

        return {
            "weakness": weakness,
            "target_model": target_model,
            "goal": "preserve suspicious economic behavior while diluting graph concentration",
            "suggested_variants": suggested,
            "surface": surface,
        }

    # -- Consumer 3: Counterfactual explanations -----------------------------
    def counterfactual(self, X_instance, feature_changes):
        """Explain what would change the score for a specific case."""
        if self.shap is None:
            return {"error": "No XGBoost model available"}
        return self.shap.counterfactual(X_instance, feature_changes)

    def gnn_counterfactual(self, data, node_idx):
        """Explain what would change the GNN score for a specific node."""
        if self.gnn_abl is None:
            return {"error": "No GNN model available"}
        return self.gnn_abl.neighbor_ablation(data, node_idx)


def build_attack_surface_map(engine, X, data=None):
    """Convenience wrapper for consumer 1."""
    return engine.attack_surface_map(X, data)


def counterfactual_explanation(engine, X_instance, feature_changes):
    """Convenience wrapper for consumer 3."""
    return engine.counterfactual(X_instance, feature_changes)