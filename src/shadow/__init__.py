"""
Shadow package — Shadow-Gradient Red Teaming (CORE adversarial mechanism).

Pipeline: distill → pgd → verify

  distill  query the victim ensemble's score function on probe transactions,
           fit BOTH an XGBoost surrogate (fidelity METRIC, reported) and a
           small torch MLP (differentiable GRADIENT CARRIER for PGD).
  pgd      projected-gradient ascent on evasion over ATTACKER-CONTROLLABLE
           feature domains only, with strict domain projection and
           recomputation of derived columns.
  verify   evaluate shadow-found candidates against the TRUE victim ensemble;
           margins are reported as ESTIMATES, never "certified".
"""

from .distill import ScoreOracleFn, collect_probes, distill_surrogates, DistillResult
from .pgd import ProjectedPGD, FeatureDomain, optimize_evasion
from .verify import Verifier, VerifyReport

__all__ = [
    "ScoreOracleFn", "collect_probes", "distill_surrogates", "DistillResult",
    "ProjectedPGD", "FeatureDomain", "optimize_evasion",
    "Verifier", "VerifyReport",
]
