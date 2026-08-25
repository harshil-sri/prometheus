"""
test_t1.py — Verification tests for T1 Financial Digital Twin.

Tests:
1. Determinism: Two twins with seed=42 produce identical transaction logs
2. Normal != Fraud: Normal TX amounts have different statistics from fraud amounts
3. 8 typologies correct with structural checks
4. Open system: EXT_SALARY deposits exist in logs
5. Cold-start: no crashes when account has no edges
6. Isolated nodes: no crashes when account has no edges
7. Transaction log dict format: keys must be "from" and "to"
"""

import sys
import os
import json
import random
import math

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.twin.core import WorldState
from src.twin.normal_behavior import NormalBehaviorGenerator, build_normal_profile, MERCHANT_CATEGORIES
from src.twin.typologies import (
    fan_in, fan_out, cycle, scatter_gather, gather_scatter,
    bipartite, stack, random_typology, run_typology
)
from src.twin.twin import FinancialDigitalTwin


passed = 0
failed = 0

def check(condition, msg):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {msg}")
    else:
        failed += 1
        print(f"  FAIL  {msg}")


# ============================================================================
# Test 1: Determinism
# ============================================================================
print("\n=== Test 1: Determinism ===")

twin1 = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=20,
                              num_devices=30, num_ip_blocks=20, num_steps=100)
txs1 = twin1.run()

twin2 = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=20,
                              num_devices=30, num_ip_blocks=20, num_steps=100)
txs2 = twin2.run()

# Compare tx IDs
ids1 = [t["tx_id"] for t in txs1]
ids2 = [t["tx_id"] for t in txs2]
check(len(ids1) == len(ids2), f"Same number of transactions: {len(ids1)} == {len(ids2)}")
check(ids1 == ids2, "Transaction ID sequences match exactly")

# Compare full dicts
check(txs1 == txs2, "Full transaction dicts match")

# Compare world states
s1 = twin1.world.snapshot()
s2 = twin2.world.snapshot()
check(len(s1["transactions"]) == len(s2["transactions"]), "World transaction counts match")
check(len(s1["trajectories"]) == len(s2["trajectories"]), "World trajectory counts match")


# ============================================================================
# Test 2: Transaction dict format (from/to keys)
# ============================================================================
print("\n=== Test 2: Transaction dict format ===")

if txs1:
    sample = txs1[0]
    check("from" in sample, "Tx dict has 'from' key")
    check("to" in sample, "Tx dict has 'to' key")
    check("from_id" not in sample, "Tx dict does NOT have 'from_id' key")
    check("to_id" not in sample, "Tx dict does NOT have 'to_id' key")
    check("tx_id" in sample, "Tx dict has 'tx_id' key")
    check("amount" in sample, "Tx dict has 'amount' key")
    check("is_fraud" in sample, "Tx dict has 'is_fraud' key")

# Also check via WorldState.log_transaction directly
w = WorldState(seed=99)
w.add_customer()
w.add_account("CUST_00001")
w.add_merchant(category="retail")
tx = w.log_transaction("ACC_00001", "MERCHANT_00001", 100.0)
check("from" in tx, "WorldState.log_transaction returns dict with 'from'")
check("to" in tx, "WorldState.log_transaction returns dict with 'to'")
check(tx["from"] == "ACC_00001", "tx['from'] matches from_id")
check(tx["to"] == "MERCHANT_00001", "tx['to'] matches to_id")
check("from_id" not in tx, "No 'from_id' in returned dict")
check("to_id" not in tx, "No 'to_id' in returned dict")


# ============================================================================
# Test 3: Normal != Fraud statistics
# ============================================================================
print("\n=== Test 3: Normal != Fraud statistics ===")

# Build a twin with a small attack to get both normal and fraud TXs
def small_attack_scheduler(world, twin):
    """Inject a single fan-in attack at step 50."""
    if world.current_step == 50:
        accounts = list(world.accounts.keys())[:5]
        run_typology("fan_in", world, random.Random(999),
                     main_account=accounts[0],
                     members=accounts[1:],
                     attack_id="TEST_ATTACK",
                     step_offset=0)

twin3 = FinancialDigitalTwin(seed=42, num_accounts=100, num_merchants=30,
                              num_devices=50, num_ip_blocks=30, num_steps=200)
txs3 = twin3.run(attack_scheduler=small_attack_scheduler)

normal_amts = [t["amount"] for t in txs3 if not t["is_fraud"]]
fraud_amts = [t["amount"] for t in txs3 if t["is_fraud"]]

check(len(normal_amts) > 0, f"Normal TXs exist: {len(normal_amts)}")
check(len(fraud_amts) > 0, f"Fraud TXs exist: {len(fraud_amts)}")

if normal_amts and fraud_amts:
    normal_mean = sum(normal_amts) / len(normal_amts)
    fraud_mean = sum(fraud_amts) / len(fraud_amts)
    print(f"    Normal amount mean: {normal_mean:.2f}")
    print(f"    Fraud amount mean:  {fraud_mean:.2f}")

    # Check that fraud amounts tend to be larger than normal amounts
    check(fraud_mean > normal_mean,
          f"Fraud mean ({fraud_mean:.2f}) > normal mean ({normal_mean:.2f})")

    # Check that ALL fraud amounts in the world (from typologies called directly
    # with a single amount, not split across members) are multiples of 1000.
    # The split amounts from fan_in/etc may not be round-1000 since they divide
    # the base amount by member count.
    # Instead, verify the base _fraud_amount function produces round-1000 values:
    test_rng = random.Random(999)
    from src.twin.typologies import _fraud_amount
    base_amount = _fraud_amount(test_rng)
    check(base_amount % 1000 == 0.0,
          f"_fraud_amount produces round-1000 value: {base_amount}")

    # Check that at least some normal amounts are NOT round multiples of 1000
    non_round = sum(1 for a in normal_amts if a % 1000 != 0.0)
    check(non_round > 0, f"Some normal amounts are NOT round multiples of 1000: {non_round}/{len(normal_amts)}")


# ============================================================================
# Test 4: Open system — EXT_SALARY deposits
# ============================================================================
print("\n=== Test 4: Open system (EXT_SALARY) ===")

ext_salary_txs = [t for t in txs3 if t.get("from") == "EXT_SALARY"]
check(len(ext_salary_txs) > 0, f"EXT_SALARY transactions exist: {len(ext_salary_txs)}")
check(all(t["to"] != "EXT_SALARY" for t in ext_salary_txs),
      "EXT_SALARY is always sender, not receiver")

# Check that external entities are tracked as constants
check("EXT_SALARY" in WorldState.EXTERNAL_ENTITIES, "EXT_SALARY in EXTERNAL_ENTITIES")
check("EXT_MERCHANT_PAYOUT" in WorldState.EXTERNAL_ENTITIES, "EXT_MERCHANT_PAYOUT in EXTERNAL_ENTITIES")


# ============================================================================
# Test 5: Cold-start accounts
# ============================================================================
print("\n=== Test 5: Cold-start accounts ===")

# Create a world with a single account and no devices
cold_world = WorldState(seed=1)
cold_world.add_customer()
cold_world.add_account("CUST_00001", balance=100000.0)
cold_world.add_merchant(category="retail")

# Assign a profile manually
cold_world.accounts["ACC_00001"].profile = build_normal_profile(
    random.Random(42), "ACC_00001"
)

# Run normal generator
cold_gen = NormalBehaviorGenerator(cold_world, seed=99)
try:
    tx = cold_gen.step("ACC_00001")
    if tx is not None:
        check("from" in tx, "Cold-start TX has 'from' key")
        check("to" in tx, "Cold-start TX has 'to' key")
        check(not tx["is_fraud"], "Cold-start TX is not fraud")
        # Account should now have a linked device
        check(len(cold_world.accounts["ACC_00001"].linked_devices) > 0,
              "Cold-start account got a device")
    else:
        # May not be due yet, that's OK — run more steps
        check(True, "Cold-start step returned None (may not be due yet)")
except Exception as e:
    check(False, f"Cold-start raised exception: {e}")


# ============================================================================
# Test 6: Isolated nodes
# ============================================================================
print("\n=== Test 6: Isolated nodes (no edges) ===")

# Create a world where an account has NO relationships at all
iso_world = WorldState(seed=5)
iso_world.add_customer()
iso_world.add_account("CUST_00001", balance=50000.0)
# Don't add a profile — test cold-start profile assignment
# This account should have zero relationships
check(len(iso_world.relationships) == 0, "Isolated account has no relationships initially")

# Run a transaction from an unrelated pair
iso_world.add_merchant(category="retail")
tx = iso_world.log_transaction("ACC_00001", "MERCHANT_00001", 100.0)
check(tx is not None, "Transaction with isolated account succeeded")
check(tx["from"] == "ACC_00001", "Isolated TX has correct from")


# ============================================================================
# Test 7: All 8 typologies structural checks
# ============================================================================
print("\n=== Test 7: 8 Typologies structural checks ===")

def make_test_world(seed=42, n_accounts=10, n_merchants=5):
    """Create a minimal world for typology testing."""
    w = WorldState(seed=seed)
    for i in range(n_accounts):
        c = w.add_customer()
        a = w.add_account(c.customer_id, balance=1000000.0)
        a.profile = {"preferred_categories": ["retail"], "mean_interval": 10, "amount_scale": 1.0}
        # Give each account a device
        d = w.add_device()
        a.linked_devices.append(d.device_id)
        d.linked_accounts.append(a.account_id)
    for i in range(n_merchants):
        w.add_merchant(category="retail")
    return w

# 7a. Fan-In
w = make_test_world(100)
accts = list(w.accounts.keys())
rng = random.Random(42)
tx_ids = fan_in(w, rng, main_account=accts[0], members=accts[1:4],
                attack_id="TEST_FANIN")
# Check all TXs go to main_account
for tid in tx_ids:
    tx = next(t for t in w.transactions if t["tx_id"] == tid)
    check(tx["to"] == accts[0], f"Fan-in: TX {tid} goes to main account")
    check(tx["is_fraud"], "Fan-in TX is fraud")
check(len(tx_ids) == 3, f"Fan-in generated 3 TXs: {len(tx_ids)}")

# 7b. Fan-Out
w = make_test_world(200)
accts = list(w.accounts.keys())
rng = random.Random(42)
tx_ids = fan_out(w, rng, main_account=accts[0], members=accts[1:4],
                 attack_id="TEST_FANOUT")
for tid in tx_ids:
    tx = next(t for t in w.transactions if t["tx_id"] == tid)
    check(tx["from"] == accts[0], f"Fan-out: TX {tid} comes from main account")
check(len(tx_ids) == 3, f"Fan-out generated 3 TXs: {len(tx_ids)}")

# 7c. Cycle
w = make_test_world(300)
accts = list(w.accounts.keys())[:5]
rng = random.Random(42)
tx_ids = cycle(w, rng, members=accts, attack_id="TEST_CYCLE")
check(len(tx_ids) == 5, f"Cycle generated 5 TXs: {len(tx_ids)}")
# Structural check: from_i -> to_i where to_0=from_1, to_1=from_2, ..., to_n=from_0
for i, tid in enumerate(tx_ids):
    tx = next(t for t in w.transactions if t["tx_id"] == tid)
    expected_from = accts[i]
    expected_to = accts[(i + 1) % len(accts)]
    check(tx["from"] == expected_from, f"Cycle: TX {i} from={tx['from']} == {expected_from}")
    check(tx["to"] == expected_to, f"Cycle: TX {i} to={tx['to']} == {expected_to}")

# 7d. Scatter-Gather
w = make_test_world(400)
accts = list(w.accounts.keys())
rng = random.Random(42)
tx_ids = scatter_gather(w, rng, main_account=accts[0],
                         intermediaries=accts[1:3],
                         beneficiary=accts[3],
                         attack_id="TEST_SG")
check(len(tx_ids) == 4, f"Scatter-gather generated 4 TXs: {len(tx_ids)}")
# Key property: gather amounts < scatter amounts (by margin_ratio)
scatter_txs = [t for t in w.transactions if t["from"] == accts[0] and t["is_fraud"]]
gather_txs = [t for t in w.transactions if t["to"] == accts[3] and t["is_fraud"]]
if scatter_txs and gather_txs:
    total_scatter = sum(t["amount"] for t in scatter_txs)
    total_gather = sum(t["amount"] for t in gather_txs)
    check(total_gather < total_scatter,
          f"Scatter-gather: gather ({total_gather:.2f}) < scatter ({total_scatter:.2f})")

# 7e. Gather-Scatter
w = make_test_world(500)
accts = list(w.accounts.keys())
rng = random.Random(42)
tx_ids = gather_scatter(w, rng, sources=accts[1:4],
                         main_account=accts[0],
                         targets=accts[4:6],
                         attack_id="TEST_GS")
check(len(tx_ids) == 5, f"Gather-scatter generated 5 TXs: {len(tx_ids)}")

# 7f. Bipartite
w = make_test_world(600)
accts = list(w.accounts.keys())
rng = random.Random(42)
tx_ids = bipartite(w, rng, sources=accts[:3], targets=accts[3:6],
                    attack_id="TEST_BIP")
check(len(tx_ids) == 3, f"Bipartite generated 3 TXs: {len(tx_ids)}")

# 7g. Stack
w = make_test_world(700)
accts = list(w.accounts.keys())
rng = random.Random(42)
layers = [accts[0:2], accts[2:4], accts[4:6]]
tx_ids = stack(w, rng, layers=layers, attack_id="TEST_STACK")
# Layer 0->1: 2*2=4 TXs, Layer 1->2: 2*2=4 TXs = 8 total
check(len(tx_ids) == 8, f"Stack generated 8 TXs: {len(tx_ids)}")

# 7h. Random propagation
w = make_test_world(800)
accts = list(w.accounts.keys())
rng = random.Random(42)
tx_ids = random_typology(w, rng, main_account=accts[0], depth=3,
                          attack_id="TEST_RANDOM")
check(len(tx_ids) == 3, f"Random typology generated 3 TXs: {len(tx_ids)}")


# ============================================================================
# Test 8: Trajectory logging
# ============================================================================
print("\n=== Test 8: Trajectory logging ===")

w = make_test_world(900)
accts = list(w.accounts.keys())
rng = random.Random(42)
actions = [
    {"from": accts[1], "to": accts[0], "amount": 50000.0},
    {"from": accts[2], "to": accts[0], "amount": 50000.0},
]
traj = w.log_trajectory("fan_in", actions, {"attack_id": "TRAJ_TEST"})
check("trajectory_id" in traj, "Trajectory has trajectory_id")
check("attack_type" in traj, "Trajectory has attack_type")
check(traj["attack_type"] == "fan_in", "Trajectory attack_type matches")
check(len(traj["actions"]) == 2, "Trajectory has 2 actions")
check(len(w.trajectories) == 1, "World has 1 trajectory logged")

# Check trajectory was logged in world
check(w.trajectories[0]["attack_type"] == "fan_in",
      "World trajectory stored correctly")


# ============================================================================
# Summary
# ============================================================================
print(f"\n{'='*60}")
print(f"RESULTS: {passed} passed, {failed} failed")
if failed > 0:
    print("SOME TESTS FAILED")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)