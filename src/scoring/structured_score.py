"""Structured Deep-Path Score.

R = w_t·T + w_g·G + w_b·B - w_u·U  mapped to 0-1000 Mastercard bands.

CRITICAL: This score is DEEP PATH ONLY. The fast path stays a pure ML probability.
"""

# Default weights (can be fit via constrained regression)
DEFAULT_WEIGHTS = {
    "w_t": 300.0,   # transaction evidence weight
    "w_g": 250.0,   # graph evidence weight
    "w_b": 200.0,   # behavioral evidence weight
    "w_u": 50.0,    # uncertainty penalty
}

# Mastercard convention bands
BANDS = [
    (0, 300, "APPROVE", "#22c55e"),
    (300, 700, "REVIEW", "#f59e0b"),
    (700, 1000, "DECLINE", "#ef4444"),
]

BAND_MAX = 1000.0


def compute_structured_score(
    transaction_evidence: float = 0.0,
    graph_evidence: float = 0.0,
    behavioral_evidence: float = 0.0,
    uncertainty: float = 0.0,
    weights: dict = None,
) -> dict:
    """Compute the structured deep-path score.

    All evidence values should be in [0, 1] range before weighting.

    Args:
        transaction_evidence: Evidence from transaction features (0-1)
        graph_evidence: Evidence from graph/relational features (0-1)
        behavioral_evidence: Evidence from behavioral sequence (0-1)
        uncertainty: Uncertainty penalty (0-1)
        weights: Dict with keys w_t, w_g, w_b, w_u

    Returns:
        dict with score, band, label, color
    """
    w = weights or DEFAULT_WEIGHTS

    # Raw score
    raw = (
        w["w_t"] * transaction_evidence
        + w["w_g"] * graph_evidence
        + w["w_b"] * behavioral_evidence
        - w["w_u"] * uncertainty
    )

    # Clamp to [0, BAND_MAX]
    score = max(0.0, min(BAND_MAX, raw))

    # Determine band
    label = "APPROVE"
    color = "#22c55e"
    for lo, hi, lbl, clr in BANDS:
        if lo <= score < hi:
            label = lbl
            color = clr
            break
    if score >= 700:
        label = "DECLINE"
        color = "#ef4444"

    return {
        "score": round(score, 1),
        "raw": round(raw, 1),
        "band": label,
        "color": color,
        "components": {
            "transaction_evidence": round(transaction_evidence, 4),
            "graph_evidence": round(graph_evidence, 4),
            "behavioral_evidence": round(behavioral_evidence, 4),
            "uncertainty": round(uncertainty, 4),
        },
        "weights": w,
    }


def score_from_ml_probs(xgb_prob: float, gnn_prob: float, meta_prob: float) -> dict:
    """Derive a structured score from ML probabilities.

    This is the DEEP-PATH converter. Takes calibrated ML probabilities and
    maps them to the structured score bands.

    Args:
        xgb_prob: XGBoost probability [0, 1]
        gnn_prob: GNN probability [0, 1]
        meta_prob: Meta-model blended probability [0, 1]

    Returns:
        structured score dict
    """
    # Transaction evidence from XGBoost
    t_evidence = xgb_prob
    # Graph evidence from GNN
    g_evidence = gnn_prob
    # Behavioral evidence from meta-model (blended)
    b_evidence = meta_prob
    # Uncertainty: disagreement between models (higher = more uncertain)
    uncertainty = abs(xgb_prob - gnn_prob)

    return compute_structured_score(
        transaction_evidence=t_evidence,
        graph_evidence=g_evidence,
        behavioral_evidence=b_evidence,
        uncertainty=uncertainty,
    )


def get_band(score: float) -> str:
    """Get the band label for a score."""
    if score < 300:
        return "APPROVE"
    elif score < 700:
        return "REVIEW"
    else:
        return "DECLINE"


def get_band_color(score: float) -> str:
    """Get the band color for a score."""
    if score < 300:
        return "#22c55e"
    elif score < 700:
        return "#f59e0b"
    else:
        return "#ef4444"


# ---------------------------------------------------------------------------
# Phase 8: FITTED structured score (no more hand-picked weights in prod)
# ---------------------------------------------------------------------------

import json
import os
from typing import List, Optional  # noqa: E402

SCORE_COLUMNS = ["xgb", "gnn", "meta", "manifold",
                 "spectral_cycle", "spectral_star"]
DEFAULT_WEIGHTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "artifacts", "structured_weights.json")


class FittedStructuredScore:
    """Logistic head over ensemble signal columns → calibrated P(fraud)
    → 0–1000 bands. Weights are FIT on training data and persisted, so the
    deep path's numbers have provenance instead of folklore constants.

    Reasons/counterfactuals are returned per prediction with exact input
    attribution (law 7 anti-fabrication: components shown ARE the inputs)."""

    def __init__(self):
        self.coef_: Optional[List[float]] = None
        self.intercept_: float = 0.0
        self.columns: List[str] = list(SCORE_COLUMNS)
        self.fit_meta: dict = {}

    # ------------------------------------------------------------------ #
    def fit(self, X_signals: "np.ndarray", y,
            feature_order: Optional[List[str]] = None) -> "FittedStructuredScore":
        import numpy as np
        from sklearn.linear_model import LogisticRegression

        order = list(feature_order or self.columns)
        Xa = np.asarray(X_signals, dtype=np.float64)
        if Xa.shape[1] != len(order):
            raise ValueError("signal matrix width mismatch")
        ya = np.asarray(y).ravel()
        if len(np.unique(ya)) < 2:
            raise ValueError("both classes required to fit the score")

        lr = LogisticRegression(C=1.0, class_weight="balanced",
                                random_state=42, max_iter=500)
        lr.fit(Xa, ya)
        self.coef_ = [float(c) for c in lr.coef_.ravel()]
        self.intercept_ = float(lr.intercept_[0])
        self.columns = order
        # store extra fit provenance (directional sanity numbers)
        p = lr.predict_proba(Xa)[:, 1]
        from sklearn.metrics import roc_auc_score
        self.fit_meta = {"n": int(Xa.shape[0]),
                         "pos": int(ya.sum()),
                         "fit_auc": round(float(roc_auc_score(ya, p)), 4)}
        return self

    # ------------------------------------------------------------------ #
    def predict_row(self, signals: dict) -> dict:
        """signals: column-name -> value (missing columns → error loudly)."""
        missing = [c for c in self.columns if c not in signals]
        if missing:
            raise KeyError(f"missing signal columns: {missing}")
        z = self.intercept_
        contribs = {}
        for c, w in zip(self.columns, self.coef_):
            v = float(signals[c])
            z += w * v
            contribs[c] = round(w * v, 4)
        import math
        p_fraud = 1.0 / (1.0 + math.exp(-z)) if z >= -35 else 0.0
        score = max(0.0, min(1000.0, p_fraud * 1000.0))
        label, color = None, None
        for lo, hi, lbl, clr in BANDS:
            if lo <= score < hi or (lbl == "DECLINE" and score >= hi):
                label, color = lbl, clr

        ranked = sorted(contribs.items(), key=lambda kv: -abs(kv[1]))
        top_reason = {"column": ranked[0][0],
                      "contribution": ranked[0][1]} if ranked else None
        # Counterfactual: Minimal feature delta to transition between decision bands
        current_band = label
        cf = None
        w_meta = self.coef_[self.columns.index("meta")] if "meta" in self.columns else 0.0

        def logit(q):
            q = min(max(q, 1e-6), 1 - 1e-6)
            return math.log(q / (1 - q))

        p_now = score / 1000.0
        if current_band == "DECLINE" and w_meta > 0:
            need = 699.0 / 1000.0
            dz = logit(need) - logit(p_now)
            cf = {
                "action": "reduce 'meta' probability by",
                "delta_needed": round(abs(dz / w_meta), 4),
                "to_reach": "REVIEW",
            }
        elif current_band == "REVIEW" and w_meta > 0:
            need = 299.0 / 1000.0
            dz = logit(need) - logit(p_now)
            cf = {
                "action": "reduce 'meta' probability by",
                "delta_needed": round(abs(dz / w_meta), 4),
                "to_reach": "APPROVE",
            }
        elif current_band == "APPROVE" and w_meta > 0:
            need = 300.0 / 1000.0
            dz = logit(need) - logit(p_now)
            cf = {
                "action": "raise 'meta' probability by",
                "delta_needed": round(abs(dz / w_meta), 4),
                "to_reach": "REVIEW",
            }
        return {
            "score": round(score, 1),
            "band": label,
            "color": color,
            "p_fraud": round(float(p_fraud), 4),
            "contribution_by_column": contribs,
            "top_reason_column": top_reason["column"] if top_reason else None,
            "top_reason_contribution": top_reason["contribution"]
            if top_reason else None,
            "counterfactual": cf,
            "weights_provenance": self.fit_meta,
            "columns": list(self.columns),
        }

    # ------------------------------------------------------------------ #
    def save(self, path: Optional[str] = None) -> str:
        path = path or DEFAULT_WEIGHTS_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"coef": self.coef_, "intercept": self.intercept_,
                       "columns": self.columns, "fit_meta": self.fit_meta},
                      f, indent=2)
        return path

    @classmethod
    def load(cls, path: Optional[str] = None) -> "FittedStructuredScore":
        path = path or DEFAULT_WEIGHTS_PATH
        obj = cls()
        with open(path) as f:
            blob = json.load(f)
        obj.coef_ = [float(x) for x in blob["coef"]]
        obj.intercept_ = float(blob["intercept"])
        obj.columns = list(blob["columns"])
        obj.fit_meta = dict(blob.get("fit_meta", {}))
        return obj

    @classmethod
    def load_or_none(cls, path: Optional[str] = None) -> Optional:
        try:
            return cls.load(path)
        except FileNotFoundError:
            return None