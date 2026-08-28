"""
test_t2.py — Verification tests for T2: Attack Compiler (pytest).

Converted from the bespoke harness on 2026-08-27 (Phase 0): the hardcoded
`/home/harshil/prometheus` sys.path insert is gone (conftest.py at repo root
resolves imports) and `check()` counters became real asserts so pytest gates.

Covers the 5 original acceptance criteria:
  1. Each of the 6 attack types compiles and executes -> produces a trajectory
  2. Held-out enforcement: assert_no_held_out_leakage works correctly
  3. generate_variants(weakness_descriptor, n=10) returns >=10 distinct variants
  4. Determinism: same seed -> same compiled plan
  5. A2 and A5 never appear in TRAINABLE_ATTACKS
  6. generate_training_attacks end-to-end produces exactly the trainable set
"""

from __future__ import annotations

import json

import pytest

from src.twin.twin import FinancialDigitalTwin
from src.attack.compiler import AttackCompiler
from src.attack.benchmark_attacks import (
    BENCHMARK_ATTACKS,
    HELD_OUT_ATTACKS,
    TRAINABLE_ATTACKS,
    ATTACK_METADATA,
    generate_training_attacks,
)


# ---------------------------------------------------------------------------
# Test 1: Each attack type compiles and executes
# ---------------------------------------------------------------------------

def test_attack_compilation_and_execution():
    twin = FinancialDigitalTwin(seed=42, num_accounts=100, num_merchants=20,
                                num_devices=30, num_ip_blocks=10, num_steps=10)
    compiler = AttackCompiler(twin, seed=42)

    for attack_id in sorted(BENCHMARK_ATTACKS.keys()):
        spec_dict = BENCHMARK_ATTACKS[attack_id]

        plan = compiler.compile(spec_dict)  # raises on failure -> test fails

        required_keys = {"spec", "preconditions", "entities",
                         "action_sequence", "timing", "constraints",
                         "world_actions"}
        assert required_keys.issubset(plan.keys()), (
            f"{attack_id}: missing plan keys {required_keys - plan.keys()}"
        )
        assert len(plan.get("world_actions", [])) > 0, f"{attack_id}: no world actions"

        traj_id = compiler.execute(plan, twin.world)
        assert traj_id and traj_id.startswith("TRAJ_"), f"{attack_id}: bad traj id"

        matching = [t for t in twin.world.trajectories if t["trajectory_id"] == traj_id]
        assert len(matching) == 1, f"{attack_id}: {len(matching)} matching trajectories"

        traj = matching[0]
        assert traj["attack_type"] == attack_id, (
            f"{attack_id}: labelled {traj['attack_type']}"
        )
        assert len(traj.get("actions", [])) > 0, f"{attack_id}: empty actions"


# ---------------------------------------------------------------------------
# Test 2: Held-out enforcement
# ---------------------------------------------------------------------------

@pytest.fixture()
def small_compiler():
    twin = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=10,
                                num_devices=20, num_ip_blocks=5, num_steps=5)
    return AttackCompiler(twin, seed=42)


def test_held_out_all_trainable_passes(small_compiler):
    small_compiler.assert_no_held_out_leakage(["A1", "A3", "A4", "A6"])  # no raise


def test_held_out_single_raises(small_compiler):
    with pytest.raises(AssertionError):
        small_compiler.assert_no_held_out_leakage(["A2"])


def test_held_out_mixed_raises(small_compiler):
    with pytest.raises(AssertionError):
        small_compiler.assert_no_held_out_leakage(["A1", "A2", "A3"])


def test_held_out_empty_passes(small_compiler):
    small_compiler.assert_no_held_out_leakage([])  # no raise


def test_generate_training_attacks_rejects_held_out(small_compiler):
    with pytest.raises(AssertionError):
        generate_training_attacks(
            small_compiler,
            small_compiler.twin.world,
            trainable={"A2"},
            allow_held_out=False,
        )


# ---------------------------------------------------------------------------
# Test 3: Variant generation
# ---------------------------------------------------------------------------

def test_variant_generation():
    twin = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=10,
                                num_devices=20, num_ip_blocks=5, num_steps=5)
    compiler = AttackCompiler(twin, seed=42)

    weakness = {
        "weakness": "relational camouflage",
        "target_model": "GNN",
        "goal": "preserve suspicious economic behavior while diluting graph concentration",
        "suggested_variants": [
            "more_devices", "more_intermediaries", "longer_paths",
            "temporal_spreading", "different_merchants",
        ],
    }

    variants = compiler.generate_variants(weakness, n=10)
    assert isinstance(variants, list)
    assert len(variants) >= 10, f"got {len(variants)} variants"

    for i, v in enumerate(variants):
        assert "attack_id" in v, f"variant {i} missing attack_id"
        assert "goal" in v, f"variant {i} missing goal"
        assert "amount" in v, f"variant {i} missing amount"

    amounts = [v.get("amount", 0) for v in variants]
    assert len(set(amounts)) > 1, "variants not distinct (all same amount)"


# ---------------------------------------------------------------------------
# Test 4: Determinism
# ---------------------------------------------------------------------------

def _stripped_world_actions(plan):
    return [{k: v for k, v in wa.items() if k != "step_offset"}
            for wa in plan.get("world_actions", [])]


def test_compiler_determinism():
    twin1 = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=10,
                                 num_devices=20, num_ip_blocks=5, num_steps=5)
    compiler1 = AttackCompiler(twin1, seed=42)
    twin2 = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=10,
                                 num_devices=20, num_ip_blocks=5, num_steps=5)
    compiler2 = AttackCompiler(twin2, seed=42)

    spec = BENCHMARK_ATTACKS["A1"]
    plan1 = compiler1.compile(spec)
    plan2 = compiler2.compile(spec)

    assert plan1["action_sequence"] == plan2["action_sequence"]
    assert _stripped_world_actions(plan1) == _stripped_world_actions(plan2)
    assert plan1["entities"].get("main_account") == plan2["entities"].get("main_account")

    plan3 = compiler1.compile(spec)
    assert _stripped_world_actions(plan1) == _stripped_world_actions(plan3)


# ---------------------------------------------------------------------------
# Test 5: TRAINABLE_ATTACKS excludes held-out
# ---------------------------------------------------------------------------

def test_trainable_attacks_exclude_held_out():
    assert "A2" not in TRAINABLE_ATTACKS
    assert "A5" not in TRAINABLE_ATTACKS
    assert len(HELD_OUT_ATTACKS & TRAINABLE_ATTACKS) == 0
    assert HELD_OUT_ATTACKS | TRAINABLE_ATTACKS == {"A1", "A2", "A3", "A4", "A5", "A6"}

    for attack_id in HELD_OUT_ATTACKS:
        assert ATTACK_METADATA[attack_id].get("held_out") is True
    for attack_id in TRAINABLE_ATTACKS:
        assert ATTACK_METADATA[attack_id].get("held_out") is False


# ---------------------------------------------------------------------------
# Test 6: generate_training_attacks end-to-end
# ---------------------------------------------------------------------------

def test_training_attacks_end_to_end():
    twin = FinancialDigitalTwin(seed=42, num_accounts=100, num_merchants=20,
                                num_devices=30, num_ip_blocks=10, num_steps=50)
    compiler = AttackCompiler(twin, seed=42)

    for _ in range(5):
        twin.step()

    results = generate_training_attacks(compiler, twin.world)

    assert isinstance(results, dict)
    assert set(results.keys()) == {"A1", "A3", "A4", "A6"}

    for attack_id, traj_id in results.items():
        matching = [t for t in twin.world.trajectories if t["trajectory_id"] == traj_id]
        assert len(matching) == 1, f"{attack_id}: trajectory {traj_id} missing"

    for hid in HELD_OUT_ATTACKS:
        assert hid not in results

    traj_types = {t["attack_type"] for t in twin.world.trajectories}
    assert len(traj_types & HELD_OUT_ATTACKS) == 0, f"leaked: {traj_types & HELD_OUT_ATTACKS}"
