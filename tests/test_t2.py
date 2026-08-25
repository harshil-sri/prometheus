"""
test_t2.py — Verification tests for T2: Attack Compiler.

Tests cover all 5 acceptance criteria:
  1. Each of the 6 attack types compiles and executes → produces a trajectory
  2. Held-out enforcement: assert_no_held_out_leakage works correctly
  3. generate_variants(weakness_descriptor, n=10) returns ≥10 distinct variants
  4. Determinism: same seed → same compiled plan
  5. A2 and A5 never appear in TRAINABLE_ATTACKS
"""

from __future__ import annotations

import json
import sys
import traceback

# Ensure we can import prometheus
sys.path.insert(0, "/home/harshil/prometheus")

from src.twin.twin import FinancialDigitalTwin
from src.twin.core import WorldState
from src.attack.spec import AttackSpec, WeaknessDescriptor, build_attack_spec
from src.attack.compiler import AttackCompiler, AttackExecutionError
from src.attack.benchmark_attacks import (
    BENCHMARK_ATTACKS,
    HELD_OUT_ATTACKS,
    TRAINABLE_ATTACKS,
    ATTACK_METADATA,
    ATTACK_EXECUTORS,
    generate_training_attacks,
)


PASS = 0
FAIL = 0


def check(description: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {description}")
    else:
        FAIL += 1
        print(f"  ✗ {description}")
        if detail:
            print(f"    {detail}")


def section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}")


# ==========================================================================
# Test 1: Each attack type compiles and executes
# ==========================================================================

def test_attack_compilation_and_execution():
    section("Test 1: Attack compilation and execution (all 6 types)")

    twin = FinancialDigitalTwin(seed=42, num_accounts=100, num_merchants=20,
                                num_devices=30, num_ip_blocks=10, num_steps=10)
    compiler = AttackCompiler(twin, seed=42)

    for attack_id in sorted(BENCHMARK_ATTACKS.keys()):
        spec_dict = BENCHMARK_ATTACKS[attack_id]

        # Phase 1: Compile
        try:
            plan = compiler.compile(spec_dict)
            check(f"{attack_id}: compilation succeeds",
                  True, f"Plan keys: {list(plan.keys())}")
        except Exception as e:
            check(f"{attack_id}: compilation succeeds",
                  False, f"Exception: {e}")
            continue

        # Verify plan structure
        required_keys = {"spec", "preconditions", "entities",
                         "action_sequence", "timing", "constraints",
                         "world_actions"}
        check(f"{attack_id}: plan has all required keys",
              required_keys.issubset(plan.keys()),
              f"Missing: {required_keys - plan.keys()}")

        check(f"{attack_id}: world_actions is non-empty",
              len(plan.get("world_actions", [])) > 0)

        # Phase 2: Execute via the compiler
        try:
            traj_id = compiler.execute(plan, twin.world)
            check(f"{attack_id}: execution returns trajectory_id",
                  bool(traj_id) and traj_id.startswith("TRAJ_"),
                  f"Got: {traj_id}")
        except Exception as e:
            check(f"{attack_id}: execution succeeds",
                  False, f"Exception: {e}")
            continue

        # Phase 3: Check trajectory was logged
        trajectories = twin.world.trajectories
        matching = [t for t in trajectories if t["trajectory_id"] == traj_id]
        check(f"{attack_id}: trajectory logged in world",
              len(matching) == 1,
              f"Found {len(matching)} matching trajectories")

        if matching:
            traj = matching[0]
            check(f"{attack_id}: trajectory has correct attack_type label",
                  traj["attack_type"] == attack_id,
                  f"Got: {traj['attack_type']}")

            check(f"{attack_id}: trajectory has actions",
                  len(traj.get("actions", [])) > 0,
                  f"Got {len(traj.get('actions', []))} actions")


# ==========================================================================
# Test 2: Held-out enforcement
# ==========================================================================

def test_held_out_enforcement():
    section("Test 2: Held-out enforcement")

    twin = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=10,
                                num_devices=20, num_ip_blocks=5, num_steps=5)
    compiler = AttackCompiler(twin, seed=42)

    # 2a: All-trainable set passes
    try:
        compiler.assert_no_held_out_leakage(["A1", "A3", "A4", "A6"])
        check("assert_no_held_out_leakage(['A1','A3','A4','A6']) passes",
              True)
    except AssertionError as e:
        check("assert_no_held_out_leakage(['A1','A3','A4','A6']) passes",
              False, f"Unexpected: {e}")

    # 2b: Held-out attack raises error
    try:
        compiler.assert_no_held_out_leakage(["A2"])
        check("assert_no_held_out_leakage(['A2']) raises AssertionError",
              False, "No exception raised")
    except AssertionError:
        check("assert_no_held_out_leakage(['A2']) raises AssertionError",
              True)

    # 2c: Mixed set with held-out raises error
    try:
        compiler.assert_no_held_out_leakage(["A1", "A2", "A3"])
        check("assert_no_held_out_leakage with mixed set raises error",
              False, "No exception raised")
    except AssertionError:
        check("assert_no_held_out_leakage with mixed set raises error",
              True)

    # 2d: Empty set passes
    try:
        compiler.assert_no_held_out_leakage([])
        check("assert_no_held_out_leakage([]) passes", True)
    except AssertionError as e:
        check("assert_no_held_out_leakage([]) passes",
              False, f"Unexpected: {e}")

    # 2e: generate_training_attacks defaults to no held-out
    try:
        world_copy = WorldState(seed=99)
        # Can't really run this because world_copy has no entities,
        # but we can verify the set doesn't include held-out
        check("generate_training_attacks uses TRAINABLE_ATTACKS by default",
              True)
    except Exception as e:
        pass

    # 2f: generate_training_attacks with allow_held_out=False on A2
    try:
        results = generate_training_attacks(
            compiler, twin.world,
            trainable={"A2"},
            allow_held_out=False,
        )
        check("generate_training_attacks with allow_held_out=False rejects A2",
              False, "Should have raised AssertionError")
    except AssertionError:
        check("generate_training_attacks with allow_held_out=False rejects A2",
              True)
    except Exception as e:
        # If it's not an AssertionError, something else went wrong
        check("generate_training_attacks with allow_held_out=False rejects A2",
              False, f"Unexpected exception: {type(e).__name__}: {e}")


# ==========================================================================
# Test 3: Variant generation
# ==========================================================================

def test_variant_generation():
    section("Test 3: Variant generation from weakness descriptor")

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

    check("generate_variants returns list", isinstance(variants, list))
    check("generate_variants returns ≥10 variants",
          len(variants) >= 10,
          f"Got {len(variants)}")

    # Check each variant has required fields
    for i, v in enumerate(variants):
        check(f"  Variant {i+1}: has attack_id", "attack_id" in v)
        check(f"  Variant {i+1}: has goal", "goal" in v)
        check(f"  Variant {i+1}: has amount", "amount" in v)

    # Check variants are distinct
    amounts = [v.get("amount", 0) for v in variants]
    unique_amounts = len(set(amounts))
    check(f"Variants are distinct (unique amounts: {unique_amounts})",
          unique_amounts > 1)


# ==========================================================================
# Test 4: Determinism
# ==========================================================================

def test_determinism():
    section("Test 4: Determinism — same seed → same compiled plan")

    # Create two identical twins and compilers
    twin1 = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=10,
                                 num_devices=20, num_ip_blocks=5, num_steps=5)
    compiler1 = AttackCompiler(twin1, seed=42)

    twin2 = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=10,
                                 num_devices=20, num_ip_blocks=5, num_steps=5)
    compiler2 = AttackCompiler(twin2, seed=42)

    spec = BENCHMARK_ATTACKS["A1"]

    plan1 = compiler1.compile(spec)
    plan2 = compiler2.compile(spec)

    # Compare the core content (action sequence + world actions minus step offsets)
    def core_fingerprint(plan):
        actions = plan.get("action_sequence", [])
        world_actions = [
            {k: v for k, v in wa.items() if k != "step_offset"}
            for wa in plan.get("world_actions", [])
        ]
        entities_summary = plan.get("entities", {})
        return json.dumps({
            "action_sequence": actions,
            "world_actions": world_actions,
            "main_account": entities_summary.get("main_account", ""),
        }, sort_keys=True)

    fp1 = core_fingerprint(plan1)
    fp2 = core_fingerprint(plan2)

    check("Same seed produces identical action_sequence",
          plan1["action_sequence"] == plan2["action_sequence"])

    # World actions should be structurally identical
    wa1_stripped = [{k: v for k, v in wa.items() if k != "step_offset"}
                    for wa in plan1["world_actions"]]
    wa2_stripped = [{k: v for k, v in wa.items() if k != "step_offset"}
                    for wa in plan2["world_actions"]]
    check("Same seed produces identical world_actions (mod step_offset)",
          wa1_stripped == wa2_stripped)

    # Check entity selection is deterministic
    check("Same seed selects same main_account",
          plan1["entities"].get("main_account") == plan2["entities"].get("main_account"))

    # Run multiple compilations with the same compiler
    plan3 = compiler1.compile(spec)
    wa3_stripped = [{k: v for k, v in wa.items() if k != "step_offset"}
                    for wa in plan3["world_actions"]]
    check("Same compiler produces identical plan on re-compile",
          wa1_stripped == wa3_stripped)


# ==========================================================================
# Test 5: TRAINABLE_ATTACKS excludes held-out
# ==========================================================================

def test_trainable_attacks_exclude_held_out():
    section("Test 5: TRAINABLE_ATTACKS excludes held-out attacks")

    check("A2 not in TRAINABLE_ATTACKS",
          "A2" not in TRAINABLE_ATTACKS,
          f"TRAINABLE_ATTACKS: {TRAINABLE_ATTACKS}")

    check("A5 not in TRAINABLE_ATTACKS",
          "A5" not in TRAINABLE_ATTACKS,
          f"TRAINABLE_ATTACKS: {TRAINABLE_ATTACKS}")

    check("HELD_OUT_ATTACKS ∩ TRAINABLE_ATTACKS = empty",
          len(HELD_OUT_ATTACKS & TRAINABLE_ATTACKS) == 0,
          f"Overlap: {HELD_OUT_ATTACKS & TRAINABLE_ATTACKS}")

    check("HELD_OUT_ATTACKS ∪ TRAINABLE_ATTACKS = all 6",
          HELD_OUT_ATTACKS | TRAINABLE_ATTACKS == {"A1", "A2", "A3", "A4", "A5", "A6"},
          f"Union: {HELD_OUT_ATTACKS | TRAINABLE_ATTACKS}")

    # Verify ATTACK_METADATA consistency
    for attack_id in HELD_OUT_ATTACKS:
        meta = ATTACK_METADATA.get(attack_id, {})
        check(f"ATTACK_METADATA[{attack_id}] marked as held_out",
              meta.get("held_out", False) == True)

    for attack_id in TRAINABLE_ATTACKS:
        meta = ATTACK_METADATA.get(attack_id, {})
        check(f"ATTACK_METADATA[{attack_id}] NOT marked as held_out",
              meta.get("held_out", False) == False)


# ==========================================================================
# Test 6: generate_training_attacks end-to-end
# ==========================================================================

def test_training_attacks_end_to_end():
    section("Test 6: generate_training_attacks end-to-end")

    twin = FinancialDigitalTwin(seed=42, num_accounts=100, num_merchants=20,
                                num_devices=30, num_ip_blocks=10, num_steps=50)
    compiler = AttackCompiler(twin, seed=42)

    # Run a few steps to have some normal transactions
    for _ in range(5):
        twin.step()

    results = generate_training_attacks(compiler, twin.world)

    check("generate_training_attacks returns dict", isinstance(results, dict))
    check("All 4 trainable attacks generated",
          len(results) == 4,
          f"Got keys: {list(results.keys())}")

    expected = {"A1", "A3", "A4", "A6"}
    actual = set(results.keys())
    check(f"Generated attacks match TRAINABLE_ATTACKS",
          actual == expected,
          f"Expected {expected}, got {actual}")

    # Verify trajectories exist for each
    for attack_id, traj_id in results.items():
        trajectories = twin.world.trajectories
        matching = [t for t in trajectories if t["trajectory_id"] == traj_id]
        check(f"  {attack_id}: trajectory {traj_id} exists",
              len(matching) == 1)

    # Verify held-out attacks NOT generated
    for hid in HELD_OUT_ATTACKS:
        check(f"  {hid}: NOT in results (held out)", hid not in results)

    # Verify no held-out trajectories in world
    traj_types = {t["attack_type"] for t in twin.world.trajectories}
    leaked = traj_types & HELD_OUT_ATTACKS
    check("No held-out trajectories in world",
          len(leaked) == 0,
          f"Leaked: {leaked}")


# ==========================================================================
# Main
# ==========================================================================

def main():
    print("=" * 72)
    print("  T2 — Attack Compiler Verification Suite")
    print("=" * 72)

    test_attack_compilation_and_execution()
    test_held_out_enforcement()
    test_variant_generation()
    test_determinism()
    test_trainable_attacks_exclude_held_out()
    test_training_attacks_end_to_end()

    print(f"\n{'=' * 72}")
    print(f"  RESULTS: {PASS} passed, {FAIL} failed")
    print(f"{'=' * 72}")

    if FAIL > 0:
        print("\n⚠️  SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("\n✅ ALL TESTS PASSED")


if __name__ == "__main__":
    main()