r"""Worker-allocation strategies for warehouse picking zones.

The entropy-based strategy uses entropy regularization to temper the spatial
concentration produced by a strictly volume-proportional allocation. Let
``d`` be normalized zone demand and ``p`` the continuous worker share. It
solves

.. math::

    \min_p D_{KL}(p \Vert d) - \lambda H(p)

on the probability simplex (restricted to positive-demand zones). Its closed
form is ``p_i proportional to d_i ** (1 / (1 + lambda))``. Thus ``lambda=0``
is volume proportional, while increasing ``lambda`` moves the allocation
toward the maximum-entropy uniform distribution. If a per-zone minimum is
configured, lower-bound constraints are imposed on the full continuous quota
with iterative water-filling. Feasible quotas are converted to worker counts
with largest-remainder apportionment. Fractional ties are resolved by the
original zone order, making results deterministic.
"""

from __future__ import annotations

from collections.abc import Iterable
import math
from typing import Literal, TypeAlias

import numpy as np
from numpy.random import Generator
from numpy.typing import NDArray


AllocationStrategy: TypeAlias = Literal[
    "random",
    "equal",
    "volume_proportional",
    "entropy_based",
]
RandomSeed: TypeAlias = int | Generator | None


def _validate_total_workers(total_workers: int) -> int:
    if isinstance(total_workers, bool) or not isinstance(total_workers, (int, np.integer)):
        raise TypeError("total_workers must be an integer")
    result = int(total_workers)
    if result < 0:
        raise ValueError("total_workers must be non-negative")
    return result


def _validate_zone_count(number_of_zones: int) -> int:
    if isinstance(number_of_zones, bool) or not isinstance(
        number_of_zones, (int, np.integer)
    ):
        raise TypeError("number_of_zones must be an integer")
    result = int(number_of_zones)
    if result <= 0:
        raise ValueError("number_of_zones must be positive")
    return result


def _validate_minimum(
    minimum_per_zone: int,
    *,
    total_workers: int,
    number_of_zones: int,
) -> int:
    if isinstance(minimum_per_zone, bool) or not isinstance(
        minimum_per_zone, (int, np.integer)
    ):
        raise TypeError("minimum_per_zone must be an integer")
    result = int(minimum_per_zone)
    if result < 0:
        raise ValueError("minimum_per_zone must be non-negative")
    if result * number_of_zones > total_workers:
        raise ValueError(
            "minimum_per_zone cannot be satisfied with the available workers"
        )
    return result


def _validate_volumes(volumes: Iterable[float]) -> NDArray[np.float64]:
    result = np.asarray(list(volumes), dtype=np.float64)
    if result.ndim != 1:
        raise ValueError("volumes must be a one-dimensional sequence")
    if result.size == 0:
        raise ValueError("volumes must contain at least one zone")
    if not np.all(np.isfinite(result)):
        raise ValueError("volumes must contain only finite values")
    if np.any(result < 0.0):
        raise ValueError("volumes must be non-negative")
    return result


def _largest_remainder(
    total_workers: int,
    weights: NDArray[np.float64],
    *,
    minimum_per_zone: int = 0,
) -> NDArray[np.int64]:
    """Apportion integer workers subject to a true per-zone lower bound.

    The unconstrained quota is calculated against the full worker total.
    Zones whose quota falls below ``minimum_per_zone`` are fixed at the lower
    bound, after which the remaining total is redistributed proportionally
    among the still-free zones. This water-filling step repeats until all
    continuous quotas are feasible. Largest remainder then supplies integer
    counts without treating the lower bound as an extra reserved allocation.
    """

    worker_total = _validate_total_workers(total_workers)
    zone_count = int(weights.size)
    minimum = _validate_minimum(
        minimum_per_zone,
        total_workers=worker_total,
        number_of_zones=zone_count,
    )
    weight_sum = float(weights.sum())
    normalized_weights = (
        np.full(zone_count, 1.0 / zone_count, dtype=np.float64)
        if weight_sum == 0.0
        else weights / weight_sum
    )
    quotas = np.zeros(zone_count, dtype=np.float64)
    free = np.ones(zone_count, dtype=np.bool_)
    remaining = worker_total

    while np.any(free):
        free_indices = np.flatnonzero(free)
        free_weights = normalized_weights[free]
        free_weight_sum = float(free_weights.sum())
        candidate = (
            np.full(free_indices.size, remaining / free_indices.size)
            if free_weight_sum == 0.0
            else remaining * free_weights / free_weight_sum
        )
        below_minimum = candidate < minimum
        if not np.any(below_minimum):
            quotas[free_indices] = candidate
            break

        fixed_indices = free_indices[below_minimum]
        quotas[fixed_indices] = minimum
        free[fixed_indices] = False
        remaining -= minimum * int(fixed_indices.size)

    floors = np.floor(quotas).astype(np.int64)
    unassigned = worker_total - int(floors.sum())
    if unassigned:
        fractional = quotas - floors
        # Stable sorting provides deterministic zone-order tie handling.
        order = np.argsort(-fractional, kind="stable")
        floors[order[:unassigned]] += 1
    return floors


def random_allocation(
    total_workers: int,
    number_of_zones: int,
    *,
    seed: RandomSeed = None,
    minimum_per_zone: int = 0,
) -> NDArray[np.int64]:
    """Assign workers independently to uniformly random zones.

    Passing an integer ``seed`` makes the allocation exactly reproducible.
    """

    worker_total = _validate_total_workers(total_workers)
    zone_count = _validate_zone_count(number_of_zones)
    minimum = _validate_minimum(
        minimum_per_zone,
        total_workers=worker_total,
        number_of_zones=zone_count,
    )
    allocation = np.full(zone_count, minimum, dtype=np.int64)
    remaining = worker_total - minimum * zone_count
    if remaining == 0:
        return allocation

    rng = seed if isinstance(seed, Generator) else np.random.default_rng(seed)
    allocation += rng.multinomial(
        remaining,
        np.full(zone_count, 1.0 / zone_count, dtype=np.float64),
    )
    return allocation


def equal_allocation(
    total_workers: int,
    number_of_zones: int,
    *,
    minimum_per_zone: int = 0,
) -> NDArray[np.int64]:
    """Distribute workers as equally as integer constraints allow."""

    zone_count = _validate_zone_count(number_of_zones)
    return _largest_remainder(
        total_workers,
        np.ones(zone_count, dtype=np.float64),
        minimum_per_zone=minimum_per_zone,
    )


def volume_proportional_allocation(
    total_workers: int,
    volumes: Iterable[float],
    *,
    minimum_per_zone: int = 0,
) -> NDArray[np.int64]:
    """Allocate workers in proportion to zone picking volume."""

    weights = _validate_volumes(volumes)
    return _largest_remainder(
        total_workers,
        weights,
        minimum_per_zone=minimum_per_zone,
    )


def entropy_based_allocation(
    total_workers: int,
    volumes: Iterable[float],
    *,
    entropy_weight: float = 1.0,
    minimum_per_zone: int = 0,
) -> NDArray[np.int64]:
    r"""Allocate workers using demand fit with Shannon-entropy regularization.

    ``entropy_weight`` is the non-negative :math:`\lambda` in the module
    formula. Zero reproduces volume-proportional allocation; larger values
    flatten positive-demand zone shares and reduce spatial concentration.
    The returned vector always contains non-negative integers summing exactly
    to ``total_workers`` and respects ``minimum_per_zone`` as a lower-bound
    constraint on the full quota, not as an allocation added before weighting.
    """

    weights = _validate_volumes(volumes)
    if not isinstance(entropy_weight, (int, float, np.integer, np.floating)):
        raise TypeError("entropy_weight must be a number")
    regularization = float(entropy_weight)
    if not math.isfinite(regularization) or regularization < 0.0:
        raise ValueError("entropy_weight must be finite and non-negative")

    if float(weights.sum()) == 0.0:
        regularized = np.ones_like(weights)
    else:
        demand = weights / float(weights.sum())
        exponent = 1.0 / (1.0 + regularization)
        regularized = np.zeros_like(demand)
        positive = demand > 0.0
        regularized[positive] = np.power(demand[positive], exponent)

    return _largest_remainder(
        total_workers,
        regularized,
        minimum_per_zone=minimum_per_zone,
    )


def normalize_strategy_name(strategy: str) -> AllocationStrategy:
    """Normalize common spelling variants to a canonical strategy name."""

    if not isinstance(strategy, str) or not strategy.strip():
        raise ValueError("strategy must be a non-empty string")
    normalized = strategy.strip().lower().replace("-", "_").replace(" ", "_")
    aliases: dict[str, AllocationStrategy] = {
        "random": "random",
        "random_allocation": "random",
        "equal": "equal",
        "equal_allocation": "equal",
        "volume": "volume_proportional",
        "volume_proportional": "volume_proportional",
        "volume_proportional_allocation": "volume_proportional",
        "entropy": "entropy_based",
        "entropy_based": "entropy_based",
        "entropy_based_allocation": "entropy_based",
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        choices = ", ".join(
            ("random", "equal", "volume_proportional", "entropy_based")
        )
        raise ValueError(f"unknown allocation strategy {strategy!r}; choose {choices}") from error


def allocate_workers(
    strategy: str,
    total_workers: int,
    volumes: Iterable[float],
    *,
    seed: RandomSeed = None,
    entropy_weight: float = 1.0,
    minimum_per_zone: int = 0,
) -> NDArray[np.int64]:
    """Dispatch to one of the four baseline allocation strategies."""

    volume_vector = _validate_volumes(volumes)
    method = normalize_strategy_name(strategy)
    if method == "random":
        return random_allocation(
            total_workers,
            int(volume_vector.size),
            seed=seed,
            minimum_per_zone=minimum_per_zone,
        )
    if method == "equal":
        return equal_allocation(
            total_workers,
            int(volume_vector.size),
            minimum_per_zone=minimum_per_zone,
        )
    if method == "volume_proportional":
        return volume_proportional_allocation(
            total_workers,
            volume_vector,
            minimum_per_zone=minimum_per_zone,
        )
    return entropy_based_allocation(
        total_workers,
        volume_vector,
        entropy_weight=entropy_weight,
        minimum_per_zone=minimum_per_zone,
    )


__all__ = [
    "AllocationStrategy",
    "allocate_workers",
    "entropy_based_allocation",
    "equal_allocation",
    "normalize_strategy_name",
    "random_allocation",
    "volume_proportional_allocation",
]
