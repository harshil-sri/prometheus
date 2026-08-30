#!/usr/bin/env python3
"""
fit_weights.py — Fit the `w_*` of the weighted structured-score formula
(updates.md 2.2 / implementation.md Phase 4).

Stops hand-picking weights: the six weighted-formula weights

    R = w_t·T + w_g·G + w_b·B + w_e·E + w_c·C − w_u·U

are FIT by a monotone-constrained (non-negative) least-squares regression
(scipy.optimize.nnls) of standardized evidence terms against a deterministic
target set, then persisted alongside the logistic head coefs at the ONE
canonical path (`src/artifacts/structured_weights.json`, schema v2).

Dataset (fully deterministic, seed 42 — the canonical demo config):
    * REAL rows   — every transaction of the canonical twin
      (500 accounts / 100 merchants / 100 steps, seed 42, plus the
      training-phase A1-A6 attacks, exactly as /api/init builds it). Evidence
      terms T=xgb, G=gnn, B=meta are the ensemble signals; U=|xgb−gnn|;
      E=C=0 (a bare twin has no OSINT dossiers / sanctions hits / campaign
      memory — zero is the honest value). Target = the FITTED logistic head's
      in-sample P(fraud)×1000, so the reduced formula reproduces the same
      decision surface the deep path already uses.
    * CALIBRATION GRID — documented evidence cells that make the sparse
      E and C axes identifiable and pin band reachability (the updates.md
      "sum=750 → DECLINE unreachable" bug). Semantics: the ML prior supplies
      a strong base (full prior = 700, not alone-DECLINE); E and C are
      ADDITIVE kill-shots pushing a suspicious prior across the line
      (everything = 1000); E or C alone with zero prior support is NOT
      auto-decline (250); max disagreement pulls a full prior down to 500,
      giving w_u a real (subtractive) penalty.

Constraints & invariants:
    * nnls ⇒ all weights ≥ 0 ⇒ the formula is monotone in every positive
      evidence term and w_u only ever subtracts (a penalty).
    * Band reachability: weights are rescaled (documented, deterministic)
      so max raw (sum of positive weights − w_u) equals 1000 — every band on
      the 0-1000 Mastercard scale is reachable.
    * The artifact carries NO wall-clock fields: rerunning the script is
      byte-for-byte deterministic.

Both scorers remain interpolatable: predict_row keeps its logistic ML prior
and uses the fitted w_e/w_c additively; compute_structured_score accepts any
weights override. The fitted weights are reported (fitted vs baseline +
provenance) in the artifact and via /api/structured-weights.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
for _p in (_PROJECT_ROOT, os.path.join(_PROJECT_ROOT, "src"), _SCRIPT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _ensure_utf8_stdout import ensure_utf8_stdout  # noqa: E402
ensure_utf8_stdout()

import numpy as np                                   # noqa: E402

from attack.benchmark_attacks import generate_training_attacks   # noqa: E402
from attack.compiler import AttackCompiler           # noqa: E402
from blue.ensemble import BlueTeamEnsemble           # noqa: E402
from scoring.structured_score import (               # noqa: E402
    DEFAULT_WEIGHTS, DEFAULT_WEIGHTS_PATH, FORMULA_TERMS, WEIGHTS_SCHEMA,
    FittedStructuredScore, SCORE_COLUMNS,
)
from scoring.weight_fit import BAND_MAX, GRID, fit_w_star  # noqa: E402

SEED = 42
ACCOUNTS = 500
MERCHANTS = 100
STEPS = 100


def build_dataset(seed: int = SEED) -> Tuple["np.ndarray", "np.ndarray",
                                             Dict[str, Any],
                                             "FittedStructuredScore"]:
    """Rebuild the canonical demo twin, fit the ensemble and the logistic
    head, and return (evidence_matrix [n x 6], targets [n], meta, head).

    Deterministic: fixed seed, fixed config, no RNG aside from the seeded
    twin/ensemble internals.
    """
    from twin.twin import FinancialDigitalTwin

    twin = FinancialDigitalTwin(seed=seed, num_accounts=ACCOUNTS,
                                num_merchants=MERCHANTS, num_steps=STEPS)
    twin.run()
    compiler = AttackCompiler(twin, seed=seed)
    generate_training_attacks(compiler, twin.world)
    txs = list(twin.world.transactions)
    labels = [1.0 if t.get("is_fraud") else 0.0 for t in txs]

    blue = BlueTeamEnsemble.untrained(seed=seed)
    blue.fit_transactions(txs, twin.world)
    sig = blue.score_all_signals(txs, twin.world, manifold=None)

    # Logistic head over the same six signal columns /api/init uses.
    X6 = np.column_stack([np.asarray(sig[c], dtype=np.float64)
                          for c in SCORE_COLUMNS])
    head = FittedStructuredScore().fit(X6, labels, feature_order=SCORE_COLUMNS)

    xgb = np.asarray(sig["xgb"], dtype=np.float64)
    gnn = np.asarray(sig["gnn"], dtype=np.float64)
    meta = np.asarray(sig["meta"], dtype=np.float64)
    T = xgb
    G = gnn
    B = meta
    U = np.abs(xgb - gnn)
    E = np.zeros_like(T)
    C = np.zeros_like(T)

    # In-sample target: the fitted logistic head's decision surface,
    # computed with the exact same math predict_row uses (deterministic).
    z = float(head.intercept_) + X6 @ np.asarray(head.coef_, dtype=np.float64)
    p = np.where(z >= -35.0, 1.0 / (1.0 + np.exp(-z)), 0.0)
    real_target = p * BAND_MAX

    X = np.column_stack([T, G, B, E, C, U])
    y = real_target

    grid_X = np.asarray([row[:6] for row in GRID], dtype=np.float64)
    grid_y = np.asarray([row[6] for row in GRID], dtype=np.float64)
    X = np.vstack([X, grid_X])
    y = np.concatenate([y, grid_y])

    n_real = len(txs)
    meta = {
        "n": n_real,
        "pos": int(sum(labels)),
        "seed": seed,
        "accounts": ACCOUNTS,
        "merchants": MERCHANTS,
        "steps": STEPS,
        "real_rows": n_real,
        "grid_rows": len(GRID),
        "logistic_fit_auc": head.fit_meta.get("fit_auc"),
        "evidence_columns": ["T", "G", "B", "E", "C", "U"],
        "target": "fitted logistic P(fraud)x1000 on real rows; "
                  "documented calibration grid for the E/C/uncertainty axes",
    }
    return X, y, meta, head


def compose_artifact(w: Dict[str, float], meta: Dict[str, Any],
                     head: "FittedStructuredScore",
                     w_fit: Optional[Dict[str, Any]] = None) -> Dict:
    """Assemble the v2 artifact blob. No wall-clock fields → deterministic."""
    fit_block = {
        "algorithm": "nnls (scipy.optimize) on standardized evidence",
        "monotone": "non-negative weights: raising any evidence term "
                    "cannot lower the raw score",
        "reachability": "rescaled so max raw = 1000 (all bands reachable)",
        "target": meta.get("target"),
        "n_real": int(meta["real_rows"]),
        "n_grid": len(GRID),
    }
    if w_fit:
        fit_block.update(w_fit)
    return {
        "schema": WEIGHTS_SCHEMA,
        "formula": "R = w_t·T + w_g·G + w_b·B + w_e·E + w_c·C − w_u·U",
        "coef": [round(float(c), 8) for c in head.coef_],
        "intercept": round(float(head.intercept_), 8),
        "columns": list(head.columns),
        "fit_meta": {
            "n": int(meta["n"]),
            "pos": int(meta["pos"]),
            "fit_auc": float(meta["logistic_fit_auc"]),
            "source": "fitted_in_sample",
            "fit_config": {
                "seed": SEED,
                "accounts": ACCOUNTS,
                "merchants": MERCHANTS,
                "steps": STEPS,
            },
        },
        "w_formula": {k: float(w[k]) for k in FORMULA_TERMS},
        "baseline_weights": {k: float(DEFAULT_WEIGHTS[k]) for k in FORMULA_TERMS},
        "w_fit": fit_block,
    }


def generate_weights(seed: int = SEED) -> Tuple[Dict, Dict]:
    """End-to-end deterministic generation: (artifact_blob, weights)."""
    X, y, meta, head = build_dataset(seed)
    w, diag = fit_w_star(X, y, n_real=int(meta["real_rows"]))
    blob = compose_artifact(w, meta, head, w_fit=diag)
    return blob, w


def main() -> int:
    blob, w = generate_weights(seed=SEED)
    out_dir = os.path.dirname(DEFAULT_WEIGHTS_PATH)
    os.makedirs(out_dir, exist_ok=True)
    with open(DEFAULT_WEIGHTS_PATH, "w") as f:
        json.dump(blob, f, indent=2)
    print(f"[weights] fitted w_*  : {w}")
    print(f"[weights] baseline    : "
          f"{ {k: DEFAULT_WEIGHTS[k] for k in FORMULA_TERMS} }")
    pos_sum = sum(v for k, v in w.items() if k != "w_u")
    print(f"[weights] max raw     : {pos_sum - w['w_u']:.2f} "
          f"(DECLINE reachable: {pos_sum - w['w_u'] >= 700.0})")
    print(f"[weights] artifact    : {DEFAULT_WEIGHTS_PATH} "
          f"(schema {blob['schema']}, n={blob['fit_meta']['n']}, "
          f"pos={blob['fit_meta']['pos']}, auc={blob['fit_meta']['fit_auc']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())