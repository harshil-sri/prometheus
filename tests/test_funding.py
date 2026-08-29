"""
test_funding.py — Ring-fenced per-attack-type funding reserves (updates.md 2.3).

Verifies the eval-phase funding partition: disjoint per-type pools, full
determinism, compiler selection EXCLUSIVELY inside a type's own reserve, and
loud funding diagnostics (per-solvency-tier pool size) on thin economies.

Env: run from repo root as
    /home/kartik/.venvs/global/bin/python -m pytest tests/test_funding.py -q
"""

from __future__ import annotations

import pytest

from twin.twin import FinancialDigitalTwin
from attack.compiler import AttackCompiler
from attack.funding import reserve_funding_pools
from attack.benchmark_attacks import BENCHMARK_ATTACKS


def _specs(*aids: str) -> dict:
    return {aid: dict(BENCHMARK_ATTACKS[aid]) for aid in aids}


def _settled_twin(seed: int = 7, accounts: int = 150, steps: int = 30):
    twin = FinancialDigitalTwin(
        seed=seed, num_accounts=accounts, num_merchants=25,
        num_devices=40, num_ip_blocks=20, num_steps=steps,
    )
    twin.run()
    return twin


# ------------------------------------------------------------------------- #
# 1. Disjointness + determinism + principal-descending exec order
# ------------------------------------------------------------------------- #
def test_pools_are_disjoint_deterministic_and_principal_ordered():
    twin = _settled_twin()
    specs = _specs("A1", "A2", "A3", "A4", "A5", "A6")

    r1 = reserve_funding_pools(twin.world, specs, eval_repeats=5)
    r2 = reserve_funding_pools(twin.world, specs, eval_repeats=5)

    # Deterministic: identical partitions and diagnostics across runs.
    assert r1.pools == r2.pools
    assert r1.diag == r2.diag
    assert r1.order == ["A5", "A4", "A6", "A1", "A2", "A3"]

    # Disjoint: an account cannot be reserved for two attack types.
    seen: set = set()
    for aid in r1.order:
        pool = set(r1.pools[aid])
        assert pool.isdisjoint(seen), f"pool overlap for {aid}"
        seen |= pool

    # Every reserved account is a real, funded account.
    for aid, pool in r1.pools.items():
        assert pool, f"empty reserve for {aid}"
        for acc_id in pool:
            assert twin.world.accounts[acc_id].balance > 0.0


# ------------------------------------------------------------------------- #
# 2. Cheap types are fully anchored (every repeat gets a 100%-tier main)
# ------------------------------------------------------------------------- #
def test_cheap_type_is_fully_anchored():
    twin = _settled_twin()
    r = reserve_funding_pools(twin.world, _specs("A3", "A5"), eval_repeats=5)
    # A3 = ₹10 principal: essentially every account can carry it.
    assert r.diag["A3"]["tier_100"] >= r.diag["A3"]["repeats"]
    assert r.diag["A3"]["tier_100"] == r.diag["A3"]["n_accounts"]


# ------------------------------------------------------------------------- #
# 3. Compiler draws accounts ONLY from its reserved pool
# ------------------------------------------------------------------------- #
def test_compiler_selects_only_from_its_funded_pool():
    twin = _settled_twin(seed=3, accounts=60, steps=12)
    # Deterministic pick of 5 accounts as the ring-fenced reserve.
    pool = sorted(twin.world.accounts.keys())[:5]
    compiler = AttackCompiler(twin, seed=1, funded_pool=pool)

    plan = compiler.compile({
        "attack_id": "SFX",
        "attack_type": "A1",
        "goal": "move_funds",
        "amount": 500.0,
        "currency": "INR",
        "resources": {"accounts": 3, "devices": 1, "days": 2},
        "constraints": {},
    })
    entities = plan["entities"]
    pool_set = set(pool)

    assert entities["main_account"] in pool_set
    assert len(entities["members"]) > 0
    for member in entities["members"]:
        assert member in pool_set
    # Loud stats: size + per-tier funding of the reserve BEFORE selection.
    stats = compiler.last_funding_stats
    assert stats is not None
    assert stats["pool_n"] == len(pool)
    assert 0 <= stats["tier_100"] <= stats["pool_n"]
    assert 0 <= stats["tier_50"] <= stats["pool_n"]
    assert 0 <= stats["tier_20"] <= stats["pool_n"]
    # Precondition reports the reserve size too.
    assert "funded_pool_5_accounts" in plan["preconditions"]


# ------------------------------------------------------------------------- #
# 4. Thin economy → loud funding warning (no silent starvation)
# ------------------------------------------------------------------------- #
def test_thin_economy_produces_loud_funding_warning():
    twin = _settled_twin(seed=11, accounts=120, steps=20)
    # Simulate a depleted upper tail: nobody can anchor A5's ₹300,000.
    for acc in twin.world.accounts.values():
        acc.balance = min(acc.balance, 90_000.0)

    r = reserve_funding_pools(twin.world, _specs("A5", "A4"), eval_repeats=3)
    assert r.diag["A5"]["tier_100"] == 0
    assert r.diag["A4"]["tier_100"] == 0
    assert any("A5" in w for w in r.warnings)

    # Selection still degrades gracefully INSIDE the (thin) reserve.
    compiler = AttackCompiler(twin, seed=2, funded_pool=r.pools["A5"])
    plan = compiler.compile(dict(BENCHMARK_ATTACKS["A5"]))
    assert plan["entities"]["main_account"] in set(r.pools["A5"])
    assert compiler.last_funding_stats["tier_100"] == 0


# ------------------------------------------------------------------------- #
# 5. Required-balance math is recorded and consistent
# ------------------------------------------------------------------------- #
def test_required_balance_accounting_is_exact():
    twin = _settled_twin(seed=17, accounts=200, steps=25)
    r = reserve_funding_pools(twin.world, _specs("A1"), eval_repeats=4,
                              safety=2.0)
    d = r.diag["A1"]
    assert d["required_balance"] == pytest.approx(100_000.0 * 4 * 2.0)
    tot = sum(twin.world.accounts[a].balance for a in r.pools["A1"])
    assert d["total_balance"] == pytest.approx(tot)
    assert d["total_balance"] >= d["required_balance"]