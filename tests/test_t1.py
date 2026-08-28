"""
test_t1.py — Verification tests for T1 Financial Digital Twin (pytest).

Converted 1:1 from the bespoke harness on 2026-08-27 (Phase 0). Assertion
logic preserved; counters/print harness replaced with plain asserts.

Covers:
1. Determinism: two twins with seed=42 produce identical transaction logs
2. Transaction dict format: keys must be "from" and "to"
3. Normal != Fraud statistics — PHASE 1 INVERTED: fraud amounts must NOT be
   on the round-1000 grid anymore (that grid was a detector cheat-code,
   audit finding #10). Paise precision is asserted instead.
4. Open system: EXT_SALARY deposits exist in logs
5. Cold-start: no crash when an account has no history/device
6. Isolated nodes: transaction succeeds with no prior relationships
7. All 8 typologies with structural checks
8. Trajectory logging
"""

from __future__ import annotations

import random

from src.twin.core import WorldState
from src.twin.normal_behavior import (
    NormalBehaviorGenerator,
    build_normal_profile,
)
from src.twin.typologies import (
    fan_in,
    fan_out,
    cycle,
    scatter_gather,
    gather_scatter,
    bipartite,
    stack,
    random_typology,
    run_typology,
    _fraud_amount,
)
from src.twin.twin import FinancialDigitalTwin


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_test_world(seed: int = 42, n_accounts: int = 10, n_merchants: int = 5) -> WorldState:
    """Minimal world for typology testing (same construction as original suite)."""
    w = WorldState(seed=seed)
    for _ in range(n_accounts):
        c = w.add_customer()
        a = w.add_account(c.customer_id, balance=1000000.0)
        a.profile = {"preferred_categories": ["retail"], "mean_interval": 10, "amount_scale": 1.0}
        d = w.add_device()
        a.linked_devices.append(d.device_id)
        d.linked_accounts.append(a.account_id)
    for _ in range(n_merchants):
        w.add_merchant(category="retail")
    return w


# ---------------------------------------------------------------------------
# Test 1: Determinism
# ---------------------------------------------------------------------------

def test_determinism():
    twin1 = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=20,
                                 num_devices=30, num_ip_blocks=20, num_steps=100)
    txs1 = twin1.run()

    twin2 = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=20,
                                 num_devices=30, num_ip_blocks=20, num_steps=100)
    txs2 = twin2.run()

    ids1 = [t["tx_id"] for t in txs1]
    ids2 = [t["tx_id"] for t in txs2]
    assert len(ids1) == len(ids2), f"tx count mismatch: {len(ids1)} != {len(ids2)}"
    assert ids1 == ids2, "Transaction ID sequences differ"
    assert txs1 == txs2, "Full transaction dicts differ"

    s1 = twin1.world.snapshot()
    s2 = twin2.world.snapshot()
    assert len(s1["transactions"]) == len(s2["transactions"])
    assert len(s1["trajectories"]) == len(s2["trajectories"])


# ---------------------------------------------------------------------------
# Test 2: Transaction dict format
# ---------------------------------------------------------------------------

def test_transaction_dict_format():
    twin = FinancialDigitalTwin(seed=42, num_accounts=20, num_merchants=10,
                                num_devices=10, num_ip_blocks=10, num_steps=20)
    txs = twin.run()
    assert txs, "twin produced no transactions"
    sample = txs[0]
    assert "from" in sample
    assert "to" in sample
    assert "from_id" not in sample
    assert "to_id" not in sample
    assert "tx_id" in sample
    assert "amount" in sample
    assert "is_fraud" in sample

    w = WorldState(seed=99)
    w.add_customer()
    w.add_account("CUST_00001")
    w.add_merchant(category="retail")
    tx = w.log_transaction("ACC_00001", "MERCHANT_00001", 100.0)
    assert "from" in tx
    assert "to" in tx
    assert tx["from"] == "ACC_00001"
    assert tx["to"] == "MERCHANT_00001"
    assert "from_id" not in tx
    assert "to_id" not in tx


# ---------------------------------------------------------------------------
# Test 3: Normal != Fraud statistics
# ---------------------------------------------------------------------------

def test_normal_vs_fraud_statistics():
    def small_attack_scheduler(world, twin):
        if world.current_step == 50:
            accounts = list(world.accounts.keys())[:5]
            run_typology("fan_in", world, random.Random(999),
                         main_account=accounts[0],
                         members=accounts[1:],
                         attack_id="TEST_ATTACK",
                         step_offset=0)

    twin = FinancialDigitalTwin(seed=42, num_accounts=100, num_merchants=30,
                                num_devices=50, num_ip_blocks=30, num_steps=200)
    txs = twin.run(attack_scheduler=small_attack_scheduler)

    normal_amts = [t["amount"] for t in txs if not t["is_fraud"]]
    fraud_amts = [t["amount"] for t in txs if t["is_fraud"]]
    assert len(normal_amts) > 0
    assert len(fraud_amts) > 0

    normal_mean = sum(normal_amts) / len(normal_amts)
    fraud_mean = sum(fraud_amts) / len(fraud_amts)
    assert fraud_mean > normal_mean, (
        f"fraud mean {fraud_mean:.2f} should exceed normal mean {normal_mean:.2f}"
    )

    # PHASE 1 (inverted): the old generator rounded fraud to multiples of
    # 1000 — a trivially learnable cheat-code. Amounts must now sit OFF the
    # grid with paise precision, and actual logged fraud transactions too.
    base_amount = _fraud_amount(random.Random(999))
    assert base_amount % 1000 != 0.0, (
        f"fraud amount still on round-1000 grid: {base_amount}"
    )
    assert base_amount == round(base_amount, 2), (
        f"fraud amount lacks paise precision: {base_amount}"
    )

    fraud_on_grid = sum(1 for a in fraud_amts if a % 1000 == 0.0)
    assert fraud_on_grid / max(1, len(fraud_amts)) < 0.02, (
        f"{fraud_on_grid}/{len(fraud_amts)} fraud txs on round-1000 grid"
    )

    non_round = sum(1 for a in normal_amts if a % 1000 != 0.0)
    assert non_round > 0, "some normal amounts should not be round-1000 multiples"


# ---------------------------------------------------------------------------
# Test 4: Open system — EXT_SALARY deposits
# ---------------------------------------------------------------------------

def test_open_system_ext_salary():
    def small_attack_scheduler(world, twin):
        if world.current_step == 50:
            accounts = list(world.accounts.keys())[:5]
            run_typology("fan_in", world, random.Random(999),
                         main_account=accounts[0],
                         members=accounts[1:],
                         attack_id="TEST_ATTACK",
                         step_offset=0)

    twin = FinancialDigitalTwin(seed=42, num_accounts=100, num_merchants=30,
                                num_devices=50, num_ip_blocks=30, num_steps=200)
    txs = twin.run(attack_scheduler=small_attack_scheduler)

    ext_salary_txs = [t for t in txs if t.get("from") == "EXT_SALARY"]
    assert len(ext_salary_txs) > 0, "EXT_SALARY transactions missing"
    assert all(t["to"] != "EXT_SALARY" for t in ext_salary_txs)
    assert "EXT_SALARY" in WorldState.EXTERNAL_ENTITIES
    assert "EXT_MERCHANT_PAYOUT" in WorldState.EXTERNAL_ENTITIES


# ---------------------------------------------------------------------------
# Test 5: Cold-start accounts
# ---------------------------------------------------------------------------

def test_cold_start_account():
    cold_world = WorldState(seed=1)
    cold_world.add_customer()
    cold_world.add_account("CUST_00001", balance=100000.0)
    cold_world.add_merchant(category="retail")
    cold_world.accounts["ACC_00001"].profile = build_normal_profile(
        random.Random(42), "ACC_00001"
    )

    cold_gen = NormalBehaviorGenerator(cold_world, seed=99)
    tx = None
    for _ in range(30):  # step until due (None = not due yet, acceptable)
        tx = cold_gen.step("ACC_00001")
        if tx is not None:
            break
    if tx is not None:
        assert "from" in tx
        assert "to" in tx
        assert not tx["is_fraud"]
        assert len(cold_world.accounts["ACC_00001"].linked_devices) > 0


# ---------------------------------------------------------------------------
# Test 6: Isolated nodes
# ---------------------------------------------------------------------------

def test_isolated_nodes():
    iso_world = WorldState(seed=5)
    iso_world.add_customer()
    iso_world.add_account("CUST_00001", balance=50000.0)
    assert len(iso_world.relationships) == 0
    iso_world.add_merchant(category="retail")
    tx = iso_world.log_transaction("ACC_00001", "MERCHANT_00001", 100.0)
    assert tx is not None
    assert tx["from"] == "ACC_00001"


# ---------------------------------------------------------------------------
# Test 7: All 8 typologies — structural checks
# ---------------------------------------------------------------------------

def _tx_by_id(w: WorldState, tid: str) -> dict:
    return next(t for t in w.transactions if t["tx_id"] == tid)


def test_typology_fan_in():
    w = make_test_world(100)
    accts = list(w.accounts.keys())
    rng = random.Random(42)
    tx_ids = fan_in(w, rng, main_account=accts[0], members=accts[1:4],
                    attack_id="TEST_FANIN")
    assert len(tx_ids) == 3
    for tid in tx_ids:
        tx = _tx_by_id(w, tid)
        assert tx["to"] == accts[0], f"fan-in tx {tid} to={tx['to']}"
        assert tx["is_fraud"]


def test_typology_fan_out():
    w = make_test_world(200)
    accts = list(w.accounts.keys())
    rng = random.Random(42)
    tx_ids = fan_out(w, rng, main_account=accts[0], members=accts[1:4],
                     attack_id="TEST_FANOUT")
    assert len(tx_ids) == 3
    for tid in tx_ids:
        tx = _tx_by_id(w, tid)
        assert tx["from"] == accts[0]


def test_typology_cycle_ring_structure():
    w = make_test_world(300)
    accts = list(w.accounts.keys())[:5]
    rng = random.Random(42)
    tx_ids = cycle(w, rng, members=accts, attack_id="TEST_CYCLE")
    assert len(tx_ids) == 5
    for i, tid in enumerate(tx_ids):
        tx = _tx_by_id(w, tid)
        assert tx["from"] == accts[i]
        assert tx["to"] == accts[(i + 1) % len(accts)]


def test_typology_scatter_gather_margin():
    w = make_test_world(400)
    accts = list(w.accounts.keys())
    rng = random.Random(42)
    tx_ids = scatter_gather(w, rng, main_account=accts[0],
                            intermediaries=accts[1:3],
                            beneficiary=accts[3],
                            attack_id="TEST_SG")
    assert len(tx_ids) == 4
    scatter_txs = [t for t in w.transactions if t["from"] == accts[0] and t["is_fraud"]]
    gather_txs = [t for t in w.transactions if t["to"] == accts[3] and t["is_fraud"]]
    assert scatter_txs and gather_txs
    total_scatter = sum(t["amount"] for t in scatter_txs)
    total_gather = sum(t["amount"] for t in gather_txs)
    assert total_gather < total_scatter, (
        f"gather {total_gather:.2f} should be < scatter {total_scatter:.2f} (margin)"
    )


def test_typology_gather_scatter_counts():
    w = make_test_world(500)
    accts = list(w.accounts.keys())
    rng = random.Random(42)
    tx_ids = gather_scatter(w, rng, sources=accts[1:4],
                            main_account=accts[0],
                            targets=accts[4:6],
                            attack_id="TEST_GS")
    assert len(tx_ids) == 5  # 3 gather + 2 scatter


def test_typology_bipartite_counts():
    w = make_test_world(600)
    accts = list(w.accounts.keys())
    rng = random.Random(42)
    tx_ids = bipartite(w, rng, sources=accts[:3], targets=accts[3:6],
                       attack_id="TEST_BIP")
    assert len(tx_ids) == 3


def test_typology_stack_counts():
    w = make_test_world(700)
    accts = list(w.accounts.keys())
    rng = random.Random(42)
    layers = [accts[0:2], accts[2:4], accts[4:6]]
    tx_ids = stack(w, rng, layers=layers, attack_id="TEST_STACK")
    assert len(tx_ids) == 8  # 2x2 per layer boundary, 2 boundaries


def test_typology_random_propagation():
    w = make_test_world(800)
    accts = list(w.accounts.keys())
    rng = random.Random(42)
    tx_ids = random_typology(w, rng, main_account=accts[0], depth=3,
                             attack_id="TEST_RANDOM")
    assert len(tx_ids) == 3


# ---------------------------------------------------------------------------
# Test 8: Trajectory logging
# ---------------------------------------------------------------------------

def test_trajectory_logging():
    w = make_test_world(900)
    accts = list(w.accounts.keys())
    actions = [
        {"from": accts[1], "to": accts[0], "amount": 50000.0},
        {"from": accts[2], "to": accts[0], "amount": 50000.0},
    ]
    traj = w.log_trajectory("fan_in", actions, {"attack_id": "TRAJ_TEST"})
    assert "trajectory_id" in traj
    assert "attack_type" in traj
    assert traj["attack_type"] == "fan_in"
    assert len(traj["actions"]) == 2
    assert len(w.trajectories) == 1
    assert w.trajectories[0]["attack_type"] == "fan_in"
