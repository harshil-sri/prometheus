"""
typologies.py — 8 AMLSim typologies for the Financial Digital Twin.

Each typology is a function that generates a transaction sequence against WorldState.
Fraud amounts use heavier tails, round-number amounts (multiples of 1000), and tighter
intervals — statistically distinct from normal behaviour.

All typology functions share this signature:
    (world, rng, *, attack_id="AUTO", trajectory_id=None, step_offset=0, **kwargs)
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

from .core import WorldState


# ---------------------------------------------------------------------------
# Fraud amount helpers
# ---------------------------------------------------------------------------

def _fraud_amount(rng: random.Random, mean: float = 25000.0,
                  std: float = 20000.0) -> float:
    """Generate a fraud-like amount: heavy tail, rounded to nearest 1000."""
    a = abs(rng.gauss(mean, std))
    a = max(100.0, min(200000.0, a))
    # Round to nearest 1000
    return round(a / 1000.0) * 1000.0


def _pick_device(rng: random.Random, world: WorldState,
                 account_id: str) -> Optional[str]:
    """Pick a device linked to the account, or create one."""
    account = world.accounts.get(account_id)
    if account is None:
        return None
    if account.linked_devices:
        return rng.choice(account.linked_devices)
    # Cold-start: create a device
    device = world.add_device()
    account.linked_devices.append(device.device_id)
    device.linked_accounts.append(account_id)
    return device.device_id


def _pick_ip(rng: random.Random, world: WorldState) -> Optional[str]:
    """Pick a random IP block."""
    if world.ips:
        return rng.choice(list(world.ips.keys()))
    return None


def _pick_merchant(rng: random.Random, world: WorldState,
                   category: str = "retail") -> Optional[str]:
    """Pick a merchant, optionally matching category."""
    matching = [m for m in world.merchants.values() if m.category == category]
    if matching:
        return rng.choice(matching).merchant_id
    if world.merchants:
        return rng.choice(list(world.merchants.keys()))
    return None


# ---------------------------------------------------------------------------
# Typology 1: Fan-In (N → 1)
# ---------------------------------------------------------------------------

def fan_in(world: WorldState, rng: random.Random, *,
           attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
           step_offset: int = 0,
           main_account: str = "", members: Optional[List[str]] = None,
           amount: Optional[float] = None, **kwargs) -> List[str]:
    """Multiple accounts send to one main account (N→1).

    Each member sends amount/len(members) to main_account.
    """
    tx_ids = []
    if members is None:
        return tx_ids
    per_member = (amount if amount is not None else _fraud_amount(rng)) / max(1, len(members))
    for i, src in enumerate(members):
        tx = world.log_transaction(
            from_id=src,
            to_id=main_account,
            amount=per_member,
            step=world.current_step + step_offset + i,
            category="p2p",
            device=_pick_device(rng, world, src),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])
    return tx_ids


# ---------------------------------------------------------------------------
# Typology 2: Fan-Out (1 → N)
# ---------------------------------------------------------------------------

def fan_out(world: WorldState, rng: random.Random, *,
            attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
            step_offset: int = 0,
            main_account: str = "", members: Optional[List[str]] = None,
            amount: Optional[float] = None, **kwargs) -> List[str]:
    """Main account distributes to multiple members (1→N).

    Main sends amount/len(members) to each member.
    """
    tx_ids = []
    if members is None:
        return tx_ids
    per_member = (amount if amount is not None else _fraud_amount(rng)) / max(1, len(members))
    for i, tgt in enumerate(members):
        tx = world.log_transaction(
            from_id=main_account,
            to_id=tgt,
            amount=per_member,
            step=world.current_step + step_offset + i,
            category="p2p",
            device=_pick_device(rng, world, main_account),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])
    return tx_ids


# ---------------------------------------------------------------------------
# Typology 3: Cycle
# ---------------------------------------------------------------------------

def cycle(world: WorldState, rng: random.Random, *,
          attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
          step_offset: int = 0,
          members: Optional[List[str]] = None,
          amount: Optional[float] = None, **kwargs) -> List[str]:
    """Closed cycle: each member sends to next member in circular fashion."""
    tx_ids = []
    if members is None or len(members) < 2:
        return tx_ids
    amt = (amount if amount is not None else _fraud_amount(rng)) / max(1, len(members))
    n = len(members)
    for i in range(n):
        src = members[i]
        tgt = members[(i + 1) % n]
        tx = world.log_transaction(
            from_id=src,
            to_id=tgt,
            amount=amt,
            step=world.current_step + step_offset + i,
            category="p2p",
            device=_pick_device(rng, world, src),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])
    return tx_ids


# ---------------------------------------------------------------------------
# Typology 4: Scatter-Gather
# ---------------------------------------------------------------------------

def scatter_gather(world: WorldState, rng: random.Random, *,
                   attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
                   step_offset: int = 0,
                   main_account: str = "",
                   intermediaries: Optional[List[str]] = None,
                   beneficiary: str = "",
                   amount: Optional[float] = None,
                   margin_ratio: float = 0.1, **kwargs) -> List[str]:
    """Main → intermediaries → beneficiary with margin_ratio deducted.

    Source sends to intermediaries (half the amount split among them).
    Each intermediary sends to beneficiary with margin_ratio deducted.
    Key property: gathered amount < scattered amount by margin_ratio factor.
    """
    tx_ids = []
    if intermediaries is None or not beneficiary:
        return tx_ids

    total_amt = amount if amount is not None else _fraud_amount(rng)

    for i, inter in enumerate(intermediaries):
        # Source → intermediary: half the total amount, spread across intermediaries
        scatter_amt = (total_amt / 2.0) / max(1, len(intermediaries))
        tx1 = world.log_transaction(
            from_id=main_account,
            to_id=inter,
            amount=scatter_amt,
            step=world.current_step + step_offset + 2 * i,
            category="p2p",
            device=_pick_device(rng, world, main_account),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx1["tx_id"])

        # Intermediary → beneficiary (with margin deducted)
        gather_amt = round(scatter_amt * (1.0 - margin_ratio), 2)
        tx2 = world.log_transaction(
            from_id=inter,
            to_id=beneficiary,
            amount=gather_amt,
            step=world.current_step + step_offset + 2 * i + 1,
            category="p2p",
            device=_pick_device(rng, world, inter),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx2["tx_id"])

    return tx_ids


# ---------------------------------------------------------------------------
# Typology 5: Gather-Scatter
# ---------------------------------------------------------------------------

def gather_scatter(world: WorldState, rng: random.Random, *,
                   attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
                   step_offset: int = 0,
                   sources: Optional[List[str]] = None,
                   main_account: str = "",
                   targets: Optional[List[str]] = None,
                   amount: Optional[float] = None, **kwargs) -> List[str]:
    """Sources → main → targets.

    Each source sends to main. Then main sends to each target.
    """
    tx_ids = []
    if sources is None or targets is None:
        return tx_ids
    amt = amount if amount is not None else _fraud_amount(rng)

    # Gather phase: sources → main
    for i, src in enumerate(sources):
        tx = world.log_transaction(
            from_id=src,
            to_id=main_account,
            amount=amt,
            step=world.current_step + step_offset + i,
            category="p2p",
            device=_pick_device(rng, world, src),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])

    # Scatter phase: main → targets
    offset = len(sources)
    for j, tgt in enumerate(targets):
        tx = world.log_transaction(
            from_id=main_account,
            to_id=tgt,
            amount=amt,
            step=world.current_step + step_offset + offset + j,
            category="p2p",
            device=_pick_device(rng, world, main_account),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])

    return tx_ids


# ---------------------------------------------------------------------------
# Typology 6: Bipartite
# ---------------------------------------------------------------------------

def bipartite(world: WorldState, rng: random.Random, *,
              attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
              step_offset: int = 0,
              sources: Optional[List[str]] = None,
              targets: Optional[List[str]] = None,
              amount: Optional[float] = None, **kwargs) -> List[str]:
    """Two disjoint sets: each source sends to a random target."""
    tx_ids = []
    if sources is None or targets is None:
        return tx_ids
    amt = (amount if amount is not None else _fraud_amount(rng)) / max(1, len(sources))
    idx = 0
    for src in sources:
        tgt = rng.choice(targets)
        tx = world.log_transaction(
            from_id=src,
            to_id=tgt,
            amount=amt,
            step=world.current_step + step_offset + idx,
            category="p2p",
            device=_pick_device(rng, world, src),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])
        idx += 1
    return tx_ids


# ---------------------------------------------------------------------------
# Typology 7: Stack (Stacked Bipartite)
# ---------------------------------------------------------------------------

def stack(world: WorldState, rng: random.Random, *,
          attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
          step_offset: int = 0,
          layers: Optional[List[List[str]]] = None,
          amount: Optional[float] = None, **kwargs) -> List[str]:
    """Stacked bipartite: multiple layers where each layer sends to the next.

    layers is a list of [sources, targets] pairs.
    """
    tx_ids = []
    if layers is None or len(layers) < 2:
        return tx_ids
    amt = (amount if amount is not None else _fraud_amount(rng)) / max(1, len(layers) - 1)
    idx = 0
    for layer_i in range(len(layers) - 1):
        current_layer = layers[layer_i]
        next_layer = layers[layer_i + 1]
        for src in current_layer:
            for tgt in next_layer:
                tx = world.log_transaction(
                    from_id=src,
                    to_id=tgt,
                    amount=amt,
                    step=world.current_step + step_offset + idx,
                    category="p2p",
                    device=_pick_device(rng, world, src),
                    ip=_pick_ip(rng, world),
                    is_fraud=True,
                    attack_id=attack_id,
                    trajectory_id=trajectory_id,
                )
                tx_ids.append(tx["tx_id"])
                idx += 1
    return tx_ids


# ---------------------------------------------------------------------------
# Typology 8: Random / Propagation
# ---------------------------------------------------------------------------

def random_typology(world: WorldState, rng: random.Random, *,
                    attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
                    step_offset: int = 0,
                    main_account: str = "", depth: int = 3,
                    amount: Optional[float] = None, **kwargs) -> List[str]:
    """Main → neighbor → neighbor's neighbors → propagating outward.

    At each hop, the current account sends to a random neighbor.
    """
    tx_ids = []
    amt = amount if amount is not None else _fraud_amount(rng)
    idx = 0
    current_source = main_account

    # Get all account IDs for random selection
    all_accounts = list(world.accounts.keys())

    for hop in range(depth):
        # Pick a single target (exclude current source)
        candidates = [a for a in all_accounts if a != current_source]
        if not candidates:
            break
        tgt = rng.choice(candidates)

        tx = world.log_transaction(
            from_id=current_source,
            to_id=tgt,
            amount=amt,
            step=world.current_step + step_offset + idx,
            category="p2p",
            device=_pick_device(rng, world, current_source),
            ip=_pick_ip(rng, world),
            is_fraud=True,
            attack_id=attack_id,
            trajectory_id=trajectory_id,
        )
        tx_ids.append(tx["tx_id"])
        idx += 1

        # Next hop source is the target
        current_source = tgt

    return tx_ids


# ---------------------------------------------------------------------------
# Typology dispatch
# ---------------------------------------------------------------------------

TYPOLOGY_FUNCTIONS = {
    "fan_in": fan_in,
    "fan_out": fan_out,
    "cycle": cycle,
    "scatter_gather": scatter_gather,
    "gather_scatter": gather_scatter,
    "bipartite": bipartite,
    "stack": stack,
    "random": random_typology,
}


def run_typology(typology_name: str, world: WorldState, rng: random.Random,
                 **kwargs) -> List[str]:
    """Run a named typology with the given kwargs.

    Returns the list of transaction IDs generated.
    """
    func = TYPOLOGY_FUNCTIONS.get(typology_name)
    if func is None:
        raise ValueError(f"Unknown typology: {typology_name}. "
                         f"Available: {list(TYPOLOGY_FUNCTIONS.keys())}")
    return func(world, rng, **kwargs)