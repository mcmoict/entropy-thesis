"""Unit tests for worker-allocation strategies."""

from __future__ import annotations

from collections.abc import Callable
import math

import numpy as np
import pytest
from numpy.typing import NDArray

from entropy_thesis.allocation import (
    allocate_workers,
    entropy_based_allocation,
    equal_allocation,
    normalize_strategy_name,
    random_allocation,
    volume_proportional_allocation,
)
from entropy_thesis.entropy import normalized_shannon_entropy


def test_equal_allocation_is_balanced_and_has_stable_remainder_order() -> None:
    allocation = equal_allocation(10, 3)
    np.testing.assert_array_equal(allocation, [4, 3, 3])


def test_volume_proportional_allocation_matches_exact_ratio() -> None:
    allocation = volume_proportional_allocation(10, [0.5, 0.3, 0.2])
    np.testing.assert_array_equal(allocation, [5, 3, 2])


def test_largest_remainder_ties_are_resolved_by_zone_order() -> None:
    allocation = volume_proportional_allocation(2, [1, 1, 1])
    np.testing.assert_array_equal(allocation, [1, 1, 0])


def test_zero_volumes_fall_back_to_equal_allocation() -> None:
    np.testing.assert_array_equal(
        volume_proportional_allocation(5, [0, 0]),
        [3, 2],
    )
    np.testing.assert_array_equal(
        entropy_based_allocation(5, [0, 0]),
        [3, 2],
    )


@pytest.mark.parametrize(
    "strategy",
    ["random", "equal", "volume_proportional", "entropy_based"],
)
def test_every_strategy_conserves_workers_and_respects_minimum(strategy: str) -> None:
    allocation = allocate_workers(
        strategy,
        17,
        [8, 3, 1, 0],
        seed=123,
        minimum_per_zone=2,
    )
    assert allocation.dtype == np.int64
    assert allocation.shape == (4,)
    assert int(allocation.sum()) == 17
    assert np.all(allocation >= 2)


def test_minimum_is_retained_for_a_zero_demand_zone() -> None:
    allocation = volume_proportional_allocation(
        10,
        [10, 0],
        minimum_per_zone=1,
    )
    np.testing.assert_array_equal(allocation, [9, 1])


def test_minimum_is_a_lower_bound_not_a_reserved_baseline() -> None:
    allocation = volume_proportional_allocation(
        10,
        [0.9, 0.1],
        minimum_per_zone=1,
    )
    np.testing.assert_array_equal(allocation, [9, 1])


def test_random_allocation_is_deterministic_for_integer_seed() -> None:
    first = random_allocation(100, 5, seed=2026, minimum_per_zone=1)
    second = random_allocation(100, 5, seed=2026, minimum_per_zone=1)
    np.testing.assert_array_equal(first, second)


def test_random_allocation_accepts_a_numpy_generator() -> None:
    first_rng = np.random.default_rng(77)
    second_rng = np.random.default_rng(77)
    np.testing.assert_array_equal(
        random_allocation(25, 3, seed=first_rng),
        random_allocation(25, 3, seed=second_rng),
    )


def test_entropy_weight_zero_is_volume_proportional() -> None:
    np.testing.assert_array_equal(
        entropy_based_allocation(101, [90, 9, 1], entropy_weight=0),
        volume_proportional_allocation(101, [90, 9, 1]),
    )


def test_entropy_regularization_flattens_a_skewed_allocation() -> None:
    proportional = volume_proportional_allocation(1000, [90, 9, 1])
    regularized = entropy_based_allocation(
        1000,
        [90, 9, 1],
        entropy_weight=3,
    )
    assert normalized_shannon_entropy(regularized) > normalized_shannon_entropy(
        proportional
    )
    assert int(regularized.sum()) == 1000


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("Random Allocation", "random"),
        ("equal-allocation", "equal"),
        ("volume", "volume_proportional"),
        ("Entropy", "entropy_based"),
    ],
)
def test_strategy_name_aliases(alias: str, canonical: str) -> None:
    assert normalize_strategy_name(alias) == canonical


def test_dispatcher_matches_direct_strategy_function() -> None:
    expected = entropy_based_allocation(
        20,
        [6, 3, 1],
        entropy_weight=2.5,
        minimum_per_zone=1,
    )
    actual = allocate_workers(
        "entropy_based",
        20,
        [6, 3, 1],
        entropy_weight=2.5,
        minimum_per_zone=1,
    )
    np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize("strategy", ["", "unknown", "round_robin"])
def test_unknown_or_empty_strategy_is_rejected(strategy: str) -> None:
    with pytest.raises(ValueError):
        allocate_workers(strategy, 5, [1, 1])


@pytest.mark.parametrize("total_workers", [-1, 1.5, True])
def test_invalid_total_workers_is_rejected(total_workers: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        equal_allocation(total_workers, 2)  # type: ignore[arg-type]


@pytest.mark.parametrize("number_of_zones", [0, -1, 2.5, True])
def test_invalid_zone_count_is_rejected(number_of_zones: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        random_allocation(5, number_of_zones)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "volumes",
    [[], [-1, 2], [math.nan, 1], [math.inf, 1], [[1], [2]]],
)
def test_invalid_volumes_are_rejected(volumes: list[object]) -> None:
    with pytest.raises(ValueError):
        volume_proportional_allocation(5, volumes)  # type: ignore[arg-type]


@pytest.mark.parametrize("minimum", [-1, 1.5, True])
def test_invalid_minimum_is_rejected(minimum: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        equal_allocation(10, 3, minimum_per_zone=minimum)  # type: ignore[arg-type]


def test_impossible_minimum_is_rejected_by_every_strategy() -> None:
    calls: list[Callable[[], NDArray[np.int64]]] = [
        lambda: random_allocation(2, 3, minimum_per_zone=1),
        lambda: equal_allocation(2, 3, minimum_per_zone=1),
        lambda: volume_proportional_allocation(2, [1, 1, 1], minimum_per_zone=1),
        lambda: entropy_based_allocation(2, [1, 1, 1], minimum_per_zone=1),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("weight", [-1, math.nan, math.inf])
def test_invalid_entropy_weight_is_rejected(weight: float) -> None:
    with pytest.raises(ValueError):
        entropy_based_allocation(10, [1, 1], entropy_weight=weight)


def test_non_numeric_entropy_weight_is_rejected() -> None:
    with pytest.raises(TypeError):
        entropy_based_allocation(10, [1, 1], entropy_weight="high")  # type: ignore[arg-type]
