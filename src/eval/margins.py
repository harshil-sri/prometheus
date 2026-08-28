"""margins.py — Empirical decision-margin distribution (Phase 9).

The decision margin of an attacked transaction is
    margin = alert_threshold − victim_score(x_adversarial)
Positive margins ⇒ evaded; larger positive ⇒ further from being caught.

HONESTY FRAME (law: estimates only): these are EMPIRICAL margins sampled by
running shadow-PGD candidate sets through the TRUE victim ensemble. They
characterize observed attack behavior on this twin; they are not formal
verification and the word c*rtified is runtime-banned in output.

Consumes the verify payload shape produced by shadow.verify.Verifier:
    per_candidate[{victim_base_score, victim_candidate_score, outcome}, ...]
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

import numpy as np

__all__ = ["margin_distribution", "BANNED_WORD"]

BANNED_WORD = "certified"


def margin_distribution(
    per_candidate: Sequence[Dict[str, Any]],
    threshold: float = 0.5,
    bins: int = 10,
) -> Dict[str, Any]:
    """Stats + histogram over (threshold − adversarial score).

    `per_candidate` items need at least 'victim_candidate_score'; base scores
    are optional context but included when present.
    """
    if not per_candidate:
        raise ValueError("no candidates supplied")

    adv_scores = np.array([float(pc["victim_candidate_score"])
                           for pc in per_candidate], dtype=np.float64)
    has_base = all("victim_base_score" in pc for pc in per_candidate)
    base_scores = np.array([float(pc.get("victim_base_score", float("nan")))
                            for pc in per_candidate]) if has_base else None

    margins = threshold - adv_scores                      # signed margins

    hist, edges = np.histogram(margins, bins=bins,
                               range=(float(min(-0.5, margins.min())),
                                      float(max(0.75, margins.max()))))
    frac_evasive = float((margins > 0).mean())
    tight = margins[(margins <= 0) & (margins > -0.05)]
    frac_nearly_caught = float(len(tight) / len(margins)) if len(margins) else 0.0

    def q(qv):
        return round(float(np.quantile(margins, qv)), 4)

    result = {
        "schema": "prometheus.margins.v1",
        "n_candidates": int(len(margins)),
        "threshold": threshold,
        "sign_split": {
            "evasive": int((margins > 0).sum()),
            "caught": int((margins <= 0).sum()),
            "frac_evasive": round(frac_evasive, 4),
        },
        "stats": {
            "min": q(0.0), "p05": q(0.05), "median": q(0.50),
            "mean": round(float(np.mean(margins)), 4), "p95": q(0.95),
            "max": q(1.0),
        },
        "near_boundary": {
            "within_0.05_of_threshold": round(frac_nearly_caught, 4),
            "within_0.10_above_threshold":
                round(float(((margins > 0) & (margins < 0.10)).mean()), 4),
        },
        "histogram": {
            "counts": [int(c) for c in hist],
            "bin_edges": [round(float(e), 4) for e in edges],
        },
        "score_drop_mean": (
            round(float(np.mean(base_scores - adv_scores)), 4)
            if base_scores is not None else None),
        "vocabulary_law": {
            "label": "empirical estimate",
            "note": ("empirical margins from PGD candidate replay against "
                     "the true ensemble; NOT a verification guarantee"),
        },
    }

    blob = json.dumps(result)
    assert BANNED_WORD not in blob.lower(), "margin vocabulary law violated"
    return result


def margins_from_verify_payload(verify_dict: Dict[str, Any],
                                threshold: float = 0.5,
                                ) -> Dict[str, Any]:
    """Adapter for VerifyReport.to_dict() output."""
    return margin_distribution(verify_dict.get("per_candidate", []),
                               threshold=threshold)
