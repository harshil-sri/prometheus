"""
mechanism_eval.py — Phase 5 gate: mechanism zoo + OOD matrix + exploitability.

Protocol (deterministic seed):
  1. twin + victim v0
  2. GA optimization (query-budgeted); elites materialized, tagged 'genetic'
  3. LLM-strategist (offline fallback unless PROMETHEUS_LLM_* configured);
     provenance reported per variant
  4. RL stretch executes under its PRE-REGISTERED kill criterion and ships
     an honest negative result when it loses to the heuristic baseline
  5. Mechanism-OOD matrix {rule_compiler, genetic, shadow_pgd,
     llm_strategist} x {A1..A6} on frozen fingerprints (+ holdout fp)
  6. StrategyRegistry records every strategy; exploitability = worst-case

Writes artifacts/ood_matrix.json + artifacts/strategy_registry.json.
"""

from __future__ import annotations

import argparse
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
from blue.splits import lock_holdout                                        # noqa: E402
from feedback.registry import StrategyRegistry, exploitability_estimate     # noqa: E402
from eval.ood_matrix import build_ood_matrix                                # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Mechanism zoo eval (P5)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--accounts", type=int, default=140)
    ap.add_argument("--steps", type=int, default=35)
    ap.add_argument("--k-per-cell", type=int, default=2)
    ap.add_argument("--ga-budget", type=int, default=40)
    ap.add_argument("--rl-episodes", type=int, default=50)
    ap.add_argument("--rl-timebudget", type=float, default=60.0)
    args = ap.parse_args()
    t0 = time.perf_counter()

    # --------------------------------------------------------------- setup
    twin = FinancialDigitalTwin(seed=args.seed, num_accounts=args.accounts,
                                num_merchants=40, num_devices=60,
                                num_ip_blocks=20, num_steps=args.steps)
    twin.run()
    compiler = AttackCompiler(twin, seed=args.seed)
    generate_training_attacks(compiler, twin.world)

    victim = BlueTeamEnsemble.untrained(seed=args.seed)
    victim.fit_transactions(list(twin.world.transactions), twin.world,
                            oof_folds=3, gnn_epochs=15)
    holdout = lock_holdout(seed=args.seed, held_out_types=("A2", "A5"))

    registry = StrategyRegistry()

    # ---------------------------------------------------------- GA
    from attack.mechanisms.genetic import GAOptimizer
    ga = GAOptimizer(victim, twin, seed=args.seed,
                     budget_queries=args.ga_budget, population=8,
                     max_generations=5)
    ga_res = ga.optimize()
    registry.register("GA_specspace", "genetic",
                      meta={"population": 8, "max_generations": 5},
                      metrics={"best_peak": ga_res.best_peak_score,
                               "initial_best": ga_res.initial_best_peak,
                               "queries": ga_res.budget_used,
                               "improved": bool(ga_res.best_peak_score <
                                                ga_res.initial_best_peak)})
    print(f"[zoo] GA best peak {ga_res.initial_best_peak:.3f} -> "
          f"{ga_res.best_peak_score:.3f} "
          f"({ga_res.budget_used} queries, "
          f"{ga_res.n_materialized} elite rows)")

    # ------------------------------------------------- LLM strategist
    from attack.mechanisms.llm_strategist import LLMStrategist
    strat = LLMStrategist(seed=args.seed)
    weakness_hint = {"weakness": "relational camouflage",
                     "target_model": "GNN",
                     "suggested_variants": ["more_intermediaries",
                                            "temporal_spreading",
                                            "amount_splitting"]}
    variants = strat.generate(weakness_hint, n_variants=4)
    origins = [v.origin for v in variants]
    print(f"[zoo] LLM strategist origins: {origins}")
    registry.register("LLM_fallback_mix", "llm_strategist",
                      meta={"n_variants": len(variants)},
                      metrics={"origin_counts": {
                          "llm": origins.count("llm"),
                          "fallback": origins.count("fallback")}})

    # ------------------------------------------------------ shadow prep
    # one distillation; per-trainable-type PGD candidate sets (replay-ready)
    from attack.mechanisms.shadow_pgd import ShadowPGDMechanism
    from shadow.distill import collect_probes, distill_surrogates
    from shadow.pgd import get_domains, ProjectedPGD
    from blue.features import compute_features

    mech_sh = ShadowPGDMechanism(victim, twin, seed=args.seed)
    probes = collect_probes(twin.world.transactions,
                            mech_sh._victim_oracle(),
                            world_state=twin.world,
                            max_probes=700, seed=args.seed)
    _surr, shadow_net, dres = distill_surrogates(probes, seed=args.seed,
                                                 mlp_epochs=250)
    domains = get_domains(victim.feature_names)

    trainable_order = ["A1", "A3", "A4", "A6"]
    cand_matrix: list = []
    base_rows_all: list = []
    for atype in trainable_order:
        fraud_rows = [t for t in twin.world.transactions
                      if t.get("is_fraud") and
                      str(t.get("attack_id", "")).startswith(atype)][:6]
        if not fraud_rows:
            fraud_rows = [t for t in twin.world.transactions
                          if t.get("is_fraud")][:6]
        X_t, _, _ = compute_features(fraud_rows, twin.world)
        pgd = ProjectedPGD(shadow_net, domains, seed=args.seed + len(cand_matrix),
                           iterations=18, restarts=2)
        cands = pgd.optimize(np.asarray(X_t, dtype=np.float64), threshold=0.5)
        cand_matrix.append([c.x_projected.tolist() for c in cands])
        base_rows_all.append(fraud_rows)
    registry.register("ShadowPGD_replay_pool", "shadow_pgd",
                      meta={"per_type_rows": [len(r) for r in base_rows_all]},
                      metrics={"distill_xgb_r2": dres.xgb_fidelity["r2"],
                               "distill_mlp_r2": dres.mlp_fidelity["r2"]})
    print(f"[zoo] shadow candidates per type: "
          f"{[len(c) for c in cand_matrix]}")

    # ------------------------------------------------------- RL stretch
    from attack.mechanisms.rl_stretch import run_rl_stretch
    rl = run_rl_stretch(victim, twin, seed=args.seed + 13,
                        episodes=args.rl_episodes,
                        time_budget_s=args.rl_timebudget)
    registry.register("DQN_rl_stretch", "rl_stretch",
                      meta={"pre_registered": rl.criterion},
                      metrics={"episodes_run": rl.episodes_run,
                               "rl_best_mean_evasion":
                                   rl.rl_best_mean_evasion,
                               "heuristic_baseline": rl.heuristic_baseline,
                               "shipped": rl.shipped})
    verdict = "SHIPPED" if rl.shipped else \
        "NEGATIVE RESULT (criterion failed — honest rigor artifact)"
    print(f"[zoo] RL stretch: {verdict} | {rl.reason}")

    # --------------------------------------------------------- OOD matrix
    artifact = build_ood_matrix(
        victim=victim, twin=twin, compiler_of_twin=compiler,
        holdout_spec=holdout, k_per_cell=args.k_per_cell, seed=args.seed,
        shadow_candidates=cand_matrix,
        shadow_base_rows=base_rows_all[0] if base_rows_all else None,
        llm_weakness=weakness_hint,
    )

    exploitable = exploitability_estimate(artifact["rates"])
    artifact["exploitability"] = exploitable
    artifact["runtime_seconds"] = round(time.perf_counter() - t0, 2)
    artifact["platform"] = {"python": platform.python_version(),
                            "os": platform.platform(), "cpu_only": True}
    artifact["rl_stretch"] = {
        "episodes_run": rl.episodes_run,
        "rl_best_mean_evasion": rl.rl_best_mean_evasion,
        "heuristic_baseline": rl.heuristic_baseline,
        "shipped": rl.shipped,
        "reason": rl.reason,
        "pre_registered_criterion": rl.criterion,
    }
    artifact["llm_strategist_origins"] = origins

    out_dir = os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "ood_matrix.json"), "w") as f:
        json.dump(artifact, f, indent=2, default=str)
    with open(os.path.join(out_dir, "strategy_registry.json"), "w") as f:
        json.dump({"manifest": registry.manifest()}, f, indent=2,
                  default=str)

    print(f"[zoo] fingerprint {artifact['fingerprint']}")
    for m, cell in artifact["rates"].items():
        row = " ".join(f"{t}:{cell.get(t, float('nan')):.2f}"
                       if isinstance(cell.get(t), float) else f"{t}:--"
                       for t in ("A1", "A2", "A3", "A4", "A5", "A6"))
        print(f"[zoo] {m:16s} {row}")
    print(f"[zoo] worst-case detection "
          f"{exploitable['overall_worst_case_detection']} | "
          f"exploitability {exploitable['overall_exploitability']}")
    print(f"[zoo] artifacts written (ood_matrix.json, strategy_registry.json)"
          f" runtime {artifact['runtime_seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
