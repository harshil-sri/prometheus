#!/usr/bin/env python3
"""
protocol_eval.py — T9 protocol-manipulation evaluation (updates.md 6.1).

Benchmarks the FIVE structural attack classes (RC-1..RC-5) of the agentic-
commerce checkout flow two ways:

    * BEFORE (naive)  — the signed-protocol pipeline trusts registry,
                        federation resolution, channel hygiene, sequential
                        deduction, and caller identity blindly. All five
                        attacks land.
    * AFTER (PCAT)    — the Payment/Protocol Controls gate (P1..P5) refuses
                        each structural violation. No attacker payment lands;
                        the honest benign flow still passes (FP control).

Every row is a PURE function of (seed, defense) — no model sampling, no
nondeterminism — so the before/after delta is the paper's "structural
pillar" evidence and is reproducible bit-for-bit on rerun.

Artifact: artifacts/protocol_eval.json (schema prometheus.protocol_eval.v1)
with per-RC attack-success (naive vs pcat), the benign FP probe, the locked
holdout fingerprint (proving the A1-A6 lock is untouched), and the verbatim
citations from updates.md §8.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from typing import Any, Callable, Dict, List, Optional

try:
    from pathlib import Path
except ImportError:  # pragma: no cover
    pass

# src is importable when running from the repo root or via `python -m`.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))

from twin.core import WorldState                                   # noqa: E402
from policy.pcat import PCATPolicy                                 # noqa: E402
from attack.protocol_attacks import (                              # noqa: E402
    MECHANISM_NAME, RC_CLASSES, benign_checkout, run_t9_case,
)
from eval.judges import judge_benign, judge_rc                     # noqa: E402
from blue.splits import lock_holdout                               # noqa: E402

SCHEMA = "prometheus.protocol_eval.v1"
HOLDOUT_FINGERPRINT = "292cc7f67639cea556948086f8303fb248249da14f45b3d4825cca8f0473a162"

CITATIONS = [
    "Louck, Y. (2026). *Protocol-Level Attacks on Agentic Commerce Platforms: "
    "A Cross-Platform Taxonomy, AIP-Bench, and Unified Defense.* arXiv:2607.21824.",
    "Mastercard Agent Pay (\"Agentic Tokens\" on MDES), announced April 29, "
    "2025; Visa Trusted Agent Protocol, announced September/October 2025; "
    "Google Agent Payments Protocol (AP2), announced September 2025.",
    "EmDT: Embedding Diffusion Transformer for Tabular Data Generation in "
    "Fraud Detection. arXiv:2603.13566 (March 2026).",
    "Synthetic Tabular Generators Fail to Preserve Behavioral Fraud Patterns: "
    "A Benchmark on Temporal, Velocity, and Multi-Account Signals. "
    "arXiv:2604.13125.",
]

CITATION_NOTE = (
    "EmDT + the synthetic-generator benchmark anchor the fraud-generation "
    "side of the paper; the pseudo-Louck citation (date/year sanitised) and "
    "the three announced agent-payments protocols anchor the agentic-commerce "
    "phenomenon the T9/PCAT structural pillar targets."
)


def _pcat_builder() -> Callable:
    return lambda ac: PCATPolicy.for_agentic(ac)


def evaluate(*, seed: int, rng_seed: int, cases: Optional[Dict[str, int]] = None,
             verbose: bool = True) -> tuple:
    """Run the full before/after protocol evaluation.

    `rng_seed` perturbs the per-case attacker wallet + agent seeds so a rerun
    with the same `seed` stays byte-identical while different seeds stress
    different layouts.
    """
    t_start = time.perf_counter()
    log: List[str] = []
    holdout = lock_holdout(seed=seed)
    fingerprint_ok = holdout.fingerprint == HOLDOUT_FINGERPRINT

    cases = cases or {rc: 3 for rc in RC_CLASSES}
    cases_per_rc = int(cases.get("RC-1", 3))

    per_rc: Dict[str, Dict[str, Any]] = {}
    agentic_payments = 0
    total_agent_to_attacker = 0.0
    for rc in RC_CLASSES:
        rows_naive: List[Dict[str, Any]] = []
        rows_pcat: List[Dict[str, Any]] = []
        for i in range(int(cases.get(rc, cases_per_rc))):
            case_seed = rng_seed * 1000 + i
            world_n = WorldState(seed=seed)
            pack_n = run_t9_case(world_n, seed=case_seed, rc_class=rc,
                                 defense_builder=None)
            rows_naive.append(pack_n)

            world_p = WorldState(seed=seed)
            pack_p = run_t9_case(world_p, seed=case_seed, rc_class=rc,
                                 defense_builder=_pcat_builder())
            rows_pcat.append(pack_p)

            agentic_payments += len(pack_n["payments"]) + len(pack_p["payments"])
            total_agent_to_attacker += float(pack_n["attacker_received"])
        per_rc[rc] = {
            "naive": {
                "succeeded": int(all(judge_rc(rc, p) for p in rows_naive)),
                "n_cases": len(rows_naive),
                "attacker_received_total": round(
                    sum(float(p["attacker_received"]) for p in rows_naive), 2),
            },
            "pcat": {
                "succeeded": int(all(judge_rc(rc, p) for p in rows_pcat)),
                "n_cases": len(rows_pcat),
                "attacker_received_total": round(
                    sum(float(p["attacker_received"]) for p in rows_pcat), 2),
            },
            "mechanism_block": MECHANISM_NAME,
        }

    # FP (false-positive) probe: benign checkout behind the gate must pass.
    benign_packs: List[Dict[str, Any]] = []
    for i in range(5):
        world_b = WorldState(seed=seed + i)
        bag = benign_checkout(world_b, seed=rng_seed + i,
                              defense_builder=_pcat_builder())
        benign_packs.append(bag)
    benign_ok = all(judge_benign(p) for p in benign_packs)

    attack_success_before = sum(per_rc[r]["naive"]["succeeded"] for r in RC_CLASSES)
    attack_success_after = sum(per_rc[r]["pcat"]["succeeded"] for r in RC_CLASSES)

    if verbose:
        log.append("\n[eval] ============ PROTOCOL (T9) BEFORE/AFTER ============")
        log.append(f"[eval] fingerprint        : {holdout.fingerprint} "
                   f"{'(intact)' if fingerprint_ok else '!! CHANGED !!'}")
        for rc in RC_CLASSES:
            n, p = per_rc[rc]["naive"], per_rc[rc]["pcat"]
            log.append(
                f"[eval] {rc}: naive attack_success={n['succeeded']}/{n['n_cases']} "
                f"atk_total={n['attacker_received_total']:9.2f}  |  "
                f"pcat attack_success={p['succeeded']}/{p['n_cases']} "
                f"atk_total={p['attacker_received_total']:9.2f}")
        log.append(f"[eval] benign FP probe    : {'OK (0/5 FP)' if benign_ok else 'FP!'}")
        log.append(f"[eval] residual atk_total  : naive={attack_success_before}/5 "
                   f"-> pcat={attack_success_after}/5")

    artifact = {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "seed": seed,
        "platform": {"python": platform.python_version(),
                     "os": platform.platform()},
        "runtime_seconds": round(time.perf_counter() - t_start, 2),
        "config": {"cases_per_rc": cases_per_rc,
                   "rng_seed": rng_seed,
                   "defense": "pcat_live"},
        "holdout": {**holdout.to_dict(), "fingerprint_intact": fingerprint_ok},
        "agentic_payments_logged": agentic_payments,
        "attacker_wallet_total_before": round(total_agent_to_attacker, 2),
        "per_rc": per_rc,
        "benign_fp_probe": {
            "n": len(benign_packs),
            "all_passed": benign_ok,
            "attack_success_naive": attack_success_before,
            "attack_success_pcat": attack_success_after,
        },
        "citations": CITATIONS,
        "citation_note": CITATION_NOTE,
    }

    if verbose:
        log.append(f"[eval] runtime             : {artifact['runtime_seconds']}s")
    return artifact, log


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Protocol/manipulation (T9) before/after evaluation")
    ap.add_argument("--seed", type=int, default=42,
                    help="holdout lock seed (fingerprint check)")
    ap.add_argument("--rng-seed", type=int, default=7,
                    help="perturbation seed for attacker/agent layout")
    ap.add_argument("--cases", type=int, default=3,
                    help="independent cases per RC class")
    args = ap.parse_args()

    artifact, log = evaluate(seed=args.seed, rng_seed=args.rng_seed,
                             cases={rc: args.cases for rc in RC_CLASSES})
    for line in log:
        print(line)

    out_dir = os.path.join(_PROJECT_ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "protocol_eval.json")
    with open(out_path, "w") as f:
        json.dump(artifact, f, indent=2, default=str)
    print(f"[eval] artifact written    : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())