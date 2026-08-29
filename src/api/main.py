"""FastAPI backend for Project Prometheus Dashboard."""

import json
import os
import sys
import random
import asyncio
from typing import Any, Dict, List, Optional
from pathlib import Path

import numpy as np

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging

logger = logging.getLogger(__name__)

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from twin.twin import FinancialDigitalTwin
from twin.typologies import run_typology
from attack.compiler import AttackCompiler
from attack.benchmark_attacks import BENCHMARK_ATTACKS, HELD_OUT_ATTACKS, TRAINABLE_ATTACKS, generate_training_attacks
from blue.ensemble import BlueTeamEnsemble
from sensitivity.engine import SensitivityEngine
from feedback.loop import FeedbackLoop
from scoring.structured_score import (
    compute_structured_score, score_from_ml_probs, get_band, get_band_color,
    FittedStructuredScore, DEFAULT_WEIGHTS_PATH,
)
from eval.harness import full_report, multi_prevalence_eval
from api.graph import build_knowledge_graph, list_trajectories_summary, extract_node_profile
from investigate.case_manager import CaseManager

from twin.agentic import AgenticCommerce
from policy.pcat import PCATPolicy
from eval.judges import judge_rc as _judge_rc

# Canonical weights path (updates.md 2.2 reconciliation): the fitted weights
# live at src/artifacts/structured_weights.json — one constant, both scorers.
STRUCTURED_WEIGHTS_PATH = DEFAULT_WEIGHTS_PATH

# Phase 7: persisted-cycle artifacts live under <project>/artifacts alongside
# the other evaluation exhibits.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
ARTIFACT_DIR = os.path.join(PROJECT_ROOT, "artifacts")
TIMELINE_PATH = os.path.join(ARTIFACT_DIR, "feedback_timeline.json")

app = FastAPI(title="Project Prometheus Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state for the demo
from api.events import EventHub

EVENTS = EventHub()
DEMO_STATE = {
    "twin": None,
    "compiler": None,
    "blue_team": None,
    "sensitivity": None,
    "feedback": None,
    "report": None,
    "structured": None,
    "case_manager": None,
    "event_log": [],
    "agentic": None,
    "ready": False,
}


def _ensure_agentic() -> Dict[str, Any]:
    """Lazy-build the agentic-commerce demo sandbox (its OWN world) so the
    twin's init dataset and the 164-test suite are never perturbed."""
    if DEMO_STATE.get("agentic") is None:
        from twin.core import WorldState
        sandbox = AgenticCommerce(WorldState(seed=2026), seed=2026)
        a1 = sandbox.new_agent(budget=250_000, identity="DEMO-OWNER-1")
        sandbox.register_merchant(domain="stablerider.example", category="retail",
                                  payout_account="WALLET_LEGIT_MAIN",
                                  owner_identity="DEMO-OWNER-1")
        a2 = sandbox.new_agent(budget=120_000, identity="DEMO-OWNER-2")
        sandbox.register_merchant(domain="grocery.example", category="grocery",
                                  payout_account="WALLET_LEGIT_GROCERY",
                                  owner_identity="DEMO-OWNER-2")
        pcat = PCATPolicy.for_agentic(sandbox)
        DEMO_STATE["agentic"] = {"sandbox": sandbox, "pcat": pcat}
    return DEMO_STATE["agentic"]


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_dummy():
    """Dummy endpoint to suppress Chrome DevTools 404 warnings in the logs."""
    return JSONResponse(content={})

@app.get("/api/status")
def get_status():
    return {
        "ready": DEMO_STATE["ready"],
        "events": len(DEMO_STATE["event_log"]),
        "report": DEMO_STATE["report"] is not None,
    }


class AgenticCheckoutRequest(BaseModel):
    merchant_id: str
    amount: float
    agent_id: str = ""
    caller_identity: Optional[str] = None
    defense: str = "pcat"                     # "pcat" (default) | "naive"
    rc_class: Optional[str] = None            # RC-1..RC-5 tag for judging
    n_authorizations: int = 1
    leak_credential: bool = False
    attacker_controlled_payout: Optional[str] = None


def _judge_live(res: Dict[str, Any], req: "AgenticCheckoutRequest",
                sandbox: AgenticCommerce) -> Optional[bool]:
    """Reuse the REAL deterministic judges on the live decision."""
    if not req.rc_class:
        return None
    budget = 0.0
    agent = sandbox.agents.get(res.get("agent_id") or req.agent_id)
    if agent:
        cred = sandbox.credentials.get(agent.get("credential_id"))
        budget = cred.get("budget", 0.0) if cred else 0.0
    paid = sum(float(p["amount"]) for p in res.get("payments") or [])
    pack = {
        "rc_class": req.rc_class,
        "defense": req.defense,
        "allowed": bool(res.get("allowed")),
        "p_blocks": list(res.get("p_blocks") or []),
        "payments": list(res.get("payments") or []),
        "paid_total": round(paid, 2),
        "attacker_received": round(sum(
            float(p["amount"]) for p in res.get("payments") or []
            if p.get("to") == req.attacker_controlled_payout), 2),
        "agent_budget": round(budget, 2),
        "credential_leaked": bool(res.get("credential_leaked", False)),
        "caller_registered": bool(
            req.caller_identity in sandbox.authz_table),
        "over_spent": bool(paid > budget),
        "payout": res.get("payout"),
        "attacker_payout": req.attacker_controlled_payout or "",
    }
    return bool(_judge_rc(req.rc_class, pack))


@app.post("/api/agentic/checkout")
def agentic_checkout(req: AgenticCheckoutRequest):
    """Agentic-commerce checkout with LIVE PCAT enforcement (or a naive
    before-state for the structural demo). Returns the REAL signed decision +
    the official RC judge verdict — never fabricated."""
    try:
        st = _ensure_agentic()
        sandbox: AgenticCommerce = st["sandbox"]
        defense = st["pcat"] if req.defense == "pcat" else None
        agent_id = req.agent_id or next(iter(sandbox.agents))
        res = sandbox.checkout(
            agent_id=agent_id,
            merchant_id=req.merchant_id,
            amount=req.amount,
            defense=defense,
            caller_identity=req.caller_identity,
            rc_class=req.rc_class,
            n_authorizations=req.n_authorizations,
            leak_credential=req.leak_credential,
            attacker_controlled_payout=req.attacker_controlled_payout,
        )
        judged = _judge_live(res, req, sandbox)
        return {
            "status": "ok",
            "defense": req.defense,
            "decision": res,
            "judged_attack_success": judged,
            "defense_note": ("gate enforced" if defense is not None else
                             "naive/unhardened — structural gaps exposed"),
        }
    except Exception as e:
        logger.exception("agentic checkout failed")
        return {"status": "error", "detail": str(e)[:300],
                "error_code": "AGENTIC_CHECKOUT_FAILED"}


@app.get("/api/agentic/status")
def agentic_status():
    """Aggregate view of the agentic-commerce sandbox + audit events."""
    st = _ensure_agentic()
    sandbox = st["sandbox"]
    leaked = sorted(c for c in sandbox.credentials
                    if sandbox.is_credential_observed(c))
    return {
        "ready": True,
        "agents": len(sandbox.agents),
        "credentials": len(sandbox.credentials),
        "merchants": sorted(sandbox.registry),
        "certified_payouts": sorted(
            st["pcat"].resolved_certified().keys()),
        "leaked_credentials": leaked,
        "session_log_lines": len(sandbox.session_log),
        "world_transactions": len(sandbox.world.transactions),
        "events": sandbox.events[-25:],
    }


@app.get("/api/protocol")
def protocol_eval_status():
    """Serve the REAL persisted T9 before/after artifact written by
    scripts/protocol_eval.py to <project>/artifacts — never fabricated.
    Honest fallback when the artifact is missing (same pattern as /api/ood)."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    path = os.path.join(project_root, "artifacts", "protocol_eval.json")
    if not os.path.exists(path):
        return {"present": False,
                "note": "protocol_eval.json not generated yet — run "
                        "scripts/protocol_eval.py."}
    try:
        d = json.load(open(path))
    except Exception as exc:                     # noqa: BLE001
        return {"present": False,
                "note": f"protocol_eval.json unreadable: {exc}"}
    return {
        "present": True,
        "schema": d.get("schema"),
        "generated_at": d.get("generated_at"),
        "per_rc": d.get("per_rc", {}),
        "benign_fp_probe": d.get("benign_fp_probe", {}),
        "holdout_fingerprint": (d.get("holdout") or {}).get("fingerprint", ""),
        "fingerprint_intact": (d.get("holdout") or {}).get("fingerprint_intact"),
        "agentic_payments_logged": d.get("agentic_payments_logged"),
        "attacker_wallet_total_before": d.get("attacker_wallet_total_before"),
        "citations": d.get("citations", []),
    }


@app.get("/api/ood")
def get_ood_matrix():
    """Mechanism × attack-type OOD detection matrix for the heatmap panel.

    Serves the REAL persisted artifact written by scripts/mechanism_eval.py to
    <project>/artifacts — never fabricated. Honest fallback when the artifact
    is missing."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    path = os.path.join(project_root, "artifacts", "ood_matrix.json")
    if not os.path.exists(path):
        return {"present": False,
                "note": "OOD matrix not generated yet — run "
                        "scripts/mechanism_eval.py."}
    try:
        d = json.load(open(path))
    except Exception as exc:                     # noqa: BLE001
        return {"present": False,
                "note": f"OOD matrix unreadable: {exc}"}
    return {
        "present": True,
        "rates": d.get("rates", {}),
        "types": (d.get("config") or {}).get("types", []),
        "mechanisms": (d.get("config") or {}).get("mechanisms", []),
        "fingerprint": d.get("fingerprint", ""),
        "holdout_fingerprint": d.get("holdout_fingerprint", ""),
    }


@app.get("/api/attack-types")
def get_attack_types():
    """Return all benchmark attack types with metadata (LIST not dict —
    finding #9 fix: dashboard forEach on object was silently dead)."""
    from attack.benchmark_attacks import ATTACK_METADATA
    attacks = [{"id": k, **v} for k, v in sorted(ATTACK_METADATA.items())]
    return {
        "attacks": attacks,
        "held_out": list(HELD_OUT_ATTACKS),
        "trainable": list(TRAINABLE_ATTACKS),
    }


class InitRequest(BaseModel):
    seed: int = 42
    num_accounts: int = 500
    num_steps: int = 100


@app.post("/api/init")
def initialize(req: InitRequest = None,
                seed: int = 42, num_accounts: int = 500,
                num_steps: int = 100):
    """Initialize the twin, compiler, and Blue Team.

    Accepts EITHER a JSON body (dashboard) or query params (TestClient).
    Tracebacks NEVER leak to clients (finding #12 fix)."""
    if req is not None:
        seed = req.seed
        num_accounts = req.num_accounts
        num_steps = req.num_steps
        
    random.seed(seed)
    np.random.seed(seed)
    
    try:
        twin = FinancialDigitalTwin(seed=seed, num_accounts=num_accounts, num_merchants=100, num_steps=num_steps)
        twin.run()

        compiler = AttackCompiler(twin, seed=seed)
        generate_training_attacks(compiler, twin.world)

        # Blue team as ONE ensemble object (finding #1: the loop previously
        # received None because this construction didn't exist).
        blue = BlueTeamEnsemble.untrained(seed=seed)
        fit_diag = blue.fit_transactions(list(twin.world.transactions),
                                         twin.world)

        # Fitted structured 0-1000 score (P8): logistic head over the six
        # ensemble signal columns; fitted here in-sample (declared) and
        # persisted so /api/score uses real per-column contributions instead
        # of the old faked-identical-inputs shortcut (finding #6).
        struct: FittedStructuredScore | None
        try:
            from blue.features import compute_features as _cf
            struct = FittedStructuredScore()
            X_all6, _y_all, _n = _cf(list(twin.world.transactions),
                                     twin.world)
            sig_cols = blue.score_all_signals(list(twin.world.transactions),
                                              twin.world, manifold=None)
            X_head = np.column_stack(
                [np.asarray(sig_cols[c], dtype=np.float64)
                 for c in struct.columns])
            struct.fit(X_head, [1.0 if t.get("is_fraud") else 0.0
                                for t in twin.world.transactions])
            struct.fit_meta["source"] = "fitted_in_sample"
            struct.save(STRUCTURED_WEIGHTS_PATH)
            # Session inits refit the logistic with a RANDOM seed (dashboard
            # quota 2000/200) — keep the committed fitted w_* by re-loading
            # the saved artifact (save() merge-preserves w_formula) onto this
            # object so /api/score stays on the canonical formula weights.
            try:
                merged = FittedStructuredScore.load(STRUCTURED_WEIGHTS_PATH)
                if merged is None:
                    raise FileNotFoundError(STRUCTURED_WEIGHTS_PATH)
                # keep this object's freshly fitted logistic coefficients
                merged.coef_ = struct.coef_
                merged.intercept_ = struct.intercept_
                merged.fit_meta = struct.fit_meta
                struct = merged
            except Exception as exc:                       # noqa: BLE001
                logger.warning("weights merge failed: %s", exc)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("structured score fitting failed: %s", exc)
            struct = None

        # Sensitivity engine (computed surface, finding #5b keys)
        sensitivity = SensitivityEngine(xgb_model=blue.xgb.model,
                                        gnn_model=blue.gnn.model if blue.gnn else None,
                                        feature_names=blue.feature_names)

        # Feedback loop with a real blue team
        feedback = FeedbackLoop(twin, compiler, blue, sensitivity, seed=seed)

        # Investigator CaseManager (P8)
        case_mgr = CaseManager(
            ensemble=blue,
            twin=twin,
            sensitivity=sensitivity,
            structured=struct,
            seed=seed,
        )

        sample_tx_id = next((t["tx_id"] for t in reversed(twin.world.transactions) if t.get("is_fraud")),
                            (twin.world.transactions[0]["tx_id"] if twin.world.transactions else ""))

        DEMO_STATE["twin"] = twin
        DEMO_STATE["compiler"] = compiler
        DEMO_STATE["blue_team"] = blue
        DEMO_STATE["sensitivity"] = sensitivity
        DEMO_STATE["feedback"] = feedback
        DEMO_STATE["structured"] = struct
        DEMO_STATE["case_manager"] = case_mgr
        DEMO_STATE["ready"] = True
        DEMO_STATE["event_log"] = [{"event": "initialized", "detail": (
            f"{len(twin.world.transactions)} TXs, {len(blue.feature_names)} "
            f"features, calibration={fit_diag.get('calibration_method')}, "
            f"oof={fit_diag.get('oof_used')}")}]

        # Broadcast the new world to live stream subscribers (non-blocking;
        # /api/init runs on a threadpool thread).
        EVENTS.publish({
            "type": "init",
            "transactions": len(twin.world.transactions),
            "features": len(blue.feature_names),
        })

        return {
            "status": "ok",
            "transactions": len(twin.world.transactions),
            "features": len(blue.feature_names),
            "fraud_ratio": float(sum(1 for t in twin.world.transactions
                                     if t.get("is_fraud"))
                                  / max(1, len(twin.world.transactions))),
            "graph_nodes": fit_diag.get("graph_nodes", 0),
            "sample_tx_id": sample_tx_id,
        }
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("init failed")
        return {"status": "error", "detail": str(e)[:300],
                "error_code": "INIT_FAILED"}








@app.post("/api/demo/run")
def run_demo():
    """Run the 3-beat demo through the REAL decontaminated FeedbackLoop.

    Beat 1   fresh trainable attacks vs current blue team
    Beat 2   weakness diagnose -> variants -> decontaminated retrain
             (eval trajectories excluded; two-axis leakage asserted)
             -> re-check on FRESH instances
    Beat 3   held-out types evaluated only (generalization)
    """
    if not DEMO_STATE["ready"]:
        return {"error": "Not initialized. Call /api/init first."}

    try:
        import numpy as np
        from blue.splits import lock_holdout

        feedback = DEMO_STATE["feedback"]
        log = DEMO_STATE["event_log"]
        log.append({"event": "demo_start",
                    "detail": "Starting 3-beat demo (decontaminated loop)"})

        trainable = sorted(TRAINABLE_ATTACKS)
        held_out = sorted(HELD_OUT_ATTACKS)

        import random
        # Lock the two-axis holdout for THIS session's training pools
        # Use a random seed so successive demo runs give dynamic results
        demo_seed = random.randint(1, 1000000)
        holdout = lock_holdout(seed=demo_seed, held_out_types=tuple(held_out))

        report = feedback.run_cycle(
            attack_ids=trainable,
            held_out_ids=held_out,
            holdout_spec=holdout,
            n_instances=2,
        )

        beat1_results = report.get("per_type_before", {})
        beat2_results = report.get("per_type_after", {})
        gen_recall = report.get("generalization_recall_unseen_generator")

        log.append({"event": "beat1", "detail":
                    f"Recall before: {report['recall_before']:.2%}"})
        log.append({"event": "beat2", "detail":
                    f"Round(s)={report['retrain_rounds_used']}, "
                    f"recall after: {report['recall_after']:.2%}, "
                    f"blind spot: {report['blind_spot']}"})
        log.append({"event": "beat3", "detail":
                    f"Held-out recall: {gen_recall}"})
        log.append({"event": "evidence_manifest",
                    "detail": [e["evidence_id"] + ":" + e["kind"]
                               for e in report.get("evidence_manifest", [])]})

        DEMO_STATE["report"] = {
            "blind_spot": report["blind_spot"],
            "recall_before": report["recall_before"],
            "recall_after": report["recall_after"],
            "improved": report["improved"],
            "generated_fixes": report["generated_fixes"],
            "retrain_rounds_used": report["retrain_rounds_used"],
            "max_retrain_rounds": report["max_retrain_rounds"],
            "generalization_recall_unseen_generator": gen_recall,
            "evidence_ids": report["evidence_ids"],
        }

        # Phase 7 blind-spot timeline: persist THIS cycle alongside the
        # committed baseline; a demo/run hiccup must never kill the demo.
        try:
            from feedback.timeline import FeedbackTimeline, summarize_cycle
            timeline = FeedbackTimeline(TIMELINE_PATH)
            timeline.append(summarize_cycle(report, seed=demo_seed,
                                            source="demo_session"))
        except Exception as exc:                          # noqa: BLE001
            logger.warning("timeline append failed: %s", exc)

        return {
            "status": "ok",
            "beat1": {"recall": report["recall_before"],
                      "results": beat1_results},
            "beat2": {"recall": report["recall_after"],
                      "results": beat2_results,
                      "blind_spot": report["blind_spot"]},
            "beat3": {"recall": gen_recall},
            "report": DEMO_STATE["report"],
        }
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("demo failed")
        return {"status": "error", "detail": str(e)[:300],
                "error_code": "DEMO_FAILED"}


@app.get("/api/report")
def get_report():
    """Get the latest Blind-Spot Report."""
    return DEMO_STATE.get("report", {"error": "No report yet. Run demo first."})


@app.get("/api/event-log")
def get_event_log():
    return {"events": DEMO_STATE.get("event_log", [])}


@app.get("/api/score")
def get_score(tx_id: str = ""):
    """Deep-path structured score from HONEST per-column signals (finding
    #6 fix: no more faking GNN/meta as XGB copies). When the CaseManager is
    live it derives the SAME evidence context as /api/investigate
    (signals + deterministic E/C), so the two endpoints cannot disagree."""
    if not DEMO_STATE["ready"]:
        return {"error": "Not initialized"}
    twin = DEMO_STATE["twin"]
    txs_all = [tx for tx in twin.world.transactions if tx.get("tx_id") == tx_id]
    if not txs_all:
        return {"error": f"Transaction {tx_id} not found"}
    tx = txs_all[0]
    bt: BlueTeamEnsemble = DEMO_STATE["blue_team"]
    struct: Optional[FittedStructuredScore] = DEMO_STATE["structured"]
    cm = DEMO_STATE.get("case_manager")

    signals = bt.score_all_signals([tx], twin.world, manifold=None)
    ctx = cm.case_evidence_context(signals, [tx]) if cm is not None else None
    sig_scalar = (ctx or {}).get("signals") or {
        k: float(v[0]) if len(v) else 0.0 for k, v in signals.items()}
    ext_ev = (ctx or {}).get("external_evidence", 0.0)
    camp_ev = (ctx or {}).get("campaign_evidence", 0.0)

    if struct is not None:
        deep = struct.predict_row(sig_scalar,
                                  external_evidence=ext_ev,
                                  campaign_evidence=camp_ev)
        weights_source = struct.fit_meta.get("source", "fitted")
        weights_formula = dict(struct.w_formula or {})
        weights_vs_baseline = struct.weights_report()["delta"] if struct.w_formula else {}
    else:
        prob = sig_scalar["meta"]
        legacy = score_from_ml_probs(prob, prob, prob)
        deep = {"score": legacy["score"], "band": legacy["band"],
                "p_fraud": round(prob, 4),
                "weights_provenance": {"source": "legacy_fallback"}}
        weights_source = "legacy_fallback"
        weights_formula = {}
        weights_vs_baseline = {}

    # Safely format counterfactual object
    cf_raw = deep.get("counterfactual")
    if isinstance(cf_raw, dict):
        cf_formatted = f"{cf_raw.get('action', 'Shift')} {cf_raw.get('delta_needed', 0):+.4f} in 'meta' probability to shift into {cf_raw.get('to_reach', 'REVIEW')}"
    elif cf_raw is not None:
        cf_formatted = str(cf_raw)
    else:
        cf_formatted = "Feature vectors reside within normal baseline distribution"

    return {
        "tx_id": tx_id,
        "amount": tx.get("amount"),
        "is_fraud": tx.get("is_fraud"),
        "ml_probability": round(sig_scalar["meta"], 4),
        "signal_columns": sig_scalar,
        "structured_score": deep["score"],
        "band": deep.get("band"),
        "external_evidence": round(float(ext_ev), 4),
        "campaign_evidence": round(float(camp_ev), 4),
        "top_reason_column": deep.get("top_reason_column"),
        "counterfactual": cf_formatted,
        "weights_source": weights_source,
        "weights_formula": weights_formula,
        "weights_vs_baseline": weights_vs_baseline,
    }


@app.get("/api/structured-weights")
def get_structured_weights():
    """Fitted-vs-baseline weighted-formula report (updates.md 2.2
    transparency). Reads the COMMITTED canonical artifact, not the
    random-seed session refit, so the panel is deterministic."""
    struct = FittedStructuredScore.load_or_none(STRUCTURED_WEIGHTS_PATH)
    if struct is None:
        return {"present": False}
    return {"present": True, **struct.weights_report()}


# --------------------------------------------------------------------------- #
# Phase 7 standout panels
# --------------------------------------------------------------------------- #

@app.get("/api/timeline")
def get_timeline():
    """Blind-Spot timeline: every completed feedback cycle, oldest first.

    Baseline rows come from the deterministic scripts/timeline_eval.py
    artifact; each live 3-beat demo appends its own cycle to the same file,
    so the trajectory grows across sessions without a schema change."""
    if not os.path.exists(TIMELINE_PATH):
        return {"present": False,
                "note": "feedback_timeline.json not generated yet — run "
                        "scripts/timeline_eval.py."}
    try:
        d = json.load(open(TIMELINE_PATH))
    except Exception as exc:                # noqa: BLE001
        return {"present": False, "note": f"timeline unreadable: {exc}"}
    entries = d.get("entries", [])
    entries.sort(key=lambda e: e.get("idx", 0))
    return {
        "present": True,
        "schema": d.get("schema"),
        "generator": d.get("generator"),
        "total": len(entries),
        "entries": entries,
    }


@app.get("/api/rl-stretch")
def get_rl_stretch():
    """RL negative-result panel: the pre-registered kill-criterion result.

    Serves the REAL `rl_stretch` measurement block persisted by
    scripts/mechanism_eval.py (honest negative when the criterion fails);
    never recomputed, never fabricated."""
    path = os.path.join(ARTIFACT_DIR, "ood_matrix.json")
    if not os.path.exists(path):
        return {"present": False,
                "note": "ood_matrix.json not generated yet — run "
                        "scripts/mechanism_eval.py."}
    try:
        d = json.load(open(path))
    except Exception as exc:                # noqa: BLE001
        return {"present": False, "note": f"ood_matrix unreadable: {exc}"}
    rl = d.get("rl_stretch") or {}
    if not rl:
        return {"present": False,
                "note": "rl_stretch measurement missing from ood_matrix.json"}
    registry_metrics = {}
    try:
        reg = json.load(open(os.path.join(ARTIFACT_DIR,
                                          "strategy_registry.json")))
        for m in reg.get("manifest", []):
            if m.get("strategy") == "DQN_rl_stretch":
                registry_metrics = m.get("metrics", {})
    except Exception:                       # noqa: BLE001
        pass
    shipped = bool(rl.get("shipped", False))
    return {
        "present": True,
        "schema": d.get("schema"),
        "episodes_run": rl.get("episodes_run"),
        "rl_best_mean_evasion": rl.get("rl_best_mean_evasion"),
        "heuristic_baseline": rl.get("heuristic_baseline"),
        "shipped": shipped,
        "honest_negative": not shipped,
        "reason": rl.get("reason", ""),
        "pre_registered_criterion": rl.get("pre_registered_criterion", {}),
        "registry_metrics": registry_metrics,
    }


@app.get("/api/attribution")
def get_attribution():
    """Mechanism × evidence-source attribution matrix.

    `exhibit` = committed deterministic multi-mechanism artifact
    (scripts/attribution_eval.py). `live` = attribution over THIS session's
    twin + agentic worlds with the session ensemble/CaseManager, so the panel
    reflects the real world in memory. Both are honest, schema-identical."""
    exhibit = None
    e_path = os.path.join(ARTIFACT_DIR, "attribution.json")
    if os.path.exists(e_path):
        try:
            exhibit = json.load(open(e_path))
        except Exception:                   # noqa: BLE001
            exhibit = None

    live = {}
    if DEMO_STATE.get("ready"):
        try:
            from eval.attribution import build_attribution_matrix, \
                combine_matrices
            bt = DEMO_STATE["blue_team"]
            cm = DEMO_STATE.get("case_manager")
            twin_m = build_attribution_matrix(
                DEMO_STATE["twin"].world, bt, case_manager=cm,
                threshold=0.5, max_rows=1200, seed=42)
            ag = _ensure_agentic()
            sandbox = ag["sandbox"]
            ag_m = build_attribution_matrix(
                sandbox.world, bt, case_manager=None,
                threshold=0.5, max_rows=1200, seed=42)
            live = combine_matrices({"twin": twin_m, "agentic": ag_m})
        except Exception as exc:            # noqa: BLE001
            logger.warning("live attribution failed: %s", exc)

    return {
        "present": exhibit is not None,
        "exhibit": exhibit,
        "live": live or None,
        "sources": ["XGB", "GNN", "OSINT", "sanctions"],
    }


@app.get("/api/sample-txs")
def get_sample_transactions():
    """Return a curated selection of benign, review, and high-risk transactions."""
    if not DEMO_STATE["ready"]:
        return {"samples": []}
    twin = DEMO_STATE["twin"]
    
    benign_txs = [t for t in twin.world.transactions if not t.get("is_fraud")]
    fraud_txs = [t for t in twin.world.transactions if t.get("is_fraud")]
    
    samples = []
    if benign_txs:
        sampled_benign = random.sample(benign_txs, min(5, len(benign_txs)))
        for t in sampled_benign:
            samples.append({
                "tx_id": t["tx_id"],
                "from": t.get("from"),
                "to": t.get("to"),
                "amount": t.get("amount"),
                "is_fraud": False,
                "category": t.get("category", "retail"),
                "type": "Benign (APPROVE)",
            })
    if fraud_txs:
        sampled_fraud = random.sample(fraud_txs, min(5, len(fraud_txs)))
        for t in sampled_fraud:
            samples.append({
                "tx_id": t["tx_id"],
                "from": t.get("from"),
                "to": t.get("to"),
                "amount": t.get("amount"),
                "is_fraud": True,
                "category": t.get("category", "retail"),
                "type": "Attack (DECLINE)",
            })
    return {"samples": samples}


@app.get("/api/eval")
def run_evaluation():
    """Run multi-prevalence evaluation."""
    if not DEMO_STATE["ready"]:
        return {"error": "Not initialized"}
    twin = DEMO_STATE["twin"]
    bt: BlueTeamEnsemble = DEMO_STATE["blue_team"]
    scores = bt.score_transactions(list(twin.world.transactions), twin.world)
    y = [1.0 if t.get("is_fraud") else 0.0 for t in twin.world.transactions]
    import numpy as _np
    report = full_report(_np.asarray(y), scores)
    return report


@app.get("/api/graph")
def get_knowledge_graph(
    filter: str = "overview",
    trajectory_id: Optional[str] = None,
    node_id: Optional[str] = None,
    max_nodes: int = 150,
    max_edges: int = 250,
):
    """Return dynamic multi-relational Knowledge Graph computed from WorldState."""
    if not DEMO_STATE["ready"]:
        return {"error": "Not initialized. Call /api/init first."}
    twin = DEMO_STATE["twin"]
    bt = DEMO_STATE["blue_team"]
    return build_knowledge_graph(
        world=twin.world,
        ensemble=bt,
        filter_type=filter,
        trajectory_id=trajectory_id,
        node_id=node_id,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )


@app.get("/api/graph/trajectories")
def get_graph_trajectories():
    """List all logged attack trajectories for Knowledge Graph filtering."""
    if not DEMO_STATE["ready"]:
        return {"trajectories": []}
    twin = DEMO_STATE["twin"]
    return {"trajectories": list_trajectories_summary(twin.world)}


@app.get("/api/graph/node/{node_id}")
def get_graph_node(node_id: str):
    """Get detailed profile for a Knowledge Graph entity node."""
    if not DEMO_STATE["ready"]:
        return {"error": "Not initialized"}
    twin = DEMO_STATE["twin"]
    bt = DEMO_STATE["blue_team"]
    return extract_node_profile(twin.world, node_id, ensemble=bt)


class InvestigateRequest(BaseModel):
    case_id: str = "CASE_001"
    tx_ids: List[str] = []


@app.post("/api/investigate")
def run_investigation(req: InvestigateRequest):
    """Run CaseManager deep investigation with EvidenceStore provenance."""
    if not DEMO_STATE["ready"]:
        return {"error": "Not initialized"}
    case_mgr = DEMO_STATE.get("case_manager")
    if case_mgr is None:
        return {"error": "CaseManager not available"}
    twin = DEMO_STATE["twin"]
    tx_ids = req.tx_ids
    if not tx_ids:
        # Default to recent fraud or suspicious transactions
        tx_ids = [t["tx_id"] for t in twin.world.transactions if t.get("is_fraud")][-5:]
        if not tx_ids and twin.world.transactions:
            tx_ids = [twin.world.transactions[-1]["tx_id"]]
    try:
        case_report = case_mgr.run_case(req.case_id, tx_ids)
        return case_report
    except Exception as e:
        logger.exception("investigation failed")
        return {"status": "error", "detail": str(e)[:300], "error_code": "INVESTIGATE_FAILED"}


class InjectRequest(BaseModel):
    attack_type: str

@app.post("/api/stream/inject")
def inject_stream(req: InjectRequest):
    if not DEMO_STATE.get("ready"):
        return {"error": "Not initialized"}
    if "pending_injections" not in DEMO_STATE:
        DEMO_STATE["pending_injections"] = []
    DEMO_STATE["pending_injections"].append(req.attack_type)
    # Non-blocking broadcast: the live stream sim loop (single producer)
    # consumes pending_injections; every subscriber ALSO sees the directive.
    EVENTS.publish({"type": "inject", "attack_type": req.attack_type})
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Live SSE War-Room stream (updates.md 5): ONE producer, N subscribers.
# The 30-step simulation runs as a single background task that publishes
# step/done events to the EventHub; every EventSource client receives the
# same run (a subscriber reopened mid-run joins via the hub snapshot). The
# old design ran a per-client inline simulation, racing over world state.
# --------------------------------------------------------------------------- #
import threading as _threading
_STREAM_LOCK_REGISTRY: Dict[int, tuple] = {}
_STREAM_LOCK_REGISTRY_LOCK = _threading.Lock()


def _stream_lock() -> asyncio.Lock:
    """Per-running-loop stream lock (a new loop ⇒ a new lock, no cross-loop
    await of a foreign asyncio.Lock when uvicorn reloads). When the loop is
    brand-new while DEMO_STATE claims a running stream, that flag is stale
    (the producing loop is dead) — clear it so a fresh run can start."""
    loop = asyncio.get_running_loop()
    with _STREAM_LOCK_REGISTRY_LOCK:
        existing = _STREAM_LOCK_REGISTRY.get(id(loop))
        if existing is not None and existing[0] is loop:
            return existing[1]
        lock = asyncio.Lock()
        _STREAM_LOCK_REGISTRY[id(loop)] = (loop, lock)
        if DEMO_STATE.get("stream_running"):
            logger.warning("stream_running stale from a dead loop; clearing")
            DEMO_STATE["stream_running"] = False
    return lock


async def _ensure_stream_producer() -> bool:
    """Start the single producer task if it is not already running.

    Returns True when the caller triggered a FRESH run (so it can reset the
    hub snapshot), False when a run is in progress or the twin is unready."""
    if not DEMO_STATE.get("ready"):
        return False
    lock = _stream_lock()
    async with lock:
        if DEMO_STATE.get("stream_running"):
            return False
        DEMO_STATE["stream_running"] = True
        asyncio.create_task(_stream_producer())
        return True


async def _stream_producer() -> None:
    """Background sim loop: 30 twin steps with dynamic adversarial attacks,
    broadcasting every step and a final "done" on the hub."""
    try:
        twin = DEMO_STATE["twin"]
        bt: BlueTeamEnsemble = DEMO_STATE["blue_team"]

        # Advance the world randomly to ensure the stream is not identical.
        random.seed()

        for step in range(30):
            step_txs = twin.step()
            injected_fraud = []
            attack_name = None

            accts = list(twin.world.accounts.keys())
            merchs = list(twin.world.merchants.keys())

            if accts and merchs:
                attack_type = None
                pending = DEMO_STATE.get("pending_injections", [])
                if pending:
                    attack_type = pending.pop(0)
                elif random.random() < 0.2:  # 20% chance of attack per step
                    attack_type = random.choice(["A3", "A1", "A4", "A6", "A5"])

                if attack_type == "A3":
                    attack_name = "A3 Card-Testing Micro Probes"
                    src = random.choice(accts)
                    for m_idx in range(min(4, len(merchs))):
                        tx_dict = twin.world.log_transaction(
                            from_id=src, to_id=merchs[m_idx],
                            amount=round(random.uniform(0.10, 0.95), 2),
                            step=twin.world.current_step, is_fraud=True,
                            category="retail", mechanism="rule_compiler",
                        )
                        injected_fraud.append(tx_dict)
                elif attack_type == "A1" and len(accts) > 5:
                    attack_name = "A1 Account Takeover"
                    victim = random.choice(accts)
                    tx1 = twin.world.log_transaction(
                        from_id=victim, to_id=random.choice(merchs), amount=1.00,
                        step=twin.world.current_step, is_fraud=True,
                        category="retail", mechanism="rule_compiler",
                    )
                    tx2 = twin.world.log_transaction(
                        from_id=victim, to_id=random.choice(merchs),
                        amount=round(random.uniform(28000, 65000), 2),
                        step=twin.world.current_step, is_fraud=True,
                        category="retail", mechanism="rule_compiler",
                    )
                    injected_fraud.extend([tx1, tx2])
                elif attack_type == "A4" and len(accts) > 10:
                    attack_name = "A4 Mule Fan-In Funnel"
                    mule = random.choice(accts)
                    senders = random.sample(accts, min(4, len(accts)))
                    for sender in senders:
                        if sender != mule:
                            t = twin.world.log_transaction(
                                from_id=sender, to_id=mule,
                                amount=round(random.uniform(8000, 18000), 2),
                                step=twin.world.current_step, is_fraud=True,
                                category="transfer", mechanism="rule_compiler",
                            )
                            injected_fraud.append(t)
                elif attack_type == "A6":
                    attack_name = "A6 Fake Storefront Cash-Out"
                    t = twin.world.log_transaction(
                        from_id=random.choice(accts), to_id=random.choice(merchs),
                        amount=round(random.uniform(45000, 92000), 2),
                        step=twin.world.current_step, is_fraud=True,
                        category="retail", mechanism="rule_compiler",
                    )
                    injected_fraud.append(t)
                elif attack_type == "A5" and len(accts) > 8:
                    attack_name = "A5 Scatter-Gather Layering"
                    send_recv = random.sample(accts, 2)
                    t = twin.world.log_transaction(
                        from_id=send_recv[0], to_id=send_recv[1],
                        amount=round(random.uniform(15000, 35000), 2),
                        step=twin.world.current_step, is_fraud=True,
                        category="transfer", mechanism="rule_compiler",
                    )
                    injected_fraud.append(t)

            all_step_txs = step_txs + injected_fraud
            fraud_txs = [t for t in all_step_txs if t.get("is_fraud")]
            normal_txs = [t for t in all_step_txs if not t.get("is_fraud")]
            scores = bt.score_transactions(all_step_txs, twin.world) if all_step_txs else np.array([])
            peak = float(scores.max()) if scores.size else 0.0
            caught = int((scores >= 0.5).sum()) if scores.size else 0
            high_risk_tx_ids = [t["tx_id"] for t, s in zip(all_step_txs, scores) if s >= 0.7]

            EVENTS.publish({
                "type": "step",
                "step": step + 1,
                "steps": 30,
                "normal": len(normal_txs),
                "fraud": len(fraud_txs),
                "peak_score": round(peak, 4),
                "caught": caught,
                "attack_type": attack_name,
                "total_volume": round(sum(t.get("amount", 0) for t in all_step_txs), 2),
                "sample_tx_id": high_risk_tx_ids[0] if high_risk_tx_ids else (all_step_txs[0]["tx_id"] if all_step_txs else ""),
            })
            await asyncio.sleep(1.0)  # real delay to make the stream readable
        EVENTS.publish({"type": "done"})
    finally:
        DEMO_STATE["stream_running"] = False


@app.get("/api/stream")
async def live_stream():
    """SSE: subscribe to the shared hub. One producer, N subscribers."""
    import json as _json

    if not DEMO_STATE.get("ready"):
        async def not_ready_gen():
            yield "data: " + _json.dumps({"type": "error",
                                          "detail": "not initialized"}) + "\n\n"
        return StreamingResponse(
            not_ready_gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"})

    fresh = await _ensure_stream_producer()
    if fresh:
        # This client started a brand-new sim: drop a stale "done" snapshot
        # NOW (we are on the loop thread) so the subscriber seed below cannot
        # instantly end the stream with an old terminal event.
        EVENTS.clear_snapshot()

    queue = await EVENTS.subscribe()

    async def event_gen():
        try:
            yield "retry: 1000\n\n"
            while True:
                try:
                    frame = await asyncio.wait_for(
                        queue.get(), timeout=EVENTS.heartbeat_interval())
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield frame
                if frame.startswith("data: ") and _json.loads(frame[6:]).get("type") == "done":
                    break
        finally:
            await EVENTS.unsubscribe(queue)

    return StreamingResponse(
        event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/combo")
def run_combo_attack():
    """Run the GenAI fraud supply-chain combo attack (Phase 10).

    Chains: synthetic-identity onboarding → merchant fraud →
    layering → cash-out — scored at each stage by the ensemble."""
    if not DEMO_STATE["ready"]:
        return {"error": "Not initialized"}
    try:
        from combo.supply_chain import SupplyChainCombo
        twin = DEMO_STATE["twin"]
        bt: BlueTeamEnsemble = DEMO_STATE["blue_team"]
        import random
        combo = SupplyChainCombo(twin, bt, seed=random.randint(1, 1000000))
        result = combo.run()
        EVENTS.publish({
            "type": "combo",
            "detected": bool(result.get("detected", False)),
            "total_layers": result.get("total_layers", 0),
            "detail": result.get("summary", result.get("detail", ""))[:256],
        })
        return result
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("combo failed")
        return {"status": "error", "detail": str(e)[:300],
                "error_code": "COMBO_FAILED"}


@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    """Serve the single-page War-Room dashboard."""
    dashboard_path = Path(__file__).parent.parent / "dashboard" / "index.html"
    if dashboard_path.exists():
        return HTMLResponse(dashboard_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found</h1>")


def main():
    print("Starting Prometheus Uvicorn server on http://0.0.0.0:8000 ...", flush=True)
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()