"""
Financial Digital Twin — T1 of Project Prometheus.

A stateful, discrete-time-step simulation of a financial world with:
  - Entity state schemas (CustomerState, AccountState, MerchantState, etc.)
  - WorldState container with registration, transaction logging, trajectory tracking
  - Normal behaviour generator with realistic statistical patterns
  - 8 AMLSim typologies for fraud injection
  - FinancialDigitalTwin orchestrator
"""

from .core import (
    WorldState,
    CustomerState,
    AccountState,
    MerchantState,
    DeviceState,
    IPState,
    WalletState,
    RelationshipState,
    BehavioralHistory,
)
from .normal_behavior import NormalBehaviorGenerator, build_normal_profile, MERCHANT_CATEGORIES
from .typologies import (
    fan_in,
    fan_out,
    cycle,
    scatter_gather,
    gather_scatter,
    bipartite,
    stack,
    random_typology,
    run_typology,
    TYPOLOGY_FUNCTIONS,
)
from .twin import FinancialDigitalTwin

__all__ = [
    "WorldState",
    "CustomerState",
    "AccountState",
    "MerchantState",
    "DeviceState",
    "IPState",
    "WalletState",
    "RelationshipState",
    "BehavioralHistory",
    "NormalBehaviorGenerator",
    "build_normal_profile",
    "MERCHANT_CATEGORIES",
    "fan_in",
    "fan_out",
    "cycle",
    "scatter_gather",
    "gather_scatter",
    "bipartite",
    "stack",
    "random_typology",
    "run_typology",
    "TYPOLOGY_FUNCTIONS",
    "FinancialDigitalTwin",
]