"""
timeline_eval.py — Phase 7: deterministic Blind-Spot timeline artifact.

Runs N decontaminated feedback cycles at fixed seeds on one small twin and
appends each Blind-Spot Report to artifacts/feedback_timeline.json — the
committed baseline served by /api/timeline under the session row D^-base.
Re-running produces a BYTE-IDENTICAL artifact (no wall-clock fields: the
Determinism law) so the gate can `cmp` it safely.
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

from twin.twin import FinancialDigitalTwin                                # noqa: E402
from attack.compiler import AttackCompiler                                 # noqa: E402
from attack.benchmark_attacks import (                                    # noqa: E402
    HELD_OUT_ATTACKS, TRAINABLE_ATTACKS, generate_training_attacks,
)
from blue.ensemble import BlueTeamEnsemble                                  # noqa: E402
from sensitivity.engine import SensitivityEngine                            # noqa: E402
from feedback.loop import FeedbackLoop                                      # noqa: E402
from feedback.timeline import FeedbackTimeline, summarize_cycle             # noqa: E402

import numpy as np                                                        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic Blind-Spot timeline")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cycles", type=int, default=4)
    ap.add_argument("--accounts", type=int, default=600)
    ap.add_argument("--steps", type=int, default=60)
    ap.add_argument("--gnn-epochs", type=int, default=15)
    ap.add_argument("--out", type=str,
                    default=os.path.join(ROOT, "artifacts",
                                         "feedback_timeline.json"))
    args = ap.parse_args()
    t0 = time.perf_counter()

    # Determinism law: pin every RNG before world construction so a
    # re-run reproduces a byte-identical artifact.
    import random
    random.seed(args.seed)
    np.random.seed(args.seed)
    try:
        import torch
        torch.manual_seed(args.seed)
    except Exception:                                    # noqa: BLE001
        pass

    twin = FinancialDigitalTwin(seed=args.seed, num_accounts=args.accounts,
                                num_merchants=40, num_devices=40,
                                num_ip_blocks=10, num_steps=args.steps)
    twin.run()
    compiler = AttackCompiler(twin, seed=args.seed)
    generate_training_attacks(compiler, twin.world)

    blue = BlueTeamEnsemble.untrained(seed=args.seed)
    blue.fit_transactions(list(twin.world.transactions), twin.world,
                          oof_folds=3, gnn_epochs=args.gnn_epochs)
    sensitivity = SensitivityEngine(xgb_model=blue.xgb.model,
                                    gnn_model=blue.gnn.model if blue.gnn else None,
                                    feature_names=blue.feature_names)

    loop = FeedbackLoop(twin, compiler, blue, sensitivity, seed=args.seed)
    timeline = FeedbackTimeline(args.out)
    trainable = sorted(TRAINABLE_ATTACKS)
    held_out = sorted(HELD_OUT_ATTACKS)

    from blue.splits import lock_holdout
    for i in range(args.cycles):
        demo_seed = args.seed + 1009 * (i + 1)
        holdout = lock_holdout(seed=demo_seed,
                               held_out_types=tuple(held_out))
        report = loop.run_cycle(
            attack_ids=trainable,
            held_out_ids=held_out,
            holdout_spec=holdout,
            n_instances=2,
        )
        idx = timeline.append(summarize_cycle(
            report, seed=demo_seed, source="timeline_eval"))
        print(f"[timeline] cycle {i + 1}: "
              f"recall {report['recall_before']:.2%} -> "
              f"{report['recall_after']:.2%} · blind_spot="
              f"{report['blind_spot']} · fixes="
              f"{report['generated_fixes']} · idx={idx}")

    blob = {
        "schema": timeline.entries() and "prometheus.feedback_timeline.v1",
        "generator":
            f"scripts/timeline_eval.py (cycles={args.cycles}, "
            f"accounts={args.accounts}, steps={args.steps}, "
            f"gnn_epochs={args.gnn_epochs})",
        "seed": args.seed,
        "platform": {"python": platform.python_version(),
                     "os": platform.platform(), "cpu_only": True},
        "entries": timeline.entries(),
    }
    with open(args.out, "w") as f:
        json.dump(blob, f, indent=2, default=str)

    for e in timeline.entries():
        g = e.get("generalization_recall_unseen_generator")
        print(f"[timeline] #{e['idx']} {e['source']} "
              f"{e['recall_before']:.2f}->{e['recall_after']:.2f} "
              f"gen={g} spot={e['blind_spot']} fixes={e['generated_fixes']}")
    print(f"[timeline] artifact written: {args.out} "
          f"({len(timeline.entries())} entries, "
          f"{round(time.perf_counter() - t0, 1)}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())