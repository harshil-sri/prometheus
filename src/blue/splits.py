"""splits.py — Two-axis holdout enforcement for Project Prometheus.

DESIGN LAW (PROMETHEUS_CONTEXT.md §2, law 2 & 3):

Detection may be evaluated only against a holdout locked on BOTH axes:

  Axis TYPE      — attack types (e.g. A2 synthetic identity, A5
                   scatter-gather layering) that are NEVER trained on.
  Axis MECHANISM — generation mechanisms (rule_compiler today; shadow_pgd /
                   genetic / llm_strategist once P4/P5 land) that are never
                   part of the training population.

The lock is computed at split time and hashed (sha256 of sorted membership)
into a fingerprint artifact, so any later attempt to quietly re-scope the
holdout shows up as a fingerprint mismatch.

Mechanism tagging: transactions carry an optional 'mechanism' key. Absent
tags are attributed to 'rule_compiler' (the only mechanism currently wired).
When new mechanisms arrive (compiler executes them), they must set tx['mechanism'];
`register_mechanism` adds them to the namespace and `lock_holdout` hashes them.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

__all__ = [
    "HoldoutSpec",
    "MECHANISM_REGISTRY",
    "DEFAULT_MECHANISM",
    "register_mechanism",
    "mechanism_of_tx",
    "attack_type_of_tx",
    "lock_holdout",
    "load_holdout_spec",
    "assert_no_leakage",
    "split_by_step",
]

# ---------------------------------------------------------------------------
# Mechanism registry (axis 2 namespace)
# ---------------------------------------------------------------------------

MECHANISM_REGISTRY: Set[str] = {"rule_compiler", "shadow_pgd"}
"""Every generation mechanism ever allowed to write into a twin.

shadow_pgd is pre-registered for Phase 4 (distill→PGD pipeline); the other
mechanisms join their implementing phases via register_mechanism()."""

DEFAULT_MECHANISM = "rule_compiler"
"""Attribution when a fraud tx carries no mechanism tag."""


def register_mechanism(name: str) -> str:
    """Register a new generation mechanism name in the axis-2 namespace."""
    name = str(name)
    if not name:
        raise ValueError("mechanism name must be non-empty")
    MECHANISM_REGISTRY.add(name)
    return name


def mechanism_of_tx(tx: Dict) -> str:
    """Mechanism attribution for one transaction."""
    mech = tx.get("mechanism")
    return DEFAULT_MECHANISM if not mech else str(mech)


def attack_type_of_tx(tx: Dict) -> Optional[str]:
    """Attack-type attribution (A1..A6 codes from the compiler/benchmarks)."""
    aid = tx.get("attack_id")
    if not aid:
        return None
    aid = str(aid)
    # benchmark ids are exact; trajectory/variant ids embed them (A1_v3 etc.)
    if aid in _KNOWN_TYPES:
        return aid
    for code in sorted(_KNOWN_TYPES, key=len, reverse=True):
        if aid.startswith(code + "_") or aid.startswith(code + "-"):
            return code
    return None


_KNOWN_TYPES: Set[str] = {"A1", "A2", "A3", "A4", "A5", "A6"}


def register_attack_types(codes: Iterable[str]) -> None:
    """Extend the known attack-type namespace (called by benchmark_attacks)."""
    _KNOWN_TYPES.update(str(c) for c in codes)


# ---------------------------------------------------------------------------
# Holdout specification
# ---------------------------------------------------------------------------

@dataclass
class HoldoutSpec:
    """Immutable description of the two-axis holdout."""

    held_out_types: frozenset
    held_out_mechanisms: frozenset
    seed: int
    fingerprint: str
    locked_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "held_out_types": sorted(self.held_out_types),
            "held_out_mechanisms": sorted(self.held_out_mechanisms),
            "seed": self.seed,
            "fingerprint": self.fingerprint,
            "locked_at": self.locked_at,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "HoldoutSpec":
        return cls(
            held_out_types=frozenset(d["held_out_types"]),
            held_out_mechanisms=frozenset(d["held_out_mechanisms"]),
            seed=int(d["seed"]),
            fingerprint=d["fingerprint"],
            locked_at=float(d.get("locked_at", 0.0)),
        )


def _fingerprint(held_types: Iterable[str], held_mechs: Iterable[str],
                 seed: int) -> str:
    """sha256 over sorted membership of both axes plus seed.

    Deterministic across processes/platforms: the lock travels with artifacts,
    so a mismatch is detectable without trusting any session state.
    """
    payload = {
        "types": sorted(map(str, held_types)),
        "mechanisms": sorted(map(str, held_mechs)),
        "seed": int(seed),
        "schema": "prometheus.holdout.v1",
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def lock_holdout(seed: int = 42,
                 held_out_types: Iterable[str] = ("A2", "A5"),
                 held_out_mechanisms: Iterable[str] = ()) -> HoldoutSpec:
    """Lock the two-axis holdout. Deterministic; re-locking with identical
    arguments yields the identical fingerprint."""
    types = frozenset(map(str, held_out_types))
    mechs = frozenset(map(str, held_out_mechanisms))

    unknown = mechs - MECHANISM_REGISTRY
    if unknown:
        raise ValueError(
            f"held-out mechanisms not registered: {unknown}. "
            f"Call register_mechanism() first."
        )

    spec = HoldoutSpec(
        held_out_types=types,
        held_out_mechanisms=mechs,
        seed=seed,
        fingerprint=_fingerprint(types, mechs, seed),
    )
    return spec


def load_holdout_spec(path_or_dict) -> HoldoutSpec:
    """Load + verify a fingerprinted HoldoutSpec from a dict or JSON file."""
    if isinstance(path_or_dict, str):
        with open(path_or_dict, "r") as f:
            d = json.load(f)
    else:
        d = dict(path_or_dict)

    spec = HoldoutSpec.from_dict(d)
    expected = _fingerprint(spec.held_out_types, spec.held_out_mechanisms,
                            spec.seed)
    if expected != spec.fingerprint:
        raise AssertionError(
            f"Holdout fingerprint mismatch! Stored {spec.fingerprint} but "
            f"axes hash to {expected}. The holdout was tampered with or "
            f"rescoped after locking."
        )
    return spec


# ---------------------------------------------------------------------------
# Leakage enforcement
# ---------------------------------------------------------------------------

def assert_no_leakage(transactions: List[Dict], spec: HoldoutSpec) -> None:
    """Raise if ANY transaction violates the two-axis holdout.

    Checks every tx (fraud or not — normal rows carry no type/mechanism and
    pass trivially):
      * its attack type must NOT be in spec.held_out_types;
      * its mechanism must NOT be in spec.held_out_mechanisms.
    Violations are collected and reported together for debuggability.
    """
    type_hits: List[str] = []
    mech_hits: List[str] = []

    for tx in transactions:
        atype = attack_type_of_tx(tx)
        if atype and atype in spec.held_out_types:
            type_hits.append(str(tx.get("tx_id", "?")))

        mech = mechanism_of_tx(tx) if tx.get("is_fraud") else None
        if mech and mech in spec.held_out_mechanisms:
            mech_hits.append(str(tx.get("tx_id", "?")))

    if type_hits or mech_hits:
        raise AssertionError(
            f"TWO-AXIS HOLDOUT LEAKAGE: "
            f"types {sorted(spec.held_out_types)} leaked into "
            f"{len(type_hits)} txs ({type_hits[:10]}...); "
            f"mechanisms {sorted(spec.held_out_mechanisms)} leaked into "
            f"{len(mech_hits)} txs ({mech_hits[:10]}...)."
        )


def split_by_step(transactions: List[Dict], eval_fraction: float = 0.3,
                  min_eval_steps: int = 10):
    """Temporal train/eval boundary. Train = earlier steps, Eval = later.

    Returns (train_indices, eval_indices). Temporal splitting mirrors real
    deployment (models are fit on history and scored forward), which is why
    it pairs cleanly with the two-axis holdout instead of random shuffles.
    """
    if not 0.0 < eval_fraction < 1.0:
        raise ValueError("eval_fraction must be in (0, 1)")
    steps = [int(t.get("step", 0)) for t in transactions]
    lo, hi = (min(steps), max(steps)) if steps else (0, -1)
    span = hi - lo
    cut_step = lo + max(int(span * (1.0 - eval_fraction)), min_eval_steps)

    train_idx = [i for i, s in enumerate(steps) if s < cut_step]
    eval_idx = [i for i, s in enumerate(steps) if s >= cut_step]
    return train_idx, eval_idx
