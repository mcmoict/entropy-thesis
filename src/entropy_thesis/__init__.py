"""Entropy-based warehouse worker-allocation research package."""

from .allocation import (
    allocate_workers,
    entropy_based_allocation,
    equal_allocation,
    random_allocation,
    volume_proportional_allocation,
)
from .entropy import normalized_shannon_entropy, shannon_entropy
from .metrics import SimulationMetrics, ZoneMetrics
from .simulation import (
    SimulationConfig,
    WarehouseSimulation,
    WarehouseSimulationResult,
    ZoneConfig,
    simulate_warehouse,
)

__all__ = [
    "SimulationConfig",
    "SimulationMetrics",
    "WarehouseSimulation",
    "WarehouseSimulationResult",
    "ZoneConfig",
    "ZoneMetrics",
    "allocate_workers",
    "entropy_based_allocation",
    "equal_allocation",
    "normalized_shannon_entropy",
    "random_allocation",
    "shannon_entropy",
    "simulate_warehouse",
    "volume_proportional_allocation",
]
