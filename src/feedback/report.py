"""Blind-Spot Report utilities."""

import json


def format_report(report: dict) -> str:
    """Format a Blind-Spot Report for display."""
    lines = [
        "=" * 60,
        "BLIND-SPOT REPORT",
        "=" * 60,
        f"Blind spot:       {report.get('blind_spot', 'unknown')}",
        f"Recall before:    {report.get('recall_before', 0):.2%}",
        f"Recall after:     {report.get('recall_after', 0):.2%}",
        f"Generalization:   {report.get('generalization_recall_unseen_generator', 'N/A')}",
        f"Fixes generated:  {report.get('generated_fixes', 0)}",
        f"Retrain rounds:   {report.get('retrain_rounds_used', 0)} / {report.get('max_retrain_rounds', 2)}",
        "-" * 60,
        "Evidence breakdown:",
    ]
    for k, v in report.get('evidence', {}).items():
        lines.append(f"  {k}: {v}")
    lines.append("=" * 60)
    return "\n".join(lines)


def report_to_dict(report: dict) -> dict:
    """Return a clean dict suitable for JSON serialization."""
    return {
        "blind_spot": report.get("blind_spot", "unknown"),
        "recall_before": round(report.get("recall_before", 0), 4),
        "recall_after": round(report.get("recall_after", 0), 4),
        "generalization_recall": round(report.get("generalization_recall_unseen_generator", 0), 4) if report.get("generalization_recall_unseen_generator") is not None else None,
        "generated_fixes": report.get("generated_fixes", 0),
        "retrain_rounds": report.get("retrain_rounds_used", 0),
        "max_retrain_rounds": report.get("max_retrain_rounds", 2),
        "evidence": report.get("evidence", {}),
    }