"""
fidelity_eval.py — Phase 7 gate: 3-layer fidelity + dual critique exhibit
(Phase 6: CTGAN AND a TabDDPM-style diffusion critic).

Protocol (deterministic seed 42):
  1. twin + benchmark attacks; temporal train slice for models
  2. real-normal tabular matrix from the training slice (label==0 ONLY)
  3. CTGANSynthesizer (sdv) fit on real-normal → synthetic normals
     (the first critique generator; fitting on normals only keeps labels clean)
  4. TabDiffusionCritic (src/eval/diffusion_tab.py — small Gaussian DDPM with
     MLP denoiser + sinusoidal time embeddings, EmDT-aligned but scaled down)
     fit on the SAME real-normal matrix → synthetic normals
  5. Layer 1 statistical per critic: twin-normals vs synth-normals per-column
     Wasserstein/KS/TV distances
  6. Layer 2 behavioral: networkx graph profile of the whole log +
     recurring-salary cadence check against the twin's declared mechanics
  7. Layer 3 adversarial per critic: XGB discriminator-trap (real vs synth
     normals) + NormalcyManifold score-transfer correlation (fit-on-real vs
     fit-on-synth, scored on held-out REAL rows)

Schema prometheus.fidelity_report.v2 (additive): the v1 `layers` block is kept
unchanged (CTGAN-era), plus a `critics` block carrying {ctgan, diffusion} L1+L3
bundles and generator diagnostics. Declared thresholds, no invented composite.

Citations carried verbatim in the artifact:
  Kuo, E.-Y. & Motsch, S. (2026). EmDT: Embedding Diffusion Transformer for
      Tabular Data Generation in Fraud Detection. arXiv:2603.13566.
  Sajja, B. (2026). Synthetic Tabular Generators Fail to Preserve Behavioral
      Fraud Patterns: A Benchmark on Temporal, Velocity, and Multi-Account
      Signals. arXiv:2604.13125.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
import warnings

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
from blue.features import compute_features                                   # noqa: E402
from eval.fidelity import (                                                  # noqa: E402
    statistical_layer, behavioral_layer, adversarial_layer,
    build_fidelity_report,
)
from eval.diffusion_tab import DiffusionConfig, TabDiffusionCritic             # noqa: E402

CITATIONS = [
    "Kuo, E.-Y. & Motsch, S. (2026). EmDT: Embedding Diffusion Transformer for "
    "Tabular Data Generation in Fraud Detection. arXiv:2603.13566.",
    "Sajja, B. (2026). Synthetic Tabular Generators Fail to Preserve Behavioral "
    "Fraud Patterns: A Benchmark on Temporal, Velocity, and Multi-Account "
    "Signals. arXiv:2604.13125.",
]


def main() -> int:
    ap = argparse.ArgumentParser(description="3-layer fidelity eval (P7)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--accounts", type=int, default=260)
    ap.add_argument("--steps", type=int, default=70)
    ap.add_argument("--ctgan-epochs", type=int, default=60)
    ap.add_argument("--synth-rows", type=int, default=4000)
    ap.add_argument("--critic-rows", type=int, default=4000,
                    help="real-normal rows fed to each critic generator")
    ap.add_argument("--diffusion-epochs", type=int, default=300)
    ap.add_argument("--diffusion-timesteps", type=int, default=100)
    ap.add_argument("--diffusion-batch", type=int, default=256)
    ap.add_argument("--skip-diffusion", action="store_true",
                    help="CTGAN-only run (keeps schema v1)")
    args = ap.parse_args()
    t0 = time.perf_counter()

    # ------------------------------------------------------------- world --
    twin = FinancialDigitalTwin(seed=args.seed, num_accounts=args.accounts,
                                num_merchants=50, num_devices=80,
                                num_ip_blocks=25, num_steps=args.steps)
    twin.run()
    compiler = AttackCompiler(twin, seed=args.seed)
    generate_training_attacks(compiler, twin.world)

    txs = list(twin.world.transactions)
    victim = BlueTeamEnsemble.untrained(seed=args.seed)
    victim.fit_transactions(txs, twin.world, oof_folds=3, gnn_epochs=15)

    X_all, y_all, names = compute_features(txs, twin.world)
    X_all = np.asarray(X_all, dtype=np.float64)
    normal_mask = np.asarray(y_all) == 0
    X_real_normal = X_all[normal_mask]

    # ------------------------------------------------------ CTGAN exhibit --
    import pandas as pd
    from sdv.single_table import CTGANSynthesizer
    from sdv.metadata import SingleTableMetadata

    critic_rows = min(int(normal_mask.sum()), args.critic_rows)
    X_critic = X_real_normal[:critic_rows]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        meta_md = SingleTableMetadata()
        meta_md.detect_from_dataframe(
            pd.DataFrame(X_critic, columns=list(names)))
        synth_model = CTGANSynthesizer(meta_md,
                                       epochs=args.ctgan_epochs,
                                       verbose=False)
        synth_model.fit(pd.DataFrame(X_critic, columns=list(names)))
        synth_df = synth_model.sample(num_rows=args.synth_rows)
    X_synth = np.nan_to_num(synth_df.to_numpy(dtype=np.float64),
                            nan=0.0, posinf=1e6, neginf=-1e6)
    ctgan_elapsed = round(time.perf_counter() - t0, 1)
    print(f"[fidelity] CTGAN trained+sampled ({args.synth_rows} rows, "
          f"{args.ctgan_epochs} epochs) in {ctgan_elapsed}s")

    # ------------------------------------------- diffusion critic exhibit --
    dfg = DiffusionConfig(T=args.diffusion_timesteps)
    diff_critic = TabDiffusionCritic(dim=len(names), seed=args.seed,
                                     config=dfg)
    diff_critic.fit(X_critic, epochs=args.diffusion_epochs,
                    batch_size=args.diffusion_batch)
    X_diff = diff_critic.sample(args.synth_rows)
    diff_elapsed = round(time.perf_counter() - t0, 1)
    print(f"[fidelity] TabDiffusionCritic trained+sampled ({args.synth_rows} "
          f"rows, {args.diffusion_epochs} epochs, T={args.diffusion_timesteps}) "
          f"loss={diff_critic.loss_history[-1]} in {diff_elapsed}s")

    # ------------------------------------------------------------ layers --
    stat = statistical_layer(X_real_normal, X_synth, names)
    behav = behavioral_layer(twin, txs)
    adv = adversarial_layer(victim, X_real_normal, X_synth, seed=args.seed,
                            feature_names=names)

    meta_info = {
        "seed": args.seed,
        "n_real_normals": int(normal_mask.sum()),
        "n_synth_normals": int(len(X_synth)),
        "features": len(names),
        "critic_rows": int(len(X_critic)),
        "ctgan_epochs": args.ctgan_epochs,
        "runtime_seconds": round(time.perf_counter() - t0, 2),
        "platform": {"python": platform.python_version(),
                     "os": platform.platform(), "cpu_only": True},
        "critique_generator": f"sdv CTGANSynthesizer "
                              f"(epochs={args.ctgan_epochs})",
        "citations": CITATIONS,
    }

    if args.skip_diffusion:
        critics = None
        meta_info["critics_ran"] = ["ctgan"]
        report = build_fidelity_report(stat, behav, adv, meta_info)
    else:
        stat_diff = statistical_layer(X_real_normal, X_diff, names)
        adv_diff = adversarial_layer(victim, X_real_normal, X_diff,
                                     seed=args.seed, feature_names=names)
        critics = {
            "ctgan": {
                "generator": f"sdv CTGANSynthesizer (epochs={args.ctgan_epochs})",
                "statistical": stat,
                "adversarial": adv,
            },
            "diffusion": {
                "generator": ("TabDiffusionCritic — Gaussian DDPM, MLP "
                              "denoiser + sinusoidal time embeddings "
                              "(EmDT-aligned, arXiv:2603.13566; scaled-down)"),
                "statistical": stat_diff,
                "adversarial": adv_diff,
                "diagnostics": diff_critic.diagnostics(),
            },
        }
        meta_info["critics_ran"] = ["ctgan", "diffusion"]
        meta_info["diffusion_epochs"] = args.diffusion_epochs
        meta_info["diffusion_timesteps"] = args.diffusion_timesteps
        report = build_fidelity_report(stat, behav, adv, meta_info,
                                       critics=critics)
    fp_payload = {"schema": report["schema"], "seed": args.seed,
                  "n_real": int(normal_mask.sum()),
                  "n_synth": len(X_synth), "epochs": args.ctgan_epochs,
                  "critics": sorted(meta_info["critics_ran"])}
    report["fingerprint"] = hashlib.sha256(json.dumps(
        fp_payload, sort_keys=True).encode()).hexdigest()[:16]

    out_dir = os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "fidelity_report.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"[fidelity] statistical: w_ratio_median="
          f"{stat['wasserstein_ratio_median']} ks_med={stat['ks_stat_median']}"
          f" pass={report['all_statistical_flags_passed']}")
    print(f"[fidelity] behavioral: salary on-cadence ratio="
          f"{behav['income_cycles']['on_cadence_ratio']} | clustering="
          f"{behav['graph']['avg_clustering']} | fraud-IAT-gap median="
          f"{behav['iat_overlap']['fraud_iat_median_gap']}")
    print(f"[fidelity] adversarial CTGAN: trap AUC="
          f"{adv['critic_trap']['auc']} "
          f"survived={adv['critic_trap']['survived_band']} | manifold"
          f"-transfer rho={adv['manifold_transfer']['spearman_score_correlation']}")
    if critics is not None:
        print(f"[fidelity] adversarial diffusion: trap AUC="
              f"{adv_diff['critic_trap']['auc']} "
              f"survived={adv_diff['critic_trap']['survived_band']} | "
              f"manifold-transfer "
              f"rho={adv_diff['manifold_transfer']['spearman_score_correlation']}")
    print(f"[fidelity] artifact written (schema {report['schema']}, "
          f"critics={meta_info['critics_ran']}): {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
