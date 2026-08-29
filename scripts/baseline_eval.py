"""
baseline_eval.py — Phase 2 honest baseline evaluation.

Produces artifacts/baseline_eval.json:
  * Two-axis holdout LOCKED and fingerprinted (splits.lock_holdout).
  * Train/eval temporal boundary (mirrors deployment: fit on history,
    score forward). The boundary is cut at `int(steps*boundary_fraction)`
    so every frozen EVAL trajectory (injected strictly after the world
    finished stepping) lands in the eval slice by construction.
  * Meta-model stacked on OUT-OF-FOLD base scores for BOTH axes
    (XGB via make_oof_scores, GNN via per-fold subgraph refits) — no
    OR-gate, honest calibration (isotonic at scale / sigmoid below it).
  * Eval rows include the HELD-OUT attack types (A2 synthetic identity,
    A5 scatter-gather layering); they never appear in any training fold.
  * Full multi-prevalence metrics (fixed-prevalence PR-AUC is the
    cross-config headline), per-attack-type breakdown (fraud-only recall
    above the legit p95), and eval-population bookkeeping.
  * GENERATION HARDENING: the run FAILS LOUDLY (exit code 2) when any
    attack type produces fewer than `--min-eval-fraud-per-type` fraud rows
    in the eval slice — silently absent types (the historical A5 collapse)
    can no longer pass as a clean evaluation. Each type is executed
    `--eval-repeats` times for statistically useful per-type n. Funding is
    RING-FENCED per type (updates.md 2.3): the funded upper tail is
    partitioned into disjoint, deterministic pools sized to each type's
    principal, priciest first, and per-tier pool funding is reported in the
    artifact's `funding` block so depletion is loud, not silent.

Usage (repo root):
    python scripts/baseline_eval.py [--seed 42] [--accounts 1200] ...

Consumed by scripts/sweep_eval.py via evaluate() (import, no side effects);
the CLI wrapper writes the artifact and prints the console summary.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from typing import Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
for _p in (ROOT, SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

from twin.twin import FinancialDigitalTwin                          # noqa: E402
from attack.compiler import AttackCompiler                          # noqa: E402
from attack.funding import reserve_funding_pools, SAFETY_DEFAULT     # noqa: E402
from attack.benchmark_attacks import BENCHMARK_ATTACKS               # noqa: E402
from blue.features import compute_features, build_graph_data         # noqa: E402
from blue.xgb_model import XGBFraudDetector                          # noqa: E402
from blue.gnn_model import GNNFraudDetector                          # noqa: E402
from blue.meta_model import MetaModel, make_oof_scores               # noqa: E402
from blue.splits import lock_holdout, assert_no_leakage              # noqa: E402
from eval.harness import full_report                                  # noqa: E402

HELD_OUT_TYPES = ("A2", "A5")
TRAINABLE_TYPES = ("A1", "A3", "A4", "A6")
EVAL_TYPES = ("A1", "A2", "A3", "A4", "A5", "A6")

HEADLINE_PREVALENCE = "0.05"


class GenerationShortfallError(RuntimeError):
    """Raised when a type generates too few fraud rows for a reliable eval."""


def run_attack(compiler, world, attack_id, rng):
    """Compile + execute a fresh instance of a benchmark attack."""
    plan = compiler.compile(BENCHMARK_ATTACKS[attack_id])
    return compiler.execute(plan, world)


def evaluate(*, seed: int = 42, accounts: int = 1200, merchants: int = 150,
             steps: int = 140, boundary_fraction: float = 0.72,
             folds: int = 4, gnn_epochs: int = 30,
             eval_repeats: int = 5,
             min_eval_fraud_per_type: int = 5,
             funding_safety: float = SAFETY_DEFAULT,
             replenish_repeats: bool = False
             ) -> Tuple[Dict, List[str]]:
    """Run one honest baseline evaluation; returns (artifact, console_lines).

    Deterministic for a fixed argument set (same seed/accounts/merchants/
    steps/repeats ⇒ byte-identical artifact apart from the two wall-clock
    fields, ``generated_at`` and the holdout ``locked_at``; the fingerprint
    itself is seed-deterministic). Raises GenerationShortfallError when the
    eval population cannot support a per-type recall estimate.

    Funding hardening (updates.md 2.3): before the eval phase the funded
    upper tail of the twin is partitioned into DISJOINT, deterministic
    per-attack-type pools sized to each type's principal × eval_repeats ×
    funding_safety; eval attacks draw accounts only from their own reserve,
    so cross-attack-type depletion (the historical A5 starvation) cannot
    happen, and each type's pool funding per solvency tier is reported in
    the artifact. Optionally a salary/replenishment twin step runs between
    eval_repeats iterations when replenish_repeats is True.
    """
    log: List[str] = []
    t_start = time.perf_counter()

    # ------------------------------------------------------------------
    # 1. Lock the two-axis holdout BEFORE anything is trained.
    # ------------------------------------------------------------------
    holdout = lock_holdout(seed=seed, held_out_types=HELD_OUT_TYPES)
    log.append(f"[eval] holdout locked: types={sorted(holdout.held_out_types)} "
               f"fingerprint={holdout.fingerprint[:16]}...")

    # ------------------------------------------------------------------
    # 2. Twin + TRAINING-phase attacks (trainable types only).
    # ------------------------------------------------------------------
    twin = FinancialDigitalTwin(seed=seed, num_accounts=accounts,
                                num_merchants=merchants,
                                num_devices=max(200, accounts // 3),
                                num_ip_blocks=max(60, accounts // 12),
                                num_steps=steps)
    world = twin.world

    n_train_steps = int(steps * boundary_fraction)

    def train_scheduler(w, tw):
        # inject one cycle of trainable attacks every ~30 steps inside window
        if 5 <= w.current_step <= n_train_steps and w.current_step % 30 == 0:
            for aid in TRAINABLE_TYPES:
                run_attack(compiler_ref, w, aid, None)

    compiler_ref = AttackCompiler(twin, seed=seed)
    twin.run(attack_scheduler=train_scheduler)

    # ------------------------------------------------------------------
    # 3. Frozen EVAL phase: every six types incl. held-out, fresh seeds,
    #    positioned strictly AFTER the world finished stepping.
    #
    #    Funding hardening (updates.md 2.3): ring-fence the funded upper
    #    tail into disjoint per-type pools BEFORE any eval attack runs.
    #    Priciest types claim first (funding.order is principal-descending)
    #    and each type compiles exclusively inside its own reserve, so no
    #    type can cannibalize another's money — and pool funding is reported
    #    per solvency tier so shortfalls are loud, not silent.
    # ------------------------------------------------------------------
    generation_warnings: List[str] = []
    funding_specs = {aid: BENCHMARK_ATTACKS[aid] for aid in EVAL_TYPES}
    funding = reserve_funding_pools(
        world, funding_specs, eval_repeats, safety=funding_safety)
    log.append("[eval] funding reserves (ring-fenced per type, exec in "
               f"principal order {funding.order}):")
    for aid in funding.order:
        d = funding.diag[aid]
        log.append(
            f"  {aid}: pool_n={d['n_accounts']} "
            f"total=₹{d['total_balance']:,.0f} tiers(100/50/20)="
            f"{d['tier_100']}/{d['tier_50']}/{d['tier_20']} "
            f"(required=₹{d['required_balance']:,.0f})")
    generation_warnings.extend(funding.warnings)

    traj_by_type: Dict[str, List[str]] = {}
    funding_observed: Dict[str, List[Dict]] = {}
    for aid in funding.order:
        reps: List[str] = []
        observed: List[Dict] = []
        for rep in range(eval_repeats):
            ec = AttackCompiler(twin, seed=seed + 777 + rep * 13,
                                funded_pool=funding.pools[aid])
            plan = ec.compile(BENCHMARK_ATTACKS[aid])
            observed.append(dict(ec.last_funding_stats or {}))
            reps.append(ec.execute(plan))
            if replenish_repeats and rep < eval_repeats - 1:
                twin.step()
        traj_by_type[aid] = reps
        funding_observed[aid] = observed
    log.append(f"[eval] eval trajectories generated: 6 types \u00d7 "
               f"{eval_repeats} repeats (held-out included: {HELD_OUT_TYPES})")

    # ------------------------------------------------------------------
    # 4. Features over the FULL log (causal streaming), then split.
    #    Boundary is EXPLICIT: train = steps before the boundary, eval =
    #    everything at/after it. All frozen eval txs have step > steps, so
    #    they land in the eval slice by construction.
    # ------------------------------------------------------------------
    txs = list(world.transactions)
    X_all, y_all, fnames = compute_features(txs, world)
    X_tab = np.asarray(X_all, dtype=np.float64)
    y_arr = np.asarray(y_all, dtype=np.float64)
    steps_arr = np.asarray([int(t.get("step", 0)) for t in txs], dtype=np.int64)

    cut_step = n_train_steps + 1
    train_idx = [i for i, s in enumerate(steps_arr) if s < cut_step]
    eval_idx = [i for i, s in enumerate(steps_arr) if s >= cut_step]
    X_tr, y_tr = X_tab[train_idx], y_arr[train_idx]
    X_ev, y_ev = X_tab[eval_idx], y_arr[eval_idx]

    # Two-axis enforcement on the TRAINING slice only (eval holds A2/A5 by design)
    train_txs = [txs[i] for i in train_idx]
    assert_no_leakage(train_txs, holdout)
    log.append(f"[eval] leakage assert PASSED on {len(train_txs)} training rows")

    # Per-type row counts in the eval slice (before any model touches them)
    total_per_type: Dict[str, int] = {}
    fraud_per_type: Dict[str, int] = {}
    for aid in EVAL_TYPES:
        att_mask = np.array([
            str(txs[i].get("attack_id")) == aid for i in eval_idx
        ])
        total_per_type[aid] = int(att_mask.sum())
        fraud_per_type[aid] = int((att_mask & (y_ev == 1)).sum())

    # FAIL LOUDLY: a silently-empty type means generation broke, not that the
    # detector is perfect on it.
    if len(np.unique(y_tr)) < 2 or y_tr.sum() < 4:
        raise GenerationShortfallError(
            f"[eval] FATAL: only {int(y_tr.sum())} fraud rows in the training "
            f"slice — the economy/attack schedule produced too little fraud. "
            f"Raise --accounts/--steps or --folds before trusting metrics."
        )
    shortfalls = {aid: n for aid, n in fraud_per_type.items()
                  if n < min_eval_fraud_per_type}
    if shortfalls:
        raise GenerationShortfallError(
            f"[eval] GENERATION SHORTFALL (fail-loud): types produced fewer "
            f"than --min-eval-fraud-per-type={min_eval_fraud_per_type} fraud "
            f"rows in the eval slice: {shortfalls}. This is an eval-integrity "
            f"failure (silently-absent types used to pass as clean runs) — "
            f"the twin economy and/or compiler funding must be fixed before "
            f"these numbers can be trusted. "
            f"(--eval-repeats={eval_repeats}; try raising repeats or accounts.)"
        )

    eval_fraud = int(y_ev.sum())
    eval_prev = eval_fraud / max(1, len(eval_idx))
    log.append(f"[eval] eval population: rows={len(eval_idx)} fraud={eval_fraud} "
               f"prevalence={eval_prev:.4f} steps=[{int(steps_arr[eval_idx].min())}"
               f"..{int(steps_arr[eval_idx].max())}] cut_step={cut_step}")
    log.append(f"[eval] per-type eval fraud rows: {fraud_per_type}")

    low_bar = 15
    for aid in EVAL_TYPES:
        if fraud_per_type[aid] < low_bar:
            generation_warnings.append(
                f"type {aid}: n_fraud={fraud_per_type[aid]} < healthy "
                f"{low_bar} — per-type recall is noisy"
            )

    # ------------------------------------------------------------------
    # 5. Out-of-fold base scores on the training slice.
    # ------------------------------------------------------------------
    def xgb_factory(tr_i):
        det = XGBFraudDetector(seed=seed)
        det.fit(X_tr[tr_i], y_tr[tr_i])
        return lambda va_i: det.predict_proba(X_tr[va_i])

    oof_xgb = make_oof_scores(xgb_factory, n_samples=len(y_tr),
                              n_splits=folds, y=y_tr, seed=seed)

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
        det.fit(data_f, epochs=gnn_epochs)
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
        n_samples=len(y_tr), n_splits=folds, y=y_tr, seed=seed,
    )

    # ------------------------------------------------------------------
    # 6. Honest meta fit on OOF columns (oof=True), then final scorers.
    # ------------------------------------------------------------------
    meta = MetaModel(seed=seed)
    meta.fit(np.column_stack([oof_xgb, oof_gnn]), y_tr, oof=True)
    log.append(f"[eval] meta fitted: calibration={meta.calibration_method} "
               f"oof_used={meta.oof_used_} coefs={meta.coefficients}")

    xgb_final = XGBFraudDetector(seed=seed).fit(X_tr, y_tr)
    data_full, idmap_full = build_graph_data(train_txs, world)
    gnn_eval_scores = np.full(len(eval_idx), 0.5)
    if data_full is not None:
        gnn_final = GNNFraudDetector(in_channels=data_full.x.shape[1], seed=seed)
        gnn_final.fit(data_full, epochs=gnn_epochs)
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

    # Per-attack-type breakdown on eval rows (FRAUD rows only for recall —
    # small_test/camouflage legs of an attack are legitimately legitimate).
    by_type: Dict[str, Dict] = {}
    for aid in sorted(traj_by_type.keys()):
        att_mask = np.array([
            str(txs[gi].get("attack_id")) == aid for gi in eval_idx
        ])
        n_txs = int(att_mask.sum())
        fraud_mask = att_mask & (y_ev == 1)
        sc = p_meta_eval[fraud_mask]
        leg_sc = p_meta_eval[~att_mask & (y_ev == 0)]
        if len(leg_sc):
            thr = float(np.quantile(leg_sc, 0.95))
            thr_strict = float(np.nextafter(thr, 2.0)) if thr <= 0 else thr
        else:
            thr_strict = 0.0
        rec = (float((sc > thr_strict).mean()) if len(sc) else 0.0)
        by_type[aid] = {
            "n_txs": n_txs,
            "n_fraud_txs": int(fraud_mask.sum()),
            "is_held_out": aid in HELD_OUT_TYPES,
            "mean_score": round(float(sc.mean()), 4) if len(sc) else None,
            "median_score": round(float(np.median(sc)), 4) if len(sc) else None,
            "recall_above_legit_p95": round(rec, 4),
            "legit_p95_threshold": round(thr_strict, 8),
        }

    elapsed = time.perf_counter() - t_start
    mp_meta = report_meta.get("multi_prevalence", {}) or {}
    headline_pr = mp_meta.get(HEADLINE_PREVALENCE, {}).get("pr_auc")
    artifact = {
        "schema": "prometheus.baseline_eval.v4",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "seed": seed,
        "platform": {"python": platform.python_version(),
                     "os": platform.platform(), "cpu_only": True},
        "runtime_seconds": round(elapsed, 2),
        "config": {
            "num_accounts": accounts, "num_merchants": merchants,
            "num_steps": steps,
            "boundary_fraction": boundary_fraction,
            "folds": folds, "gnn_epochs": gnn_epochs,
            "eval_repeats": eval_repeats,
            "min_eval_fraud_per_type": min_eval_fraud_per_type,
            "funding_safety": funding_safety,
            "replenish_repeats": replenish_repeats,
        },
        "holdout": {
            **holdout.to_dict(),
            "axis_type_note": ("held-out attack TYPES absent from every "
                               "training fold"),
            "axis_mechanism_note": ("mechanism axis active; rule_compiler "
                                    "only mechanism until P4/P5 land"),
        },
        "eval_population": {
            "n_eval_rows": int(len(eval_idx)),
            "n_eval_fraud": eval_fraud,
            "n_eval_normal": int(len(eval_idx)) - eval_fraud,
            "eval_fraud_prevalence": round(eval_prev, 6),
            "step_window": [int(steps_arr[eval_idx].min()),
                            int(steps_arr[eval_idx].max())],
            "cut_step": int(cut_step),
            "per_type_total_rows": total_per_type,
            "per_type_fraud_rows": fraud_per_type,
        },
        "funding": {
            "reserve_policy": ("disjoint per-type pools carved from the "
                               "funded upper tail, principal-descending; "
                               "each type compiles only inside its pool"),
            "safety": funding_safety,
            "replenish_between_repeats": replenish_repeats,
            "exec_order": funding.order,
            "reserved_pools": funding.diag,
            "observed_at_compile": funding_observed,
        },
        "generation_warnings": generation_warnings,
        "provenance": {
            "or_gate_removed": True,
            "meta_diagnostics": meta.diagnostics,
            "n_train_rows": int(len(train_idx)),
            "n_eval_rows": int(len(eval_idx)),
            "n_features": len(fnames),
            "train_fraud_rows": int(y_tr.sum()),
            "eval_fraud_rows": eval_fraud,
        },
        "headline": {
            "metric": f"meta PR-AUC @ {HEADLINE_PREVALENCE} prevalence "
                      f"(fixed prevalence, cross-config comparable)",
            "pr_auc": headline_pr,
            "overall_meta_pr_auc": report_meta["overall"]["pr_auc"],
            "overall_xgb_pr_auc": report_xgb["overall"]["pr_auc"],
            "eval_fraud_prevalence": round(eval_prev, 6),
        },
        "honest_holdout_metrics": {
            "meta": report_meta,
            "xgb_only_baseline": report_xgb,
        },
        "per_attack_type": by_type,
    }

    pr = report_meta["overall"]["pr_auc"]
    log.append("\n[eval] ================= BASELINE (honest, two-axis) ================")
    log.append(f"[eval] holdout fingerprint : {holdout.fingerprint}")
    log.append(f"[eval] meta PR-AUC (eval)  : {pr:.4f}   | XGB-only: "
               f"{report_xgb['overall']['pr_auc']:.4f}")
    log.append(f"[eval] headline (fixed {HEADLINE_PREVALENCE} prevalence): "
               f"{headline_pr}")
    for aid, st in by_type.items():
        tag = "(HELD-OUT)" if st["is_held_out"] else ""
        log.append(f"[eval] {aid}: n_txs={st['n_txs']} n_fraud={st['n_fraud_txs']} "
                   f"mean={st['mean_score']} recall>p95(fraud)="
                   f"{st['recall_above_legit_p95']:.2f} {tag}")
    log.append(f"[eval] runtime             : {elapsed:.1f}s")
    return artifact, log


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
    ap.add_argument("--eval-repeats", type=int, default=5,
                    help="fresh executions of each attack type in the eval phase")
    ap.add_argument("--min-eval-fraud-per-type", type=int, default=5,
                    help="fail loudly (exit 2) if a type scores below this "
                         "many fraud rows in the eval slice; 0 disables")
    ap.add_argument("--funding-safety", type=float, default=SAFETY_DEFAULT,
                    help="reserve multiplier: each type's pool is sized to "
                         "amount*repeats*safety (updates.md 2.3 ring-fence)")
    ap.add_argument("--replenish-repeats", action="store_true",
                    help="run one salary/replenishment twin step between "
                         "eval_repeats iterations")
    args = ap.parse_args()

    try:
        artifact, log = evaluate(
            seed=args.seed, accounts=args.accounts, merchants=args.merchants,
            steps=args.steps, boundary_fraction=args.boundary_fraction,
            folds=args.folds, gnn_epochs=args.gnn_epochs,
            eval_repeats=args.eval_repeats,
            min_eval_fraud_per_type=args.min_eval_fraud_per_type,
            funding_safety=args.funding_safety,
            replenish_repeats=args.replenish_repeats,
        )
    except GenerationShortfallError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    for line in log:
        print(line)

    out_dir = os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "baseline_eval.json")
    with open(out_path, "w") as f:
        json.dump(artifact, f, indent=2, default=str)

    print(f"[eval] artifact written    : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())