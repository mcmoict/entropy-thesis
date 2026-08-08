"""Entropy measures used by the warehouse allocation experiments."""

from __future__ import annotations

from collections.abc import Iterable
import math

import numpy as np
from numpy.typing import NDArray


def _as_nonnegative_vector(values: Iterable[float]) -> NDArray[np.float64]:
    """Return *values* as a validated one-dimensional float vector."""

    vector = np.asarray(list(values), dtype=np.float64)
    if vector.ndim != 1:
        raise ValueError("values must be a one-dimensional sequence")
    if vector.size == 0:
        raise ValueError("values must contain at least one category")
    if not np.all(np.isfinite(vector)):
        raise ValueError("values must contain only finite numbers")
    if np.any(vector < 0.0):
        raise ValueError("values must be non-negative")
    return vector


def entropy_contributions(
    values: Iterable[float],
    *,
    base: float = 2.0,
) -> NDArray[np.float64]:
    """Return each category's contribution to Shannon entropy.

    ``values`` may be probabilities, counts, volumes, or any other
    non-negative weights. They are normalized internally. Zero-valued
    categories contribute zero, following the convention ``0 log(0) = 0``.
    """

    vector = _as_nonnegative_vector(values)
    if not math.isfinite(base) or base <= 0.0 or math.isclose(base, 1.0):
        raise ValueError("base must be finite, positive, and different from 1")

    total = float(vector.sum())
    if total == 0.0:
        return np.zeros_like(vector)

    probabilities = vector / total
    contributions = np.zeros_like(probabilities)
    positive = probabilities > 0.0
    contributions[positive] = -(
        probabilities[positive] * np.log(probabilities[positive]) / math.log(base)
    )
    return contributions


def shannon_entropy(
    values: Iterable[float],
    *,
    base: float = 2.0,
    normalized: bool = False,
) -> float:
    """Calculate Shannon entropy for non-negative category weights.

    Args:
        values: Probabilities or unnormalized non-negative weights.
        base: Logarithm base. The default returns entropy in bits.
        normalized: Divide by the maximum entropy for the number of supplied
            categories. The normalized result is in ``[0, 1]``. For a
            one-category or all-zero vector, it is defined as zero.
    """

    contributions = entropy_contributions(values, base=base)
    entropy = float(contributions.sum())
    if not normalized:
        return entropy

    category_count = int(contributions.size)
    if category_count <= 1:
        return 0.0
    maximum = math.log(category_count, base)
    return float(np.clip(entropy / maximum, 0.0, 1.0))


def normalized_shannon_entropy(values: Iterable[float]) -> float:
    """Return base-2 Shannon entropy normalized to the interval ``[0, 1]``."""

    return shannon_entropy(values, base=2.0, normalized=True)


__all__ = [
    "entropy_contributions",
    "normalized_shannon_entropy",
    "shannon_entropy",
]
