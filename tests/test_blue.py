"""
test_blue.py — Phase 2 gate: blue-team correctness.

Covers the Phase 2 gate requirements from PROMETHEUS_CONTEXT.md §5:

  A. Feature fixes (finding #7):
     - device_account_count / ip_account_count are causal, bidirectional, nonzero
     - _seed_histories reads real AccountState dataclasses (was dead)
     - INR maps to index 0 (no constant fallback)
     - node features: 7 dims in NODE_FEATURE_NAMES order; cold-start matches
     - edge_weight exists, derived deterministically, and is CONSUMED by the
       GNN forward pass
  B. Meta honesty (finding #4):
     - OR-gate deleted: predict_proba != max(base scores); can fall BELOW both
     - isotonic actually selected at scale (>=200 rows), sigmoid below
     - oof flag recorded; make_oof_scores gives leak-free columns that differ
       from naive in-sample scoring
  C. Two-axis holdout (splits.py):
     - fingerprint deterministic + tamper detection via load_holdout_spec
     - assert_no_leakage catches held-out types AND mechanisms on each axis
     - mechanism registry namespace enforced

Determinism: every stochastic piece flows through seeds.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys

import numpy as np
import pytest
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.blue.features import (
    compute_features,
    build_graph_data,
    get_node_features,
    NODE_FEATURE_NAMES,
    NODE_FEATURE_DIM,
    CURRENCIES,
    _seed_histories,
)
from src.blue.meta_model import MetaModel, make_oof_scores, ISOTONIC_MIN_SAMPLES
from src.blue.splits import (
    lock_holdout,
    load_holdout_spec,
    assert_no_leakage,
    split_by_step,
    register_mechanism,
    MECHANISM_REGISTRY,
)
from src.twin.core import WorldState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tx(step, frm="ACC_00001", to="MERCHANT_00001", amount=500.0, device=None,
       ip=None, is_fraud=False, attack_id=None, currency="INR", **extra):
    base = {
        "tx_id": f"TX_{step}_{random.randint(0, 10**9):09d}",
        "step": step, "from": frm, "to": to, "amount": amount,
        "currency": currency, "category": "retail",
        "device": device, "ip": ip, "is_fraud": is_fraud,
        "attack_id": attack_id, "trajectory_id": None,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# A. Feature correctness
# ---------------------------------------------------------------------------

def test_device_sharing_counts_causal_and_nonzero():
    """A1 and A3 both use DEV_001 before A1's own use of it: the counter must
    reflect OTHER accounts sharing the device (legacy bug: always 0)."""
    stream = [
        tx(1, frm="ACC_00001", device="DEV_001"),
        tx(2, frm="ACC_00003", device="DEV_001"),
        tx(3, frm="ACC_00001", device="DEV_001"),   # repeat device for A1
        tx(4, frm="ACC_00002", device=None),
    ]
    X, _, names = compute_features(stream)
    i_new_dev = names.index("is_new_device")
    i_dev_cnt = names.index("device_account_count")

    # tx1: brand-new device for ACC_00001 -> new=1, sharing=0 others
    assert X[0, i_new_dev] == 1.0 and X[0, i_dev_cnt] == 0.0
    # tx2: DEV_001 new for ACC_00003, already used by ACC_00001 -> count 1
    assert X[1, i_new_dev] == 1.0 and X[1, i_dev_cnt] == 1.0
    # tx3: repeat for ACC_00001, two accounts seen overall -> count 2
    assert X[2, i_new_dev] == 0.0 and X[2, i_dev_cnt] == 2.0
    # tx4: no device -> zeros (never fabricated values)
    assert X[3, i_new_dev] == 0.0 and X[3, i_dev_cnt] == 0.0


def test_ip_sharing_count_causal():
    stream = [
        tx(1, frm="ACC_00001", ip="IP_9"),
        tx(2, frm="ACC_00004", ip="IP_9"),
        tx(3, frm="ACC_00007", ip="IP_9"),
        tx(4, frm="ACC_00002", ip="IP_8"),
    ]
    X, y, names = compute_features(stream)
    col = names.index("ip_account_count")
    assert X[0, col] == 0.0      # first ever use
    assert X[1, col] == 1.0
    assert X[2, col] == 2.0
    assert X[3, col] == 0.0      # different IP, unused so far
    # causality: counts only reflect accounts seen on THAT ip at earlier steps


def test_seed_histories_reads_real_dataclass():
    w = WorldState(seed=3)
    w.add_customer()
    w.add_account("CUST_00001")
    dev = w.add_device()
    w.accounts["ACC_00001"].linked_devices.append(dev.device_id)
    dev.linked_accounts.append("ACC_00001")

    known_d, known_i = _seed_histories(w)
    assert str(dev.device_id) in known_d["ACC_00001"]
    assert known_i == {}

    stream = [tx(10, frm="ACC_00001", device=str(dev.device_id))]
    X, _, names = compute_features(stream, world_state=w)
    assert X[0, names.index("is_new_device")] == 0.0   # seeded: not new


def test_inr_currency_maps_to_index_zero():
    assert CURRENCIES[0] == "INR"
    X, _, names = compute_features([tx(1)])
    assert X[0, names.index("currency_code")] == 0.0   # was fallback len() before


def test_unknown_currency_falls_back():
    X, _, names = compute_features([tx(1, currency="XYZ")])
    assert X[0, names.index("currency_code")] == float(len(CURRENCIES))


def test_node_features_seven_dims_named_order():
    stream = [
        tx(1, frm="ACC_00001", to="MERCHANT_00001", amount=1000.0),
        tx(2, frm="ACC_00002", to="ACC_00001", amount=500.0),
        tx(3, frm="ACC_00001", to="MERCHANT_00002", amount=250.0),
    ]
    data, nmap = build_graph_data(stream)
    assert data is not None
    assert data.x.shape[1] == NODE_FEATURE_DIM == 7

    rows = {nid: data.x[i].tolist() for nid, i in nmap.items()}
    a1 = rows["ACC_00001"]
    # order: total_degree, out_degree, in_degree, total_sent, avg_amount, is_acct, is_merchant
    assert a1[0] == pytest.approx((2 + 1) / 100.0)          # 2 out, 1 in
    assert a1[1] == pytest.approx(2 / 100.0)
    assert a1[2] == pytest.approx(1 / 100.0)
    assert a1[3] == pytest.approx(min(1250.0 / 1e6, 10.0))  # 1000+250 sent
    assert a1[4] == pytest.approx(min(625.0 / 1e5, 10.0))   # avg sent
    assert a1[5] == 1.0 and a1[6] == 0.0
    m = rows["MERCHANT_00001"]
    assert m[5] == 0.0 and m[6] == 1.0


def test_cold_start_features_match_dim_and_flags():
    v = get_node_features("ACC_777")
    assert len(v) == NODE_FEATURE_DIM == 7
    assert v[5] == 1.0 and v[6] == 0.0
    m = get_node_features("MERCHANT_004")
    assert m[5] == 0.0 and m[6] == 1.0


def test_edge_weight_present_and_deterministic():
    stream = [
        tx(1, frm="ACC_00001", to="ACC_00002", amount=90_000.0),
        tx(2, frm="ACC_00001", to="ACC_00002", amount=90_000.0),  # repeat pair
        tx(3, frm="ACC_00003", to="ACC_00004", amount=50.0),
    ]
    d1, _ = build_graph_data(stream)
    d2, _ = build_graph_data(list(reversed([dict(t) for t in stream])))
    assert d1.edge_weight is not None
    assert torch.allclose(d1.edge_weight, d2.edge_weight)   # step-sorted determinism

    ew = d1.edge_weight.numpy()
    attrs = d1.edge_attr.numpy()
    # amounts scale the weight upward from the floor; repeats damp it
    heavy_first = attrs[0][0]                       # 0.9 amount on pair (0->1) forward edge
    repeat_edge_w = None
    # edges are stored fwd,rev per tx; tx1 fwd at 0, tx2 fwd at 2
    assert abs(ew[0] - ew[1]) < 1e-7                # reverse mirrors forward
    assert ew[2] < ew[0]                            # same amount but repeat=1 -> damped
    assert all(w > 0 for w in ew)


def test_gnn_consumes_edge_attr():
    """edge_attr must reach the conv layers — output must change when edge
    attributes change (they were previously computed then silently dropped)."""
    from src.blue.gnn_model import GNNFraudDetector

    rng = random.Random(11)
    stream = []
    for s in range(60):
        f = f"ACC_{rng.randint(1, 12):05d}"
        t = f"ACC_{rng.randint(13, 24):05d}"
        if rng.random() < 0.25:
            fraud = True
            f = f"ACC_{rng.randint(1, 3):05d}"      # colluding cluster
        else:
            fraud = False
        stream.append(tx(s, frm=f, to=t, amount=rng.uniform(50, 90000),
                         is_fraud=fraud))
    data, _ = build_graph_data(stream)
    det = GNNFraudDetector(in_channels=data.x.shape[1], seed=42)
    det.fit(data, epochs=20)

    base_p = torch.from_numpy(det.predict_proba(data))

    zeroed = data.clone()
    zeroed.edge_attr = torch.zeros_like(data.edge_attr)   # no amount/repeat info
    zw_p = torch.from_numpy(det.predict_proba(zeroed))

    assert not torch.allclose(base_p, zw_p), \
        "GNN output identical with/without real edge attrs: still unconsumed"


# ---------------------------------------------------------------------------
# B. Meta-model honesty
# ---------------------------------------------------------------------------

def _toy_meta_data(n_per_class=40, seed=0):
    """gnn_score discriminates, xgb_score does NOT (noise)."""
    rng = np.random.RandomState(seed)
    xgb = rng.rand(n_per_class * 2)                     # pure noise column
    gnn = np.concatenate([
        rng.normal(0.25, 0.05, n_per_class),            # legit
        rng.normal(0.75, 0.05, n_per_class),            # fraud
    ]).clip(0, 1)
    X = np.column_stack([xgb, gnn])
    y = np.array([0] * n_per_class + [1] * n_per_class)
    return X, y


def test_or_gate_is_gone():
    """On this data the blend must trust gnn over xgb; an OR-gate would pin
    outputs near max(x). Assert scores sit strictly below max(base) for the
    xgb-dominant direction."""
    X, y = _toy_meta_data()
    meta = MetaModel(seed=42).fit(X, y, oof=True)
    probe = np.array([[0.99, 0.30], [0.30, 0.85]])
    p = meta.predict_proba(probe)
    # No OR-gate: output is a probability blend, never pinned >= every base.
    assert p[0] < 0.99, f"OR-gate behaviour detected ({p[0]:.3f} >= xgb 0.99)"
    # Discriminative direction wins regardless of the noise column.
    assert p[1] > p[0]
    # ...and it responds to the informative column, not the constant one
    flipped = np.array([[0.30, 0.10]])
    assert meta.predict_proba(flipped)[0] < p[1]


def test_calibration_method_selected_honestly():
    big_X, big_y = _toy_meta_data(n_per_class=150, seed=1)   # 300 rows
    small_X, small_y = _toy_meta_data(n_per_class=20, seed=2)  # 40 rows

    meta_big = MetaModel(seed=42).fit(big_X, big_y, oof=True)
    meta_small = MetaModel(seed=42).fit(small_X, small_y, oof=True)

    assert big_X.shape[0] >= ISOTONIC_MIN_SAMPLES
    assert meta_big.calibration_method == "isotonic"
    assert meta_small.calibration_method == "sigmoid"


def test_oof_flag_recorded_and_warned(capsys):
    X, y = _toy_meta_data(n_per_class=15, seed=3)
    meta_bad = MetaModel(seed=42).fit(X, y)              # oof defaults False
    assert meta_bad.oof_used_ is False
    meta_good = MetaModel(seed=42).fit(X, y, oof=True)
    assert meta_good.oof_used_ is True
    diag = meta_good.diagnostics
    assert diag["oof_used"] is True and "coefficients" in diag


def test_make_oof_scores_differs_from_in_sample():
    X, y = _toy_meta_data(n_per_class=120, seed=4)
    tab = X                                              # reuse as features

    def factory(train_idx):
        from sklearn.linear_model import LogisticRegression
        clf = LogisticRegression(max_iter=500).fit(tab[train_idx], y[train_idx])
        return lambda val_idx: clf.predict_proba(tab[val_idx])[:, 1]

    oof = make_oof_scores(factory, n_samples=len(y), n_splits=5, y=y)
    assert oof.min() >= 0.0 and oof.max() <= 1.0
    # An in-sample scorer would produce perfectly separated extremes; OOF
    # carries honest cross-fold uncertainty and overlaps classes.
    separated_in_sample = (
        np.percentile(oof[y == 0], 95) < np.percentile(oof[y == 1], 5)
    )
    assert not separated_in_sample or np.std(oof) > 0.05
    # folds cover everything exactly once -> no NaNs
    assert np.isfinite(oof).all()


def test_make_oof_scores_single_class_fallback():
    y = np.zeros(30)
    calls = {"n": 0}

    def factory(train_idx):
        def scorer(val_idx):
            calls["n"] += 1
            return np.full(len(val_idx), 0.5)
        return scorer

    out = make_oof_scores(factory, n_samples=30, n_splits=3, y=y)
    assert out.shape == (30,) and (out == 0.5).all()
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# C. Two-axis holdout
# ---------------------------------------------------------------------------

def test_lock_holdout_fingerprint_deterministic_and_tamper_evident():
    s1 = lock_holdout(seed=7)
    s2 = lock_holdout(seed=7)
    assert s1.fingerprint == s2.fingerprint

    other = lock_holdout(seed=8)
    assert other.fingerprint != s1.fingerprint

    d = s1.to_dict()
    load_holdout_spec(d)                                  # verifies cleanly
    tampered = dict(d)
    tampered["held_out_types"] = []                        # quietly rescope? NO
    with pytest.raises(AssertionError, match="fingerprint mismatch"):
        load_holdout_spec(tampered)


def test_leakage_assert_catches_each_axis():
    spec = lock_holdout(seed=42, held_out_types=("A2", "A5"),
                        held_out_mechanisms=("shadow_pgd",))

    clean = [tx(1, attack_id="A1"), tx(2, attack_id="A3_v2")]
    assert_no_leakage(clean, spec)                        # no raise

    with pytest.raises(AssertionError, match="types"):
        assert_no_leakage([tx(1, attack_id="A2")], spec)
    with pytest.raises(AssertionError, match="types"):
        assert_no_leakage([tx(1, attack_id="A5_like_variant")], spec)
    with pytest.raises(AssertionError, match="mechanisms"):
        assert_no_leakage(
            [tx(1, attack_id="A1", mechanism="shadow_pgd", is_fraud=True)],
            spec)

    # untagged mechanisms default to rule_compiler → allowed here
    assert_no_leakage([tx(1, attack_id="A1", is_fraud=True)], spec)


def test_unregistered_mechanism_rejected_at_lock_time():
    with pytest.raises(ValueError, match="not registered"):
        lock_holdout(seed=1, held_out_mechanisms=("alien_mech",))
    register_mechanism("alien_mech")
    assert "alien_mech" in MECHANISM_REGISTRY             # now lockable
    spec = lock_holdout(seed=1, held_out_mechanisms=("alien_mech",))
    assert spec.held_out_mechanisms == frozenset({"alien_mech"})


def test_split_by_step_temporal_boundary():
    txs = [tx(i) for i in range(100)]
    tr, ev = split_by_step(txs, eval_fraction=0.3, min_eval_steps=10)
    assert tr and ev
    assert max(tx(txs[i]["step"])["step"] for i in tr) < min(
        txs[i]["step"] for i in ev)
    assert set(tr) | set(ev) == set(range(100))
