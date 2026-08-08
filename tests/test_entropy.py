"""Unit tests for Shannon entropy calculations."""

from __future__ import annotations

import math

import numpy as np
import pytest

from entropy_thesis.entropy import (
    entropy_contributions,
    normalized_shannon_entropy,
    shannon_entropy,
)


def test_shannon_entropy_matches_known_distribution() -> None:
    assert shannon_entropy([0.5, 0.3, 0.2]) == pytest.approx(
        1.4854752972273344
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([1.0], 0.0),
        ([1.0, 0.0, 0.0], 0.0),
        ([1.0, 1.0], 1.0),
        ([1.0, 1.0, 1.0, 1.0], 2.0),
        ([0.0, 0.0, 0.0], 0.0),
    ],
)
def test_shannon_entropy_boundary_cases(
    values: list[float], expected: float
) -> None:
    assert shannon_entropy(values) == pytest.approx(expected)


def test_shannon_entropy_accepts_counts_and_generators() -> None:
    values = (value for value in [5, 3, 2])
    assert shannon_entropy(values) == pytest.approx(
        shannon_entropy([0.5, 0.3, 0.2])
    )


def test_entropy_is_invariant_to_positive_scaling() -> None:
    assert shannon_entropy([2, 3, 5]) == pytest.approx(
        shannon_entropy([20, 30, 50])
    )


def test_entropy_supports_other_logarithm_bases() -> None:
    assert shannon_entropy([1, 1, 1], base=math.e) == pytest.approx(math.log(3))
    assert shannon_entropy([1, 1, 1], base=10) == pytest.approx(math.log10(3))


def test_entropy_contributions_sum_to_total_entropy() -> None:
    contributions = entropy_contributions([5, 3, 2])
    assert contributions.shape == (3,)
    assert contributions[0] == pytest.approx(0.5)
    assert contributions.sum() == pytest.approx(shannon_entropy([5, 3, 2]))


@pytest.mark.parametrize(
    "values",
    [
        [],
        [-1, 2],
        [math.nan, 1],
        [math.inf, 1],
        [[1, 2], [3, 4]],
    ],
)
def test_entropy_rejects_invalid_values(values: list[object]) -> None:
    with pytest.raises(ValueError):
        shannon_entropy(values)  # type: ignore[arg-type]


@pytest.mark.parametrize("base", [-2.0, 0.0, 1.0, math.inf, math.nan])
def test_entropy_rejects_invalid_base(base: float) -> None:
    with pytest.raises(ValueError):
        shannon_entropy([1, 1], base=base)


def test_normalized_entropy_has_expected_extremes() -> None:
    assert normalized_shannon_entropy([10, 0, 0]) == pytest.approx(0.0)
    assert normalized_shannon_entropy([1, 1, 1]) == pytest.approx(1.0)
    assert normalized_shannon_entropy([1]) == pytest.approx(0.0)
    assert normalized_shannon_entropy([0, 0]) == pytest.approx(0.0)


def test_normalized_entropy_stays_in_unit_interval() -> None:
    value = normalized_shannon_entropy([8, 4, 2, 1])
    assert 0.0 <= value <= 1.0
    assert shannon_entropy([8, 4, 2, 1], normalized=True) == pytest.approx(value)


def test_normalized_entropy_is_permutation_invariant() -> None:
    assert normalized_shannon_entropy([7, 2, 1]) == pytest.approx(
        normalized_shannon_entropy([1, 7, 2])
    )


def test_normalized_entropy_does_not_mutate_numpy_input() -> None:
    values = np.array([4.0, 2.0, 1.0])
    original = values.copy()
    normalized_shannon_entropy(values)
    np.testing.assert_array_equal(values, original)
