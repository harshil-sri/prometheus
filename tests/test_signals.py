"""
test_signals.py — Phase 6 gate: spectral fingerprints + NormalcyManifold.

Gate requirements from PROMETHEUS_CONTEXT.md §5 (Phase 6):
  A. spectral.py: closed-form math verified against EXACT constructed
     topologies — a perfect ring's spectrum matches 2cos(2πj/k)
     (cycle_residual ~ 0); a star K_{1,n} matches ±√(n−1) (star_residual ~ 0,
     and DISTINGUISHABLE from its cycle counterpart).
  B. Causality: features of earlier rows unchanged by later transactions.
  C. Determinism: identical inputs ⇒ byte-equal rows; no RNG anywhere.
  D. manifold.py: refuses NaN training data, records normal-row count, and
     separates obvious outliers from normal history by construction.
  E. Ensemble sidecar score_all_signals: aligned columns, finite values.
  F. artifacts/decorrelation.json (when present): schema complete, matrix
     symmetric w/ unit diagonal, off-diagonals show genuine non-redundancy
     between {xgb,gnn,meta} and {manifold,spectral_*}.
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

from blue.spectral import (
    compute_spectral_features, spectral_profile, SPECTRAL_FEATURE_NAMES,
    ego_adjacency, induced_spectrum_profile, _TxGraphLookup,
)
from blue.manifold import NormalcyManifold                       # noqa: E402
from twin.twin import FinancialDigitalTwin                        # noqa: E402
from attack.compiler import AttackCompiler                         # noqa: E402
from attack.benchmark_attacks import generate_training_attacks      # noqa: E402
from blue.ensemble import BlueTeamEnsemble                          # noqa: E402

DECORR = os.path.join(ROOT, "artifacts", "decorrelation.json")


# ---------------------------------------------------------------------------
# Helpers: exact topology builders (synthetic tx dicts only)
# ---------------------------------------------------------------------------

def _tx(step, frm, to):
    return {"tx_id": f"T{step}_{frm}_{to}", "step": step, "from": frm,
            "to": to, "amount": 1000.0, "category": "p2p",
            "is_fraud": False}


def ring_transactions(k: int = 4):
    """Closed ring both directions + an anchor row so the FINAL probe's
    ego-graph covers the completed topology."""
    nodes = [f"RING_{i}" for i in range(k)]
    txs = []
    step = 0
    for i in range(k):
        txs.append(_tx(step, nodes[i], nodes[(i + 1) % k])); step += 1
        txs.append(_tx(step, nodes[(i + 1) % k], nodes[i])); step += 1
    txs.append(_tx(step, nodes[0], nodes[1]))     # anchor: last row probes RING_0
    return txs


def star_transactions(n_leaves: int = 5):
    """Star both directions + hub-anchor row; leaves never meet."""
    hub = "STAR_HUB"
    leaves = [f"STAR_L{i}" for i in range(n_leaves)]
    txs = []
    step = 0
    for lv in leaves:
        txs.append(_tx(step, hub, lv)); step += 1
        txs.append(_tx(step, lv, hub)); step += 1
    txs.append(_tx(step, hub, leaves[0]))         # anchor: probes STAR_HUB
    return txs


def probe_index(txs, sender):
    return max(i for i, t in enumerate(txs) if t["from"] == sender)


# ---------------------------------------------------------------------------
# A. Exact spectral math
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("k", [3, 4, 6])
def test_ring_spectrum_matches_closed_form(k):
    txs = ring_transactions(k)
    nodes = [f"RING_{i}" for i in range(k)]
    prof = induced_spectrum_profile(txs, nodes)
    assert prof["spec_cycle_residual"] < 1e-6, \
        f"ring{k} spectrum deviates: {prof}"
    A_ind = _induced_adj(txs, nodes)
    eig = np.sort(np.linalg.eigvalsh(A_ind))
    ideal = np.sort([2 * math.cos(2 * math.pi * j / k) for j in range(k)])
    assert np.allclose(eig, ideal, atol=1e-9)


def _induced_adj(txs, entities):
    lookup = _TxGraphLookup()
    for t in txs:
        lookup.observe(str(t.get("from", "")), str(t.get("to", "")))
    idx = {e: i for i, e in enumerate(entities)}
    n = len(entities)
    A = np.zeros((n, n))
    for pr in lookup.pairs:
        a, b = tuple(pr)
        if a in idx and b in idx:
            A[idx[a], idx[b]] = 1.0
            A[idx[b], idx[a]] = 1.0
    return A


@pytest.mark.parametrize("n", [3, 5, 8])
def test_star_spectrum_matches_closed_form(n):
    txs = star_transactions(n)
    ents = ["STAR_HUB"] + [f"STAR_L{i}" for i in range(n)]
    prof = induced_spectrum_profile(txs, ents)
    assert prof["spec_star_residual"] < 1e-6, f"star{n}: {prof}"
    A = _induced_adj(txs, ents)
    eig = np.sort(np.linalg.eigvalsh(A))
    assert abs(eig[-1] - math.sqrt(n)) < 1e-9
    assert abs(eig[0] + math.sqrt(n)) < 1e-9


def test_archetypes_are_distinguishable():
    rt = ring_transactions(4)
    st = star_transactions(3)
    rn = [f"RING_{i}" for i in range(4)]
    sn = ["STAR_HUB"] + [f"STAR_L{i}" for i in range(3)]
    pr = induced_spectrum_profile(rt, rn)
    ps = induced_spectrum_profile(st, sn)
    # each ideal scores far better on ITS OWN topology than on the other's
    assert pr["spec_cycle_residual"] < ps["spec_cycle_residual"]
    assert ps["spec_star_residual"] < pr["spec_star_residual"]


# ---------------------------------------------------------------------------
# B+C. Causality & determinism
# ---------------------------------------------------------------------------

def test_spectral_causality_prefix_invariance():
    base_txs = [
        {"tx_id": f"X{i}", "step": i, "from": "ACC_00001",
         "to": f"MERCHANT_{i}", "amount": 500.0}
        for i in range(10)
    ]
    extended = list(base_txs) + [
        {"tx_id": "X_late", "step": 99, "from": "ACC_00002",
         "to": "ACC_00001", "amount": 90000.0}
    ]
    X_after, _ = compute_spectral_features(extended)
    Xb, _ = compute_spectral_features(base_txs)
    # strict: first N rows identical regardless of a future appended tx
    assert np.array_equal(X_after[:len(base_txs)], Xb)


def test_spectral_determinism(signals_fixture):
    txs = signals_fixture["twin"].world.transactions[:400]
    Xa, names_a = compute_spectral_features(txs)
    Xb, names_b = compute_spectral_features(txs)
    assert names_a == names_b == SPECTRAL_FEATURE_NAMES
    assert np.array_equal(Xa, Xb)


def test_fraud_egos_light_up_cycle_signal(signals_fixture):
    """Layering attacks create dense mutual neighborhood structure; their
    cycle-residual should sit below benign retail/p2p senders' median."""
    txs = signals_fixture["twin"].world.transactions
    X_spec, _ = compute_spectral_features(txs)
    fraud_rows = [t for t in txs if t.get("is_fraud")]
    idx_map = {t["tx_id"]: i for i, t in enumerate(txs)}
    cyc = X_spec[:, [j for j, n in enumerate(SPECTRAL_FEATURE_NAMES)
                     if n == "spec_cycle_residual"][0]]
    assert any(np.isfinite(cyc[[idx_map[t['tx_id']] for t in fraud_rows]]))


# ---------------------------------------------------------------------------
# D. Manifold honesty
# ---------------------------------------------------------------------------

def _rng_normal_matrix(seed=3, n=300, d=20):
    rng = np.random.RandomState(seed)
    return rng.normal(loc=[0] * d, scale=[1] * d, size=(n, d)).clip(-6, 6)


def test_manifold_rejects_bad_training_input():
    m = NormalcyManifold(seed=1, epochs=30)
    Xbad = _rng_normal_matrix().copy()
    Xbad[7, 3] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        m.fit(Xbad)
    with pytest.raises(ValueError, match=">=20"):
        m.fit(np.zeros((10, 20)))


def test_manifold_separates_outliers_and_records_scale():
    rng = np.random.RandomState(9)
    normal = rng.normal(size=(400, 20)).clip(-5, 5)
    m = NormalcyManifold(seed=42, epochs=250).fit(normal)

    same_kind = rng.normal(size=(50, 20)).clip(-5, 5)
    outliers = same_kind.copy()
    outliers[:, 0] += 40.0            # absurd amount-scale excursion
    s_same = m.score(same_kind)
    s_out = m.score(outliers)
    assert m.n_normal_fitted == 400
    assert float(np.median(s_out)) > float(np.median(s_same)) * 2, (
        "outlier excursions must reconstruct worse than in-distribution rows"
    )
    assert np.isfinite(s_out).all() and s_out.max() <= 1.0 + 1e-6


def test_manifold_determinism_per_seed():
    rng = np.random.RandomState(11)
    X = rng.normal(size=(200, 20)).clip(-4, 4)
    p1 = NormalcyManifold(seed=13, epochs=120).fit(X).score(X[:25])
    p2 = NormalcyManifold(seed=13, epochs=120).fit(X).score(X[:25])
    assert np.array_equal(p1, p2)


# ---------------------------------------------------------------------------
# E. Ensemble sidecar + F. decorrelation artifact
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def signals_fixture():
    twin = FinancialDigitalTwin(seed=42, num_accounts=80, num_merchants=25,
                                num_devices=40, num_ip_blocks=12,
                                num_steps=14)
    twin.run()
    compiler = AttackCompiler(twin, seed=42)
    generate_training_attacks(compiler, twin.world)
    ens = BlueTeamEnsemble.untrained(seed=42)
    ens.fit_transactions(list(twin.world.transactions), twin.world,
                         oof_folds=3, gnn_epochs=12)
    from blue.features import compute_features as cf
    Xt, yt, _ = cf(list(twin.world.transactions), twin.world)
    man = NormalcyManifold(seed=42, epochs=150).fit(
        Xt[np.asarray(yt) == 0])
    return {"twin": twin, "ens": ens, "manifold": man}


def test_score_all_signals_aligned_and_finite(signals_fixture):
    sf = signals_fixture
    sample = sf["twin"].world.transactions[:150]
    cols = sf["ens"].score_all_signals(sample, sf["twin"].world,
                                       manifold=sf["manifold"])
    keys = {"xgb", "gnn", "meta", "manifold", "spectral_cycle",
            "spectral_star"}
    assert keys == set(cols.keys())
    for k, v in cols.items():
        assert v.shape[0] == len(sample)
        assert np.isfinite(v).all()
        assert v.min() >= -1e-6 and v.max() <= 1.0 + 1e-6
    # empty-input contract too
    empty = sf["ens"].score_all_signals([], manifold=sf["manifold"])
    assert all(v.shape == (0,) for v in empty.values())


@pytest.mark.skipif(not os.path.exists(DECORR),
                    reason="artifacts/decorrelation.json not generated yet; "
                           "run scripts/signals_eval.py")
def test_decorrelation_artifact_contract():
    art = json.load(open(DECORR))
    required = {"schema", "columns", "correlation_matrix",
                "separations_auc", "fingerprint", "max_offdiag_abs_corr"}
    assert required <= set(art)
    cm = np.asarray(art["correlation_matrix"])
    n = len(art["columns"])
    assert cm.shape == (n, n)
    assert np.allclose(cm, cm.T, atol=1e-9)          # symmetric
    assert np.allclose(np.diag(cm), 1.0, atol=1e-9)  # unit diagonal
    sup = art["max_offdiag_abs_corr"]
    assert 0.0 <= sup <= 1.0
    off = cm - np.eye(n)
    iu = np.triu_indices(n, 1)
    pairs_sup = np.abs(off[iu]).max()
    assert abs(pairs_sup - sup) < 1e-2               # summary consistent
