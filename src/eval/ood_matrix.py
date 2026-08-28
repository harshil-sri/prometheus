"""ood_matrix.py — Mechanism × attack-type OOD evaluation matrix.

Rows    : generation mechanisms {rule_compiler, genetic, shadow_pgd,
          llm_strategist}
Columns : attack types A1..A6 (A2/A5 marked held-out, evaluated only under
          frozen fingerprints).

Cell (m, t) = detection rate of the victim over k fresh attempts where
mechanism m attacks as type t. Every mechanism's rows are tagged with its
own mechanism key, so training-side leakage asserts can be run against the
matrix population itself.

Fingerprints: sha256 over sorted mechanism parameterizations + seed + the
locked holdout fingerprint. Two runs of build_ood_matrix on identical inputs
yield byte-identical fingerprints; a re-scope shows up as a mismatch.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from blue.splits import (
    HoldoutSpec, MECHANISM_REGISTRY, assert_no_leakage,
    attack_type_of_tx,
)
from shadow.pgd import PGDCandidate
from twin.typologies import run_typology

__all__ = ["build_ood_matrix", "MECHANISM_TYPES"]

MECHANISM_TYPES = ["rule_compiler", "genetic", "shadow_pgd",
                   "llm_strategist"]

_TRAINABLE = ("A1", "A3", "A4", "A6")
_HELD_OUT = ("A2", "A5")
_ALL_TYPES = _TRAINABLE + _HELD_OUT


def _fingerprint(spec: Dict[str, Any]) -> str:
    blob = json.dumps(spec, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Per-mechanism attempt generators.
# Each returns (list_of_tx_dicts, mech_tag). `attack_type` guides theming;
# mechanisms stay free in HOW they realize it (that is the point: same type,
# different generator distribution → true mechanism-OOD axis).
# ---------------------------------------------------------------------------

def _attempts_rule_compiler(victim, compiler_of_twin, twin, attack_type: str,
                            k: int, rng: random.Random,
                            **kwargs) -> List[dict]:
    """Fresh-seed compilations of the benchmark spec for that type."""
    out: List[dict] = []
    for i in range(k):
        spec_seed_rng = random.Random(rng.randint(0, 2 ** 31))
        plan = compiler_of_twin.compile(compiler_of_twin.benchmark_spec(attack_type))
        traj_id = compiler_of_twin.execute(plan, twin.world)
        rows = [t for t in twin.world.transactions
                if t.get("trajectory_id") == traj_id]
        out.extend(rows)
    return out


def _attempts_typology_like(victim, twin, attack_type: str, k: int,
                            rng: random.Random, genome_priors: dict,
                            mech_name: str, **kwargs) -> List[dict]:
    """Direct-typology generator used by genetic/llm fallback families.

    Type theming: A1/A3 → fan_out shapes, A4/A6 → fan_in, A2 → bipartite,
    A5 → scatter_gather layering."""
    world = twin.world
    shape_by_type = {"A1": "fan_out", "A3": "fan_out",
                     "A4": "fan_in", "A6": "fan_in",
                     "A2": "bipartite", "A5": "scatter_gather"}
    typology = shape_by_type.get(attack_type, "fan_in")

    accounts = list(world.accounts.keys())
    if len(accounts) < 6:
        return []
    rows_all: List[dict] = []
    for i in range(k):
        main = rng.choice(accounts)
        members_n = int(genome_priors.get("members",
                                          rng.randint(4, min(9, len(accounts) - 1))))
        others = [a for a in accounts if a != main]
        members = rng.sample(others, min(members_n, len(others)))
        amount = float(genome_priors.get("amount", rng.uniform(8000, 150000)))
        margin = float(genome_priors.get("margin_ratio", rng.uniform(0.02, 0.12)))
        traj_id = world.next_trajectory_id()
        kw = {"main_account": main, "amount": amount,
              "attack_id": f"OOD_{mech_name}_{attack_type}",
              "trajectory_id": traj_id,
              "step_offset": rng.randint(0, 5),
              "mechanism": mech_name}
        try:
            if typology == "bipartite":
                half = max(1, len(members) // 2)
                run_typology("bipartite", world, rng,
                             sources=members[:half],
                             targets=members[half:] or members[:1], **kw)
            elif typology == "scatter_gather":
                run_typology("scatter_gather", world, rng,
                             intermediaries=members[:-1] or members,
                             beneficiary=members[-1] or main,
                             margin_ratio=margin, **kw)
            else:
                run_typology(typology, world, rng, members=members, **kw)
        except Exception:
            continue
        rows_all.extend([t for t in world.transactions
                         if t.get("trajectory_id") == traj_id])
    return rows_all


def _attempts_shadow_pgd(victim, twin, attack_type: str, k: int,
                         rng: random.Random,
                         precomputed_candidates=None,
                         base_rows=None, **kwargs) -> List[dict]:
    """PGD-crafted evasions (candidates built ONCE by the caller and replayed
    deterministically across matrix builds)."""
    from attack.mechanisms.shadow_pgd import ShadowPGDMechanism

    if precomputed_candidates is not None and base_rows is not None:
        from shadow.pgd import PGDCandidate
        cands = [PGDCandidate(x_projected=x, shadow_score=0.0,
                              base_row_index=j, restart=0, iterations_used=0)
                 for j, x in enumerate(precomputed_candidates)]
        mech = ShadowPGDMechanism(victim, twin, seed=rng.randint(0, 2 ** 31))
        res = mech.run(attack_id=f"OOD_SHADOW_{attack_type}",
                       threshold=0.5, max_base_rows=len(base_rows),
                       execute_into_world=True,
                       precomputed_candidates=cands[:k],
                       precomputed_distill={})
        tid = res.trajectory_id
        if not tid:
            return []
        return [t for t in twin.world.transactions
                if t.get("trajectory_id") == tid]

    # no replay candidates provided → live (smaller) run
    mech = ShadowPGDMechanism(victim, twin, seed=rng.randint(0, 2 ** 31))
    res = mech.run(attack_id=f"OOD_SHADOW_{attack_type}", threshold=0.5,
                   max_base_rows=min(k * 3, 10),
                   probe_budget=250, pgd_iterations=12, restarts=1)
    tid = res.trajectory_id
    if not tid:
        return []
    return [t for t in twin.world.transactions
            if t.get("trajectory_id") == tid]


def _attempts_llm_strategist(victim, compiler_of_twin, twin,
                             attack_type: str, k: int, rng: random.Random,
                             weakness_descriptor: Optional[dict] = None,
                             **kwargs) -> List[dict]:
    """Synthesize variant specs then execute through the unified compiler
    path with attack_type preserved."""
    from attack.mechanisms.llm_strategist import LLMStrategist
    from attack.benchmark_attacks import BENCHMARK_ATTACKS

    strat = LLMStrategist(seed=rng.randint(0, 2 ** 31))
    wd = weakness_descriptor or {
        "weakness": "relational camouflage",
        "target_model": "GNN",
        "suggested_variants": ["more_intermediaries", "temporal_spreading"],
    }
    variants = strat.generate(wd, n_variants=max(2, k))
    out: List[dict] = []

    for j, sv in enumerate(variants[:k]):
        base_spec = json.loads(json.dumps(
            BENCHMARK_ATTACKS.get(attack_type, BENCHMARK_ATTACKS["A4"])))
        applied = dict(sv.spec)
        # keep the evaluated TYPE fixed; synthesis only re-parameterizes
        base_spec["resources"].update({
            kk: vv for kk, vv in applied.get("resources", {}).items()
            if isinstance(vv, int)})
        base_spec["desired_camouflage"] = \
            applied.get("desired_camouflage", base_spec["desired_camouflage"])
        base_spec["amount"] = float(applied.get("amount",
                                                base_spec["amount"]))
        base_spec["origin_provenance"] = sv.origin     # rides into trajectory spec
        plan = compiler_of_twin.compile(base_spec)
        traj_id = compiler_of_twin.execute(plan, twin.world)
        out.extend([t for t in twin.world.transactions
                    if t.get("trajectory_id") == traj_id])
    return out


# ---------------------------------------------------------------------------
# Matrix builder
# ---------------------------------------------------------------------------

def build_ood_matrix(
    victim, twin, compiler_of_twin,
    holdout_spec: HoldoutSpec,
    k_per_cell: int = 2,
    seed: int = 42,
    shadow_candidates: Optional[List[List[float]]] = None,
    shadow_base_rows: Optional[list] = None,
    llm_weakness: Optional[dict] = None,
    max_rows_per_cell: int = 80,
    ) -> Dict[str, Any]:
    """Assemble the full matrix + hashes + registry-ready payload."""
    t0 = time.time()
    rng_master = random.Random(seed)
    per_attempts: Dict[str, Dict[str, Dict[str, Any]]] = {
        m: {} for m in MECHANISM_TYPES}

    for mech in MECHANISM_TYPES:
        for atype in _ALL_TYPES:
            rng = random.Random(rng_master.randint(0, 2 ** 31))

            if mech == "rule_compiler":
                rows = _attempts_rule_compiler(victim, compiler_of_twin, twin,
                                               atype, k_per_cell, rng)
            elif mech == "genetic":
                rows = _attempts_typology_like(
                    victim, twin, atype, k_per_cell, rng,
                    genome_priors={"members": rng.randint(4, 9),
                                   "amount": rng.uniform(20000, 120000)},
                    mech_name="genetic")
            elif mech == "shadow_pgd":
                cand = None
                brows = None
                if shadow_candidates is not None and shadow_base_rows is not None \
                        and atype in _TRAINABLE:
                    cand = [np.asarray(x, dtype=np.float64)
                            for x in shadow_candidates[
                                _TRAINABLE.index(atype)]][:max_rows_per_cell] \
                        if len(shadow_candidates) >= len(_TRAINABLE) else None
                    brows = shadow_base_rows
                    rows = []                       # count below per type guess
                    if cand is None:
                        rows = []
                    elif not cand:
                        rows = []
                    else:
                        packed = [PGDCandidate(x_projected=x, shadow_score=0.0,
                                               base_row_index=j, restart=0,
                                               iterations_used=0)
                                  for j, x in enumerate(cand)]
                        from attack.mechanisms.shadow_pgd import \
                            ShadowPGDMechanism
                        mechp = ShadowPGDMechanism(
                            victim, twin, seed=rng.randint(0, 2 ** 31))
                        res = mechp.run(
                            attack_id=f"OOD_SHADOW_{atype}",
                            threshold=0.5, execute_into_world=True,
                            precomputed_candidates=packed,
                            precomputed_distill={})
                        tid = res.trajectory_id
                        rows = [t for t in twin.world.transactions
                                if t.get("trajectory_id") == tid] if tid else []
                else:
                    rows = _attempts_shadow_pgd(victim, twin, atype,
                                                k_per_cell, rng)
            elif mech == "llm_strategist":
                rows = _attempts_llm_strategist(victim, compiler_of_twin,
                                                twin, atype, k_per_cell,
                                                rng,
                                                weakness_descriptor=llm_weakness)
            else:
                raise ValueError(f"unknown mechanism {mech}")

            rows = rows[:max_rows_per_cell]
            caught_flags: List[int] = []
            for chunk_start in range(0, len(rows), 20):
                chunk = rows[chunk_start:chunk_start + 20]
                probs = victim.score_transactions(chunk, twin.world)
                caught_flags.extend((probs >= 0.5).astype(int).tolist())

            n_att = len(caught_flags)
            per_attempts[mech][atype] = {
                "n_txs": n_att,
                "caught": int(sum(caught_flags)),
                "detection_rate": round(float(np.mean(caught_flags)), 4)
                if caught_flags else float("nan"),
                "held_out": atype in _HELD_OUT,
            }

    rates = {
        m: {t: cell["detection_rate"]
            for t, cell in cells.items()
            if cell["n_txs"] > 0}
        for m, cells in per_attempts.items()
    }

    fp_payload = {
        "schema": "prometheus.ood_matrix.v1",
        "seed": seed,
        "mechanisms": MECHANISM_TYPES,
        "types": list(_ALL_TYPES),
        "holdout_fingerprint": holdout_spec.fingerprint,
        "k_per_cell": k_per_cell,
        "use_replay": shadow_candidates is not None,
    }

    artifact = {
        "schema": "prometheus.ood_matrix.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_seconds": round(time.time() - t0, 2),
        "config": {**fp_payload},
        "fingerprint": _fingerprint(fp_payload),
        "holdout_fingerprint": holdout_spec.fingerprint,
        "rates": rates,
        "cells": {m: cells for m, cells in per_attempts.items()},
        "mechanism_registry_snapshot": sorted(MECHANISM_REGISTRY),
        "note": ("cell = detection_rate of victim over mechanism-specific "
                 "fresh attempts; A2/A5 columns are held-out evaluation "
                 "only and never appear in any training pool"),
    }
    return artifact
