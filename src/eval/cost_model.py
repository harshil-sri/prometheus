"""cost_model.py — INR cost economics of alert budgets (Phase 9).

A DECLARED-ASSUMPTION model, not a prophecy: every knob lives in the output
so judges can twist them. Inputs come from MEASURED twin metrics
(mean fraud loss proxy, recall@budget from eval harness on real scores).

Per 1,000 transactions at prevalence p:
    fraud_txns   = 1000 · p
    alerts       = 1000 · budget_fraction
    review_cost  = alerts / reviews_per_analyst_hour · analyst_rate_INR_h
    prevented    = fraud_txns · recall_at_budget
    gross_saved  = prevented · avg_fraud_loss_INR
    churn_cost   = false_positives · false_decline_cost_INR
    net          = gross_saved − review_cost − churn_cost

False positives at budget b ≈ (b − tp_rate)·1000 where tp_rate =
recall·p; and cost/prevented-fraud reported with None when prevented==0.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = ["DEFAULT_ASSUMPTIONS", "inr_economics", "sensitivity_grid"]

DEFAULT_ASSUMPTIONS: Dict[str, Any] = {
    "avg_fraud_loss_inr": 12_000.0,
    "analyst_rate_inr_per_hour": 480.0,
    "analyst_review_minutes": 6.0,
    "false_decline_cost_inr": 250.0,
}


def inr_economics(prevalence: float, budget_pct: float,
                  recall_at_budget: float, precision_at_budget: float = 0.0,
                  assumptions: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    A = {**DEFAULT_ASSUMPTIONS, **(assumptions or {})}
    tx = 1000.0
    n_fraud = tx * prevalence
    n_alerts = tx * budget_pct / 100.0

    prevented = n_fraud * recall_at_budget
    review_minutes = n_alerts * A["analyst_review_minutes"]
    review_cost = review_minutes / 60.0 * A["analyst_rate_inr_per_hour"]

    # expected FP at this budget = alerts − true positives
    tp = min(n_alerts, n_fraud * precision_at_budget * max(recall_at_budget, 0))
    fp = max(0.0, n_alerts - tp)
    churn_cost = fp * A["false_decline_cost_inr"]

    gross_saved = prevented * A["avg_fraud_loss_inr"]
    net = gross_saved - review_cost - churn_cost

    cppf = round(net + review_cost, 2)
    cost_per_prevented = (
        round((review_cost + churn_cost) / prevented, 2)
        if prevented > 0 else None)

    return {
        "per_1000_transactions": {
            "fraud_txn_expected": round(n_fraud, 3),
            "alerts_generated": round(n_alerts, 2),
            "est_prevented_frauds": round(prevented, 3),
            "est_false_positives": round(fp, 2),
        },
        "inr_breakdown": {
            "gross_saved_by_prevention": round(gross_saved, 2),
            "review_cost": round(review_cost, 2),
            "false_decline_cost": round(churn_cost, 2),
            "net_benefit": round(net, 2),
            "cost_per_prevented_fraud": cost_per_prevented,
        },
        "assumptions_declared": A,
        "inputs_measured": {
            "prevalence": prevalence,
            "budget_pct": budget_pct,
            "recall_at_budget": recall_at_budget,
            "precision_at_budget": precision_at_budget,
        },
    }


def sensitivity_grid(evals: Dict[str, Dict[str, float]],
                     assumptions_variants: Dict[str, Dict[str, Any]],
                     ) -> Dict[str, Any]:
    """Grid over {eval_label → {prevalence,budget,recall,precision}} ×
    assumption variants. Produces comparable net-benefit table."""
    table: Dict[str, Any] = {}
    for eval_label, e in evals.items():
        row = {}
        for var_label, var_assump in assumptions_variants.items():
            res = inr_economics(
                e["prevalence"], e["budget"], e["recall"],
                e.get("precision", 0.0), var_assump)
            row[var_label] = res["inr_breakdown"]["net_benefit"]
        table[eval_label] = {"inputs": e, "net_by_variant": row}
    return {"schema": "prometheus.cost_model.v1", "grid": table}
