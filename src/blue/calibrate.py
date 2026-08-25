"""Probability calibration utilities for Project Prometheus fraud detection.

Wraps scikit-learn's calibration estimators (Platt scaling / sigmoid and
isotonic regression) and provides diagnostic tools (reliability curves and
Expected Calibration Error) for verifying that predicted fraud probabilities
match observed fraud frequencies.
"""

import numpy as np
from sklearn.calibration import CalibratedClassifierCV, calibration_curve

__all__ = [
    "platt_calibrate",
    "isotonic_calibrate",
    "reliability_curve",
    "expected_calibration_error",
]


def _validate_pair(y_true, y_prob):
    """Coerce labels/probabilities to aligned 1-D float arrays."""
    y_true = np.asarray(y_true, dtype=float).ravel()
    y_prob = np.asarray(y_prob, dtype=float).ravel()
    if y_true.shape != y_prob.shape:
        raise ValueError(
            f"y_true and y_prob must have the same shape, got "
            f"{y_true.shape} and {y_prob.shape}."
        )
    if y_true.size == 0:
        raise ValueError("y_true and y_prob must contain at least one sample.")
    return y_true, y_prob


def platt_calibrate(clf, X_val, y_val):
    """Apply Platt scaling (sigmoid calibration) to a classifier.

    Parameters
    ----------
    clf : estimator
        Unfitted (or fitted) base classifier implementing ``predict_proba``.
        Because ``cv=3`` is used, clones of ``clf`` are refit internally on
        cross-validation folds of ``(X_val, y_val)``.
    X_val : array-like of shape (n_samples, n_features)
        Calibration feature matrix.
    y_val : array-like of shape (n_samples,)
        Binary target labels (0 = legitimate, 1 = fraud).

    Returns
    -------
    CalibratedClassifierCV
        Fitted calibrated classifier whose ``predict_proba`` outputs are
        sigmoid-calibrated probabilities.
    """
    calibrator = CalibratedClassifierCV(clf, method="sigmoid", cv=3)
    calibrator.fit(X_val, y_val)
    return calibrator


def isotonic_calibrate(clf, X_val, y_val):
    """Apply isotonic regression calibration to a classifier.

    Isotonic calibration is more flexible than Platt scaling but requires
    substantially more calibration data (>~1000 samples recommended);
    otherwise it risks overfitting.

    Parameters
    ----------
    clf : estimator
        Base classifier. Clones are refit on CV folds of ``(X_val, y_val)``
        because ``cv=3`` is used.
    X_val : array-like of shape (n_samples, n_features)
        Calibration feature matrix.
    y_val : array-like of shape (n_samples,)
        Binary target labels (0 = legitimate, 1 = fraud).

    Returns
    -------
    CalibratedClassifierCV
        Fitted calibrated classifier with isotonic probability mapping.
    """
    calibrator = CalibratedClassifierCV(clf, method="isotonic", cv=3)
    calibrator.fit(X_val, y_val)
    return calibrator


def reliability_curve(y_true, y_prob, n_bins=10):
    """Compute a reliability (calibration) curve.

    Partitions predicted probabilities into ``n_bins`` equal-width bins over
    [0, 1] and reports, per non-empty bin, the fraction of positives versus
    the mean predicted probability.

    Parameters
    ----------
    y_true : array-like of shape (n_samples,)
        Binary ground-truth labels.
    y_prob : array-like of shape (n_samples,)
        Predicted probabilities of the positive (fraud) class.
    n_bins : int, default=10
        Number of equal-width bins.

    Returns
    -------
    prob_true : ndarray
        Observed positive rate in each non-empty bin.
    prob_pred : ndarray
        Mean predicted probability in each non-empty bin.

    Notes
    -----
    Empty bins are dropped by scikit-learn, so the returned arrays may be
    shorter than ``n_bins``.
    """
    y_true, y_prob = _validate_pair(y_true, y_prob)
    prob_true, prob_pred = calibration_curve(
        y_true, y_prob, n_bins=n_bins, strategy="uniform"
    )
    return prob_true, prob_pred


def expected_calibration_error(y_true, y_prob, n_bins=10):
    """Compute Expected Calibration Error (ECE).

    ECE = sum_b (n_b / N) * |acc(b) - conf(b)|

    where ``acc(b)`` is the observed positive rate and ``conf(b)`` the mean
    predicted probability within bin ``b``. Lower is better; 0 indicates
    perfect calibration.

    Implemented with explicit binning rather than pairing against
    ``calibration_curve`` output, which can silently drop empty bins and
    cause shape mismatches.

    Parameters
    ----------
    y_true : array-like of shape (n_samples,)
        Binary ground-truth labels.
    y_prob : array-like of shape (n_samples,)
        Predicted probabilities of the positive (fraud) class.
    n_bins : int, default=10
        Number of equal-width bins over [0, 1].

    Returns
    -------
    float
        Expected calibration error in [0, 1].
    """
    y_true, y_prob = _validate_pair(y_true, y_prob)
    n_bins = int(n_bins)
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1, got {n_bins}.")

    y_prob = np.clip(y_prob, 0.0, 1.0)
    n_samples = y_true.size

    # Equal-width bin assignment; clip maps p=1.0 into the final bin.
    bin_indices = np.minimum((y_prob * n_bins).astype(int), n_bins - 1)

    counts = np.bincount(bin_indices, minlength=n_bins).astype(float)
    sum_confidence = np.bincount(bin_indices, weights=y_prob, minlength=n_bins)
    sum_accuracy = np.bincount(bin_indices, weights=y_true, minlength=n_bins)

    nonempty = counts > 0
    avg_confidence = sum_confidence[nonempty] / counts[nonempty]
    avg_accuracy = sum_accuracy[nonempty] / counts[nonempty]
    bin_weights = counts[nonempty] / n_samples

    return float(np.sum(bin_weights * np.abs(avg_accuracy - avg_confidence)))