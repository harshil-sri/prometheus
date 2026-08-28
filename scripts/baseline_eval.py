"""
baseline_eval.py — Phase 2 honest baseline evaluation.

Produces artifacts/baseline_eval.json:
  * Two-axis holdout LOCKED and fingerprinted (splits.lock_holdout).
  * Train/eval temporal boundary (mirrors deployment: fit on history,
    score forward).
  * Meta-model stacked on OUT-OF-FOLD base scores for BOTH axes
    (XGB via make_oof_scores, GNN via per-fold subgraph refits) — no
    OR-gate, honest calibration (isotonic at scale / sigmoid below it).
  * Eval rows include the HELD-OUT attack types (A2 synthetic identity,
    A5 scatter-gather layering); they never appear in any training fold.
  * Full multi-prevalence metrics + per-attack-type breakdown + provenance.

Usage (repo root):
    python scripts/baseline_eval.py [--seed 42] [--accounts 1200] ...
Exit code 0 on success.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
for _p in (ROOT, SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

from twin.twin import FinancialDigitalTwin                          # noqa: E402
from attack.compiler import AttackCompiler                          # noqa: E402
from attack.benchmark_attacks import BENCHMARK_ATTACKS               # noqa: E402
from blue.features import compute_features, build_graph_data         # noqa: E402
from blue.xgb_model import XGBFraudDetector                          # noqa: E402
from blue.gnn_model import GNNFraudDetector                          # noqa: E402
from blue.meta_model import MetaModel, make_oof_scores               # noqa: E402
from blue.splits import (                                            # noqa: E402
    lock_holdout, assert_no_leakage, split_by_step,
)
from eval.harness import full_report                                  # noqa: E402

HELD_OUT_TYPES = ("A2", "A5")
TRAINABLE_TYPES = ("A1", "A3", "A4", "A6")


def run_attack(compiler, world, attack_id, rng):
    """Compile + execute a fresh instance of a benchmark attack."""
    plan = compiler.compile(BENCHMARK_ATTACKS[attack_id])
    return compiler.execute(plan, world)


def main() -> int:
    ap = argparse.ArgumentParser(description="Honest baseline eval (Phase 2)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--accounts", type=int, default=1200)
    ap.add_argument("--merchants", type=int, default=150)
    ap.add_argument("--steps", type=int, default=140)
    ap.add_argument("--boundary-fraction", type=float, default=0.72,
                    help="fraction of steps in the training window")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--gnn-epochs", type=int, default=30)
    args = ap.parse_args()

    seed = args.seed
    t_start = time.perf_counter()

    # ------------------------------------------------------------------
    # 1. Lock the two-axis holdout BEFORE anything is trained.
    # ------------------------------------------------------------------
    holdout = lock_holdout(seed=seed, held_out_types=HELD_OUT_TYPES)
    print(f"[eval] holdout locked: types={sorted(holdout.held_out_types)} "
          f"fingerprint={holdout.fingerprint[:16]}...")

    # ------------------------------------------------------------------
    # 2. Twin + TRAINING-phase attacks (trainable types only).
    # ------------------------------------------------------------------
    twin = FinancialDigitalTwin(seed=seed, num_accounts=args.accounts,
                                num_merchants=args.merchants,
                                num_devices=max(200, args.accounts // 3),
                                num_ip_blocks=max(60, args.accounts // 12),
                                num_steps=args.steps)
    world = twin.world

    n_train_steps = int(args.steps * args.boundary_fraction)

    def train_scheduler(w, tw):
        # inject one cycle of trainable attacks every ~30 steps inside window
        if 5 <= w.current_step <= n_train_steps and w.current_step % 30 == 0:
            for aid in TRAINABLE_TYPES:
                run_attack(compiler_ref, w, aid, None)

    compiler_ref = AttackCompiler(twin, seed=seed)
    pre_tx_count = len(world.transactions)
    twin.run(attack_scheduler=train_scheduler)
    print(f"[eval] training phase: steps<={n_train_steps}, "
          f"{len(world.transactions) - pre_tx_count} training txs")

    # EXECUTE step counts all as already-run world steps? AttackCompiler
    # executes with explicit step offsets into *future* slots of the current
    # step, so attacks land within the running window above.

    # ------------------------------------------------------------------
    # 3. Frozen EVAL phase: every six types incl. held-out, fresh seeds,
    #    positioned strictly AFTER the boundary step.
    # ------------------------------------------------------------------
    eval_compiler = AttackCompiler(twin, seed=seed + 777)
    traj_by_type = {}
    for aid in ("A1", "A2", "A3", "A4", "A5", "A6"):
        plan = eval_compiler.compile(BENCHMARK_ATTACKS[aid])
        traj_by_type[aid] = eval_compiler.execute(plan)
    print(f"[eval] eval trajectories generated for all 6 types "
          f"(held-out included: {HELD_OUT_TYPES})")
    # NOTE: the world finished stepping (current_step == --steps), so executed
    # plans land strictly after the training boundary by construction.

    # ------------------------------------------------------------------
    # 4. Features over the FULL log (causal streaming), then split.
    # ------------------------------------------------------------------
    txs = list(world.transactions)
    X_all, y_all, fnames = compute_features(txs, world)
    X_tab = np.asarray(X_all, dtype=np.float64)
    y_arr = np.asarray(y_all, dtype=np.float64)

    train_idx, eval_idx = split_by_step(txs, eval_fraction=1 - args.boundary_fraction,
                                        min_eval_steps=5)
    X_tr, y_tr = X_tab[train_idx], y_arr[train_idx]
    X_ev, y_ev = X_tab[eval_idx], y_arr[eval_idx]

    # Two-axis enforcement on the TRAINING slice only (eval holds A2/A5 by design)
    train_txs = [txs[i] for i in train_idx]
    assert_no_leakage(train_txs, holdout)
    print(f"[eval] leakage assert PASSED on {len(train_txs)} training rows")

    if len(np.unique(y_tr)) < 2 or y_tr.sum() < 4:
        print("[eval] FATAL: not enough fraud rows in the training slice "
              f"(pos={int(y_tr.sum())}); raise --accounts/--steps.")
        return 1

    # ------------------------------------------------------------------
    # 5. Out-of-fold base scores on the training slice.
    # ------------------------------------------------------------------
    def xgb_factory(tr_i):
        det = XGBFraudDetector(seed=seed)
        det.fit(X_tr[tr_i], y_tr[tr_i])
        return lambda va_i: det.predict_proba(X_tr[va_i])

    oof_xgb = make_oof_scores(xgb_factory, n_samples=len(y_tr),
                              n_splits=args.folds, y=y_tr, seed=seed)

    train_tx_set = list(train_idx)  # global row ids of training rows

    def gnn_factory(fold_train_pos):
        """Fold-local transductive refit: graph from this fold's txs only.

        Positions are slice-space (0..len(y_tr)); mapping to global tx ids
        happens via the outer `train_idx` closure.
        """
        f_txs = [txs[train_idx[p]] for p in fold_train_pos]
        data_f, idmap_f = build_graph_data(f_txs, world)
        if data_f is None or data_f.x.shape[0] == 0:
            return lambda va: np.full(len(va), 0.5)
        det = GNNFraudDetector(in_channels=data_f.x.shape[1], seed=seed)
        det.fit(data_f, epochs=args.gnn_epochs)
        node_p = det.predict_proba(data_f)[:, 1]
        idx_of = {a: i for i, a in enumerate(idmap_f.keys())}

        def scorer(val_pos):
            out = np.empty(len(val_pos), dtype=np.float64)
            for k, li in enumerate(val_pos):
                frm = str(txs[train_idx[li]]["from"])
                out[k] = node_p[idx_of[frm]] if frm in idx_of else 0.5
            return out
        return scorer

    # OOF loop stays entirely in slice-position space.
    oof_gnn = make_oof_scores(
        gnn_factory,
        n_samples=len(y_tr), n_splits=args.folds, y=y_tr, seed=seed,
    )

    # ------------------------------------------------------------------
    # 6. Honest meta fit on OOF columns (oof=True), then final scorers.
    # ------------------------------------------------------------------
    meta = MetaModel(seed=seed)
    meta.fit(np.column_stack([oof_xgb, oof_gnn]), y_tr, oof=True)
    print(f"[eval] meta fitted: calibration={meta.calibration_method} "
          f"oof_used={meta.oof_used_} coefs={meta.coefficients}")

    xgb_final = XGBFraudDetector(seed=seed).fit(X_tr, y_tr)
    data_full, idmap_full = build_graph_data(train_txs, world)
    gnn_final = None
    gnn_eval_scores = np.full(len(eval_idx), 0.5)
    if data_full is not None:
        gnn_final = GNNFraudDetector(in_channels=data_full.x.shape[1], seed=seed)
        gnn_final.fit(data_full, epochs=args.gnn_epochs)
        node_p = gnn_final.predict_proba(data_full)[:, 1]
        for j, gi in enumerate(eval_idx):
            frm = str(txs[gi]["from"])
            if frm in idmap_full:
                gnn_eval_scores[j] = float(node_p[idmap_full[frm]])

    xgb_eval_scores = xgb_final.predict_proba(X_ev)
    p_meta_eval = meta.predict_proba(np.column_stack(
        [xgb_eval_scores, gnn_eval_scores]))

    # baseline single-model numbers for comparison honesty
    report_meta = full_report(y_ev, p_meta_eval)
    report_xgb = full_report(y_ev, xgb_eval_scores)

    # Per-attack-type breakdown on eval rows
    by_type = {}
    for aid in sorted(traj_by_type.keys()):
        mask = np.array([
            str(txs[gi].get("attack_id")) == aid for gi in eval_idx
        ])
        if mask.any():
            sc = p_meta_eval[mask]
            leg_sc = p_meta_eval[~mask & (y_ev == 0)]
            # strict top-5%-of-legitimates alert bar; nextafter guard keeps
            # the metric meaningful when >95% of legitimates score exactly 0
            thr = float(np.quantile(leg_sc, 0.95))
            thr_strict = float(np.nextafter(thr, 2.0)) if thr <= 0 else thr
            by_type[aid] = {
                "n_txs": int(mask.sum()),
                "is_held_out": aid in HELD_OUT_TYPES,
                "mean_score": round(float(sc.mean()), 4),
                "median_score": round(float(np.median(sc)), 4),
                "recall_above_legit_p95": round(
                    float((sc > thr_strict).mean()), 4),
                "legit_p95_threshold": round(thr_strict, 8),
            }

    elapsed = time.perf_counter() - t_start
    artifact = {
        "schema": "prometheus.baseline_eval.v2",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "seed": seed,
        "platform": {"python": platform.python_version(),
                     "os": platform.platform(), "cpu_only": True},
        "runtime_seconds": round(elapsed, 2),
        "config": {
            "num_accounts": args.accounts, "num_merchants": args.merchants,
            "num_steps": args.steps,
            "boundary_fraction": args.boundary_fraction,
            "folds": args.folds, "gnn_epochs": args.gnn_epochs,
        },
        "holdout": {
            **holdout.to_dict(),
            "axis_type_note": ("held-out attack TYPES absent from every "
                               "training fold"),
            "axis_mechanism_note": ("mechanism axis active; rule_compiler "
                                    "only mechanism until P4/P5 land"),
        },
        "provenance": {
            "or_gate_removed": True,
            "meta_diagnostics": meta.diagnostics,
            "n_train_rows": int(len(train_idx)),
            "n_eval_rows": int(len(eval_idx)),
            "n_features": len(fnames),
            "train_fraud_rows": int(y_tr.sum()),
            "eval_fraud_rows": int(y_ev.sum()),
        },
        "honest_holdout_metrics": {
            "meta": report_meta,
            "xgb_only_baseline": report_xgb,
        },
        "per_attack_type": by_type,
    }

    out_dir = os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "baseline_eval.json")
    with open(out_path, "w") as f:
        json.dump(artifact, f, indent=2, default=str)

    pr = report_meta["overall"]["pr_auc"]
    held_pr = None
    print("\n[eval] ================= BASELINE (honest, two-axis) ================")
    print(f"[eval] holdout fingerprint : {holdout.fingerprint}")
    print(f"[eval] meta PR-AUC (eval)  : {pr:.4f}   | XGB-only: "
          f"{report_xgb['overall']['pr_auc']:.4f}")
    print(f"[eval] meta multi-prevalence sample 5%: "
          f"{report_meta['multi_prevalence'].get('0.05', {}).get('pr_auc')}")
    for aid, st in by_type.items():
        tag = "(HELD-OUT)" if st["is_held_out"] else ""
        print(f"[eval] {aid}: n={st['n_txs']} mean={st['mean_score']:.3f} "
              f"recall>p95={st['recall_above_legit_p95']:.2f} {tag}")
    print(f"[eval] runtime             : {elapsed:.1f}s")
    print(f"[eval] artifact written    : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
