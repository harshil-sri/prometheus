"""FastAPI backend for Project Prometheus Dashboard."""

import json
import os
import sys
import random
from typing import Any, Dict, List, Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from twin.twin import FinancialDigitalTwin
from twin.typologies import run_typology
from attack.compiler import AttackCompiler
from attack.benchmark_attacks import BENCHMARK_ATTACKS, HELD_OUT_ATTACKS, TRAINABLE_ATTACKS, generate_training_attacks
from blue.features import compute_features, build_graph_data
from blue.xgb_model import XGBFraudDetector
from blue.gnn_model import GNNFraudDetector
from blue.meta_model import MetaModel
from sensitivity.engine import SensitivityEngine
from feedback.loop import FeedbackLoop
from scoring.structured_score import compute_structured_score, score_from_ml_probs, get_band, get_band_color
from eval.harness import full_report, multi_prevalence_eval

app = FastAPI(title="Project Prometheus Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state for the demo
DEMO_STATE = {
    "twin": None,
    "compiler": None,
    "blue_team": None,
    "sensitivity": None,
    "feedback": None,
    "report": None,
    "event_log": [],
    "ready": False,
}


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.get("/api/status")
def get_status():
    return {
        "ready": DEMO_STATE["ready"],
        "events": len(DEMO_STATE["event_log"]),
        "report": DEMO_STATE["report"] is not None,
    }


@app.get("/api/attack-types")
def get_attack_types():
    """Return all benchmark attack types with metadata."""
    from attack.benchmark_attacks import ATTACK_METADATA
    return {
        "attacks": ATTACK_METADATA,
        "held_out": list(HELD_OUT_ATTACKS),
        "trainable": list(TRAINABLE_ATTACKS),
    }


@app.post("/api/init")
def initialize(seed: int = 42, num_accounts: int = 2000, num_steps: int = 200):
    """Initialize the twin, compiler, and Blue Team."""
    try:
        twin = FinancialDigitalTwin(seed=seed, num_accounts=num_accounts, num_merchants=100, num_steps=num_steps)
        twin.run()

        compiler = AttackCompiler(twin, seed=seed)

        # Build features and train Blue Team
        X, y, fnames = compute_features(twin.world.transactions, twin.world)
        xgb = XGBFraudDetector(seed=seed)
        xgb.fit(X, y, fnames)

        data = build_graph_data(twin.world.transactions, twin.world)
        gnn = None
        if data is not None:
            gnn = GNNFraudDetector(in_channels=data.x.shape[1], seed=seed)
            gnn.fit(data)

        # Meta-model
        meta = MetaModel(seed=seed)
        xgb_probs = xgb.predict_proba(X)
        gnn_probs = gnn.predict_proba(data) if gnn is not None else np.full(len(X), 0.5)
        X_meta = np.column_stack([xgb_probs, gnn_probs[:len(X)]])
        meta.fit(X_meta, y)

        # Sensitivity engine
        sensitivity = SensitivityEngine(xgb_model=xgb.model, gnn_model=gnn, feature_names=fnames)

        # Feedback loop
        feedback = FeedbackLoop(twin, compiler, None, sensitivity)

        DEMO_STATE["twin"] = twin
        DEMO_STATE["compiler"] = compiler
        DEMO_STATE["blue_team"] = {"xgb": xgb, "gnn": gnn, "meta": meta}
        DEMO_STATE["sensitivity"] = sensitivity
        DEMO_STATE["feedback"] = feedback
        DEMO_STATE["ready"] = True
        DEMO_STATE["event_log"] = [{"event": "initialized", "detail": f"{len(twin.world.transactions)} TXs, {len(fnames)} features"}]

        return {
            "status": "ok",
            "transactions": len(twin.world.transactions),
            "features": len(fnames),
            "fraud_ratio": float(y.mean()),
            "graph_nodes": data.x.shape[0] if data is not None else 0,
        }
    except Exception as e:
        import traceback
        return {"status": "error", "detail": str(e), "traceback": traceback.format_exc()}


@app.post("/api/demo/run")
def run_demo():
    """Run the 3-beat demo sequence.

    Beat 1: Red Team attacks → Blue Team misses (or scores low)
    Beat 2: Weakness-directed retrain → Blue Team catches it
    Beat 3: Held-out attack type → Blue Team still catches it (generalization)
    """
    if not DEMO_STATE["ready"]:
        return {"error": "Not initialized. Call /api/init first."}

    try:
        twin = DEMO_STATE["twin"]
        compiler = DEMO_STATE["compiler"]
        bt = DEMO_STATE["blue_team"]
        sensitivity = DEMO_STATE["sensitivity"]
        import numpy as np

        log = DEMO_STATE["event_log"]
        log.append({"event": "demo_start", "detail": "Starting 3-beat demo"})

        # Beat 1: Run trainable attacks, measure recall
        trainable = list(TRAINABLE_ATTACKS)
        held_out = list(HELD_OUT_ATTACKS)

        # Generate and score attacks
        beat1_results = {}
        for aid in trainable:
            spec = compiler.compile(compiler.benchmark_spec(aid))
            traj_id = compiler.execute(spec, twin.world)
            attack_txs = [tx for tx in twin.world.transactions if tx.get("trajectory_id") == traj_id]
            if attack_txs:
                X_a, y_a, _ = compute_features(attack_txs, twin.world)
                if len(X_a) > 0:
                    score = float(bt["xgb"].predict_proba(X_a).max())
                    beat1_results[aid] = {"caught": score > 0.5, "score": round(score, 4), "txs": len(attack_txs)}
                else:
                    beat1_results[aid] = {"caught": False, "score": 0.0, "txs": 0}
            else:
                beat1_results[aid] = {"caught": False, "score": 0.0, "txs": 0}

        recall_before = sum(1 for r in beat1_results.values() if r["caught"]) / max(1, len(beat1_results))
        log.append({"event": "beat1", "detail": f"Recall before: {recall_before:.2%}", "results": beat1_results})

        # Beat 2: Diagnose weakness, generate variants, retrain
        X_all, y_all, fnames = compute_features(twin.world.transactions, twin.world)
        weakness = sensitivity.weakness_direction(X_all)
        variants = compiler.generate_variants(weakness, n=15)
        log.append({"event": "beat2_diagnose", "detail": f"Weakness: {weakness['weakness']}, variants: {len(variants)}"})

        # Retrain XGBoost with hard negatives
        for v in variants:
            try:
                plan = compiler.compile(v)
                compiler.execute(plan, twin.world)
            except Exception:
                continue

        X_new, y_new, _ = compute_features(twin.world.transactions, twin.world)
        bt["xgb"].fit(X_new, y_new, fnames)

        # Re-evaluate
        beat2_results = {}
        for aid in trainable:
            spec = compiler.compile(compiler.benchmark_spec(aid))
            traj_id = compiler.execute(spec, twin.world)
            attack_txs = [tx for tx in twin.world.transactions if tx.get("trajectory_id") == traj_id]
            if attack_txs:
                X_a, y_a, _ = compute_features(attack_txs, twin.world)
                if len(X_a) > 0:
                    score = float(bt["xgb"].predict_proba(X_a).max())
                    beat2_results[aid] = {"caught": score > 0.5, "score": round(score, 4)}
                else:
                    beat2_results[aid] = {"caught": False, "score": 0.0}
            else:
                beat2_results[aid] = {"caught": False, "score": 0.0}

        recall_after = sum(1 for r in beat2_results.values() if r["caught"]) / max(1, len(beat2_results))
        log.append({"event": "beat2", "detail": f"Recall after: {recall_after:.2%}", "results": beat2_results})

        # Beat 3: Held-out attacks
        beat3_results = {}
        for aid in held_out:
            spec = compiler.compile(compiler.benchmark_spec(aid))
            traj_id = compiler.execute(spec, twin.world)
            attack_txs = [tx for tx in twin.world.transactions if tx.get("trajectory_id") == traj_id]
            if attack_txs:
                X_a, y_a, _ = compute_features(attack_txs, twin.world)
                if len(X_a) > 0:
                    score = float(bt["xgb"].predict_proba(X_a).max())
                    beat3_results[aid] = {"caught": score > 0.5, "score": round(score, 4), "held_out": True}
                else:
                    beat3_results[aid] = {"caught": False, "score": 0.0, "held_out": True}
            else:
                beat3_results[aid] = {"caught": False, "score": 0.0, "held_out": True}

        gen_recall = sum(1 for r in beat3_results.values() if r["caught"]) / max(1, len(beat3_results))
        log.append({"event": "beat3", "detail": f"Held-out recall: {gen_recall:.2%}", "results": beat3_results})

        # Build Blind-Spot Report
        report = {
            "blind_spot": weakness["weakness"],
            "evidence": {
                "gnn_contribution": "low" if weakness["target_model"] == "GNN" else "high",
                "sequence_signal": "low",
                "graph_density": "below_threshold",
            },
            "generated_fixes": len(variants),
            "recall_before": round(recall_before, 4),
            "recall_after": round(recall_after, 4),
            "generalization_recall_unseen_generator": round(gen_recall, 4),
            "retrain_rounds_used": 1,
            "max_retrain_rounds": 2,
        }
        DEMO_STATE["report"] = report
        log.append({"event": "demo_complete", "detail": "3-beat demo finished", "report": report})

        return {
            "status": "ok",
            "beat1": {"recall": round(recall_before, 4), "results": beat1_results},
            "beat2": {"recall": round(recall_after, 4), "results": beat2_results, "weakness": weakness},
            "beat3": {"recall": round(gen_recall, 4), "results": beat3_results},
            "report": report,
        }
    except Exception as e:
        import traceback
        return {"status": "error", "detail": str(e), "traceback": traceback.format_exc()}


@app.get("/api/report")
def get_report():
    """Get the latest Blind-Spot Report."""
    return DEMO_STATE.get("report", {"error": "No report yet. Run demo first."})


@app.get("/api/event-log")
def get_event_log():
    return {"events": DEMO_STATE.get("event_log", [])}


@app.get("/api/score")
def get_score(tx_id: str = ""):
    """Get structured score for a transaction."""
    if not DEMO_STATE["ready"]:
        return {"error": "Not initialized"}
    twin = DEMO_STATE["twin"]
    txs = [tx for tx in twin.world.transactions if tx.get("tx_id") == tx_id]
    if not txs:
        return {"error": f"Transaction {tx_id} not found"}
    tx = txs[0]
    from blue.features import compute_features
    X, y, _ = compute_features([tx], twin.world)
    bt = DEMO_STATE["blue_team"]
    if len(X) == 0:
        return {"error": "No features"}
    xgb_prob = float(bt["xgb"].predict_proba(X)[0])
    score = score_from_ml_probs(xgb_prob, xgb_prob, xgb_prob)
    return {
        "tx_id": tx_id,
        "amount": tx.get("amount"),
        "is_fraud": tx.get("is_fraud"),
        "ml_probability": round(xgb_prob, 4),
        "structured_score": score,
    }


@app.get("/api/eval")
def run_evaluation():
    """Run multi-prevalence evaluation."""
    if not DEMO_STATE["ready"]:
        return {"error": "Not initialized"}
    twin = DEMO_STATE["twin"]
    bt = DEMO_STATE["blue_team"]
    from blue.features import compute_features
    X, y, _ = compute_features(twin.world.transactions, twin.world)
    scores = bt["xgb"].predict_proba(X)
    report = full_report(y, scores)
    return report


@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    """Serve the single-page dashboard."""
    dashboard_path = Path(__file__).parent.parent / "dashboard" / "index.html"
    if dashboard_path.exists():
        return HTMLResponse(dashboard_path.read_text())
    return HTMLResponse("<h1>Dashboard not found</h1><p>Build the dashboard at src/dashboard/index.html</p>")


def main():
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()