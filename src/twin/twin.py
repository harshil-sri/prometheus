"""
twin.py — Financial Digital Twin orchestrator.

Bootstrap the world, run discrete time steps, accept attack scheduler callbacks,
and produce state summaries.
"""

from __future__ import annotations

import random
from typing import Any, Callable, Dict, List, Optional, Tuple

from .core import WorldState
from .normal_behavior import MERCHANT_CATEGORIES, NormalBehaviorGenerator, build_normal_profile
from .typologies import run_typology


# ---------------------------------------------------------------------------
# IP block generation helpers
# ---------------------------------------------------------------------------

def _generate_ip_blocks(rng: random.Random, count: int) -> List[str]:
    """Generate `count` unique IP blocks in CIDR notation."""
    blocks = set()
    while len(blocks) < count:
        a = rng.randint(1, 223)
        b = rng.randint(0, 255)
        c = rng.randint(0, 255)
        prefix = rng.choice([24, 24, 24, 20, 16])
        blocks.add(f"{a}.{b}.{c}.0/{prefix}")
    return list(blocks)


def rng_uniform_salary(rng: random.Random, lo: float, hi: float) -> float:
    return rng.uniform(lo, hi)


# ---------------------------------------------------------------------------
# Financial Digital Twin
# ---------------------------------------------------------------------------

class FinancialDigitalTwin:
    """Stateful discrete-time-step simulation of a financial world.

    Bootstraps entities, runs normal-behaviour generation, and accepts
    attack-scheduler callbacks that inject fraud transactions at specified steps.
    """

    def __init__(self, seed: int = 42,
                 num_accounts: int = 10000,
                 num_merchants: int = 500,
                 num_devices: int = 2000,
                 num_ip_blocks: int = 1000,
                 num_steps: int = 1000):
        self.seed = seed
        self.rng = random.Random(seed)
        self.world = WorldState(seed)
        self.normal_gen = NormalBehaviorGenerator(self.world, seed + 1)

        self.num_accounts = num_accounts
        self.num_merchants = num_merchants
        self.num_devices = num_devices
        self.num_ip_blocks = num_ip_blocks
        self.num_steps = num_steps

        # Recurring-salary realism (P7 behavioral fidelity): EVERY account
        # receives a salary-sized income on a ~30-step cadence, sized to its
        # own spending so the economy holds a steady state. State lives here
        # so runs stay deterministic.
        self.salary_recipients: List[str] = []
        self.salary_interval: int = 30
        self.salary_schedule: Dict[str, float] = {}

        # Bootstrap
        self._bootstrap()

    def _bootstrap(self) -> None:
        """Create all entities and assign normal profiles."""
        rng = self.rng
        world = self.world

        # Create IP blocks
        ip_blocks = _generate_ip_blocks(rng, self.num_ip_blocks)
        for block in ip_blocks:
            geo = rng.choice(["IN-DL", "IN-MH", "IN-KA", "IN-TN", "IN-UP", "US-CA", "US-NY", "GB-LON"])
            world.add_ip(block, geo=geo)

        # Create customers and accounts
        for _ in range(self.num_accounts):
            customer = world.add_customer(
                risk_state=rng.choice(["normal", "normal", "normal", "normal", "elevated"]),
                kyc_tier=rng.choice(["standard", "standard", "standard", "low", "enhanced"]),
            )
            account = world.add_account(
                customer_id=customer.customer_id,
                balance=rng.uniform(1000.0, 500000.0),
            )
            # Build and assign normal profile
            account.profile = build_normal_profile(rng, account.account_id)

        # Create merchants
        merchant_categories = ["retail", "grocery", "dining", "travel",
                               "entertainment", "utilities", "subscription",
                               "health", "education"]
        for i in range(self.num_merchants):
            cat = rng.choice(merchant_categories)
            world.add_merchant(
                domain=f"merchant{i}.com",
                category=cat,
                hosting_asn=rng.choice(["ASN_1", "ASN_2", "ASN_3", "ASN_4"]),
                template_fingerprint=f"wp-plugin-hash-{rng.randint(1, 20)}",
            )

        # Create devices
        for _ in range(self.num_devices):
            world.add_device()

        # Recurring income (steady-state economy fix): EVERY account receives
        # a salary-sized deposit on the fixed cadence, sized from its own
        # expected cycle spend with a modest surplus (a bounded ceiling keeps
        # the credits from dominating the normal-amount mix — the P1 law
        # "fraud amounts statistically exceed normal amounts" must hold).
        # Merchants are EXTERNAL entities — money spent on them never
        # returns, so a 10%-only salary pool used to drain the economy over
        # long horizons, pushing balances negative and silently starving
        # salary-funded attack typologies (A5 scatter_gather).
        MIN_SALARY_INR = 100.0
        MAX_SALARY_INR = 20000.0
        all_accounts = list(world.accounts.keys())
        for acc_id in all_accounts:
            profile = getattr(world.accounts[acc_id], "profile", None) or {}
            expected = self._expected_cycle_spend(profile)
            scaled = expected * self.rng.uniform(1.05, 1.4)
            salary = round(min(MAX_SALARY_INR, max(MIN_SALARY_INR, scaled)), 2)
            self.salary_schedule[acc_id] = salary
            world.log_transaction(
                from_id="EXT_SALARY",
                to_id=acc_id,
                amount=salary,
                step=0,
                currency="INR",
                category="salary",
                device=None,
                ip=None,
                is_fraud=False,
            )
        self.salary_recipients = list(self.salary_schedule.keys())

    def _expected_cycle_spend(self, profile: Dict[str, Any]) -> float:
        """Expected normal-behaviour spend over one salary cycle.

        Approximates the normal generator's per-tx amount (preferred
        merchant categories weighted by profile weights, 20% P2P) times the
        expected number of txs per interval. Used to size each account's
        recurring income near its burn rate so balances stay healthy.
        """
        scale = float(profile.get("amount_scale", 1.0))
        mean_interval = max(1.0, float(profile.get("mean_interval", 10)))
        cats = profile.get("preferred_categories") or ["retail"]
        weights = profile.get("preferred_weights") or \
            [1.0 / len(cats)] * len(cats)
        merchant_mean = sum(
            w * MERCHANT_CATEGORIES.get(c, {"mean": 1000.0})["mean"]
            for w, c in zip(weights, cats)
        )
        p2p_mean = MERCHANT_CATEGORIES["p2p"]["mean"]
        per_tx = 0.8 * merchant_mean + 0.2 * p2p_mean
        n_tx = self.salary_interval / mean_interval
        return per_tx * n_tx * scale

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    def step(self) -> List[Dict]:
        """Advance one time step.

        For each account, check if the normal generator produces a transaction.
        Returns a list of all transaction dicts for this step.
        """
        self.world.current_step += 1
        step_txs: List[Dict] = []

        # Recurring salary deposits (all accounts, fixed cadence, per-account
        # amount sized to each account's own spending)
        if self.salary_schedule and \
                self.world.current_step % self.salary_interval == 0:
            for acc_id, amt in self.salary_schedule.items():
                tx = self.world.log_transaction(
                    from_id="EXT_SALARY", to_id=acc_id, amount=amt,
                    step=self.world.current_step, currency="INR",
                    category="salary",
                )
                step_txs.append(tx)

        # Process each account
        for account_id in list(self.world.accounts.keys()):
            # Normal merchant/P2P transaction
            tx = self.normal_gen.step(account_id)
            if tx is not None:
                step_txs.append(tx)

        return step_txs

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------

    def run(self, attack_scheduler: Optional[Callable] = None) -> List[Dict]:
        """Run all steps, accepting an optional attack scheduler callback.

        The attack_scheduler is called at each step with (world, twin).
        Returns ALL transactions in the world (including bootstrap and attack TXs).
        """
        # Record count before stepping to know what's new
        pre_tx_count = len(self.world.transactions)

        for _ in range(self.num_steps):
            self.step()

            # Run attack scheduler if provided
            if attack_scheduler is not None:
                attack_scheduler(self.world, self)

        # Return all transactions in the world (includes bootstrap + normal + attacks)
        return list(self.world.transactions)

    # ------------------------------------------------------------------
    # State summary
    # ------------------------------------------------------------------

    def state_summary(self) -> Dict[str, Any]:
        """Return metrics about the current state."""
        total_tx = len(self.world.transactions)
        fraud_tx = sum(1 for t in self.world.transactions if t["is_fraud"])
        normal_tx = total_tx - fraud_tx
        total_trajectories = len(self.world.trajectories)

        # Amount statistics
        normal_amounts = [t["amount"] for t in self.world.transactions if not t["is_fraud"]]
        fraud_amounts = [t["amount"] for t in self.world.transactions if t["is_fraud"]]

        return {
            "current_step": self.world.current_step,
            "num_customers": len(self.world.customers),
            "num_accounts": len(self.world.accounts),
            "num_merchants": len(self.world.merchants),
            "num_devices": len(self.world.devices),
            "num_ips": len(self.world.ips),
            "total_transactions": total_tx,
            "normal_transactions": normal_tx,
            "fraud_transactions": fraud_tx,
            "fraud_ratio": fraud_tx / max(1, total_tx),
            "total_trajectories": total_trajectories,
            "normal_amount_mean": sum(normal_amounts) / max(1, len(normal_amounts)),
            "fraud_amount_mean": sum(fraud_amounts) / max(1, len(fraud_amounts)),
            "normal_amount_max": max(normal_amounts) if normal_amounts else 0.0,
            "fraud_amount_max": max(fraud_amounts) if fraud_amounts else 0.0,
        }