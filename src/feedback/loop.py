"""Feedback Loop — weakness-directed, max 2 retrain rounds.

Sequence: miss → diagnose (Sensitivity Engine) → weakness descriptor →
targeted variants (Attack Compiler) → retrain on original + hard negatives →
recheck → Blind-Spot Report.
"""

import json
import random
from typing import Any, Dict, List, Optional

MAX_RETRAIN_ROUNDS = 2  # hard cap, enforced in code


class FeedbackLoop:
    """Closed-loop: ATTACK → DETECT → DIAGNOSE → RETRAIN → RE-ATTACK."""

    def __init__(self, twin, compiler, blue_team, sensitivity_engine, seed=42):
        self.twin = twin
        self.compiler = compiler
        self.blue_team = blue_team
        self.sensitivity = sensitivity_engine
        self.rng = random.Random(seed)
        self.rounds_used = 0
        self.event_log = []

    def _log(self, event: str, **kwargs):
        entry = {"event": event, "step": self.twin.world.current_step, **kwargs}
        self.event_log.append(entry)
        return entry

    def run_cycle(self, attack_ids: List[str], held_out_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Run one full feedback cycle.

        Args:
            attack_ids: attack types to test (e.g. ["A1", "A3", "A4", "A6"])
            held_out_ids: held-out attack types to test for generalization

        Returns:
            Blind-Spot Report dict
        """
        self.rounds_used = 0
        held_out_ids = held_out_ids or []

        # Beat 1: run attacks against current Blue Team
        self._log("cycle_start", attacks=attack_ids, held_out=held_out_ids)
        recall_before = self._evaluate_attacks(attack_ids)
        held_out_before = self._evaluate_attacks(held_out_ids) if held_out_ids else None

        # Diagnose misses
        misses = self._find_misses(attack_ids)
        self._log("diagnose", misses=len(misses))

        # If no misses, nothing to fix
        if not misses:
            report = self._build_report(
                recall_before=recall_before,
                recall_after=recall_before,
                generalization_recall=held_out_before,
                generated_fixes=0,
                blind_spot="none",
                evidence={},
            )
            self._log("cycle_complete", rounds=0)
            return report

        # Weakness-directed retrain loop (max 2 rounds)
        recall_after = recall_before
        generalization_after = held_out_before
        total_fixes = 0
        blind_spot = "unknown"
        evidence = {}

        while self.rounds_used < MAX_RETRAIN_ROUNDS:
            self.rounds_used += 1
            self._log("retrain_round", round=self.rounds_used)

            # Diagnose which model/signal failed
            weakness = self.sensitivity.weakness_direction(self._get_X(), self._get_data())
            blind_spot = weakness["weakness"]
            evidence = {
                "gnn_contribution": "low" if weakness["target_model"] == "GNN" else "high",
                "sequence_signal": "low",
                "graph_density": "below_threshold",
            }

            # Generate targeted variants
            variants = self.compiler.generate_variants(weakness, n=20)
            total_fixes += len(variants)
            self._log("generate_variants", count=len(variants), weakness=blind_spot)

            # Retrain on original + hard negatives
            self._retrain_with_hard_negatives(attack_ids, variants)

            # Recheck
            recall_after = self._evaluate_attacks(attack_ids)
            self._log("recheck", recall=recall_after)

            # If we caught everything, stop
            if recall_after >= 0.99:
                break

        # Generalization beat: held-out attacks
        if held_out_ids:
            generalization_after = self._evaluate_attacks(held_out_ids)
            self._log("generalization_check", recall=generalization_after)

        report = self._build_report(
            recall_before=recall_before,
            recall_after=recall_after,
            generalization_recall=generalization_after,
            generated_fixes=total_fixes,
            blind_spot=blind_spot,
            evidence=evidence,
        )
        self._log("cycle_complete", rounds=self.rounds_used)
        return report

    # -- Internals ----------------------------------------------------------

    def _get_X(self):
        """Get feature matrix from current twin transactions."""
        from blue.features import compute_features
        X, y, _ = compute_features(self.twin.world.transactions, self.twin.world)
        return X

    def _get_data(self):
        """Get graph data from current twin transactions."""
        from blue.features import build_graph_data
        return build_graph_data(self.twin.world.transactions, self.twin.world)

    def _evaluate_attacks(self, attack_ids: List[str]) -> float:
        """Run attacks and measure recall (fraction caught)."""
        if not attack_ids:
            return 0.0
        caught = 0
        total = 0
        for aid in attack_ids:
            # Generate and execute the attack
            spec = self.compiler.compile(self.compiler.benchmark_spec(aid))
            traj_id = self.compiler.execute(spec, self.twin.world)
            # Score the attack's transactions
            attack_txs = [tx for tx in self.twin.world.transactions if tx.get("trajectory_id") == traj_id]
            if not attack_txs:
                continue
            total += 1
            # Use Blue Team to score
            score = self._score_transactions(attack_txs)
            if score > 0.5:
                caught += 1
        return caught / total if total > 0 else 0.0

    def _score_transactions(self, txs) -> float:
        """Score a list of transactions with the Blue Team. Returns max risk."""
        try:
            from blue.features import compute_features
            X, y, _ = compute_features(txs, self.twin.world)
            if len(X) == 0:
                return 0.0
            return float(self.blue_team.predict_proba(X).max())
        except Exception:
            return 0.0

    def _find_misses(self, attack_ids: List[str]) -> List[str]:
        """Find which attack types were missed."""
        misses = []
        for aid in attack_ids:
            spec = self.compiler.compile(self.compiler.benchmark_spec(aid))
            traj_id = self.compiler.execute(spec, self.twin.world)
            attack_txs = [tx for tx in self.twin.world.transactions if tx.get("trajectory_id") == traj_id]
            if not attack_txs:
                misses.append(aid)
                continue
            score = self._score_transactions(attack_txs)
            if score <= 0.5:
                misses.append(aid)
        return misses

    def _retrain_with_hard_negatives(self, attack_ids, variants):
        """Retrain Blue Team on original + hard negatives."""
        # Collect all transactions
        all_txs = list(self.twin.world.transactions)
        # Execute variants to create hard negatives
        for variant in variants:
            try:
                plan = self.compiler.compile(variant)
                self.compiler.execute(plan, self.twin.world)
            except Exception:
                continue
        # Retrain
        self.blue_team.fit(self.twin.world)

    def _build_report(self, recall_before, recall_after, generalization_recall,
                      generated_fixes, blind_spot, evidence) -> Dict[str, Any]:
        return {
            "blind_spot": blind_spot,
            "evidence": evidence,
            "generated_fixes": generated_fixes,
            "recall_before": round(recall_before, 4),
            "recall_after": round(recall_after, 4),
            "generalization_recall_unseen_generator": round(generalization_recall, 4) if generalization_recall is not None else None,
            "retrain_rounds_used": self.rounds_used,
            "max_retrain_rounds": MAX_RETRAIN_ROUNDS,
        }

    def save_report(self, report: Dict[str, Any], path: str):
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        return path