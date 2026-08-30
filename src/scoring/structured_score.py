"""Structured Deep-Path Score.

R = w_t·T + w_g·G + w_b·B + w_e·E + w_c·C - w_u·U
mapped to 0-1000 Mastercard bands.

CRITICAL: This score is DEEP PATH ONLY. The fast path stays a pure ML probability.

E (external evidence) is derived deterministically from sanctions screens
and OSINT dossier risk fields; C (campaign evidence) from repeat campaign
fingerprints in three-class memory (see scoring.evidence_mapping).
"""

# Default weights — actually fit and persisted via scripts/fit_weights.py
# (Phase 4 / implementation.md §2.2). The committed canonical artifact lives at
# src/artifacts/structured_weights.json (schema prometheus.structured_weights.v2);
# FittedStructuredScore.load() reads it at API init. The values below are the
# pre-Phase-4 hand-picked defaults, retained as a baseline reference and as a
# safe fallback if the artifact is ever missing.
DEFAULT_WEIGHTS = {
    "w_t": 300.0,   # transaction evidence weight
    "w_g": 250.0,   # graph evidence weight
    "w_b": 200.0,   # behavioral evidence weight
    "w_e": 120.0,   # external (sanctions/OSINT) evidence weight
    "w_c": 80.0,    # campaign evidence weight
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
    external_evidence: float = 0.0,
    campaign_evidence: float = 0.0,
) -> dict:
    """Compute the structured deep-path score.

    All evidence values should be in [0, 1] range before weighting.

    Args:
        transaction_evidence: Evidence from transaction features (0-1)
        graph_evidence: Evidence from graph/relational features (0-1)
        behavioral_evidence: Evidence from behavioral sequence (0-1)
        uncertainty: Uncertainty penalty (0-1)
        weights: Dict with keys w_t, w_g, w_b, w_e, w_c, w_u
        external_evidence: Deterministic sanctions/OSINT evidence (0-1)
        campaign_evidence: Repeat-campaign evidence from memory (0-1)

    Returns:
        dict with score, band, label, color
    """
    w = weights or DEFAULT_WEIGHTS

    # Raw score
    raw = (
        w["w_t"] * transaction_evidence
        + w["w_g"] * graph_evidence
        + w["w_b"] * behavioral_evidence
        + w["w_e"] * external_evidence
        + w["w_c"] * campaign_evidence
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
            "external_evidence": round(external_evidence, 4),
            "campaign_evidence": round(campaign_evidence, 4),
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
# Canonical weights path (Phase 4 reconciliation, updates.md 2.2): the fit
# lives in `src/artifacts/structured_weights.json` — NOT the repo-root
# `artifacts/` dir, which never existed for weights. v2 schema carries the
# fitted `w_*` (weighted formula) alongside the logistic coefs.
DEFAULT_WEIGHTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "artifacts", "structured_weights.json")

WEIGHTS_SCHEMA = "prometheus.structured_weights.v2"

# Evidence terms of the weighted formula R = w_t·T + w_g·G + w_b·B
# + w_e·E + w_c·C − w_u·U (order used by the fitter).
FORMULA_TERMS = ["w_t", "w_g", "w_b", "w_e", "w_c", "w_u"]


def _read_weights_file(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _validate_monotone(w: dict) -> None:
    """Fail loudly if any fitted formula weight is negative.

    The weighted formula must be monotone in every evidence term: raising a
    positive evidence term never lowers the raw score, and the uncertainty
    penalty term w_u must itself be non-negative (it only ever subtracts)."""
    for k in FORMULA_TERMS:
        v = float(w.get(k, 0.0))
        if v < 0.0:
            raise ValueError(
                f"monotone constraint violated by fitted formula: {k}={v} < 0. "
                f"Refuse to load an artifact that would make the deep score "
                f"non-monotone in its inputs.")


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
        # Fitted weighted-formula weights (Phase 4 / updates.md 2.2). None
        # until a v2 weights artifact (scripts/fit_weights.py) is loaded;
        # when present, predict_row uses its w_e/w_c instead of the hand-set
        # DEFAULT_WEIGHTS — the live formula stops being folklore constants.
        self.w_formula: Optional[dict] = None
        self.baseline_weights: Optional[dict] = None
        # Fit diagnostics (Phase 4): why a term may collapse to 0, the
        # calibration grid, reachability scale. Canonical blob only; session
        # refits merge-preserve it so the panel keeps its explanation.
        self.w_fit: Optional[dict] = None

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
    def predict_row(self, signals: dict,
                    external_evidence: float = 0.0,
                    campaign_evidence: float = 0.0) -> dict:
        """signals: column-name -> value (missing columns → error loudly).

        Combination rule (documented, law 7 anti-fabrication):
        the logistic head over the six ML signal columns supplies the ML prior
        (fills the T/G/B evidence slots); deterministic E (sanctions+OSINT)
        and C (repeat-campaign) evidence are added linearly with the shared
        DEFAULT_WEIGHTS and clamped to [0, 1000]. Both scalars are bounded,
        pure and deterministic — never invented or LLM-produced here.
        """
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

        w_e = float((self.w_formula or DEFAULT_WEIGHTS).get("w_e", 0.0))
        w_c = float((self.w_formula or DEFAULT_WEIGHTS).get("w_c", 0.0))
        e_ext = max(0.0, min(1.0, float(external_evidence)))
        e_camp = max(0.0, min(1.0, float(campaign_evidence)))
        score = max(0.0, min(1000.0, p_fraud * 1000.0
                             + w_e * e_ext + w_c * e_camp))
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
            "external_evidence": round(e_ext, 4),
            "campaign_evidence": round(e_camp, 4),
            "evidence_components": {
                "external_source": "sanctions+osint (deterministic mapping)",
                "campaign_source": "memory.attack_signatures recurrence",
                "w_e": round(w_e, 4),
                "w_c": round(w_c, 4),
            },
            "weights_provenance": self.fit_meta,
            "formula_weights": dict(self.w_formula or DEFAULT_WEIGHTS),
            "columns": list(self.columns),
        }

    # ------------------------------------------------------------------ #
    def weights_report(self) -> dict:
        """Fitted-vs-baseline weighted-formula weights + provenance.

        The weighted formula R = w_t·T + w_g·G + w_b·B + w_e·E + w_c·C − w_u·U
        is what the dashboard/API surface as the interpolatable deep-score
        view; this report is the Phase 4 "show the fit" payload."""
        fitted = self.w_formula or {}
        baseline = dict(self.baseline_weights or DEFAULT_WEIGHTS)
        delta = {k: round(float(fitted.get(k, 0.0)) - float(baseline.get(k, 0.0)), 4)
                 for k in FORMULA_TERMS}
        pos_sum = sum(float(v) for k, v in fitted.items() if k != "w_u")
        reach = round(pos_sum - float(fitted.get("w_u", 0.0)), 4)
        return {
            "schema": WEIGHTS_SCHEMA,
            "fitted": {k: round(float(fitted.get(k, 0.0)), 4)
                       for k in FORMULA_TERMS},
            "baseline": {k: round(float(baseline.get(k, 0.0)), 4)
                         for k in FORMULA_TERMS},
            "delta": delta,
            "monotone": (all(float(v) >= 0.0 for v in fitted.values())
                         if fitted else None),
            "band_reachability": {
                "max_raw": reach,
                "decline_reachable": reach >= 700.0,
            },
            "provenance": self.fit_meta,
        }

    # ------------------------------------------------------------------ #
    def save(self, path: Optional[str] = None) -> str:
        path = path or DEFAULT_WEIGHTS_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        prev = _read_weights_file(path)
        if prev is not None:
            wf = self.w_formula or prev.get("w_formula")
            bl = self.baseline_weights or prev.get("baseline_weights")
            wfit = self.w_fit or prev.get("w_fit")
        else:
            wf, bl = self.w_formula, self.baseline_weights
            wfit = self.w_fit
        blob = {
            "schema": WEIGHTS_SCHEMA,
            "formula": "R = w_t·T + w_g·G + w_b·B + w_e·E + w_c·C − w_u·U",
            "coef": self.coef_,
            "intercept": self.intercept_,
            "columns": self.columns,
            "fit_meta": self.fit_meta,
            "w_formula": wf,
            "baseline_weights": bl,
            "w_fit": wfit,
        }
        with open(path, "w") as f:
            json.dump(blob, f, indent=2, default=str)
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
        obj.w_formula = ({k: float(blob["w_formula"][k]) for k in FORMULA_TERMS}
                         if blob.get("w_formula") else None)
        obj.baseline_weights = dict(blob.get("baseline_weights") or {})
        obj.w_fit = blob.get("w_fit")
        if obj.w_formula:
            _validate_monotone(obj.w_formula)
        return obj

    @classmethod
    def load_or_none(cls, path: Optional[str] = None) -> Optional:
        try:
            return cls.load(path)
        except FileNotFoundError:
            return None