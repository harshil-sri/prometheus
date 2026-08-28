"""evidence_store.py — Case-scoped EvidenceStore with provenance chaining.

Reuses the single anti-fabrication core (feedback.evidence.ComputedEvidence /
EvidenceStore) so investigator reports and feedback-loop reports share ONE
evidence vocabulary (law 7: every claim cites a registered computation).

Adds case binding + an integrity chain digest so later tampering with the
manifest is detectable.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List

from feedback.evidence import ComputedEvidence, EvidenceStore  # noqa: F401

__all__ = ["CaseEvidence", "INTEGRITY_ALGO"]

INTEGRITY_ALGO = "sha256"


class CaseEvidence(EvidenceStore):
    """Evidence store bound to one validated case id."""

    def __init__(self, case_id: str, seed: int = 42):
        from .guardrails import validate_case_id
        self.case_id = validate_case_id(case_id)
        super().__init__(seed=seed)
        self.opened_at = time.time()

    def integrity_digest(self) -> str:
        """Order-sensitive chain over registered evidence fingerprints."""
        h = hashlib.new(INTEGRITY_ALGO)
        for ev in self.all():
            h.update(ev.evidence_id.encode())
            h.update(ev.fingerprint().encode())
        return f"{INTEGRITY_ALGO}:{h.hexdigest()[:24]}"

    def manifest(self) -> Dict[str, Any]:
        m = super().as_manifest()
        return {
            "case_id": self.case_id,
            "opened_at": self.opened_at,
            "integrity": self.integrity_digest(),
            "items": m,
        }
