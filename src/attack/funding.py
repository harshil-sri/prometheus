"""
funding.py — Ring-fenced per-attack-type funding reserves (updates.md 2.3).

Root cause this fixes: the eval phase runs A1..A6 sequentially against ONE
live twin world with no stepping/replenishment between attacks. Every
cash-out permanently exits money via EXT_BANK, and pricier types (A5 is a
₹300,000 scatter_gather) execute LAST in the naive A1-first ordering — so by
the time A5 runs, A1/A4/A6 (A6 is ₹150k, A4 ₹200k) have already drawn down
the funded upper tail. Funding-aware selection reads live balances, but with
no defense against CROSS-ATTACK-TYPE depletion it silently falls back to
unconstrained selection, gets clamped by the never-overdraft rule, and the
run yields "not enough fraud rows" with zero visibility into why.

Fix (the real one): partition the funded upper tail into DISJOINT,
seed-deterministic per-attack-type account pools sized to each type's
principal × eval_repeats × safety. Compilation for a type draws ONLY from
its own reserve, so no attack can cannibalize another's funding, and each
type's pool funding is reported per solvency tier (100%/50%/20% of the
type's amount) in the eval artifact — failures become loud and diagnosable
instead of silent depletion.

Determinism: the partition is a pure function of the (already-deterministic)
world state — accounts are ranked by live balance desc, then account_id asc
for stable ties — so identical seeds ⇒ identical reservations. No RNG of its
own.
"""

from __future__ import annotations

from typing import Any, Dict, List

SAFETY_DEFAULT = 1.25


class FundingReservation:
    """Disjoint per-type funding pools carved from the funded upper tail.

    Roles:
      * ``order`` — attack types sorted by principal descending (the exec
        order that lets priciest types claim funds first, but pools are
        already disjoint so ordering is just defensive depth).
      * ``pools`` — ``{attack_id: [account_id, ...]}``, disjoint across types.
      * ``diag`` — ``{attack_id: {amount, n_accounts, total_balance,
        tier_100, tier_50, tier_20, repeats, required_balance}}`` for the
        eval artifact's ``funding`` block.
      * ``warnings`` — loud, human-readable notes when a pool cannot fully
        anchor every repeat (tier_100 < eval_repeats).
    """

    def __init__(self, world: Any, specs_by_type: Dict[str, Dict[str, Any]],
                 eval_repeats: int, safety: float = SAFETY_DEFAULT):
        self.world = world
        self.specs_by_type = specs_by_type
        self.eval_repeats = max(1, int(eval_repeats))
        self.safety = float(safety)

        self.order: List[str] = sorted(
            specs_by_type.keys(),
            key=lambda aid: (-_spec_amount(specs_by_type[aid]), aid),
        )
        self.pools: Dict[str, List[str]] = {}
        self.diag: Dict[str, Dict[str, Any]] = {}
        self.warnings: List[str] = []
        self._partition()

    # ------------------------------------------------------------------ #
    def pool_for(self, attack_type: str) -> List[str]:
        """Accounts reserved for ``attack_type`` (empty list if unknown)."""
        return list(self.pools.get(attack_type, []))

    # ------------------------------------------------------------------ #
    def _ranked_accounts(self) -> List[str]:
        def _key(acc_id: str):
            return (-self.world.accounts[acc_id].balance, acc_id)
        return sorted(self.world.accounts.keys(), key=_key)

    def _tiers(self, pool: List[str], amount: float) -> Dict[str, int]:
        if amount <= 0.0:
            return {"tier_100": len(pool), "tier_50": len(pool),
                    "tier_20": len(pool)}
        bal_100 = amount
        bal_50 = amount * 0.5
        bal_20 = amount * 0.2
        t100 = t50 = t20 = 0
        for acc_id in pool:
            b = float(self.world.accounts[acc_id].balance)
            t100 += 1 if b >= bal_100 else 0
            t50 += 1 if b >= bal_50 else 0
            t20 += 1 if b >= bal_20 else 0
        return {"tier_100": t100, "tier_50": t50, "tier_20": t20}

    def _partition(self) -> None:
        remaining = self._ranked_accounts()
        for aid in self.order:
            spec = self.specs_by_type[aid]
            amount = _spec_amount(spec)
            required_balance = amount * self.eval_repeats * self.safety

            resources = spec.get("resources") or {}
            n_accounts_needed = max(1, int(resources.get("accounts", 1)))
            max_pool = max(self.eval_repeats * n_accounts_needed,
                           self.eval_repeats * 4)

            pool: List[str] = []
            pooled_balance = 0.0
            for acc_id in remaining:
                if pooled_balance >= required_balance and \
                        len(pool) >= self.eval_repeats:
                    break
                if len(pool) >= max_pool:
                    break
                pool.append(acc_id)
                pooled_balance += float(self.world.accounts[acc_id].balance)

            self.pools[aid] = pool
            tiers = self._tiers(pool, amount)
            self.diag[aid] = {
                "amount": round(amount, 2),
                "repeats": self.eval_repeats,
                "required_balance": round(required_balance, 2),
                "n_accounts": len(pool),
                "total_balance": round(pooled_balance, 2),
                **tiers,
            }
            if tiers["tier_100"] < self.eval_repeats:
                self.warnings.append(
                    f"type {aid}: reserved pool has {tiers['tier_100']} "
                    f"accounts at the 100% tier < eval_repeats="
                    f"{self.eval_repeats} (tier_50={tiers['tier_50']}, "
                    f"tier_20={tiers['tier_20']}) — later repeats may "
                    f"under-fund; the economy may be too thin for this "
                    f"principal at this scale."
                )
            remaining = [a for a in remaining if a not in pool]


def _spec_amount(spec: Dict[str, Any]) -> float:
    amt = spec.get("amount", 0.0)
    try:
        return max(0.0, float(amt))
    except (TypeError, ValueError):
        return 0.0


def reserve_funding_pools(
    world: Any,
    specs_by_type: Dict[str, Dict[str, Any]],
    eval_repeats: int,
    safety: float = SAFETY_DEFAULT,
) -> FundingReservation:
    """Build the disjoint, deterministic per-type funding partition.

    Args:
        world: Twin world whose live account balances define the economy.
        specs_by_type: ``{attack_id: benchmark-spec-dict}`` (amounts drive
            pool sizing).
        eval_repeats: How many fresh executions each type runs; each repeat
            wants a fully-funded (100%-tier) anchor account.
        safety: Multiplier on required balance per type (default 1.25).
    """
    return FundingReservation(world, specs_by_type, eval_repeats, safety)