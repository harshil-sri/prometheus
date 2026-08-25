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