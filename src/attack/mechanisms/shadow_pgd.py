"""shadow_pgd.py — The Shadow-Gradient adversarial MECHANISM.

Zoo slot (mechanism axis = "shadow_pgd", pre-registered in blue.splits).
This module CLOSES THE LOOP between the shadow stack and the twin:

    distill(victim) → PGD(shadow net) → verify(true victim)
        → materialize REALIZABLE evading transactions into the WorldState,
          mechanism-tagged 'shadow_pgd', is_fraud=True

Materialization maps ONLY attacker-controllable feature deltas back onto
concrete transaction attributes:
    amount           -> tx.amount (± bound honoured at PGD level)
    time_since_last  -> shifted step (non-negative)
    merchant_category-> category string via CAT_MAP inverse
    is_new_device    -> mint/reuse a device when flipped
History-derived features (velocity etc.) are NOT fabricated — the candidate
rows only ever differ in fields a real adversary controls.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from blue.features import compute_features, FEATURE_NAMES, CAT_MAP
from blue.splits import register_mechanism
from twin.core import WorldState
from shadow.distill import collect_probes, distill_surrogates, ScoreOracleFn
from shadow.pgd import get_domains, ProjectedPGD
from shadow.verify import Verifier, VerifyReport

logger = logging.getLogger(__name__)

MECHANISM_NAME = "shadow_pgd"
register_mechanism(MECHANISM_NAME)      # idempotent namespace add

_CAT_INVERSE = {i: c for c, i in CAT_MAP.items()}

__all__ = ["ShadowPGDMechanism", "ShadowPGDResult"]


@dataclass
class ShadowPGDResult:
    distill: dict                       # DistillResult as plain dict
    verify: dict                        # VerifyReport.to_dict()
    trajectory_id: Optional[str] = None
    n_materialized: int = 0
    seed: int = 42
    candidates_used: List[np.ndarray] = field(default_factory=list)
    base_rows: List[dict] = field(default_factory=list)


class ShadowPGDMechanism:
    """Distill → evade → verify → materialize, against one victim ensemble."""

    def __init__(self, victim_ensemble, twin, seed: int = 42):
        self.victim = victim_ensemble
        self.twin = twin
        self.seed = seed

    # ------------------------------------------------------------------ #
    def _victim_oracle(self) -> ScoreOracleFn:
        """Black-box view of the victim: predict_proba_features on probe rows.

        The GNN column needs a sender id present in the trained graph; probe
        stubs default to the first trained account (documented caveat: probes
        measure the ensemble through a fixed graph anchor — same anchor used
        at verification for consistency).
        """
        ens = self.victim
        try:
            data, idmap, _ = ens._graph_cache
            anchor = next(iter(idmap.keys()), "ACC_00001")
        except AttributeError:
            anchor = "ACC_00001"

        def fn(X: np.ndarray) -> np.ndarray:
            stubs = [{"tx_id": f"PROBE_{k}", "step": 0,
                      "from": anchor, "to": "", "amount": 0.0}
                     for k in range(len(X))]
            return np.asarray(ens.predict_proba_features(X, stubs),
                              dtype=np.float64)

        return fn

    # ------------------------------------------------------------------ #
    def run(self,
            attack_id: str = "SHADOW_PGD",
            max_base_rows: int = 24,
            threshold: float = 0.5,
            probe_budget: int = 900,
            pgd_iterations: int = 30,
            restarts: int = 2,
            execute_into_world: bool = True,
            precomputed_candidates: Optional[Sequence] = None,
            precomputed_distill: Optional[dict] = None,
            ) -> ShadowPGDResult:
        """Full cycle against the CURRENT victim ensemble.

        `precomputed_candidates` + `precomputed_distill` enable the
        deterministic replay path (same candidate matrix re-verified against
        a different victim) used by the adversarial-training A/B protocol.
        """
        world: WorldState = self.twin.world
        rng = random.Random(self.seed)

        # --- choose candidate attack rows: existing FRAUD rows ------------- #
        fraud_rows = [tx for tx in world.transactions if tx.get("is_fraud")]
        if not fraud_rows:
            raise ValueError("no fraud rows in world to weaponize; run "
                             "benchmark attacks first")
        rng.shuffle(fraud_rows)
        base_txs = fraud_rows[:max_base_rows]

        # --- distill -------------------------------------------------------- #
        X_all, _, names = compute_features(base_txs, world)
        domains = get_domains(names)

        if precomputed_candidates is None:
            probes = collect_probes(world.transactions, self._victim_oracle(),
                                    world_state=world, max_probes=probe_budget,
                                    seed=self.seed)
            surr, shadow_net, dres = distill_surrogates(probes, seed=self.seed)
            pgd = ProjectedPGD(shadow_net, domains, seed=self.seed,
                               iterations=pgd_iterations, restarts=restarts)
            candidates = pgd.optimize(np.asarray(X_all, dtype=np.float64),
                                      threshold=threshold)
        else:
            # replay path: caller supplies the exact candidate matrix
            candidates = list(precomputed_candidates)
            probes = None
            surr = shadow_net = dres = None
            distill_report = dict(precomputed_distill or {})
            for c in candidates:
                c.shadow_score = getattr(c, "shadow_score", 0.0)

        if dres is not None:
            distill_report = {
                "n_queries": dres.n_queries, "n_holdout": dres.n_holdout,
                "xgb_fidelity": dres.xgb_fidelity,
                "mlp_fidelity": dres.mlp_fidelity, "epochs_mlp": dres.epochs_mlp,
            }

        # --- verify against TRUE victim -------------------------------------- #
        verifier = Verifier(self.victim, tx_stub_factory=lambda k: {
            "tx_id": f"CAND_{k}", "step": 0,
            "from": base_txs[k].get("from", "ACC_00001") if k < len(base_txs)
                    else "ACC_00001",
            "to": "", "amount": 0.0})
        vrep = verifier.verify(candidates, np.asarray(X_all, dtype=np.float64),
                               threshold=threshold)

        # --- materialize into the world --------------------------------------- #
        traj_id = None
        made = 0
        if execute_into_world:
            traj_id = world.next_trajectory_id()
            actions = []
            confirmed_idx = [j for j, pc in enumerate(vrep.per_candidate)
                             if pc["outcome"] == "confirmed_evasion"]
            chosen = confirmed_idx or list(range(len(candidates)))

            for j in chosen:
                src_tx = base_txs[j]
                new_x = candidates[j].x_projected
                dom = domains
                idx_of = {d.name: i for i, d in dom.items()}
                amt = float(new_x[idx_of["amount"]])
                dtls = int(max(0, round(float(new_x[idx_of["time_since_last_tx"]])
                                        - float(min(src_tx.get("step", 0), 1e9)))))
                cat_code = int(round(float(new_x[idx_of["merchant_category"]])))
                category = _CAT_INVERSE.get(cat_code, "retail")
                flip_new_dev = float(new_x[idx_of["is_new_device"]]) >= 0.5 \
                    and not bool(src_tx.get("device"))

                device = src_tx.get("device")
                if flip_new_dev:
                    dev = world.add_device(first_seen_step=world.current_step + dtls)
                    account = world.accounts.get(str(src_tx["from"]))
                    if account is not None:
                        account.linked_devices.append(dev.device_id)
                        dev.linked_accounts.append(account.account_id)
                    device = dev.device_id

                tx = world.log_transaction(
                    from_id=str(src_tx["from"]),
                    to_id=str(src_tx["to"]),
                    amount=round(max(amt, 0.01), 2),
                    step=world.current_step + dtls,
                    category=category,
                    device=device,
                    ip=src_tx.get("ip"),
                    is_fraud=True,
                    attack_id=f"{attack_id}_{src_tx.get('attack_id') or 'X'}",
                    trajectory_id=traj_id,
                    mechanism=MECHANISM_NAME,
                )
                actions.append({"step": tx["step"], "action": "shadow_pgd_evade",
                                "base_tx": src_tx.get("tx_id"),
                                "new_tx": tx["tx_id"],
                                "amount": tx["amount"]})
                made += 1

            world.log_trajectory(
                attack_type=MECHANISM_NAME, actions=actions,
                spec={"attack_id": attack_id, "seed": self.seed},
                trajectory_id=traj_id)

        return ShadowPGDResult(
            distill=distill_report,
            verify=vrep.to_dict(),
            trajectory_id=traj_id,
            n_materialized=made,
            seed=self.seed,
            candidates_used=[c.x_projected for c in candidates],
            base_rows=list(base_txs),
        )
