"""
test_phase1_integrity.py — Phase 1 gate: twin integrity.

Covers the Phase 1 gate requirements from PROMETHEUS_CONTEXT.md §5:
  1. Fraud amounts are OFF the round-1000 grid (de-fraudified, paise precision)
  2. Internal money-supply conservation across all 8 typologies
  3. Per-hop margin economics: cycle principal recovery, scatter-gather /
     gather-scatter / stack / random all chain off ACTUAL transferred amounts
     (no value created or destroyed)
  4. Temporal spreading: fraud inter-arrival steps overlap normal cadence —
     no more "all fraud lands on consecutive steps"
  5. Open-system accounting: only EXT_* endpoints move internal supply
  6. Performance artifact artifacts/twin_perf.json respects the <30s budget

All randomness flows through seeded RNGs -> deterministic outcomes.
"""

from __future__ import annotations

import json
import math
import os
import random

import pytest

from src.twin.core import WorldState
from src.twin.typologies import (
    _fraud_amount,
    _split_exact,
    fan_in,
    fan_out,
    cycle,
    scatter_gather,
    gather_scatter,
    bipartite,
    stack,
    random_typology,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERF_ARTIFACT = os.path.join(ROOT, "artifacts", "twin_perf.json")


def funded_world(seed: int = 42, n_accounts: int = 12, balance: float = 5_000_000.0) -> WorldState:
    """World where every account is comfortably funded (no solvency clamps)."""
    w = WorldState(seed=seed)
    for _ in range(n_accounts):
        c = w.add_customer()
        w.add_account(c.customer_id, balance=balance)
    return w


def accounts_of(w: WorldState):
    return list(w.accounts.keys())


# ---------------------------------------------------------------------------
# 1. De-fraudified amounts
# ---------------------------------------------------------------------------

def test_fraud_amounts_off_round_grid():
    rng = random.Random(2024)
    amounts = [_fraud_amount(rng) for _ in range(500)]
    on_grid = [a for a in amounts if a % 1000 == 0.0]
    assert not on_grid, f"round-grid amounts leaked: {on_grid[:5]}"
    # paise precision: overwhelmingly non-integer rupees
    with_cents = sum(1 for a in amounts if int(round(a * 100)) % 100 != 0)
    assert with_cents >= 400, f"only {with_cents}/500 have paise-level precision"
    # still heavy-tailed vs normal retail band
    assert min(amounts) > 0 and max(amounts) <= 200_000 * 1.10 + 1


def test_split_exact_sums_back():
    rng = random.Random(7)
    for total in (25_000.0, 123_456.78, 999.99, 10.0):
        for n in (1, 2, 3, 7, 13):
            parts = _split_exact(total, n, rng)
            assert len(parts) == n
            assert math.isclose(sum(parts), round(total, 2), rel_tol=1e-9, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# 2. Money conservation across all 8 typologies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typology_run", ["fan_in", "fan_out", "cycle",
                                          "scatter_gather", "gather_scatter",
                                          "bipartite", "stack", "random"])
def test_internal_supply_conserved_per_typology(typology_run):
    w = funded_world(seed=100 + hash(typology_run) % 50)
    accts = accounts_of(w)
    supply_before = w.internal_supply()
    tx_ids: list = []

    if typology_run == "fan_in":
        tx_ids = fan_in(w, random.Random(1), main_account=accts[0],
                        members=accts[1:6], amount=200_000.0, margin_ratio=None)
    elif typology_run == "fan_out":
        tx_ids = fan_out(w, random.Random(2), main_account=accts[0],
                         members=accts[1:6], amount=200_000.0)
    elif typology_run == "cycle":
        tx_ids = cycle(w, random.Random(3), members=accts[0:5],
                       amount=150_000.0, margin_ratio=0.07)
    elif typology_run == "scatter_gather":
        tx_ids = scatter_gather(w, random.Random(4), main_account=accts[0],
                                intermediaries=accts[1:4], beneficiary=accts[5],
                                amount=300_000.0, margin_ratio=0.05)
    elif typology_run == "gather_scatter":
        tx_ids = gather_scatter(w, random.Random(5), sources=accts[1:5],
                                main_account=accts[0], targets=accts[6:9],
                                amount=250_000.0, margin_ratio=0.09)
    elif typology_run == "bipartite":
        tx_ids = bipartite(w, random.Random(6), sources=accts[1:4],
                           targets=accts[6:10], amount=180_000.0)
    elif typology_run == "stack":
        layers = [accts[0:2], accts[2:4], accts[4:6], accts[6:8]]
        tx_ids = stack(w, random.Random(7), layers=layers,
                       amount=400_000.0, margin_ratio=0.06)
    elif typology_run == "random":
        tx_ids = random_typology(w, random.Random(8), main_account=accts[0],
                                 depth=5, amount=120_000.0, margin_ratio=0.04)

    assert tx_ids, f"{typology_run}: produced nothing"
    assert math.isclose(w.internal_supply(), supply_before, rel_tol=1e-9), (
        f"{typology_run} changed internal supply: "
        f"{supply_before} -> {w.internal_supply()}"
    )


# ---------------------------------------------------------------------------
# 3. Margin economics chained off actuals
# ---------------------------------------------------------------------------

def test_cycle_principal_recovery_formula():
    w = funded_world(seed=301)
    accts = accounts_of(w)
    P, m, n = 100_000.0, 0.10, 5
    tx_ids = cycle(w, random.Random(11), members=accts[0:n], amount=P, margin_ratio=m)
    assert len(tx_ids) == n
    flows = w.flow_summary(tx_ids)
    initiator = accts[0]
    # n-1 intermediaries each keep an m-cut -> initiator recovers P*(1-m)^(n-1)
    expected_recover = P * ((1 - m) ** (n - 1))
    assert flows[initiator]["in"] == pytest.approx(expected_recover, rel=1e-9)
    # every intermediate keeps its cut -> strictly positive net
    for a in accts[1:n]:
        assert flows[a]["net"] > 0, f"{a} should profit from its hop"
    # initiator loses exactly what the ring retained
    assert flows[initiator]["net"] == pytest.approx(expected_recover - P, rel=1e-9)
    # geometric decay: successive edge amounts shrink by factor (1-m)
    amts = [t["amount"] for tid in tx_ids
            for t in w.transactions if t["tx_id"] == tid]
    for prev, cur in zip(amts, amts[1:]):
        assert cur == pytest.approx(prev * (1 - m), rel=1e-9)


def test_hop_chain_off_actual_logged_amount():
    """Every downstream amount must derive from the previous hop's logged
    amount, not a requested one — robust under solvency clamping."""
    w = WorldState(seed=302)
    c = w.add_customer()
    w.add_account(c.customer_id, balance=1_000.0)          # poor initiator
    rich = w.add_customer(); w.add_account(rich.customer_id, balance=10_000_000.0)
    mids = []
    for _ in range(3):
        cc = w.add_customer(); mid = w.add_account(cc.customer_id, balance=0.0)
        mids.append(mid.account_id)
    # fund mids just above what they'll need so clamps don't zero them out
    for i, mid in enumerate(mids):
        w.accounts[mid].balance = 60_000.0 * (0.8 ** i)

    tx_ids = random_typology(w, random.Random(12),
                             main_account="ACC_00001", depth=4,
                             amount=50_000.0, margin_ratio=0.10)
    assert len(tx_ids) == 4
    amounts = [next(t["amount"] for t in w.transactions if t["tx_id"] == tid)
               for tid in tx_ids]
    # first hop clamped by the poor account's capacity
    cap = max(1_000.0 * (1 - 0.02), 0.0)
    assert amounts[0] == pytest.approx(round(cap, 2), abs=0.01)
    for prev, cur in zip(amounts, amounts[1:]):
        assert cur <= prev * 1.0001          # never more than arrived
        assert cur > 0


def test_scatter_gather_full_principal_no_haircut():
    """Legacy bug: source scattered only half the principal. Fixed — full."""
    w = funded_world(seed=303)
    accts = accounts_of(w)
    P, m = 200_000.0, 0.08
    tx_ids = scatter_gather(w, random.Random(21), main_account=accts[0],
                            intermediaries=accts[1:4], beneficiary=accts[5],
                            amount=P, margin_ratio=m)
    assert len(tx_ids) == 6                     # 3 scatter + 3 gather
    flows = w.flow_summary(tx_ids)
    scatter_total = flows[accts[1]]["in"] + flows[accts[2]]["in"] + flows[accts[3]]["in"]
    gather_total = flows[accts[5]]["in"]
    assert scatter_total == pytest.approx(P, rel=1e-9)      # FULL principal left main
    assert gather_total == pytest.approx(P * (1 - m), rel=1e-9)


def test_scatter_gather_margin_parameterized():
    results = {}
    for tag, m in (("low", 0.05), ("high", 0.15)):
        w = funded_world(seed=304)
        accts = accounts_of(w)
        tx_ids = scatter_gather(w, random.Random(22), main_account=accts[0],
                                intermediaries=accts[1:4], beneficiary=accts[5],
                                amount=100_000.0, margin_ratio=m)
        ben_in = w.flow_summary(tx_ids)[accts[5]]["in"]
        results[tag] = ben_in
    ratio = results["high"] / results["low"]
    assert ratio == pytest.approx(0.85 / 0.95, rel=1e-6)


def test_gather_scatter_pool_economics():
    w = funded_world(seed=305)
    accts = accounts_of(w)
    P, m = 160_000.0, 0.08
    tx_ids = gather_scatter(w, random.Random(31), sources=accts[1:5],
                            main_account=accts[0], targets=accts[6:9],
                            amount=P, margin_ratio=m)
    assert len(tx_ids) == 4 + 3
    flows = w.flow_summary(tx_ids)
    gathered = flows[accts[0]]["in"]
    scattered = sum(flows[t]["in"] for t in accts[6:9])
    assert gathered == pytest.approx(P, rel=1e-9)
    assert scattered == pytest.approx(P * (1 - m), rel=1e-9)
    # legacy bug would have produced gathered == 4*P and scattered == 3*P
    assert abs(scattered - 3 * P) > 1.0, "legacy create-money behaviour returned?"


def test_stack_layer_cascade_decreasing_and_conserved():
    w = funded_world(seed=306)
    accts = accounts_of(w)
    P, m = 90_000.0, 0.10
    layers = [accts[0:2], accts[2:4], accts[4:6]]
    before = w.internal_supply()
    tx_ids = stack(w, random.Random(41), layers=layers, amount=P, margin_ratio=m)
    assert len(tx_ids) == 8                     # 2x2 per boundary x 2 boundaries
    amounts = [t["amount"] for tid in tx_ids
               for t in w.transactions if t["tx_id"] == tid]
    b0, b1 = amounts[:4], amounts[4:]
    assert sum(b0) == pytest.approx(P, rel=1e-9)              # boundary 0 moves full pool
    assert sum(b1) == pytest.approx(P * (1 - m), rel=1e-9)    # layer L1 keeps its cut
    # pools strictly decrease; individual jittered shares may band-overlap,
    # so compare boundary means rather than extremes
    assert sum(b1) / len(b1) < sum(b0) / len(b0)
    assert math.isclose(w.internal_supply(), before, rel_tol=1e-9)


def test_solvency_clamp_prevents_overdraft():
    w = WorldState(seed=307)
    c = w.add_customer()
    poor = w.add_account(c.customer_id, balance=5_000.0).account_id
    c2 = w.add_customer()
    recv = w.add_account(c2.customer_id, balance=0.0).account_id
    tx_ids = fan_in(w, random.Random(51), main_account=recv,
                    members=[poor], amount=1_000_000.0)
    assert len(tx_ids) == 1
    amt = next(t["amount"] for t in w.transactions if t["tx_id"] == tx_ids[0])
    assert amt == pytest.approx(4_900.0, abs=0.01)   # clamped to 98% of balance
    assert w.accounts[poor].balance >= 0.0           # never overdrawn


# ---------------------------------------------------------------------------
# 4. Temporal spreading
# ---------------------------------------------------------------------------

def test_typology_actions_temporally_spread():
    w = funded_world(seed=401)
    accts = accounts_of(w)
    tx_ids = cycle(w, random.Random(61), members=accts[0:6],
                   amount=90_000.0, margin_ratio=0.05)
    steps = {tid: next(t["step"] for t in w.transactions if t["tx_id"] == tid)
             for tid in tx_ids}
    ordered = [steps[tid] for tid in tx_ids]
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    assert all(g >= 1 for g in gaps)
    assert any(g >= 2 for g in gaps), (
        f"fraud still packed at consecutive steps: gaps={gaps}"
    )


def test_fraud_iat_overlaps_normal_cadence():
    """Integration: run a twin with periodic attacks; median gap between
    consecutive fraud transactions must sit in the same order of magnitude as
    the normal inter-arrival cadence (previously it was hard-packed at 1 step
    while normal cadence was 3-25)."""
    from src.twin.twin import FinancialDigitalTwin

    def scheduler(world, twin):
        if world.current_step % 40 == 0 and world.current_step > 0:
            accts = list(world.accounts.keys())
            cycle(world, random.Random(700 + world.current_step),
                  members=accts[2:7], amount=80_000.0, margin_ratio=0.05)

    twin = FinancialDigitalTwin(seed=43, num_accounts=120, num_merchants=25,
                                num_devices=40, num_ip_blocks=20, num_steps=240)
    twin.run(attack_scheduler=scheduler)

    fraud_steps = sorted(t["step"] for t in twin.world.transactions if t["is_fraud"])
    assert len(fraud_steps) >= 20, f"too few fraud txs: {len(fraud_steps)}"
    gaps = [b - a for a, b in zip(fraud_steps, fraud_steps[1:]) if b != a]
    median_gap = sorted(gaps)[len(gaps) // 2]
    assert median_gap >= 2, f"median fraud IAT still 1 step ({gaps[:10]})"
    assert any(g >= 4 for g in gaps), "no temporal spread at all"
    # order-of-magnitude overlap with the normal 3..25 band
    assert median_gap <= 25 * 4


# ---------------------------------------------------------------------------
# 5. Open-system accounting
# ---------------------------------------------------------------------------

def test_only_external_entities_move_internal_supply():
    w = funded_world(seed=402)
    accts = accounts_of(w)
    s0 = w.internal_supply()

    bipartite(w, random.Random(71), sources=accts[1:4],
              targets=accts[5:8], amount=50_000.0)
    assert w.internal_supply() == pytest.approx(s0, rel=1e-9)

    before_ext = w.internal_supply()
    w.log_transaction(from_id="EXT_SALARY", to_id=accts[0], amount=77_777.0,
                      step=w.current_step, category="salary")
    assert w.internal_supply() == pytest.approx(before_ext + 77_777.0, rel=1e-9)


# ---------------------------------------------------------------------------
# 6. Performance artifact gate
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(PERF_ARTIFACT),
                    reason="artifacts/twin_perf.json not yet generated; "
                           "run scripts/bench_twin.py")
def test_perf_artifact_within_budget():
    with open(PERF_ARTIFACT, "r") as f:
        perf = json.load(f)
    required = {"num_accounts", "num_steps", "elapsed_seconds",
                "budget_seconds", "seed", "tx_count", "passed"}
    missing = required - set(perf.keys())
    assert not missing, f"perf artifact missing keys: {missing}"
    assert perf["num_accounts"] >= 2000 and perf["num_steps"] >= 200, (
        f"benchmark below gate scale: {perf['num_accounts']}x{perf['num_steps']}"
    )
    assert perf["passed"] is True
    assert perf["elapsed_seconds"] <= perf["budget_seconds"]
