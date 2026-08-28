"""
compiler.py — Rule-Based Attack Compiler for Project Prometheus.

IMPORTANT: This is a RULE-BASED planner, NOT an LLM and NOT a formal compiler.
It performs template expansion + parameter search to transform attack
specifications into concrete, executable action sequences against the
Financial Digital Twin.

The compilation pipeline (from PRD §F2):
    spec → preconditions → entity selection → action sequence
    → timing strategy → feature-level constraints → world actions
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from typing import Any, Dict, List, Optional, Tuple

from twin.core import WorldState
from twin.twin import FinancialDigitalTwin
from twin.typologies import run_typology as run_twin_typology
from .spec import AttackSpec, WeaknessDescriptor, build_attack_spec
from .rl_agent import RLAgent


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class AttackExecutionError(RuntimeError):
    """Raised when an attack execution fails at runtime."""
    pass


# ---------------------------------------------------------------------------
# AttackCompiler
# ---------------------------------------------------------------------------

class AttackCompiler:
    """Rule-based planner that compiles attack specs into action sequences.

    The compiler is deterministic given a fixed seed. Every compile() call
    with the same spec and seed produces identical plans.

    IMPORTANT: This is NOT an LLM. All decisions are made via template
    expansion, weighted random sampling from a fixed seed, and rule-based
    parameter search.
    """

    def __init__(self, twin: FinancialDigitalTwin, seed: int = 42):
        self.twin = twin
        self.world = twin.world
        self.seed = seed
        self.rng = random.Random(seed)
        self.rl_agent = RLAgent(state_dim=3, action_dim=4, seed=seed)

    def benchmark_spec(self, attack_id: str) -> Dict[str, Any]:
        """Fetch the benchmark spec dict for an attack ID."""
        from attack.benchmark_attacks import BENCHMARK_ATTACKS
        if attack_id not in BENCHMARK_ATTACKS:
            raise KeyError(f"Unknown benchmark attack: {attack_id}")
        return dict(BENCHMARK_ATTACKS[attack_id])

    # ------------------------------------------------------------------
    # Main compilation
    # ------------------------------------------------------------------

    def compile(self, spec: dict) -> Dict[str, Any]:
        """Compile an attack specification into an executable plan.

        Args:
            spec: Attack specification dict (or AttackSpec object).
                  Can contain keys: goal, amount, currency, target,
                  constraints, resources, desired_camouflage, attack_id,
                  attack_type, typology, variant_params.

        Returns:
            dict with keys: spec, preconditions, entities, action_sequence,
            timing, constraints, world_actions.
        """
        # Normalise to AttackSpec
        if isinstance(spec, AttackSpec):
            attack_spec = spec
        else:
            attack_spec = AttackSpec.from_dict(spec)

        # Use deterministic sub-RNG derived from seed + spec fingerprint
        # This ensures compile() is always reproducible for the same spec,
        # even when called multiple times on the same compiler instance.
        spec_fingerprint = hashlib.md5(
            json.dumps(attack_spec.to_dict(), sort_keys=True).encode()
        ).hexdigest()[:8]
        compile_rng = random.Random(f"{self.seed}_{spec_fingerprint}")

        # 1. Check preconditions
        preconditions = self._check_preconditions(attack_spec, self.world)

        # 2. Select entities (use compile_rng for deterministic per-spec selection)
        entities = self._select_entities(attack_spec, rng=compile_rng)

        # 3. Build action sequence
        action_sequence = self._build_action_sequence(attack_spec, entities)

        # 4. Build timing strategy (use compile_rng for deterministic timing)
        timing = self._build_timing(attack_spec, action_sequence, rng=compile_rng)

        # 5. Build feature-level constraints
        constraints = self._build_constraints(attack_spec)

        # 6. Generate concrete world actions
        world_actions = self._generate_world_actions(
            attack_spec, action_sequence, entities, timing, rng=compile_rng
        )

        return {
            "spec": attack_spec.to_dict(),
            "preconditions": preconditions,
            "entities": {k: v if isinstance(v, list) else str(v) for k, v in entities.items()},
            "action_sequence": action_sequence,
            "timing": timing,
            "constraints": constraints,
            "world_actions": world_actions,
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, plan: Dict[str, Any],
                world: Optional[WorldState] = None) -> str:
        """Execute a compiled plan against the world, logging a trajectory.

        Args:
            plan: Compiled plan dict from compile().
            world: WorldState to execute against. Defaults to self.world.

        Returns:
            trajectory_id: The ID of the logged trajectory.
        """
        w = world or self.world
        spec = plan.get("spec", {})
        world_actions = plan.get("world_actions", [])
        attack_id = spec.get("attack_id", f"ATTACK_{w._next_account}")
        attack_type = spec.get("attack_type", "unknown")
        trajectory_id = w.next_trajectory_id()

        action_log: List[Dict[str, Any]] = []

        for step_idx, action in enumerate(world_actions):
            act_type = action.get("action", "")
            step_offset = action.get("step_offset", 0)
            actual_step = w.current_step + step_offset

            # Execute the action against the world
            if act_type == "compromise_account":
                account_id = action["account_id"]
                if account_id in w.accounts:
                    # Find the customer and elevate risk state
                    acct = w.accounts[account_id]
                    cust_id = acct.customer_id
                    if cust_id in w.customers:
                        w.customers[cust_id].risk_state = "elevated"
                action_log.append({
                    "step": actual_step,
                    "action": "compromise_account",
                    "account_id": account_id,
                })

            elif act_type == "register_device":
                account_id = action["account_id"]
                device = w.add_device(
                    first_seen_step=actual_step,
                )
                if account_id in w.accounts:
                    w.accounts[account_id].linked_devices.append(device.device_id)
                    device.linked_accounts.append(account_id)
                action_log.append({
                    "step": actual_step,
                    "action": "register_device",
                    "account_id": account_id,
                    "device_id": device.device_id,
                })

            elif act_type == "add_payee":
                account_id = action["account_id"]
                payee = action["payee"]
                if account_id in w.accounts and payee not in w.accounts[account_id].linked_payees:
                    w.accounts[account_id].linked_payees.append(payee)
                action_log.append({
                    "step": actual_step,
                    "action": "add_payee",
                    "account_id": account_id,
                    "payee": payee,
                })

            elif act_type == "small_test_transaction":
                from_id = action["from"]
                to_id = action["to"]
                amount = action["amount"]
                tx = w.log_transaction(
                    from_id=from_id,
                    to_id=to_id,
                    amount=amount,
                    step=actual_step,
                    category="p2p",
                    is_fraud=False,
                    attack_id=attack_id,
                    trajectory_id=trajectory_id,
                )
                action_log.append({
                    "step": actual_step,
                    "action": "small_test_transaction",
                    "from": from_id,
                    "to": to_id,
                    "amount": amount,
                    "tx_id": tx["tx_id"],
                })

            elif act_type == "behavioral_camouflage":
                account_id = action["account_id"]
                count = action.get("count", 1)
                camouflage_txs = []
                for i in range(count):
                    # Camouflage: send small natural-looking amounts to merchants
                    merchant_ids = list(w.merchants.keys())
                    if merchant_ids:
                        merchant = self.rng.choice(merchant_ids)
                        camo_amt = round(self.rng.uniform(100.0, 2000.0), 2)
                        tx = w.log_transaction(
                            from_id=account_id,
                            to_id=merchant,
                            amount=camo_amt,
                            step=actual_step + i,
                            category="retail",
                            is_fraud=False,
                            attack_id=attack_id,
                            trajectory_id=trajectory_id,
                        )
                        camouflage_txs.append(tx["tx_id"])
                action_log.append({
                    "step": actual_step,
                    "action": "behavioral_camouflage",
                    "account_id": account_id,
                    "count": count,
                    "tx_ids": camouflage_txs,
                })

            elif act_type == "large_transfer":
                from_id = action["from"]
                to_id = action["to"]
                amount = action["amount"]
                tx = w.log_transaction(
                    from_id=from_id,
                    to_id=to_id,
                    amount=amount,
                    step=actual_step,
                    category="p2p",
                    is_fraud=True,
                    attack_id=attack_id,
                    trajectory_id=trajectory_id,
                )
                action_log.append({
                    "step": actual_step,
                    "action": "large_transfer",
                    "from": from_id,
                    "to": to_id,
                    "amount": amount,
                    "tx_id": tx["tx_id"],
                })

            elif act_type == "cash_out":
                from_id = action["from"]
                to_id = action.get("to", "EXT_BANK")
                amount = action["amount"]
                tx = w.log_transaction(
                    from_id=from_id,
                    to_id=to_id,
                    amount=amount,
                    step=actual_step,
                    category="p2p",
                    is_fraud=True,
                    attack_id=attack_id,
                    trajectory_id=trajectory_id,
                )
                action_log.append({
                    "step": actual_step,
                    "action": "cash_out",
                    "from": from_id,
                    "to": to_id,
                    "amount": amount,
                    "tx_id": tx["tx_id"],
                })

            elif act_type == "typology":
                typology_name = action["typology"]
                kwargs = dict(action["kwargs"])
                kwargs["world"] = w
                kwargs["rng"] = self.rng
                kwargs["attack_id"] = attack_id
                kwargs["trajectory_id"] = trajectory_id
                if "step_offset" in action:
                    kwargs.setdefault("step_offset", action["step_offset"])
                tx_ids = run_twin_typology(typology_name, **kwargs)
                action_log.append({
                    "step": actual_step,
                    "action": "typology",
                    "typology": typology_name,
                    "tx_ids": tx_ids,
                })

            elif act_type == "create_merchant":
                merchant_id = action.get("merchant_id")
                domain = action.get("domain", "")
                category = action.get("category", "retail")
                merchant = w.add_merchant(
                    merchant_id=merchant_id,
                    domain=domain,
                    category=category,
                )
                # Add domain history if specified
                domain_history = action.get("domain_history", [])
                for entry in domain_history:
                    merchant.domain_history.append(dict(entry))
                action_log.append({
                    "step": actual_step,
                    "action": "create_merchant",
                    "merchant_id": merchant.merchant_id,
                })

            elif act_type == "create_account":
                customer_id = action.get("customer_id")
                balance = action.get("balance", 0.0)
                count = action.get("count", 1)
                account_ids = action.get("account_ids", [])
                for i in range(count):
                    opened_at = actual_step
                    aid = account_ids[i] if i < len(account_ids) else None
                    acct = w.add_account(
                        customer_id=customer_id,
                        account_id=aid,
                        opened_at=opened_at,
                        balance=balance,
                    )
                    action_log.append({
                        "step": actual_step,
                        "action": "create_account",
                        "account_id": acct.account_id,
                        "customer_id": customer_id,
                    })

            elif act_type == "create_customer":
                risk_state = action.get("risk_state", "normal")
                kyc_tier = action.get("kyc_tier", "standard")
                count = action.get("count", 1)
                customer_ids = action.get("customer_ids", [])
                for i in range(count):
                    cid = customer_ids[i] if i < len(customer_ids) else None
                    cust = w.add_customer(customer_id=cid, risk_state=risk_state, kyc_tier=kyc_tier)
                    action_log.append({
                        "step": actual_step,
                        "action": "create_customer",
                        "customer_id": cust.customer_id,
                    })

            else:
                raise AttackExecutionError(
                    f"Unknown world action type: {act_type}"
                )

        # Log the trajectory
        traj = w.log_trajectory(
            attack_type=attack_type,
            actions=action_log,
            spec=spec,
            trajectory_id=trajectory_id,
        )
        return traj["trajectory_id"]

    # ------------------------------------------------------------------
    # Variant generation
    # ------------------------------------------------------------------

    def generate_variants(self, weakness_descriptor: Dict[str, Any],
                          n: int = 10) -> List[Dict[str, Any]]:
        """Given a weakness descriptor, generate N targeted attack variants.

        Each variant is a modified attack spec that targets the described
        weakness. Variants differ in their parameters (more devices, more
        intermediaries, longer paths, temporal spreading, different merchants).

        Args:
            weakness_descriptor: dict with keys: weakness, target_model, goal,
                                 suggested_variants (list of strings).
            n: Number of variants to generate (default 10).

        Returns:
            List of dicts, each being a complete attack spec variant.
        """
        wd = WeaknessDescriptor.from_dict(weakness_descriptor)
        suggested = wd.suggested_variants or [
            "more_devices", "more_intermediaries", "longer_paths",
            "temporal_spreading", "different_merchants",
        ]

        variants: List[Dict[str, Any]] = []

        # Base spec: a generic money-moving attack
        base_spec = {
            "goal": "move_funds",
            "amount": 100000.0,
            "currency": "INR",
            "target": "compromised_cardholders",
            "constraints": {"max_fraud_score": 0.35, "max_behavioral_anomaly": 0.4},
            "resources": {"devices": 5, "accounts": 8, "days": 7},
            "desired_camouflage": "high",
            "attack_type": "variant",
        }

        for i in range(n):
            variant = dict(base_spec)
            variant["attack_id"] = f"VARIANT_{i+1:02d}"

            # Pick a variant strategy (cycle through them)
            strategy = suggested[i % len(suggested)]
            variant_meta = {"strategy": strategy}

            if strategy == "more_devices":
                variant["resources"]["devices"] = 10 + (i * 2)
                variant["desired_camouflage"] = "high"

            elif strategy == "more_intermediaries":
                variant["resources"]["accounts"] = 12 + (i * 3)
                variant["amount"] = 150000.0 + (i * 25000.0)
                variant["typology"] = "fan_in"

            elif strategy == "longer_paths":
                variant["resources"]["accounts"] = 10 + (i * 2)
                variant["amount"] = 200000.0
                variant["typology"] = "stack"

            elif strategy == "temporal_spreading":
                variant["resources"]["days"] = 14 + (i * 3)
                variant["amount"] = 75000.0
                variant["desired_camouflage"] = "very_high"

            elif strategy == "different_merchants":
                variant["target"] = "merchant"
                variant["amount"] = 50000.0 + (i * 10000.0)
                variant["typology"] = "bipartite"

            else:
                # Generic random perturbation
                variant["resources"]["devices"] += i
                variant["resources"]["accounts"] += i * 2

            variant["variant_params"] = variant_meta
            
            # Incorporate RL Agent to mutate the generated spec
            # State vector can be a simplistic mapping of resources
            state_vec = [
                variant["resources"].get("devices", 5),
                variant["resources"].get("accounts", 8),
                variant["amount"] / 100000.0
            ]
            action = self.rl_agent.select_action(state_vec)
            variant = self.rl_agent.mutate_spec(variant, action)
            variant["rl_action_taken"] = action

            variants.append(variant)

        return variants

    # ------------------------------------------------------------------
    # Held-out enforcement
    # ------------------------------------------------------------------

    def assert_no_held_out_leakage(self, attack_ids: List[str]) -> None:
        """Assert that no held-out attack IDs appear in a training set.

        Raises AssertionError if any held-out attack is found.
        Held-out attacks: A2 (synthetic identity), A5 (scatter_gather layering).
        """
        from .benchmark_attacks import HELD_OUT_ATTACKS
        leaked = [aid for aid in attack_ids if aid in HELD_OUT_ATTACKS]
        if leaked:
            raise AssertionError(
                f"Held-out attack(s) found in training set: {leaked}. "
                f"These attacks ({', '.join(sorted(HELD_OUT_ATTACKS))}) "
                f"must never be generated during training runs."
            )

    # ------------------------------------------------------------------
    # Internal compilation pipeline
    # ------------------------------------------------------------------

    def _check_preconditions(self, spec: AttackSpec,
                             world: WorldState) -> List[str]:
        """Verify that the world state can support the attack.

        Returns a list of precondition strings (e.g. "enough_accounts",
        "enough_balance"). Returns empty list if all preconditions pass.
        """
        preconditions: List[str] = []
        n_accounts_needed = spec.resources.get("accounts", 1)
        n_devices_needed = spec.resources.get("devices", 0)

        # Check we have enough accounts
        if len(world.accounts) >= n_accounts_needed:
            preconditions.append(f"at_least_{n_accounts_needed}_accounts")

        # Check we have enough balance in the system
        total_balance = sum(
            a.balance for a in world.accounts.values()
        )
        if total_balance >= spec.amount:
            preconditions.append("sufficient_system_balance")

        # Check devices
        if n_devices_needed == 0 or len(world.devices) + n_devices_needed >= 0:
            preconditions.append("device_capacity_ok")

        # Check merchants exist for merchant-targeted attacks
        if spec.target == "merchant" and len(world.merchants) > 0:
            preconditions.append("merchants_available")
        elif spec.target != "merchant":
            preconditions.append("no_merchant_needed")

        # Check IPs
        if len(world.ips) > 0:
            preconditions.append("ips_available")

        return preconditions

    def _select_entities(self, spec: AttackSpec,
                         rng: Optional[random.Random] = None) -> Dict[str, Any]:
        """Select specific entities from the twin to use in the attack.

        Args:
            spec: The attack specification.
            rng: Optional random generator. If None, uses self.rng.

        Returns a dict with keys like main_account, members, merchants,
        devices, ips, etc.
        """
        world = self.world
        rng = rng or self.rng
        n_accounts = spec.resources.get("accounts", 5)
        n_devices = spec.resources.get("devices", 2)

        all_accounts = list(world.accounts.keys())
        all_merchants = list(world.merchants.keys()) if world.merchants else []

        # Select a main account (compromised target)
        main_account = rng.choice(all_accounts)

        # Select member/mule accounts (exclude main)
        candidates = [a for a in all_accounts if a != main_account]
        n_members = min(n_accounts - 1, len(candidates))
        members = rng.sample(candidates, n_members) if n_members > 0 else []

        # Select merchants
        n_merchants = min(3, len(all_merchants))
        merchants = (
            rng.sample(all_merchants, n_merchants) if n_merchants > 0 else []
        )

        # Select devices
        all_device_ids = list(world.devices.keys())
        n_dev = min(n_devices, len(all_device_ids))
        devices = (
            rng.sample(all_device_ids, n_dev) if n_dev > 0 else []
        )

        # Select IP
        all_ips = list(world.ips.keys())
        ip = rng.choice(all_ips) if all_ips else None

        return {
            "main_account": main_account,
            "members": members,
            "merchants": merchants,
            "devices": devices,
            "ip": ip,
        }

    def _build_action_sequence(self, spec: AttackSpec,
                               entities: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Build an ordered list of abstract actions for the attack.

        The action sequence depends on the attack_type and typology.
        Returns a list of action dicts with keys: action, params.
        """
        attack_type = spec.attack_type
        sequence: List[Dict[str, Any]] = []

        if attack_type == "A1":
            # Account takeover: compromise → device → payee → test → camouflage → transfer → cash-out
            sequence = [
                {"action": "compromise_account"},
                {"action": "register_device"},
                {"action": "add_payee"},
                {"action": "small_test_transaction"},
                {"action": "behavioral_camouflage", "count": 3},
                {"action": "large_transfer"},
                {"action": "cash_out"},
            ]
        elif attack_type == "A2":
            # Synthetic identity: create identities → create accounts → transact among themselves
            n_accs = spec.resources.get("accounts", 12)
            sequence = [
                {"action": "create_customer", "count": n_accs},
                {"action": "create_account", "count": n_accs},
                {"action": "typology", "typology": "bipartite"},
            ]
        elif attack_type == "A3":
            # Card testing: many tiny probes to merchants
            sequence = [
                {"action": "register_device"},
                {"action": "typology", "typology": "fan_out", "amount_modifier": "micro"},
            ]
        elif attack_type == "A4":
            # Money laundering — fan_in layering
            sequence = [
                {"action": "register_device"},
                {"action": "typology", "typology": "fan_in"},
                {"action": "large_transfer"},
                {"action": "cash_out"},
            ]
        elif attack_type == "A5":
            # Money laundering — scatter_gather layering
            sequence = [
                {"action": "register_device"},
                {"action": "typology", "typology": "scatter_gather"},
            ]
        elif attack_type == "A6":
            # Merchant fraud
            sequence = [
                {"action": "create_merchant", "aged_domain": True},
                {"action": "typology", "typology": "fan_in", "target_merchant": True},
            ]
        else:
            # Generic fallback: compromise → transfer → cash-out
            sequence = [
                {"action": "compromise_account"},
                {"action": "large_transfer"},
                {"action": "cash_out"},
            ]

        return sequence

    def _build_timing(self, spec: AttackSpec,
                      action_sequence: List[Dict[str, Any]],
                      rng: Optional[random.Random] = None) -> Dict[str, Any]:
        """Determine step offsets and timing strategy for the action sequence.

        Args:
            spec: Attack specification.
            action_sequence: List of abstract action steps.
            rng: Optional random generator. If None, uses self.rng.

        Returns a dict with step_offsets (list of ints) and a strategy name.
        """
        rng = rng or self.rng
        n_actions = len(action_sequence)
        resources_days = spec.resources.get("days", 7)
        desired_camouflage = spec.desired_camouflage

        # Map days to approximate step spread
        steps_per_action = max(1, resources_days // max(1, n_actions))

        if desired_camouflage == "high" or desired_camouflage == "very_high":
            # Spread actions out more with camouflage gaps
            strategy = "slow_spread"
            offsets = [
                i * steps_per_action + rng.randint(0, 2)
                for i in range(n_actions)
            ]
        elif desired_camouflage == "medium":
            strategy = "medium_spread"
            offsets = [i * max(1, steps_per_action // 2) for i in range(n_actions)]
        else:
            # Low camouflage: burst in quick succession
            strategy = "burst"
            offsets = list(range(n_actions))

        return {
            "strategy": strategy,
            "step_offsets": offsets,
            "resources_days": resources_days,
        }

    def _build_constraints(self, spec: AttackSpec) -> List[Dict[str, Any]]:
        """Build feature-level constraints based on the spec.

        Returns a list of constraint dicts, each with field, min, max.
        """
        constraints: List[Dict[str, Any]] = []

        if "max_fraud_score" in spec.constraints:
            constraints.append({
                "field": "fraud_score",
                "max": spec.constraints["max_fraud_score"],
            })

        if "max_behavioral_anomaly" in spec.constraints:
            constraints.append({
                "field": "behavioral_anomaly",
                "max": spec.constraints["max_behavioral_anomaly"],
            })

        # Add resource constraints
        constraints.append({
            "field": "max_devices_per_account",
            "max": spec.resources.get("devices", 5) + 1,
        })

        constraints.append({
            "field": "max_tx_velocity",
            "max": spec.resources.get("accounts", 8) * 2,
        })

        return constraints

    def _generate_world_actions(self, spec: AttackSpec,
                                action_sequence: List[Dict[str, Any]],
                                entities: Dict[str, Any],
                                timing: Dict[str, Any],
                                rng: Optional[random.Random] = None) -> List[Dict[str, Any]]:
        """Transform the abstract action sequence into concrete world actions.

        Each world action is a dict with keys like:
          action, account_id, from, to, amount, step_offset, etc.

        Args:
            spec: Attack specification.
            action_sequence: List of abstract action steps.
            entities: Selected entity map.
            timing: Timing strategy dict.
            rng: Optional random generator. If None, uses self.rng.
        """
        rng = rng or self.rng
        world_actions: List[Dict[str, Any]] = []
        offsets = timing.get("step_offsets", list(range(len(action_sequence))))

        main_account = entities.get("main_account", "")
        members = entities.get("members", [])
        merchants = entities.get("merchants", [])
        devices = entities.get("devices", [])

        attack_type = spec.attack_type

        # Tracks state: which accounts/devices we've created or compromised
        compromised_accounts: List[str] = []
        created_accounts: List[str] = []
        created_devices: List[str] = []

        for idx, action_step in enumerate(action_sequence):
            step_offset = offsets[idx] if idx < len(offsets) else idx
            act_type = action_step["action"]
            wa: Dict[str, Any] = {"action": act_type, "step_offset": step_offset}

            if act_type == "compromise_account":
                wa["account_id"] = main_account
                compromised_accounts.append(main_account)

            elif act_type == "register_device":
                wa["account_id"] = main_account

            elif act_type == "add_payee":
                # Payee is first available member
                target_payee = members[0] if members else main_account
                wa["account_id"] = main_account
                wa["payee"] = target_payee

            elif act_type == "small_test_transaction":
                target = members[0] if members else main_account
                test_amount = min(100.0, spec.amount * 0.001)
                wa["from"] = main_account
                wa["to"] = target
                wa["amount"] = round(test_amount, 2)

            elif act_type == "behavioral_camouflage":
                wa["account_id"] = main_account
                wa["count"] = action_step.get("count", 3)

            elif act_type == "large_transfer":
                target = members[0] if members else "EXT_MERCHANT_PAYOUT"
                transfer_amount = spec.amount * 0.8
                wa["from"] = main_account
                wa["to"] = target
                wa["amount"] = round(transfer_amount, 2)

            elif act_type == "cash_out":
                source = members[0] if members else main_account
                cash_amount = spec.amount * 0.7
                wa["from"] = source
                wa["to"] = "EXT_BANK"
                wa["amount"] = round(cash_amount, 2)

            elif act_type == "create_customer":
                count = action_step.get("count", 5)
                wa["count"] = count
                wa["customer_ids"] = [f"C_{rng.randint(100000, 999999)}_{i}" for i in range(count)]

            elif act_type == "create_account":
                count = action_step.get("count", 5)
                wa["count"] = count
                wa["customer_id"] = "AUTO"
                wa["balance"] = rng.uniform(5000.0, 50000.0)
                new_accts = [f"A_{rng.randint(100000, 999999)}_{i}" for i in range(count)]
                wa["account_ids"] = new_accts
                if attack_type == "A2":
                    members = new_accts

            elif act_type == "typology":
                typology_name = action_step["typology"]
                wa["typology"] = typology_name
                kwargs: Dict[str, Any] = {}

                if typology_name == "fan_in":
                    kwargs["main_account"] = main_account
                    kwargs["members"] = members
                    kwargs["amount"] = spec.amount

                elif typology_name == "fan_out":
                    kwargs["main_account"] = main_account
                    kwargs["members"] = members
                    amount_modifier = action_step.get("amount_modifier", "")
                    if amount_modifier == "micro":
                        # Card testing: sub-₹1 amounts with jitter
                        kwargs["amount"] = round(rng.uniform(0.1, 0.9), 2)
                        kwargs["members"] = merchants if merchants else members
                    else:
                        # Normal fan-out with jitter
                        kwargs["amount"] = round(spec.amount * rng.uniform(0.9, 1.1), 2)

                elif typology_name == "scatter_gather":
                    kwargs["main_account"] = main_account
                    n_inter = max(2, len(members) // 2)
                    kwargs["intermediaries"] = members[:n_inter]
                    kwargs["beneficiary"] = members[-1] if len(members) > 1 else main_account
                    kwargs["amount"] = spec.amount
                    kwargs["margin_ratio"] = 0.1

                elif typology_name == "bipartite":
                    if attack_type == "A2":
                        # Synthetic identity: use ALL members (new accounts)
                        kwargs["sources"] = members[:len(members)//2]
                        kwargs["targets"] = members[len(members)//2:]
                    else:
                        kwargs["sources"] = members[:len(members)//2]
                        kwargs["targets"] = members[len(members)//2:] if len(members) > 2 else merchants
                    kwargs["amount"] = spec.amount

                elif typology_name == "stack":
                    kwargs["layers"] = [members[i::3] for i in range(3)]
                    kwargs["amount"] = spec.amount

                wa["kwargs"] = kwargs

            elif act_type == "create_merchant":
                aged = action_step.get("aged_domain", False)
                domain = f"fraud-merchant-{rng.randint(1000, 9999)}.com"
                wa["domain"] = domain
                wa["category"] = "retail"
                if aged:
                    wa["domain_history"] = [
                        {"event": "registered", "step": -365},
                        {"event": "registrar_change", "step": -30},
                        {"event": "template_update", "step": -7},
                    ]

            world_actions.append(wa)

        return world_actions

    # ------------------------------------------------------------------
    # Utility: deterministic hashing
    # ------------------------------------------------------------------

    def plan_fingerprint(self, plan: Dict[str, Any]) -> str:
        """Return a deterministic hash of a compiled plan's core content."""
        digest = hashlib.sha256()
        core = {
            "action_sequence": plan.get("action_sequence", []),
            "world_actions": [
                {k: v for k, v in wa.items() if k != "step_offset"}
                for wa in plan.get("world_actions", [])
            ],
        }
        digest.update(json.dumps(core, sort_keys=True).encode())
        return digest.hexdigest()[:16]