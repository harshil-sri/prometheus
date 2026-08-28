"""drift.py — Population-Stability Index across time windows (Phase 9).

PSI(expected, actual) with quantile binning on the REFERENCE window:
    PSI = Σ (a_i − e_i) · ln(a_i / e_i),  ε-smoothed zeros.

Verdict bands are the industry standard:
    < 0.10 stable | < 0.25 moderate shift | ≥ 0.25 significant shift

Two reports matter for this twin:
    * normal-only windows: organic drift the generator itself exhibits;
    * full windows: drift INCLUDING late-window attacks → separates
      attack-induced shift from baseline wobble. The gap between those two
      is the honest story.
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np

__all__ = ["psi", "drift_report", "DRIFT_BANDS"]

DRIFT_BANDS = {"stable": 0.10, "moderate": 0.25}
_EPS = 1e-4


def psi(expected: np.ndarray, actual: np.ndarray,
        bins: int = 10) -> float:
    """Quantile-binned PSI between two 1-D samples."""
    e = np.asarray(expected, dtype=np.float64).ravel()
    a = np.asarray(actual, dtype=np.float64).ravel()
    e = e[np.isfinite(e)]
    a = a[np.isfinite(a)]
    if e.size == 0 or a.size == 0:
        return float("nan")
    qs = np.quantile(e, np.linspace(0, 1, bins + 1)[1:-1])
    edges = np.concatenate([[-np.inf], qs, [np.inf]])
    eh = np.histogram(e, bins=edges)[0] / max(1, e.size)
    ah = np.histogram(a, bins=edges)[0] / max(1, a.size)
    eh = np.clip(eh, _EPS, None)
    ah = np.clip(ah, _EPS, None)
    return round(float(np.sum((ah - eh) * np.log(ah / eh))), 6)


def _verdict(v: float) -> str:
    if not np.isfinite(v):
        return "unknown"
    if v < DRIFT_BANDS["stable"]:
        return "stable"
    if v < DRIFT_BANDS["moderate"]:
        return "moderate"
    return "significant"


def drift_report(X_ref: np.ndarray, X_cur: np.ndarray,
                 feature_names: List[str],
                 top_k: int = 8) -> Dict[str, Any]:
    """Per-feature PSI ref→cur + ranked worst columns."""
    if X_ref.shape[1] != X_cur.shape[1]:
        raise ValueError("feature width mismatch between windows")
    per_col: Dict[str, Dict[str, Any]] = {}
    for j, name in enumerate(feature_names):
        v = psi(X_ref[:, j], X_cur[:, j])
        per_col[name] = {"psi": v, "verdict": _verdict(v)}

    counts = {"stable": 0, "moderate": 0, "significant": 0, "unknown": 0}
    for m in per_col.values():
        counts[m["verdict"]] += 1

    worst = sorted(per_col.items(),
                   key=lambda kv: -(kv[1]["psi"] if kv[1]["psi"] == kv[1]["psi"]
                                    else -1))[:top_k]
    return {
        "per_feature": per_col,
        "verdict_counts": counts,
        "max_psi": max((m["psi"] for m in per_col.values()),
                       default=float("nan")),
        "shifted_features_top": [
            {"column": k, **v} for k, v in worst[:top_k]],
    }
