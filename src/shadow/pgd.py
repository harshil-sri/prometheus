"""pgd.py — Domain-projected gradient search for evasion (shadow-guided).

THE CORE IDEA (locked in PROMETHEUS_CONTEXT.md §6, 2026-08-27):

XGBoost trees are piecewise-constant → no usable gradients. We therefore run
PGD on the distilled MLP ("shadow net") and verify every candidate against
the TRUE victim ensemble afterwards. Approximate margins stay approximations.

THREAT MODEL — what an attacker may actually change on a NEW transaction:

  FREE fields      amount, time_since_last_tx, is_new_device,
                   device_account_count, ip_account_count, merchant_category,
                   hour_of_day, is_p2p, is_external, currency_code
  INTEGER domains  counts, category/currency codes, hour (wrap 0..23)
  CATEGORICAL      merchant_category ∈ [0..9], currency_code ∈ [0..5]
                   (index 6 = "unknown" fallback kept reachable)
  DERIVED/LOCKED   log_amount, amount_roundness, is_high_amount, is_night,
                   sender history stats (velocity, z-score, counts…)
                   → recomputed from the free fields each projection step.
                     An attacker cannot rewrite their ledger history; the
                     optimizer therefore never moves those columns directly.

The candidate rows this produces are thus REALIZABLE transactions, not
arbitrary points in R^20 — that realizability is what verification checks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np
import torch
import torch.nn as nn

logger = logging.getLogger(__name__)

__all__ = ["FEATURE_DOMAINS", "FeatureDomain", "ProjectedPGD",
           "PGDCandidate", "optimize_evasion", "project_batch",
           "recompute_derived"]

# ---------------------------------------------------------------------------
# Feature domain table (indices into blue.features.FEATURE_NAMES)
# ---------------------------------------------------------------------------

KIND_CONT = "continuous"
KIND_COUNT = "count"          # non-negative integer
KIND_BINARY = "binary"
KIND_CATEGORICAL = "categorical"
KIND_DERIVED = "derived"


@dataclass(frozen=True)
class FeatureDomain:
    name: str
    kind: str
    lo: float = 0.0
    hi: float = float("inf")
    categories: Optional[int] = None     # valid code count for categorical


def _build_domains(feature_names: Sequence[str]) -> dict:
    by_name = {n: i for i, n in enumerate(feature_names)}
    table = {
        "amount":                FeatureDomain("amount", KIND_CONT, 0.0, 200_000.0),
        "log_amount":            FeatureDomain("log_amount", KIND_DERIVED),
        "amount_roundness":      FeatureDomain("amount_roundness", KIND_DERIVED),
        "is_high_amount":        FeatureDomain("is_high_amount", KIND_DERIVED),
        "velocity_10":           FeatureDomain("velocity_10", KIND_DERIVED),
        "velocity_50":           FeatureDomain("velocity_50", KIND_DERIVED),
        "sender_tx_count":       FeatureDomain("sender_tx_count", KIND_DERIVED),
        "sender_avg_amount":     FeatureDomain("sender_avg_amount", KIND_DERIVED),
        "sender_amount_zscore":  FeatureDomain("sender_amount_zscore", KIND_DERIVED),
        "time_since_last_tx":    FeatureDomain("time_since_last_tx", KIND_COUNT, 0.0, 1000.0),
        "repeat_recipient_50":   FeatureDomain("repeat_recipient_50", KIND_DERIVED),
        "is_new_device":         FeatureDomain("is_new_device", KIND_BINARY),
        "device_account_count":  FeatureDomain("device_account_count", KIND_COUNT, 0.0, 500.0),
        "ip_account_count":      FeatureDomain("ip_account_count", KIND_COUNT, 0.0, 500.0),
        "merchant_category":     FeatureDomain("merchant_category", KIND_CATEGORICAL, 0.0, 9.0, categories=10),
        "hour_of_day":           FeatureDomain("hour_of_day", KIND_CATEGORICAL, 0.0, 23.0, categories=24),
        "is_night":              FeatureDomain("is_night", KIND_DERIVED),
        "is_p2p":                FeatureDomain("is_p2p", KIND_BINARY),
        "is_external":           FeatureDomain("is_external", KIND_BINARY),
        "currency_code":         FeatureDomain("currency_code", KIND_CATEGORICAL, 0.0, 5.0, categories=6),
    }
    missing = set(by_name) - set(table)
    if missing:
        raise ValueError(f"unmapped features in domain table: {missing}")
    return {by_name[n]: d for n, d in table.items()}


#: populated lazily via get_domains(feature_names) — index-keyed
_DOMAIN_CACHE: dict = {}


def get_domains(feature_names: Sequence[str]) -> dict:
    key = tuple(feature_names)
    if key not in _DOMAIN_CACHE:
        _DOMAIN_CACHE[key] = _build_domains(feature_names)
    return _DOMAIN_CACHE[key]


def free_indices(domains: dict) -> List[int]:
    return sorted(i for i, d in domains.items() if d.kind != KIND_DERIVED)


# ---------------------------------------------------------------------------
# Projection + derived recomputation
# ---------------------------------------------------------------------------

def recompute_derived(X: np.ndarray, domains: dict) -> np.ndarray:
    """Recompute all KIND_DERIVED columns from the current free fields.

    Only pairwise-consistent derivations are touched; columns whose base row
    data we do not control are left as-is (documented caveat)."""
    Xo = X.copy()
    idx = {d.name: i for i, d in domains.items()}
    amount = np.clip(X[:, idx["amount"]], 0.0, None)

    Xo[:, idx["log_amount"]] = np.log1p(amount)
    # roundness grid check mirrored from blue.features (₹≥1000 multiples)
    Xo[:, idx["amount_roundness"]] = (
        (amount >= 1000) & (np.abs(amount / 1000.0 - np.round(amount / 1000.0)) < 1e-9)
    ).astype(np.float64)
    Xo[:, idx["is_high_amount"]] = (amount > 50000.0).astype(np.float64)
    hour = X[:, idx["hour_of_day"]]
    Xo[:, idx["is_night"]] = ((hour >= 0) & (hour < 6)).astype(np.float64)
    return Xo


def project_batch(Xr: torch.Tensor, base: np.ndarray,
                  domains: dict, max_amount_delta_frac: float = 0.30,
                  ) -> torch.Tensor:
    """Project relaxed tensors back into the realizable attack domain.

    - LOCKED columns snap to the ORIGINAL row values (history can't change).
    - amount stays within ±max_amount_delta_frac of the original.
    - integer kinds round; binary snaps to {0,1}; categoricals round+clip to
      their valid code ranges; count floor at 0.
    """
    with torch.no_grad():
        X = Xr.clone().numpy()
        for i, dom in domains.items():
            col = X[:, i]
            if dom.kind == KIND_DERIVED:
                # history/analytics cannot be rewritten by an attacker:
                # freeze to the ORIGINAL row; true derivables are rebuilt
                # afterwards by recompute_derived()
                X[:, i] = base[:, i]
                continue
            b = base[:, i]
            if dom.name == "amount":
                span = np.maximum(1.0, np.abs(b)) * max_amount_delta_frac
                col = np.clip(col, b - span, b + span)
                col = np.maximum(col, 0.0)
                col = np.round(col, 2)                    # paise precision
            elif dom.kind == KIND_BINARY:
                col = np.where(col - 0.5 > 0, 1.0, 0.0)
                # keep flips sane: attacker may flip flags but anchored to a
                # plausible value either way (projection handles it)
            elif dom.kind == KIND_COUNT:
                col = np.clip(np.round(col), dom.lo, dom.hi)
            elif dom.kind == KIND_CATEGORICAL:
                col = np.clip(np.round(col), dom.lo, dom.hi)
                if dom.name == "hour_of_day":
                    col = np.mod(col, 24.0)
                col = np.round(col)
            else:                                          # continuous others
                col = np.clip(col, dom.lo, min(dom.hi, 1e7))
            X[:, i] = col

        X = recompute_derived(X, domains)
        return torch.tensor(X, dtype=torch.float32)


# ---------------------------------------------------------------------------
# PGD engine
# ---------------------------------------------------------------------------

@dataclass
class PGDCandidate:
    x_projected: np.ndarray               # full realized row (n_features,)
    shadow_score: float
    base_row_index: int
    restart: int
    iterations_used: int


class ProjectedPGD:
    """Minimize shadow score s(x) subject to the attack domain."""

    def __init__(self, shadow_net, domains: dict, seed: int = 42,
                 iterations: int = 40, alpha: float = 3.0,
                 restarts: int = 3):
        self.net = shadow_net
        self.domains = domains
        self.free_idx = free_indices(domains)
        self.iterations = iterations
        self.alpha = alpha
        self.restarts = restarts
        self.seed = seed
        rng = np.random.RandomState(seed)
        torch.manual_seed(seed)
        self._restart_noise = [
            rng.uniform(-1.0, 1.0, size=len(self.free_idx)).astype(np.float32)
            * (rng.rand() * 8 + 2)                      # varied kick strength
            for _ in range(max(1, restarts))
        ]

    def _shadow_score(self, Xf: torch.Tensor) -> torch.Tensor:
        return self.net(Xf)

    def optimize(self, X_base: np.ndarray,
                 threshold: float = 0.5,
                 max_amount_delta_frac: float = 0.30,
                 target_below: Optional[float] = None,
                 ) -> List[PGDCandidate]:
        """For each base row, search evasive variants; return best per row."""
        X_base64 = np.asarray(X_base, dtype=np.float64)
        base_t = torch.tensor(X_base64, dtype=torch.float32)
        gate = threshold if target_below is None else min(threshold, target_below)

        best_per_row: List[Optional[PGDCandidate]] = [None] * len(X_base64)

        for r, noise in enumerate(self._restart_noise):
            fi = torch.tensor(self.free_idx, dtype=torch.long)
            kick = torch.tensor(noise).unsqueeze(0).repeat(len(X_base64), 1)

            with torch.no_grad():
                # seeded kick on free columns only → distinct basins
                Xt = base_t.clone()
                Xt[:, fi] += kick

            # rows still being optimized this restart
            active = np.ones(len(X_base64), dtype=bool)
            # rows already under the gate: freeze their realized point now
            with torch.no_grad():
                proj_now = project_batch(Xt, X_base64, self.domains,
                                         max_amount_delta_frac)
            settled: List[Optional[np.ndarray]] = [None] * len(X_base64)
            with torch.no_grad():
                s0 = self.net(proj_now).numpy()
            settled = [proj_now[k].numpy() if s0[k] < gate else None
                       for k in range(len(X_base64))]
            active &= np.array([s is None for s in settled])

            iters_used = 0
            for _ in range(self.iterations):
                if not active.any():
                    break
                iters_used += 1

                Xt.requires_grad_(True)
                scores = self.net(Xt)
                scores.sum().backward()
                grad = Xt.grad.detach()

                with torch.no_grad():
                    upd = project_batch(Xt - self.alpha * grad, X_base64,
                                        self.domains, max_amount_delta_frac)
                    cur = self.net(upd).detach().numpy()

                    # newly-settled rows are recorded and leave the set
                    newly = active & (cur < gate)
                    for k in np.where(newly)[0]:
                        settled[k] = upd[k].numpy()
                    active &= ~newly

                    # still-active rows keep moving from the projection
                    stay = torch.tensor(active).unsqueeze(1)
                    Xt = torch.where(stay, upd, Xt)
                Xt.grad = None

            # finalize restart: whatever remains unsolved records its LAST
            # projected state as-is (best-effort, verified later anyway)
            with torch.no_grad():
                last_scores = self.net(Xt).numpy()
            for k in range(len(X_base64)):
                x_final = settled[k] if settled[k] is not None \
                    else Xt[k].numpy()
                cand = PGDCandidate(
                    x_projected=np.asarray(x_final, dtype=np.float64),
                    shadow_score=float(last_scores[k]) if settled[k] is None
                    else 0.0,                     # settled ⇒ strictly < gate
                    base_row_index=k,
                    restart=r,
                    iterations_used=iters_used,
                )
                if best_per_row[k] is None or \
                        cand.shadow_score < best_per_row[k].shadow_score:
                    best_per_row[k] = cand

        return [c for c in best_per_row if c is not None]


def optimize_evasion(shadow_net, feature_names: Sequence[str],
                     X_base: np.ndarray, threshold: float = 0.5,
                     **kwargs) -> List[PGDCandidate]:
    """Convenience wrapper wiring the standard domain table."""
    domains = get_domains(feature_names)
    pgd = ProjectedPGD(shadow_net, domains, **kwargs)
    return pgd.optimize(X_base, threshold=threshold)
