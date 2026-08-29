"""eval/attribution.py — Mechanism × evidence-source attribution matrix.

Phase 7 panel 3. For each CAUGHT attack transaction in a world, attribute the
evidence sources that fired — model signals (XGB / GNN) per transaction from
the real ensemble, and entity-level signals (OSINT dossiers / sanctions
WATCH_HIT) per sender account from the live CaseManager providers — then fold
counts into a {mechanism → {source → n caught}} matrix with margins and rates.

Everything here is measured: every row traces to an actual world transaction
scored by the actual ensemble; no invented mechanism tags, no invented
evidence. A deterministic fingerprint covers the exact payload so the
committed exhibit can be verified byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Dict, List, Optional


MECHANISM_ORDER = ["rule_compiler", "shadow_pgd", "llm_strategist",
                   "protocol_structural", "genetic"]
SOURCE_ORDER = ["XGB", "GNN", "OSINT", "sanctions"]
SCHEMA = "prometheus.attribution.v1"


def mechanism_label(raw: Any) -> str:
    """Map any tx mechanism tag onto the declared mechanism vocabulary.
    Unknown/absent tags stay honest as 'other' rather than being renamed."""
    if not raw:
        return "other"
    label = str(raw)
    for m in MECHANISM_ORDER:
        if label == m:
            return m
    return "other"


def _fingerprint(payload: Dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def build_attribution_matrix(
    world,
    victim,
    case_manager=None,
    threshold: float = 0.5,
    max_rows: int = 4000,
    seed: int = 42,
) -> Dict[str, Any]:
    """Attribute caught fraud transactions in `world`.

    victim: BlueTeamEnsemble (used for per-tx probabilities and signals).
    case_manager: optional CaseManager whose fixtures/sanctions agent flags
        sender accounts; when None, OSINT/sanctions cells stay zero (honest).
    """
    fraud = [t for t in world.transactions if t.get("is_fraud")]
    if not fraud:
        return _empty(threshold=threshold, seed=seed)

    probs = victim.score_transactions(fraud, world)
    caught_rows: List[tuple] = [
        (t, float(p)) for t, p in zip(fraud, probs)
        if float(p) >= threshold
    ][:max_rows]
    rows = [t for t, _ in caught_rows]

    signals = victim.score_all_signals(rows, world, manifold=None)
    xgb = signals.get("xgb")
    gnn = signals.get("gnn")

    senders = sorted({str(t.get("from")) for t in rows
                      if str(t.get("from", "")).startswith("ACC_")})
    flags: Dict[str, Dict] = (
        case_manager.evidence_flags_for(senders)
        if case_manager is not None and senders
        else {s: {"osint": False, "sanctions": False} for s in senders})

    counts: Dict[str, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int))
    totals: Dict[str, int] = defaultdict(int)
    hits = {"osint": 0, "sanctions": 0}

    for i, (t, _p) in enumerate(caught_rows):
        mech: str = mechanism_label(t.get("mechanism"))
        totals[mech] += 1
        if xgb is not None and len(xgb) > i and float(xgb[i]) >= threshold:
            counts[mech]["XGB"] += 1
        if gnn is not None and len(gnn) > i and float(gnn[i]) >= threshold:
            counts[mech]["GNN"] += 1
        f = flags.get(str(t.get("from")),
                      {"osint": False, "sanctions": False})
        if f.get("osint"):
            counts[mech]["OSINT"] += 1
            hits["osint"] += 1
        if f.get("sanctions"):
            counts[mech]["sanctions"] += 1
            hits["sanctions"] += 1

    mechanisms_present = [m for m in MECHANISM_ORDER if totals.get(m)]
    if totals.get("other"):
        mechanisms_present.append("other")

    matrix = {
        m: {s: int(counts[m].get(s, 0)) for s in SOURCE_ORDER}
        for m in mechanisms_present
    }
    rates = {
        m: {s: round(matrix[m][s] / totals[m], 4) if totals[m] else 0.0
            for s in SOURCE_ORDER}
        for m in mechanisms_present
    }

    fp_payload = {
        "schema": SCHEMA,
        "seed": seed,
        "threshold": threshold,
        "mechanisms": mechanisms_present,
        "sources": SOURCE_ORDER,
        "caught": totals,
        "matrix": matrix,
    }

    return {
        "schema": SCHEMA,
        "threshold": threshold,
        "seed": seed,
        "caught_attributed": int(sum(totals.values())),
        "mechanisms": mechanisms_present,
        "sources": SOURCE_ORDER,
        "matrix": matrix,
        "margins": {m: totals[m] for m in mechanisms_present},
        "rates": rates,
        "coverage": {
            "accounts_screened": int(len(senders)),
            "osint_flagged_accounts": int(hits["osint"]),
            "sanctions_hits": int(hits["sanctions"]),
            "n_txs_attributed": int(len(rows)),
            "seed": seed,
        },
        "fingerprint": _fingerprint(fp_payload),
    }


def combine_matrices(worlds: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Fold per-world attribution matrices into one combined exhibit.

    Columns are summed per (mechanism, source) so protocol_structural rows
    from the agentic world join the twin-world mechanisms on the SAME scale
    (both count caught attacks). Per-world provenance is preserved for the
    anti-fabrication trail.
    """
    seed = next((v.get("seed") for v in worlds.values()
                 if v.get("seed") is not None), 42)
    threshold = next((float(v["threshold"]) for v in worlds.values()
                      if v.get("threshold") is not None), 0.5)

    combined: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    margins: Dict[str, int] = defaultdict(int)
    world_log: Dict[str, Dict[str, Any]] = {}
    for name, m in worlds.items():
        world_log[name] = {
            "caught_attributed": m.get("caught_attributed", 0),
            "mechanisms": m.get("mechanisms", []),
            "fingerprint": m.get("fingerprint", ""),
        }
        for mech, cells in (m.get("matrix") or {}).items():
            for s, c in cells.items():
                combined[mech][s] += int(c)
            margins[mech] += int(m.get("margins", {}).get(mech, 0))

    mechanisms_present = [m for m in MECHANISM_ORDER
                          if margins.get(m)]
    if margins.get("other"):
        mechanisms_present.append("other")

    matrix = {
        m: {s: int(combined[m].get(s, 0)) for s in SOURCE_ORDER}
        for m in mechanisms_present
    }
    rates = {
        m: {s: round(matrix[m][s] / margins[m], 4)
            if margins.get(m) else 0.0
            for s in SOURCE_ORDER}
        for m in mechanisms_present
    }

    fp_payload = {
        "schema": SCHEMA,
        "seed": seed,
        "threshold": threshold,
        "mechanisms": mechanisms_present,
        "sources": SOURCE_ORDER,
        "caught": dict(margins),
        "matrix": matrix,
        "worlds": world_log,
    }
    return {
        "schema": SCHEMA,
        "threshold": threshold,
        "seed": seed,
        "caught_attributed": int(sum(margins.values())),
        "mechanisms": mechanisms_present,
        "sources": SOURCE_ORDER,
        "matrix": matrix,
        "margins": {m: int(margins[m]) for m in mechanisms_present},
        "rates": rates,
        "worlds": world_log,
        "fingerprint": _fingerprint(fp_payload),
    }


def _empty(threshold: float, seed: int) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "threshold": threshold,
        "seed": seed,
        "caught_attributed": 0,
        "mechanisms": [],
        "sources": SOURCE_ORDER,
        "matrix": {},
        "margins": {},
        "rates": {},
        "coverage": {"accounts_screened": 0, "osint_flagged_accounts": 0,
                     "sanctions_hits": 0, "n_txs_attributed": 0,
                     "seed": seed},
        "fingerprint": _fingerprint({"schema": SCHEMA, "seed": seed,
                                     "threshold": threshold,
                                     "mechanisms": [], "sources": SOURCE_ORDER,
                                     "caught": {}, "matrix": {}}),
    }