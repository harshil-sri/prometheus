"""spectral.py — Spectral ego-graph fingerprints (idea #5, Phase 6).

For each transaction we build the sender's CAUSAL ego-graph at its step:
    nodes = sender + its distinct counterparties so far (capped, most recent)
    edges = any observed direct transfer between those entities (symmetrized)

The symmetric adjacency's eigenvalues are compared against the two
closed-form archetypes that dominate laundering topology:

  perfect ring of k nodes : λ_j = 2·cos(2πj/k), j = 1..k
  star K_{1,n}            : {√n, −√n} plus zeros

Reporting residuals to these IDEAL spectra gives near-zero signals only when
the local subgraph truly is that shape — a computed discriminator against
cycle layering and fan-in/fan-out bursts (audit idea #5; TDA remains a
stretch goal and is NOT claimed here).

Causality: rows are emitted in step order and the lookup mutates AFTER each
row is processed, so earlier transactions can never see later edges.
Deterministic: no RNG anywhere.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Sequence, Tuple

import numpy as np

__all__ = ["SPECTRAL_FEATURE_NAMES", "compute_spectral_features",
           "ego_adjacency", "spectral_profile"]

N_SPECTRAL_FEATURES_EXPECTED = 8

#: max counterparties kept per ego-graph (bounded compute, ~10x10 eig)
DEFAULT_EGO_CAP = 10

SPECTRAL_FEATURE_NAMES = [
    "spec_lambda_max",
    "spec_neg_magnitude",
    "spec_cycle_residual",
    "spec_star_residual",
    "spec_neighbor_edges",
    "spec_ego_size",
    "spec_density",
    "spec_triangle_proxy",
]


# ---------------------------------------------------------------------------
# Ego-graph construction
# ---------------------------------------------------------------------------

class _TxGraphLookup:
    """Incremental pair-index over transactions, mutated causally."""

    def __init__(self):
        # account -> set of counterparties ever seen
        self.neighbors: Dict[str, set] = defaultdict(set)
        # frozenset({a,b}) -> count   (unordered pair, self excluded)
        self.pairs: Dict[frozenset, int] = defaultdict(int)
        # account -> deque-like list of counterparties in recency order
        self.recent: Dict[str, List[str]] = defaultdict(list)

    def observe(self, frm: str, to: str) -> None:
        if not frm or not to or frm == to:
            return
        self.pairs[frozenset((frm, to))] += 1
        # maintain recency for BOTH endpoints (an ego must see inbound edges)
        for center, nb in ((frm, to), (to, frm)):
            if nb not in self.neighbors[center]:
                self.neighbors[center].add(nb)
            lst = self.recent[center]
            if nb in lst:
                lst.remove(nb)
            lst.append(nb)

    def ego_nodes(self, center: str, cap: int) -> List[str]:
        rec = list(reversed(self.recent.get(center, [])))   # most recent first
        return list(reversed(rec))[-cap:]

    def adjacency(self, center: str, nodes: List[str]) -> np.ndarray:
        n = len(nodes) + 1                         # + center slot 0
        A = np.zeros((n, n), dtype=np.float64)
        idx = {nid: i + 1 for i, nid in enumerate(nodes)}
        for nb in nodes:
            if frozenset((center, nb)) in self.pairs:
                A[0, idx[nb]] = 1.0
                A[idx[nb], 0] = 1.0
        for i_i in range(len(nodes)):
            for j_i in range(i_i + 1, len(nodes)):
                if frozenset((nodes[i_i], nodes[j_i])) in self.pairs:
                    A[idx[nodes[i_i]], idx[nodes[j_i]]] = 1.0
                    A[idx[nodes[j_i]], idx[nodes[i_i]]] = 1.0
        return A


def ego_adjacency(transactions: Sequence[dict], index: int,
                  cap: int = DEFAULT_EGO_CAP,
                  lookup: "_TxGraphLookup | None" = None
                  ) -> Tuple[np.ndarray, "_TxGraphLookup"]:
    """Adjacency matrix for the ego-graph of transactions[index]'s sender.

    State SEMANTICS: the current transaction's own edge IS included (the
    row is scored knowing what it did) — lookup covers txs[:index+1].
    Pass a shared lookup inside batch loops for O(total) processing."""
    own = False
    if lookup is None:
        own = True
        lookup = _TxGraphLookup()
        for t in transactions[:index + 1]:
            lookup.observe(str(t.get("from", "")), str(t.get("to", "")))

    tx = transactions[index]
    frm = str(tx.get("from", ""))
    to = str(tx.get("to", ""))
    nodes_all = [n for n in lookup.ego_nodes(frm, cap - 1)]
    if to not in nodes_all:
        nodes_all.append(to)
    else:
        nodes_all = nodes_all
    nodes = nodes_all[:cap]
    A = lookup.adjacency(frm, nodes)
    return A, (lookup if not own else None)


# ---------------------------------------------------------------------------
# Spectral profile vs closed-form ideals
# ---------------------------------------------------------------------------

def spectral_profile(A: np.ndarray) -> Dict[str, float]:
    """Eigen-spectrum residuals of one ego-adjacency matrix."""
    n = A.shape[0]
    out = {
        "spec_lambda_max": 0.0,
        "spec_neg_magnitude": 0.0,
        "spec_cycle_residual": 0.0,
        "spec_star_residual": 0.0,
        "spec_neighbor_edges": float(np.sum(np.triu(A, 1))),
        "spec_ego_size": float(n),
        "spec_density": 0.0,
        "spec_triangle_proxy": 0.0,
    }
    if n < 3 or out["spec_neighbor_edges"] == 0:
        if n > 1:
            dens = out["spec_neighbor_edges"] / (n * (n - 1) / 2.0)
            out["spec_density"] = round(dens, 6)
        return out

    w = np.linalg.eigvalsh(A)                     # ascending real parts
    lam_max = float(w[-1])
    neg_mag = float(max(-w[0], 0.0))

    # ring ideal: λ_j = 2 cos(2πj/n), j=0..n−1 → compare sorted magnitudes
    ideal_ring = np.sort([2.0 * math.cos(2.0 * math.pi * j / n)
                          for j in range(n)])
    cycle_residual = float(np.mean(np.abs(w - ideal_ring)))

    # star ideal K_{1,n−1}: {±√(n−1)} plus zeros
    star_ideal = np.zeros(n)
    star_ideal[0], star_ideal[-1] = -math.sqrt(n - 1), math.sqrt(n - 1)
    # align extremes (spectra are sets): compare sorted arrays
    star_residual = float(np.mean(np.abs(np.sort(w) - np.sort(star_ideal))))

    m = out["spec_neighbor_edges"]
    dens = m / (n * (n - 1) / 2.0)
    tri_proxy = float((np.trace(np.linalg.matrix_power(A, 3)) / 6.0)
                      / max(1.0, m))

    out.update({
        "spec_lambda_max": round(lam_max, 6),
        "spec_neg_magnitude": round(neg_mag, 6),
        "spec_cycle_residual": round(cycle_residual, 6),
        "spec_star_residual": round(star_residual, 6),
        "spec_density": round(dens, 6),
        "spec_triangle_proxy": round(tri_proxy, 6),
    })
    return out


# ---------------------------------------------------------------------------
# Batch computation (causal + deterministic)
# ---------------------------------------------------------------------------

def compute_spectral_features(transactions: Sequence[dict],
                              cap: int = DEFAULT_EGO_CAP
                              ) -> tuple[np.ndarray, List[str]]:
    """Rows aligned 1:1 with `transactions`, in original order.

    Returns (X_spec (n, 8) float32, SPECTRAL_FEATURE_NAMES)."""
    lookup = _TxGraphLookup()
    X = np.zeros((len(transactions), len(SPECTRAL_FEATURE_NAMES)),
                 dtype=np.float32)
    if not transactions:
        return X, list(SPECTRAL_FEATURE_NAMES)

    # process in stable causal order but write back to original indices
    order = sorted(range(len(transactions)),
                   key=lambda i: (int(t_step(transactions[i])), i))
    pos_in_order = {}
    for pos, i in enumerate(order):
        pos_in_order[i] = pos

    for i, txi in enumerate(order):
        t = transactions[txi]
        frm, to = str(t.get("from", "")), str(t.get("to", ""))
        # the row's own edge is part of its state: observe THEN compute
        lookup.observe(frm, to)
        nodes_all = lookup.ego_nodes(frm, cap - 1)
        if to not in nodes_all:
            nodes_all.append(to)
        nodes = nodes_all[:cap]
        A = lookup.adjacency(frm, nodes)
        prof = spectral_profile(A)
        X[txi, :] = [prof[k] for k in SPECTRAL_FEATURE_NAMES]
    return X, list(SPECTRAL_FEATURE_NAMES)


# ---------------------------------------------------------------------------
# Batch computation (causal + deterministic)
# ---------------------------------------------------------------------------

def induced_spectrum_profile(transactions: Sequence[dict],
                             entities: Sequence[str]) -> Dict[str, float]:
    """Spectral profile of the subgraph INDUCED by an explicit entity set,
    over the complete transaction history.

    This is where the closed-form archetypes live literally: hand a full
    laundering ring's account ids and the spectrum IS 2cos(2πj/k); hand a
    hub-and-leaves set and it IS {±√(n−1)}. Per-tx ego rows approximate
    these signatures causally; this helper verifies them exactly.
    """
    lookup = _TxGraphLookup()
    for t in transactions:
        lookup.observe(str(t.get("from", "")), str(t.get("to", "")))
    ents = [e for e in dict.fromkeys(entities)]
    idx = {e: i for i, e in enumerate(ents)}
    n = len(ents)
    A = np.zeros((n, n))
    for pr, cnt in lookup.pairs.items():
        a, b = tuple(pr)
        if a in idx and b in idx:
            A[idx[a], idx[b]] = 1.0
            A[idx[b], idx[a]] = 1.0
    return spectral_profile(A)


def t_step(t: dict) -> int:
    try:
        return int(t.get("step", 0))
    except (TypeError, ValueError):
        return 0
