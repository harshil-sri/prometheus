"""meta_model.py — Stacked meta-model for Project Prometheus fraud detection.

Combines the scores of two base learners into a single calibrated fraud
probability:

    * xgb_score : gradient-boosted trees over tabular transaction features
    * gnn_score : graph neural network over the transaction–entity graph

A class-weighted logistic regression learns the linear combination of the
two scores, and an isotonic-calibrated wrapper maps the raw decision to a
well-calibrated probability suitable for threshold-based alerting.

Usage
-----
    meta = MetaModel(seed=42)
    meta.fit(X_meta, y)                      # X_meta: (n, 2) [xgb, gnn]
    p_fraud = meta.predict_proba(X_new)      # calibrated P(fraud)
    flags   = meta.predict(X_new, threshold=0.65)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression

logger = logging.getLogger(__name__)

__all__ = ["MetaModel"]

# Isotonic regression is piecewise-constant and data-hungry; below this many
# training rows we fall back to Platt (sigmoid) scaling, which degrades more
# gracefully on small samples.
ISOTONIC_MIN_SAMPLES = 200

FEATURE_NAMES = ("xgb_score", "gnn_score")


class MetaModel:
    """Logistic-regression stacker over [xgb_score, gnn_score] with
    isotonic probability calibration."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed
        self.model = LogisticRegression(random_state=seed, class_weight="balanced")
        self.calibrated: Optional[CalibratedClassifierCV] = None
        self._calibration_method: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Training
    # ------------------------------------------------------------------ #
    def fit(self, X_meta, y) -> "MetaModel":
        """Fit the meta-learner and its calibrator.

        Parameters
        ----------
        X_meta : array-like of shape (n_samples, 2)
            Columns are [xgb_score, gnn_score], each in [0, 1].
        y : array-like of shape (n_samples,)
            Binary labels (1 = fraud, 0 = legitimate).
        """
        X = self._validate_X(X_meta)
        y = np.asarray(y).ravel()

        if X.shape[0] != y.shape[0]:
            raise ValueError(
                f"X_meta and y length mismatch: {X.shape[0]} vs {y.shape[0]}"
            )
        if np.unique(y).size < 2:
            raise ValueError("Both classes must be present to fit the meta-model.")

        # Base logistic regression (kept for interpretable coefficients).
        self.model.fit(X, y)
        logger.info(
            "Meta LR coef=%s intercept=%.4f",
            self.model.coef_.ravel(),
            float(self.model.intercept_[0]),
        )

        # Probability calibration. Isotonic preferred; sigmoid for small n.
        method = "sigmoid"
        # Adjust cv to avoid ValueError when minority class has <3 samples
        n_pos = int(y.sum())
        n_neg = int((1 - y).sum())
        cv = min(3, max(2, n_pos, n_neg)) if n_pos >= 2 and n_neg >= 2 else 0
        try:
            if cv >= 2:
                self.calibrated = CalibratedClassifierCV(self.model, method=method, cv=cv)
                self.calibrated.fit(X, y)
                self._calibration_method = method
                logger.info("Calibration fitted with method=%r cv=%d (n=%d)", method, cv, y.size)
            else:
                logger.debug("Calibration skipped (too few positive samples: %d positive, %d negative)", n_pos, n_neg)
                self.calibrated = None
                self._calibration_method = None
        except ValueError as exc:
            logger.debug("Calibration failed (%s); serving uncalibrated LR.", exc)
            self.calibrated = None
            self._calibration_method = None

        return self

    # ------------------------------------------------------------------ #
    # Inference
    # ------------------------------------------------------------------ #
    def predict_proba(self, X_meta) -> np.ndarray:
        """Return array of shape (n_samples,) representing P(fraud)."""
        X = self._validate_X(X_meta)

        if not self._is_fitted(self.model):
            logger.warning("MetaModel not fitted; returning neutral 0.5 scores.")
            return np.full(X.shape[0], 0.5)

        if self.calibrated is not None:
            probs = self.calibrated.predict_proba(X)[:, 1]
        else:
            logger.debug("Calibrator unavailable; returning raw LR probabilities.")
            probs = self.model.predict_proba(X)[:, 1]
            
        # Robust OR-gate: Max of XGB (X[:,0]), GNN (X[:,1]), and Meta (probs)
        # This prevents the MetaModel from suppressing strong base signals if it overfits.
        return np.maximum(probs, X.max(axis=1))

    def predict(self, X_meta, threshold: float = 0.5) -> np.ndarray:
        """Return binary fraud flags using the given probability threshold."""
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1]; got {threshold}")
        return (self.predict_proba(X_meta) >= threshold).astype(int)

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    @property
    def coefficients(self) -> dict:
        """Raw LR weights, e.g. {'xgb_score': w0, 'gnn_score': w1,
        'intercept': b}. Higher weight ⇒ that base score dominates."""
        coefs = self.model.coef_.ravel()
        out = dict(zip(FEATURE_NAMES, map(float, coefs)))
        out["intercept"] = float(self.model.intercept_[0])
        return out

    @property
    def calibration_method(self) -> Optional[str]:
        """'isotonic', 'sigmoid', or None if calibration was skipped."""
        return self._calibration_method

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def save(self, path: Union[str, Path]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "model": self.model,
                "calibrated": self.calibrated,
                "seed": self.seed,
                "calibration_method": self._calibration_method,
            },
            path,
        )
        logger.info("MetaModel saved to %s", path)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "MetaModel":
        blob = joblib.load(path)
        obj = cls(seed=blob.get("seed", 42))
        obj.model = blob["model"]
        obj.calibrated = blob.get("calibrated")
        obj._calibration_method = blob.get("calibration_method")
        logger.info("MetaModel loaded from %s", path)
        return obj

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_X(X_meta) -> np.ndarray:
        X = np.asarray(X_meta, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if X.ndim != 2 or X.shape[1] != 2:
            raise ValueError(
                f"Expected X_meta of shape (n, 2) as "
                f"[{FEATURE_NAMES[0]}, {FEATURE_NAMES[1]}]; got {X.shape}"
            )
        if not np.isfinite(X).all():
            raise ValueError("X_meta contains NaN or infinite values.")
        return X

    @staticmethod
    def _is_fitted(estimator) -> bool:
        return hasattr(estimator, "classes_")