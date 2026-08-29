"""test_phase7.py — Phase 7 gate: 3 standout panels (timeline / RL
negative-result / mechanism × evidence-source attribution).

Gate requirements from implementation.md Phase 7:
  1. Blind-spot timeline: persisted per-cycle rows, capped, deterministic.
  2. RL negative-result panel data endpoint serves the real `rl_stretch`
     measurement (honest negative semantics, never fabricated).
  3. Attribution matrix: mechanism × evidence-source (XGB/GNN/OSINT/sanctions),
     rates in [0,1], margins sum to caught, byte-deterministic given inputs.
  4. Sanctions watch-list build order is canonical (regression: the RNG draws
     used to run over set-iteration order, which varies per process).
"""

from __future__ import annotations

import os
import random
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from twin.twin import FinancialDigitalTwin                                  # noqa: E402
from attack.compiler import AttackCompiler                               # noqa: E402
from attack.benchmark_attacks import generate_training_attacks            # noqa: E402
from blue.ensemble import BlueTeamEnsemble                                # noqa: E402
from feedback.timeline import FeedbackTimeline, summarize_cycle, SCHEMA   # noqa: E402
from eval.attribution import (                                            # noqa: E402
    MECHANISM_ORDER, SOURCE_ORDER, build_attribution_matrix,
    combine_matrices, mechanism_label,
)
from investigate.case_manager import CaseManager                           # noqa: E402
from investigate.sanctions import build_watch_list                        # noqa: E402


@pytest.fixture(scope="module")
def attribution_world():
    """Tiny world with tagged training attacks + a fitted ensemble.

    Mirrors test_feedback's trained_ensemble shape so attribution can be
    exercised end-to-end without the heavy trigger/feedback machinery."""
    twin = FinancialDigitalTwin(seed=42, num_accounts=60, num_merchants=20,
                                num_devices=30, num_ip_blocks=10,
                                num_steps=6)
    twin.run()
    compiler = AttackCompiler(twin, seed=42)
    generate_training_attacks(compiler, twin.world)
    assert any(t.get("is_fraud") for t in twin.world.transactions)
    blue = BlueTeamEnsemble.untrained(seed=42)
    blue.fit_transactions(list(twin.world.transactions), twin.world,
                          oof_folds=3, gnn_epochs=15)
    cm = CaseManager(ensemble=blue, twin=twin, seed=42)
    return {"twin": twin, "blue": blue, "cm": cm}


# ---------------------------------------------------------------------------
# 1. Timeline storage
# ---------------------------------------------------------------------------

def test_summarize_cycle_folds_report_fields():
    summary = summarize_cycle(
        {
            "blind_spot": "sybil v2",
            "recall_before": 0.42123456,
            "recall_after": 1.0,
            "improved": True,
            "generated_fixes": 7,
            "retrain_rounds_used": 2,
            "max_retrain_rounds": 2,
            "generalization_recall_unseen_generator": 0.9,
            "evidence_ids": [{"x": 1}],
        },
        seed=7, source="demo")
    assert round(summary["recall_before"], 4) == 0.4212
    assert summary["blind_spot"] == "sybil v2"
    assert summary["generated_fixes"] == 7
    assert summary["source"] == "demo"
    assert summary["seed_used"] == 7


def test_timeline_append_load_roundtrip(tmp_path):
    path = str(tmp_path / "timeline.json")
    tl = FeedbackTimeline(path)
    assert tl.count() == 0
    i0 = tl.append(summarize_cycle({"recall_before": 0.5, "recall_after": 1.0}))
    i1 = tl.append(summarize_cycle({"recall_before": 1.0, "recall_after": 1.0}))
    assert (i0, i1) == (0, 1)

    loaded = FeedbackTimeline.load(path)
    assert loaded.count() == 2
    assert [e["idx"] for e in loaded.entries()] == [0, 1]
    assert loaded.entries()[1]["recall_before"] == 1.0


def test_timeline_cap_retention(tmp_path):
    path = str(tmp_path / "tl.json")
    tl = FeedbackTimeline(path, max_entries=5)
    idxs = [tl.append(summarize_cycle({})) for _ in range(12)]
    assert idxs[-1] == 11
    tl = FeedbackTimeline(path, max_entries=5)
    assert tl.count() == 5
    assert [e["idx"] for e in tl.entries()] == [7, 8, 9, 10, 11]


def test_timeline_load_skips_non_dict_and_bogus_rows(tmp_path):
    path = str(tmp_path / "tl.json")
    with open(path, "w") as f:
        import json
        json.dump({
            "schema": SCHEMA,
            "entries": [{"idx": 0, "blind_spot": "x"}, "junk", {"no_idx": 1},
                        {"idx": 2}],
        }, f)
    tl = FeedbackTimeline(path)
    assert [e["idx"] for e in tl.entries()] == [0, 2]


def test_timeline_append_does_not_rely_on_wall_clock(tmp_path):
    """Rows change only when inputs change — no time fields leak in."""
    path = str(tmp_path / "tl.json")
    tl = FeedbackTimeline(path)
    tl.append(summarize_cycle({"blind_spot": "relational camouflage"}))
    blob = open(path).read()
    assert "run_cycle" not in blob and "runtime" not in blob


# ---------------------------------------------------------------------------
# 3. Attribution matrix contracts
# ---------------------------------------------------------------------------

def test_attribution_contract_bounds_and_margins(attribution_world):
    m = build_attribution_matrix(
        attribution_world["twin"].world, attribution_world["blue"],
        case_manager=attribution_world["cm"], threshold=0.5, max_rows=4000,
        seed=42)
    assert m["schema"] == "prometheus.attribution.v1"
    assert m["sources"] == SOURCE_ORDER
    assert set(m["mechanisms"]) <= set(MECHANISM_ORDER + ["other"])
    for mech, cells in m["matrix"].items():
        # per-source hit counts are a subset of the caught margin
        assert set(cells) == set(SOURCE_ORDER)
        assert all(0 <= cells[s] <= m["margins"][mech]
                   for s in SOURCE_ORDER)
        assert m["margins"][mech] >= 1
    assert sum(m["margins"].values()) == m["caught_attributed"]
    for mech, rates in m["rates"].items():
        for s, r in rates.items():
            assert 0.0 <= r <= 1.0
    assert isinstance(m["fingerprint"], str) and len(m["fingerprint"]) == 16
    assert m["coverage"]["n_txs_attributed"] >= 0


def test_attribution_deterministic_across_global_rng_states(attribution_world):
    kw = dict(world=attribution_world["twin"].world,
              victim=attribution_world["blue"],
              case_manager=attribution_world["cm"])
    random.seed(1)
    a = build_attribution_matrix(**kw)
    random.seed(2)
    b = build_attribution_matrix(**kw)
    assert a["matrix"] == b["matrix"]
    assert a["fingerprint"] == b["fingerprint"]


def test_attribution_mechanism_label_unknown():
    assert mechanism_label("rule_compiler") == "rule_compiler"
    assert mechanism_label("shadow_pgd") == "shadow_pgd"
    assert mechanism_label(None) == "other"
    assert mechanism_label("mystery") == "other"


def test_combine_matrices_folds_and_preserves_provenance(attribution_world):
    kw = dict(world=attribution_world["twin"].world,
              victim=attribution_world["blue"],
              case_manager=attribution_world["cm"])
    single = build_attribution_matrix(**kw, seed=42)
    combined = combine_matrices({"a": single, "b": single})
    for mech in single["mechanisms"]:
        if mech in combined["mechanisms"]:
            assert combined["margins"][mech] >= single["margins"][mech]
    assert set(combined["worlds"]) == {"a", "b"}
    assert combined["worlds"]["a"]["fingerprint"] == single["fingerprint"]


# ---------------------------------------------------------------------------
# 4. Sanctions watch-list canonical-order regression
# ---------------------------------------------------------------------------

def test_watch_list_result_independent_of_input_set_hash_order():
    names = {f"n{i:03d}" for i in range(200)}
    wl = build_watch_list(names, seed=42)
    wl_sorted = build_watch_list(sorted(names), seed=42)
    assert wl == wl_sorted
    # perturb global rng: the draw order must be canonical, not global-state
    random.seed(7)
    wl2 = build_watch_list(names, seed=42)
    assert wl2 == wl


def test_sanctions_agent_screen_deterministic_for_same_fixtures(attribution_world):
    from investigate.sanctions import SanctionsAgent
    fx = attribution_world["cm"]._fixtures()
    a1 = SanctionsAgent(fx, mode="fixture", call_budget=999, watch_seed=42)
    a2 = SanctionsAgent(fx, mode="fixture", call_budget=999, watch_seed=42)
    accs = sorted(k for k in fx if k.startswith("ACC_"))[:20]
    for acc in accs:
        assert a1.screen(acc)["result"] == a2.screen(acc)["result"]


# ---------------------------------------------------------------------------
# Panel data endpoints
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def panel_client():
    """TestClient WITHOUT /api/init — the three panel endpoints are
    read-only and must not depend on session state."""
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def test_timeline_endpoint_schema(panel_client):
    r = panel_client.get("/api/timeline")
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert isinstance(data.get("present"), bool)
    if data["present"]:
        assert data.get("schema") == "prometheus.feedback_timeline.v1"
        for e in data.get("entries", []):
            assert set(e).issuperset(
                {"idx", "blind_spot", "recall_before", "recall_after",
                 "generated_fixes", "retrain_rounds_used"})
    else:
        assert isinstance(data.get("note"), str)


def test_attribution_endpoint_exhibit_schema(panel_client):
    r = panel_client.get("/api/attribution")
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert data["sources"] == SOURCE_ORDER
    ex = data.get("exhibit")
    if ex:
        assert ex["schema"] == "prometheus.attribution.v1"
        assert len(ex["fingerprint"]) == 16
        for s in SOURCE_ORDER:
            for mech in ex["mechanisms"]:
                assert s in ex["matrix"][mech]
    else:
        assert data["present"] is False


def test_rl_stretch_endpoint_schema(panel_client):
    r = panel_client.get("/api/rl-stretch")
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert isinstance(data.get("present"), bool)
    if data["present"]:
        # honest semantics: negative == decision not to ship
        assert data["honest_negative"] == (not data["shipped"])
        assert data.get("episodes_run") is not None
        assert isinstance(data.get("reason"), str)
    else:
        assert isinstance(data.get("note"), str)