"""
protocol_attacks.py — T9: protocol / agentic-manipulation campaign (updates.md 6.1).

Structural attack classes over the agentic-commerce checkout flow
(Mastercard Agent Pay / Visa Trusted Agent Protocol / Google AP2):

    RC-1  Rogue registry entry          ->  P1 registry response integrity
    RC-2  Blind trust of a federation   ->  P2 identity binding of payout
          payout-resolution response
    RC-3  Credential leak via an        ->  P3 credential channel control
          observable channel
    RC-4  Check-vs-deduct race (TOCTOU) ->  P4 atomic check-then-deduct
    RC-5  Privileged checkout           ->  P5 tool-call authorization
          without caller authz

Unlike A1-A6 (which perturb transaction vectors), T9 models the LM-driven
agent's *trust of the surrounding protocol*: the attack succeeds regardless
of which LLM drives the agent, because it is a structural/handling fault of
the signing + checkout pipeline. Each case runs twice — once with no defense
(the structural gap is exercised and a payment lands) and once behind the
PCAT gate (the structural check refuses the call and NO payment lands).

Determinism: every case is a pure function of (world, seed, defense), so
scripts/protocol_eval.py can diff before/after with no model sampling.
Fraud rows flow through the normal twin pipeline (mechanism =
protocol_structural, attack_id = T9, per-payment rc_class tag).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from twin.agentic import AgenticCommerce
from twin.core import WorldState
from blue.splits import register_attack_types, register_mechanism

MECHANISM_NAME = "protocol_structural"
"""Axis-2 generation mechanism for T9 rows (own namespace; fingerprint-safe)."""

RC_CLASSES = ("RC-1", "RC-2", "RC-3", "RC-4", "RC-5")

register_mechanism(MECHANISM_NAME)      # idempotent namespace register
register_attack_types(["T9"])           # extends attack-type namespace

DefenseBuilder = Callable[[AgenticCommerce], Optional[Any]]


def _attacker_wallet(seed: int, rc: str) -> str:
    return f"WALLET_ATK_{seed}_{rc}"


def _pack(rc: str, defense: Optional[Any], ac: AgenticCommerce,
          res: Dict[str, Any], *, budget: float,
          attacker_payout: str, leaked: bool = False,
          caller_registered: bool = True, n_auth: int = 1) -> Dict[str, Any]:
    payments = list(res.get("payments") or [])
    paid = sum(float(p["amount"]) for p in payments)
    to_attacker = sum(float(p["amount"]) for p in payments
                      if str(p.get("to")) == attacker_payout)
    over_spent = paid > budget
    return {
        "rc_class": rc,
        "defense": "pcat" if defense is not None else "naive",
        "allowed": bool(res.get("allowed")),
        "p_blocks": list(res.get("p_blocks") or []),
        "payments": payments,
        "paid_total": round(paid, 2),
        "attacker_received": round(to_attacker, 2),
        "agent_budget": round(budget, 2),
        "credential_leaked": bool(leaked),
        "caller_registered": bool(caller_registered),
        "over_spent": bool(over_spent),
        "n_authorizations": n_auth,
        "payout": res.get("payout"),
        "attacker_payout": attacker_payout,
    }


def rc1_rogue_registry(world: WorldState, seed: int,
                       defense_builder: Optional[DefenseBuilder],
                       budget: float = 5_000.0, amount: float = 1_500.0) -> Dict[str, Any]:
    ac = AgenticCommerce(world, seed=seed)
    victim = ac.new_agent(budget=budget)
    ac.mark_attack(victim["agent_id"])
    atk = _attacker_wallet(seed, "RC-1")
    # Prompt-driven registry poisoning: an unsigned attacker entry lands in
    # the agent-facing registry (and mirrors a benign-looking merchant in the
    # twin graph) — the agent blindly trusts whatever the registry returns.
    ac.registry["ROGUE_MERCHANT"] = {
        "merchant_id": "ROGUE_MERCHANT",
        "domain": "rogue.example.com",
        "category": "retail",
        "payout_account": atk,
        "signed": False,
        "signature": "",
        "signer": None,
    }
    world.add_merchant(merchant_id="ROGUE_MERCHANT",
                       domain="rogue.example.com", category="retail")
    defense = defense_builder(ac) if defense_builder else None
    res = ac.checkout(victim["agent_id"], "ROGUE_MERCHANT", amount=amount,
                      defense=defense, rc_class="RC-1",
                      caller_identity=victim["identity"])
    return _pack("RC-1", defense, ac, res, budget=budget,
                 attacker_payout=atk)


def rc2_blind_federation(world: WorldState, seed: int,
                         defense_builder: Optional[DefenseBuilder],
                         budget: float = 5_000.0, amount: float = 1_500.0) -> Dict[str, Any]:
    ac = AgenticCommerce(world, seed=seed)
    defense = defense_builder(ac) if defense_builder else None
    victim = ac.new_agent(budget=budget)
    ac.mark_attack(victim["agent_id"])
    merch = ac.register_merchant(domain="fed.example.com", category="retail",
                                 payout_account=f"WALLET_LEGIT_{seed}",
                                 owner_identity=victim["identity"])
    atk = _attacker_wallet(seed, "RC-2")
    # A federation-style resolution call returns the ATTACKER's wallet; the
    # agent trusts it blindly (no identity binding of the destination).
    res = ac.checkout(victim["agent_id"], merch["merchant_id"], amount=amount,
                      defense=defense, rc_class="RC-2",
                      caller_identity=victim["identity"],
                      attacker_controlled_payout=atk)
    return _pack("RC-2", defense, ac, res, budget=budget,
                 attacker_payout=atk)


def rc3_credential_leak(world: WorldState, seed: int,
                        defense_builder: Optional[DefenseBuilder],
                        budget: float = 5_000.0, amount: float = 1_500.0) -> Dict[str, Any]:
    ac = AgenticCommerce(world, seed=seed)
    defense = defense_builder(ac) if defense_builder else None
    victim = ac.new_agent(budget=budget)
    ac.mark_attack(victim["agent_id"])
    merch = ac.register_merchant(domain="shop.example.com", category="retail",
                                 payout_account=f"WALLET_LEGIT_{seed}",
                                 owner_identity=victim["identity"])
    atk = _attacker_wallet(seed, "RC-3")
    # Leak the session secret into an observable channel, then complete a
    # checkout into an attacker-controlled destination with the leaked cred.
    res = ac.checkout(victim["agent_id"], merch["merchant_id"], amount=amount,
                      defense=defense, rc_class="RC-3",
                      caller_identity=victim["identity"],
                      leak_credential=True,
                      attacker_controlled_payout=atk)
    return _pack("RC-3", defense, ac, res, budget=budget,
                 attacker_payout=atk, leaked=True)


def rc4_toctou(world: WorldState, seed: int,
               defense_builder: Optional[DefenseBuilder],
               budget: float = 2_000.0, amount: float = 1_500.0) -> Dict[str, Any]:
    ac = AgenticCommerce(world, seed=seed)
    defense = defense_builder(ac) if defense_builder else None
    victim = ac.new_agent(budget=budget)
    ac.mark_attack(victim["agent_id"])
    merch = ac.register_merchant(domain="shop.example.com", category="retail",
                                 payout_account=f"WALLET_LEGIT_{seed}",
                                 owner_identity=victim["identity"])
    # Two concurrent authorizations of `amount` against ONE mandate budget
    # B with B in [amount, 2*amount): the naive path races check-vs-deduct,
    # the P4 path deducts atomically and only one authorization can succeed.
    res = ac.checkout(victim["agent_id"], merch["merchant_id"], amount=amount,
                      defense=defense, rc_class="RC-4",
                      caller_identity=victim["identity"],
                      n_authorizations=2)
    return _pack("RC-4", defense, ac, res, budget=budget,
                 attacker_payout="", n_auth=2)


def rc5_privileged_tool(world: WorldState, seed: int,
                        defense_builder: Optional[DefenseBuilder],
                        budget: float = 5_000.0, amount: float = 1_500.0) -> Dict[str, Any]:
    ac = AgenticCommerce(world, seed=seed)
    defense = defense_builder(ac) if defense_builder else None
    victim = ac.new_agent(budget=budget)
    ac.mark_attack(victim["agent_id"])
    merch = ac.register_merchant(domain="shop.example.com", category="retail",
                                 payout_account=f"WALLET_LEGIT_{seed}",
                                 owner_identity=victim["identity"])
    atk = _attacker_wallet(seed, "RC-5")
    # The checkout tool is invoked with NO caller identity (not pre-registered
    # in the authz table) — a prompt-injected privileged call.
    res = ac.checkout(victim["agent_id"], merch["merchant_id"], amount=amount,
                      defense=defense, rc_class="RC-5",
                      caller_identity=None,
                      attacker_controlled_payout=atk)
    return _pack("RC-5", defense, ac, res, budget=budget,
                 attacker_payout=atk, caller_registered=False)


_RC_BUILDERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "RC-1": rc1_rogue_registry,
    "RC-2": rc2_blind_federation,
    "RC-3": rc3_credential_leak,
    "RC-4": rc4_toctou,
    "RC-5": rc5_privileged_tool,
}


def benign_checkout(world: WorldState, seed: int,
                    defense_builder: Optional[DefenseBuilder],
                    budget: float = 5_000.0, amount: float = 1_500.0) -> Dict[str, Any]:
    """Honest agent checkout — the false-positive control for the gate.

    A signed merchant bound to an identity, an honest agent whose caller
    identity IS pre-registered, correct scope, no leak, single authorization.
    The PCAT gate must let this through (low FP), not just block attacks.
    """
    ac = AgenticCommerce(world, seed=seed)
    defense = defense_builder(ac) if defense_builder else None
    agent = ac.new_agent(budget=budget)
    merch = ac.register_merchant(domain="honest.example", category="retail",
                                 payout_account=f"WALLET_HONEST_{seed}",
                                 owner_identity=agent["identity"])
    res = ac.checkout(agent["agent_id"], merch["merchant_id"], amount,
                      defense=defense, rc_class=None,
                      caller_identity=agent["identity"])
    pack = _pack("BENIGN", defense, ac, res, budget=budget,
                 attacker_payout="", caller_registered=True)
    pack["agent_id"] = agent["agent_id"]
    return pack


def run_t9_case(world: WorldState, seed: int, rc_class: str,
                defense_builder: Optional[DefenseBuilder] = None) -> Dict[str, Any]:
    """Run one structural attack class against one flavor of defense.

    defense_builder=None -> naive/unhardened flow (attack lands).
    defense_builder=lambda ac: PCATPolicy.for_agentic(ac) -> gate active.
    """
    if rc_class not in _RC_BUILDERS:
        raise ValueError(f"unknown RC class: {rc_class}")
    return _RC_BUILDERS[rc_class](world, seed, defense_builder)