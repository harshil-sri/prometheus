"""
test_feasibility.py — Phase 9 gate: margins, latency, INR cost, drift PSI.

Gate requirements from PROMETHEUS_CONTEXT.md §5 (Phase 9):
  A. margins.py: hand-verified signed-margin math; sign split & boundary
     fractions exact; vocabulary law (no 'certified') enforced on output.
  B. drift.py: PSI identity ≈ 0 on identical samples; a strong shift crosses
     the 'significant' band; width mismatch raises; verdict counts complete.
  C. latency.py: measured structure (iterations honored, quantiles ordered,
     environment declared CPU-only).
  D. cost_model.py: economics respond MONOTONICALLY to knobs (recall up ⇒
     net up; analyst rate up ⇒ review cost up); zero-recall ⇒ no
     cost-per-prevented (None, never fabricated).
  E. All four artifacts (when present): schema valid, margin law enforced,
     latency quantiles ordered, cost grid carries declared variants, drift
     counts sum to feature count.
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

from eval.margins import margin_distribution, margins_from_verify_payload, BANNED_WORD
from eval.drift import psi, drift_report, DRIFT_BANDS
from eval.latency import measure_latency, environment_info
from eval.cost_model import inr_economics, sensitivity_grid, DEFAULT_ASSUMPTIONS
from blue.features import FEATURE_NAMES

ART = os.path.join(ROOT, "artifacts")


# ---------------------------------------------------------------------------
# A. Margins math
# ---------------------------------------------------------------------------

def test_margin_distribution_hand_checked():
    per = [
        {"victim_candidate_score": 0.40, "victim_base_score": 0.90},
        {"victim_candidate_score": 0.60, "victim_base_score": 0.90},
        {"victim_candidate_score": 0.45, "victim_base_score": 0.88},
    ]
    d = margin_distribution(per, threshold=0.5)
    margins = [0.1, -0.1, 0.05]
    assert d["sign_split"]["evasive"] == 2
    assert d["sign_split"]["caught"] == 1
    assert d["sign_split"]["frac_evasive"] == pytest.approx(2 / 3, abs=1e-3)
    assert d["stats"]["mean"] == pytest.approx(np.mean(margins), abs=1e-4)
    assert d["stats"]["min"] == pytest.approx(-0.1, abs=1e-4)
    assert d["score_drop_mean"] == pytest.approx(np.mean([0.5, 0.3, 0.43]),
                                                 abs=1e-3)
    assert d["vocabulary_law"]["label"] == "empirical estimate"


def test_margin_adapter_and_banned_word():
    payload = {"per_candidate": [
        {"victim_candidate_score": 0.2, "victim_base_score": 0.8}],
        "note": "x"}
    d = margins_from_verify_payload(payload, threshold=0.5)
    assert d["n_candidates"] == 1 and d["sign_split"]["frac_evasive"] == 1.0
    assert BANNED_WORD not in json.dumps(d).lower()


# ---------------------------------------------------------------------------
# B. PSI / drift
# ---------------------------------------------------------------------------

def test_psi_identity_zero_and_shift_detected():
    rng = np.random.RandomState(0)
    a = rng.normal(size=2000)
    assert psi(a, a) < 1e-6
    b = rng.normal(loc=2.5, size=2000)
    v = psi(a, b)
    assert v > DRIFT_BANDS["moderate"]          # ≥ 0.25
    d = drift_report(np.stack([a, rng.rand(2000)], axis=1),
                     np.stack([b, rng.rand(2000)], axis=1),
                     ["col_shift", "col_same"])
    assert d["per_feature"]["col_shift"]["verdict"] == "significant"
    assert d["per_feature"]["col_same"]["verdict"] == "stable"
    assert sum(d["verdict_counts"].values()) == 2


def test_drift_width_mismatch_raises():
    with pytest.raises(ValueError):
        drift_report(np.zeros((10, 3)), np.zeros((10, 4)),
                     ["a", "b"])


# ---------------------------------------------------------------------------
# C. Latency measurement
# ---------------------------------------------------------------------------

def test_latency_structure_and_ordering():
    out = measure_latency(lambda: sum(range(200)), warmup=2, iterations=12)
    assert out["iterations"] == 12
    assert out["p50"] <= out["p99"] + 1e-12
    assert out["min"] <= out["p50"] <= out["max"]
    assert len(out["samples_s"]) == 12
    env = environment_info()
    assert env["cpu_only"] is True and env["cpu_count"] >= 1


# ---------------------------------------------------------------------------
# D. Cost economics
# ---------------------------------------------------------------------------

def test_cost_zero_recall_has_no_cost_per_prevented():
    r = inr_economics(prevalence=0.01, budget_pct=2.0, recall_at_budget=0.0,
                      precision_at_budget=0.0)
    assert r["per_1000_transactions"]["est_prevented_frauds"] == 0.0
    assert r["inr_breakdown"]["cost_per_prevented_fraud"] is None
    assert r["inr_breakdown"]["net_benefit"] < 0          # pure cost


def test_cost_monotone_in_recall_and_rate():
    base = inr_economics(0.01, 5.0, recall_at_budget=0.6,
                         precision_at_budget=0.5)
    better = inr_economics(0.01, 5.0, recall_at_budget=0.9,
                           precision_at_budget=0.5)
    assert better["inr_breakdown"]["net_benefit"] > \
        base["inr_breakdown"]["net_benefit"]

    cheap = inr_economics(0.01, 5.0, 0.6, 0.5,
                          {**DEFAULT_ASSUMPTIONS,
                           "analyst_rate_inr_per_hour": 240.0})
    dear = inr_economics(0.01, 5.0, 0.6, 0.5,
                         {**DEFAULT_ASSUMPTIONS,
                          "analyst_rate_inr_per_hour": 960.0})
    assert dear["inr_breakdown"]["review_cost"] > \
        cheap["inr_breakdown"]["review_cost"]


def test_sensitivity_grid_carries_variants():
    pts = {"p05": {"prevalence": 0.005, "budget": 5.0, "recall": 0.7,
                   "precision": 0.4}}
    grid = sensitivity_grid(pts, {"base": {}, "hot": {
        "avg_fraud_loss_inr": 24000.0}})
    g = grid["grid"]["p05"]
    assert set(g["net_by_variant"]) == {"base", "hot"}
    assert g["net_by_variant"]["hot"] > g["net_by_variant"]["base"]


# ---------------------------------------------------------------------------
# E. Artifact contracts (skip when not yet generated)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(os.path.join(ART, "margins.json")),
                    reason="run scripts/feasibility_eval.py")
def test_artifact_margins_contract():
    art = json.load(open(os.path.join(ART, "margins.json")))
    assert art["schema"] == "prometheus.margins.v1"
    dist = art["distribution"]
    assert dist["n_candidates"] > 0
    s = dist["stats"]
    assert -0.5 - 1e-9 <= s["min"] and s["max"] <= 1.0 + 1e-9
    assert s["min"] <= s["median"] <= s["max"]
    assert "certified" not in json.dumps(art).lower()


@pytest.mark.skipif(not os.path.exists(os.path.join(ART, "latency.json")),
                    reason="run scripts/feasibility_eval.py")
def test_artifact_latency_contract():
    art = json.load(open(os.path.join(ART, "latency.json")))
    assert art["schema"] == "prometheus.latency.v1"
    assert art["environment"]["cpu_only"] is True
    for name, p in art["paths"].items():
        assert p["p50"] <= p["p99"] + 1e-9, name
        assert p["iterations"] >= 5, name


@pytest.mark.skipif(not os.path.exists(os.path.join(ART, "cost_model.json")),
                    reason="run scripts/feasibility_eval.py")
def test_artifact_cost_contract():
    art = json.load(open(os.path.join(ART, "cost_model.json")))
    assert art["schema"] == "prometheus.cost_model.v1"
    assert art["points"], "no measured eval points"
    for label, ex in art["worked_examples"].items():
        assert {"inr_breakdown"} <= set(ex), label
        nb = ex["inr_breakdown"]["net_benefit"]
        assert isinstance(nb, (int, float))


@pytest.mark.skipif(not os.path.exists(os.path.join(ART, "drift.json")),
                    reason="run scripts/feasibility_eval.py")
def test_artifact_drift_contract():
    art = json.load(open(os.path.join(ART, "drift.json")))
    assert art["schema"] == "prometheus.drift.v1"
    n_feat = len(FEATURE_NAMES)
    for block in ("normal_only", "full_late_window"):
        counts = art[block]["verdict_counts"]
        assert sum(counts.values()) == n_feat
    assert art["attack_induced_gap_top"]           # non-empty ranking
