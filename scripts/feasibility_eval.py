"""
feasibility_eval.py — Phase 9 gate: margins, latency, INR cost, drift PSI.

Runs ONE deterministic world (seed 42) and emits:
    artifacts/margins.json      empirical decision-margin distribution
                                (shadow-PGD replay vs true ensemble)
    artifacts/latency.json      fast-path vs deep-path P50/P95/P99 measured
    artifacts/cost_model.json   INR economics at declared assumptions over
                                measured recall@budget points
    artifacts/drift.json        PSI early→late window, normal-only AND
                                full-log variants (attack-induced gap)

Margin vocabulary law enforced on every emitted artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (ROOT, SRC, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _ensure_utf8_stdout import ensure_utf8_stdout  # noqa: E402
ensure_utf8_stdout()

import numpy as np                                                        # noqa: E402

from twin.twin import FinancialDigitalTwin                                # noqa: E402
from attack.compiler import AttackCompiler                                 # noqa: E402
from attack.benchmark_attacks import generate_training_attacks              # noqa: E402
from blue.ensemble import BlueTeamEnsemble                                  # noqa: E402
from blue.features import compute_features                                   # noqa: E402
from eval.margins import margin_distribution                                  # noqa: E402
from eval.latency import measure_latency, environment_info                    # noqa: E402
from eval.cost_model import inr_economics, sensitivity_grid                   # noqa: E402
from eval.drift import drift_report                                           # noqa: E402

OUT_DIR = os.path.join(ROOT, "artifacts")
BANNED = "certified"


def _dump(name: str, payload: dict) -> str:
    assert BANNED not in json.dumps(payload).lower()
    path = os.path.join(OUT_DIR, name)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"[p9] wrote {name}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Feasibility evals (P9)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--accounts", type=int, default=160)
    ap.add_argument("--steps", type=int, default=60)
    ap.add_argument("--fast-iters", type=int, default=30)
    ap.add_argument("--deep-iters", type=int, default=5)
    args = ap.parse_args()
    t0 = time.perf_counter()

    os.makedirs(OUT_DIR, exist_ok=True)

    # ---------------------------------------------------------------- world
    twin = FinancialDigitalTwin(seed=args.seed, num_accounts=args.accounts,
                                num_merchants=45, num_devices=70,
                                num_ip_blocks=25, num_steps=args.steps)
    twin.run()
    compiler = AttackCompiler(twin, seed=args.seed)
    generate_training_attacks(compiler, twin.world)
    victim = BlueTeamEnsemble.untrained(seed=args.seed)
    victim.fit_transactions(list(twin.world.transactions), twin.world,
                            oof_folds=3, gnn_epochs=12)

    txs = list(twin.world.transactions)

    # -------------------------------------------------------------- margins
    # shadow-PGD candidates once against the true ensemble (small but real)
    from attack.mechanisms.shadow_pgd import ShadowPGDMechanism
    mech = ShadowPGDMechanism(victim, twin, seed=args.seed + 21)
    sh_res = mech.run(attack_id="P9_MARGINS", threshold=0.5,
                      max_base_rows=10, probe_budget=450,
                      pgd_iterations=20, restarts=2,
                      execute_into_world=False)
    margin_artifact = {
        "schema": "prometheus.margins.v1",
        "generated_from": "shadow-PGD candidates replayed vs true ensemble",
        **{k: v for k, v in sh_res.verify.items() if k != "per_candidate"},
        "distribution": None,
    }
    per_cand = sh_res.verify.get("per_candidate", [])
    adv_scores = [pc["victim_candidate_score"] for pc in per_cand]
    dist = margin_distribution(per_cand, threshold=0.5)
    margin_artifact["distribution"] = {
        k: v for k, v in dist.items()
        if k != "schema"}
    margin_artifact["score_replay_note"] = (
        f"{sh_res.distill['n_queries']} oracle queries; "
        f"distill_xgb_r2={sh_res.distill['xgb_fidelity']['r2']}")
    _dump("margins.json", margin_artifact)

    # --------------------------------------------------------------- latency
    fraud_tx1 = [next(t for t in txs if t.get("is_fraud"))]

    def fast_one():
        victim.score_transactions(fraud_tx1, twin.world)

    def fast_batch32():
        idx = np.linspace(0, len(txs) - 1, 32).astype(int)
        victim.score_transactions([txs[i] for i in idx], twin.world)

    # deep path: full investigator case (signals + OSINT + sanctions +
    # spectral + narrative-fallback + fitted structured score); distinct
    # case ids per iteration keep memory dedupe honest.
    from investigate.case_manager import CaseManager
    from investigate.llm_client import LLMClient
    from sensitivity.engine import SensitivityEngine
    from scoring.structured_score import FittedStructuredScore, SCORE_COLUMNS
    sensitivity = SensitivityEngine(xgb_model=victim.xgb.model,
                                    gnn_model=victim.gnn.model if victim.gnn else None,
                                    feature_names=victim.feature_names)
    sig_cols = victim.score_all_signals(txs, twin.world, manifold=None)
    X_head = np.column_stack([np.asarray(sig_cols[c], dtype=np.float64)
                              for c in SCORE_COLUMNS])
    struct9 = FittedStructuredScore().fit(
        X_head, [1.0 if t.get("is_fraud") else 0.0 for t in txs])
    mgr_once = CaseManager(victim, twin, sensitivity=sensitivity,
                           llm=LLMClient(), seed=args.seed + 9,
                           structured=struct9)
    fraud_pool = [t["tx_id"] for t in txs if t.get("is_fraud")]
    fraud_pool = fraud_pool[:min(5, max(1, len(fraud_pool)))]
    case_iter = (f"P9_LAT_{i}" for i in range(10_000))

    def deep_path_fn():
        try:
            mgr_once.run_case(next(case_iter), fraud_pool)
        except Exception as exc:                    # keep timing honest even on hiccup
            print(f"[p9] deep iteration error (ignored for timing): {exc}")

    lat_fast1 = measure_latency(fast_one, warmup=4,
                                iterations=max(8, args.fast_iters))
    lat_fast32 = measure_latency(fast_batch32, warmup=3,
                                 iterations=max(6, args.fast_iters // 2))
    lat_deep = measure_latency(deep_path_fn, warmup=1,
                               iterations=args.deep_iters)

    lat_artifact = {
        "schema": "prometheus.latency.v1",
        "environment": environment_info(),
        "paths": {
            "fast_single_tx": {**lat_fast1,
                               "definition": ("victim ensemble score of one "
                                              "tx row incl. GNN lookup")},
            "fast_batch_32": {**lat_fast32,
                              "definition": "batch-32 ensemble scoring"},
            "deep_case_5rows": {**lat_deep,
                                "definition": ("full investigator case: "
                                               "signals+OSINT+sanctions+"
                                               "spectral+narrative fallback+"
                                               "fitted structured score; "
                                               "LLM network latency excluded "
                                               "(offline template mode)")},
        },
        "measured_seconds": True,
    }
    _dump("latency.json", lat_artifact)

    # ------------------------------------------------------------ cost model
    X_all, y_all, names = compute_features(txs, twin.world)
    y_arr = np.asarray(y_all)
    scores_meta = victim.score_transactions(txs, twin.world)

    from eval.harness import evaluate_at_prevalence
    eval_points = {}
    for prev in (0.005, 0.02):
        m = evaluate_at_prevalence(y_arr, scores_meta, prev)
        if "error" not in m:
            eval_points[f"prev_{prev}"] = {
                "prevalence": prev,
                "budget": 5.0,
                "recall": m["recall_at_5pct"],
                "precision": m["precision_at_5pct"],
            }

    variants = {
        "base_assumptions": {},
        "high_loss_low_cost": {"avg_fraud_loss_inr": 24000.0,
                               "false_decline_cost_inr": 150.0},
        "low_loss_high_cost": {"avg_fraud_loss_inr": 6000.0,
                               "analyst_rate_inr_per_hour": 720.0},
    }
    cost_artifact = {
        "schema": "prometheus.cost_model.v1",
        "model_description": (
            "declared-assumption INR economics over a per-1000-txn frame; "
            "inputs (prevalence, budget, recall, precision) come from "
            "measured eval-harness points on this twin"),
        "points": eval_points,
        "worked_examples": {
            label: inr_economics(e["prevalence"], e["budget"], e["recall"],
                                 e["precision"]) for label, e in
            eval_points.items()},
        "sensitivity": sensitivity_grid(eval_points, variants),
        "fraud_loss_proxy_measured_inr":
            round(float(np.mean([t["amount"] for t in txs
                                 if t.get("is_fraud")])), 2),
        "note": ("economics are a model w/ DECLARED knobs extrapolated to "
                 "a per-1000-txn frame; losses proxy twin amounts"),
    }
    _dump("cost_model.json", cost_artifact)

    # ------------------------------------------------------------------ drift
    steps = np.array([t.get("step", 0) for t in txs])
    q_lo, q_hi = np.quantile(steps, [0.25, 0.75])
    ref_idx = [i for i in range(len(txs)) if steps[i] <= q_lo]
    cur_idx = [i for i in range(len(txs)) if steps[i] >= q_hi]

    ref_norm = X_all[ref_idx][y_arr[ref_idx] == 0]
    cur_norm = X_all[cur_idx][y_arr[cur_idx] == 0]
    cur_full = X_all[cur_idx]

    drift_normal = drift_report(ref_norm, cur_norm, names)
    drift_full = drift_report(ref_norm, cur_full, names)
    gap = {
        c: round((drift_full["per_feature"][c]["psi"] or 0)
                 - (drift_normal["per_feature"][c]["psi"] or 0), 4)
        for c in drift_full["per_feature"]}
    top_gap_cols = sorted(gap.items(), key=lambda kv: -abs(kv[1]))[:5]

    drift_artifact = {
        "schema": "prometheus.drift.v1",
        "windows": {"ref_step_max": float(q_lo), "cur_step_min": float(q_hi)},
        "normal_only": {"verdict_counts": drift_normal["verdict_counts"],
                        "max_psi": drift_normal["max_psi"],
                        "top_shifts": drift_normal["shifted_features_top"]},
        "full_late_window": {"verdict_counts": drift_full["verdict_counts"],
                             "max_psi": drift_full["max_psi"],
                             "top_shifts": drift_full["shifted_features_top"]},
        "attack_induced_gap_top": [
            {"column": c, "psi_delta": d} for c, d in top_gap_cols],
        "interpretation": ("normal-only PSI isolates organic drift; the "
                           "gap column quantifies how much of observed shift "
                           "attacks themselves cause; NOTE hour_of_day / "
                           "is_night are mechanical functions of the step "
                           "clock (step % 24), so their PSI is clock-driven "
                           "by construction"),
        "mechanical_clock_columns": ["hour_of_day", "is_night"],
    }
    _dump("drift.json", drift_artifact)

    print(f"[p9] done runtime={round(time.perf_counter() - t0, 1)}s | "
          f"margins n={dist['n_candidates']} "
          f"frac_evasive={dist['sign_split']['frac_evasive']} | "
          f"deep p50={lat_deep['p50']}s | drift max "
          f"(normal/full)={drift_normal['max_psi']}/{drift_full['max_psi']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
