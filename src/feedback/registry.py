"""registry.py — Strategy/Model registries and the exploitability estimate.

DESIGN LAW (§2 law 8): every blue-model version and red strategy is
registered; the final "exploitability" figure is computed as the WORST-CASE
recall across the whole attack population — i.e. the defense's weakest link,
not an average that hides holes.

Exploitability (attackers' view) = max over strategies of their best miss
rate. Detection robustness (defenders' view, what we ship in reports) =
min over types of worst-case detection across strategies.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["StrategyRegistry", "exploitability_estimate"]


@dataclass
class StrategyRecord:
    strategy_id: str
    mechanism: str
    meta: Dict[str, Any]
    metrics: Dict[str, Any]
    registered_at: float = field(default_factory=time.time)
    fingerprint: str = ""

    def __post_init__(self):
        blob = json.dumps({"mechanism": self.mechanism,
                           "meta": self.meta,
                           "metrics": self.metrics},
                          sort_keys=True, default=str)
        self.fingerprint = hashlib.sha256(blob.encode()).hexdigest()[:16]


class StrategyRegistry:
    """Append-only registry of red-team strategies (and blue model versions)."""

    def __init__(self):
        self._records: Dict[str, StrategyRecord] = {}
        self._history: List[str] = []

    def register(self, strategy_id: str, mechanism: str,
                 meta: Dict[str, Any], metrics: Dict[str, Any]) -> StrategyRecord:
        rec = StrategyRecord(strategy_id=strategy_id, mechanism=mechanism,
                             meta=meta, metrics=metrics)
        self._records[strategy_id] = rec
        self._history.append(strategy_id)
        return rec

    def get(self, strategy_id: str) -> StrategyRecord:
        if strategy_id not in self._records:
            raise KeyError(f"unknown strategy {strategy_id!r}")
        return self._records[strategy_id]

    def all(self) -> List[StrategyRecord]:
        return [self._records[sid] for sid in self._history]

    def manifest(self) -> List[Dict[str, Any]]:
        return [
            {"strategy_id": r.strategy_id, "mechanism": r.mechanism,
             "fingerprint": r.fingerprint, "registered_at": r.registered_at,
             "meta": r.meta, "metrics": r.metrics}
            for r in self.all()
        ]


def exploitability_estimate(
        detection_matrix: Dict[str, Dict[str, float]],
        ) -> Dict[str, Any]:
    """Compute the honest worst-case picture from a matrix
    {strategy_id: {attack_type: detection_rate}}.

    Returns:
        worst_case_detection_per_type : min rate per type over strategies
        overall_worst_case_detection  : min over all cells (the weak link)
        overall_exploitability        : 1 − that worst-case detection
        mean_detection                : transparency companion number
        strongest_attack              : argmin-detection strategy per type
    """
    if not detection_matrix:
        raise ValueError("empty detection matrix")

    types = sorted({t for cell in detection_matrix.values() for t in cell})
    per_type_min: Dict[str, float] = {}
    strongest: Dict[str, str] = {}

    for t in types:
        rates = {sid: float(cell.get(t, 1.0))
                 for sid, cell in detection_matrix.items()}
        worst_sid = min(rates, key=rates.get)      # type: ignore[arg-type]
        per_type_min[t] = round(rates[worst_sid], 4)
        strongest[t] = worst_sid

    all_rates = [v for cell in detection_matrix.values() for v in cell.values()]
    overall_worst = round(min(all_rates), 4)

    return {
        "worst_case_detection_per_type": per_type_min,
        "strongest_attack_per_type": strongest,
        "overall_worst_case_detection": overall_worst,
        "overall_exploitability": round(1.0 - overall_worst, 4),
        "mean_detection": round(sum(all_rates) / len(all_rates), 4),
        "note": ("exploitability is defined on the WORST case (law 8): "
                 "averages hide the hole an attacker would find"),
    }
