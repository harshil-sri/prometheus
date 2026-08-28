"""
normal_behavior.py — Normal Behaviour Generator for the Financial Digital Twin.

Each account gets a normal-behaviour profile that governs when and how it transacts.
Normal transactions follow moderate Gaussian distributions with natural-looking amounts
and realistic intervals — statistically distinct from fraud patterns (heavy tails,
round-number amounts, tight intervals).
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional, Tuple

from .core import WorldState


# ---------------------------------------------------------------------------
# Category-level amount distributions (normal — moderate, natural amounts)
# ---------------------------------------------------------------------------

MERCHANT_CATEGORIES: Dict[str, Dict[str, float]] = {
    "retail":        {"mean": 1200.0, "std": 400.0,  "min": 50.0,   "max": 8000.0},
    "grocery":       {"mean": 800.0,  "std": 300.0,  "min": 20.0,   "max": 5000.0},
    "dining":        {"mean": 600.0,  "std": 250.0,  "min": 30.0,   "max": 4000.0},
    "travel":        {"mean": 15000.0, "std": 8000.0, "min": 500.0,  "max": 80000.0},
    "utilities":     {"mean": 2500.0, "std": 800.0,  "min": 100.0,  "max": 15000.0},
    "subscription":  {"mean": 300.0,  "std": 100.0,  "min": 50.0,   "max": 2000.0},
    "health":        {"mean": 2000.0, "std": 1500.0, "min": 100.0,  "max": 30000.0},
    "education":     {"mean": 5000.0, "std": 3000.0, "min": 200.0,  "max": 50000.0},
    "entertainment": {"mean": 500.0,  "std": 300.0,  "min": 20.0,   "max": 5000.0},
    "p2p":           {"mean": 1500.0, "std": 1000.0, "min": 50.0,   "max": 20000.0},
}


def _natural_amount(rng: random.Random, mean: float, std: float,
                    lo: float = 1.0, hi: float = 100000.0) -> float:
    """Generate a natural-looking amount (not round-number multiples)."""
    a = rng.gauss(mean, std)
    # Add small random fractional part to avoid round-number look
    a += rng.uniform(-0.49, 0.49)
    return round(max(lo, min(hi, a)), 2)


#: fraction of balance a normal (non-fraud) spender keeps as headroom
#: when the spend floor clamps — mirrors the fraud-side solvency law so the
#: whole world (normal + fraud) respects the same "never overdraft" rule.
NORMAL_TX_HEADROOM = 0.02

#: transfers below this amount are skipped (won't log zero-value rows)
NORMAL_TX_MIN = 0.01


# ---------------------------------------------------------------------------
# Profile builder
# ---------------------------------------------------------------------------

def build_normal_profile(rng: random.Random, account_id: str) -> Dict:
    """Build a normal-behaviour profile for one account.

    Returns a dict with:
      - preferred_categories: list of 2-4 merchant categories
      - preferred_weights: weights summing to 1 for the categories
      - mean_interval: steps between transactions (3-25)
      - amount_scale: multiplier applied to category means (0.3-3.0)
      - n_devices: how many devices this account uses (1-2)
      - n_ips: how many IPs this account uses (1-2)
      - next_tx_step: first step at which this account should transact
    """
    all_cats = list(MERCHANT_CATEGORIES.keys())
    # Pick 2-4 preferred categories
    n_cats = rng.randint(2, 4)
    preferred = rng.sample(all_cats, n_cats)
    # Random weights summing to 1
    raw_weights = [rng.random() for _ in preferred]
    total = sum(raw_weights)
    weights = [w / total for w in raw_weights]

    return {
        "account_id": account_id,
        "preferred_categories": preferred,
        "preferred_weights": weights,
        "mean_interval": rng.randint(3, 25),
        "amount_scale": rng.uniform(0.3, 3.0),
        "n_devices": rng.randint(1, 2),
        "n_ips": rng.randint(1, 2),
        "next_tx_step": rng.randint(1, 10),
    }


# ---------------------------------------------------------------------------
# Normal Behaviour Generator
# ---------------------------------------------------------------------------

class NormalBehaviorGenerator:
    """Generates normal (non-fraud) transactions for all accounts at each step.

    Each account has a profile dict (built by build_normal_profile) stored in
    AccountState.profile.  At each time step, step() checks whether the
    account is due for a transaction based on its mean_interval.
    If due, it produces a transaction via world.log_transaction().
    """

    def __init__(self, world: WorldState, seed: int = 43):
        self.world = world
        self.rng = random.Random(seed)
        # Per-account step counters for inter-arrival timing
        self._step_since_last_tx: Dict[str, int] = {}

    def step(self, account_id: str) -> Optional[Dict]:
        """Check if account_id is due for a normal TX at the current step.

        If due, generate and log the transaction, returning the dict.
        Otherwise return None.
        """
        account = self.world.accounts.get(account_id)
        if account is None:
            return None

        profile = account.profile
        if not profile:
            # Cold-start: build a profile on the fly
            profile = build_normal_profile(self.rng, account_id)
            account.profile = profile

        # Advance step counter
        self._step_since_last_tx[account_id] = self._step_since_last_tx.get(account_id, 0) + 1
        since = self._step_since_last_tx[account_id]
        mean_interval = profile.get("mean_interval", 10)
        next_tx_step = profile.get("next_tx_step", 1)

        # Check if due: either we've hit next_tx_step, or passed mean_interval
        if self.world.current_step < next_tx_step:
            return None

        if since < mean_interval:
            # Small probability even before mean_interval (jitter)
            if since > 0 and self.rng.random() < 0.05:
                pass  # proceed to generate
            else:
                return None

        # Reset counter
        self._step_since_last_tx[account_id] = 0

        # Decide: merchant vs P2P (80% merchant, 20% P2P)
        is_p2p = self.rng.random() < 0.2

        if is_p2p:
            to_id = self._pick_p2p_recipient(account_id)
            category = "p2p"
        else:
            category = self._pick_category(profile)
            to_id = self._pick_merchant(category)

        if to_id is None:
            return None

        # Generate amount: gauss with category mean/std, scaled by profile amount_scale
        scale = profile.get("amount_scale", 1.0)
        cat_info = MERCHANT_CATEGORIES.get(category, {"mean": 1000.0, "std": 500.0})
        amount = _natural_amount(self.rng, cat_info["mean"] * scale, cat_info["std"] * scale,
                                 lo=cat_info.get("min", 1.0), hi=cat_info.get("max", 100000.0))

        # Solvency floor (steady-state economy fix): cap any normal spend at
        # what the sender actually holds (minus headroom) so the twin never
        # drives balances negative; skip rather than log a zero-value row.
        # All RNG draws above already happened, so this stays deterministic.
        available = max(account.balance * (1.0 - NORMAL_TX_HEADROOM), 0.0)
        if available < NORMAL_TX_MIN:
            return None
        amount = min(amount, available)

        device = self._pick_device(account_id, profile)
        ip = self._pick_ip()

        tx = self.world.log_transaction(
            from_id=account_id,
            to_id=to_id,
            amount=amount,
            category=category,
            device=device,
            ip=ip,
            is_fraud=False,
            attack_id=None,
            trajectory_id=None,
        )
        return tx

    def _pick_category(self, profile: Dict) -> str:
        """Pick a transaction category from the profile's preferred list."""
        preferred = profile.get("preferred_categories", ["retail"])
        weights = profile.get("preferred_weights", None)
        # 80% preferred, 20% other
        if self.rng.random() < 0.8:
            if weights and len(weights) == len(preferred):
                return self.rng.choices(preferred, weights=weights, k=1)[0]
            return self.rng.choice(preferred)
        all_cats = list(MERCHANT_CATEGORIES.keys())
        return self.rng.choice(all_cats)

    def _pick_device(self, account_id: str, profile: Dict) -> Optional[str]:
        """Return a device for this account, creating one if needed (cold-start)."""
        account = self.world.accounts.get(account_id)
        if account is None:
            return None

        # If account has linked devices, pick one
        if account.linked_devices:
            return self.rng.choice(account.linked_devices)

        # Cold-start: create a new device
        n_devices = profile.get("n_devices", 1)
        for _ in range(n_devices):
            device = self.world.add_device()
            account.linked_devices.append(device.device_id)
            device.linked_accounts.append(account_id)

        return self.rng.choice(account.linked_devices)

    def _pick_ip(self) -> Optional[str]:
        """Return a random IP block from the world, or None."""
        if self.world.ips:
            return self.rng.choice(list(self.world.ips.keys()))
        return None

    def _pick_merchant(self, category: str) -> Optional[str]:
        """Pick a merchant matching the category, or any merchant."""
        matching = [m for m in self.world.merchants.values() if m.category == category]
        if matching:
            return self.rng.choice(matching).merchant_id
        if self.world.merchants:
            return self.rng.choice(list(self.world.merchants.keys()))
        return None

    def _pick_p2p_recipient(self, from_id: str) -> Optional[str]:
        """Pick another account for P2P transfer."""
        candidates = [aid for aid in self.world.accounts if aid != from_id]
        if not candidates:
            return None
        return self.rng.choice(candidates)