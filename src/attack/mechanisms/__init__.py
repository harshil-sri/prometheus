"""attack.mechanisms — generation mechanisms beyond the rule compiler.

Each mechanism registers itself in blue.splits.mechanism registry and tags
every produced transaction row accordingly. shadow_pgd lands here (P4);
genetic/llm_strategist join in P5.
"""

from .shadow_pgd import ShadowPGDMechanism

__all__ = ["ShadowPGDMechanism"]
