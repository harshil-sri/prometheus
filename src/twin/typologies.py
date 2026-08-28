"""
typologies.py — 8 AMLSim typologies for the Financial Digital Twin.

PHASE 1 INTEGRITY MODEL (2026-08-27):

Amounts
    Fraud amounts are drawn from a heavy-tailed Gaussian and multiplied by a
    seed-controlled jitter, then rounded to paise. They are NOT rounded to
    ₹1000 multiples any more — the old grid was a detector cheat-code
    (finding #10) and has been removed.

Money conservation
    Every transfer is applied to account balances by WorldState.log_transaction,
    so internal-only typologies (all eight, category "p2p") preserve the total
    internal supply exactly. Layered typologies carry a per-hop `margin_ratio`
    (fee kept by the forwarding account) and ALWAYS derive the next hop's
    amount from the PREVIOUS HOP'S ACTUAL LOGGED AMOUNT — no hop creates or
    destroys value. When `margin_ratio=None`, a plausible fee is sampled from
    U[0.02, 0.12] with the supplied RNG.

Timing
    Attack actions are temporally spread: consecutive actions are separated by
    U{1..timing_max_lag} steps (default lag sampled per call from U{2..8}).
    The old consecutive-step packing ("fraud lands exactly 1 step apart",
    finding #10) is gone. Fraud inter-arrival times now overlap the normal
    cadence instead of sitting far below it.

Solvency
    `_send()` clamps every outgoing amount to the sender's current balance
    (minus headroom). An unfunded hop is SKIPPED rather than allowed to
    overdraft — declared preconditions handle funding at the compiler level.

Shared signature contract (unchanged):
    (world, rng, *, attack_id="AUTO", trajectory_id=None, step_offset=0, **kwargs)
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

from .core import WorldState


# ---------------------------------------------------------------------------
# Amount / timing / solvency helpers
# ---------------------------------------------------------------------------

#: transfers below this amount are treated as noise and skipped.
#: Set at paise level so LEGITIMATE micro-payment fraud patterns
#: (e.g. A3 card testing at ₹0.5) survive the solvency check — the floor's
#: job is preventing degenerate zero-value rows, not filtering amounts.
MIN_TRANSFER_INR = 0.01

#: fraction of balance the sender keeps as headroom when clamping
BALANCE_HEADROOM = 0.02

#: plausible laundering-fee band (sampled when margin_ratio is not provided)
MARGIN_RATIO_RANGE = (0.02, 0.12)


def _fraud_amount(rng: random.Random, mean: float = 25000.0,
                  std: float = 20000.0) -> float:
    """Generate a realistic fraud-like amount: heavy tail, paise precision.

    PHASE 1: no longer rounded to ₹1000 multiples — that grid was an
    artificial separability signal (audit finding #10).
    """
    a = abs(rng.gauss(mean, std))
    a = max(100.0, min(200000.0, a))
    # De-grid: multiplicative jitter kills any systematic rounding artefact
    a *= rng.uniform(0.90, 1.10)
    return round(a, 2)


def _resolve_margin(rng: random.Random, margin_ratio: Optional[float]) -> float:
    """Return an explicit margin_ratio or sample a plausible laundering fee."""
    if margin_ratio is not None:
        return float(margin_ratio)
    lo, hi = MARGIN_RATIO_RANGE
    return round(rng.uniform(lo, hi), 4)


def _split_exact(total: float, n: int, rng: random.Random) -> List[float]:
    """Split `total` into `n` paise-exact parts summing back to it.

    Uses jittered weights (seeded) for natural-looking proportions, works in
    integer paise, and distributes the remainder so the float sum matches the
    original amount within float epsilon. Parts may be zero if total is tiny.
    """
    if n <= 0:
        return []
    cents = max(0, int(round(total * 100)))
    if n == 1:
        return [cents / 100.0]
    weights = [rng.uniform(0.75, 1.25) for _ in range(n)]
    scale = sum(weights)
    alloc = [int(cents * w / scale) for w in weights]
    remainder = cents - sum(alloc)
    order = sorted(range(n), key=lambda i: -weights[i])
    for k in range(remainder):
        alloc[order[k % n]] += 1
    return [a / 100.0 for a in alloc]


def _schedule_steps(world: WorldState, rng: random.Random, n_actions: int,
                    step_offset: int, max_lag: Optional[int]) -> List[int]:
    """Temporally spread n_actions over coming steps.

    Consecutive gaps are uniform over {1..max_lag}. Returns the absolute step
    for each action (world.current_step + step_offset + cumulative gaps).
    """
    if n_actions <= 0:
        return []
    if max_lag is None:
        max_lag = rng.randint(2, 8)
    base = world.current_step + step_offset
    steps = [base]
    for _ in range(n_actions - 1):
        steps.append(steps[-1] + rng.randint(1, max_lag))
    return steps


def _capacity(world: WorldState, from_id: str, desired: float,
              headroom: float = BALANCE_HEADROOM) -> float:
    """Clamp a desired transfer to what the sender can actually fund."""
    account = world.accounts.get(from_id)
    if account is None:            # external counterparty — no clamping
        return round(desired, 2)
    cap = max(account.balance * (1.0 - headroom), 0.0)
    return round(min(desired, cap), 2)


def _send(world: WorldState, from_id: str, to_id: str, amount: float,
          step: int, rng: random.Random, attack_id: str,
          trajectory_id: Optional[str] = None,
          mechanism: Optional[str] = None):
    """Log one solvency-checked, paise-rounded fraud transfer.

    Returns the transaction dict, or None when the sender cannot fund it
    (skipped rather than overdrafted). The RETURNED amount is authoritative —
    layered typologies must chain off `tx["amount"]`, never off the request.
    """
    amt = _capacity(world, from_id, amount)
    if amt < MIN_TRANSFER_INR:
        return None
    return world.log_transaction(
        from_id=from_id,
        to_id=to_id,
        amount=amt,
        step=step,
        category="p2p",
        device=_pick_device(rng, world, from_id),
        ip=_pick_ip(rng, world),
        is_fraud=True,
        attack_id=attack_id,
        trajectory_id=trajectory_id,
        mechanism=mechanism,
    )


def _pick_device(rng: random.Random, world: WorldState,
                 account_id: str) -> Optional[str]:
    """Pick a device linked to the account, or create one."""
    account = world.accounts.get(account_id)
    if account is None:
        return None
    if account.linked_devices:
        return rng.choice(account.linked_devices)
    device = world.add_device()
    account.linked_devices.append(device.device_id)
    device.linked_accounts.append(account_id)
    return device.device_id


def _pick_ip(rng: random.Random, world: WorldState) -> Optional[str]:
    if world.ips:
        return rng.choice(list(world.ips.keys()))
    return None


# ---------------------------------------------------------------------------
# Typology 1: Fan-In (N → 1)
# ---------------------------------------------------------------------------

def fan_in(world: WorldState, rng: random.Random, *,
           attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
           step_offset: int = 0,
           main_account: str = "", members: Optional[List[str]] = None,
           amount: Optional[float] = None,
           timing_max_lag: Optional[int] = None,
           mechanism: Optional[str] = None, **kwargs) -> List[str]:
    """Multiple accounts send to one main account (N→1).

    The principal is split into solvency-aware shares across members; the
    exact float split sums back to the principal (paise precision).
    """
    tx_ids: List[str] = []
    if not members or not main_account:
        return tx_ids
    principal = amount if amount is not None else _fraud_amount(rng)
    shares = _split_exact(principal, len(members), rng)
    steps = _schedule_steps(world, rng, len(members), step_offset, timing_max_lag)
    for src, share, step in zip(members, shares, steps):
        tx = _send(world, src, main_account, share, step, rng,
                   attack_id, trajectory_id,
                   mechanism=mechanism)
        if tx is not None:
            tx_ids.append(tx["tx_id"])
    return tx_ids


# ---------------------------------------------------------------------------
# Typology 2: Fan-Out (1 → N)
# ---------------------------------------------------------------------------

def fan_out(world: WorldState, rng: random.Random, *,
            attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
            step_offset: int = 0,
            main_account: str = "", members: Optional[List[str]] = None,
            amount: Optional[float] = None,
            timing_max_lag: Optional[int] = None,
           mechanism: Optional[str] = None, **kwargs) -> List[str]:
    """Main account distributes to multiple members (1→N). Exact split."""
    tx_ids: List[str] = []
    if not members or not main_account:
        return tx_ids
    principal = amount if amount is not None else _fraud_amount(rng)
    shares = _split_exact(principal, len(members), rng)
    steps = _schedule_steps(world, rng, len(members), step_offset, timing_max_lag)
    for tgt, share, step in zip(members, shares, steps):
        tx = _send(world, main_account, tgt, share, step, rng,
                   attack_id, trajectory_id,
                   mechanism=mechanism)
        if tx is not None:
            tx_ids.append(tx["tx_id"])
    return tx_ids


# ---------------------------------------------------------------------------
# Typology 3: Cycle
# ---------------------------------------------------------------------------

def cycle(world: WorldState, rng: random.Random, *,
          attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
          step_offset: int = 0,
          members: Optional[List[str]] = None,
          amount: Optional[float] = None,
          margin_ratio: Optional[float] = None,
          timing_max_lag: Optional[int] = None,
           mechanism: Optional[str] = None, **kwargs) -> List[str]:
    """Closed ring: each member forwards to the next; final hop returns to the
    initiator.

    Edge i carries principal*(1-margin_ratio)^i, chained off the previous
    hop's ACTUAL logged amount (solvency-safe). The n-1 intermediaries each
    keep a margin cut on receipt, so the initiator ends up losing exactly
    principal*(1-(1-m)^(n-1)) — and no hop creates or destroys value.
    """
    tx_ids: List[str] = []
    if not members or len(members) < 2:
        return tx_ids
    n = len(members)
    m = _resolve_margin(rng, margin_ratio)
    principal = amount if amount is not None else _fraud_amount(rng)
    steps = _schedule_steps(world, rng, n, step_offset, timing_max_lag)
    moving = principal                      # amount currently in flight
    for i in range(n):
        src = members[i]
        tgt = members[(i + 1) % n]
        tx = _send(world, src, tgt, moving, steps[i], rng,
                   attack_id, trajectory_id,
                   mechanism=mechanism)
        if tx is not None:
            tx_ids.append(tx["tx_id"])
            moving = tx["amount"]           # forward what ACTUALLY arrived
        moving *= (1.0 - m)                 # the forwarding node keeps its cut
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
                   margin_ratio: Optional[float] = None,
                   timing_max_lag: Optional[int] = None,
           mechanism: Optional[str] = None, **kwargs) -> List[str]:
    """Main → intermediaries → beneficiary, with the FULL principal scattered.

    Fix vs legacy: the source scatters the whole principal (previously an
    unexplained 50% haircut left the beneficiary ~45%). Each intermediary
    forwards its receipt minus its margin cut. gathered == scattered*(1-m)
    exactly; nothing is created or destroyed.
    """
    tx_ids: List[str] = []
    if not intermediaries or not beneficiary or not main_account:
        return tx_ids
    m = _resolve_margin(rng, margin_ratio)
    principal = amount if amount is not None else _fraud_amount(rng)
    shares = _split_exact(principal, len(intermediaries), rng)
    steps = _schedule_steps(world, rng, len(intermediaries) * 2,
                            step_offset, timing_max_lag)
    for i, (inter, share) in enumerate(zip(intermediaries, shares)):
        s_step, g_step = steps[2 * i], steps[2 * i + 1]
        tx1 = _send(world, main_account, inter, share, s_step, rng,
                    attack_id, trajectory_id,
                   mechanism=mechanism)
        if tx1 is None:
            continue
        tx_ids.append(tx1["tx_id"])
        tx2 = _send(world, inter, beneficiary, tx1["amount"] * (1.0 - m),
                    g_step, rng, attack_id, trajectory_id)
        if tx2 is not None:
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
                   amount: Optional[float] = None,
                   margin_ratio: Optional[float] = None,
                   timing_max_lag: Optional[int] = None,
           mechanism: Optional[str] = None, **kwargs) -> List[str]:
    """Sources → main → targets, money conserved.

    LEGACY BUG FIXED: previously every source sent a FULL amount AND every
    target received a FULL amount — if len(sources) != len(targets), value was
    created or destroyed (finding #11). Now the gathered pool is explicit;
    the main account retains `margin_ratio` and redistributes the remainder
    across targets as an exact split.
    """
    tx_ids: List[str] = []
    if not sources or not targets or not main_account:
        return tx_ids
    m = _resolve_margin(rng, margin_ratio)
    principal = amount if amount is not None else _fraud_amount(rng)
    gather_shares = _split_exact(principal, len(sources), rng)
    g_steps = _schedule_steps(world, rng, len(sources), step_offset, timing_max_lag)

    gathered_total = 0.0
    for src, share, step in zip(sources, gather_shares, g_steps):
        tx = _send(world, src, main_account, share, step, rng,
                   attack_id, trajectory_id,
                   mechanism=mechanism)
        if tx is not None:
            tx_ids.append(tx["tx_id"])
            gathered_total += tx["amount"]

    if gathered_total <= 0.0:
        return tx_ids
    scatter_pool = gathered_total * (1.0 - m)      # main keeps the margin
    scatter_shares = _split_exact(scatter_pool, len(targets), rng)
    s_steps = _schedule_steps(
        world, rng, len(targets),
        step_offset + (g_steps[-1] - (world.current_step + step_offset)) + 1,
        timing_max_lag)
    for tgt, share, step in zip(targets, scatter_shares, s_steps):
        tx = _send(world, main_account, tgt, share, step, rng,
                   attack_id, trajectory_id,
                   mechanism=mechanism)
        if tx is not None:
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
              amount: Optional[float] = None,
              timing_max_lag: Optional[int] = None,
           mechanism: Optional[str] = None, **kwargs) -> List[str]:
    """Two disjoint sets: each source sends an exact share of the principal to
    a randomly chosen target."""
    tx_ids: List[str] = []
    if not sources or not targets:
        return tx_ids
    principal = amount if amount is not None else _fraud_amount(rng)
    shares = _split_exact(principal, len(sources), rng)
    steps = _schedule_steps(world, rng, len(sources), step_offset, timing_max_lag)
    for src, share, step in zip(sources, shares, steps):
        tgt = rng.choice(targets)
        tx = _send(world, src, tgt, share, step, rng,
                   attack_id, trajectory_id,
                   mechanism=mechanism)
        if tx is not None:
            tx_ids.append(tx["tx_id"])
    return tx_ids


# ---------------------------------------------------------------------------
# Typology 7: Stack (Stacked Bipartite)
# ---------------------------------------------------------------------------

def stack(world: WorldState, rng: random.Random, *,
          attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
          step_offset: int = 0,
          layers: Optional[List[List[str]]] = None,
          amount: Optional[float] = None,
          margin_ratio: Optional[float] = None,
          timing_max_lag: Optional[int] = None,
           mechanism: Optional[str] = None, **kwargs) -> List[str]:
    """Layer cascade: each layer's pooled receipts minus its margin cut are
    fanned down to the next layer.

    LEGACY BUG FIXED: previously every (src,tgt) pair in every boundary sent a
    CONSTANT amount, ignoring pool size — with asymmetric layer shapes money
    appeared out of nowhere (finding #11). Now a running pool tracks actuals:
        pool_{b+1} = (sum of actually-sent edge amounts at boundary b) * (1-m)
    and the edges at boundary b split that pool exactly.
    """
    tx_ids: List[str] = []
    if not layers or len(layers) < 2:
        return tx_ids
    m = _resolve_margin(rng, margin_ratio)
    principal = amount if amount is not None else _fraud_amount(rng)
    pool = principal
    cursor = step_offset
    for b in range(len(layers) - 1):
        cur_layer, nxt_layer = layers[b], layers[b + 1]
        n_edges = len(cur_layer) * len(nxt_layer)
        if n_edges == 0 or pool <= 0.0:
            pool = 0.0
            break
        edge_shares = _split_exact(pool, n_edges, rng)
        steps = _schedule_steps(world, rng, n_edges, cursor, timing_max_lag)
        sent_total = 0.0
        k = 0
        for src in cur_layer:
            for tgt in nxt_layer:
                tx = _send(world, src, tgt, edge_shares[k], steps[k], rng,
                           attack_id, trajectory_id,
                   mechanism=mechanism)
                if tx is not None:
                    tx_ids.append(tx["tx_id"])
                    sent_total += tx["amount"]
                k += 1
        pool = sent_total * (1.0 - m)               # this layer keeps its cut
        cursor = steps[-1] + 1
    return tx_ids


# ---------------------------------------------------------------------------
# Typology 8: Random / Propagation
# ---------------------------------------------------------------------------

def random_typology(world: WorldState, rng: random.Random, *,
                    attack_id: str = "AUTO", trajectory_id: Optional[str] = None,
                    step_offset: int = 0,
                    main_account: str = "", depth: int = 3,
                    amount: Optional[float] = None,
                    margin_ratio: Optional[float] = None,
                    timing_max_lag: Optional[int] = None,
           mechanism: Optional[str] = None, **kwargs) -> List[str]:
    """Main → neighbour → neighbour-of-neighbour … propagating outward.

    LEGACY BUG FIXED: each hop previously re-sent the FULL principal, minting
    money at every hop (finding #11). Now each hop forwards what it actually
    received minus its margin cut, exactly like an open-chain layering walk.
    """
    tx_ids: List[str] = []
    m = _resolve_margin(rng, margin_ratio)
    moving = amount if amount is not None else _fraud_amount(rng)
    all_accounts = list(world.accounts.keys())
    if len(all_accounts) < 2:
        return tx_ids
    steps = _schedule_steps(world, rng, depth, step_offset, timing_max_lag)
    current_source = main_account
    for hop in range(depth):
        candidates = [a for a in all_accounts if a != current_source]
        if not candidates:
            break
        tgt = rng.choice(candidates)
        tx = _send(world, current_source, tgt, moving, steps[hop], rng,
                   attack_id, trajectory_id,
                   mechanism=mechanism)
        if tx is not None:
            tx_ids.append(tx["tx_id"])
            moving = tx["amount"] * (1.0 - m)
        else:
            moving *= (1.0 - m)          # can't move — value stays put
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
