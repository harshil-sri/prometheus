"""supply_chain.py — GenAI fraud supply-chain combo attack (Phase 10).

Chains four stages across a time window, each using different twin
typologies, to simulate a REAL multi-step fraud supply chain:

  Stage 1  Synthetic identity onboarding (bipartite among new accounts)
  Stage 2  Merchant fraud (fan-in to a freshly created fake merchant)
  Stage 3  Layering (scatter-gather through intermediaries w/ margin)
  Stage 4  Cash-out (large transfer to EXT_BANK)

Every stage is tagged mechanism='rule_compiler' + combo_stage=N; the
ensemble scores each stage so judges see WHERE detection breaks down
(if it does). This is the differentiator: not isolated typologies but a
FULL supply chain an adversary would actually run.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List

import numpy as np

from twin.typologies import run_typology

__all__ = ["SupplyChainCombo"]

STAGE_NAMES = [
    "synthetic_identity_onboarding",
    "merchant_fraud_funnel",
    "layering_scatter_gather",
    "cash_out_exit",
]


class SupplyChainCombo:
    def __init__(self, twin, ensemble, seed: int = 42):
        self.twin = twin
        self.ensemble = ensemble
        self.seed = seed
        self.rng = random.Random(seed)

    def run(self) -> Dict[str, Any]:
        world = self.twin.world
        rng = self.rng
        accounts = list(world.accounts.keys())
        if len(accounts) < 8:
            return {"status": "error",
                    "detail": "need >=8 accounts for a combo run"}

        traj_id = world.next_trajectory_id()
        base_step = world.current_step
        stage_reports: List[Dict] = []

        # --- Stage 1: synthetic identity onboarding --------------------------
        synthetic_accts = []
        for _ in range(6):
            cust = world.add_customer(kyc_tier="low")
            acct = world.add_account(cust.customer_id,
                                      balance=rng.uniform(2000, 30000),
                                      opened_at=base_step)
            synthetic_accts.append(acct.account_id)

        half = len(synthetic_accts) // 2
        s1_txs = run_typology(
            "bipartite", world, rng,
            sources=synthetic_accts[:half],
            targets=synthetic_accts[half:] or synthetic_accts[:1],
            amount=rng.uniform(2000, 15000),
            attack_id="COMBO_S1",
            trajectory_id=traj_id,
            step_offset=0,
            mechanism="rule_compiler")

        s1_rows = [t for t in world.transactions
                   if t.get("tx_id") in s1_txs]
        stage_reports.append(self._score_stage(1, s1_rows))

        # --- Stage 2: merchant fraud funnel ---------------------------------
        merchant = world.add_merchant(
            domain=f"fraud-store-{rng.randint(10000, 99999)}.com",
            category="retail")
        s2_txs = run_typology(
            "fan_in", world, rng,
            main_account=merchant.merchant_id,
            members=synthetic_accts[:4],
            amount=rng.uniform(20000, 80000),
            attack_id="COMBO_S2",
            trajectory_id=traj_id,
            step_offset=5,
            mechanism="rule_compiler")

        s2_rows = [t for t in world.transactions
                   if t.get("tx_id") in s2_txs]
        stage_reports.append(self._score_stage(2, s2_rows))

        # --- Stage 3: layering scatter-gather --------------------------------
        others = [a for a in accounts if a not in synthetic_accts][:4]
        s3_txs = run_typology(
            "scatter_gather", world, rng,
            main_account=synthetic_accts[0],
            intermediaries=others[:3] or synthetic_accts[1:4],
            beneficiary=synthetic_accts[-1] if len(synthetic_accts) > 1
            else synthetic_accts[0],
            amount=rng.uniform(30000, 120000),
            margin_ratio=0.08,
            attack_id="COMBO_S3",
            trajectory_id=traj_id,
            step_offset=10,
            mechanism="rule_compiler")

        s3_rows = [t for t in world.transactions
                   if t.get("tx_id") in s3_txs]
        stage_reports.append(self._score_stage(3, s3_rows))

        # --- Stage 4: cash-out to EXT_BANK -----------------------------------
        s4_amt = round(rng.uniform(50000, 150000), 2)
        tx = world.log_transaction(
            from_id=synthetic_accts[0],
            to_id="EXT_BANK",
            amount=s4_amt,
            step=base_step + 15,
            category="p2p",
            is_fraud=True,
            attack_id="COMBO_S4",
            trajectory_id=traj_id,
            mechanism="rule_compiler")
        s4_rows = [tx]
        stage_reports.append(self._score_stage(4, s4_rows))

        world.log_trajectory(
            attack_type="supply_chain_combo",
            actions=[{"stage": i + 1, "name": STAGE_NAMES[i],
                      "tx_ids": [r["tx_id"] for r in
                                 stage_reports[i].get("rows", [])]}
                     for i in range(4)],
            spec={"seed": self.seed, "mechanism": "rule_compiler",
                  "combo": True},
            trajectory_id=traj_id)

        stages_caught = sum(1 for s in stage_reports if s["caught"])
        return {
            "status": "ok",
            "trajectory_id": traj_id,
            "n_stages": 4,
            "stages_caught": stages_caught,
            "fully_detected": stages_caught == 4,
            "stages": stage_reports,
            "stage_names": STAGE_NAMES,
        }

    def _score_stage(self, stage_num: int, rows: List[dict]) -> Dict:
        if not rows:
            return {"stage": stage_num, "n_txs": 0, "caught": False,
                    "peak_score": 0.0}
        probs = self.ensemble.score_transactions(rows, self.twin.world)
        peak = float(probs.max()) if probs.size else 0.0
        return {"stage": stage_num,
                "n_txs": len(rows),
                "caught": bool(peak >= 0.5),
                "peak_score": round(peak, 4),
                "mean_score": round(float(probs.mean()), 4) if probs.size else 0.0,
                "tx_ids": [r["tx_id"] for r in rows]}
