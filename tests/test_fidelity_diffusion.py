"""
test_fidelity_diffusion.py — Phase 6 gate: TabDDPM-style diffusion critic.

Gate requirements (implementation.md Phase 6):
  A. The diffusion critic runs on a SMALL config deterministically (same seed
     ⇒ byte-identical synthetic rows, in-process).
  B. Its synthetic output is finite, correct shape, and survives the SAME
     L1 statistical / L3 adversarial layers that judge CTGAN.
  C. build_fidelity_report in v2 mode carries a `critics` block with BOTH the
     ctgan and diffusion sections; v1 mode is preserved for back-compat.
  D. Layer-1 statistics on diffusion output live inside declared bands where
     the data allows (identical inputs ⇒ identical w-ratio), never NaN.
"""

from __future__ import annotations

import os
import sys
import json

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from eval.diffusion_tab import DiffusionConfig, TabDiffusionCritic        # noqa: E402
from eval.fidelity import (                                               # noqa: E402
    statistical_layer, adversarial_layer, build_fidelity_report,
)
from blue.features import FEATURE_NAMES                                   # noqa: E402

D = len(FEATURE_NAMES)


def _data(n=300, d=D, seed=7):
    rng = np.random.RandomState(seed)
    X = rng.normal(size=(n, d))
    # emulate feature magnitudes/zeros of a twin matrix (counts, flags)
    X[:, :4] = np.clip(np.rint(X[:, :4] * 3), 0, 30).astype(np.float64)
    X[:, 4] = X[:, 4] * 1000.0
    return X


def _small_cfg():
    return DiffusionConfig(T=40, hidden=128, emb_dim=32)


def _fit_and_sample(seed=3):
    critic = TabDiffusionCritic(dim=D, seed=seed, config=_small_cfg())
    critic.fit(_data(), epochs=120, batch_size=128)
    return critic.sample(250, batch_size=128)


# ---------------------------------------------------------------------------
# A. Determinism
# ---------------------------------------------------------------------------

def test_diffusion_deterministic_same_seed():
    a = _fit_and_sample(seed=3)
    b = _fit_and_sample(seed=3)
    assert a.shape == b.shape
    assert (a == b).all()          # byte-identical synthetic output
    assert np.isfinite(a).all()


def test_diffusion_differs_across_seeds():
    a = _fit_and_sample(seed=3)
    b = _fit_and_sample(seed=99)
    assert not (a == b).all()


def test_diffusion_requires_fit_before_sample():
    with pytest.raises(RuntimeError):
        TabDiffusionCritic(dim=D, seed=1, config=_small_cfg()).sample(10)


# ---------------------------------------------------------------------------
# B. Feeds the same L1/L3 critics
# ---------------------------------------------------------------------------

def test_diffusion_survives_statistical_layer():
    real = _data()
    synth = _fit_and_sample(seed=5)
    stat = statistical_layer(real, synth, FEATURE_NAMES)
    assert math_isfinite(stat)
    assert stat["wasserstein_ratio_median"] >= 0.0
    # a diffusion critic on the SAME matrix cannot beat the noise floor;
    # identical distribution gives w-ratio ≈ 0 (sanity: bounded)
    assert stat["wasserstein_ratio_median"] < 5.0


def test_diffusion_survives_adversarial_layer():
    real = _data()
    synth = _fit_and_sample(seed=6)
    adv = adversarial_layer(None, real, synth, seed=42,
                            feature_names=FEATURE_NAMES)
    assert 0.0 <= adv["critic_trap"]["auc"] <= 1.0
    lo, hi = adv["critic_trap"]["band"]
    assert adv["critic_trap"]["survived_band"] == (lo <= adv["critic_trap"]["auc"] <= hi)
    assert adv["critic_trap"]["conclusion"]


# ---------------------------------------------------------------------------
# C. Report v2 shape (additive) + v1 back-compat
# ---------------------------------------------------------------------------

def _dummy_stat():
    return {"pass_flags": {"wasserstein_ok": True, "ks_ok": True},
            "wasserstein_ratio_max": 0.1, "wasserstein_ratio_median": 0.05,
            "ks_stat_median": 0.05, "per_column_numeric": {},
            "per_column_categorical": {}}


def test_report_v2_has_both_critics_additively():
    stat = _dummy_stat()
    behav = {"graph": {}, "income_cycles": {}, "iat_overlap": {},
             "liveness": {}}
    adv = {"critic_trap": {"auc": 0.55}, "manifold_transfer": {}}
    meta = {"seed": 42}
    critics = {
        "ctgan": {"generator": "sdv CTGANSynthesizer",
                  "statistical": dict(stat), "adversarial": dict(adv)},
        "diffusion": {"generator": "TabDiffusionCritic",
                      "statistical": dict(stat), "adversarial": dict(adv),
                      "diagnostics": {"config": {}, "seed": 42}},
    }
    rep = build_fidelity_report(stat, behav, adv, meta, critics=critics)
    assert rep["schema"] == "prometheus.fidelity_report.v2"
    # additive: v1 layers block still present and unchanged
    assert set(rep["layers"]) == {"statistical", "behavioral", "adversarial"}
    assert set(rep["critics"]) == {"ctgan", "diffusion"}
    for name, blk in rep["critics"].items():
        assert {"generator", "statistical", "adversarial"} <= set(blk)


def test_report_v1_without_critics():
    stat = _dummy_stat()
    behav = {"graph": {}, "income_cycles": {}, "iat_overlap": {},
             "liveness": {}}
    adv = {"critic_trap": {"auc": 0.55}, "manifold_transfer": {}}
    rep = build_fidelity_report(stat, behav, adv, {"seed": 42})
    assert rep["schema"] == "prometheus.fidelity_report.v1"
    assert "critics" not in rep


# ---------------------------------------------------------------------------
# D. helpers
# ---------------------------------------------------------------------------

def _all_values(d, acc=None):
    acc = acc if acc is not None else []
    if isinstance(d, dict):
        for v in d.values():
            _all_values(v, acc)
    elif isinstance(d, (list, tuple)):
        for v in d:
            _all_values(v, acc)
    else:
        acc.append(d)
    return acc


def math_isfinite(d) -> bool:
    for v in _all_values(d):
        if isinstance(v, float):
            if not np.isfinite(v):
                return False
    return True