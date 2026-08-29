"""feedback/timeline.py — persisted Blind-Spot timeline (Phase 7 panel 1).

One append-only summary row per completed feedback cycle, capped retention,
no wall-clock fields so regenerated artifacts are byte-identical (Determinism
law). Shared by the live demo (`main.py` /api/demo/run) and the deterministic
generator (scripts/timeline_eval.py), so committed and session rows share one
schema.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

SCHEMA = "prometheus.feedback_timeline.v1"
MAX_ENTRIES = 30


def summarize_cycle(report: Dict, seed: Optional[int] = None,
                    source: str = "cycle") -> Dict:
    """Fold a Blind-Spot Report into one deterministic timeline row."""
    return {
        "source": source,
        "seed_used": seed,
        "blind_spot": report.get("blind_spot", "none"),
        "recall_before": round(float(report.get("recall_before", 0.0)), 4),
        "recall_after": round(float(report.get("recall_after", 0.0)), 4),
        "improved": bool(report.get("improved", False)),
        "generated_fixes": int(report.get("generated_fixes", 0)),
        "retrain_rounds_used": int(report.get("retrain_rounds_used", 0)),
        "max_retrain_rounds": int(report.get("max_retrain_rounds", 2)),
        "generalization_recall_unseen_generator":
            report.get("generalization_recall_unseen_generator"),
        "evidence_ids": list(report.get("evidence_ids", [])),
    }


class FeedbackTimeline:
    """File-backed, append-only cycle history with a hard retention cap."""

    def __init__(self, path: str, max_entries: int = MAX_ENTRIES):
        self.path = path
        self.max_entries = int(max_entries)
        self._entries: List[Dict] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as f:
                blob = json.load(f)
            entries = blob.get("entries", [])
            if isinstance(entries, list):
                self._entries = [
                    e for e in entries if isinstance(e, dict) and
                    isinstance(e.get("idx"), int)
                ]
                self._entries.sort(key=lambda e: e["idx"])
        except Exception:                                 # noqa: BLE001
            self._entries = []

    def entries(self) -> List[Dict]:
        return list(self._entries)

    def count(self) -> int:
        return len(self._entries)

    def append(self, summary: Dict) -> int:
        """Append one row (capped), persist, return the assigned index."""
        idx = self._entries[-1]["idx"] + 1 if self._entries else 0
        row = {"idx": idx, **{k: v for k, v in summary.items()}}
        self._entries.append(row)
        if len(self._entries) > self.max_entries:
            self._entries = self._entries[-self.max_entries:]
        self._save()
        return idx

    def _save(self) -> None:
        blob = {
            "schema": SCHEMA,
            "max_entries": self.max_entries,
            "entries": self._entries,
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(blob, f, indent=2, default=str)
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, path: str) -> "FeedbackTimeline":
        return cls(path)