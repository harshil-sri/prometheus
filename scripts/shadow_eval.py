"""
shadow_eval.py — Phase 4 gate: shadow-gradient red teaming, measured.

Protocol (deterministic, seed=42):
  1. victim_v0 = ensemble trained on twin + benchmark training attacks
  2. ShadowPGDMechanism.run against v0
       distill (fidelity reported) → PGD → verify → materialize evasions
  3. evasion_rate_before = v0 confirm rate on its OWN attack rows
  4. victim_v1 = retrain INCLUDING v0's confirmed-evasion rows as hard
     negatives (mechanism-tagged shadow_pgd; two-axis holdout respected —
     only trainable attack types exist in this world)
  5. Re-run the SAME shadow cycle parameterization against v1 →
     evasion_rate_after. Adversarial training claim = after < before.

Writes artifacts/shadow_eval.json:
  fidelity (xgb+mlp), before/after verify reports, deltas, seeds,
  runtime. No "certified" anywhere.
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
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (ROOT, SRC, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _ensure_utf8_stdout import ensure_utf8_stdout  # noqa: E402
ensure_utf8_stdout()

from twin.twin import FinancialDigitalTwin                       # noqa: E402
from attack.compiler import AttackCompiler                        # noqa: E402
from attack.benchmark_attacks import generate_training_attacks      # noqa: E402
from attack.mechanisms.shadow_pgd import ShadowPGDMechanism         # noqa: E402
from blue.ensemble import BlueTeamEnsemble                          # noqa: E402


def _build_victim(twin: FinancialDigitalTwin, seed: int) -> BlueTeamEnsemble:
    compiler = AttackCompiler(twin, seed=seed)
    generate_training_attacks(compiler, twin.world)
    blue = BlueTeamEnsemble.untrained(seed=seed)
    blue.fit_transactions(list(twin.world.transactions), twin.world,
                          oof_folds=3, gnn_epochs=20)
    return blue


def main() -> int:
    ap = argparse.ArgumentParser(description="Shadow-gradient eval (P4)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--accounts", type=int, default=260)
    ap.add_argument("--steps", type=int, default=45)
    ap.add_argument("--probes", type=int, default=900)
    ap.add_argument("--pgd-iterations", type=int, default=30)
    args = ap.parse_args()
    t0 = time.perf_counter()

    # --- shared world; adversarial-training world grows monotonically ------
    twin = FinancialDigitalTwin(seed=args.seed, num_accounts=args.accounts,
                                num_merchants=60, num_devices=90,
                                num_ip_blocks=30, num_steps=args.steps)
    twin.run()

    # -------- Round A: victim v0 ----------------------------------------
    v0 = _build_victim(twin, args.seed)
    mech0 = ShadowPGDMechanism(v0, twin, seed=args.seed)
    res0 = mech0.run(attack_id="SHADOW_PGD_R1", threshold=0.5,
                     probe_budget=args.probes,
                     pgd_iterations=args.pgd_iterations)
    print(f"[shadow] v0 fidelity xgb={res0.distill['xgb_fidelity']} "
          f"mlp={res0.distill['mlp_fidelity']}")
    print(f"[shadow] v0 verify: {res0.verify['n_confirmed']} confirmed / "
          f"{res0.verify['n_false_hope']} false-hope / "
          f"evasion={res0.verify['evasion_rate']}")

    # -------- Round B: retrain on (history incl. v0 shadow rows) --------
    v1 = BlueTeamEnsemble.untrained(seed=args.seed + 1)
    diag1 = v1.fit_transactions(list(twin.world.transactions), twin.world,
                                oof_folds=3, gnn_epochs=20)

    mech1 = ShadowPGDMechanism(v1, twin, seed=args.seed + 5)
    res1 = mech1.run(attack_id="SHADOW_PGD_R2", threshold=0.5,
                     probe_budget=args.probes,
                     pgd_iterations=args.pgd_iterations,
                     execute_into_world=False)   # measurement-only vs v1
    after_str = res1.verify['evasion_rate']
    print(f"[shadow] v1 verify: {res1.verify['n_confirmed']} confirmed / "
          f"evasion={after_str}")

    elapsed = time.perf_counter() - t0

    # -------- summary + artifact -----------------------------------------
    before = res0.verify["evasion_rate"]
    after = res1.verify["evasion_rate"]
    improved_defense = after < before or (
        after == before and res1.verify["margin_estimate_mean"] is not None
        and res0.verify["margin_estimate_mean"] is not None
        and res1.verify["margin_estimate_mean"] >
        res0.verify["margin_estimate_mean"])

    artifact = {
        "schema": "prometheus.shadow_eval.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "seed": args.seed,
        "platform": {"python": platform.python_version(),
                     "os": platform.platform(), "cpu_only": True},
        "runtime_seconds": round(elapsed, 2),
        "config": {"accounts": args.accounts, "steps": args.steps,
                   "probes": args.probes,
                   "pgd_iterations": args.pgd_iterations},
        "round_A_victim_v0": {
            "distillation": res0.distill,
            "verify": res0.verify,
            "materialized_rows": res0.n_materialized,
            "trajectory_id": res0.trajectory_id,
        },
        "retrain_diag_v1": {
            k: diag1.get(k) for k in ("oof_used", "calibration_method",
                                      "graph_nodes", "folds")
        },
        "round_B_victim_v1": {
            "distillation": res1.distill,
            "verify_measurement_only": res1.verify,
        },
        "adversarial_training_claim": {
            "evasion_rate_before": round(before, 4),
            "evasion_rate_after": round(after, 4),
            "improved": bool(improved_defense),
            "note": ("improved can also come from wider margins when rates "
                     "tie at zero; margins are ESTIMATES"),
            "margin_estimates": {
                "before": res0.verify["margin_estimate_mean"],
                "after": res1.verify["margin_estimate_mean"],
            },
        },
        "mechanism_axis_tagged": "shadow_pgd",
    }
    banned = "certified"
    assert banned not in json.dumps(artifact).lower(), \
        "the word 'certified' must never appear in margin reporting"

    out_dir = os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "shadow_eval.json")
    with open(out_path, "w") as f:
        json.dump(artifact, f, indent=2, default=str)
    print(f"[shadow] evasion {before:.3f} -> {after:.3f} "
          f"(improved={improved_defense}) | runtime {elapsed:.1f}s")
    print(f"[shadow] artifact written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
