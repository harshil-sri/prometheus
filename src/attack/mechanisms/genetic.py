"""genetic.py — Black-box genetic search over attack-spec space (mechanism
"genetic").

Fitness = the victim's PEAK calibrated probability over an executed
trajectory (lower = better evasion). Every execution spends one query from a
hard budget — real black-box constraint, no oracle abuse. Surviving elite
trajectories are materialized into the world, tagged mechanism='genetic'.

Genome (spec-space, NOT feature-space like PGD — complementary axis):
    typology          categorical ∈ {fan_in, fan_out, bipartite, stack,
                                     scatter_gather}
    amount            log-uniform [1000 .. 200000], mutated multiplicatively
    members           int [3 .. 12]            # mule/intermediary count
    days_spread       int [1 .. 14]            # temporal spread pressure
    margin_ratio      float [0.02 .. 0.12]     # layering fee (layering types)

Deterministic given seed; elitism guarantees best-so-far monotonicity.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from blue.splits import register_mechanism

MECHANISM_NAME = "genetic"
register_mechanism(MECHANISM_NAME)

logger = logging.getLogger(__name__)

__all__ = ["GAOptimizeResult", "GAOptimizer"]

TYPOLOGIES = ["fan_in", "fan_out", "bipartite", "stack", "scatter_gather"]


@dataclass
class Genome:
    typology: str
    amount: float
    members: int
    days_spread: int
    margin_ratio: float


@dataclass
class GAOptimizeResult:
    budget_used: int
    generations: int
    best_genome: dict
    best_peak_score: float
    initial_best_peak: float
    history_best: List[float]
    elite_trajectory_ids: List[str] = field(default_factory=list)
    n_materialized: int = 0
    seed: int = 42


def _random_genome(rng: random.Random) -> Genome:
    return Genome(
        typology=rng.choice(TYPOLOGIES),
        amount=round(10 ** rng.uniform(3.0, np.log10(200000.0)), 2),
        members=rng.randint(3, 12),
        days_spread=rng.randint(1, 14),
        margin_ratio=round(rng.uniform(0.02, 0.12), 4),
    )


def _mutate(g: Genome, rng: random.Random) -> Genome:
    g = Genome(**vars(g))
    gene = rng.randrange(5)
    if gene == 0:
        g.typology = rng.choice(TYPOLOGIES)
    elif gene == 1:
        g.amount = round(min(200000.0, max(1000.0,
                        g.amount * rng.uniform(0.6, 1.6))), 2)
    elif gene == 2:
        g.members = int(np.clip(g.members + rng.randint(-2, 2), 3, 12))
    elif gene == 3:
        g.days_spread = int(np.clip(g.days_spread + rng.randint(-2, 2), 1, 14))
    else:
        g.margin_ratio = round(float(np.clip(
            g.margin_ratio + rng.uniform(-0.02, 0.02), 0.02, 0.12)), 4)
    return g


def _crossover(a: Genome, b: Genome, rng: random.Random) -> Genome:
    d = vars(a)
    gb = vars(b)
    for k in rng.sample(list(d.keys()), k=2):
        d[k] = gb[k]
    return Genome(**d)


class GAOptimizer:
    """Execute-and-score GA against the live victim ensemble."""

    def __init__(self, victim_ensemble, twin, seed: int = 42,
                 budget_queries: int = 60, population: int = 8,
                 max_generations: int = 6, threshold: float = 0.5):
        self.victim = victim_ensemble
        self.twin = twin
        self.seed = seed
        self.rng = random.Random(seed)
        self.budget = budget_queries
        self.population = population
        self.max_generations = max_generations
        self.threshold = threshold
        self.queries_used = 0

    # -- genome execution -------------------------------------------------

    def _execute(self, g: Genome, attack_id: str) -> Optional[List[dict]]:
        """Materialize a genome as transactions; returns its rows or None."""
        from twin.typologies import run_typology
        world = self.twin.world
        accounts = list(world.accounts.keys())
        if len(accounts) < g.members + 2:
            return None

        main = self.rng.choice(accounts)
        others = [a for a in accounts if a != main]
        members = self.rng.sample(others, min(g.members, len(others)))
        traj_id = world.next_trajectory_id()
        step_offset = self.rng.randint(0, max(1, g.days_spread))

        base = {"main_account": main, "amount": g.amount,
                "attack_id": attack_id, "trajectory_id": traj_id,
                "step_offset": step_offset,
                "mechanism": MECHANISM_NAME}

        kwargs: Dict = dict(base)
        rows: List[dict] = []
        try:
            if g.typology in ("fan_in", "fan_out"):
                kwargs.update(members=members)
                run_typology(g.typology, world, self.rng, **kwargs)
            elif g.typology == "bipartite":
                half = max(1, len(members) // 2)
                kwargs.update(sources=members[:half],
                              targets=members[half:] or members[:1])
                run_typology("bipartite", world, self.rng, **kwargs)
            elif g.typology == "scatter_gather":
                kwargs.update(main_account=main,
                              intermediaries=members[:-1] or members,
                              beneficiary=members[-1],
                              margin_ratio=g.margin_ratio)
                run_typology("scatter_gather", world, self.rng, **kwargs)
            elif g.typology == "stack":
                layers = [members[i::3] or members for i in range(3)]
                kwargs.pop("main_account", None)
                kwargs.update(layers=layers, margin_ratio=g.margin_ratio)
                run_typology("stack", world, self.rng, **kwargs)
        except Exception as exc:
            logger.debug("genome execution failed: %s", exc)
            return None

        rows = [t for t in world.transactions
                if t.get("trajectory_id") == traj_id]
        if not rows:
            return None

        world.log_trajectory(
            attack_type="genetic",
            actions=[{"action": "ga_genome", "genome": vars(g),
                      "tx_ids": [r["tx_id"] for r in rows]}],
            spec={"seed": self.seed, "mechanism": MECHANISM_NAME},
            trajectory_id=traj_id)
        return rows

    def _fitness(self, genome: Genome, attack_id: str) -> Optional[float]:
        if self.queries_used >= self.budget:
            return None
        self.queries_used += 1
        rows = self._execute(genome, attack_id)
        if not rows:
            return 1.0                     # failed execution = worst fitness
        probs = self.victim.score_transactions(rows, self.twin.world)
        return float(probs.max()) if probs.size else 1.0

    # ------------------------------------------------------------------ #
    def optimize(self, attack_id: str = "GA_SEARCH") -> GAOptimizeResult:
        rng = self.rng
        pop = [_random_genome(rng) for _ in range(self.population)]
        scored = [(self._fitness(g, attack_id) or 1.0, i, g)
                  for i, g in enumerate(pop)]
        # genome executions pick entities stochastically, so fitness is
        # noisy per query: track the OBSERVED running best explicitly.
        best_so_far = min(s for s, _, _ in scored)
        history_best = [best_so_far]
        initial_best = best_so_far

        gen = 0
        while gen < self.max_generations and \
                self.queries_used < self.budget - self.population // 2:
            gen += 1
            scored.sort(key=lambda t: t[0])
            elites = [g for _, _, g in scored[:2]]        # elitism

            children: List[Genome] = list(elites)
            while len(children) < self.population and \
                    self.queries_used < self.budget:
                a = rng.choice(scored)[2]
                b = rng.choice(scored)[2]
                child = _mutate(_crossover(a, b, rng), rng)
                children.append(child)

            new_scored = []
            for g in children:
                f = self._fitness(g, attack_id)
                new_scored.append((f if f is not None else 1.0,
                                   self.queries_used, g))
            scored = new_scored
            gen_best = min(s for s, _, _ in scored)
            best_so_far = min(best_so_far, gen_best)
            history_best.append(best_so_far)

        scored.sort(key=lambda t: t[0])
        best_fitness, _, best_g = scored[0]

        # Materialize top-2 elites (fresh executions)
        elite_ids: List[str] = []
        made = 0
        world = self.twin.world
        for rank, (_, _, g) in enumerate(scored[:2]):
            traj_rows = self._execute(g, f"GA_ELITE_{rank}")
            if traj_rows:
                elite_ids.append(traj_rows[0].get("trajectory_id", ""))
                made += len(traj_rows)

        return GAOptimizeResult(
            budget_used=self.queries_used,
            generations=gen,
            best_genome=dict(vars(best_g)),
            best_peak_score=float(best_fitness),
            initial_best_peak=float(initial_best),
            history_best=[round(x, 4) for x in history_best],
            elite_trajectory_ids=[t for t in elite_ids if t],
            n_materialized=made,
            seed=self.seed,
        )
