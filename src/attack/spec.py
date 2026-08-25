"""
spec.py — Attack Specification Schema for the Attack Compiler.

Defines:
  - AttackSpec: a structured dataclass representing a single attack specification
  - WeaknessDescriptor: schema for describing system weaknesses (used by F4/F5)
  - build_attack_spec: factory function with validation
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# AttackSpec — Full attack specification
# ---------------------------------------------------------------------------

@dataclass
class AttackSpec:
    """A complete attack specification.

    Matches the PRD JSON contract:
    {
      "goal": "move_funds",
      "amount": 100000,
      "currency": "INR",
      "target": "compromised_cardholders",
      "constraints": {"max_fraud_score": 0.35, "max_behavioral_anomaly": 0.4},
      "resources": {"devices": 5, "accounts": 8, "days": 7},
      "desired_camouflage": "high"
    }
    """

    goal: str = "move_funds"
    amount: float = 100000.0
    currency: str = "INR"
    target: str = "compromised_cardholders"
    constraints: Dict[str, float] = field(default_factory=lambda: {
        "max_fraud_score": 0.35,
        "max_behavioral_anomaly": 0.4,
    })
    resources: Dict[str, int] = field(default_factory=lambda: {
        "devices": 5,
        "accounts": 8,
        "days": 7,
    })
    desired_camouflage: str = "high"

    # Internal fields (not part of the public spec JSON)
    attack_id: str = ""
    attack_type: str = ""
    typology: str = ""
    variant_params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AttackSpec":
        # Only pass known fields
        known = {
            "goal", "amount", "currency", "target",
            "constraints", "resources", "desired_camouflage",
            "attack_id", "attack_type", "typology", "variant_params",
        }
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)

    def __repr__(self) -> str:
        return (f"AttackSpec(attack_id={self.attack_id!r}, "
                f"attack_type={self.attack_type!r}, "
                f"goal={self.goal!r}, "
                f"amount={self.amount:.0f} {self.currency})")


# ---------------------------------------------------------------------------
# WeaknessDescriptor — Schema for system weaknesses
# ---------------------------------------------------------------------------

@dataclass
class WeaknessDescriptor:
    """Describes a system weakness that the attack compiler can target.

    Used by F4 (adversarial generator) and F5 (adaptive red teaming).
    """
    weakness: str = ""
    target_model: str = "GNN"
    goal: str = ""
    suggested_variants: List[str] = field(default_factory=lambda: [
        "more_devices", "more_intermediaries", "longer_paths", "temporal_spreading",
    ])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WeaknessDescriptor":
        known = {"weakness", "target_model", "goal", "suggested_variants"}
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def build_attack_spec(
    attack_id: str,
    attack_type: str,
    goal: str = "move_funds",
    amount: float = 100000.0,
    currency: str = "INR",
    target: str = "compromised_cardholders",
    max_fraud_score: float = 0.35,
    max_behavioral_anomaly: float = 0.4,
    resources_devices: int = 5,
    resources_accounts: int = 8,
    resources_days: int = 7,
    desired_camouflage: str = "high",
    typology: str = "",
    variant_params: Optional[Dict[str, Any]] = None,
) -> AttackSpec:
    """Build an AttackSpec with validation.

    Validates that amount is positive and resources make sense.
    """
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")
    if resources_devices < 0:
        raise ValueError(f"resources_devices must be >= 0, got {resources_devices}")
    if resources_accounts < 1:
        raise ValueError(f"resources_accounts must be >= 1, got {resources_accounts}")
    if resources_days < 1:
        raise ValueError(f"resources_days must be >= 1, got {resources_days}")

    return AttackSpec(
        goal=goal,
        amount=amount,
        currency=currency,
        target=target,
        constraints={
            "max_fraud_score": max_fraud_score,
            "max_behavioral_anomaly": max_behavioral_anomaly,
        },
        resources={
            "devices": resources_devices,
            "accounts": resources_accounts,
            "days": resources_days,
        },
        desired_camouflage=desired_camouflage,
        attack_id=attack_id,
        attack_type=attack_type,
        typology=typology,
        variant_params=variant_params or {},
    )