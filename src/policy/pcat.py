"""
pcat.py — Payment/Protocol Controls for Agentic Transactions (updates.md 6.1).

The Gemot/otpad (AIP-Bench heterogeneous-agent benchmark) PCAT layer: a
non-neural, deterministic gate that blocks agentic payments whose *structure*
violates the signed-protocol trust assumptions. Each P-block maps 1:1 onto a
structural attack class (RC-x): P1 registry, P2 identity, P3 channel,
P4 atomicity, P5 tool-call authz.

The gate is deliberately independent of any LM — it inspects only the
structural facts that the protocol checks promise to verify. One of the
paper's core claims is that in a regime where structural attacks fail, the
residual (content-level) abuse gets concentrated and caught downstream; this
module is that first gate.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from twin.agentic import _verify


class PCATPolicy:
    """Deterministic structural gate. `enforce(op) -> (allowed, reason)`."""

    def __init__(self, *, certified_payouts: Optional[Dict[str, str]] = None,
                 allowed_callers: Optional[Dict[str, tuple]] = None,
                 agentic: Any = None):
        # payout_account -> merchant_id (identity table from AgenticCommerce).
        self.certified_payouts = certified_payouts or {}
        # caller identity -> allowed tool scopes (P5 registry).
        self.allowed_callers = allowed_callers or {}
        # Optional live wiring: tables resolve from the AgenticCommerce at
        # enforce() time, so construction order never matters.
        self._agentic = agentic

    # -- live table resolution -----------------------------------------------

    def resolved_certified(self) -> Dict[str, str]:
        """Payout->merchant map over signed, identity-bound registry entries.

        Public so the API/eval can report exactly which payouts are certified
        under the live tables (the agentic_status endpoint surfaces this)."""
        if self._agentic is not None:
            return {
                entry["payout_account"]: mid
                for mid, entry in self._agentic.registry.items()
                if entry.get("signed") and mid in self._agentic.identity_table
            }
        return self.certified_payouts

    def resolved_callers(self) -> Dict[str, tuple]:
        if self._agentic is not None:
            return dict(self._agentic.authz_table)
        return self.allowed_callers

    # -- individual blocks -------------------------------------------------

    def _callers(self) -> Dict[str, tuple]:
        if self._agentic is not None:
            return dict(self._agentic.authz_table)
        return self.allowed_callers

    # -- individual blocks -------------------------------------------------

    def p1_registry(self, op: Dict[str, Any]) -> Tuple[bool, str]:
        """Registry response integrity: never trust an unsigned entry."""
        signed = op.get("signed", False)
        signature = op.get("signature", "")
        key = op.get("registry_key", "")
        payload = op.get("payload", {})
        if signed and _verify(payload, signature, key):
            return True, ""
        return False, "P1 unsigned/forged registry entry"

    def p2_identity(self, op: Dict[str, Any]) -> Tuple[bool, str]:
        """Payout destination must resolve to a certified identity."""
        payout = op.get("payout_account", "")
        if payout in self.resolved_certified():
            return True, ""
        return False, f"P2 untrusted payout destination {payout}"

    def p3_channel(self, op: Dict[str, Any]) -> Tuple[bool, str]:
        """Credentials must not flow through an observable channel."""
        if op.get("observed", False):
            return False, "P3 credential appeared in an observable channel"
        return True, ""

    def p4_atomicity(self, op: Dict[str, Any]) -> Tuple[bool, str]:
        """Information-only probe; real enforcement is the atomic CAS."""
        return True, ""

    def p5_authz(self, op: Dict[str, Any]) -> Tuple[bool, str]:
        """Sensitive tool calls need a pre-registered caller identity."""
        caller = op.get("caller_identity")
        requested = set(op.get("scope", []))
        registered = self.resolved_callers().get(caller)
        if registered is not None and requested <= set(registered):
            return True, ""
        return False, "P5 unregistered caller for sensitive tool call"

    # -- dispatch ------------------------------------------------------------

    def enforce(self, op: Dict[str, Any]) -> Tuple[bool, str]:
        kind = op.get("kind", "")
        if kind == "registry_update":
            return self.p1_registry(op)
        if kind == "payout_resolve":
            return self.p2_identity(op)
        if kind == "credential_channel":
            return self.p3_channel(op)
        if kind == "checkout_authorize":
            return self.p4_atomicity(op)
        if kind == "tool_call_authz":
            return self.p5_authz(op)
        return True, ""

    # -- conveniences used by eval / API ------------------------------------

    @classmethod
    def for_agentic(cls, agentic: Any) -> "PCATPolicy":
        """Live-wire the gate to an AgenticCommerce (tables read at enforce
        time, so setup order never matters). Only signed, identity-bound
        registry entries certify a payout — a rogue (unsigned/forged) poisoned
        entry is never trusted by P2."""
        return cls(agentic=agentic)