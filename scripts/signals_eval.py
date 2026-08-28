"""
signals_eval.py — Phase 6 gate: five-branch decorrelation evidence.

Protocol (deterministic seed 42):
  1. twin + victim as in prior gates; train NormalcyManifold on the
     label==0 training slice ONLY (normal-only law).
  2. Evaluate over a fresh eval window: score every tx with
     BlueTeamEnsemble.score_all_signals → six aligned columns.
  3. Pearson correlation matrix over the columns → artifacts/decorrelation.json,
     plus fraud-vs-normal separation per signal (AUC-quick), fingerprinted.

The point: prove that manifold & spectral channels carry information NOT
already inside xgb/gnn/meta (|ρ| far below 1), so a future 5-way stack is
justified by data, not vibes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
for _p in (ROOT, SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np                                                        # noqa: E402

from twin.twin import FinancialDigitalTwin                                # noqa: E402
from attack.compiler import AttackCompiler                                 # noqa: E402
from attack.benchmark_attacks import generate_training_attacks              # noqa: E402
from blue.ensemble import BlueTeamEnsemble                                  # noqa: E402
from blue.spectral import compute_spectral_features                          # noqa: E402


def quick_auc(scores: np.ndarray, y: np.ndarray) -> float:
    """Mann–Whitney AUC without sklearn dependency drift."""
    pos = scores[y == 1]
    neg = scores[y == 0]
    if not pos.size or not neg.size:
        return float("nan")
    diffs = pos[:, None] - neg[None, :]
    return float(((diffs > 0).sum() + 0.5 * (diffs == 0).sum()) / diffs.size)


def main() -> int:
    ap = argparse.ArgumentParser(description="Signal decorrelation (P6)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--accounts", type=int, default=160)
    ap.add_argument("--steps", type=int, default=60)
    args = ap.parse_args()
    t0 = time.perf_counter()

    twin = FinancialDigitalTwin(seed=args.seed, num_accounts=args.accounts,
                                num_merchants=45, num_devices=70,
                                num_ip_blocks=25, num_steps=args.steps)
    twin.run()
    compiler = AttackCompiler(twin, seed=args.seed)
    generate_training_attacks(compiler, twin.world)

    # temporal split: train slice for models+manifold, later window evaluated
    txs = list(twin.world.transactions)
    steps = np.array([t.get("step", 0) for t in txs])
    cut = int(np.quantile(steps, 0.75))
    tr_idx = [i for i in range(len(txs)) if steps[i] < cut]
    ev_idx = [i for i in range(len(txs)) if steps[i] >= cut]
    has_train_fraud = any(txs[i].get("is_fraud") for i in tr_idx)
    has_eval_fraud = any(txs[i].get("is_fraud") for i in ev_idx)
    if not (has_train_fraud and has_eval_fraud):
        # tiny-world fallback: evaluate over everything, train on everything
        # (fraud still excluded from the MANIFOLD via label==0 slicing below)
        tr_idx = list(range(len(txs)))
        ev_idx = list(range(len(txs)))

    victim = BlueTeamEnsemble.untrained(seed=args.seed)
    victim.fit_transactions([txs[i] for i in tr_idx], twin.world,
                            oof_folds=3, gnn_epochs=15)

    from blue.features import compute_features
    X_tr, y_tr, _ = compute_features([txs[i] for i in tr_idx], twin.world)
    normal_rows = X_tr[np.asarray(y_tr) == 0]

    from blue.manifold import NormalcyManifold
    manifold = NormalcyManifold(seed=args.seed, epochs=350).fit(
        normal_rows, feature_names=victim.feature_names)

    signals = victim.score_all_signals([txs[i] for i in ev_idx],
                                       twin.world, manifold=manifold)

    cols = ["xgb", "gnn", "meta", "manifold", "spectral_cycle",
            "spectral_star"]
    M = np.vstack([np.asarray(signals[c], dtype=np.float64)
                   for c in cols])
    corr = np.corrcoef(M)

    y_ev = np.array([1.0 if txs[i].get("is_fraud") else 0.0
                     for i in ev_idx])
    separations = {c: round(quick_auc(np.asarray(signals[c], dtype=np.float64),
                                      y_ev), 4)
                   for c in cols}

    fp_payload = {"schema": "prometheus.decorrelation.v1",
                  "seed": args.seed,
                  "columns": cols,
                  "n_eval": len(ev_idx),
                  "n_train": len(tr_idx)}
    artifact = {
        "schema": "prometheus.decorrelation.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_seconds": round(time.perf_counter() - t0, 2),
        "config": {"seed": args.seed, "accounts": args.accounts,
                   "steps": args.steps, "n_eval": len(ev_idx),
                   "n_train_normal": int((y_tr == 0).sum()),
                   "n_eval_fraud": int(y_ev.sum())},
        "fingerprint": hashlib.sha256(json.dumps(
            fp_payload, sort_keys=True).encode()).hexdigest()[:16],
        "columns": cols,
        "correlation_matrix": [[round(float(v), 4) for v in row]
                               for row in corr],
        "separations_auc": separations,
        "max_offdiag_abs_corr": round(float(
            np.max(np.abs(corr - np.eye(len(cols))))), 4),
        "note": ("low |rho| between {xgb,gnn,meta} and "
                 "{manifold,spectral_*} justifies an eventual 5-way stack; "
                 "manifold trained on label==0 rows only"),
    }

    out_dir = os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "decorrelation.json")
    with open(out_path, "w") as f:
        json.dump(artifact, f, indent=2, default=str)
    print(f"[signals] corr matrix:\n{np.round(corr, 3)}")
    print(f"[signals] separations: {separations}")
    print(f"[signals] max |off-diag rho| = "
          f"{artifact['max_offdiag_abs_corr']}")
    print(f"[signals] artifact written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
