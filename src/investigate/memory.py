"""memory.py — Three-class investigator memory (Phase 8).

    case_records     closed investigations: tx set, evidence ids, verdict
    attack_signatures  generator fingerprints seen before (genome/spec digests)
    defender_notes   learned heuristics / policy notes, tagged by phase

Persisted as JSON at artifacts/memory/three_class.json when the caller opts
in (`save()`), so tests and demos stay hermetic by default. All appends are
deduplicated on content hash; nothing here invents entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(ROOT, "artifacts", "memory", "three_class.json")

__all__ = ["ThreeClassMemory", "digest_of"]


def digest_of(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


class ThreeClassMemory:
    def __init__(self):
        self.case_records: Dict[str, Dict[str, Any]] = {}
        self.attack_signatures: Dict[str, Dict[str, Any]] = {}
        self.defender_notes: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def remember_case(self, case_id: str, payload: Dict[str, Any]) -> str:
        dg = digest_of({"case_id": case_id, **payload})
        self.case_records[case_id] = {
            "payload": payload,
            "digest": dg,
            "recorded_at": time.time(),
        }
        return dg

    def remember_attack_signature(self, mechanism: str,
                                  signature_payload: Dict[str, Any],
                                  ) -> str:
        key = f"{mechanism}:{digest_of(signature_payload)}"
        if key not in self.attack_signatures:
            self.attack_signatures[key] = {
                "mechanism": mechanism,
                "signature": signature_payload,
                "first_seen_at": time.time(),
                "recurrence": 0,
            }
        self.attack_signatures[key]["recurrence"] += 1
        return key

    def add_defender_note(self, note: str, phase: str) -> int:
        entry = {"note": str(note)[:400], "phase": phase,
                 "digest": digest_of({"note": note, "phase": phase}),
                 "at": time.time()}
        for e in self.defender_notes:
            if e["digest"] == entry["digest"]:
                return len(self.defender_notes)
        self.defender_notes.append(entry)
        return len(self.defender_notes)

    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_records": dict(self.case_records),
            "attack_signatures": dict(self.attack_signatures),
            "defender_notes": list(self.defender_notes),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ThreeClassMemory":
        m = cls()
        m.case_records = dict(d.get("case_records", {}))
        m.attack_signatures = dict(d.get("attack_signatures", {}))
        m.defender_notes = list(d.get("defender_notes", []))
        return m

    def save(self, path: Optional[str] = None) -> str:
        path = path or DEFAULT_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        return path

    @classmethod
    def load(cls, path: Optional[str] = None) -> "ThreeClassMemory":
        path = path or DEFAULT_PATH
        if not os.path.exists(path):
            return cls()
        with open(path) as f:
            return cls.from_dict(json.load(f))
