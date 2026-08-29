"""diffusion_tab.py — small TabDDPM-style Gaussian DDPM tabular critic (Phase 6).

A minimal, CPU-friendly continuous-feature DDPM used as a SECOND critique
generator alongside the SDV CTGAN exhibit in `scripts/fidelity_eval.py`.
Architecturally aligned with EmDT (Kuo & Motsch, arXiv:2603.13566): a noise-
predicting network with sinusoidal time embeddings denoising Gaussian diffusion
— but deliberately scaled down (single MLP denoiser, no per-cluster UMAP, no
transformer) because here a critique generator, not an oversampler, is needed.

Contract:
    * `fit(X)` standardizes internally (per-column mean/std stored) and trains
      a noise predictor with MSE on the DDPM auxiliary objective.
    * `sample(n)` runs the reverse process (T ancestral steps, DDPM schedule
      beta 1e-4→0.02, Ho et al.) in batches and returns denormalized rows.
    * Reproducible per seed on a single box: torch + numpy seeded, single
      thread, fixed generator — same inputs ⇒ identical synthetic rows.
    * Never invents a composite; the result is just plain rows for the same
      L1/L3 critics that judge CTGAN.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import torch
import torch.nn as nn

logger = logging.getLogger(__name__)

__all__ = ["TabDiffusionCritic", "DiffusionConfig"]


class DiffusionConfig:
    def __init__(self, T: int = 100, hidden: int = 256,
                 beta_min: float = 1e-4, beta_max: float = 0.02,
                 emb_dim: int = 64) -> None:
        self.T = T
        self.hidden = hidden
        self.beta_min = beta_min
        self.beta_max = beta_max
        self.emb_dim = emb_dim

    def to_dict(self) -> Dict[str, Any]:
        return {"T": self.T, "hidden": self.hidden,
                "beta_min": self.beta_min, "beta_max": self.beta_max,
                "emb_dim": self.emb_dim}


class _Denoiser(nn.Module):
    """Noise predictor: x + sinusoidal t-embedding → eps-hat (MLP, SiLU)."""

    def __init__(self, dim: int, hidden: int, emb_dim: int) -> None:
        super().__init__()
        self.t_proj = nn.Sequential(
            nn.Linear(emb_dim, hidden), nn.SiLU())
        self.net = nn.Sequential(
            nn.Linear(dim + hidden, hidden), nn.SiLU(),
            nn.Linear(hidden, hidden), nn.SiLU(),
            nn.Linear(hidden, dim))
        self._emb_dim = emb_dim

    def _time_embedding(self, t: torch.Tensor) -> torch.Tensor:
        # t in [0, T); sinusoidal (positional) embedding, no learned params
        half = self._emb_dim // 2
        freqs = torch.exp(-np.log(10000.0) * torch.arange(
            half, device=t.device, dtype=torch.float32) / half)
        args = t[:, None].float() * freqs[None, :]
        return torch.cat([torch.sin(args), torch.cos(args)], dim=-1)

    def forward(self, x: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
        te = self.t_proj(self._time_embedding(t))
        return self.net(torch.cat([x, te], dim=-1))


class TabDiffusionCritic:
    """Gaussian DDPM over standardized continuous features."""

    def __init__(self, dim: int, seed: int = 42,
                 config: Optional[DiffusionConfig] = None) -> None:
        if dim < 1:
            raise ValueError("diffusion critic needs at least 1 feature")
        self.dim = dim
        self.seed = seed
        self.config = config or DiffusionConfig()
        self._build_schedule()
        self.model: Optional[_Denoiser] = None
        self.mean_: Optional[np.ndarray] = None
        self.std_: Optional[np.ndarray] = None
        self.train_rows = 0
        self.loss_history: List[float] = []
        # Reproducibility: deterministic global RNG sequence + single thread.
        torch.set_num_threads(1)
        torch.manual_seed(seed)
        np.random.seed((seed + 7) % (2**31))

    # ---------------- diffusion schedule (Ho et al. linear betas) -------- #
    def _build_schedule(self) -> None:
        c = self.config
        self.betas = np.linspace(c.beta_min, c.beta_max, c.T,
                                 dtype=np.float64)
        self.alphas = 1.0 - self.betas
        self.alpha_bar = np.cumprod(self.alphas)
        self.sqrt_alpha_bar = np.sqrt(self.alpha_bar)
        self.sqrt_one_minus_alpha_bar = np.sqrt(1.0 - self.alpha_bar)
        # posterior variance factor per step (beta_t for the reverse process)
        self.post_sigma = np.sqrt(self.betas)

    def _torch_s(self, arr) -> torch.Tensor:
        if isinstance(arr, torch.Tensor):
            return arr
        return torch.from_numpy(np.asarray(arr, dtype=np.float32))

    # ---------------------------- training ------------------------------- #
    def fit(self, X: np.ndarray, epochs: int = 300, batch_size: int = 256,
            lr: float = 1e-3) -> "TabDiffusionCritic":
        """Train noise predictor on a standardized copy of X.

        Rows are sanitized to finite floats (features like velocities can carry
        Inf after engineering); standardization is fit on the training rows and
        stored so `sample()` denormalizes consistently."""
        X = np.asarray(X, dtype=np.float64)
        X = np.nan_to_num(X, nan=0.0, posinf=1e6, neginf=-1e6)
        assert X.ndim == 2 and X.shape[1] == self.dim, \
            f"expect ({-1}, {self.dim}) got {X.shape}"
        n = X.shape[0]
        self.mean_ = X.mean(axis=0)
        self.std_ = X.std(axis=0)
        self.std_[self.std_ < 1e-9] = 1.0
        Xs = (X - self.mean_) / self.std_
        self.train_rows = n

        self.model = _Denoiser(self.dim, self.config.hidden,
                               self.config.emb_dim)
        opt = torch.optim.Adam(self.model.parameters(), lr=lr)
        mse = nn.MSELoss()
        gen = torch.Generator().manual_seed(self.seed + 11)
        T = self.config.T

        self.model.train()
        for _ in range(epochs):
            perm = torch.randperm(n, generator=gen)
            epoch_loss = 0.0
            steps = 0
            for i in range(0, n, batch_size):
                idx = perm[i:i + batch_size]
                x0 = torch.from_numpy(Xs[idx].astype(np.float32))
                t = torch.randint(0, T, size=(len(idx),))
                noise = torch.randn_like(x0)
                ab = self._torch_s(self.sqrt_alpha_bar[t])
                ob = self._torch_s(self.sqrt_one_minus_alpha_bar[t])
                x_t = ab[:, None] * x0 + ob[:, None] * noise
                pred = self.model(x_t, t)
                loss = mse(pred, noise)
                opt.zero_grad()
                loss.backward()
                opt.step()
                epoch_loss += float(loss.detach())
                steps += 1
            self.loss_history.append(round(epoch_loss / max(1, steps), 6))
        return self

    # ---------------------------- sampling ------------------------------- #
    def sample(self, n: int, batch_size: int = 512) -> np.ndarray:
        """Reverse process → denormalized synthetic rows (shape n×d)."""
        if self.model is None or self.mean_ is None:
            raise RuntimeError("fit() before sample()")
        self.model.eval()
        c = self.config
        out: List[np.ndarray] = []
        with torch.no_grad():
            for start in range(0, n, batch_size):
                m = min(batch_size, n - start)
                xt = torch.randn(m, self.dim)
                for tt in range(c.T - 1, -1, -1):
                    t = torch.full((m,), tt, dtype=torch.long)
                    eps_hat = self.model(xt, t)
                    xt = self._reverse_step(xt, eps_hat, tt)
                out.append(xt.numpy())
        X = np.vstack(out)[:n]
        return (X * self.std_) + self.mean_

    def _reverse_step(self, xt: torch.Tensor, eps_hat: torch.Tensor,
                      tt: int) -> torch.Tensor:
        """x_{t-1} = (x_t − β_t/√(1−ᾱ_t)·ε_θ)/√α_t + σ_t z   (Ho et al. '20)."""
        beta = self.betas[tt]
        alpha = self.alphas[tt]
        sqrt_1m = self.sqrt_one_minus_alpha_bar[tt]
        if sqrt_1m < 1e-12:
            sqrt_1m = 1e-12
        mean = (xt - (beta / sqrt_1m) * eps_hat) / np.sqrt(alpha)
        if tt == 0:
            return mean
        return mean + self.post_sigma[tt] * torch.randn_like(xt)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "generator": "TabDiffusionCritic (Gaussian DDPM, MLP denoiser, "
                        "sinusoidal time embeddings)",
            "config": self.config.to_dict(),
            "seed": self.seed,
            "train_rows": self.train_rows,
            "feature_dim": self.dim,
            "final_train_loss_mse": (self.loss_history[-1]
                                     if self.loss_history else None),
            "epochs": len(self.loss_history),
        }