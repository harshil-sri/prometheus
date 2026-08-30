"""
sweep_eval.py — seed × scale robustness sweep on the honest baseline.

Runs baseline_eval.evaluate() over a matrix of seeds and (accounts,
merchants, steps) configs, enforcing the fail-loud generation check per
config, and aggregates:

  * fixed-prevalence (5%) meta PR-AUC headline — mean/min/max and 95% CI
    across seeds for each scale, plus the overall meta and XGB-only PR-AUC;
  * per-attack-type recall slate (recall above the legit p95, fraud-only)
    with the n that backs each estimate;
  * eval-population caveats (rows / fraud / prevalence / step window) so a
    config's headline is never quoted without its sample size.

Writes artifacts/sweep_eval.json (schema prometheus.sweep_eval.v1).

Usage (repo root):
    python scripts/sweep_eval.py
    python scripts/sweep_eval.py --seeds 42 43 44 45 \\
        --scale 600-75-70 1200-150-70 1200-150-140 --eval-repeats 5

Exit code 0 when every config passed the generation gate; 2 when any
config hit a generation shortfall (fail-loud; the failed configs are still
recorded in the artifact with their error).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
for _p in (ROOT, SRC, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import baseline_eval  # noqa: E402
from baseline_eval import GenerationShortfallError, evaluate  # noqa: E402
from _ensure_utf8_stdout import ensure_utf8_stdout  # noqa: E402
ensure_utf8_stdout()

SCALE_KEY_SEP = "x"
DEFAULTS = {
    "seeds": [42, 43, 44, 45],
    "scales": [(600, 75, 70), (1200, 150, 70), (1200, 150, 140)],
}


def _fmt_or_invalid(value, fmt: str) -> str:
    """Format a numeric as `fmt`; return 'INVALID' when None (all-configs shortfall).

    Sweep's summary block can be reached with an empty `mean`/`ci95` when every
    seed×scale config hits a generation shortfall (fail-loud). Formatting `None`
    via f"{None:.4f}" raises TypeError. This helper keeps the honest-FAIL
    summary readable instead of crashing the runner.
    """
    if value is None:
        return "INVALID"
    try:
        return format(value, fmt)
    except (TypeError, ValueError):
        return "INVALID"


def parse_scale(token: str) -> Tuple[int, int, int]:
    parts = [int(p) for p in token.split(SCALE_KEY_SEP)]
    if len(parts) != 3:
        raise SystemExit(f"invalid scale token '{token}' (want A{SCALE_KEY_SEP}M{SCALE_KEY_SEP}S)")
    return tuple(parts)  # type: ignore[return-value]


def scale_key(scale: Tuple[int, int, int]) -> str:
    return SCALE_KEY_SEP.join(str(s) for s in scale)


def ci95(values: List[float]) -> Dict:
    """Mean / std / min / max + normal-approx 95% CI (n is small)."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "std": None, "min": None,
                "max": None, "ci95": None, "values": []}
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / max(1, n - 1)
    std = var ** 0.5
    half = (1.96 * std / (n ** 0.5)) if std > 0 else 0.0
    return {
        "n": n,
        "mean": round(mean, 4),
        "std": round(std, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "ci95": [round(mean - half, 4), round(mean + half, 4)],
        "values": [round(v, 4) for v in values],
    }


def aggregate_scale(seed_results: Dict[int, Dict]):
    """Collapse per-seed artifacts for one scale into a summary block."""
    val_meta_05 = []
    val_meta_overall = []
    val_xgb_overall = []
    per_type: Dict[str, Dict] = {}
    faults: List[str] = []
    low_pop: List[str] = []
    for seed, res in sorted(seed_results.items()):
        if res.get("error"):
            faults.append(f"seed={seed}: {res['error']}")
            continue
        a = res["artifact"]
        hl = a.get("headline", {}) or {}
        val_meta_05.append(hl.get("pr_auc"))
        val_meta_overall.append(hl.get("overall_meta_pr_auc"))
        val_xgb_overall.append(hl.get("overall_xgb_pr_auc"))
        for aid, st in (a.get("per_attack_type") or {}).items():
            slot = per_type.setdefault(aid, {"recalls": [], "n_frauds": [], "n_txs": []})
            slot["recalls"].append(st["recall_above_legit_p95"])
            slot["n_frauds"].append(st["n_fraud_txs"])
            slot["n_txs"].append(st["n_txs"])
        gp = a.get("eval_population", {}) or {}
        if (gp.get("n_eval_fraud") or 0) < 5 * (a.get("config") or {}).get("eval_repeats", 1):
            low_pop.append(
                f"seed={seed}: n_eval_fraud={gp.get('n_eval_fraud')} rows={gp.get('n_eval_rows')} "
                f"prevalence={gp.get('eval_fraud_prevalence')} window={gp.get('step_window')}"
            )
        for w in (a.get("generation_warnings") or []):
            low_pop.append(f"seed={seed}: {w}")

    per_type_summary: Dict[str, Dict] = {}
    for aid, slot in per_type.items():
        per_type_summary[aid] = {
            "recall_above_legit_p95": ci95([r for r in slot["recalls"] if r is not None]),
            "n_fraud_min": min(slot["n_frauds"]) if slot["n_frauds"] else None,
            "n_fraud_max": max(slot["n_frauds"]) if slot["n_frauds"] else None,
            "n_txs_min": min(slot["n_txs"]) if slot["n_txs"] else None,
        }

    return {
        "seeds_run": sorted(seed_results.keys()),
        "seeds_ok": len(seed_results) - len(faults),
        "all_configs_valid": not faults and len(seed_results) > 0,
        "generation_failures": faults,
        "population_caveats": low_pop,
        "headline_pr_auc_05": ci95([v for v in val_meta_05 if v is not None]),
        "overall_meta_pr_auc": ci95([v for v in val_meta_overall if v is not None]),
        "overall_xgb_pr_auc": ci95([v for v in val_xgb_overall if v is not None]),
        "per_attack_type": per_type_summary,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Seed × scale robustness sweep over the honest baseline")
    ap.add_argument("--seeds", type=int, nargs="+", default=DEFAULTS["seeds"])
    ap.add_argument("--scale", nargs="+", default=None,
                    help=f"configs as A{SCALE_KEY_SEP}M{SCALE_KEY_SEP}S; default built-ins")
    ap.add_argument("--eval-repeats", type=int, default=5)
    ap.add_argument("--min-eval-fraud-per-type", type=int, default=5)
    ap.add_argument("--boundary-fraction", type=float, default=0.72)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--gnn-epochs", type=int, default=30)
    ap.add_argument("--funding-safety", type=float, default=1.25,
                    help="SAFETY multiplier passed to baseline_eval.evaluate(); "
                         "larger = more disjoint funding headroom per attack "
                         "type; if a config still trips the generation gate, "
                         "raise this OR --replenish-repeats before scaling "
                         "down the eval slice.")
    ap.add_argument("--replenish-repeats", action="store_true",
                    help="Force a twin salary step between eval_repeats "
                         "iterations to refill drained accounts. Off by "
                         "default; the ring-fence plus disjoint per-type "
                         "pools is the primary defense.")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-config console output")
    args = ap.parse_args()

    scales = [scale for scale in DEFAULTS["scales"]]
    if args.scale:
        scales = [parse_scale(t) for t in args.scale]

    t_start = time.perf_counter()
    per_config: Dict[str, Dict] = {}
    exit_code = 0

    for seed in args.seeds:
        for scale in scales:
            key = f"seed={seed}|{scale_key(scale)}"
            res: Dict = {"seed": seed, "scale": scale_key(scale), "error": None,
                         "artifact": None, "log": []}
            try:
                artifact, log = evaluate(
                    seed=seed, accounts=scale[0], merchants=scale[1],
                    steps=scale[2], boundary_fraction=args.boundary_fraction,
                    folds=args.folds, gnn_epochs=args.gnn_epochs,
                    eval_repeats=args.eval_repeats,
                    min_eval_fraud_per_type=args.min_eval_fraud_per_type,
                    funding_safety=args.funding_safety,
                    replenish_repeats=args.replenish_repeats,
                )
                res["artifact"] = artifact
                res["log"] = log
                if not args.quiet:
                    for line in log:
                        print(f"[{key}] {line}")
            except GenerationShortfallError as exc:
                res["error"] = str(exc)
                exit_code = 2
                print(f"[{key}] GENERATION SHORTFALL (fail-loud): {exc}",
                      file=sys.stderr)
            except Exception as exc:  # keep the sweep going, flag the config
                res["error"] = f"unexpected: {type(exc).__name__}: {exc}"
                exit_code = 2
                print(f"[{key}] ERROR: {res['error']}", file=sys.stderr)
            per_config[key] = res

    by_scale: Dict[str, Dict[int, Dict]] = {}
    for key, res in per_config.items():
        by_scale.setdefault(res["scale"], {})[res["seed"]] = res

    aggregate = {sk: aggregate_scale(seed_map)
                 for sk, seed_map in sorted(by_scale.items())}

    artifact = {
        "schema": "prometheus.sweep_eval.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "runtime_seconds": round(time.perf_counter() - t_start, 2),
        "config": {
            "seeds": args.seeds,
            "scales": [scale_key(s) for s in scales],
            "eval_repeats": args.eval_repeats,
            "min_eval_fraud_per_type": args.min_eval_fraud_per_type,
            "boundary_fraction": args.boundary_fraction,
            "folds": args.folds,
            "gnn_epochs": args.gnn_epochs,
            "funding_safety": args.funding_safety,
            "replenish_repeats": args.replenish_repeats,
        },
        "headline_metric": "meta PR-AUC @ 5% prevalence (fixed prevalence)",
        "headline_note": "per-scale CI across seeds; eval-population caveats "
                         "recorded per config so weak configs are never "
                         "quoted without their sample size",
        "per_config": {k: {kk: v for kk, v in r.items() if kk != "log"}
                       for k, r in per_config.items()},
        "aggregate_by_scale": aggregate,
    }
    # Surface per-config funding diagnostics so a downstream reader can verify
    # the ring-fence actually executed per seed×scale (not just at one config).
    # Path: per_config[key]["artifact"]["funding"] is the baseline_eval block;
    # stash it under funding_per_config for cheap top-level access.
    funding_per_config: Dict[str, Any] = {}
    for k, r in per_config.items():
        art = r.get("artifact")
        if isinstance(art, dict) and isinstance(art.get("funding"), dict):
            funding_per_config[k] = {
                "exec_order": art["funding"].get("exec_order"),
                "safety": art["funding"].get("safety"),
                "reserved_pools": art["funding"].get("reserved_pools"),
            }
    if funding_per_config:
        artifact["funding_per_config"] = funding_per_config

    out_dir = os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "sweep_eval.json")
    with open(out_path, "w") as f:
        json.dump(artifact, f, indent=2, default=str)

    print("\n[sweep] =================== SEED × SCALE SWEEP ===================")
    for sk, agg in sorted(aggregate.items()):
        ok = "OK" if agg["all_configs_valid"] else "INVALID (shortfall)"
        print(f"[sweep] scale={sk:<15} seeds={len(agg['seeds_run'])} status={ok}")
        ci95 = agg["headline_pr_auc_05"]["ci95"] or []
        ci95_str = (
            "[" + ", ".join(_fmt_or_invalid(v, ".4f") for v in ci95) + "]"
            if ci95 else "-"
        )
        print(f"        headline 5% PR-AUC: mean={_fmt_or_invalid(agg['headline_pr_auc_05']['mean'], '.4f')} "
              f"CI95={ci95_str}")
        print(f"        overall meta PR-AUC: {_fmt_or_invalid(agg['overall_meta_pr_auc']['mean'], '.4f')} "
              f"| XGB-only: {_fmt_or_invalid(agg['overall_xgb_pr_auc']['mean'], '.4f')}")
        slate = "  ".join(
            f"{aid}:{_fmt_or_invalid(st['recall_above_legit_p95']['mean'], '.2f')}"
            f"(n≥{st['n_fraud_min']})"
            for aid, st in sorted(agg["per_attack_type"].items())
        )
        print(f"        per-type recall>p95: {slate}")
        for f in agg["generation_failures"]:
            print(f"        FAIL: {f}")
        for c in agg["population_caveats"]:
            print(f"        caveat: {c}")
    print(f"[sweep] artifact written : {out_path}")
    print(f"[sweep] exit code        : {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())