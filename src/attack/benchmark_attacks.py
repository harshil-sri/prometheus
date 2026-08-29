"""
benchmark_attacks.py — 6 Benchmark Attack Types for Project Prometheus.

Defines 6 attack types (A1–A6) as spec templates, with A2 and A5 held out
from training runs. Each attack type has a function that constructs and
executes it against the Financial Digital Twin.

Held-out enforcement:
  - A2 (Synthetic identity onboarding burst) is HELD OUT — never generated during training
  - A5 (Money laundering — scatter_gather layering) is HELD OUT — never generated during training
  - AttackCompiler.assert_no_held_out_leakage() enforces this at runtime
"""

from __future__ import annotations

import copy
import random
from typing import Any, Dict, List, Optional, Set, Tuple

from twin.core import WorldState
from twin.twin import FinancialDigitalTwin
from twin.typologies import (
    fan_in as typology_fan_in,
    fan_out as typology_fan_out,
    scatter_gather as typology_scatter_gather,
    bipartite as typology_bipartite,
    run_typology,
)
from .spec import AttackSpec, build_attack_spec
from .compiler import AttackCompiler


# ---------------------------------------------------------------------------
# Attack IDs and sets
# ---------------------------------------------------------------------------

HELD_OUT_ATTACKS: Set[str] = {"A2", "A5"}
"""Attack types that are held out for evaluation — never generated during training."""

TRAINABLE_ATTACKS: Set[str] = {"A1", "A3", "A4", "A6"}
"""Attack types that can appear in training runs."""

# Sanity: no overlap
assert HELD_OUT_ATTACKS & TRAINABLE_ATTACKS == set(), (
    f"HELD_OUT_ATTACKS and TRAINABLE_ATTACKS overlap: "
    f"{HELD_OUT_ATTACKS & TRAINABLE_ATTACKS}"
)
# Sanity: all 6 accounted for
ALL_ATTACKS = HELD_OUT_ATTACKS | TRAINABLE_ATTACKS
assert ALL_ATTACKS == {"A1", "A2", "A3", "A4", "A5", "A6"}, (
    f"Not all 6 attacks accounted for: {ALL_ATTACKS}"
)

# ---------------------------------------------------------------------------
# T9 — Protocol/agentic-manipulation (updates.md 6.1)
# ---------------------------------------------------------------------------
# T9 is NOT a TRAINABLE_ATTACK and is deliberately kept OUT of the A1-A6
# axes-2/3 holdout and ALL_ATTACKS sets: it lives in its own independent
# (single-model, deterministic) "protocol_eval" story so the baseline lock
# (fingerprint, train/eval cardinals) is bit-for-bit untouched. It uses its
# own mechanism namespace (protocol_structural) and attack-type code T9.

from blue.splits import register_attack_types as _register_t9_types

_register_t9_types(["T9"])

PROTOCOL_ATTACKS = {
    "T9": {
        "name": "Protocol / agentic-manipulation",
        "mechanism": "protocol_structural",
        "rc_classes": ("RC-1", "RC-2", "RC-3", "RC-4", "RC-5"),
        "story": "Agentic-commerce checkout (Mastercard Agent Pay / Visa TAP / "
                 "Google AP2): an LM-driven agent with a scoped payment "
                 "credential completes checkout without a human step. "
                 "Structural attack classes that succeed regardless of the "
                 "model: RC-1 rogue registry entry, RC-2 blind trust of "
                 "federation payout resolution, RC-3 credential leak via an "
                 "observable channel, RC-4 check-vs-deduct race (TOCTOU), "
                 "RC-5 privileged checkout tool without caller authz.",
        "independent_eval": "scripts/protocol_eval.py -> artifacts/protocol_eval.json",
    },
}


# ---------------------------------------------------------------------------
# Attack metadata (for dashboard threat-intel cards)
# ---------------------------------------------------------------------------

ATTACK_METADATA: Dict[str, Dict[str, Any]] = {
    "A1": {
        "name": "Account Takeover",
        "category": "ATO",
        "held_out": False,
        "description": (
            "Adversary compromises a victim account, registers a new device, "
            "adds a payee, performs a small test transaction, conducts "
            "behavioral camouflage, then executes a large transfer and cash-out."
        ),
        "complexity": "medium",
        "typology": "fan_out",
    },
    "A2": {
        "name": "Synthetic Identity Onboarding Burst",
        "category": "Synthetic Identity",
        "held_out": True,
        "description": (
            "A burst of synthetic identities onboard simultaneously, creating "
            "new accounts that transact among themselves to build artificial "
            "credit history and trust signals."
        ),
        "complexity": "high",
        "typology": "bipartite",
    },
    "A3": {
        "name": "Card Testing / Coordinated Micro-Testing",
        "category": "Card Testing",
        "held_out": False,
        "description": (
            "Many sub-₹1 probe transactions from one account to many merchants "
            "in rapid succession, testing card validity before larger fraud."
        ),
        "complexity": "low",
        "typology": "fan_out",
    },
    "A4": {
        "name": "Money Laundering — Fan-In Layering",
        "category": "AML",
        "held_out": False,
        "description": (
            "Multiple mule accounts funnel funds into a single main account "
            "(fan-in), which then sends the consolidated amount to an external "
            "destination."
        ),
        "complexity": "medium",
        "typology": "fan_in",
    },
    "A5": {
        "name": "Money Laundering — Scatter-Gather Layering",
        "category": "AML",
        "held_out": True,
        "description": (
            "Funds scatter from a main account through multiple intermediary "
            "accounts, then gather into a beneficiary account with a margin "
            "ratio deducted at each hop."
        ),
        "complexity": "high",
        "typology": "scatter_gather",
    },
    "A6": {
        "name": "Merchant Fraud (Fake Storefront)",
        "category": "Merchant Fraud",
        "held_out": False,
        "description": (
            "A fake merchant is created with aged-domain churn signals "
            "(registrar changes, template updates). Funds are funneled to it "
            "via fan-in transactions."
        ),
        "complexity": "high",
        "typology": "fan_in",
    },
}


# ---------------------------------------------------------------------------
# Attack spec templates
# ---------------------------------------------------------------------------

BENCHMARK_ATTACKS: Dict[str, Dict[str, Any]] = {
    "A1": {
        "goal": "move_funds",
        "amount": 100000.0,
        "currency": "INR",
        "target": "compromised_cardholders",
        "constraints": {"max_fraud_score": 0.35, "max_behavioral_anomaly": 0.4},
        "resources": {"devices": 3, "accounts": 4, "days": 5},
        "desired_camouflage": "high",
        "attack_id": "A1",
        "attack_type": "A1",
        "typology": "fan_out",
    },
    "A2": {
        "goal": "create_synthetic_identities",
        "amount": 50000.0,
        "currency": "INR",
        "target": "new_accounts",
        "constraints": {"max_fraud_score": 0.20, "max_behavioral_anomaly": 0.30},
        "resources": {"devices": 8, "accounts": 12, "days": 3},
        "desired_camouflage": "low",
        "attack_id": "A2",
        "attack_type": "A2",
        "typology": "bipartite",
    },
    "A3": {
        "goal": "test_card_validity",
        "amount": 10.0,
        "currency": "INR",
        "target": "merchants",
        "constraints": {"max_fraud_score": 0.50, "max_behavioral_anomaly": 0.60},
        "resources": {"devices": 1, "accounts": 2, "days": 1},
        "desired_camouflage": "low",
        "attack_id": "A3",
        "attack_type": "A3",
        "typology": "fan_out",
    },
    "A4": {
        "goal": "move_funds",
        "amount": 200000.0,
        "currency": "INR",
        "target": "mule_accounts",
        "constraints": {"max_fraud_score": 0.30, "max_behavioral_anomaly": 0.35},
        "resources": {"devices": 4, "accounts": 6, "days": 7},
        "desired_camouflage": "high",
        "attack_id": "A4",
        "attack_type": "A4",
        "typology": "fan_in",
    },
    "A5": {
        "goal": "move_funds",
        "amount": 300000.0,
        "currency": "INR",
        "target": "layering_network",
        "constraints": {"max_fraud_score": 0.25, "max_behavioral_anomaly": 0.30},
        "resources": {"devices": 6, "accounts": 10, "days": 10},
        "desired_camouflage": "very_high",
        "attack_id": "A5",
        "attack_type": "A5",
        "typology": "scatter_gather",
    },
    "A6": {
        "goal": "merchant_fraud",
        "amount": 150000.0,
        "currency": "INR",
        "target": "fake_merchant",
        "constraints": {"max_fraud_score": 0.30, "max_behavioral_anomaly": 0.35},
        "resources": {"devices": 3, "accounts": 5, "days": 14},
        "desired_camouflage": "high",
        "attack_id": "A6",
        "attack_type": "A6",
        "typology": "fan_in",
    },
}


# ---------------------------------------------------------------------------
# Execution goes through the AttackCompiler pipeline ONLY.
#
# Phase 3 unification (audit finding #8): the previous per-attack executor
# functions (execute_a1..a6) were a SECOND, divergent implementation of the
# benchmark attacks and are gone. The compiler action-sequence templates in
# compiler._build_action_sequence / _generate_world_actions / execute() are
# the single source of truth; benchmark specs here feed straight into
# compile() -> execute(), so training, evaluation, variants and the feedback
# loop all share one code path. Mechanism tagging (rule_compiler) is applied
# inside the compiler execution layer.
# ---------------------------------------------------------------------------


def generate_training_attacks(
    compiler: AttackCompiler,
    world: WorldState,
    trainable: Optional[Set[str]] = None,
    allow_held_out: bool = False,
) -> Dict[str, str]:
    """Generate all trainable benchmark attacks and return trajectory IDs.

    UNIFIED PATH (Phase 3): every attack is compiled and executed through the
    AttackCompiler pipeline — the same code path used for evaluation,
    variants and the feedback loop. No per-attack executor functions exist
    any more.

    Args:
        compiler: AttackCompiler instance.
        world: WorldState to execute attacks against. Defaults to the
               compiler's own world.
        trainable: Set of attack IDs to generate. Defaults to TRAINABLE_ATTACKS.
        allow_held_out: If True, allow generating held-out attacks.
                        Default False (safe for training runs).

    Returns:
        Dict mapping attack_id → trajectory_id.

    Raises:
        AssertionError: If held-out attacks would be generated when
                        allow_held_out=False.
    """
    if trainable is None:
        trainable = TRAINABLE_ATTACKS

    if not allow_held_out:
        compiler.assert_no_held_out_leakage(list(trainable))

    results: Dict[str, str] = {}
    target_world = world if world is not compiler.world else compiler.world

    for attack_id in sorted(trainable):
        spec_dict = BENCHMARK_ATTACKS.get(attack_id)
        if spec_dict is None:
            continue

        # Single source of truth: compile -> execute (mechanism-tagged
        # rule_compiler inside execute()).
        plan = compiler.compile(spec_dict)
        traj_id = compiler.execute(plan, target_world)
        matching = [t for t in target_world.trajectories
                    if t["trajectory_id"] == traj_id]
        assert matching and matching[0]["attack_type"] == attack_id, (
            f"{attack_id}: unified execution mislabelled or missing"
        )
        results[attack_id] = traj_id

        # Advance step after each attack (temporal separation between types)
        target_world.current_step += 1

    return results