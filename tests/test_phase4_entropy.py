from __future__ import annotations

import pytest

from entropy_thesis.simulation.phase3 import allocate_phase3_workers
from entropy_thesis.simulation.phase4 import (
    allocate_phase4_workers,
    build_entropy_candidates,
)
from entropy_thesis.entropy import normalized_shannon_entropy


def test_phase4_lambda_zero_matches_phase3_volume_proportional() -> None:
    workloads = (0.0, 90.0, 9.0, 1.0)
    phase3 = allocate_phase3_workers(
        "volume_proportional",
        total_workers=12,
        workloads=workloads,
        minimum_per_active_zone=1,
    )
    phase4 = allocate_phase4_workers(
        total_workers=12,
        workloads=workloads,
        entropy_weight=0.0,
        minimum_per_active_zone=1,
    )
    assert phase4 == phase3


def test_phase4_entropy_weight_flattens_active_zone_worker_distribution() -> None:
    low_lambda = allocate_phase4_workers(
        total_workers=100,
        workloads=(90.0, 9.0, 1.0),
        entropy_weight=0.0,
        minimum_per_active_zone=1,
    )
    high_lambda = allocate_phase4_workers(
        total_workers=100,
        workloads=(90.0, 9.0, 1.0),
        entropy_weight=4.0,
        minimum_per_active_zone=1,
    )
    assert normalized_shannon_entropy(high_lambda) > normalized_shannon_entropy(low_lambda)
    assert sum(high_lambda) == 100


def test_phase4_keeps_zero_workload_zone_at_zero() -> None:
    allocation = allocate_phase4_workers(
        total_workers=8,
        workloads=(20.0, 0.0, 10.0, 0.0),
        entropy_weight=2.0,
        minimum_per_active_zone=1,
    )
    assert allocation[1] == 0
    assert allocation[3] == 0
    assert allocation[0] >= 1
    assert allocation[2] >= 1
    assert sum(allocation) == 8


def test_candidate_builder_reuses_identical_integer_allocations() -> None:
    candidates = build_entropy_candidates(
        total_workers=4,
        workloads=(1.0, 1.0),
        entropy_weights=(0.0, 0.5, 1.0, 4.0),
        minimum_per_active_zone=1,
    )
    assert len({candidate.worker_counts for candidate in candidates}) == 1
    assert len({candidate.allocation_id for candidate in candidates}) == 1
    assert candidates[0].reused_allocation is False
    assert all(candidate.reused_allocation for candidate in candidates[1:])


def test_candidate_builder_sorts_and_deduplicates_lambda_values() -> None:
    candidates = build_entropy_candidates(
        total_workers=6,
        workloads=(5.0, 3.0, 2.0),
        entropy_weights=(2.0, 0.0, 1.0, 1.0),
    )
    assert [candidate.entropy_weight for candidate in candidates] == [0.0, 1.0, 2.0]


@pytest.mark.parametrize("weight", [-1.0, float("nan"), float("inf")])
def test_invalid_phase4_entropy_weight_is_rejected(weight: float) -> None:
    with pytest.raises(ValueError):
        allocate_phase4_workers(
            total_workers=8,
            workloads=(1.0, 2.0),
            entropy_weight=weight,
        )


def test_selection_minimizes_primary_kpi_and_prefers_smaller_lambda_on_tie() -> None:
    from types import SimpleNamespace

    from entropy_thesis.simulation.phase4 import (
        EntropyAllocationCandidate,
        Phase4CandidateResult,
        select_phase4_candidate,
    )

    def item(lam: float, value: float) -> Phase4CandidateResult:
        candidate = EntropyAllocationCandidate(lam, f"A{lam}", (2, 2), False)
        simulation = SimpleNamespace(summary=SimpleNamespace(mean_flow_time_seconds=value))
        return Phase4CandidateResult(candidate, simulation)  # type: ignore[arg-type]

    selected = select_phase4_candidate(
        (item(1.0, 10.0), item(0.5, 10.0), item(0.0, 12.0)),
        metric="mean_flow_time_seconds",
    )
    assert selected.candidate.entropy_weight == 0.5


def test_selection_maximizes_spatial_entropy() -> None:
    from types import SimpleNamespace

    from entropy_thesis.simulation.phase4 import (
        EntropyAllocationCandidate,
        Phase4CandidateResult,
        select_phase4_candidate,
    )

    low = Phase4CandidateResult(
        EntropyAllocationCandidate(0.0, "A001", (3, 1), False),
        SimpleNamespace(summary=SimpleNamespace(mean_spatial_entropy_normalized=0.4)),  # type: ignore[arg-type]
    )
    high = Phase4CandidateResult(
        EntropyAllocationCandidate(2.0, "A002", (2, 2), False),
        SimpleNamespace(summary=SimpleNamespace(mean_spatial_entropy_normalized=0.7)),  # type: ignore[arg-type]
    )
    selected = select_phase4_candidate(
        (low, high), metric="mean_spatial_entropy_normalized"
    )
    assert selected.candidate.entropy_weight == 2.0
