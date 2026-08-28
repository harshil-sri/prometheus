"""fidelity.py — Three-layer fidelity evaluation (Phase 7).

LAYER 1 — STATISTICAL: per-feature distributional distances between twin
    normal traffic and a CTGAN critique generator's output. Wasserstein-1
    and KS statistics for numeric columns; total-variation distance for
    categorical columns. Aggregated as per-column dicts; no invented
    composite score — the report carries the raw measurements.

LAYER 2 — BEHAVIORAL: structural realism of the twin itself. Networkx
    degree/motif statistics, income-cycle regularity (recurring salaries on
    their cadence), inter-arrival overlap between normal and fraud
    populations, category coverage vs configured catalog, liveness (share of
    accounts that ever transact).

LAYER 3 — ADVERSARIAL: discriminator-trap portability checks.
    * critic AUC: can XGBoost separate real-normal from CTGAN-synthetic
      normal rows? (~0.5 = generator survives the trap = strong fidelity)
    * manifold transfer: Spearman correlation between manifold scores fitted
      on real-normals vs on synth-normals when scoring held-out REAL data
      (high corr ⇒ anomaly structure transfers).
All outputs are measured numbers with provenance; thresholds live only in
the artifact schema as explicit pass flags.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

__all__ = ["statistical_layer", "behavioral_layer", "adversarial_layer",
           "build_fidelity_report", "NUMERIC_PROXY_COLUMNS",
           "CATEGORICAL_COLUMNS"]

#: feature columns treated as continuous for distributional tests
NUMERIC_PROXY_COLUMNS = [
    "amount", "log_amount", "velocity_10", "velocity_50",
    "sender_tx_count", "sender_avg_amount", "time_since_last_tx",
    "device_account_count", "ip_account_count", "hour_of_day",
]
CATEGORICAL_COLUMNS = ["merchant_category", "is_p2p", "is_night",
                       "currency_code"]

STATISTICAL_PASS = {"wasserstein_ratio_max": 0.75,
                    "ks_stat_median": 0.35}
ADVERSARIAL_PASS = {"critic_auc_band": (0.45, 0.72)}


# ---------------------------------------------------------------------------
# Layer 1 — statistical
# ---------------------------------------------------------------------------

def statistical_layer(X_real: np.ndarray, X_synth: np.ndarray,
                      feature_names: Sequence[str]) -> Dict[str, Any]:
    """Per-column Wasserstein + KS + TV distance matrix."""
    from scipy.stats import wasserstein_distance, ks_2samp

    names = list(feature_names)
    num_idx = [names.index(c) for c in NUMERIC_PROXY_COLUMNS
               if c in names]
    cat_idx = {c: names.index(c) for c in CATEGORICAL_COLUMNS if c in names}

    scale_real = np.array([np.std(X_real[:, i]) or 1.0 for i in num_idx])
    per_numeric: Dict[str, Dict[str, float]] = {}
    ratios = []
    ks_values = []
    for k, i in enumerate(num_idx):
        col_r, col_s = X_real[:, i], X_synth[:, i]
        w = float(wasserstein_distance(col_r, col_s))
        ratio = w / float(scale_real[k])
        ks = ks_2samp(col_r, col_s)
        per_numeric[names[i]] = {
            "wasserstein": round(w, 6),
            "wasserstein_over_std": round(ratio, 4),
            "ks_stat": round(float(ks.statistic), 4),
        }
        ratios.append(ratio)
        ks_values.append(float(ks.statistic))

    per_cat: Dict[str, Dict[str, float]] = {}
    tvs = []
    for c, i in cat_idx.items():
        cats = np.unique(np.concatenate([X_real[:, i], X_synth[:, i]]))
        pr = np.array([(X_real[:, i] == c).mean() for c in cats])
        ps = np.array([(X_synth[:, i] == c).mean() for c in cats])
        tv = 0.5 * float(np.abs(pr - ps).sum())
        per_cat[c] = {"tv_distance": round(tv, 4)}
        tvs.append(tv)

    return {
        "per_column_numeric": per_numeric,
        "per_column_categorical": per_cat,
        "wasserstein_ratio_median": round(float(np.median(ratios)), 4),
        "wasserstein_ratio_max": round(float(np.max(ratios)), 4),
        "worst_columns_by_w_ratio": sorted(
            per_numeric.items(),
            key=lambda kv: -kv[1]["wasserstein_over_std"])[:3],
        "ks_stat_median": round(float(np.median(ks_values)), 4),
        "tv_median": round(float(np.median(tvs)), 4),
        "pass_flags": {
            "wasserstein_ok":
                bool(float(np.median(ratios)) <=
                     STATISTICAL_PASS["wasserstein_ratio_max"]),
            "wasserstein_tail_ok":
                bool(float(np.max(ratios)) <=
                     STATISTICAL_PASS["wasserstein_ratio_max"] * 3),
            "ks_ok": bool(float(np.median(ks_values)) <=
                          STATISTICAL_PASS["ks_stat_median"]),
        },
    }


# ---------------------------------------------------------------------------
# Layer 2 — behavioral
# ---------------------------------------------------------------------------

def behavioral_layer(twin, transactions: List[dict],
                     config: Optional[dict] = None) -> Dict[str, Any]:
    import networkx as nx

    world = twin.world
    G = nx.DiGraph()
    salary_by_acct: Dict[str, List[int]] = {}
    iat_normal: List[float] = []
    last_step_normal: Dict[str, int] = {}
    categories_used = set()
    active_accounts = set()

    for t in transactions:
        frm, to = str(t.get("from")), str(t.get("to"))
        step = int(t.get("step", 0))
        # liveness counts INTERNAL ACCOUNTS only (merchants/EXT excluded)
        if frm in world.accounts:
            active_accounts.add(frm)
        if to in world.accounts:
            active_accounts.add(to)
        if to.startswith("MERCHANT"):
            categories_used.add(t.get("category"))
        if t.get("category") != "salary" and frm in world.accounts:
            prev = last_step_normal.get(frm)
            if prev is not None:
                iat_normal.append(step - prev)
            last_step_normal[frm] = step
        if t.get("from") == "EXT_SALARY":
            salary_by_acct.setdefault(to, []).append(step)

        G.add_edge(frm, to, weight=float(t.get("amount", 0.0)))

    und = G.to_undirected()
    clustering = nx.average_clustering(und) if und.number_of_nodes() else 0.0
    deg_seq = np.array([d for _, d in und.degree()] or [0])
    triangle_nodes = sum(nx.triangles(und).values()) // 3 \
        if und.number_of_nodes() else 0

    # recurring salary cadence check against the TWIN's declared mechanics
    interval = int(getattr(twin, "salary_interval", 30))
    expected_recs = set(getattr(twin, "salary_recipients", []))
    on_cadence = 0
    multi = 0
    tol = 1
    for acct, steps_l in salary_by_acct.items():
        sl = sorted(steps_l)
        if len(sl) < 2:
            continue
        multi += 1
        gaps_ok = all(abs((b - a) - interval) <= tol
                      for a, b in zip(sl, sl[1:]))
        on_cadence += int(gaps_ok)

    n_active = len(active_accounts)
    n_accounts = max(1, len(world.accounts))
    fraud_steps = sorted(t["step"] for t in transactions if t.get("is_fraud"))

    salary_on_cadence_ratio = round(on_cadence / multi, 4) if multi else None

    return {
        "graph": {
            "nodes": und.number_of_nodes(),
            "edges": und.number_of_edges(),
            "avg_clustering": round(clustering, 4),
            "degree_p50": float(np.percentile(deg_seq, 50)),
            "degree_p95": float(np.percentile(deg_seq, 95)),
            "triangle_count_approx": int(triangle_nodes),
        },
        "income_cycles": {
            "expected_interval": interval,
            "salary_recipients_seen": len(salary_by_acct),
            "declared_recipients": len(expected_recs),
            "recipients_paid_more_than_once": multi,
            "recipients_on_expected_cadence": on_cadence,
            "on_cadence_ratio": salary_on_cadence_ratio,
        },
        "iat_overlap": {
            "normal_iat_median": float(np.median(iat_normal))
            if iat_normal else None,
            "normal_iat_p10": float(np.percentile(iat_normal, 10))
            if iat_normal else None,
            "fraud_iat_median_gap": (
                float(np.median(np.diff(fraud_steps)))
                if len(fraud_steps) > 2 else None),
        },
        "liveness": {
            "active_accounts": n_active,
            "account_active_ratio": round(n_active / n_accounts, 4),
            "categories_used_vs_catalog":
                [len(categories_used), len(set(
                    m.category for m in world.merchants.values())) or None],
        },
    }


# ---------------------------------------------------------------------------
# Layer 3 — adversarial (discriminator-trap + manifold transfer)
# ---------------------------------------------------------------------------

def conform_to_support(X_synth: np.ndarray, X_real_ref: np.ndarray,
                       feature_names: Sequence[str]) -> np.ndarray:
    """Post-process synthetic rows to sit on the SAME discrete supports as
    the reference distribution.

    Counters/flags/categorical codes are integers (0..k) or near-degenerate
    columns in tabular fraud features; generators emit floats which any
    tree-based critic can spot instantly regardless of generator quality.
    Snapping to observed unique values is standard synthetic-release
    practice and is DECLARED wherever this transform is used. Columns whose
    reference support is rich (>32 uniques) stay continuous."""
    X = X_synth.copy()
    for j, name in enumerate(feature_names):
        uniq = np.unique(X_real_ref[:, j])
        if len(uniq) <= 32:
            pos = np.searchsorted(uniq, X[:, j])
            pos = np.clip(pos, 0, len(uniq) - 1)
            left, right = uniq[pos], uniq[np.clip(pos + 1, 0, len(uniq) - 1)]
            choose_right = np.abs(right - X[:, j]) < np.abs(left - X[:, j])
            X[:, j] = np.where(choose_right, right, left)
    return X


def adversarial_layer(victim_ensemble, real_normal: np.ndarray,
                      synth_normal: np.ndarray, seed: int = 42,
                      feature_names: Optional[Sequence[str]] = None,
                      ) -> Dict[str, Any]:
    from blue.xgb_model import XGBFraudDetector
    from scipy.stats import spearmanr

    rng = np.random.RandomState(seed)

    # --- RAW trap (before support projection): reported for transparency ---

    def _auc(rows_a, rows_b) -> float:
        n = min(len(rows_a), len(rows_b))
        a_idx = rng.choice(len(rows_a), size=n, replace=False)
        b_idx = rng.choice(len(rows_b), size=n, replace=False)
        Xd = np.vstack([rows_a[a_idx], rows_b[b_idx]])
        yd = np.concatenate([np.zeros(n), np.ones(n)])
        perm = rng.permutation(len(yd))
        split = int(0.7 * len(yd))
        disc = XGBFraudDetector(seed=seed, n_estimators=120, max_depth=5)
        disc.fit(Xd[perm][:split], yd[perm][:split])
        from sklearn.metrics import roc_auc_score
        return float(roc_auc_score(
            yd[perm][split:], disc.predict_proba(Xd[perm][split:])))

    raw_auc = _auc(real_normal, synth_normal)

    # --- SUPPORT-PROJECTED trap (the one judged against the band) ----------
    if feature_names is not None:
        synth_conformed = conform_to_support(synth_normal, real_normal,
                                             feature_names)
    else:
        synth_conformed = synth_normal
    proj_auc = _auc(real_normal, synth_conformed)

    lo, hi = ADVERSARIAL_PASS["critic_auc_band"]
    survived = bool(lo <= proj_auc <= hi)
    if survived:
        conclusion = ("critique generator survives the discriminator "
                      "trap on this population")
    else:
        direction = "leaks artifacts to trees" if proj_auc > hi \
            else "under-covers the normal manifold"
        conclusion = (
            f"declared band NOT met ({proj_auc:.3f} vs [{lo}, {hi}]); "
            f"the critique generator {direction} at the 20-dim joint "
            f"level — reported as a measured limitation, never padded")
    interpretation_note = (
        "judged on support-projected synthetics (counters/flags snapped to "
        "observed supports); raw AUC kept for transparency")

    # --- manifold-score transfer -----------------------------------------
    swap_corr: Optional[float] = None
    try:
        from blue.manifold import NormalcyManifold
        hold = rng.choice(len(real_normal), size=min(400, len(real_normal)),
                          replace=False)
        m1 = NormalcyManifold(seed=seed, epochs=250).fit(real_normal)
        s_real_fit = m1.score(real_normal[hold])
        m2 = NormalcyManifold(seed=seed + 3, epochs=250).fit(synth_normal)
        s_syn_fit = m2.score(real_normal[hold])
        rho = spearmanr(s_real_fit, s_syn_fit).statistic
        swap_corr = round(float(rho), 4)
    except Exception as exc:
        logger.warning("manifold transfer skipped: %s", exc)

    return {
        "critic_trap": {
            "raw_auc_before_support_projection": round(raw_auc, 4),
            "auc": round(proj_auc, 4),
            "survived_band": survived,
            "band": list(ADVERSARIAL_PASS["critic_auc_band"]),
            "conclusion": conclusion,
            "interpretation": interpretation_note,
        },
        "manifold_transfer": {
            "spearman_score_correlation": swap_corr,
            "held_out_real_rows": int(min(400, len(real_normal))),
        },
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_fidelity_report(statistical: Dict[str, Any],
                          behavioral: Dict[str, Any],
                          adversarial: Dict[str, Any],
                          meta: Dict[str, Any]) -> Dict[str, Any]:
    """Ship the three layers plus declared pass flags — nothing composed."""
    return {
        "schema": "prometheus.fidelity_report.v1",
        "layers": {
            "statistical": statistical,
            "behavioral": behavioral,
            "adversarial": adversarial,
        },
        "declared_thresholds": {
            "statistical_pass": STATISTICAL_PASS,
            "adversarial_critique_band": ADVERSARIAL_PASS["critic_auc_band"],
        },
        "all_statistical_flags_passed":
            bool(all(statistical["pass_flags"].values())),
        "meta": meta,
    }
