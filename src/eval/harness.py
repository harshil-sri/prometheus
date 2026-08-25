"""Evaluation Harness — multi-prevalence evaluation and cost model.

Fraud prevalence levels: 0.01%, 0.05%, 0.1%, 0.5%, 1%, 5%
Metrics: PR-AUC, recall/budget, precision/budget, false-decline rate, cost/prevented fraud.
"""

import numpy as np
from sklearn.metrics import precision_recall_curve, auc, precision_score, recall_score


PREVALENCES = [0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05]


def evaluate_at_prevalence(y_true, y_score, prevalence):
    """Compute metrics at a given fraud prevalence by subsampling.

    Args:
        y_true: Ground truth labels
        y_score: Predicted scores/probabilities
        prevalence: Target fraud prevalence

    Returns:
        dict of metrics
    """
    fraud_indices = np.where(y_true == 1)[0]
    legit_indices = np.where(y_true == 0)[0]

    # Calculate required samples
    n_fraud = len(fraud_indices)
    n_legit_target = int(n_fraud * (1 - prevalence) / prevalence) if prevalence > 0 else len(legit_indices)
    n_legit = min(n_legit_target, len(legit_indices))

    if n_legit < 100:
        return {"error": f"Not enough legitimate samples for prevalence {prevalence}"}

    # Subsample legitimate
    rng = np.random.RandomState(42)
    legit_sample = rng.choice(legit_indices, n_legit, replace=False)

    eval_indices = np.concatenate([fraud_indices, legit_sample])
    y_eval = y_true[eval_indices]
    y_score_eval = y_score[eval_indices]

    return _compute_metrics(y_eval, y_score_eval)


def _compute_metrics(y_true, y_score):
    """Compute all metrics for a set of predictions."""
    precision, recall, thresholds = precision_recall_curve(y_true, y_score)
    pr_auc = float(auc(recall, precision))

    # Metrics at various alert budgets (top K% of scores)
    metrics = {"pr_auc": pr_auc, "n_samples": len(y_true), "n_fraud": int(y_true.sum())}

    # Alert budgets: 1%, 2%, 5%, 10% of transactions
    for budget_pct in [1, 2, 5, 10]:
        k = max(1, int(len(y_score) * budget_pct / 100))
        top_k_idx = np.argsort(y_score)[-k:]
        y_pred_top = np.zeros_like(y_true)
        y_pred_top[top_k_idx] = 1

        recall_at_k = recall_score(y_true, y_pred_top) if y_true.sum() > 0 else 0.0
        prec_at_k = precision_score(y_true, y_pred_top) if y_pred_top.sum() > 0 else 0.0
        false_decline = (y_pred_top.sum() - (y_pred_top.astype(bool) & y_true.astype(bool)).sum()) / max(1, len(y_true))

        metrics[f"recall_at_{budget_pct}pct"] = round(float(recall_at_k), 4)
        metrics[f"precision_at_{budget_pct}pct"] = round(float(prec_at_k), 4)
        metrics[f"false_decline_rate_{budget_pct}pct"] = round(float(false_decline), 6)

    # Optimal threshold via Elkan cost
    optimal = _elkan_threshold(y_true, y_score)
    metrics.update(optimal)

    return metrics


def _elkan_threshold(y_true, y_score, c_fp=1.0, c_fn=10.0):
    """Find optimal threshold using Elkan's cost-sensitive method.

    τ* = C_FP / (C_FP + C_FN)  for binary case, then refined.

    Args:
        y_true: Ground truth
        y_score: Predicted scores
        c_fp: Cost of false positive (investigation cost)
        c_fn: Cost of false negative (fraud loss)

    Returns:
        dict with optimal_threshold, cost_at_optimal
    """
    # Elkan formula for binary classification
    tau_star = c_fp / (c_fp + c_fn)

    # Refine by grid search
    thresholds = np.linspace(0.01, 0.99, 100)
    best_cost = float('inf')
    best_t = tau_star

    for t in thresholds:
        y_pred = (y_score >= t).astype(int)
        fp = ((y_pred == 1) & (y_true == 0)).sum()
        fn = ((y_pred == 0) & (y_true == 1)).sum()
        cost = c_fp * fp + c_fn * fn
        if cost < best_cost:
            best_cost = cost
            best_t = t

    return {
        "elkan_threshold": round(float(tau_star), 4),
        "optimal_threshold": round(float(best_t), 4),
        "min_cost": float(best_cost),
        "cost_per_prevented_fraud": round(float(best_cost / max(1, y_true.sum())), 2),
    }


def multi_prevalence_eval(y_true, y_score):
    """Run evaluation at all 6 prevalence levels.

    Returns:
        dict of prevalence -> metrics
    """
    results = {}
    for prev in PREVALENCES:
        results[str(prev)] = evaluate_at_prevalence(y_true, y_score, prev)
    return results


def full_report(y_true, y_score) -> dict:
    """Generate a full evaluation report."""
    results = multi_prevalence_eval(y_true, y_score)
    overall = _compute_metrics(y_true, y_score)

    return {
        "multi_prevalence": results,
        "overall": overall,
        "n_samples": len(y_true),
        "n_fraud": int(y_true.sum()),
        "fraud_ratio": round(float(y_true.mean()), 6),
        "mean_score": round(float(y_score.mean()), 4),
    }