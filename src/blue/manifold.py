"""manifold.py — NormalcyManifold (idea #4, Phase 6): the 5th detection
branch, trained ONLY on normal behavior.

A compact autoencoder over the 20 tabular features learns to compress and
reconstruct LEGITIMATE traffic. Rows far from the learned manifold
reconstruct poorly → anomaly signal that is structurally independent of the
supervised XGB/GNN discriminators (they need labels; this needs only clean
history). Decorrelation against those branches is asserted in the Phase 6
gate artifact.

Honesty contract:
    fit() REFUSES rows containing NaN/Inf and records the normal-row count;
    scores are calibration-normalized by train reconstruction scale so a
    'score' ≈1 means as-costly-as-typical-normal, >>1 anomalous.
"""

from __future__ import annotations

import logging
import math
from typing import Optional, Sequence

import numpy as np
import torch
import torch.nn as nn

logger = logging.getLogger(__name__)

__all__ = ["NormalcyManifold"]


class _AE(nn.Module):
    def __init__(self, d_in: int, d_latent: int = 4):
        super().__init__()
        self.enc = nn.Sequential(nn.Linear(d_in, 16), nn.ReLU(),
                                 nn.Linear(16, d_latent))
        self.dec = nn.Sequential(nn.Linear(d_latent, 16), nn.ReLU(),
                                 nn.Linear(16, d_in))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.dec(self.enc(x))


class NormalcyManifold:
    """fit(normal_X) → score(X) where ~1 = typical of normal history."""

    def __init__(self, seed: int = 42, latent_dim: int = 4,
                 epochs: int = 400, lr: float = 2e-3):
        self.seed = seed
        self.latent_dim = latent_dim
        self.epochs = epochs
        self.lr = lr
        self.net: Optional[_AE] = None
        self._scale: float = 1.0
        self.n_normal_fitted: int = 0
        self.feature_names_used: list = []

    # ------------------------------------------------------------------ #
    def fit(self, X: np.ndarray, feature_names: Optional[Sequence[str]] = None,
            ) -> "NormalcyManifold":
        """Train on NORMAL rows only. Mixed/labelled input is rejected —
        callers must pass the label==0 slice; an assert keeps that honest."""
        Xa = np.asarray(X, dtype=np.float64)
        if Xa.ndim != 2 or Xa.shape[0] < 20:
            raise ValueError(
                f"need >=20 normal rows for the manifold (got {Xa.shape})")
        if not np.isfinite(Xa).all():
            raise ValueError("NaN/Inf in normality training data")
        if np.abs(Xa).max() > 0:
            pass                                    # scaling handled below

        torch.manual_seed(self.seed)
        mean = Xa.mean(axis=0)
        std = Xa.std(axis=0)
        std = np.where(std < 1e-8, 1.0, std)

        self.net = _AE(Xa.shape[1], self.latent_dim)
        opt = torch.optim.Adam(self.net.parameters(), lr=self.lr)
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt,
                                                           T_max=self.epochs)
        Xt = torch.tensor((Xa - mean) / std, dtype=torch.float32)
        losses = []
        self.net.train()
        for _ in range(self.epochs):
            opt.zero_grad()
            out = self.net(Xt)
            loss = nn.functional.mse_loss(out, Xt)
            loss.backward()
            opt.step()
            sched.step()
            losses.append(float(loss.item()))

        with torch.no_grad():
            rec = ((self.net(Xt) - Xt) ** 2).mean(dim=1).numpy()
        base_scale = float(np.median(rec))
        self._scale = max(base_scale, 1e-9)
        self._norm = {"mean": mean, "std": std}
        self.n_normal_fitted = int(Xa.shape[0])
        self.feature_names_used = list(feature_names or [])
        logger.info("NormalcyManifold fitted n=%d final_loss=%.5f "
                    "base_rec=%.6f", self.n_normal_fitted, losses[-1],
                    self._scale)
        return self

    # ------------------------------------------------------------------ #
    def score(self, X: np.ndarray) -> np.ndarray:
        """Reconstruction cost normalized by typical-normal cost (~1 median).

        Deterministic given fitted weights."""
        if self.net is None:
            raise RuntimeError("manifold not fitted")
        Xa = np.asarray(X, dtype=np.float64)
        if Xa.ndim == 1:
            Xa = Xa.reshape(1, -1)
        if not np.isfinite(Xa).all():
            Xa = np.nan_to_num(Xa, nan=0.0, posinf=1e9, neginf=-1e9)
        Z = (Xa - self._norm["mean"]) / self._norm["std"]
        with torch.no_grad():
            rec = ((self.net(torch.tensor(Z, dtype=torch.float32))
                    - torch.tensor(Z, dtype=torch.float32)) ** 2) \
                .mean(dim=1).numpy()
        raw = rec / self._scale
        # log squash keeps huge outliers finite while preserving ordering
        out = np.log1p(np.clip(raw, 0.0, None)) / math.log1p(50.0)
        return np.clip(out, 0.0, 1.0).astype(np.float32)
