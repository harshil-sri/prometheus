"""verify.py — Reality check for shadow-found candidates.

Every candidate PGD proposes against the SHADOW net is scored by the TRUE
victim ensemble (BlueTeamEnsemble). Outcomes:

  confirmed_evasion : shadow said evasive, victim agrees
  false_hope        : shadow fooled itself, victim still catches
  not_evasive       : shadow already knew it failed

Margins: threshold − best true score, reported as margin_estimate. The word
"certified" is banned from this module's output vocabulary — approximate
margins on a distilled surrogate are estimates by construction (law §2/9).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

__all__ = ["Verifier", "VerifyReport"]


@dataclass
class VerifyReport:
    n_candidates: int
    n_confirmed: int
    n_false_hope: int
    n_not_evasive: int
    evasion_rate: float                # confirmed / (confirmed+false_hope)
    mean_true_score_drop: float        # victim(base) - victim(candidate)
    margin_estimate_min: Optional[float]
    margin_estimate_mean: Optional[float]
    per_candidate: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n_candidates": self.n_candidates,
            "n_confirmed": self.n_confirmed,
            "n_false_hope": self.n_false_hope,
            "n_not_evasive": self.n_not_evasive,
            "evasion_rate": round(self.evasion_rate, 4),
            "mean_true_score_drop": round(self.mean_true_score_drop, 4),
            "margin_estimate_min": None if self.margin_estimate_min is None
            else round(self.margin_estimate_min, 4),
            "margin_estimate_mean": None if self.margin_estimate_mean is None
            else round(self.margin_estimate_mean, 4),
            "note": ("margins are ESTIMATES derived through a distilled "
                     "surrogate; approximate by construction and never "
                     "treated as guarantees"),
            "per_candidate": self.per_candidate,
        }


class Verifier:
    """Scores candidate rows with the true ensemble's feature path."""

    def __init__(self, victim_ensemble,
                 tx_stub_factory: Optional[Callable[[int], dict]] = None):
        """
        Args:
            victim_ensemble: BlueTeamEnsemble (or anything exposing
                predict_proba_features(X_tab, transactions)).
            tx_stub_factory: k -> minimal tx dict whose 'from' selects the
                graph node for the GNN column; defaults to index-based ids.
        """
        self.victim = victim_ensemble
        self.tx_stub_factory = tx_stub_factory or self._default_stub

    @staticmethod
    def _default_stub(k: int) -> dict:
        return {"tx_id": f"CAND_{k}", "step": 0,
                "from": "ACC_00001", "to": "", "amount": 0.0}

    def verify(self, candidates: Sequence["PGDCandidate"],
               X_base: np.ndarray, threshold: float = 0.5) -> VerifyReport:
        if not candidates:
            return VerifyReport(0, 0, 0, 0, 0.0, 0.0, None, None)

        idx_of = [c.base_row_index for c in candidates]
        X_base_sel = np.asarray(X_base, dtype=np.float64)[idx_of]

        def true_scores(rows: np.ndarray) -> np.ndarray:
            stubs = [self.tx_stub_factory(k) for k in range(len(rows))]
            return np.asarray(
                self.victim.predict_proba_features(rows, stubs), dtype=np.float64)

        base_scores = true_scores(X_base_sel)
        cand_rows = np.vstack([c.x_projected[None, :] for c in candidates])
        cand_scores = true_scores(cand_rows)

        confirmed = false_hope = not_evasive = 0
        margins: List[float] = []
        drops: List[float] = []
        per_cand: List[dict] = []

        for j, c in enumerate(candidates):
            b = float(base_scores[j])
            t = float(cand_scores[j])
            drop = b - t
            drops.append(drop)

            shadow_evaded = c.shadow_score < threshold
            true_evaded = t < threshold

            if shadow_evaded:
                if true_evaded:
                    outcome = "confirmed_evasion"
                    confirmed += 1
                    margins.append(threshold - t)
                else:
                    outcome = "false_hope"
                    false_hope += 1
            else:
                outcome = "not_evasive"
                not_evasive += 1

            per_cand.append({
                "base_row_index": c.base_row_index,
                "restart": c.restart,
                "shadow_score": round(float(c.shadow_score), 4),
                "victim_base_score": round(b, 4),
                "victim_candidate_score": round(t, 4),
                "true_score_drop": round(drop, 4),
                "outcome": outcome,
            })

        denom = confirmed + false_hope
        report = VerifyReport(
            n_candidates=len(candidates),
            n_confirmed=confirmed,
            n_false_hope=false_hope,
            n_not_evasive=not_evasive,
            evasion_rate=(confirmed / denom) if denom else 0.0,
            mean_true_score_drop=float(np.mean(drops)) if drops else 0.0,
            margin_estimate_min=float(min(margins)) if margins else None,
            margin_estimate_mean=float(np.mean(margins)) if margins else None,
            per_candidate=per_cand,
        )
        logger.info("verify: %s", {k: v for k, v in report.to_dict().items()
                                   if k != "per_candidate"})
        return report
