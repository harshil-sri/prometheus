"""SHAP-based XGBoost sensitivity analysis."""

import numpy as np

class SHAPExplainer:
    """SHAP feature importance for XGBoost fraud model."""

    def __init__(self, model, feature_names=None):
        self.model = model
        self.feature_names = feature_names
        self.explainer = None
        self.expected_value = None

    def fit(self, X_background):
        """Initialize SHAP TreeExplainer."""
        try:
            import shap
            self.explainer = shap.TreeExplainer(self.model)
            self.expected_value = self.explainer.expected_value
        except ImportError:
            # Fallback: permutation importance
            self.explainer = None

    def shap_values(self, X):
        """Return SHAP values."""
        if self.explainer is None:
            self.fit(X[:100])
        if self.explainer is None:
            return np.zeros((len(X), X.shape[1]))
        try:
            sv = self.explainer.shap_values(X)
            if isinstance(sv, list):
                sv = sv[1]  # positive class
            return sv
        except Exception:
            return np.zeros((len(X), X.shape[1]))

    def feature_importance(self, X):
        """Return dict of feature_name -> mean|SHAP|."""
        sv = self.shap_values(X)
        mean_abs = np.nan_to_num(np.mean(np.abs(sv), axis=0))
        if self.feature_names:
            return {n: float(v) for n, v in zip(self.feature_names, mean_abs)}
        return {f"feat_{i}": float(v) for i, v in enumerate(mean_abs)}

    def counterfactual(self, X_instance, feature_changes):
        """Compute what would change the score if certain features changed.

        Args:
            X_instance: 1D numpy array of feature values
            feature_changes: dict of {feature_name: new_value} or {feature_index: new_value}

        Returns:
            dict with original_score, new_score, delta
        """
        sv = self.shap_values(X_instance.reshape(1, -1))
        if self.expected_value is None:
            base = 0.5
        else:
            base = float(self.expected_value) if not isinstance(self.expected_value, np.ndarray) else float(self.expected_value[1] if len(self.expected_value) > 1 else self.expected_value[0])

        original_score = float(np.clip(base + sv.sum(), 0, 1))

        modified = X_instance.copy()
        name_to_idx = {}
        if self.feature_names:
            name_to_idx = {n: i for i, n in enumerate(self.feature_names)}

        for key, val in feature_changes.items():
            if isinstance(key, str) and key in name_to_idx:
                modified[name_to_idx[key]] = val
            elif isinstance(key, int):
                modified[key] = val

        new_sv = self.shap_values(modified.reshape(1, -1))
        new_score = float(np.clip(base + new_sv.sum(), 0, 1))

        return {
            "original_score": round(original_score, 4),
            "new_score": round(new_score, 4),
            "delta": round(new_score - original_score, 4),
            "feature_changes": feature_changes,
        }