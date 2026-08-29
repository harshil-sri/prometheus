"""
test_weights.py — Phase 4 gate (updates.md 2.2): fitted w_* weights.

Gate requirements from implementation.md Phase 4:
  1. Weights file written to ONE canonical path (reconciled
     DEFAULT_WEIGHTS_PATH == src/artifacts/structured_weights.json),
     schema-valid, six w_* keys, monotone (every w_* ≥ 0), and band
     reachability: max raw = 1000 so every decision band is reachable.
  2. fit_w_star (scoring.weight_fit, shared by scripts/fit_weights.py) is
     deterministic, monotone (nnls non-negativity) and reachability-rescaled
     — verified on a fixed matrix so no heavy training runs in the suite.
  3. v2 artifact round-trips through save()/load(); v1 artifacts (no
     w_formula) still load (back-compat); a negative fitted weight FAILS
     LOUDLY (ValueError) instead of silently making the score non-monotone.
  4. predict_row uses the FITTED w_e/w_c additively (seam equality), so both
     scorers stay interpolatable with fitted weights.
  5. /api/structured-weights exposes the fitted-vs-baseline report; it is
     honest about a missing artifact.
"""

from __future__ import annotations

import json
import math
import os

import numpy as np
import pytest

from scoring.structured_score import (
    DEFAULT_WEIGHTS_PATH, FORMULA_TERMS, WEIGHTS_SCHEMA,
    FittedStructuredScore,
)
from scoring.weight_fit import BAND_MAX, GRID, fit_w_star

np.random.seed(7)


def _fixed_matrix(n_real: int = 60) -> tuple:
    """Deterministic real rows + the documented calibration grid."""
    rng = np.random.RandomState(42)
    real = rng.rand(n_real, 6)
    X = np.vstack([real, np.asarray([r[:6] for r in GRID])])
    y = np.concatenate([real @ np.array([0.4, 0.3, 0.2, 0.05, 0.05, -0.1]),
                        np.asarray([r[6] for r in GRID])])
    return X, y, n_real


# --------------------------------------------------------------------------- #
# 1. Canonical artifact
# --------------------------------------------------------------------------- #

def test_canonical_artifact_schema_monotone_reachable():
    assert os.path.isfile(DEFAULT_WEIGHTS_PATH), \
        "Phase 4 gate: committed canonical weights artifact must exist"
    scored = FittedStructuredScore.load()
    assert scored.w_formula is not None, "artifact must carry fitted w_*"
    assert set(scored.w_formula) == set(FORMULA_TERMS)
    assert all(v >= 0.0 for v in scored.w_formula.values()), \
        "weighted formula must be monotone (nnls ⇒ w_* ≥ 0)"
    report = scored.weights_report()
    assert report["schema"] == WEIGHTS_SCHEMA
    assert report["monotone"] is True
    assert report["band_reachability"]["decline_reachable"] is True
    assert abs(report["band_reachability"]["max_raw"] - BAND_MAX) < 0.01
    prov = report["provenance"]
    assert prov["n"] > 0 and prov["pos"] >= 0 and prov["fit_auc"] > 0.0
    assert scored.w_fit is not None, "artifact must explain the fit"
    assert "degenerate_on_real_rows" in scored.w_fit
    assert scored.baseline_weights


# --------------------------------------------------------------------------- #
# 2. Determinism + monotone + reachability of the pure fit
# --------------------------------------------------------------------------- #

def test_fit_w_star_deterministic_monotone_reachable():
    X, y, n_real = _fixed_matrix()
    w1, d1 = fit_w_star(X, y, n_real)
    w2, d2 = fit_w_star(X, y, n_real)
    assert w1 == w2 and d1 == d2, "fit must be byte-deterministic"
    assert set(w1) == set(FORMULA_TERMS)
    assert all(v >= 0.0 for v in w1.values()), "nnls ⇒ monotone (≥ 0)"
    max_raw = sum(v for k, v in w1.items() if k != "w_u") - w1["w_u"]
    assert abs(max_raw - BAND_MAX) < 0.01, "reachability rescale to 1000"
    assert 0.0 < d1["residual"], "diagnostics carry the residual"
    assert len(d1["grid"]) == len(GRID)


# --------------------------------------------------------------------------- #
# 3. Save/load round-trip, v1 back-compat, fail-loud monotone
# --------------------------------------------------------------------------- #

def test_v2_roundtrip_and_v1_backcompat(tmp_path):
    wf = {"w_t": 300, "w_g": 250, "w_b": 200, "w_e": 120, "w_c": 80, "w_u": 50}
    scorer = FittedStructuredScore()
    scorer.coef_ = [0.1 * (i + 1) for i in range(6)]
    scorer.intercept_ = -1.0
    scorer.w_formula = wf
    p2 = str(tmp_path / "weights_v2.json")
    scorer.save(p2)
    loaded = FittedStructuredScore.load(p2)
    assert loaded.w_formula == wf
    assert loaded.coef_ == scorer.coef_ and loaded.intercept_ == scorer.intercept_
    assert loaded.schema if hasattr(loaded, "schema") else True

    # merge-preserve: a fresh session fit (no w_formula) must keep the fitted
    # w_* already on disk when it saves back over the same file.
    fresh = FittedStructuredScore()
    fresh.coef_ = [0.5] * 6
    fresh.intercept_ = 0.0
    fresh.save(p2)
    merged = FittedStructuredScore.load(p2)
    assert merged.w_formula == wf, "save() must merge-preserve fitted w_*"
    assert merged.coef_ == fresh.coef_, "fresh session logistic stays"

    # v1 blob (no w_formula / w_fit keys) must still load.
    v1 = {"coef": [1.0] * 6, "intercept": 0.0, "columns": list(scorer.columns),
          "fit_meta": {"n": 786, "pos": 16, "fit_auc": 1.0,
                       "source": "fitted_in_sample"}}
    p1 = str(tmp_path / "weights_v1.json")
    json.dump(v1, open(p1, "w"))
    back = FittedStructuredScore.load(p1)
    assert back.w_formula is None, "v1 artifacts carry no fitted w_*"
    assert back.coef_ == [1.0] * 6


def test_negative_weight_fails_loud(tmp_path):
    blob = {"schema": WEIGHTS_SCHEMA, "coef": [1.0] * 6, "intercept": 0.0,
            "columns": list(FittedStructuredScore().columns),
            "fit_meta": {"n": 10, "pos": 2, "fit_auc": 1.0},
            "w_formula": {"w_t": 300, "w_g": 250, "w_b": 200, "w_e": 120,
                          "w_c": 80, "w_u": -50}}   # -w_u with w_u<0 ⇒ +50
    p = str(tmp_path / "bad_weights.json")
    json.dump(blob, open(p, "w"))
    with pytest.raises(ValueError, match="monotone"):
        FittedStructuredScore.load(p)


def test_uncertainty_penalty_sign_and_magnitude():
    """The formula is R = … − w_u·U, so w_u must behave as a penalty:
    raising uncertainty must LOWER the raw score by exactly w_u per unit —
    in BOTH the pure fit and the committed artifact. Guards the design sign
    convention (D = [T,G,B,E,C,−U]): a +U fit would invert this (bug caught
    in Phase 4 review)."""
    from scoring.structured_score import compute_structured_score
    w = FittedStructuredScore.load().w_formula
    assert w is not None and w["w_u"] >= 0.0
    s0 = compute_structured_score(0.4, 0.4, 0.4, uncertainty=0.0, weights=w)
    s1 = compute_structured_score(0.4, 0.4, 0.4, uncertainty=1.0, weights=w)
    assert s1["raw"] < s0["raw"], "uncertainty must penalize, not reward"
    assert math.isclose(s0["raw"] - s1["raw"], w["w_u"], abs_tol=0.05)
    # pure fit on the fixed matrix obeys the same sign convention
    X, y, n_real = _fixed_matrix()
    wf, _diag = fit_w_star(X, y, n_real)
    assert wf["w_u"] >= 0.0


# --------------------------------------------------------------------------- #
# 4. predict_row seam: fitted w_e/w_c used additively
# --------------------------------------------------------------------------- #

def test_predict_row_uses_fitted_w_ec():
    wf = {"w_t": 300, "w_g": 250, "w_b": 200, "w_e": 90.0, "w_c": 90.0,
          "w_u": 50}
    scorer = FittedStructuredScore()
    scorer.coef_ = [0.0] * 6
    scorer.intercept_ = 0.0
    scorer.w_formula = wf
    row = {c: 0.002 for c in scorer.columns}
    r0 = scorer.predict_row(row, external_evidence=0.0, campaign_evidence=0.0)
    r1 = scorer.predict_row(row, external_evidence=1.0, campaign_evidence=0.0)
    r2 = scorer.predict_row(row, external_evidence=1.0, campaign_evidence=1.0)
    assert math.isclose(r1["score"] - r0["score"], wf["w_e"], abs_tol=1e-3)
    assert math.isclose(r2["score"] - r0["score"],
                        wf["w_e"] + wf["w_c"], abs_tol=1e-3)
    assert r0["formula_weights"] == wf


# --------------------------------------------------------------------------- #
# 5. /api/structured-weights endpoint honesty
# --------------------------------------------------------------------------- #

def test_api_structured_weights_endpoint():
    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)
    resp = client.get("/api/structured-weights")
    assert resp.status_code == 200
    body = resp.json()
    if not os.path.isfile(DEFAULT_WEIGHTS_PATH):
        assert body == {"present": False}
        return
    assert body["present"] is True
    assert body["schema"] == WEIGHTS_SCHEMA
    assert body["monotone"] is True
    assert set(body["fitted"]) == set(FORMULA_TERMS)
    assert set(body["baseline"]) == set(FORMULA_TERMS)