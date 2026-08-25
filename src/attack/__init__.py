"""
Attack Compiler — T2 of Project Prometheus.

A rule-based attack planner that:
  - Defines attack specifications as structured dataclasses
  - Compiles specs into concrete, executable action sequences
  - Executes attacks against the Financial Digital Twin
  - Generates targeted variants from weakness descriptors
  - Provides 6 benchmark attack types (A1–A6, with A2/A5 held out for eval)
"""

from .spec import (
    AttackSpec,
    WeaknessDescriptor,
    build_attack_spec,
)
from .compiler import (
    AttackCompiler,
    AttackExecutionError,
)
from .benchmark_attacks import (
    BENCHMARK_ATTACKS,
    HELD_OUT_ATTACKS,
    TRAINABLE_ATTACKS,
    ATTACK_METADATA,
    generate_training_attacks,
)

__all__ = [
    "AttackSpec",
    "WeaknessDescriptor",
    "build_attack_spec",
    "AttackCompiler",
    "AttackExecutionError",
    "BENCHMARK_ATTACKS",
    "HELD_OUT_ATTACKS",
    "TRAINABLE_ATTACKS",
    "ATTACK_METADATA",
    "generate_training_attacks",
]