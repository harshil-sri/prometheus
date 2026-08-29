"""
agentic.py — Agentic-commerce checkout flow inside the twin (updates.md 6.1).

Models the real agentic-payments trend (Mastercard Agent Pay, Visa Trusted
Agent Protocol, Google AP2) as a deterministic, self-contained subsystem on
top of the twin: an AGENT holds a scoped payment credential and can complete
checkout with a merchant without a human step, using Mandate-style signed
objects (Intent -> Cart -> Payment).

Every decision is a PURE function of seeded inputs; signing is a deterministic
sha256 over the canonical-JSON payload ring-fenced to this instance's key
material — an honest simulation of the paper's signed-protocol layer, built
exactly to expose the STRUCTURAL attack classes (RC-1..RC-5) that succeed
regardless of the model driving the agent.

The `checkout()` method is the single entry point. It resolves the merchant
registry, builds the mandate, applies each PCAT protocol check through the
optional `defense` hook (None = naive/unhardened path where the structural
attacks land), and commits each payment via `world.log_transaction` with
mechanism="protocol_structural" so T9 rows flow through the normal graph /
detection pipeline. An audit `events` log (plus an observable `session_log`
channel used by the RC-3 credential leak) is kept for the deterministic judges.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
from typing import Any, Dict, List, Optional

from twin.core import WorldState

DEFAULT_SCOPE = ("payment",)


def _canonical(payload: Dict[str, Any]) -> str:
    """Deterministic canonical JSON (sorted keys) for signing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sign(payload: Dict[str, Any], secret: str) -> str:
    """Deterministic signature: sha256(canonical(payload) | secret)."""
    return hashlib.sha256(
        (_canonical(payload) + "|" + secret).encode("utf-8")).hexdigest()


def _verify(payload: Dict[str, Any], signature: str, secret: str) -> bool:
    if not signature:
        return False
    return _sign(payload, secret) == str(signature)


class AgenticCommerce:
    """Deterministic agentic-checkout coordinator over a twin WorldState.

    Args:
        world: Twin world (merchant mirror + payments land here).
        seed: Determinism seed (RNG + ring-fenced signing key material).
    """

    def __init__(self, world: WorldState, seed: int = 1):
        self.world = world
        self.seed = seed
        self.rng = random.Random(seed)
        # Ring-fenced protocol key material (simulated; never leaves this object).
        self.registry_key = f"REG-AGENTIC-{seed}"
        self.agent_keys: Dict[str, str] = {}

        self.agents: Dict[str, Dict[str, Any]] = {}
        self.credentials: Dict[str, Dict[str, Any]] = {}
        self.registry: Dict[str, Dict[str, Any]] = {}
        # P2 identity table: merchant_id -> certified payout owner string.
        self.identity_table: Dict[str, str] = {}
        # P5 authz table: pre-registered caller identities and their scopes.
        self.authz_table: Dict[str, tuple] = {}

        # Audit/observability channels for the deterministic judges.
        self.events: List[Dict[str, Any]] = []
        self.session_log: List[str] = []
        self._budget_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Signing + credentials
    # ------------------------------------------------------------------ #

    def _agent_secret(self, agent_id: str) -> str:
        if agent_id not in self.agent_keys:
            self.agent_keys[agent_id] = f"AGENT-SECRET-{agent_id}-{self.seed}"
        return self.agent_keys[agent_id]

    # ------------------------------------------------------------------ #
    # Construction helpers (deterministic, mirror into the twin)
    # ------------------------------------------------------------------ #

    def new_agent(self, *, budget: float = 100000.0,
                  agent_id: Optional[str] = None,
                  owner: Optional[str] = None,
                  identity: Optional[str] = None,
                  scope: tuple = DEFAULT_SCOPE) -> Dict[str, Any]:
        """Create an agent + owner customer + funded linked account + credential."""
        if owner is not None:
            cid = owner
        else:
            cid = self.world.add_customer().customer_id
        acct = self.world.add_account(customer_id=cid, balance=budget)

        aid = agent_id or f"AGENT_{len(self.agents) + 1:03d}"
        cred_id = f"CRED_{aid}"
        cred_secret = self._agent_secret(aid)
        credential = {
            "credential_id": cred_id,
            "agent_id": aid,
            "scope": tuple(scope),
            "secret": cred_secret,
            "budget": float(budget),
            "budget_remaining": float(budget),
            "status": "active",
        }
        self.credentials[cred_id] = credential
        self.agents[aid] = {
            "agent_id": aid,
            "owner": cid,
            "linked_account": acct.account_id,
            "credential_id": cred_id,
            "identity": identity or f"ID-{cid}",
        }
        # P5: pre-register an honest caller identity + scope for this agent.
        self.authz_table[self.agents[aid]["identity"]] = tuple(scope)
        return self.agents[aid]

    def register_merchant(self, *, merchant_id: Optional[str] = None,
                          domain: str = "agentic.example.com",
                          category: str = "retail",
                          payout_account: Optional[str] = None,
                          signed: bool = True,
                          owner_identity: Optional[str] = None) -> Dict[str, Any]:
        """Register (or inject — when signed=False) a merchant registry entry.

        Signed entries carry a registry-key signature over merchant_id +
        payout_account (P1 requires this). Unsigned/forged entries are exactly
        the RC-1 primitive: the twin merchant mirror looks identical, but the
        agent-facing registry has NO signature the agent verifies.
        """
        mid = merchant_id or self.world.next_merchant_id()
        payout = payout_account or f"WALLET_{mid}"
        payload = {"merchant_id": mid, "payout_account": payout}
        entry = {
            "merchant_id": mid,
            "domain": domain,
            "category": category,
            "payout_account": payout,
            "signed": bool(signed),
            "signature": _sign(payload, self.registry_key) if signed else "",
            "signer": self.registry_key if signed else None,
        }
        self.registry[mid] = entry
        # P2: certified payouts are only the ones bound to a registered owner.
        if signed and owner_identity is not None:
            self.identity_table[mid] = owner_identity
        # Mirror into the twin so the merchant is graph-visible.
        self.world.add_merchant(merchant_id=mid, domain=domain, category=category)
        return entry

    # ------------------------------------------------------------------ #
    # Policy hooks used by checkout()
    # ------------------------------------------------------------------ #

    def is_credential_observed(self, credential_id: str) -> bool:
        """RC-3 probe: is this credential's secret in an observable channel?"""
        cred = self.credentials.get(credential_id)
        if not cred:
            return False
        secret = str(cred.get("secret", ""))
        return any(secret in line for line in self.session_log)

    def resolve_payout(self, merchant_id: str,
                       attacker_controlled: bool = False,
                       attacker_payout: Optional[str] = None) -> str:
        """Federation-style payout resolution (RC-2 seam).

        Baseline trust: whatever the call returns is used blindly. With P2,
        checkout() refuses payouts that are not in the certified table.
        """
        if attacker_controlled:
            return attacker_payout or f"WALLET_ATK_{merchant_id}"
        entry = self.registry.get(merchant_id, {})
        return entry.get("payout_account", f"WALLET_{merchant_id}")

    # ------------------------------------------------------------------ #
    # Authorize (P4 atomicity seam)
    # ------------------------------------------------------------------ #

    def _authorize_batch(self, credential_id: str, amount: float, n: int,
                         atomic: bool) -> int:
        """Run `n` concurrent authorizations of `amount` against one budget.

        atomic=True  (PCAT P4): each authorization deducts under the lock, so
                     the second sees the first's deduction — with a single
                     mandate budget below 2*amount only one can succeed.
        atomic=False (naive): the CHECK pass of every authorization reads the
                     ORIGINAL remaining (the TOCTOU window) before any DEDUCT
                     happens -> both concurrent auths pass when amount <= B,
                     and the budget can go negative. This is the reproducible,
                     deterministic form of the RC-4 race.
        """
        cred = self.credentials.get(credential_id)
        if not cred:
            return 0
        if atomic:
            accepted = 0
            for _ in range(max(1, n)):
                with self._budget_lock:
                    if amount <= float(cred["budget_remaining"]):
                        cred["budget_remaining"] -= amount
                        accepted += 1
            return accepted
        # Naive TOCTOU: evaluate every check first, then deduct for all passes.
        passes = sum(1 for _ in range(max(1, n)) if amount <= float(cred["budget_remaining"]))
        if passes:
            cred["budget_remaining"] -= passes * amount
        return passes

    # ------------------------------------------------------------------ #
    # Checkout
    # ------------------------------------------------------------------ #

    def checkout(self, agent_id: str, merchant_id: str, amount: float,
                 defense: Optional[Any] = None,
                 caller_identity: Optional[str] = None,
                 step: Optional[int] = None,
                 leak_credential: bool = False,
                 attacker_controlled_payout: Optional[str] = None,
                 n_authorizations: int = 1,
                 rc_class: Optional[str] = None) -> Dict[str, Any]:
        """Complete an agentic checkout (or attempt it).

        Defense None = naive/unhardened flow where structural attacks land;
        a PCATPolicy instance enforces P1..P5 at their natural hooks and makes
        the budget deduction atomic (P4).

        Returns a decision dict consumed by the deterministic judges:
        {allowed, reason, p_blocks, payments (tx dicts), amount, ...}
        """
        step = step if step is not None else self.world.current_step
        entry = self.registry.get(merchant_id)
        payout = self.resolve_payout(
            merchant_id, attacker_controlled=(attacker_controlled_payout is not None),
            attacker_payout=attacker_controlled_payout,
        )

        result: Dict[str, Any] = {
            "agent_id": agent_id,
            "merchant_id": merchant_id,
            "amount": float(amount),
            "step": step,
            "allowed": True,
            "reason": "granted",
            "p_blocks": [],
            "payments": [],
            "rc_class": rc_class,
            "payout": payout,
        }
        agent = self.agents.get(agent_id)
        credential_id = (agent or {}).get("credential_id", f"CRED_{agent_id}")
        cred = self.credentials.get(credential_id)

        if entry is None:
            result.update(allowed=False, reason="merchant_not_found")
            self._event(step, agent_id, merchant_id, amount, result)
            return result

        if leak_credential and cred is not None:
            # RC-3: session secret lands in an observable channel (log/URL).
            self.session_log.append(str(cred["secret"]))
            result["credential_leaked"] = True

        blocks: List[str] = []
        if defense is not None:
            # P1 — registry response integrity (unsigned entry = RC-1).
            ok, why = defense.enforce({
                "kind": "registry_update",
                "merchant_id": merchant_id,
                "signed": entry.get("signed", False),
                "signature": entry.get("signature", ""),
                "registry_key": self.registry_key,
                "payload": {"merchant_id": merchant_id,
                            "payout_account": entry.get("payout_account", "")},
            })
            if not ok:
                blocks.append(why)
            # P2 — payout destination must resolve to a certified identity.
            ok, why = defense.enforce({
                "kind": "payout_resolve",
                "merchant_id": merchant_id,
                "payout_account": payout,
            })
            if not ok:
                blocks.append(why)
            # P3 — credentials must not flow through an observable channel.
            ok, why = defense.enforce({
                "kind": "credential_channel",
                "credential_id": credential_id,
                "observed": self.is_credential_observed(credential_id),
            })
            if not ok:
                blocks.append(why)
            # P5 — sensitive tool calls need a pre-registered caller identity.
            ok, why = defense.enforce({
                "kind": "tool_call_authz",
                "scope": list(cred["scope"]) if cred else list(DEFAULT_SCOPE),
                "caller_identity": caller_identity,
            })
            if not ok:
                blocks.append(why)
            atomic = True  # P4 active: check-then-deduct under lock.
        else:
            atomic = False

        result["p_blocks"] = blocks
        if blocks:
            result.update(allowed=False, reason="; ".join(blocks))
            self._event(step, agent_id, merchant_id, amount, result)
            return result

        if cred is None:
            result.update(allowed=False, reason="credential_not_found")
            self._event(step, agent_id, merchant_id, amount, result)
            return result

        # RC-4: n_authorizations concurrent auths against one budget.
        accepted = self._authorize_batch(credential_id, float(amount),
                                         n_authorizations, atomic=atomic)
        payments: List[Dict[str, Any]] = []
        for _ in range(accepted):
            tx = self._pay(agent, payout, amount, step, rc_class)
            payments.append(tx)
        if accepted > 0:
            # P4 denied-spend visibility for the judge.
            reason = ("granted"
                      if accepted == n_authorizations
                      else "P4 atomic budget denied (insufficient remaining)")
            result.update(allowed=True, reason=reason,
                          accepted=accepted,
                          total_requested=float(n_authorizations) * float(amount),
                          payments=payments)
        else:
            result.update(allowed=False, reason="P4 atomic budget denied")
        self._event(step, agent_id, merchant_id, amount, result)
        return result

    def _pay(self, agent: Dict[str, Any], payout: str,
             amount: float, step: int, rc_class: Optional[str]) -> Dict[str, Any]:
        """Commit one payment through the real twin transaction pipeline."""
        world = self.world
        trajectory_id = world.next_trajectory_id()
        is_fraud = agent.get("is_attack", False)
        tx = world.log_transaction(
            from_id=agent["linked_account"],
            to_id=payout,
            amount=amount,
            step=step,
            currency="INR",
            category="agentic",
            is_fraud=is_fraud,
            attack_id="T9" if rc_class else None,
            trajectory_id=trajectory_id,
            mechanism="protocol_structural",
        )
        if rc_class:
            tx["rc_class"] = rc_class
        world.log_trajectory(
            attack_type="T9" if rc_class else "AGENTIC",
            actions=[{"action": "checkout", "from": agent["linked_account"],
                      "to": payout, "amount": amount, "step": step}],
            spec={"agent_id": agent["agent_id"], "rc_class": rc_class},
            trajectory_id=trajectory_id,
        )
        self._event(step, agent["agent_id"], self._merchant_of_payout(payout),
                    amount, {"allowed": True, "reason": "payment",
                             "tx_id": tx["tx_id"], "payout": payout})
        return tx

    def _merchant_of_payout(self, payout: str) -> Optional[str]:
        for mid, entry in self.registry.items():
            if entry["payout_account"] == payout:
                return mid
        return None

    def mark_attack(self, agent_id: str) -> None:
        """Flag an agent as attacker-controlled (payments become fraud rows)."""
        if agent_id in self.agents:
            self.agents[agent_id]["is_attack"] = True

    def _event(self, step: int, agent_id: str, merchant_id: Optional[str],
               amount: float, result: Dict[str, Any]) -> None:
        self.events.append({
            "step": step,
            "agent_id": agent_id,
            "merchant_id": merchant_id,
            "amount": round(float(amount), 2),
            "allowed": bool(result.get("allowed")),
            "reason": result.get("reason"),
            "p_blocks": list(result.get("p_blocks") or []),
        })