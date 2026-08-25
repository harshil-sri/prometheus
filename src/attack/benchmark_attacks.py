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
# Individual attack execution functions
#
# Each function signature:
#   execute_<attack>(compiler, world, rng, spec, trajectory_id) -> List[str]
#
# Returns list of transaction IDs generated.
# ---------------------------------------------------------------------------

def _pick_merchant(rng: random.Random, world: WorldState,
                   category: str = "retail") -> Optional[str]:
    """Pick a merchant, optionally matching category."""
    matching = [m for m in world.merchants.values() if m.category == category]
    if matching:
        return rng.choice(matching).merchant_id
    if world.merchants:
        return rng.choice(list(world.merchants.keys()))
    return None


def _pick_device(rng: random.Random, world: WorldState,
                 account_id: str) -> Optional[str]:
    """Pick a device linked to the account, or create one."""
    account = world.accounts.get(account_id)
    if account is None:
        return None
    if account.linked_devices:
        return rng.choice(account.linked_devices)
    device = world.add_device()
    account.linked_devices.append(device.device_id)
    device.linked_accounts.append(account_id)
    return device.device_id


def _pick_ip(rng: random.Random, world: WorldState) -> Optional[str]:
    """Pick a random IP block."""
    if world.ips:
        return rng.choice(list(world.ips.keys()))
    return None


def execute_a1(compiler: AttackCompiler, world: WorldState,
               rng: random.Random, spec: AttackSpec,
               trajectory_id: str) -> List[str]:
    """A1: Account Takeover.

    Pipeline: compromise → device → payee → test → camouflage → transfer → cash-out
    """
    tx_ids: List[str] = []
    rng = random.Random(rng.randint(0, 2**31))

    # Select target account
    all_accounts = list(world.accounts.keys())
    target = rng.choice(all_accounts)
    victims = [a for a in all_accounts if a != target]
    victim = rng.choice(victims) if victims else target

    # Get the customer for risk elevation
    acct = world.accounts.get(target)
    if acct and acct.customer_id in world.customers:
        world.customers[acct.customer_id].risk_state = "elevated"

    # 1. Register a new device for the target
    device = world.add_device(
        first_seen_step=world.current_step
    )
    if target in world.accounts:
        world.accounts[target].linked_devices.append(device.device_id)
        device.linked_accounts.append(target)

    # 2. Add payee (victim account as funnel)
    if target in world.accounts and victim not in world.accounts[target].linked_payees:
        world.accounts[target].linked_payees.append(victim)

    # 3. Small test transaction
    test_amt = min(100.0, spec.amount * 0.001)
    tx = world.log_transaction(
        from_id=target, to_id=victim, amount=round(test_amt, 2),
        step=world.current_step, category="p2p",
        device=device.device_id, ip=_pick_ip(rng, world),
        is_fraud=False, attack_id=spec.attack_id, trajectory_id=trajectory_id,
    )
    tx_ids.append(tx["tx_id"])

    # 4. Behavioral camouflage (3 normal-looking small transactions)
    merchants_list = list(world.merchants.keys())
    for i in range(3):
        merchant = rng.choice(merchants_list) if merchants_list else None
        camo_amt = round(rng.uniform(200.0, 1500.0), 2)
        tx = world.log_transaction(
            from_id=target,
            to_id=merchant or victim,
            amount=camo_amt,
            step=world.current_step + 1 + i,
            category="retail",
            device=device.device_id,
            ip=_pick_ip(rng, world),
            is_fraud=False,
            attack_id=spec.attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])

    # 5. Large transfer (main fraud event)
    transfer_amt = round(spec.amount * 0.8, 2)
    tx = world.log_transaction(
        from_id=target, to_id=victim, amount=transfer_amt,
        step=world.current_step + 4, category="p2p",
        device=device.device_id, ip=_pick_ip(rng, world),
        is_fraud=True, attack_id=spec.attack_id, trajectory_id=trajectory_id,
    )
    tx_ids.append(tx["tx_id"])

    # 6. Cash out
    cash_amt = round(transfer_amt * 0.9, 2)
    tx = world.log_transaction(
        from_id=victim, to_id="EXT_BANK", amount=cash_amt,
        step=world.current_step + 5, category="p2p",
        device=_pick_device(rng, world, victim), ip=_pick_ip(rng, world),
        is_fraud=True, attack_id=spec.attack_id, trajectory_id=trajectory_id,
    )
    tx_ids.append(tx["tx_id"])

    return tx_ids


def execute_a2(compiler: AttackCompiler, world: WorldState,
               rng: random.Random, spec: AttackSpec,
               trajectory_id: str) -> List[str]:
    """A2: Synthetic Identity Onboarding Burst. HELD OUT.

    Creates synthetic identities (customers + accounts) in a burst,
    then they transact among themselves using bipartite typology.
    """
    tx_ids: List[str] = []
    rng = random.Random(rng.randint(0, 2**31))

    # Create synthetic identities (customers with low KYC)
    new_customers = []
    new_accounts = []
    for i in range(6):
        cust = world.add_customer(
            risk_state="normal",
            kyc_tier="low",
        )
        new_customers.append(cust.customer_id)
        acct = world.add_account(
            customer_id=cust.customer_id,
            opened_at=world.current_step,
            balance=rng.uniform(1000.0, 10000.0),
        )
        new_accounts.append(acct.account_id)

    # Transact among themselves (bipartite)
    if len(new_accounts) >= 4:
        sources = new_accounts[:len(new_accounts)//2]
        targets = new_accounts[len(new_accounts)//2:]
        amt = spec.amount / max(1, len(sources))
        for i, src in enumerate(sources):
            tgt = rng.choice(targets)
            tx = world.log_transaction(
                from_id=src, to_id=tgt, amount=round(amt, 2),
                step=world.current_step + i, category="p2p",
                device=_pick_device(rng, world, src),
                ip=_pick_ip(rng, world),
                is_fraud=True,
                attack_id=spec.attack_id,
                trajectory_id=trajectory_id,
            )
            tx_ids.append(tx["tx_id"])

    return tx_ids


def execute_a3(compiler: AttackCompiler, world: WorldState,
               rng: random.Random, spec: AttackSpec,
               trajectory_id: str) -> List[str]:
    """A3: Card Testing / Coordinated Micro-Testing.

    Many sub-₹1 probe transactions from one account to many merchants.
    """
    tx_ids: List[str] = []
    rng = random.Random(rng.randint(0, 2**31))

    all_accounts = list(world.accounts.keys())
    all_merchants = list(world.merchants.keys())

    if not all_accounts or not all_merchants:
        return tx_ids

    source = rng.choice(all_accounts)
    device = _pick_device(rng, world, source)
    ip = _pick_ip(rng, world)

    # 10 micro-probes to random merchants
    n_probes = min(10, len(all_merchants))
    probe_merchants = rng.sample(all_merchants, n_probes)

    for i, merchant in enumerate(probe_merchants):
        micro_amt = round(rng.uniform(0.1, 0.9), 2)
        tx = world.log_transaction(
            from_id=source, to_id=merchant, amount=micro_amt,
            step=world.current_step + i, category="retail",
            device=device, ip=ip,
            is_fraud=True,
            attack_id=spec.attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])

    return tx_ids


def execute_a4(compiler: AttackCompiler, world: WorldState,
               rng: random.Random, spec: AttackSpec,
               trajectory_id: str) -> List[str]:
    """A4: Money Laundering — Fan-In Layering.

    Multiple mule accounts → main account → external.
    """
    tx_ids: List[str] = []
    rng = random.Random(rng.randint(0, 2**31))

    all_accounts = list(world.accounts.keys())
    if len(all_accounts) < 4:
        return tx_ids

    # Main account (beneficiary)
    main_acct = rng.choice(all_accounts)
    mules = [a for a in all_accounts if a != main_acct]
    rng.shuffle(mules)
    mules = mules[:4]  # 4 mules

    # Fan-in: mules → main account
    per_mule = spec.amount / len(mules)
    for i, mule in enumerate(mules):
        tx = world.log_transaction(
            from_id=mule, to_id=main_acct, amount=round(per_mule, 2),
            step=world.current_step + i, category="p2p",
            device=_pick_device(rng, world, mule),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=spec.attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])

    # Main → external
    offset = len(mules)
    cash_amt = round(spec.amount * 0.9, 2)
    tx = world.log_transaction(
        from_id=main_acct, to_id="EXT_BANK", amount=cash_amt,
        step=world.current_step + offset, category="p2p",
        device=_pick_device(rng, world, main_acct),
        ip=_pick_ip(rng, world),
        is_fraud=True,
        attack_id=spec.attack_id,
        trajectory_id=trajectory_id,
    )
    tx_ids.append(tx["tx_id"])

    return tx_ids


def execute_a5(compiler: AttackCompiler, world: WorldState,
               rng: random.Random, spec: AttackSpec,
               trajectory_id: str) -> List[str]:
    """A5: Money Laundering — Scatter-Gather Layering. HELD OUT.

    Main → intermediaries → beneficiary with margin_ratio.
    """
    tx_ids: List[str] = []
    rng = random.Random(rng.randint(0, 2**31))

    all_accounts = list(world.accounts.keys())
    if len(all_accounts) < 5:
        return tx_ids

    main_acct = rng.choice(all_accounts)
    others = [a for a in all_accounts if a != main_acct]
    rng.shuffle(others)
    intermediaries = others[:3]
    beneficiary = others[3] if len(others) > 3 else main_acct

    # Scatter-gather
    margin = 0.1
    for i, inter in enumerate(intermediaries):
        scatter_amt = (spec.amount / 2.0) / len(intermediaries)
        tx1 = world.log_transaction(
            from_id=main_acct, to_id=inter, amount=round(scatter_amt, 2),
            step=world.current_step + 2 * i, category="p2p",
            device=_pick_device(rng, world, main_acct),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=spec.attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx1["tx_id"])

        gather_amt = round(scatter_amt * (1.0 - margin), 2)
        tx2 = world.log_transaction(
            from_id=inter, to_id=beneficiary, amount=gather_amt,
            step=world.current_step + 2 * i + 1, category="p2p",
            device=_pick_device(rng, world, inter),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=spec.attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx2["tx_id"])

    return tx_ids


def execute_a6(compiler: AttackCompiler, world: WorldState,
               rng: random.Random, spec: AttackSpec,
               trajectory_id: str) -> List[str]:
    """A6: Merchant Fraud (Fake Storefront).

    Create a fake merchant with aged-domain churn signals,
    then funnel money to it via fan-in from mule accounts.
    """
    tx_ids: List[str] = []
    rng = random.Random(rng.randint(0, 2**31))

    # 1. Create fake merchant with aged-domain churn signals
    merchant = world.add_merchant(
        domain=f"fraud-store-{rng.randint(10000, 99999)}.com",
        category="retail",
        hosting_asn=rng.choice(["ASN_1", "ASN_2"]),
        template_fingerprint="wp-plugin-hash-custom-001",
    )
    # Add churn history
    merchant.domain_history = [
        {"event": "registered", "step": world.current_step - 365},
        {"event": "registrar_change", "step": world.current_step - 30},
        {"event": "template_update", "step": world.current_step - 7},
        {"event": "dns_change", "step": world.current_step - 3},
    ]

    # 2. Funnel money: mule accounts → merchant
    all_accounts = list(world.accounts.keys())
    n_mules = min(4, len(all_accounts))
    mules = rng.sample(all_accounts, n_mules)

    per_mule = spec.amount / len(mules)
    for i, mule in enumerate(mules):
        tx = world.log_transaction(
            from_id=mule, to_id=merchant.merchant_id,
            amount=round(per_mule, 2),
            step=world.current_step + i, category="retail",
            device=_pick_device(rng, world, mule),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=spec.attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])

    return tx_ids


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

ATTACK_EXECUTORS = {
    "A1": execute_a1,
    "A2": execute_a2,
    "A3": execute_a3,
    "A4": execute_a4,
    "A5": execute_a5,
    "A6": execute_a6,
}


# ---------------------------------------------------------------------------
# Training attack generation
# ---------------------------------------------------------------------------

def generate_training_attacks(
    compiler: AttackCompiler,
    world: WorldState,
    trainable: Optional[Set[str]] = None,
    allow_held_out: bool = False,
) -> Dict[str, str]:
    """Generate all trainable benchmark attacks and return trajectory IDs.

    Args:
        compiler: AttackCompiler instance.
        world: WorldState to execute attacks against.
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
    rng = random.Random(compiler.seed + 100)

    for attack_id in sorted(trainable):
        spec_dict = BENCHMARK_ATTACKS.get(attack_id)
        if spec_dict is None:
            continue

        spec = AttackSpec.from_dict(spec_dict)
        executor = ATTACK_EXECUTORS.get(attack_id)
        if executor is None:
            continue

        trajectory_id = world.next_trajectory_id()

        # Run attack-specific executor
        tx_ids = executor(compiler, world, rng, spec, trajectory_id)

        # Log the trajectory
        action_log = [
            {
                "step": world.current_step,
                "action": f"{attack_id}_execution",
                "tx_ids": tx_ids,
            }
        ]
        traj = world.log_trajectory(
            attack_type=attack_id,
            actions=action_log,
            spec=spec.to_dict(),
        )
        results[attack_id] = traj["trajectory_id"]

        # Advance step after each attack
        world.current_step += 1

    return results