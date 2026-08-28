"""sanctions.py — Guarded sanctions screening agent.

Provider modes:
  * "fixture" (default, always available): screens against a watch list
    generated INSIDE the synthetic namespace from the twin seed.
  * "yente"   : would call OpenSanctions' yente API. GUARDED — refuses to
    transmit any name that is not in the registered synthetic namespace
    (law 6: nothing resembling real PII ever leaves), honors a hard call
    budget, and requires PROMETHEUS_SANCTIONS_URL when configured.

As an orchestrator delegate it NEVER runs free — every screen consumes
budget and returns evidence tagged with provenance.
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

__all__ = ["SanctionsAgent", "BudgetExceeded", "NameNotInSandbox",
           "build_watch_list"]


class BudgetExceeded(RuntimeError):
    """Delegate attempted more screens than the case budget allows."""


class NameNotInSandbox(RuntimeError):
    """Refusal guard: outbound name not present in the synthetic namespace."""


def _name_hash(name: str) -> str:
    return hashlib.sha256(f"sandbox:{name}".encode()).hexdigest()[:16]


def build_watch_list(names: Set[str], hit_ratio: float = 0.04,
                     seed: int = 42) -> Dict[str, Dict[str, Any]]:
    """Deterministic sanctions-style watch list over SYNTHETIC names only."""
    rng = random.Random(seed)
    chosen = sorted(n for n in names if rng.random() < hit_ratio)
    wl: Dict[str, Dict[str, Any]] = {}
    for n in chosen:
        wl[n] = {
            "list": rng.choice(["SDN-SANDBOX", "PEP-SANDBOX",
                                "ADVERSEMEDIA-SANDBOX"]),
            "match_strength": round(rng.uniform(0.72, 0.98), 3),
            "subject_ref": f"SBOX-{_name_hash(n)[:8]}",
        }
    return wl


class SanctionsAgent:
    """Screens names/ids against fixture or external provider."""

    def __init__(self, fixtures: Dict[str, Dict[str, Any]],
                 mode: str = "fixture", call_budget: int = 6,
                 yente_url_provider: Optional[Callable[[], str]] = None,
                 watch_seed: int = 42):
        self.fixtures = fixtures
        self.sandbox_names = set(fixtures.keys()) | {
            r.get("pseudonym") for r in fixtures.values()}
        self.mode = mode
        self.budget_left = call_budget
        self.yente_url_provider = yente_url_provider or (
            lambda: __import__("os").environ.get("PROMETHEUS_SANCTIONS_URL",
                                                 ""))
        self.watch_list = build_watch_list(self.sandbox_names,
                                           seed=watch_seed)

    # ------------------------------------------------------------------ #
    def _assert_budget(self) -> None:
        if self.budget_left <= 0:
            raise BudgetExceeded(
                f"sanctions budget exhausted ({self.__dict__.get('budget_total', 'n/a')})")
        self.budget_left -= 1

    def _refuse_outside_sandbox(self, name: str) -> None:
        if name not in self.sandbox_names:
            raise NameNotInSandbox(
                f"{name[:24]}!r is not part of the synthetic namespace; "
                f"external transmission refused"
                if self.mode == "yente" else
                f"{name[:24]!r} not found in sandbox fixtures")

    # ------------------------------------------------------------------ #
    def screen(self, entity_id: str, by_pseudonym: bool = False,
               ) -> Dict[str, Any]:
        """One screen. Returns evidence-ready dict; never fabricates hits.

        SECURITY ORDER: the synthetic-namespace refusal fires FIRST (even
        for unknown ids) so an attacker can't probe which ids exist via
        error-type differences."""
        target_id = entity_id
        rec = self.fixtures.get(entity_id)
        if by_pseudonym:
            if entity_id not in self.sandbox_names:
                raise NameNotInSandbox(
                    f"{entity_id[:24]!r} not in synthetic namespace")
            target_id = next(k for k, v in self.fixtures.items()
                             if v["pseudonym"] == entity_id)
        else:
            self._refuse_outside_sandbox(target_id)

        if rec is None:
            # known-not-in-sandbox semantics: unknown = no dossier, refuse
            raise NameNotInSandbox(f"{entity_id!r} has no sandbox dossier")

        target = rec["pseudonym"] if by_pseudonym else entity_id
        self._assert_budget()

        hit = None
        if self.mode == "fixture":
            entry = self.watch_list.get(target)
            if entry:
                hit = dict(entry)
        elif self.mode == "yente":
            # Real HTTP call stays OUT of scope for tests/demos unless env
            # configured; even then, transport goes through caller-injected
            # callable later (P10 hardening). Here: guarded refusal fallback.
            url = self.yente_url_provider()
            if not url:
                logger.info("yente URL unset; falling back to fixtures")
                entry = self.watch_list.get(target)
                hit = dict(entry) if entry else None
            else:
                raise NotImplementedError(
                    "live yente transport wired at P10 hardening "
                    "(env already validated by guardrails)")
        else:
            raise ValueError(f"unknown mode {self.mode}")

        return {
            "entity_id": entity_id,
            "screened_name": target,
            "mode": self.mode,
            "hit": hit,
            "result": "WATCH_HIT" if hit else "CLEAR",
            "sandbox_guaranteed": True,
        }
