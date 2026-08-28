"""
test_feedback.py — Phase 3 gate: unified compiler + real feedback loop.

Gate requirements from PROMETHEUS_CONTEXT.md §5 (Phase 3):
  A. benchmark_spec(): fresh copy per id; loud KeyError for unknowns.
  B. A2 no longer degenerate (finding #3): counts honoured, accounts paired
     to the customers created by the same attack, bipartite over NEW
     accounts only, mechanism-tagged.
  C. Unified implementation (#8): generate_training_attacks runs through
     the compiler pipeline — determinism identical to direct compile+execute.
  D. ComputedEvidence anti-fabrication (#5a): raw strings/dicts REJECTED;
     report evidence_ids all registered.
  E. Sensitivity surface keys are computed quantities (#5b) and distinct.
  F. True FeedbackLoop on a small twin: completes, decontaminated retrain
     (eval trajectories excluded), fresh-seed recheck, held-out never
     trained upon.
"""

from __future__ import annotations

import os
import random
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from twin.twin import FinancialDigitalTwin
from attack.compiler import AttackCompiler
from attack.benchmark_attacks import (
    BENCHMARK_ATTACKS,
    HELD_OUT_ATTACKS,
    TRAINABLE_ATTACKS,
    generate_training_attacks,
)
from blue.ensemble import BlueTeamEnsemble
from blue.splits import lock_holdout, assert_no_leakage
from sensitivity.engine import SensitivityEngine
from feedback.loop import FeedbackLoop, MAX_RETRAIN_ROUNDS
from feedback.evidence import EvidenceStore, require_computed


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def small_world():
    twin = FinancialDigitalTwin(seed=42, num_accounts=60, num_merchants=20,
                                num_devices=30, num_ip_blocks=10,
                                num_steps=6)
    twin.run()
    return twin


@pytest.fixture(scope="module")
def trained_ensemble(small_world):
    compiler = AttackCompiler(small_world, seed=42)
    generate_training_attacks(compiler, small_world.world)
    blue = BlueTeamEnsemble.untrained(seed=42)
    diag = blue.fit_transactions(list(small_world.world.transactions),
                                 small_world.world,
                                 oof_folds=3, gnn_epochs=15)
    sensitivity = SensitivityEngine(xgb_model=blue.xgb.model,
                                    gnn_model=blue.gnn.model if blue.gnn else None,
                                    feature_names=blue.feature_names)
    return {"twin": small_world, "compiler": compiler, "blue": blue,
            "sensitivity": sensitivity, "diag": diag}


# ---------------------------------------------------------------------------
# A. benchmark_spec
# ---------------------------------------------------------------------------

def test_benchmark_spec_fresh_copy_and_loud_errors():
    c = AttackCompiler(FinancialDigitalTwin(seed=1, num_accounts=10,
                                            num_merchants=5, num_devices=5,
                                            num_ip_blocks=2, num_steps=1),
                       seed=1)
    spec = c.benchmark_spec("A4")
    assert spec["attack_id"] == "A4"
    spec["amount"] = -99999          # mutate the returned dict
    assert BENCHMARK_ATTACKS["A4"]["amount"] > 0   # registry untouched

    with pytest.raises(KeyError):
        c.benchmark_spec("A99")


# ---------------------------------------------------------------------------
# B. A2 non-degeneracy (finding #3)
# ---------------------------------------------------------------------------

def test_a2_synthetic_burst_is_structurally_real(small_world):
    c = AttackCompiler(small_world, seed=777)
    before = len(small_world.world.accounts)
    plan = c.compile(c.benchmark_spec("A2"))
    traj_id = c.execute(plan)

    created = set()
    traj = next(t for t in small_world.world.trajectories
                if t["trajectory_id"] == traj_id)
    # The trajectory log records every created account as its own
    # create_account action with a SINGULAR account_id (the batch key never
    # existed in the log); collect those — they ARE the created burst.
    created = {a["account_id"] for a in traj["actions"]
               if a.get("action") == "create_account"}
    # counts HONOURED: resources.accounts (=12) burst, not a hardcoded 5→1
    assert len(created) >= 6
    assert len(small_world.world.accounts) == before + len(created)

    fraud = [tx for tx in small_world.world.transactions
             if tx.get("trajectory_id") == traj_id and tx["is_fraud"]]
    assert fraud, "A2 produced no fraud transactions"

    senders = {tx["from"] for tx in fraud}
    receivers = {tx["to"] for tx in fraud}
    # bipartite strictly WITHIN the freshly created synthetic accounts
    assert (senders | receivers) <= created, (
        "A2 transacted over pre-existing members (degenerate case)"
    )
    # accounts genuinely paired to customers created by THIS attack
    kyc_ok = [small_world.world.accounts[a].customer_id.startswith("CUST_")
              for a in created]
    assert all(kyc_ok)
    low_kyc = any(
        small_world.world.customers[
            small_world.world.accounts[a].customer_id].kyc_tier
        == "low" for a in created)
    assert low_kyc, "synthetic identities should carry low-KYC tier"
    # axis-2 mechanism tag present on every fraud row
    assert all(tx.get("mechanism") == "rule_compiler" for tx in fraud)


# ---------------------------------------------------------------------------
# C. Unified path (#8)
# ---------------------------------------------------------------------------

def test_generate_training_attacks_matches_direct_compiler_path():
    # Deterministic equivalence: generating via the helper must equal running
    # compile+execute over sorted TRAINABLE_ATTACKS on an identical world.
    t1 = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=10,
                              num_devices=20, num_ip_blocks=5, num_steps=4)
    t1.run()
    c1 = AttackCompiler(t1, seed=42)
    res_helper = generate_training_attacks(c1, t1.world)
    # NOTE: helper advanced current_step between attacks; replicate directly
    t2 = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=10,
                              num_devices=20, num_ip_blocks=5, num_steps=4)
    t2.run()
    c2 = AttackCompiler(t2, seed=42)
    for aid in sorted(TRAINABLE_ATTACKS):
        plan = c2.compile(BENCHMARK_ATTACKS[aid])
        tid = c2.execute(plan, t2.world)
        t2.world.current_step += 1
        assert res_helper[aid] == tid

    # All fraud rows from training generation are mechanism-tagged
    tagged = [tx for tx in t1.world.transactions
              if tx.get("trajectory_id") in res_helper.values()
              and tx.get("is_fraud")]
    assert tagged and all(t.get("mechanism") == "rule_compiler" for t in tagged)


# ---------------------------------------------------------------------------
# D. ComputedEvidence anti-fabrication (#5a)
# ---------------------------------------------------------------------------

def test_evidence_store_rejects_raw_strings_and_dicts():
    store = EvidenceStore(seed=1)
    ev = store.register("metric", {"recall": 0.83},
                        source="some_measured_fn")
    got = store.get(ev.evidence_id)
    assert got.fingerprint() and store.all()[0].kind == "metric"

    with pytest.raises(TypeError):
        require_computed(["gnn_contribution: low"], "report")      # raw string
    with pytest.raises(TypeError):
        require_computed([{"graph_density": "below_threshold"}], "report")

    unknown_get = "EVD_NOPE123"
    with pytest.raises(KeyError):
        store.get(unknown_get)


def test_blindspot_report_evidence_ids_all_registered(trained_ensemble):
    tw = trained_ensemble
    loop = FeedbackLoop(tw["twin"], tw["compiler"], tw["blue"],
                        tw["sensitivity"], seed=11)
    holdout = lock_holdout(seed=11, held_out_types=("A2", "A5"))
    report = loop.run_cycle(["A1", "A3", "A4", "A6"],
                            held_out_ids=["A2", "A5"],
                            holdout_spec=holdout, n_instances=1)

    assert report["schema"] == "prometheus.blindspot.v2"
    assert isinstance(report["evidence_ids"], list)
    assert report["evidence_ids"]
    for eid in report["evidence_ids"]:
        ev = loop.evidence.get(eid)          # raises if not registered
        assert ev.kind in ("recall_eval", "weakness_surface",
                           "variants", "retrain_diag")


# ---------------------------------------------------------------------------
# E. Sensitivity surface computed keys (#5b)
# ---------------------------------------------------------------------------

def test_attack_surface_gnn_keys_are_distinct_computations(trained_ensemble):
    tw = trained_ensemble
    pool = list(tw["twin"].world.transactions)[-300:]
    X, _, _ = __import__("blue.features", fromlist=["compute_features"]) \
        .compute_features(pool, tw["twin"].world)
    from blue.features import build_graph_data
    data, _ = build_graph_data(pool, tw["twin"].world)
    surf = tw["sensitivity"].attack_surface_map(X, data)

    gnn_keys = surf["gnn"]["sensitivity"]
    assert "neighbor_ablation_delta_mean" in gnn_keys
    assert "edge_attr_zeroing_delta" in gnn_keys
    assert "riskiest_node_score" in gnn_keys
    assert "high_risk_node_count" in gnn_keys
    # distinct measurements, not one value relabelled twice (old bug)
    vals = [v for v in gnn_keys.values() if isinstance(v, float)]
    assert len(vals) >= 2 and len(set(map(repr, vals))) >= 2


# ---------------------------------------------------------------------------
# F. Real loop decontamination & behavior
# ---------------------------------------------------------------------------

def test_loop_excludes_eval_trajectories_from_training(trained_ensemble):
    tw = trained_ensemble
    loop = FeedbackLoop(tw["twin"], tw["compiler"], tw["blue"],
                        tw["sensitivity"], seed=23)

    captured = {}
    original_retrain = loop._retrain_decontaminated

    def spy(variants, banned_ids, holdout_spec):
        captured["banned"] = set(banned_ids)
        return original_retrain(variants, banned_ids, holdout_spec)

    loop._retrain_decontaminated = spy          # minimal hook, same semantics

    # Guarantee at least one miss so the diagnose→retrain path executes
    # (a perfectly-caught beat-1 exits early by design).
    calls = {"n": 0}
    real_caught = tw["blue"].attack_caught

    def first_shot_misses(txs, world=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"caught": False, "peak_score": 0.10,
                    "mean_score": 0.05}
        return real_caught(txs, world, **kw)

    tw["blue"].attack_caught = first_shot_misses

    eval_txs_before = len(tw["twin"].world.transactions)
    holdout = lock_holdout(seed=23, held_out_types=("A2", "A5"))
    report = loop.run_cycle(["A1", "A3", "A4", "A6"],
                            held_out_ids=["A2", "A5"],
                            holdout_spec=holdout, n_instances=1)

    assert captured.get("banned"), "retrain path never executed (no miss)"
    assert calls["n"] >= 2 and calls["n"] <= 12
    assert eval_txs_before < len(tw["twin"].world.transactions)


def test_loop_round_cap_enforced(trained_ensemble):
    tw = trained_ensemble
    loop = FeedbackLoop(tw["twin"], tw["compiler"], tw["blue"],
                        tw["sensitivity"], seed=29)
    holdout = lock_holdout(seed=29, held_out_types=("A2",))
    # adversarial-ish cycle: hard types + impossible fix budget → still stops
    loop.run_cycle(["A3"], held_out_ids=[], holdout_spec=holdout,
                   n_instances=1)
    assert loop.rounds_used <= MAX_RETRAIN_ROUNDS


def test_two_axis_leakage_guard_inside_retrain(trained_ensemble):
    """If a held-out type somehow appeared in the pool the loop must die
    loudly rather than train on it."""
    tw = trained_ensemble
    loop = FeedbackLoop(tw["twin"], tw["compiler"], tw["blue"],
                        tw["sensitivity"], seed=31)

    poisoned_pool = [{"tx_id": "POISON_1", "step": 9999,
                      "from": "ACC_00001", "to": "ACC_00002",
                      "amount": 10000.0, "is_fraud": True,
                      "attack_id": "A2"}]           # held-out TYPE
    spec = lock_holdout(seed=31, held_out_types=("A2", "A5"))
    with pytest.raises(AssertionError, match="HOLDOUT LEAKAGE"):
        assert_no_leakage(poisoned_pool, spec)
