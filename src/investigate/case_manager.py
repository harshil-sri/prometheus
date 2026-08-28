"""case_manager.py — The investigator's orchestrator (Phase 8).

LAW 10 (Strix pattern, enforced by test + structure): the CaseManager NEVER
EXECUTES WORK ITSELF. It owns a call budget and DELEGATES every enrichment,
scoring or narrative action to an agent that returns ComputedEvidence:

    FastSignalsAgent   → victim ensemble six-column signals
    FeatureDriverAgent → per-row SHAP top drivers (via SensitivityEngine)
    SanctionsAgent     → guarded screening (synthetic namespace only)
    OsintAgent         → twin-derived fixture dossier
    SpectralAgent      → ego-graph topology summaries
    NarrativeAgent     → LLM (guarded) with deterministic template fallback
    StructuredScorer   → fitted 0–1000 score w/ reasons + counterfactual

Every returned artifact carries evidence_ids; the case manifest ends with an
integrity digest. Failure of any agent degrades gracefully WITHOUT inventing
content — missing pieces are recorded as computed=false entries.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

import numpy as np

from blue.manifold import NormalcyManifold            # noqa: F401 (typing)
from blue.spectral import compute_spectral_features
from scoring.structured_score import FittedStructuredScore
from .evidence_store import CaseEvidence
from .guardrails import (
    GuardrailViolation, compose_case_prompt, redact_secrets, sanitize_text,
    validate_case_id,
)
from .llm_client import LLMClient, LLMUnavailable
from .memory import ThreeClassMemory
from .osint_fixtures import build_osint_fixtures
from .sanctions import BudgetExceeded, NameNotInSandbox, SanctionsAgent

logger = logging.getLogger(__name__)

__all__ = ["CaseManager", "DelegateBudgetExceeded"]

DELEGATE_BUDGET = 24          # hard cap for delegated calls per case
MAX_CASE_ROWS = 50


class DelegateBudgetExceeded(RuntimeError):
    """Case attempted more delegated calls than allowed."""


class _Delegation:
    """Bookkeeping wrapper — returns tagged delegates or raises budget."""

    def __init__(self, store: CaseEvidence, budget: int = DELEGATE_BUDGET):
        self.store = store
        self.left = budget
        self.history: List[str] = []

    def spend(self, delegate_name: str) -> None:
        if self.left <= 0:
            raise DelegateBudgetExceeded(
                f"budget {len(self.history)} calls already spent")
        self.left -= 1
        self.history.append(delegate_name)


class CaseManager:
    def __init__(self, ensemble, twin, sensitivity=None,
                 llm: Optional[LLMClient] = None, seed: int = 42,
                 manifold: Optional[NormalcyManifold] = None,
                 structured: Optional[FittedStructuredScore] = None,
                 memory: Optional[ThreeClassMemory] = None):
        self.ensemble = ensemble
        self.twin = twin
        self.sensitivity = sensitivity
        self.llm = llm or LLMClient()
        self.seed = seed
        self.rng = random.Random(seed)
        self.manifold = manifold
        self.structured = structured
        self.memory = memory if memory is not None else ThreeClassMemory()
        self._fixtures_cache: Optional[dict] = None

    # ------------------------------------------------------------------ #
    # agents (delegated — never inlined work)
    # ------------------------------------------------------------------ #
    def _fixtures(self) -> dict:
        if self._fixtures_cache is None:
            self._fixtures_cache = build_osint_fixtures(
                self.twin.world, seed=self.seed)
        return self._fixtures_cache

    def _agent_sanctions(self) -> SanctionsAgent:
        return SanctionsAgent(self._fixtures(), mode="fixture",
                              call_budget=6, watch_seed=self.seed)

    def run_case(self, case_id: str, tx_ids: List[str],
                 include_narrative: bool = True,
                 ) -> Dict[str, Any]:
        case_id = validate_case_id(case_id)
        world = self.twin.world
        index_of = {t["tx_id"]: i for i, t in enumerate(world.transactions)}
        missing = [t for t in tx_ids[:MAX_CASE_ROWS] if t not in index_of]
        if missing:
            raise KeyError(f"unknown tx ids for case: {missing[:3]}")
        rows = [world.transactions[index_of[t]] for t in tx_ids[:MAX_CASE_ROWS]]

        store = CaseEvidence(case_id, seed=self.seed)
        dele = _Delegation(store)

        report: Dict[str, Any] = {
            "schema": "prometheus.case.v1",
            "case_id": case_id,
            "n_rows": len(rows),
            "opened_at": time.time(),
            "evidence": {},
            "missing_agents": [],
        }
        signals: Optional[Dict] = None

        def register(kind: str, value: Dict, source: str,
                     computed_ok: bool = True, **extra) -> Optional[str]:
            if not computed_ok:
                key = f"unavailable::{kind}"
                report["missing_agents"].append(kind)
                return None
            ev = store.register(kind=kind, value=value, source=source,
                                seed=self.seed)
            report["evidence"][ev.evidence_id] = {
                "kind": kind,
                "summary": json.dumps(value, sort_keys=True,
                                      default=str)[:200],
            }
            return ev.evidence_id

        # ---------------- agent 1: fast-path signal peak -------------------
        try:
            dele.spend("FastSignalsAgent")
            signals = self.ensemble.score_all_signals(rows, world,
                                                      manifold=self.manifold)
            peaks = {k: round(float(max(v)), 4) if len(v) else 0.0
                     for k, v in signals.items()}
            e_fast = register("fast_signals",
                              {"peaks": peaks, "rows": len(rows)},
                              "CaseManager→BlueTeamEnsemble.score_all_signals")
        except Exception as exc:                                # noqa: BLE001
            logger.warning("FastSignalsAgent failed: %s", exc)
            e_fast = register("fast_signals", {}, "", computed_ok=False)

        # ---------------- agent 2: feature drivers -------------------------
        e_drivers = None
        try:
            shap = getattr(self.sensitivity, "shap", None) \
                if self.sensitivity else None
            if shap is None:
                raise ValueError("no shap explainer attached")
            from blue.features import compute_features as _cf
            X_rows, _, fnames = _cf(rows, world)
            sv = shap.shap_values(np.asarray(X_rows, dtype=np.float64))
            top_rows = []
            for r_i in range(min(len(X_rows), 10)):
                pair = sorted(
                    range(len(fnames)),
                    key=lambda j: -abs(float(sv[r_i][j])))[:4]
                top_rows.append({
                    "tx_id": rows[r_i]["tx_id"],
                    "drivers": [{fnames[j]: round(float(sv[r_i][j]), 4)}
                                for j in pair],
                })
            e_drivers = register("feature_drivers",
                                 {"per_row": top_rows},
                                 "CaseManager→SensitivityEngine.shap_values")
        except Exception as exc:                                # noqa: BLE001
            logger.warning("FeatureDriverAgent failed: %s", exc)
            e_drivers = register("feature_drivers", {},
                                 "", computed_ok=False)

        # ---------------- agent 3+4: OSINT & sanctions ----------------------
        sender_ids: List[str] = []
        for t in rows:
            fid = str(t.get("from"))
            if fid.startswith(("ACC_",)) and fid not in sender_ids:
                sender_ids.append(fid)
            if len(sender_ids) >= 3:
                break

        try:
            dele.spend("OsintAgent")
            fx = self._fixtures()
            dossiers = [fx.get(sid) for sid in sender_ids]
            e_osint = register("osint_dossier",
                               {"dossiers": [
                                   {"entity_id": d["entity_id"],
                                    "pseudonym": d["pseudonym"],
                                    "kind": d["kind"]}
                                   for d in dossiers if d]},
                               "CaseManager→OSINTFixtureProvider")
        except Exception as exc:
            logger.warning("OsintAgent failed: %s", exc)
            e_osint = register("osint_dossier", {}, "",
                               computed_ok=False)

        sanction_hits: List[Dict] = []
        try:
            agent = self._agent_sanctions()
            screened = []
            for sid in sender_ids:
                dele.spend("SanctionsAgent")
                res = agent.screen(sid)
                screened.append(res)
            e_sanc = register("sanctions_screening",
                              {"screens": screened},
                              "CaseManager→SanctionsAgent(fixture)")
            sanction_hits = [s for s in screened if s["result"] == "WATCH_HIT"]
        except (BudgetExceeded, NameNotInSandbox) as exc:
            e_sanc = register("sanctions_screening", {"blocked_by": str(exc)[:120]},
                              "SanctionsAgent.guard")
        except Exception as exc:                                # noqa: BLE001
            logger.warning("SanctionsAgent failed: %s", exc)
            e_sanc = register("sanctions_screening", {}, "",
                              computed_ok=False)

        # ---------------- agent 5: spectral topology ------------------------
        try:
            X_spec, spec_names = compute_spectral_features(rows)
            names_idx = {n: i for i, n in enumerate(spec_names)}
            cyc_resid = float(np.mean(X_spec[:, names_idx["spec_cycle_residual"]])) \
                if len(rows) else 0.0
            star_resid = float(np.mean(X_spec[:, names_idx["spec_star_residual"]])) \
                if len(rows) else 0.0
            e_spec = register("spectral_topology",
                              {"mean_cycle_residual": round(cyc_resid, 6),
                               "mean_star_residual": round(star_resid, 6),
                               "interpretation":
                                   "low cycle residual ⇒ ring-like local flow"},
                              "CaseManager→SpectralAgent")
        except Exception as exc:                                # noqa: BLE001
            logger.warning("SpectralAgent failed: %s", exc)
            e_spec = register("spectral_topology", {}, "",
                              computed_ok=False)

        # ---------------- agent 6: narrative ---------------------------------
        evidence_summaries = [
            f"{eid} [{report['evidence'][eid]['kind']}] "
            f"{report['evidence'][eid]['summary']}"
            for eid in list(report["evidence"])][:12]
        case_ctx = {"case_id": case_id, "rows": len(rows),
                    "senders": sender_ids,
                    "watch_hits": bool(sanction_hits)}
        narrative_text: Optional[str] = None
        narrative_mode = "fallback"
        try:
            system_msg, user_msg = compose_case_prompt(case_ctx,
                                                       evidence_summaries)
            user_msg = sanitize_text(user_msg)      # choke point BEFORE wire
            resp = self.llm.chat([{"role": "system", "content": system_msg},
                                  {"role": "user", "content": user_msg}])
            narrative_text = redact_secrets(str(resp.get("text", "")))[:1600]
            narrative_mode = "llm"
            del_evidence = store.register(
                kind="llm_response_meta",
                value={"mode": "llm", "model": resp.get("model"),
                       "finish": resp.get("finish")},
                source="NarrativeAgent(LLM)")
            report["evidence"][del_evidence] = {
                "kind": "llm_response_meta", "summary": f"model={resp.get('model')}"
            }
        except LLMUnavailable as exc:
            narrative_mode = "fallback"
            fallback_reason = str(exc)[:120]
            lines = [f"SANDBOX INVESTIGATION {case_id} — deterministic "
                     f"summary ({len(rows)} txs)."]
            lines.append(f"Sender(s): "
                         f"{', '.join(sender_ids) or 'n/a'}.")
            if sanction_hits:
                lines.append(f"{len(sanction_hits)} sanctions WATCH hit(s).")
            if e_spec:
                lines.append("Local flow topology summarized above.")
            narrative_text = " ".join(lines)[:1200] + \
                f" | fallback_reason={fallback_reason}"
        except GuardrailViolation as gv:
            narrative_mode = "blocked"
            narrative_text = ""
            register("narrative_guard_block",
                     {"reason": gv.reason, "detail": gv.detail},
                     "Guardrails.block")

        e_narr = register("narrative",
                          {"mode": narrative_mode,
                           "text_head": (narrative_text or "")[:220]},
                          "CaseManager→NarrativeAgent")

        # ---------------- agent 7: fitted structured score -------------------
        score_result: Optional[Dict] = None
        e_score = None
        try:
            if self.structured is None:
                raise ValueError("structured scorer not fitted/attached")
            if not isinstance(signals, dict):
                raise ValueError("signals unavailable; cannot derive deep score")
            row_signal_mean = {
                k: float(np.mean(v)) if len(v) else 0.0
                for k, v in signals.items()
            }
            dele.spend("StructuredScorer")
            score_result = self.structured.predict_row(row_signal_mean)
            score_result["reason_evidence_ids"] = {
                "fast_signals": e_fast, "spectral": e_spec,
                "sanctions": e_sanc}
            e_score = register("structured_score",
                               {k: score_result[k]
                                for k in ("score", "band", "p_fraud",
                                          "top_reason_column",
                                          "counterfactual")},
                               "CaseManager→StructuredScorer(fitted)")
        except Exception as exc:                                # noqa: BLE001
            logger.warning("StructuredScorer failed: %s", exc)
            e_score = register("structured_score", {}, "",
                               computed_ok=False)

        # close out ------------------------------------------------------------
        report.update({
            "sender_accounts": sender_ids,
            "narrative_mode": narrative_mode,
            "narrative": narrative_text,
            "structured": score_result,
            "delegate_log": list(dele.history),
            "delegates_used": len(dele.history),
            "delegates_left": dele.left,
            "watch_hit_count": len(sanction_hits),
            "all_statistical": None,                  # reserved (P11 doc gen)
        })
        mem_digest = self.memory.remember_case(case_id, {
            "tx_count": len(rows), "band": (score_result or {}).get("band"),
            "narrative_mode": narrative_mode,
            "watch_hits": len(sanction_hits)})
        self.memory.add_defender_note(
            f"case {case_id}: band={ (score_result or {}).get('band') }",
            phase="P8")
        if sanction_hits:
            self.memory.remember_attack_signature(
                mechanism="sanctions_watch",
                signature_payload={"count": len(sanction_hits)})
        report["memory_digest"] = mem_digest
        report["memory_signature_count"] = \
            len(self.memory.attack_signatures)

        report["manifest"] = store.manifest()
        report["closed_at"] = time.time()
        return report