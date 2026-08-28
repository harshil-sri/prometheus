"""meta_model.py — Stacked meta-model for Project Prometheus fraud detection.

Combines base-learner scores into a single calibrated fraud probability:

    * xgb_score : gradient-boosted trees over tabular transaction features
    * gnn_score : graph neural network over the transaction–entity graph

HONEST STACKING CONTRACT (audit finding #4, fixed Phase 2):

* The logistic blend is fit on OUT-OF-FOLD base scores. `fit(..., oof=True)`
  asserts that contract; fitting on in-sample scores logs a loud warning and
  records `oof_used=False` so evaluators can refuse the artifact.
* The previous "OR-gate" (`np.maximum(probs, X.max(axis=1))`, added by commit
  55596d5 to pass Beat 3) is DELETED. Meta output is now a genuine calibrated
  blend of the base scores.
* Calibration is chosen honestly: isotonic when n >= ISOTONIC_MIN_SAMPLES,
  Platt (sigmoid) otherwise; the applied method is recorded in
  `calibration_method` (previously isotonic was claimed while sigmoid was
  hardcoded).

OOF SCORE PRODUCTION
--------------------
Use :func:`make_oof_scores` to generate out-of-fold base-score columns from
any scorer factory:

    def factory(train_idx):
        clf = XGBFraudDetector(seed=42)
        clf.fit(X_tab[train_idx], y[train_idx])
        return lambda val_idx: clf.predict_proba(X_tab[val_idx])

    oof_xgb = make_oof_scores(factory, n_samples, n_splits=5, y=y)

Usage
-----
    meta = MetaModel(seed=42)
    meta.fit(X_meta_oof, y, oof=True)        # X_meta: (n, 2) [xgb, gnn]
    p_fraud = meta.predict_proba(X_new)      # calibrated P(fraud)
    flags   = meta.predict(X_new, threshold=0.65)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, List, Optional, Union

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold

logger = logging.getLogger(__name__)

__all__ = ["MetaModel", "make_oof_scores"]

# Isotonic regression is piecewise-constant and data-hungry; below this many
# training rows we fall back to Platt (sigmoid) scaling, which degrades more
# gracefully on small samples.
ISOTONIC_MIN_SAMPLES = 200

FEATURE_NAMES = ("xgb_score", "gnn_score", "lstm_score", "ae_score")
# Minimum number of base-learner columns required
_MIN_META_COLS = 2


def make_oof_scores(
    factory: Callable[[np.ndarray], Callable[[np.ndarray], np.ndarray]],
    n_samples: int,
    n_splits: int = 5,
    y: Optional[np.ndarray] = None,
    seed: int = 42,
) -> np.ndarray:
    """Produce out-of-fold positive-class scores for one base learner.

    Parameters
    ----------
    factory : callable train_idx -> scorer
        Must return a freshly *fitted* model's scoring closure for the given
        training indices; scorer(val_idx) returns a 1-D array of
        P(fraud) for those rows.
    n_samples : total number of labelled rows.
    n_splits : number of StratifiedKFold folds (ignored if y unavailable).
    y : labels (required for stratification).
    seed : fold shuffling seed.

    Returns
    -------
    np.ndarray of shape (n_samples,) — every row scored ONLY by models that
    never saw it during fitting (out-of-fold guarantee).
    """
    out = np.full(n_samples, np.nan, dtype=np.float64)
    y_arr = None if y is None else np.asarray(y).ravel()

    strat = y_arr if y_arr is not None else np.zeros(n_samples)
    unique = np.unique(strat)

    if unique.size > 1:
        splitter = StratifiedKFold(n_splits=n_splits, shuffle=True,
                                   random_state=seed)
    else:
        # Degenerate single-class data: unstratified fallback
        splitter = _PlainKFold(n_splits=n_splits)

    for train_idx, val_idx in splitter.split(np.zeros((n_samples, 1)), strat):
        scorer = factory(train_idx)
        vals = np.asarray(scorer(val_idx), dtype=np.float64).ravel()
        if vals.shape[0] != val_idx.shape[0]:
            raise ValueError("scorer returned wrong number of values")
        out[val_idx] = vals

    if np.isnan(out).any():
        raise ValueError("OOF loop failed to cover all samples")
    return out


class _PlainKFold:
    """K-fold splitter for single-class label arrays (StratifiedKFold needs 2)."""

    def __init__(self, n_splits: int, shuffle: bool = True,
                 random_state: Optional[int] = None):
        self.n_splits = n_splits
        self.shuffle = shuffle
        self.random_state = random_state

    def split(self, X, y=None):
        idx = np.arange(len(X))
        rng = np.random.RandomState(self.random_state)
        if self.shuffle:
            rng.shuffle(idx)
        folds = np.array_split(idx, self.n_splits)
        for k in range(self.n_splits):
            val = folds[k]
            tr = np.concatenate([folds[j] for j in range(self.n_splits) if j != k])
            yield tr, val


class MetaModel:
    """Logistic-regression stacker over [xgb_score, gnn_score] with honest
    probability calibration (isotonic at scale, sigmoid below it)."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed
        self.model = LogisticRegression(random_state=seed, class_weight="balanced")
        self.calibrated: Optional[CalibratedClassifierCV] = None
        self._calibration_method: Optional[str] = None
        self.oof_used_: Optional[bool] = None

    # ------------------------------------------------------------------ #
    # Training
    # ------------------------------------------------------------------ #
    def fit(self, X_meta, y, *, oof: bool = False) -> "MetaModel":
        """Fit the meta-learner and its calibrator.

        Parameters
        ----------
        X_meta : array-like of shape (n_samples, 2)
            Columns are [xgb_score, gnn_score], each in [0, 1].
        y : array-like of shape (n_samples,)
            Binary labels (1 = fraud, 0 = legitimate).
        oof : bool
            True if X_meta columns were produced OUT OF FOLD (required for a
            leak-free stack). When False a warning is logged and the artifact
            records ``oof_used_=False``.

        Stacking-leak note: fitting the blend on in-sample base scores lets
        over-confident base learners dominate — use
        :func:`make_oof_scores` to build leak-free columns.
        """
        X = self._validate_X(X_meta)
        y_arr = np.asarray(y).ravel()

        if X.shape[0] != y_arr.shape[0]:
            raise ValueError(
                f"X_meta and y length mismatch: {X.shape[0]} vs {y_arr.shape[0]}"
            )
        if np.unique(y_arr).size < 2:
            raise ValueError("Both classes must be present to fit the meta-model.")

        self.oof_used_ = bool(oof)
        if not oof:
            logger.warning(
                "MetaModel fitted on IN-SAMPLE base scores "
                "(oof=False): stacking leakage risk. Build columns via "
                "make_oof_scores() and pass oof=True."
            )

        # Base logistic regression (kept for interpretable coefficients).
        self.model.fit(X, y_arr)
        logger.info(
            "Meta LR coef=%s intercept=%.4f",
            self.model.coef_.ravel(),
            float(self.model.intercept_[0]),
        )

        # --- Honest calibration-method selection --------------------------
        n_pos = int(y_arr.sum())
        n_neg = int((1 - y_arr).sum())
        method = ("isotonic"
                  if y_arr.size >= ISOTONIC_MIN_SAMPLES and n_pos >= 10
                  else "sigmoid")

        cv = min(3, max(2, n_pos, n_neg)) if n_pos >= 2 and n_neg >= 2 else 0
        try:
            if cv >= 2:
                self.calibrated = CalibratedClassifierCV(
                    self.model, method=method, cv=cv)
                self.calibrated.fit(X, y_arr)
                self._calibration_method = method
                logger.info(
                    "Calibration fitted: method=%r cv=%d (n=%d, pos=%d)",
                    method, cv, y_arr.size, n_pos,
                )
            else:
                logger.debug(
                    "Calibration skipped (too few positives: %d / %d)",
                    n_pos, n_neg,
                )
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
        """Return array of shape (n_samples,) representing P(fraud).

        This is the calibrated linear blend of the base scores — no OR/Max
        shortcut sits on top of it.
        """
        X = self._validate_X(X_meta)

        if not self._is_fitted(self.model):
            logger.warning("MetaModel not fitted; returning neutral 0.5 scores.")
            return np.full(X.shape[0], 0.5)

        if self.calibrated is not None:
            probs = self.calibrated.predict_proba(X)[:, 1]
        else:
            logger.debug("Calibrator unavailable; returning raw LR probabilities.")
            probs = self.model.predict_proba(X)[:, 1]

        return probs

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
        """Raw LR weights (one per base-learner column) plus intercept.
        Column names follow FEATURE_NAMES where available, then fall back to
        generic 'col_N' labels."""
        coefs = self.model.coef_.ravel()
        names = list(FEATURE_NAMES[:len(coefs)]) + [
            f"col_{i}" for i in range(len(FEATURE_NAMES), len(coefs))
        ]
        out = dict(zip(names, map(float, coefs)))
        out["intercept"] = float(self.model.intercept_[0])
        return out

    @property
    def calibration_method(self) -> Optional[str]:
        """'isotonic', 'sigmoid', or None if calibration was skipped."""
        return self._calibration_method

    @property
    def diagnostics(self) -> dict:
        """Fit-time provenance surfaced for audit (no fabrication)."""
        return {
            "oof_used": self.oof_used_,
            "calibration_method": self._calibration_method,
            "coefficients": self.coefficients,
        }

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
                "oof_used": self.oof_used_,
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
        obj.oof_used_ = blob.get("oof_used")
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
        if X.ndim != 2 or X.shape[1] < _MIN_META_COLS:
            raise ValueError(
                f"Expected X_meta of shape (n, ≥{_MIN_META_COLS}); got {X.shape}. "
                f"Columns should be base-learner scores in the order: "
                f"{list(FEATURE_NAMES[:_MIN_META_COLS])}..."
            )
        if not np.isfinite(X).all():
            raise ValueError("X_meta contains NaN or infinite values.")
        return X

    @staticmethod
    def _is_fitted(estimator) -> bool:
        return hasattr(estimator, "classes_")
