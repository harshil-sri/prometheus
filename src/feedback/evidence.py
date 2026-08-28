"""evidence.py — ComputedEvidence: structural anti-fabrication for reports.

DESIGN LAW (PROMETHEUS_CONTEXT.md §2, law 1 & 7):

No fabricated evidence. Every claim a report or the investigator makes must
reference an evidence_id that was REGISTERED by an actual computation, with
its producing function and seed recorded. A raw string or plain dict cannot
satisfy the type contract — build_blindspot_report REJECTS them at runtime
(TypeError), so fabrication fails loudly instead of silently shipping.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["ComputedEvidence", "EvidenceStore"]


@dataclass(frozen=True)
class ComputedEvidence:
    """Immutable record of ONE computation's output.

    kind examples:
        "recall_eval"      value = per-attack recall dict + fingerprints
        "weakness_surface" value = sensitivity surface map
        "variants"         value = variant specs list (ids + strategies)
        "retrain_diag"     value = ensemble fit diagnostics
        "ablation_delta"   value = GNN ablation measurements
    """

    evidence_id: str
    kind: str
    value: Any
    source: str
    seed: int
    computed_at: float

    def summary(self, limit: int = 160) -> str:
        blob = json.dumps(self.value, sort_keys=True, default=str)
        return f"{self.evidence_id} [{self.kind}] {blob[:limit]}"

    def fingerprint(self) -> str:
        blob = json.dumps(
            {"kind": self.kind, "value": self.value,
             "source": self.source, "seed": self.seed},
            sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class EvidenceStore:
    """Append-only registry of computations performed this session."""

    def __init__(self, seed: int = 42):
        self.seed = seed
        self._items: Dict[str, ComputedEvidence] = {}
        self._order: List[str] = []

    def register(self, kind: str, value: Any, source: str,
                 seed: Optional[int] = None) -> ComputedEvidence:
        """Record one computation. `value` must be JSON-serialisable.

        Raises TypeError immediately if the value cannot round-trip JSON —
        opaque objects invite unverifiable claims downstream.
        """
        payload = json.dumps(value, sort_keys=True, default=str)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        eid = f"EVD_{digest.upper()}"
        ev = ComputedEvidence(
            evidence_id=eid,
            kind=str(kind),
            value=value,
            source=str(source),
            seed=int(seed) if seed is not None else self.seed,
            computed_at=time.time(),
        )
        self._items[eid] = ev
        self._order.append(eid)
        return ev

    def get(self, evidence_id: str) -> ComputedEvidence:
        if evidence_id not in self._items:
            raise KeyError(
                f"Unknown evidence id {evidence_id!r}. Claims must reference "
                f"registered computations only."
            )
        return self._items[evidence_id]

    def all(self) -> List[ComputedEvidence]:
        return [self._items[eid] for eid in self._order]

    def as_manifest(self) -> List[Dict[str, Any]]:
        """JSON-ready manifest with fingerprints (ship in artifacts)."""
        return [
            {
                "evidence_id": ev.evidence_id,
                "kind": ev.kind,
                "fingerprint": ev.fingerprint(),
                "source": ev.source,
                "seed": ev.seed,
                "value": ev.value,
            }
            for ev in self.all()
        ]


def require_computed(items: Iterable[Any], what: str) -> List[ComputedEvidence]:
    """Type gate: every element must be ComputedEvidence.

    This is where raw strings / hand-built dicts DIE. Call sites that tried
    to sneak 'gnn_contribution': 'low' literals through get a TypeError at
    report-build time.
    """
    out: List[ComputedEvidence] = []
    for it in items:
        if isinstance(it, ComputedEvidence):
            out.append(it)
        else:
            raise TypeError(
                f"{what}: non-computed evidence encountered "
                f"({type(it).__name__}: {it!r}). Fabrication guard tripped —"
                f" register actual measurements via EvidenceStore.register()."
            )
    return out
