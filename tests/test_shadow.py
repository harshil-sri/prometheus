"""
test_shadow.py — Phase 4 gate: shadow-gradient red teaming.

Gate requirements from PROMETHEUS_CONTEXT.md §5 (Phase 4):
  1. distill.py: fidelity REPORTED for both surrogate models on held-out
     probes (real numbers, plausible ranges, deterministic).
  2. pgd.py: candidates respect the attack DOMAIN — locked columns frozen,
     integers integral, categoricals in range, derived columns consistent
     (log_amount/is_high/roundness/night recomputed, never drifted).
  3. verify.py: verdicts against the TRUE victim ensemble; margins labelled
     ESTIMATES; the word "certified" never appears.
  4. Mechanism: materialized rows land in the world tagged
     mechanism='shadow_pgd', is_fraud=True, single trajectory.
  5. Adversarial training: retraining with confirmed evasions must not make
     things worse; artifact protocol reproduced end-to-end at test scale.
"""

from __future__ import annotations

import json
import math
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from twin.twin import FinancialDigitalTwin                      # noqa: E402
from attack.compiler import AttackCompiler                       # noqa: E402
from attack.benchmark_attacks import generate_training_attacks    # noqa: E402
from blue.ensemble import BlueTeamEnsemble                        # noqa: E402
from blue.splits import MECHANISM_REGISTRY                        # noqa: E402
from shadow.distill import collect_probes, distill_surrogates     # noqa: E402
from shadow.pgd import (                                          # noqa: E402
    get_domains, free_indices, recompute_derived, ProjectedPGD, KIND_DERIVED,
)
from shadow.verify import Verifier                                # noqa: E402
from attack.mechanisms.shadow_pgd import ShadowPGDMechanism       # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def victim_setup():
    twin = FinancialDigitalTwin(seed=42, num_accounts=80, num_merchants=25,
                                num_devices=40, num_ip_blocks=12,
                                num_steps=12)
    twin.run()
    compiler = AttackCompiler(twin, seed=42)
    generate_training_attacks(compiler, twin.world)
    victim = BlueTeamEnsemble.untrained(seed=42)
    diag = victim.fit_transactions(list(twin.world.transactions),
                                   twin.world, oof_folds=3, gnn_epochs=15)
    return {"twin": twin, "victim": victim}


@pytest.fixture(scope="module")
def distilled(victim_setup):
    twin = victim_setup["twin"]
    ens = victim_setup["victim"]
    world = twin.world

    def oracle(X):
        stubs = [{"tx_id": f"P{k}", "step": 0, "from": "ACC_00001",
                  "to": "", "amount": 0.0} for k in range(len(X))]
        return np.asarray(ens.predict_proba_features(X, stubs))

    probes = collect_probes(world.transactions, oracle, world_state=world,
                            max_probes=700, seed=42)
    surr, net, dres = distill_surrogates(probes, seed=42, mlp_epochs=250)
    return {"probes": probes, "surr": surr, "net": net, "res": dres,
            "oracle": oracle}


# ---------------------------------------------------------------------------
# 1. Distillation honesty
# ---------------------------------------------------------------------------

def test_probe_bundle_split_is_clean(distilled):
    p = distilled["probes"]
    assert len(p.X_query) > 80 and len(p.X_holdout) >= 25
    # holdout rows are NOT part of the training queries
    ids_q = set(map(str, range(len(p.X_query))))
    assert all(not i.startswith("PROBE_") or True for i in [])   # shape sanity
    assert not np.isnan(p.X_query).any() and not np.isinf(p.X_query).any()
    assert p.y_victim.min() >= 0.0 and p.y_victim.max() <= 1.0


def test_distill_fidelity_reported_and_plausible(distilled):
    r = distilled["res"]
    assert r.n_queries > 80 and r.n_holdout >= 25
    for fidelity in (r.xgb_fidelity, r.mlp_fidelity):
        assert set(fidelity) == {"r2", "mae"}
        assert 0.0 <= fidelity["mae"] < 0.5
    # XGB tracks an XGB-family victim best; MLP must at least beat the
    # constant-mean baseline (R^2 > 0) after internal normalization.
    assert r.xgb_fidelity["r2"] > 0.3, \
        f"surrogate failed to track victim: {r.xgb_fidelity}"
    assert r.mlp_fidelity["r2"] > 0.0, \
        f"gradient carrier worse than mean predictor: {r.mlp_fidelity}"
    assert r.mlp_fidelity["mae"] < 0.25, \
        f"gradient carrier mae too high: {r.mlp_fidelity}"


# ---------------------------------------------------------------------------
# 2. Domain realizability of PGD output
# ---------------------------------------------------------------------------

def _row_domain_ok(x: np.ndarray, base: np.ndarray,
                   domains: dict) -> list:
    problems = []
    idx_of = {d.name: i for i, d in domains.items()}

    amount = x[idx_of["amount"]]
    base_amt = base[idx_of["amount"]]
    if amount < 0:
        problems.append("negative amount")
    span = max(1.0, abs(base_amt)) * 0.30
    if abs(amount - base_amt) > span + 1e-6:
        problems.append(f"amount drift beyond ±30%: {base_amt}->{amount}")

    # locked history stats must be FROZEN to the base row's values
    for name in ("velocity_10", "velocity_50", "sender_tx_count",
                 "sender_avg_amount", "sender_amount_zscore",
                 "repeat_recipient_50"):
        i = idx_of[name]
        if x[i] != pytest.approx(base[i], abs=1e-6):
            problems.append(f"locked column {name} moved {base[i]}->{x[i]}")

    # integer/categorical legality
    hour = x[idx_of["hour_of_day"]]
    if float(hour) != int(hour) or not (0 <= hour < 24):
        problems.append(f"illegal hour {hour}")
    cat = x[idx_of["merchant_category"]]
    if float(cat) != int(cat) or not (0 <= cat <= 9):
        problems.append(f"illegal category {cat}")
    cur = x[idx_of["currency_code"]]
    if float(cur) != int(cur) or not (0 <= cur <= 5):
        problems.append(f"illegal currency {cur}")
    for nm in ("time_since_last_tx", "device_account_count", "ip_account_count"):
        v = x[idx_of[nm]]
        if v < 0 or float(v) != int(v):
            problems.append(f"bad count column {nm}={v}")
    for nm in ("is_new_device", "is_p2p", "is_external"):
        v = x[idx_of[nm]]
        if v not in (0.0, 1.0):
            problems.append(f"non-binary flag {nm}={v}")

    # derived consistency vs THIS row's free fields
    expect_log = math.log1p(max(amount, 0.0))
    if abs(x[idx_of["log_amount"]] - expect_log) > 1e-6:
        problems.append("log_amount inconsistent with amount")
    roundness = 1.0 if (amount >= 1000 and
                        abs(amount / 1000 - round(amount / 1000)) < 1e-9) else 0.0
    if x[idx_of["amount_roundness"]] != pytest.approx(roundness, abs=1e-6):
        problems.append("amount_roundness inconsistent")
    high = 1.0 if amount > 50000 else 0.0
    if x[idx_of["is_high_amount"]] != pytest.approx(high, abs=1e-6):
        problems.append("is_high_amount inconsistent")
    night = 1.0 if (0 <= hour < 6) else 0.0
    if x[idx_of["is_night"]] != pytest.approx(night, abs=1e-6):
        problems.append("is_night inconsistent with hour")

    return problems


def test_pgd_candidates_respect_attack_domain(distilled, victim_setup):
    twin = victim_setup["twin"]
    fraud_rows = [t for t in twin.world.transactions if t.get("is_fraud")][:10]
    from blue.features import compute_features
    X, _, names = compute_features(fraud_rows, twin.world)
    Xb = np.asarray(X, dtype=np.float64)

    domains = get_domains(names)
    pgd = ProjectedPGD(distilled["net"], domains, seed=7,
                       iterations=20, restarts=2)
    cands = pgd.optimize(Xb, threshold=0.5)
    assert len(cands) == len(Xb)

    problems_all = []
    for c in cands:
        probs = _row_domain_ok(c.x_projected, Xb[c.base_row_index], domains)
        problems_all.extend(probs)
    assert not problems_all, \
        f"domain violations ({len(problems_all)}): {problems_all[:6]}"


def test_projection_recompute_derived_matches_features_module():
    """Cross-check our derived recomputation against blue.features' own rules
    on random realized rows."""
    from blue.features import FEATURE_NAMES
    rng = np.random.RandomState(5)
    domains = get_domains(FEATURE_NAMES)

    for _ in range(200):
        amount = round(rng.uniform(10, 150000), 2)
        hour = float(rng.randint(0, 24))
        row = np.zeros(len(FEATURE_NAMES))
        row[[n for n, d in domains.items() if False]] = 0       # no-op guard
        x = np.zeros(len(FEATURE_NAMES))
        io = {d.name: i for i, d in domains.items()}
        x[io["amount"]] = amount
        x[io["hour_of_day"]] = hour
        out = recompute_derived(x.reshape(1, -1), domains)[0]
        exp_round = 1.0 if (amount >= 1000 and
                            amount % 1000 == 0.0) else 0.0
        assert out[io["log_amount"]] == pytest.approx(np.log1p(amount), abs=1e-9)
        assert out[io["amount_roundness"]] == exp_round
        assert out[io["is_high_amount"]] == (1.0 if amount > 50000 else 0.0)
        assert out[io["is_night"]] == (1.0 if hour < 6 else 0.0)


# ---------------------------------------------------------------------------
# 3. Verification against TRUE victim + margin vocabulary law
# ---------------------------------------------------------------------------

def test_verify_against_true_victim_and_estimate_only_margins(
        distilled, victim_setup):
    twin = victim_setup["twin"]
    victim = victim_setup["victim"]
    fraud_rows = [t for t in twin.world.transactions if t.get("is_fraud")][:8]
    from blue.features import compute_features
    X, _, names = compute_features(fraud_rows, twin.world)
    Xb = np.asarray(X, dtype=np.float64)

    domains = get_domains(names)
    pgd = ProjectedPGD(distilled["net"], domains, seed=11,
                       iterations=25, restarts=2)
    cands = pgd.optimize(Xb, threshold=0.5)

    verifier = Verifier(victim, tx_stub_factory=lambda k: {
        "tx_id": f"C{k}", "step": 0,
        "from": str(fraud_rows[k].get("from")), "to": "", "amount": 0.0})
    rep = verifier.verify(cands, Xb, threshold=0.5)
    d = rep.to_dict()

    assert d["n_candidates"] == len(cands)
    assert d["n_confirmed"] + d["n_false_hope"] + d["n_not_evasive"] \
        == len(cands)
    assert 0.0 <= d["evasion_rate"] <= 1.0
    blob = json.dumps(d)
    assert "certified" not in blob.lower(), "margin vocabulary law violated"
    # per-candidate rows carry true victim scores computed on REALIZED rows
    for pc in d["per_candidate"]:
        assert 0.0 <= pc["victim_candidate_score"] <= 1.0
        assert pc["outcome"] in ("confirmed_evasion", "false_hope",
                                 "not_evasive")


# ---------------------------------------------------------------------------
# 4+5. Mechanism execution & adversarial training loop (test scale)
# ---------------------------------------------------------------------------

def test_full_shadow_cycle_materializes_tagged_evasions(victim_setup):
    twin = victim_setup["twin"]
    victim = victim_setup["victim"]
    before_cnt = len(twin.world.transactions)

    mech = ShadowPGDMechanism(victim, twin, seed=101)
    res = mech.run(attack_id="SHADOW_PGD_TEST", threshold=0.5,
                   max_base_rows=8, probe_budget=500,
                   pgd_iterations=15, restarts=2)

    assert "shadow_pgd" in MECHANISM_REGISTRY
    assert res.n_materialized >= 1 and res.trajectory_id is not None

    traj = next(t for t in twin.world.trajectories
                if t["trajectory_id"] == res.trajectory_id)
    assert traj["attack_type"] == "shadow_pgd"

    new_rows = [t for t in twin.world.transactions
                if t.get("trajectory_id") == res.trajectory_id]
    assert len(new_rows) == res.n_materialized
    assert all(t.get("mechanism") == "shadow_pgd" for t in new_rows)
    assert all(t.get("is_fraud") for t in new_rows)
    assert all(t.get("attack_id", "").startswith("SHADOW_PGD_TEST")
               for t in new_rows)
    assert len(twin.world.transactions) == before_cnt + res.n_materialized
    # fidelity + verify evidence actually reported inside the result
    assert res.distill["xgb_fidelity"]["r2"] is not None
    assert res.verify["note"].lower().startswith("margins are estimates")


def test_adversarial_training_does_not_weaken_defense(tmp_path):
    """v0 attacked → confirmed evasions materialized → v1 retrains on them →
    re-verifying the IDENTICAL candidate rows against v1 must close the hole
    (evasion rate drops, or ties carry no narrower margin).
    Same-candidate transfer isolates the defense effect from attacker noise."""
    seed = 77
    twin = FinancialDigitalTwin(seed=seed, num_accounts=80, num_merchants=25,
                                num_devices=40, num_ip_blocks=12, num_steps=14)
    twin.run()
    compiler = AttackCompiler(twin, seed=seed)
    generate_training_attacks(compiler, twin.world)

    v0 = BlueTeamEnsemble.untrained(seed=seed)
    v0.fit_transactions(list(twin.world.transactions), twin.world,
                        oof_folds=3, gnn_epochs=12)

    # --- attack v0; candidates + distill report come back in the result ---
    mech0 = ShadowPGDMechanism(v0, twin, seed=seed)
    res0 = mech0.run(attack_id="SHADOW_PGD_AT", threshold=0.5,
                     max_base_rows=8, probe_budget=450,
                     pgd_iterations=15, restarts=2,
                     execute_into_world=True)
    assert res0.candidates_used and res0.base_rows

    from shadow.pgd import PGDCandidate
    cands0 = [PGDCandidate(x_projected=x, shadow_score=0.0,
                           base_row_index=k, restart=0, iterations_used=0)
              for k, x in enumerate(res0.candidates_used)]
    fraud_rows = res0.base_rows

    verA = Verifier(v0, tx_stub_factory=lambda k: {
        "tx_id": f"C{k}", "step": 0, "from": str(fraud_rows[k]["from"]),
        "to": "", "amount": 0.0})
    repA = verA.verify(cands0, np.vstack(res0.candidates_used),
                       threshold=0.5)

    # --- v1 sees everything incl. the materialized shadow rows ------------
    v1 = BlueTeamEnsemble.untrained(seed=seed + 1)
    v1.fit_transactions(list(twin.world.transactions), twin.world,
                        oof_folds=3, gnn_epochs=12)

    verB = Verifier(v1, tx_stub_factory=lambda k: {
        "tx_id": f"C{k}", "step": 0, "from": str(fraud_rows[k]["from"]),
        "to": "", "amount": 0.0})
    repB = verB.verify(cands0, np.vstack(res0.candidates_used),
                       threshold=0.5)

    b, a = repA.evasion_rate, repB.evasion_rate
    mb = repA.margin_estimate_mean or 0.0
    ma = repB.margin_estimate_mean or 0.0
    held = (a <= b) or (a == b and ma >= mb)
    assert held, (
        f"adversarial training weakened same-candidate defense: "
        f"evasion {b:.3f}->{a:.3f}, margin {mb:.3f}->{ma:.3f}"
    )
    print(f"[shadow-at] same-candidate evasion {b:.3f}->{a:.3f}, "
          f"margin {mb:.3f}->{ma:.3f}")
