"""evidence_mapping.py — Deterministic E (external) and C (campaign) terms.

Maps investigator-produced evidence — sanctions screens, OSINT dossier risk
fields, and three-class memory campaign fingerprints — to bounded [0,1]
scalars that feed the structured score formula:

    R = w_t·T + w_g·G + w_b·B + w_e·E + w_c·C - w_u·U

These mappers are PURE and DETERMINISTIC (no LLM, no RNG): identical inputs
always produce identical scalars, every scalar is bounded in [0,1], and the
composed evidence is never invented — it is a documented fold of fields that
an investigator can point back to in the evidence store.
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["sanctions_evidence", "osint_evidence", "external_evidence",
           "campaign_evidence", "OSINT_RISK_LEVELS"]

# customer risk_state → bounded evidence contribution
OSINT_RISK_LEVELS = {"flagged": 0.9, "elevated": 0.6, "normal": 0.0}

# device-history sprawl: at/under this many distinct devices → 0 contribution;
# at/over DEVICE_EVIDENCE_RATIO devices → 1.0 evidence
DEVICE_EVIDENCE_FLOOR = 3
DEVICE_EVIDENCE_RATIO = 8.0

# analyst watch flags (e.g. new_domain / hosting_change) present on a dossier
WATCH_FLAG_EVIDENCE = 0.5

# a repeat fingerprint needs this many recurrences to saturate C evidence
RECURRENCE_SATURATION = 3


def sanctions_evidence(screens: List[Dict[str, Any]]) -> float:
    """Bounded [0,1] evidence from sanctions screens.

    WATCH_HIT screens contribute their bounding match_strength (0.72-0.98);
    the strongest hit dominates. No hits → 0.0.
    """
    strengths = [
        float(s["hit"]["match_strength"])
        for s in screens
        if s.get("result") == "WATCH_HIT" and s.get("hit")
    ]
    return round(min(1.0, max(strengths)), 4) if strengths else 0.0


def osint_evidence(dossiers: List[Dict[str, Any]]) -> float:
    """Bounded [0,1] evidence from OSINT dossiers.

    Risk state (flagged/elevated), device-history sprawl and analyst watch
    flags are folded deterministically. The strongest single indicator
    dominates (max-composition, documented) so one strong signal is enough.
    """
    subs: List[float] = []
    for d in dossiers:
        lvl = d.get("risk_state_at_enrichment")
        if lvl in OSINT_RISK_LEVELS:
            subs.append(OSINT_RISK_LEVELS[lvl])
        dev = (d.get("device_history") or {}).get("distinct_devices", 0)
        if isinstance(dev, (int, float)) and dev > DEVICE_EVIDENCE_FLOOR:
            subs.append(min(1.0, float(dev) / DEVICE_EVIDENCE_RATIO))
        if d.get("watch_flags"):
            subs.append(WATCH_FLAG_EVIDENCE)
    return round(min(1.0, max(subs)) if subs else 0.0, 4)


def external_evidence(dossiers: List[Dict[str, Any]],
                      screens: List[Dict[str, Any]]) -> float:
    """Combined bounded E term: max of sanctions and OSINT evidence.

    A strong hit on either axis is sufficient evidence.
    """
    return round(max(sanctions_evidence(screens), osint_evidence(dossiers)),
                 4)


def campaign_evidence(attack_signatures: Dict[str, Dict[str, Any]]) -> float:
    """Bounded [0,1] C term from repeated campaign fingerprints.

    A mechanism/signature repeating 2 times → 0.667, RECURRENCE_SATURATION
    (3) times → 1.0; the most-repeated pattern dominates. Single
    occurrences (recurrence == 1) contribute nothing.
    """
    recs = [float(v.get("recurrence", 0))
            for v in attack_signatures.values()
            if v.get("recurrence", 0) >= 2]
    if not recs:
        return 0.0
    return round(min(1.0, max(recs) / float(RECURRENCE_SATURATION)), 4)