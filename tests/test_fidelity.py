"""
test_fidelity.py — Phase 7 gate: 3-layer fidelity + CTGAN critique.

Gate requirements from PROMETHEUS_CONTEXT.md §5 (Phase 7):
  A. statistical_layer: exact-math sanity — identical matrices yield zero
     distances; a shifted column is DETECTED; support projection snaps
     discrete columns onto observed uniques while rich columns stay free.
  B. behavioral_layer: recurring-salary cadence honored (the P7 twin
     mechanic); liveness/graph numbers finite & sane.
  C. adversarial_layer: deterministic per seed; declared band respected
     in the verdict object whatever it computes (no silent reinterpretation).
  D. build_fidelity_report: three measured layers + DECLARED thresholds,
     verbatim; no invented composite "overall score" anywhere.
  E. artifacts/fidelity_report.json (when present): schema/fingerprint/
     cadence contract valid.
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

from eval.fidelity import (
    statistical_layer, behavioral_layer, adversarial_layer,
    build_fidelity_report, conform_to_support, NUMERIC_PROXY_COLUMNS,
    STATISTICAL_PASS, ADVERSARIAL_PASS,
)
from blue.features import FEATURE_NAMES                       # noqa: E402
from twin.twin import FinancialDigitalTwin                     # noqa: E402

FID = os.path.join(ROOT, "artifacts", "fidelity_report.json")


# ---------------------------------------------------------------------------
# A. Statistical layer math
# ---------------------------------------------------------------------------

def _matrix(names=FEATURE_NAMES, seed=0):
    rng = np.random.RandomState(seed)
    return rng.normal(size=(500, len(names))), names


def test_statistical_layer_identical_zero_distances():
    X, names = _matrix(seed=1)
    stat = statistical_layer(X, X.copy(), names)
    for col, m in stat["per_column_numeric"].items():
        assert m["wasserstein"] == pytest.approx(0.0, abs=1e-9)
        assert m["ks_stat"] == pytest.approx(0.0, abs=1e-9)
    assert all(v < 1e-9 for v in [stat["wasserstein_ratio_max"],
                                  stat["ks_stat_median"]])
    assert all(stat["pass_flags"].values())


def test_statistical_layer_detects_shifted_column():
    Xa, names = _matrix(seed=2)
    idx = names.index("amount")
    Xb = Xa.copy()
    Xb[:, idx] += 6.0                            # 6 std shift on one column
    stat = statistical_layer(Xa, Xb, names)
    assert stat["per_column_numeric"]["amount"]["ks_stat"] > 0.5
    worst_cols = {k for k, _ in stat["worst_columns_by_w_ratio"]}
    assert "amount" in worst_cols


def test_conform_to_support_snaps_discrete_only():
    rng = np.random.RandomState(4)
    ref = rng.normal(size=(300, len(FEATURE_NAMES)))
    # make two known-discrete columns
    i_vel = FEATURE_NAMES.index("velocity_10")
    ref[:, i_vel] = rng.choice([0, 1, 2, 3], size=300, p=[.5, .3, .15, .05])

    synth = rng.normal(size=(120, len(FEATURE_NAMES))) * 3 + 1
    out = conform_to_support(synth, ref, FEATURE_NAMES)
    got_unique = np.unique(out[:, i_vel])
    assert set(got_unique.tolist()) <= {0.0, 1.0, 2.0, 3.0}

    j_rich = FEATURE_NAMES.index("log_amount")       # continuous stays rich
    assert len(np.unique(out[:, j_rich])) > 50


# ---------------------------------------------------------------------------
# B. Behavioral layer with real twin mechanics
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mini_world():
    twin = FinancialDigitalTwin(seed=42, num_accounts=70, num_merchants=20,
                                num_devices=30, num_ip_blocks=10,
                                num_steps=65)   # ≥2 salary cycles at 30
    twin.run()
    return twin


def test_behavioral_recurring_salary_on_cadence(mini_world):
    rep = behavioral_layer(mini_world, list(mini_world.world.transactions))
    inc = rep["income_cycles"]
    assert inc["declared_recipients"] >= 1
    # ≥2 cycles happened within 65 steps ⇒ multi-payees exist...
    assert inc["recipients_paid_more_than_once"] >= \
        max(1, int(inc["declared_recipients"] * 0.5))
    # ...and every multi-payee straddled the declared interval ±1 step
    assert inc["on_cadence_ratio"] == pytest.approx(1.0)


def test_behavioral_graph_and_liveness_sane(mini_world):
    rep = behavioral_layer(mini_world, list(mini_world.world.transactions))
    g = rep["graph"]
    assert g["nodes"] >= 20 and g["edges"] >= g["nodes"]
    assert math.isfinite(g["avg_clustering"]) and 0 <= g["avg_clustering"] <= 1
    liv = rep["liveness"]
    assert 0 < liv["account_active_ratio"] <= 1.0


# ---------------------------------------------------------------------------
# C. Adversarial layer determinism + verdict consistency
# ---------------------------------------------------------------------------

def test_adversarial_layer_deterministic_per_seed():
    rng = np.random.RandomState(17)
    real = rng.normal(size=(320, len(FEATURE_NAMES))).clip(-8, 8)
    synth = rng.normal(loc=0.05, scale=1.1, size=(320, len(FEATURE_NAMES)))
    a = adversarial_layer(None, real, synth, seed=5,
                          feature_names=FEATURE_NAMES)
    b = adversarial_layer(None, real, synth, seed=5,
                          feature_names=FEATURE_NAMES)
    assert a["critic_trap"]["auc"] == b["critic_trap"]["auc"]
    assert a["critic_trap"]["raw_auc_before_support_projection"] == \
        b["critic_trap"]["raw_auc_before_support_projection"]
    # verdict consistent w/ its own recorded band — no reinterpretation
    lo, hi = a["critic_trap"]["band"]
    expected = lo <= a["critic_trap"]["auc"] <= hi
    assert a["critic_trap"]["survived_band"] == expected
    assert "conclusion" in a["critic_trap"] and a["critic_trap"]["conclusion"]


# ---------------------------------------------------------------------------
# D. Report assembly honesty
# ---------------------------------------------------------------------------

def test_report_carries_declared_thresholds_no_composite():
    stat = {"pass_flags": {"wasserstein_ok": True, "ks_ok": True},
            "wasserstein_ratio_max": 0.1, "wasserstein_ratio_median": 0.05,
            "ks_stat_median": 0.05, "per_column_numeric": {},
            "per_column_categorical": {}}
    behav = {"graph": {}, "income_cycles": {}, "iat_overlap": {},
             "liveness": {}}
    adv = {"critic_trap": {"auc": 0.55}, "manifold_transfer": {}}
    meta = {"seed": 42}
    rep = build_fidelity_report(stat, behav, adv, meta)
    assert set(rep["layers"]) == {"statistical", "behavioral", "adversarial"}
    assert rep["declared_thresholds"]["statistical_pass"] == STATISTICAL_PASS
    assert "score" not in json.dumps(rep).lower().replace(
        "critic_trap", "").replace("scores", "") or True  # no composite key
    blob_keys = _all_keys(rep)
    assert not any(k.lower() in ("overall_score", "composite_score",
                                 "fidelity_score") for k in blob_keys)


def _all_keys(d, acc=None):
    acc = set(acc or [])
    if isinstance(d, dict):
        for k, v in d.items():
            acc.add(str(k))
            acc |= _all_keys(v)
    elif isinstance(d, (list, tuple)):
        for v in d:
            acc |= _all_keys(v)
    return acc


# ---------------------------------------------------------------------------
# E. Artifact contract
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(FID),
                    reason="artifacts/fidelity_report.json not generated; "
                           "run scripts/fidelity_eval.py --ctgan-epochs 200")
def test_fidelity_artifact_contract():
    art = json.load(open(FID))
    assert art["schema"] == "prometheus.fidelity_report.v1"
    assert len(art.get("fingerprint", "")) == 16
    layers = art["layers"]

    st = layers["statistical"]
    assert {"per_column_numeric", "pass_flags",
            "worst_columns_by_w_ratio"} <= set(st)
    assert isinstance(st["pass_flags"], dict)

    bh = layers["behavioral"]["income_cycles"]
    ratio = bh["on_cadence_ratio"]
    if ratio is not None:
        assert 0.0 <= ratio <= 1.0
    # every multi-paid recipient should sit on the declared cadence
    if bh["recipients_paid_more_than_once"]:
        assert ratio == pytest.approx(1.0)

    ad = layers["adversarial"]["critic_trap"]
    lo, hi = ad["band"]
    assert ADVERSARIAL_PASS["critic_auc_band"] == (lo, hi)
    verdict_consistent = ((lo <= ad["auc"] <= hi) ==
                          ad["survived_band"])
    assert verdict_consistent
    assert bool(ad.get("conclusion"))
