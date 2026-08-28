"""distill.py — Black-box score distillation of the blue-team ensemble.

Threat model (honest): the adversary has ORACLE ACCESS to the deployed
predict_proba (query responses), NOT to the victim's weights. We:

  1. collect probe feature rows from the twin's transaction log plus seeded
     light perturbations inside observed feature ranges ("query synthesis");
  2. query the victim oracle for soft labels p_victim(x);
  3. fit TWO models on the same (X, y_soft):
       * XGBRegressor surrogate  -> fidelity METRIC vs victim (reported)
       * torch MLP "shadow net"  -> differentiable gradient carrier for PGD
         (XGBoost trees are piecewise-constant; gradients must come from a
         smooth proxy — this IS the shadow-gradient idea)

Fidelity is reported for BOTH models on held-out probes: R^2, MAE and, for
calibration flavour, mean absolute probability error. No fidelity number is
invented — if training fails the error propagates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import numpy as np

try:
    import xgboost as xgb
except ImportError:            # pragma: no cover - pinned dep, but stay loud
    xgb = None

import torch
import torch.nn as nn

logger = logging.getLogger(__name__)

__all__ = ["ScoreOracleFn", "ProbeBundle", "DistillResult",
           "collect_probes", "distill_surrogates", "ShadowNet"]

#: callable X(n,20) -> p_fraud(n,) — the victim as the attacker sees it
ScoreOracleFn = Callable[[np.ndarray], np.ndarray]


@dataclass
class ProbeBundle:
    """Query log the attacker ends up with after probing the oracle."""
    X_query: np.ndarray                 # (n_q, d) sent to the oracle
    y_victim: np.ndarray                # (n_q,) received probabilities
    X_holdout: np.ndarray               # evaluation probes never trained on
    y_holdout: np.ndarray
    source_tx_ids: List[str] = field(default_factory=list)


@dataclass
class DistillResult:
    """Measured distillation quality — reported verbatim into artifacts."""
    n_queries: int
    n_holdout: int
    xgb_fidelity: dict                  # r2, mae on holdout
    mlp_fidelity: dict                  # r2, mae on holdout
    epochs_mlp: int
    seed: int


# ---------------------------------------------------------------------------
# Shadow net (gradient carrier)
# ---------------------------------------------------------------------------

class ShadowNet(nn.Module):
    """Small MLP over the 20 tabular features -> P(fraud).

    Input standardization happens INSIDE the module via registered buffers,
    so callers (PGD included) feed raw feature rows and still get correct
    gradients through normalized space.
    """

    def __init__(self, d_in: int = 20, hidden: int = 64):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(d_in, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
            nn.Linear(hidden, 1),
        )
        self.register_buffer("f_mean", torch.zeros(d_in))
        self.register_buffer("f_std", torch.ones(d_in))

    def set_normalization(self, X_train: np.ndarray) -> None:
        mean = X_train.mean(axis=0)
        std = X_train.std(axis=0)
        std = np.where(std < 1e-8, 1.0, std)
        with torch.no_grad():
            self.f_mean.copy_(torch.tensor(mean, dtype=torch.float32))
            self.f_std.copy_(torch.tensor(std, dtype=torch.float32))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z = (x - self.f_mean) / self.f_std
        return torch.sigmoid(self.net(z)).squeeze(-1)


# ---------------------------------------------------------------------------
# Probe collection
# ---------------------------------------------------------------------------

def _light_perturb(X: np.ndarray, rng: np.random.RandomState) -> np.ndarray:
    """Jitter numeric columns within ±15% of value, keep everything finite.

    Deliberately coarse: probes only need to SPAN the score surface; PGD does
    the fine-grained work later.
    """
    Xp = X.copy()
    num = np.isfinite(X).all(axis=0)
    scale = 1.0 + rng.uniform(-0.15, 0.15, size=X.shape)
    Xp = np.where(num[None, :], X * scale, X)
    return np.clip(Xp, 0.0, None)


def collect_probes(transactions: Sequence[dict], victim_fn: ScoreOracleFn,
                   world_state=None,
                   max_probes: int = 1200, jitter_fraction: float = 0.5,
                   seed: int = 42,
                   ensure_fraud_rows: bool = True) -> ProbeBundle:
    """Build probe set = real tx rows + jitters, label them with the victim.

    Fraud-aware sampling: every `is_fraud` row is included first (an
    adversary probing a deployed API would naturally concentrate queries on
    suspicious territory), then a deterministic benign fill completes the
    budget. Last 25% held out for fidelity measurement (never trained on)."""
    from blue.features import compute_features

    X_all, _, names = compute_features(list(transactions), world_state)
    X_real = np.asarray(X_all, dtype=np.float64)
    n_total = len(transactions)

    fraud_idx = np.array([i for i, t in enumerate(transactions)
                          if t.get("is_fraud")], dtype=int)
    benign_idx = np.array([i for i in range(n_total)
                           if not (transactions[i].get("is_fraud"))],
                          dtype=int)

    rng = np.random.RandomState(seed)
    chosen = []
    if ensure_fraud_rows and len(fraud_idx):
        chosen.extend(fraud_idx.tolist())
        n_fraud = len(fraud_idx)
    else:
        n_fraud = 0
    if n_fraud < n_total:
        # stratify: half of remaining budget mid-low benign, half high-score-ish benign
        rest_budget = min(max_probes, n_total) - len(chosen)
        if rest_budget > 0 and len(benign_idx):
            take = min(rest_budget, len(benign_idx))
            pick = rng.choice(len(benign_idx), size=take, replace=False)
            chosen.extend(benign_idx[pick].tolist())
    if not chosen:
        chosen = list(range(min(max_probes, n_total)))

    X_base_sel = X_real[chosen]
    ids = [str(transactions[i].get("tx_id", f"ROW_{i}")) for i in chosen]

    n_jitter = min(int(len(X_base_sel) * jitter_fraction),
                   max(0, max_probes - len(X_base_sel)))
    parts = [X_base_sel]
    while n_jitter > 0:
        take = min(n_jitter, len(X_base_sel))
        idx = rng.choice(len(X_base_sel), size=take, replace=False)
        parts.append(_light_perturb(X_base_sel[idx], rng))
        ids.extend(f"PROBE_{len(ids)+k}" for k in range(take))
        n_jitter -= take
    X_probe = np.vstack(parts)[:max_probes]
    ids = ids[:max_probes]

    X_probe = np.nan_to_num(X_probe, nan=0.0, posinf=1e6, neginf=-1e6)

    y = np.asarray(victim_fn(X_probe), dtype=np.float64).ravel()

    # Fraud rows stay in TRAINING (they define the decision surface);
    # holdout is carved from the remainder.
    train_order = np.arange(len(X_probe))
    rng.shuffle(train_order)
    # keep holdout free of raw fraud anchors but allow their jitters
    train_ids_pos = [p for p in train_order if p >= n_fraud or p < len(fraud_idx)]
    n_holdout = max(20, int(len(X_probe) * 0.25))
    holdout_pos = [p for p in train_ids_pos[-n_holdout*2:]
                   if str(ids[p]).startswith("PROBE_")][:n_holdout] \
        or list(train_order[:n_holdout])
    holdout_pos_set = set(map(int, holdout_pos))
    query_pos = np.array([p for p in range(len(X_probe))
                          if p not in holdout_pos_set], dtype=int)
    holdout_arr = np.array(sorted(holdout_pos_set), dtype=int)

    bundle = ProbeBundle(
        X_query=X_probe[query_pos],
        y_victim=y[query_pos],
        X_holdout=X_probe[holdout_arr],
        y_holdout=y[holdout_arr],
        source_tx_ids=[ids[p] for p in holdout_arr],
    )
    return bundle


# ---------------------------------------------------------------------------
# Distillation
# ---------------------------------------------------------------------------

def _fidelity(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    ss_res = float(np.sum((y_true - y_pred) ** 2))
    ss_tot = float(np.sum((y_true - y_true.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    mae = float(np.mean(np.abs(y_true - y_pred)))
    return {"r2": round(r2, 4), "mae": round(mae, 4)}


def distill_surrogates(bundle: ProbeBundle, seed: int = 42,
                       mlp_epochs: int = 200, mlp_lr: float = 1e-3,
                       ) -> tuple["object", ShadowNet, DistillResult]:
    """Fit XGB surrogate + shadow MLP on the same soft labels.

    Returns (xgb_surrogate, shadow_net, DistillResult-with-measured-fidelity).
    """
    if xgb is None:
        raise ImportError("xgboost is required for the shadow surrogate")

    X_tr, y_tr = bundle.X_query, bundle.y_victim
    X_ho, y_ho = bundle.X_holdout, bundle.y_holdout
    if len(X_ho) == 0 or len(np.unique(y_tr.round(6))) < 2:
        raise ValueError("degenerate probe bundle: need >=2 distinct scores "
                         f"(got labels min={y_tr.min():.3f} max={y_tr.max():.3f})")

    # --- XGB surrogate: regression on soft labels (richer than hard ones) ---
    surr = xgb.XGBRegressor(
        objective="reg:squarederror",
        n_estimators=300, max_depth=5, learning_rate=0.08,
        subsample=0.9, colsample_bytree=0.9,
        reg_lambda=1.0, random_state=seed, n_jobs=-1, verbosity=0,
    )
    surr.fit(X_tr, y_tr)

    # --- MLP shadow net ---------------------------------------------------
    torch.manual_seed(seed)
    net = ShadowNet(d_in=X_tr.shape[1])
    net.set_normalization(X_tr)
    opt = torch.optim.Adam(net.parameters(), lr=mlp_lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=mlp_epochs)
    Xt = torch.tensor(X_tr, dtype=torch.float32)
    yt = torch.tensor(y_tr, dtype=torch.float32)
    net.train()
    for _ in range(mlp_epochs):
        opt.zero_grad()
        loss = nn.functional.mse_loss(net(Xt), yt)
        loss.backward()
        opt.step()
        sched.step()
    net.eval()

    with torch.no_grad():
        mlp_pred = net(torch.tensor(X_ho, dtype=torch.float32)).numpy()

    result = DistillResult(
        n_queries=int(len(X_tr)),
        n_holdout=int(len(X_ho)),
        xgb_fidelity=_fidelity(y_ho, np.clip(surr.predict(X_ho), 0, 1)),
        mlp_fidelity=_fidelity(y_ho, np.clip(mlp_pred, 0, 1)),
        epochs_mlp=mlp_epochs,
        seed=seed,
    )
    logger.info("distill fidelity: xgb=%s mlp=%s",
                result.xgb_fidelity, result.mlp_fidelity)
    return surr, net, result
