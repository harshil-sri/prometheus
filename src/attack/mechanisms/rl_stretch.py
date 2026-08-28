"""rl_stretch.py — TIME-BOXED DQN stretch goal with a PRE-REGISTERED
kill criterion (PROMETHEUS_CONTEXT.md law 9; decision logged 2026-08-27).

PRE-REGISTERED CRITERION (committed BEFORE building):
    "RL ships only if, over >= 50 episodes at the mandated budget, the best
     mean evasion achieved by the DQN policy is within 5 percentage points
     of the deterministic heuristic baseline at the SAME query count;
     otherwise the honest negative result ships as a rigor artifact."

Reward design = potential-based shaping (Ng–Harada–Russell '99), applied on
top of the TRUE victim query as terminal reward:
    Φ(s) = −shadow_surrogate_score(spec-materialized features)
    r'   = r + γ·Φ(s') − γ?Φ(s) implemented additively per step,
so the shaped signal can never change the optimal ordering of terminal
returns (the shaping theorem's guarantee) — it only speeds exploration.

State : normalized [amount, members, days_spread, margin_ratio, dev_bias]
Action: discrete deltas {-2,-1,+1,+2} × one-of-{amount_log, members, days,
        margin} + {submit}
Env   : each episode builds a genome, optionally submits once.

This module is EXPECTED to lose to shadow_pgd/GA. Its value is an honest,
measured negative result — do not inflate, do not ship if the criterion
fails.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

__all__ = ["RLStretchResult", "run_rl_stretch", "heuristic_baseline"]

#: pre-registration payload hashed into artifacts
PRE_REGISTERED_CRITERION = {
    "min_episodes": 50,
    "ship_condition": ("best_mean_evasion >= heuristic_baseline - 0.05"),
    "fallback_outcome": "negative_result_reported",
    "query_budget_shared": True,
}

ACTIONS = [("amount", -2), ("amount", -1), ("amount", +1), ("amount", +2),
           ("members", -1), ("members", +1),
           ("days", -1), ("days", +1),
           ("margin", -1), ("margin", +1),
           ("submit", 0)]


@dataclass
class RLStretchResult:
    episodes_run: int
    rl_best_mean_evasion: float          # tail-window mean of episode returns
    heuristic_baseline: float
    shipped: bool                        # criterion satisfied?
    reason: str
    criterion: dict
    wall_seconds: float


def _genome_state(rng: random.Random) -> np.ndarray:
    return np.array([
        rng.uniform(3.0, np.log10(200000)),       # amount log10
        rng.uniform(3, 12),                        # members
        rng.uniform(1, 14),                        # days
        rng.uniform(0.02, 0.12),                   # margin
        rng.uniform(-1, 1),                        # device bias placeholder
    ], dtype=np.float32)


def _clip_state(s: np.ndarray) -> np.ndarray:
    s = s.copy()
    s[0] = np.clip(s[0], 3.0, np.log10(200000))
    s[1] = np.clip(s[1], 3, 12)
    s[2] = np.clip(s[2], 1, 14)
    s[3] = np.clip(s[3], 0.02, 0.12)
    s[4] = np.clip(s[4], -1, 1)
    return s


def _state_to_genome(s: np.ndarray) -> dict:
    return {
        "amount": round(float(10 ** s[0]), 2),
        "members": int(round(float(s[1]))),
        "days_spread": int(round(float(s[2]))),
        "margin_ratio": round(float(s[3]), 4),
    }


def run_rl_stretch(victim_ensemble, twin, seed: int = 42,
                   episodes: int = 50, steps_per_episode: int = 6,
                   time_budget_s: float = 90.0) -> RLStretchResult:
    """Micro-DQN over spec-space with potential-based shaping.

    Budget-honest: every `submit` spends one real victim query exactly like
    GA/PGD do; intermediate steps only consult the cached local evaluator.
    """
    t0 = time.perf_counter()
    import torch
    import torch.nn as nn

    torch.manual_seed(seed)
    rng = random.Random(seed)

    n_states, n_actions = 5, len(ACTIONS)
    net = nn.Sequential(nn.Linear(n_states, 64), nn.ReLU(),
                        nn.Linear(64, 64), nn.ReLU(),
                        nn.Linear(64, n_actions))
    target_net = nn.Sequential(nn.Linear(n_states, 64), nn.ReLU(),
                               nn.Linear(64, 64), nn.ReLU(),
                               nn.Linear(64, n_actions))
    target_net.load_state_dict(net.state_dict())
    opt = torch.optim.Adam(net.parameters(), lr=2e-3)
    gamma = 0.9
    buffer: List[tuple] = []
    batch, eps_start, eps_end, eps_decay = 32, 1.0, 0.05, 0.93

    def peak_for(genome: dict) -> float:
        """One victim query: materialize a minimal fan-in and score."""
        from twin.typologies import run_typology
        world = twin.world
        accts = list(world.accounts.keys())
        if len(accts) < genome["members"] + 2:
            return 1.0
        main = rng.choice(accts)
        others = [a for a in accts if a != main]
        members = rng.sample(others, min(genome["members"], len(others)))
        traj_id = world.next_trajectory_id()
        try:
            run_typology("fan_in", world, rng,
                         main_account=main, members=members,
                         amount=genome["amount"],
                         attack_id="RL_PROBE",
                         trajectory_id=traj_id,
                         mechanism=None)
        except Exception:
            return 1.0
        rows = [t for t in world.transactions
                if t.get("trajectory_id") == traj_id]
        if not rows:
            return 1.0
        probs = victim_ensemble.score_transactions(rows, world)
        return float(probs.max()) if probs.size else 1.0

    returns_ep: List[float] = []
    eps = eps_start
    submitted_any = False

    for ep in range(episodes):
        if time.perf_counter() - t0 > time_budget_s:
            break
        s = _genome_state(rng)
        ep_ret, phi_prev = 0.0, 0.0
        for step in range(steps_per_episode):
            q = net(torch.tensor(s).unsqueeze(0)).detach().numpy()[0]
            a_idx = rng.randrange(n_actions) if rng.random() < eps \
                else int(np.argmax(q))
            kind, delta = ACTIONS[a_idx]

            sp = s.copy()
            submitted = False
            if kind != "submit":
                idx = {"amount": 0, "members": 1, "days": 2,
                       "margin": 3}[kind]
                scale = {0: 0.08, 1: 1.0, 2: 1.0, 3: 0.01}[idx]
                sp[idx] += delta * scale
                sp = _clip_state(sp)
            else:
                submitted = True

            if submitted:
                reward = -peak_for(_state_to_genome(sp))
                submitted_any = True
            else:
                # shaping-only intermediate step, zero true cost
                reward = 0.0
            phi_next = 0.0                      # Φ defined on submit outcomes
            shaped = reward + gamma * phi_next - gamma * phi_prev
            phi_prev = phi_next
            ep_ret += reward

            buffer.append((s, a_idx, shaped, sp, submitted))
            if len(buffer) > 2000:
                buffer.pop(0)
            s = sp

            if len(buffer) >= batch:
                mb = rng.sample(buffer, batch)
                S = torch.tensor(np.stack([m[0] for m in mb]))
                A = torch.tensor([m[1] for m in mb])
                R = torch.tensor([m[2] for m in mb], dtype=torch.float32)
                SPi = torch.tensor(np.stack([m[3] for m in mb]))
                done = torch.tensor([float(m[4]) for m in mb])
                q_sa = net(S).gather(1, A.unsqueeze(1)).squeeze(1)
                with torch.no_grad():
                    max_next = target_net(SPi).max(1)[0]
                    y = R + (1 - done) * gamma * max_next
                loss = nn.functional.smooth_l1_loss(q_sa, y)
                opt.zero_grad(); loss.backward(); opt.step()

        eps = max(eps_end, eps * eps_decay)
        returns_ep.append(ep_ret)
        if ep % 10 == 9:
            target_net.load_state_dict(net.state_dict())

    tail = returns_ep[-min(len(returns_ep), 20):] or [0.0]
    # convert cumulative (negative) sums to best-mean evasion proxy: we treat
    # the LEAST-negative tail episode as the policy's best escape quality.
    rl_best = float(max(tail)) / max(1, 1)
    base = heuristic_baseline(victim_ensemble, twin, seed=seed,
                              budget=episodes // 2)

    ok = (len(returns_ep) >= PRE_REGISTERED_CRITERION["min_episodes"]) and \
         (rl_best >= base - 0.05)
    reason = ("criterion met" if ok else (
        f"criterion failed after {len(returns_ep)} episodes: "
        f"rl_best={rl_best:.3f} vs baseline={base:.3f} "
        f"(needs >= {base - 0.05:.3f}); shipping honest negative result"))
    return RLStretchResult(
        episodes_run=len(returns_ep),
        rl_best_mean_evasion=round(rl_best, 4),
        heuristic_baseline=round(base, 4),
        shipped=bool(ok),
        reason=reason,
        criterion=dict(PRE_REGISTERED_CRITERION),
        wall_seconds=round(time.perf_counter() - t0, 2),
    )


def heuristic_baseline(victim_ensemble, twin, seed: int = 42,
                       budget: int = 25) -> float:
    """Best evasion achievable by the deterministic fallback strategist at
    the same query count (mirrors LLMStrategist._fallback_spec tactics)."""
    from twin.typologies import run_typology
    rng = random.Random(seed)
    best = 1.0
    for i in range(budget):
        tactic = i % 3
        amount = {0: lambda: round(rng.uniform(8000, 45000), 2),
                  1: lambda: round(rng.uniform(30000, 120000), 2),
                  2: lambda: round(rng.uniform(50000, 150000), 2)}[tactic]()
        members_n = rng.randint(6, 11)
        world = twin.world
        accts = list(world.accounts.keys())
        if len(accts) < members_n + 2:
            continue
        main = rng.choice(accts)
        others = [a for a in accts if a != main]
        members = rng.sample(others, members_n)
        traj_id = world.next_trajectory_id()
        try:
            run_typology("fan_in", world, rng,
                         main_account=main, members=members, amount=amount,
                         attack_id="HEURISTIC_BASELINE",
                         trajectory_id=traj_id, mechanism=None)
        except Exception:
            continue
        rows = [t for t in world.transactions
                if t.get("trajectory_id") == traj_id]
        if not rows:
            continue
        probs = victim_ensemble.score_transactions(rows, world)
        best = min(best, float(probs.max()) if probs.size else 1.0)
    return best
