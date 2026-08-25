"""
xgb_model.py

XGBoost-based fraud detection model for Project Prometheus.
"""

import numpy as np
import xgboost as xgb
from sklearn.metrics import accuracy_score, roc_auc_score


class XGBFraudDetector:
    """XGBoost classifier wrapper for binary fraud detection."""

    def __init__(self, seed=42, n_estimators=200, max_depth=6, scale_pos_weight=None):
        self.model = None
        self.seed = seed
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.scale_pos_weight = scale_pos_weight
        self.feature_names = None
        self.params = {
            "objective": "binary:logistic",
            "eval_metric": "auc",
            "learning_rate": 0.1,
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "min_child_weight": 1,
            "gamma": 0.0,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_alpha": 0.1,
            "reg_lambda": 1.0,
            "random_state": seed,
            "n_jobs": -1,
            "verbosity": 0,
        }

    def _resolve_scale_pos_weight(self, y):
        """Use provided weight, or auto-compute from class counts."""
        if self.scale_pos_weight is not None:
            return float(self.scale_pos_weight)
        n_neg = int(np.sum(y == 0))
        n_pos = int(np.sum(y == 1))
        if n_pos == 0:
            return 1.0
        return n_neg / n_pos

    def fit(self, X, y, feature_names=None):
        """Train the XGBoost classifier and print training metrics."""
        X = np.asarray(X, dtype=np.float64)
        y = np.asarray(y).astype(int)

        params = dict(self.params)
        params["scale_pos_weight"] = self._resolve_scale_pos_weight(y)

        if feature_names is not None:
            self.feature_names = [str(f) for f in feature_names]
        elif hasattr(X, "columns"):
            self.feature_names = [str(c) for c in X.columns]

        self.model = xgb.XGBClassifier(**params)
        self.model.fit(X, y, eval_set=[(X, y)], verbose=False)

        # Training metrics
        proba = self.model.predict_proba(X)[:, 1]
        preds = (proba >= 0.5).astype(int)
        acc = accuracy_score(y, preds)
        try:
            auc = roc_auc_score(y, proba)
        except ValueError:
            auc = float("nan")

        print("[XGBFraudDetector] Training complete")
        print(f"  samples:          {len(y)}")
        print(f"  positives:        {int(np.sum(y == 1))}")
        print(f"  negatives:        {int(np.sum(y == 0))}")
        print(f"  scale_pos_weight: {params['scale_pos_weight']:.4f}")
        print(f"  train accuracy:   {acc:.4f}")
        print(f"  train AUC:        {auc:.4f}")

        return self

    def predict_proba(self, X):
        """Return probability of the positive (fraud) class."""
        if self.model is None:
            X = np.asarray(X)
            return np.full(len(X), 0.5)
        X = np.asarray(X, dtype=np.float64)
        return self.model.predict_proba(X)[:, 1]

    def predict(self, X, threshold=0.5):
        """Binary prediction using a decision threshold on positive-class probability."""
        proba = self.predict_proba(X)
        return (proba >= threshold).astype(int)

    def feature_importance(self):
        """Return dict mapping feature_name -> importance score."""
        if self.model is None:
            return {}
        scores = self.model.feature_importances_
        if self.feature_names is not None and len(self.feature_names) == len(scores):
            names = self.feature_names
        else:
            names = [f"f{i}" for i in range(len(scores))]
        return dict(zip(names, scores))