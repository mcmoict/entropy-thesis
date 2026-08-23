from __future__ import annotations

import pytest

from entropy_thesis.simulation.phase3 import allocate_phase3_workers
from entropy_thesis.simulation.phase4 import (
    allocate_phase4_workers,
    build_entropy_candidates,
    phase4_pareto_records,
    select_phase4_pareto_knee_from_daily,
)


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
        microzone_concentrations=(0.0, 0.2, 0.8, 0.5),
        minimum_per_active_zone=1,
    )
    assert phase4 == phase3


def test_phase4_entropy_weight_avoids_worker_pairs_in_concentrated_macrozone() -> None:
    low_lambda = allocate_phase4_workers(
        total_workers=100,
        workloads=(50.0, 50.0),
        entropy_weight=0.0,
        microzone_concentrations=(0.0, 1.0),
        minimum_per_active_zone=1,
    )
    high_lambda = allocate_phase4_workers(
        total_workers=100,
        workloads=(50.0, 50.0),
        entropy_weight=4.0,
        microzone_concentrations=(0.0, 1.0),
        minimum_per_active_zone=1,
    )
    assert low_lambda == (50, 50)
    assert high_lambda[1] < high_lambda[0]
    assert sum(high_lambda) == 100


def test_integer_objective_moves_one_worker_for_2023_01_05_profile() -> None:
    candidates = build_entropy_candidates(
        total_workers=8,
        workloads=(822.0, 417.0, 254.0, 503.0),
        microzone_concentrations=(0.073863, 0.175485, 0.098158, 0.411893),
        entropy_weights=(0.0, 0.25, 0.5, 1.0),
        minimum_per_active_zone=1,
    )

    by_lambda = {candidate.entropy_weight: candidate for candidate in candidates}
    assert by_lambda[0.0].worker_counts == (3, 2, 1, 2)
    assert by_lambda[0.25].worker_counts == (3, 2, 1, 2)
    assert by_lambda[0.5].worker_counts == (3, 2, 2, 1)
    assert by_lambda[0.5].moved_workers_from_volume == 1
    assert by_lambda[0.5].congestion_risk < by_lambda[0.0].congestion_risk
    assert by_lambda[0.5].demand_mismatch > by_lambda[0.0].demand_mismatch
    assert by_lambda[0.5].objective_value < (
        by_lambda[0.0].demand_mismatch + 0.5 * by_lambda[0.0].congestion_risk
    )


def test_phase4_keeps_zero_workload_zone_at_zero() -> None:
    allocation = allocate_phase4_workers(
        total_workers=8,
        workloads=(20.0, 0.0, 10.0, 0.0),
        entropy_weight=2.0,
        microzone_concentrations=(0.2, 0.0, 0.8, 0.0),
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


def test_phase4_default_lambda_grid_is_dense() -> None:
    from entropy_thesis.simulation.phase4 import DEFAULT_ENTROPY_WEIGHTS

    assert DEFAULT_ENTROPY_WEIGHTS == (0.0, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 4.0, 8.0)


def test_phase4_chronological_split_reserves_holdout_dates() -> None:
    from datetime import date

    from entropy_thesis.simulation.phase4 import split_phase4_dates

    values = tuple(date(2023, 1, day) for day in range(1, 11))
    calibration, holdout = split_phase4_dates(
        values,
        calibration_ratio=0.7,
        split_strategy="chronological",
    )
    assert calibration == values[:7]
    assert holdout == values[7:]
    assert set(calibration).isdisjoint(holdout)


def test_phase4_random_split_is_reproducible_and_date_disjoint() -> None:
    from datetime import date

    from entropy_thesis.simulation.phase4 import split_phase4_dates

    values = tuple(date(2023, 1, day) for day in range(1, 11))
    first = split_phase4_dates(values, calibration_ratio=0.7, split_strategy="random", seed=42)
    second = split_phase4_dates(values, calibration_ratio=0.7, split_strategy="random", seed=42)
    assert first == second
    calibration, holdout = first
    assert len(calibration) == 7
    assert len(holdout) == 3
    assert set(calibration).isdisjoint(holdout)


def test_phase4_multidate_selection_uses_equal_weight_date_mean() -> None:
    import pandas as pd

    from entropy_thesis.simulation.phase4 import select_phase4_entropy_weight_from_daily

    daily = pd.DataFrame(
        [
            {"selected_date": "2023-01-01", "entropy_weight": 0.0, "mean_flow_time_seconds": 100.0},
            {"selected_date": "2023-01-02", "entropy_weight": 0.0, "mean_flow_time_seconds": 100.0},
            {"selected_date": "2023-01-01", "entropy_weight": 0.5, "mean_flow_time_seconds": 80.0},
            {"selected_date": "2023-01-02", "entropy_weight": 0.5, "mean_flow_time_seconds": 90.0},
            {"selected_date": "2023-01-01", "entropy_weight": 1.0, "mean_flow_time_seconds": 70.0},
            {"selected_date": "2023-01-02", "entropy_weight": 1.0, "mean_flow_time_seconds": 110.0},
        ]
    )
    selected = select_phase4_entropy_weight_from_daily(daily)
    assert selected == 0.5


def test_phase4_paired_statistics_count_wins_against_lambda_zero() -> None:
    import pandas as pd

    from entropy_thesis.simulation.phase4 import paired_phase4_lambda_records

    rows = []
    for day, zero, candidate in [
        ("2023-01-01", 100.0, 90.0),
        ("2023-01-02", 100.0, 80.0),
        ("2023-01-03", 100.0, 100.0),
    ]:
        for weight, flow in [(0.0, zero), (0.5, candidate)]:
            rows.append(
                {
                    "selected_date": day,
                    "entropy_weight": weight,
                    "mean_flow_time_seconds": flow,
                }
            )
    result = paired_phase4_lambda_records(
        pd.DataFrame(rows),
        metrics=("mean_flow_time_seconds",),
    )
    item = next(record for record in result if record["entropy_weight"] == 0.5)
    assert item["wins"] == 2
    assert item["ties"] == 1
    assert item["losses"] == 0
    assert item["mean_improvement_pct"] == pytest.approx(10.0)


def test_phase4_pareto_knee_balances_flow_time_and_congestion() -> None:
    import pandas as pd

    daily = pd.DataFrame(
        [
            {
                "selected_date": "2023-01-01",
                "entropy_weight": 0.0,
                "mean_flow_time_seconds": 100.0,
                "congestion_conflicts": 100.0,
                "congestion_wait_seconds": 100.0,
                "congestion_delay_ratio": 0.10,
            },
            {
                "selected_date": "2023-01-01",
                "entropy_weight": 0.25,
                "mean_flow_time_seconds": 105.0,
                "congestion_conflicts": 70.0,
                "congestion_wait_seconds": 70.0,
                "congestion_delay_ratio": 0.07,
            },
            {
                "selected_date": "2023-01-01",
                "entropy_weight": 0.5,
                "mean_flow_time_seconds": 110.0,
                "congestion_conflicts": 80.0,
                "congestion_wait_seconds": 80.0,
                "congestion_delay_ratio": 0.08,
            },
            {
                "selected_date": "2023-01-01",
                "entropy_weight": 1.0,
                "mean_flow_time_seconds": 140.0,
                "congestion_conflicts": 50.0,
                "congestion_wait_seconds": 50.0,
                "congestion_delay_ratio": 0.05,
            },
        ]
    )

    records = phase4_pareto_records(daily)
    by_lambda = {record["entropy_weight"]: record for record in records}

    assert by_lambda[0.5]["pareto_frontier"] is False
    assert by_lambda[0.25]["pareto_frontier"] is True
    assert by_lambda[0.25]["flow_time_change_vs_lambda0_pct"] == pytest.approx(5.0)
    assert by_lambda[0.25]["conflicts_reduction_vs_lambda0_pct"] == pytest.approx(30.0)
    assert by_lambda[0.25]["composite_congestion_reduction_vs_lambda0_pct"] == pytest.approx(30.0)
    assert select_phase4_pareto_knee_from_daily(daily) == pytest.approx(0.25)
