"""
attribution_eval.py — Phase 7: deterministic mechanism × evidence-source
attribution exhibit (artifacts/attribution.json).

Builds ONE deterministic forensic world where every attack-generation family
leaves scored, mechanism-tagged transactions:
  rule_compiler      — fresh compilations of the benchmark specs
  genetic            — typology-like priors (relational-camouflage family)
  shadow_pgd         — PGD evasions executed into the world (distill once)
  llm_strategist     — compiled fallback variant specs
  protocol_structural— PCAT-naive RC-1..RC-5 attempts (their own agentic world)

Each caught transaction is then attributed to its evidence sources (XGB / GNN
model signals + OSINT dossiers / sanctions WATCH_HIT on the sender account)
and folded into a matrix. Re-running yields a byte-identical artifact (no
wall-clock fingerprints inside build_attribution_matrix).
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
from eval.ood_matrix import build_ood_matrix                                # noqa: E402
from eval.attribution import (                                            # noqa: E402
    build_attribution_matrix,
    combine_matrices,
)
from investigate.case_manager import CaseManager                            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Attribution exhibit (P7)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--accounts", type=int, default=140)
    ap.add_argument("--steps", type=int, default=35)
    ap.add_argument("--k-per-cell", type=int, default=1)
    ap.add_argument("--out", type=str,
                    default=os.path.join(ROOT, "artifacts", "attribution.json"))
    args = ap.parse_args()
    t0 = time.perf_counter()

    # Determinism law: pin every RNG before world construction (the sanctions
    # fixture providers draw from the global `random` module).
    import random
    random.seed(args.seed)
    np.random.seed(args.seed)
    try:
        import torch
        torch.manual_seed(args.seed)
    except Exception:                                    # noqa: BLE001
        pass

    # ------------------------------------------------------------- twin ---
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
    case_mgr = CaseManager(ensemble=victim, twin=twin, seed=args.seed)

    # ----------------------------------------------- shadow candidates ----
    from attack.mechanisms.shadow_pgd import ShadowPGDMechanism            # noqa: E402
    from shadow.distill import collect_probes, distill_surrogates           # noqa: E402
    from shadow.pgd import get_domains, ProjectedPGD                        # noqa: E402
    from blue.features import compute_features                              # noqa: E402

    mech_sh = ShadowPGDMechanism(victim, twin, seed=args.seed)
    probes = collect_probes(twin.world.transactions, mech_sh._victim_oracle(),
                            world_state=twin.world, max_probes=500,
                            seed=args.seed)
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
        pgd = ProjectedPGD(shadow_net, domains,
                           seed=args.seed + len(cand_matrix),
                           iterations=18, restarts=2)
        cands = pgd.optimize(np.asarray(X_t, dtype=np.float64), threshold=0.5)
        cand_matrix.append([c.x_projected.tolist() for c in cands])
        base_rows_all.append(fraud_rows)
    print(f"[attr] shadow distilled (xgb r2={dres.xgb_fidelity['r2']:.3f}, "
          f"mlp r2={dres.mlp_fidelity['r2']:.3f})")

    # ----------------------------------------------- multi-mechanism world
    llm_weakness = {"weakness": "relational camouflage",
                    "target_model": "GNN",
                    "suggested_variants": ["more_intermediaries",
                                           "temporal_spreading",
                                           "amount_splitting"]}
    ood = build_ood_matrix(
        victim=victim, twin=twin, compiler_of_twin=compiler,
        holdout_spec=holdout, k_per_cell=args.k_per_cell, seed=args.seed,
        shadow_candidates=cand_matrix,
        shadow_base_rows=base_rows_all[0] if base_rows_all else None,
        llm_weakness=llm_weakness,
    )
    twin_matrix = build_attribution_matrix(
        twin.world, victim, case_manager=case_mgr,
        threshold=0.5, seed=args.seed)

    # ----------------------------------------------- protocol_structural
    from twin.core import WorldState                                     # noqa: E402
    from attack.protocol_attacks import run_t9_case                       # noqa: E402
    w_ag = WorldState(seed=2026)
    for rc in ("RC-1", "RC-2", "RC-3", "RC-4", "RC-5"):
        run_t9_case(w_ag, seed=3, rc_class=rc, defense_builder=None)
    ag_matrix = build_attribution_matrix(
        w_ag, victim, case_manager=None, threshold=0.5, seed=args.seed)

    combined = combine_matrices(
        {"twin": twin_matrix, "agentic": ag_matrix})

    combined["generator"] = (
        f"scripts/attribution_eval.py (seed={args.seed}, "
        f"accounts={args.accounts}, steps={args.steps}, "
        f"k_per_cell={args.k_per_cell})")
    combined["platform"] = {"python": platform.python_version(),
                            "os": platform.platform(), "cpu_only": True}
    combined["note"] = (
        "cell = number of CAUGHT attacks (ensemble score>=0.5) whose sender "
        "also triggered that evidence source; OSINT/sanctions are per-account "
        "flags from the CaseManager fixture providers, so coverage follows "
        "real fixture reach. protocol_structural rows come from the PCAT-naive "
        "agentic world, scored by the same ensemble.")
    combined["ood_fingerprint"] = ood["fingerprint"]

    out_dir = os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(combined, f, indent=2, default=str)

    print(f"[attr] matrix fingerprint {combined['fingerprint']}")
    for m in combined["mechanisms"]:
        row = " ".join(f"{s}:{combined['matrix'][m].get(s, 0)}"
                       for s in combined["sources"])
        print(f"[attr] {m:20s} n={combined['margins'].get(m, 0):4d}  {row}")
    print(f"[attr] artifact written: {args.out} "
          f"({round(time.perf_counter() - t0, 1)}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())