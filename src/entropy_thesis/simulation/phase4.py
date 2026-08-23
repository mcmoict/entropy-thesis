from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import date
import json
import math
import random
from pathlib import Path
from typing import Iterable, Literal

import pandas as pd
from scipy.stats import wilcoxon

from ..allocation import allocate_workers
from ..entropy import normalized_shannon_entropy
from .data_loader import DatasetBundle, PickingList, load_dataset
from .phase1 import Phase1Audit, audit_picking_locations
from .phase2 import DemandEntropyMetrics, calculate_demand_entropy, select_phase2_lists
from .phase3 import (
    AisleZone,
    THESIS_MODEL_REVISION,
    Phase3MethodResult,
    PickingListZoneAssignment,
    _congestion_records,
    _worker_records,
    build_aisle_zones,
    classify_picking_lists_by_zone,
    macro_zone_demand_profiles,
    run_phase3_method,
    zone_workload,
)
from .progress import ConsoleProgress, format_duration
from .warehouse import WarehouseGraph


SelectionMetric = Literal[
    "mean_flow_time_seconds",
    "makespan_seconds",
    "congestion_wait_seconds",
    "congestion_conflicts",
    "congestion_delay_ratio",
    "total_distance_m",
    "mean_release_delay_seconds",
    "mean_spatial_entropy_normalized",
]
SelectionRule = Literal["pareto_knee", "single_metric"]

DEFAULT_ENTROPY_WEIGHTS: tuple[float, ...] = (0.0, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 4.0, 8.0)
DEFAULT_SELECTION_METRIC: SelectionMetric = "mean_flow_time_seconds"
DEFAULT_SELECTION_RULE: SelectionRule = "pareto_knee"
MAXIMIZE_METRICS = {"mean_spatial_entropy_normalized"}
PARETO_EFFICIENCY_METRIC: SelectionMetric = "mean_flow_time_seconds"
PARETO_CONGESTION_METRICS: tuple[SelectionMetric, ...] = (
    "congestion_conflicts",
    "congestion_wait_seconds",
    "congestion_delay_ratio",
)
PHASE4_MODEL_REVISION = f"{THESIS_MODEL_REVISION}-integer-objective-v1-pareto-knee-v1"


@dataclass(frozen=True)
class EntropyAllocationCandidate:
    entropy_weight: float
    allocation_id: str
    worker_counts: tuple[int, ...]
    reused_allocation: bool
    demand_mismatch: float = float("nan")
    congestion_risk: float = float("nan")
    objective_value: float = float("nan")
    moved_workers_from_volume: int = 0


@dataclass(frozen=True)
class Phase4CandidateResult:
    candidate: EntropyAllocationCandidate
    simulation: Phase3MethodResult


@dataclass(frozen=True)
class Phase4Recommendation:
    selection_metric: str
    direction: str
    entropy_weight: float
    allocation_id: str
    worker_counts: tuple[int, ...]
    metric_value: float
    allocation_entropy_normalized: float
    demand_worker_l1_gap: float


def _validate_entropy_weights(values: Iterable[float]) -> tuple[float, ...]:
    result: list[float] = []
    seen: set[float] = set()
    for value in values:
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0.0:
            raise ValueError("entropy weight는 0 이상의 유한한 수여야 합니다.")
        if numeric not in seen:
            result.append(numeric)
            seen.add(numeric)
    if not result:
        raise ValueError("entropy weight 후보가 하나 이상 필요합니다.")
    return tuple(sorted(result))


def allocate_phase4_workers(
    *,
    total_workers: int,
    workloads: Iterable[float],
    entropy_weight: float,
    microzone_concentrations: Iterable[float] | None = None,
    minimum_per_active_zone: int = 1,
) -> tuple[int, ...]:
    """Return the best *integer* worker vector for the Phase-4 objective.

    Feasible integer allocations are evaluated directly rather than creating a
    continuous entropy-adjusted weight and rounding it afterwards.

    Let ``d_z = workload_z / sum(workload)`` and ``p_z = n_z / N``.  The
    demand-fit term is the total-variation distance

        D(n) = 0.5 * sum_z |p_z - d_z|.

    Let ``C_z = 1 - H_z`` be the within-macro-zone micro-demand concentration.
    The congestion-risk term counts worker pairs placed in the same macro-zone,
    weighted by ``C_z``:

        R(n) = sum_z C_z * choose(n_z, 2).

    Phase 4 minimizes

        J(n; lambda) = D(n) + lambda * R(n)

    subject to integer worker counts, the fixed total-worker constraint, zero
    workers in inactive zones, and the requested minimum in every active zone.
    Consequently lambda can change one worker's discrete assignment directly.
    At lambda=0 the existing Phase-3 Volume Proportional allocation is used as
    the exact control allocation.
    """

    if isinstance(total_workers, bool) or not isinstance(total_workers, int):
        raise TypeError("total_workers는 정수여야 합니다.")
    if total_workers <= 0:
        raise ValueError("total_workers는 1 이상이어야 합니다.")
    if (
        isinstance(minimum_per_active_zone, bool)
        or not isinstance(minimum_per_active_zone, int)
        or minimum_per_active_zone < 1
    ):
        raise ValueError("minimum_per_active_zone은 1 이상의 정수여야 합니다.")

    regularization = float(entropy_weight)
    if not math.isfinite(regularization) or regularization < 0.0:
        raise ValueError("entropy_weight는 0 이상의 유한한 수여야 합니다.")

    values = tuple(float(value) for value in workloads)
    if not values:
        raise ValueError("workloads가 비어 있습니다.")
    if any(not math.isfinite(value) or value < 0.0 for value in values):
        raise ValueError("workloads는 0 이상의 유한한 수여야 합니다.")

    if microzone_concentrations is None:
        concentrations = (0.0,) * len(values)
    else:
        concentrations = tuple(float(value) for value in microzone_concentrations)
        if len(concentrations) != len(values):
            raise ValueError("microzone_concentrations와 workloads 길이가 다릅니다.")
        if any(
            not math.isfinite(value) or value < 0.0 or value > 1.0
            for value in concentrations
        ):
            raise ValueError("microzone_concentrations는 0~1 범위의 유한한 값이어야 합니다.")

    active_indices = [index for index, value in enumerate(values) if value > 0.0]
    if not active_indices:
        raise ValueError("양의 workload를 가진 zone이 없습니다.")
    required = len(active_indices) * minimum_per_active_zone
    if total_workers < required:
        raise ValueError(
            "활성 zone 최소 작업자 수를 만족할 수 없습니다. "
            f"workers={total_workers}, active_zones={len(active_indices)}, "
            f"minimum={minimum_per_active_zone}"
        )

    options, volume_counts = _integer_allocation_options(
        total_workers=total_workers,
        workloads=values,
        microzone_concentrations=concentrations,
        minimum_per_active_zone=minimum_per_active_zone,
    )
    if math.isclose(regularization, 0.0, abs_tol=1e-15):
        return volume_counts

    best_counts, _, _ = min(
        options,
        key=lambda item: (
            item[1] + regularization * item[2],
            item[1],
            item[2],
            _moved_worker_count(item[0], volume_counts),
            item[0],
        ),
    )
    return best_counts


def _moved_worker_count(
    worker_counts: Iterable[int],
    reference_counts: Iterable[int],
) -> int:
    """Minimum number of workers that must change zones between two vectors."""

    left = tuple(int(value) for value in worker_counts)
    right = tuple(int(value) for value in reference_counts)
    if len(left) != len(right):
        raise ValueError("worker count 벡터 길이가 다릅니다.")
    if sum(left) != sum(right):
        raise ValueError("worker count 벡터의 총 작업자 수가 다릅니다.")
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) // 2


def score_phase4_allocation(
    *,
    worker_counts: Iterable[int],
    workloads: Iterable[float],
    microzone_concentrations: Iterable[float],
    entropy_weight: float,
) -> tuple[float, float, float]:
    """Return ``(D, R, J)`` for one integer worker allocation."""

    counts = tuple(int(value) for value in worker_counts)
    values = tuple(float(value) for value in workloads)
    concentrations = tuple(float(value) for value in microzone_concentrations)
    if not counts or len(counts) != len(values) or len(values) != len(concentrations):
        raise ValueError("worker_counts/workloads/concentrations 길이가 올바르지 않습니다.")
    if any(value < 0 for value in counts):
        raise ValueError("worker_counts는 음수가 될 수 없습니다.")
    if any(not math.isfinite(value) or value < 0.0 for value in values):
        raise ValueError("workloads는 0 이상의 유한한 수여야 합니다.")
    if any(
        not math.isfinite(value) or value < 0.0 or value > 1.0
        for value in concentrations
    ):
        raise ValueError("microzone_concentrations는 0~1 범위여야 합니다.")
    weight = float(entropy_weight)
    if not math.isfinite(weight) or weight < 0.0:
        raise ValueError("entropy_weight는 0 이상의 유한한 수여야 합니다.")

    total_workers = sum(counts)
    total_workload = sum(values)
    if total_workers <= 0 or total_workload <= 0.0:
        raise ValueError("양의 total workers/workload가 필요합니다.")

    demand_mismatch = 0.5 * sum(
        abs((count / total_workers) - (workload / total_workload))
        for count, workload in zip(counts, values, strict=True)
    )
    congestion_risk = sum(
        concentration * math.comb(count, 2)
        for count, concentration in zip(counts, concentrations, strict=True)
    )
    objective_value = demand_mismatch + weight * congestion_risk
    return demand_mismatch, congestion_risk, objective_value


def _feasible_integer_allocations(
    *,
    total_workers: int,
    workloads: tuple[float, ...],
    minimum_per_active_zone: int,
) -> tuple[tuple[int, ...], ...]:
    active = tuple(index for index, workload in enumerate(workloads) if workload > 0.0)
    minimums = [0] * len(workloads)
    for index in active:
        minimums[index] = minimum_per_active_zone
    remaining = total_workers - sum(minimums)
    if remaining < 0:
        return tuple()

    allocations: list[tuple[int, ...]] = []

    def visit(active_position: int, workers_left: int, counts: list[int]) -> None:
        if active_position == len(active) - 1:
            final = active[active_position]
            counts[final] = minimums[final] + workers_left
            allocations.append(tuple(counts))
            counts[final] = minimums[final]
            return
        zone_index = active[active_position]
        for extra in range(workers_left + 1):
            counts[zone_index] = minimums[zone_index] + extra
            visit(active_position + 1, workers_left - extra, counts)
        counts[zone_index] = minimums[zone_index]

    if not active:
        return tuple()
    visit(0, remaining, minimums.copy())
    return tuple(allocations)


def _integer_allocation_options(
    *,
    total_workers: int,
    workloads: tuple[float, ...],
    microzone_concentrations: tuple[float, ...],
    minimum_per_active_zone: int,
) -> tuple[tuple[tuple[tuple[int, ...], float, float], ...], tuple[int, ...]]:
    active_indices = [index for index, value in enumerate(workloads) if value > 0.0]
    active_workloads = [workloads[index] for index in active_indices]
    active_volume_counts = allocate_workers(
        "volume_proportional",
        total_workers,
        active_workloads,
        minimum_per_zone=minimum_per_active_zone,
    )
    volume_result = [0] * len(workloads)
    for index, count in zip(active_indices, active_volume_counts, strict=True):
        volume_result[index] = int(count)
    volume_counts = tuple(volume_result)

    feasible = _feasible_integer_allocations(
        total_workers=total_workers,
        workloads=workloads,
        minimum_per_active_zone=minimum_per_active_zone,
    )
    options: list[tuple[tuple[int, ...], float, float]] = []
    for counts in feasible:
        demand_mismatch, congestion_risk, _ = score_phase4_allocation(
            worker_counts=counts,
            workloads=workloads,
            microzone_concentrations=microzone_concentrations,
            entropy_weight=0.0,
        )
        options.append((counts, demand_mismatch, congestion_risk))
    return tuple(options), volume_counts

def build_entropy_candidates(
    *,
    total_workers: int,
    workloads: Iterable[float],
    entropy_weights: Iterable[float] = DEFAULT_ENTROPY_WEIGHTS,
    microzone_concentrations: Iterable[float] | None = None,
    minimum_per_active_zone: int = 1,
) -> tuple[EntropyAllocationCandidate, ...]:
    """Create lambda candidates and mark duplicate integer allocations.

    The same integer worker vector has exactly the same Phase 3/4 DES inputs,
    so only its first occurrence needs an expensive simulation run.
    """

    weights = _validate_entropy_weights(entropy_weights)
    workload_values = tuple(float(value) for value in workloads)
    if microzone_concentrations is None:
        concentrations = (0.0,) * len(workload_values)
    else:
        concentrations = tuple(float(value) for value in microzone_concentrations)
    options, volume_counts = _integer_allocation_options(
        total_workers=total_workers,
        workloads=workload_values,
        microzone_concentrations=concentrations,
        minimum_per_active_zone=minimum_per_active_zone,
    )

    allocation_ids: dict[tuple[int, ...], str] = {}
    candidates: list[EntropyAllocationCandidate] = []
    for entropy_weight in weights:
        if math.isclose(entropy_weight, 0.0, abs_tol=1e-15):
            counts = volume_counts
            demand_mismatch, congestion_risk, objective_value = score_phase4_allocation(
                worker_counts=counts,
                workloads=workload_values,
                microzone_concentrations=concentrations,
                entropy_weight=entropy_weight,
            )
        else:
            counts, demand_mismatch, congestion_risk = min(
                options,
                key=lambda item: (
                    item[1] + entropy_weight * item[2],
                    item[1],
                    item[2],
                    _moved_worker_count(item[0], volume_counts),
                    item[0],
                ),
            )
            objective_value = demand_mismatch + entropy_weight * congestion_risk
        if counts not in allocation_ids:
            allocation_ids[counts] = f"A{len(allocation_ids) + 1:03d}"
            reused = False
        else:
            reused = True
        candidates.append(
            EntropyAllocationCandidate(
                entropy_weight=entropy_weight,
                allocation_id=allocation_ids[counts],
                worker_counts=counts,
                reused_allocation=reused,
                demand_mismatch=demand_mismatch,
                congestion_risk=congestion_risk,
                objective_value=objective_value,
                moved_workers_from_volume=_moved_worker_count(counts, volume_counts),
            )
        )
    return tuple(candidates)


def _selection_value(result: Phase4CandidateResult, metric: SelectionMetric) -> float:
    return float(getattr(result.simulation.summary, metric))


def select_phase4_candidate(
    results: Iterable[Phase4CandidateResult],
    *,
    metric: SelectionMetric = DEFAULT_SELECTION_METRIC,
) -> Phase4CandidateResult:
    """Select the best lambda by one explicit KPI, not an arbitrary composite.

    Operational cost metrics are minimized. Spatial entropy is maximized.
    Exact ties prefer the smaller lambda, which is the more conservative
    regularization strength and avoids claiming a stronger entropy effect than
    the observed KPI requires.
    """

    candidates = tuple(results)
    if not candidates:
        raise ValueError("Phase 4 candidate result가 비어 있습니다.")
    if metric not in {
        "mean_flow_time_seconds",
        "makespan_seconds",
        "congestion_wait_seconds",
        "congestion_conflicts",
        "congestion_delay_ratio",
        "total_distance_m",
        "mean_release_delay_seconds",
        "mean_spatial_entropy_normalized",
    }:
        raise ValueError(f"지원하지 않는 selection metric입니다: {metric}")

    if metric in MAXIMIZE_METRICS:
        return min(
            candidates,
            key=lambda item: (-_selection_value(item, metric), item.candidate.entropy_weight),
        )
    return min(
        candidates,
        key=lambda item: (_selection_value(item, metric), item.candidate.entropy_weight),
    )


def _percent_change(value: float, baseline: float) -> float:
    if baseline == 0.0:
        return 0.0 if value == 0.0 else float("nan")
    return 100.0 * (value - baseline) / baseline


def _candidate_summary_records(
    results: tuple[Phase4CandidateResult, ...],
    selected: Phase4CandidateResult,
) -> list[dict[str, object]]:
    lambda_zero = next(
        (item for item in results if math.isclose(item.candidate.entropy_weight, 0.0)),
        None,
    )
    baseline = lambda_zero.simulation.summary if lambda_zero is not None else None
    records: list[dict[str, object]] = []
    for item in results:
        record: dict[str, object] = {
            "entropy_weight": item.candidate.entropy_weight,
            "allocation_id": item.candidate.allocation_id,
            "worker_counts": "|".join(str(v) for v in item.candidate.worker_counts),
            "reused_allocation": item.candidate.reused_allocation,
            "D_demand_mismatch": item.candidate.demand_mismatch,
            "R_congestion_risk": item.candidate.congestion_risk,
            "J_objective": item.candidate.objective_value,
            "moved_workers_from_volume": item.candidate.moved_workers_from_volume,
            "selected": item.candidate == selected.candidate,
        }
        summary = asdict(item.simulation.summary)
        summary["method"] = "entropy_based"
        record.update(summary)
        if baseline is not None:
            record.update(
                {
                    "mean_flow_time_change_vs_lambda0_pct": _percent_change(
                        item.simulation.summary.mean_flow_time_seconds,
                        baseline.mean_flow_time_seconds,
                    ),
                    "makespan_change_vs_lambda0_pct": _percent_change(
                        item.simulation.summary.makespan_seconds,
                        baseline.makespan_seconds,
                    ),
                    "congestion_wait_change_vs_lambda0_pct": _percent_change(
                        item.simulation.summary.congestion_wait_seconds,
                        baseline.congestion_wait_seconds,
                    ),
                    "distance_change_vs_lambda0_pct": _percent_change(
                        item.simulation.summary.total_distance_m,
                        baseline.total_distance_m,
                    ),
                }
            )
        records.append(record)
    return records


def _allocation_records(
    zones: tuple[AisleZone, ...],
    workloads: tuple[float, ...],
    candidates: tuple[EntropyAllocationCandidate, ...],
) -> list[dict[str, object]]:
    total_workload = sum(workloads)
    records: list[dict[str, object]] = []
    for candidate in candidates:
        total_workers = sum(candidate.worker_counts)
        for zone, workload, workers in zip(
            zones, workloads, candidate.worker_counts, strict=True
        ):
            records.append(
                {
                    "entropy_weight": candidate.entropy_weight,
                    "allocation_id": candidate.allocation_id,
                    "zone_id": zone.zone_id,
                    "aisle_count": len(zone.aisle_y_values),
                    "workload": workload,
                    "workload_share": 0.0 if total_workload == 0 else workload / total_workload,
                    "workers": workers,
                    "worker_share": 0.0 if total_workers == 0 else workers / total_workers,
                }
            )
    return records


def write_phase4_results(
    output_dir: str | Path,
    *,
    zones: tuple[AisleZone, ...],
    workloads: tuple[float, ...],
    results: tuple[Phase4CandidateResult, ...],
    selected: Phase4CandidateResult,
    origin: pd.Timestamp,
    metadata: dict[str, object],
) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(_candidate_summary_records(results, selected)).to_csv(
        output_dir / "phase4_summary.csv", index=False
    )
    pd.DataFrame(
        _allocation_records(zones, workloads, tuple(item.candidate for item in results))
    ).to_csv(output_dir / "phase4_allocations.csv", index=False)

    unique_by_allocation: dict[str, Phase4CandidateResult] = {}
    for item in results:
        unique_by_allocation.setdefault(item.candidate.allocation_id, item)
    unique_records: list[dict[str, object]] = []
    for allocation_id, item in unique_by_allocation.items():
        lambdas = [
            candidate.candidate.entropy_weight
            for candidate in results
            if candidate.candidate.allocation_id == allocation_id
        ]
        record: dict[str, object] = {
            "allocation_id": allocation_id,
            "representative_entropy_weight": min(lambdas),
            "entropy_weights": "|".join(f"{value:g}" for value in lambdas),
            "candidate_count": len(lambdas),
            "worker_counts": "|".join(str(v) for v in item.candidate.worker_counts),
        }
        record.update(asdict(item.simulation.summary))
        record["method"] = "entropy_based"
        unique_records.append(record)
    pd.DataFrame(unique_records).to_csv(
        output_dir / "phase4_unique_runs.csv", index=False
    )

    selected_result = selected.simulation
    worker_records = _worker_records(selected_result)
    for record in worker_records:
        record["method"] = "entropy_based"
        record["entropy_weight"] = selected.candidate.entropy_weight
        record["allocation_id"] = selected.candidate.allocation_id
    pd.DataFrame(worker_records).to_csv(
        output_dir / "phase4_selected_workers.csv", index=False
    )

    list_records = [asdict(event) for event in selected_result.executions]
    for record in list_records:
        record["method"] = "entropy_based"
        record["entropy_weight"] = selected.candidate.entropy_weight
        record["allocation_id"] = selected.candidate.allocation_id
    pd.DataFrame(list_records).to_csv(
        output_dir / "phase4_selected_lists.csv", index=False
    )

    congestion_records = _congestion_records(selected_result)
    for record in congestion_records:
        record["method"] = "entropy_based"
        record["entropy_weight"] = selected.candidate.entropy_weight
        record["allocation_id"] = selected.candidate.allocation_id
    pd.DataFrame(congestion_records).to_csv(
        output_dir / "phase4_selected_congestion.csv", index=False
    )

    entropy_records: list[dict[str, object]] = []
    for sample in selected_result.entropy_samples:
        record = asdict(sample)
        record["entropy_weight"] = selected.candidate.entropy_weight
        record["allocation_id"] = selected.candidate.allocation_id
        record["timestamp"] = origin + pd.to_timedelta(sample.time_seconds, unit="s")
        entropy_records.append(record)
    pd.DataFrame(entropy_records).to_csv(
        output_dir / "phase4_selected_entropy.csv", index=False
    )

    occupancy_records: list[dict[str, object]] = []
    for cell in selected_result.occupancy:
        record = asdict(cell)
        record["entropy_weight"] = selected.candidate.entropy_weight
        record["allocation_id"] = selected.candidate.allocation_id
        occupancy_records.append(record)
    pd.DataFrame(occupancy_records).to_csv(
        output_dir / "phase4_selected_occupancy.csv", index=False
    )

    recommendation = Phase4Recommendation(
        selection_metric=str(metadata["parameters"]["selection_metric"]),  # type: ignore[index]
        direction=(
            "maximize"
            if metadata["parameters"]["selection_metric"] in MAXIMIZE_METRICS  # type: ignore[index]
            else "minimize"
        ),
        entropy_weight=selected.candidate.entropy_weight,
        allocation_id=selected.candidate.allocation_id,
        worker_counts=selected.candidate.worker_counts,
        metric_value=_selection_value(
            selected,
            metadata["parameters"]["selection_metric"],  # type: ignore[index,arg-type]
        ),
        allocation_entropy_normalized=normalized_shannon_entropy(
            selected.candidate.worker_counts
        ),
        demand_worker_l1_gap=selected.simulation.summary.demand_worker_l1_gap,
    )
    with (output_dir / "phase4_recommendation.json").open("w", encoding="utf-8") as file:
        json.dump(asdict(recommendation), file, ensure_ascii=False, indent=2)
    with (output_dir / "phase4_metadata.json").open("w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2, default=str)


def build_and_run_phase4(
    data_dir: str | Path,
    *,
    target_date: date | None = None,
    max_lists: int | None = None,
    number_of_zones: int = 4,
    total_workers: int | None = None,
    volume_basis: Literal["tasks", "units"] = "tasks",
    minimum_per_active_zone: int = 1,
    entropy_weights: Iterable[float] = DEFAULT_ENTROPY_WEIGHTS,
    selection_metric: SelectionMetric = DEFAULT_SELECTION_METRIC,
    seed: int = 42,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    edge_capacity: int = 1,
    pick_node_capacity: int = 1,
    sample_seconds: float = 5.0,
    return_to_io: bool = True,
    progress: ConsoleProgress | None = None,
    print_objective_diagnostic: bool = False,
) -> tuple[
    DatasetBundle,
    WarehouseGraph,
    Phase1Audit,
    date,
    list[PickingList],
    tuple[AisleZone, ...],
    tuple[PickingListZoneAssignment, ...],
    DemandEntropyMetrics,
    tuple[Phase4CandidateResult, ...],
    Phase4CandidateResult,
    pd.Timestamp,
]:
    dataset = load_dataset(data_dir)
    if progress is not None:
        progress.report(0.10, "Building deterministic warehouse graph")
    warehouse = WarehouseGraph.build(
        dataset.storage_locations,
        dataset.support_points,
        deterministic_order=True,
    )
    audit = audit_picking_locations(warehouse, dataset.picking_lists)
    selected_date, selected_lists = select_phase2_lists(
        warehouse,
        dataset.picking_lists,
        target_date=target_date,
        max_lists=max_lists,
    )
    if progress is not None:
        progress.report(0.17, "Building 20 demand micro-zones / 4 workforce macro-zones")
    zones = build_aisle_zones(warehouse, number_of_zones=number_of_zones)
    assignments = classify_picking_lists_by_zone(warehouse, selected_lists, zones)
    workloads = zone_workload(zones, assignments, basis=volume_basis)
    macro_profiles = macro_zone_demand_profiles(
        warehouse, selected_lists, zones, basis=volume_basis
    )
    microzone_concentrations = tuple(
        profile.microzone_concentration for profile in macro_profiles
    )
    demand_entropy, _ = calculate_demand_entropy(warehouse, selected_lists)

    inferred_workers = len({p.operator for p in selected_lists})
    workers_to_use = inferred_workers if total_workers is None else total_workers
    if workers_to_use <= 0:
        raise ValueError("Phase 4 total_workers는 1 이상이어야 합니다.")

    candidates = build_entropy_candidates(
        total_workers=workers_to_use,
        workloads=workloads,
        entropy_weights=entropy_weights,
        microzone_concentrations=microzone_concentrations,
        minimum_per_active_zone=minimum_per_active_zone,
    )
    if print_objective_diagnostic:
        _print_integer_objective_diagnostic_pre_des(
            selected_date=selected_date,
            zones=zones,
            workloads=workloads,
            microzone_concentrations=microzone_concentrations,
            candidates=candidates,
        )
    unique_candidates: dict[str, EntropyAllocationCandidate] = {}
    for candidate in candidates:
        unique_candidates.setdefault(candidate.allocation_id, candidate)

    origin = min(p.created_at for p in selected_lists if p.created_at is not None)
    simulations: dict[str, Phase3MethodResult] = {}
    unique_list = list(unique_candidates.values())
    for run_index, candidate in enumerate(unique_list):
        base = 0.22 + 0.68 * (run_index / len(unique_list))
        span = 0.68 / len(unique_list)
        report_every = max(1, len(selected_lists) // 10)

        def simulation_progress(completed: int, total: int, execution, *, _base=base, _span=span, _candidate=candidate):
            if progress is None:
                return
            if completed != 1 and completed != total and completed % report_every != 0:
                return
            progress.report(
                _base + _span * (completed / total),
                f"Simulating entropy λ={_candidate.entropy_weight:g}: {completed:,}/{total:,} lists",
                current=f"allocation={_candidate.allocation_id}, zone={execution.assigned_zone}",
            )

        if progress is not None:
            progress.report(
                base,
                f"Starting entropy λ={candidate.entropy_weight:g}",
                current=(
                    f"{candidate.allocation_id}: "
                    + ", ".join(
                        f"{zone.zone_id}={count}"
                        for zone, count in zip(zones, candidate.worker_counts, strict=True)
                    )
                    + (
                        f" | D={candidate.demand_mismatch:.6f}, "
                        f"R={candidate.congestion_risk:.6f}, "
                        f"J={candidate.objective_value:.6f}, "
                        f"move={candidate.moved_workers_from_volume}"
                    )
                ),
            )
        simulations[candidate.allocation_id] = run_phase3_method(
            warehouse,
            selected_lists,
            zones,
            assignments,
            method=f"entropy_lambda_{candidate.entropy_weight:g}",
            worker_counts=candidate.worker_counts,
            selected_date=selected_date,
            demand_entropy=demand_entropy,
            seed=seed,
            walking_speed_mps=walking_speed_mps,
            pick_seconds_per_unit=pick_seconds_per_unit,
            edge_capacity=edge_capacity,
            pick_node_capacity=pick_node_capacity,
            sample_seconds=sample_seconds,
            return_to_io=return_to_io,
            volume_basis=volume_basis,
            progress_callback=simulation_progress,
        )

    results = tuple(
        Phase4CandidateResult(candidate, simulations[candidate.allocation_id])
        for candidate in candidates
    )
    selected = select_phase4_candidate(results, metric=selection_metric)
    return (
        dataset,
        warehouse,
        audit,
        selected_date,
        selected_lists,
        zones,
        assignments,
        demand_entropy,
        results,
        selected,
        origin,
    )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def _parse_entropy_weights(value: str) -> tuple[float, ...]:
    try:
        return _validate_entropy_weights(
            float(part.strip()) for part in value.split(",") if part.strip()
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _movement_description(
    candidate_counts: tuple[int, ...],
    volume_counts: tuple[int, ...],
    zones: tuple[AisleZone, ...],
) -> str:
    decreases: list[str] = []
    increases: list[str] = []
    for zone, current, base in zip(zones, candidate_counts, volume_counts, strict=True):
        delta = current - base
        if delta < 0:
            decreases.extend([zone.zone_id] * (-delta))
        elif delta > 0:
            increases.extend([zone.zone_id] * delta)
    if not decreases and not increases:
        return "none"
    if len(decreases) == len(increases):
        return ", ".join(f"{source}->{target}" for source, target in zip(decreases, increases, strict=True))
    return "allocation changed"


def _print_integer_objective_diagnostic_pre_des(
    *,
    selected_date: date,
    zones: tuple[AisleZone, ...],
    workloads: tuple[float, ...],
    microzone_concentrations: tuple[float, ...],
    candidates: tuple[EntropyAllocationCandidate, ...],
) -> None:
    """Print the complete integer-objective table before any DES execution."""

    print()
    print(f"=== Phase 4 Integer Objective | PRE-DES | {selected_date.isoformat()} ===")
    print("Objective             : J(n;λ) = D(n) + λ R(n)")
    print("D(n)                  : 0.5 × Σ |worker_share_z - workload_share_z|")
    print("R(n)                  : Σ C_z × C(n_z, 2),  C_z = 1 - H_z")
    print("Integer constraints    : Σ n_z=N; active n_z>=minimum; inactive n_z=0")
    print()
    print("Zone        Workload   Share      C_z(1-H)")
    total_workload = sum(workloads)
    for zone, workload, concentration in zip(
        zones, workloads, microzone_concentrations, strict=True
    ):
        share = 0.0 if total_workload <= 0.0 else workload / total_workload
        print(f"{zone.zone_id:<8} {workload:>10,.2f}   {share:>7.4f}      {concentration:>8.6f}")

    volume = next(
        (candidate for candidate in candidates if math.isclose(candidate.entropy_weight, 0.0, abs_tol=1e-15)),
        candidates[0],
    )
    print()
    print("=== ①~③ Integer Allocation Check (before DES) ===")
    print("Lambda   Allocation       D          R          J        Move   Worker move")
    first_move: EntropyAllocationCandidate | None = None
    for candidate in candidates:
        if candidate.moved_workers_from_volume > 0 and first_move is None:
            first_move = candidate
        allocation = "[" + ",".join(str(value) for value in candidate.worker_counts) + "]"
        movement = _movement_description(candidate.worker_counts, volume.worker_counts, zones)
        print(
            f"{candidate.entropy_weight:<8g} {allocation:<15} "
            f"{candidate.demand_mismatch:>9.6f} "
            f"{candidate.congestion_risk:>10.6f} "
            f"{candidate.objective_value:>10.6f} "
            f"{candidate.moved_workers_from_volume:>5d}   {movement}"
        )
    print()
    if first_move is None:
        print("Worker movement check : no λ candidate changes the Volume allocation")
    else:
        allocation = "[" + ",".join(str(value) for value in first_move.worker_counts) + "]"
        movement = _movement_description(first_move.worker_counts, volume.worker_counts, zones)
        print(
            "Worker movement check : "
            f"FIRST at λ={first_move.entropy_weight:g} -> {allocation}; "
            f"{first_move.moved_workers_from_volume} worker moved ({movement})"
        )
    print("DES status            : objective table confirmed; DES starts next")
    print()


def _print_single_date_diagnostic(
    *,
    selected_date: date,
    results: tuple[Phase4CandidateResult, ...],
    selected: Phase4CandidateResult,
    output_dir: Path,
) -> None:
    print()
    print(f"=== ④ DES KPI by Lambda | {selected_date.isoformat()} ===")
    print(
        "Lambda   Allocation       Distance(m)  Conflicts   Wait(s)  Cong(%)  "
        "Release(s)  Flow(s)  Makespan(s)  SpatialH(2+)"
    )
    for item in results:
        summary = item.simulation.summary
        allocation = "[" + ",".join(str(value) for value in item.candidate.worker_counts) + "]"
        marker = "*" if item.candidate == selected.candidate else " "
        print(
            f"{marker}{item.candidate.entropy_weight:<7g} {allocation:<15} "
            f"{summary.total_distance_m:>11,.2f} "
            f"{summary.congestion_conflicts:>10,d} "
            f"{summary.congestion_wait_seconds:>9,.2f} "
            f"{100.0 * summary.congestion_delay_ratio:>8.2f} "
            f"{summary.mean_release_delay_seconds:>10,.2f} "
            f"{summary.mean_flow_time_seconds:>8,.2f} "
            f"{summary.makespan_seconds:>11,.2f} "
            f"{summary.mean_spatial_entropy_multiworker:>13.4f}"
        )
    print()
    print(f"Selected by DES KPI   : λ={selected.candidate.entropy_weight:g}")
    print(f"Diagnostic results    : {output_dir}")


# ---------------------------------------------------------------------------
# Phase 4A~4E: multi-date calibration / holdout workflow
# ---------------------------------------------------------------------------

DEFAULT_MIN_LISTS_PER_DATE = 20
DEFAULT_CALIBRATION_RATIO = 0.70
DEFAULT_SPLIT_STRATEGY = "chronological"
PHASE4_COMPARISON_METRICS: tuple[SelectionMetric, ...] = (
    "mean_flow_time_seconds",
    "makespan_seconds",
    "congestion_wait_seconds",
    "congestion_conflicts",
    "congestion_delay_ratio",
    "total_distance_m",
    "mean_release_delay_seconds",
    "mean_spatial_entropy_normalized",
)

PHASE4_CONGESTION_SIGNIFICANCE_METRICS: tuple[SelectionMetric, ...] = (
    "congestion_conflicts",
    "congestion_wait_seconds",
    "congestion_delay_ratio",
)
PHASE4_CONGESTION_METRIC_LABELS: dict[str, str] = {
    "congestion_conflicts": "Conflicts",
    "congestion_wait_seconds": "Wait(s)",
    "congestion_delay_ratio": "Congestion(%)",
}


@dataclass(frozen=True)
class Phase4DateProfile:
    selected_date: date
    picking_lists: int
    pick_tasks: int
    picked_units: float
    observed_workers: int
    effective_workers: int
    active_zones: int
    eligible: bool
    reason: str


@dataclass(frozen=True)
class Phase4CalibrationDateResult:
    selected_date: date
    picking_lists: tuple[PickingList, ...]
    assignments: tuple[PickingListZoneAssignment, ...]
    workloads: tuple[float, ...]
    microzone_concentrations: tuple[float, ...]
    demand_entropy: DemandEntropyMetrics
    observed_workers: int
    effective_workers: int
    results: tuple[Phase4CandidateResult, ...]
    unique_simulation_count: int


@dataclass(frozen=True)
class Phase4MultiDateRun:
    dataset: DatasetBundle
    warehouse: WarehouseGraph
    audit: Phase1Audit
    zones: tuple[AisleZone, ...]
    profiles: tuple[Phase4DateProfile, ...]
    eligible_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    holdout_dates: tuple[date, ...]
    calibration_results: tuple[Phase4CalibrationDateResult, ...]
    entropy_weights: tuple[float, ...]
    selection_rule: SelectionRule
    selection_metric: SelectionMetric
    selected_entropy_weight: float
    split_strategy: str
    calibration_ratio: float


def _phase4_date_map(
    warehouse: WarehouseGraph,
    picking_lists: Iterable[PickingList],
) -> dict[date, list[PickingList]]:
    grouped: dict[date, list[PickingList]] = {}
    for picking_list in picking_lists:
        if picking_list.created_at is None or not picking_list.picks:
            continue
        if not all(warehouse.has_location(task.location_id) for task in picking_list.picks):
            continue
        grouped.setdefault(picking_list.created_at.date(), []).append(picking_list)
    for items in grouped.values():
        items.sort(key=lambda item: (item.created_at, item.wave_number, item.operator))
    return grouped


def extract_phase4_date_profiles(
    warehouse: WarehouseGraph,
    picking_lists: Iterable[PickingList],
    zones: tuple[AisleZone, ...],
    *,
    min_lists_per_date: int = DEFAULT_MIN_LISTS_PER_DATE,
    max_lists: int | None = None,
    total_workers: int | None = None,
    volume_basis: Literal["tasks", "units"] = "tasks",
    minimum_per_active_zone: int = 1,
) -> tuple[tuple[Phase4DateProfile, ...], dict[date, list[PickingList]]]:
    """Phase 4A: enumerate every fully-resolvable operating date and eligibility.

    Eligibility is evaluated on exactly the list subset that will enter DES.  This
    keeps `--max-lists` development runs internally consistent with the reported
    operator count and active-zone count.
    """

    if min_lists_per_date <= 0:
        raise ValueError("min_lists_per_date는 1 이상이어야 합니다.")
    if max_lists is not None and max_lists <= 0:
        raise ValueError("max_lists는 1 이상이어야 합니다.")

    grouped = _phase4_date_map(warehouse, picking_lists)
    profiles: list[Phase4DateProfile] = []
    selected_by_date: dict[date, list[PickingList]] = {}
    for selected_date in sorted(grouped):
        selected = list(grouped[selected_date])
        if max_lists is not None:
            selected = selected[:max_lists]
        selected_by_date[selected_date] = selected

        observed_workers = len({item.operator for item in selected})
        effective_workers = observed_workers if total_workers is None else total_workers
        assignments = classify_picking_lists_by_zone(warehouse, selected, zones)
        workloads = zone_workload(zones, assignments, basis=volume_basis)
        active_zones = sum(value > 0 for value in workloads)
        required_workers = active_zones * minimum_per_active_zone

        reason = "eligible"
        eligible = True
        if len(selected) < min_lists_per_date:
            eligible = False
            reason = "too_few_lists"
        elif effective_workers <= 0:
            eligible = False
            reason = "no_workers"
        elif active_zones <= 0:
            eligible = False
            reason = "no_active_zones"
        elif effective_workers < required_workers:
            eligible = False
            reason = "insufficient_workers_for_active_zones"

        profiles.append(
            Phase4DateProfile(
                selected_date=selected_date,
                picking_lists=len(selected),
                pick_tasks=sum(len(item.picks) for item in selected),
                picked_units=sum(
                    task.quantity_units for item in selected for task in item.picks
                ),
                observed_workers=observed_workers,
                effective_workers=effective_workers,
                active_zones=active_zones,
                eligible=eligible,
                reason=reason,
            )
        )
    return tuple(profiles), selected_by_date


def split_phase4_dates(
    eligible_dates: Iterable[date],
    *,
    calibration_ratio: float = DEFAULT_CALIBRATION_RATIO,
    split_strategy: Literal["chronological", "random"] = DEFAULT_SPLIT_STRATEGY,
    seed: int = 42,
) -> tuple[tuple[date, ...], tuple[date, ...]]:
    """Phase 4B: split *dates*, never picking lists, into calibration/holdout.

    `chronological` uses earlier dates for calibration and later dates for
    holdout. `random` performs a reproducible date-level split using `seed`.
    At least one date is reserved for each side.
    """

    values = tuple(sorted(dict.fromkeys(eligible_dates)))
    if len(values) < 2:
        raise ValueError("Calibration/Holdout 분리를 위해 적합 날짜가 최소 2개 필요합니다.")
    ratio = float(calibration_ratio)
    if not math.isfinite(ratio) or not (0.0 < ratio < 1.0):
        raise ValueError("calibration_ratio는 0과 1 사이여야 합니다.")
    calibration_count = max(1, min(len(values) - 1, int(math.floor(len(values) * ratio))))

    if split_strategy == "chronological":
        calibration = values[:calibration_count]
        holdout = values[calibration_count:]
    elif split_strategy == "random":
        shuffled = list(values)
        random.Random(seed).shuffle(shuffled)
        calibration_set = set(shuffled[:calibration_count])
        calibration = tuple(value for value in values if value in calibration_set)
        holdout = tuple(value for value in values if value not in calibration_set)
    else:
        raise ValueError(f"지원하지 않는 split_strategy입니다: {split_strategy}")
    return tuple(calibration), tuple(holdout)


def _run_phase4_calibration_date(
    warehouse: WarehouseGraph,
    zones: tuple[AisleZone, ...],
    selected_date: date,
    selected_lists: list[PickingList],
    *,
    total_workers: int | None,
    volume_basis: Literal["tasks", "units"],
    minimum_per_active_zone: int,
    entropy_weights: tuple[float, ...],
    seed: int,
    walking_speed_mps: float,
    pick_seconds_per_unit: float,
    edge_capacity: int,
    pick_node_capacity: int,
    sample_seconds: float,
    return_to_io: bool,
    progress: ConsoleProgress | None,
    date_index: int,
    date_count: int,
) -> Phase4CalibrationDateResult:
    assignments = classify_picking_lists_by_zone(warehouse, selected_lists, zones)
    workloads = zone_workload(zones, assignments, basis=volume_basis)
    macro_profiles = macro_zone_demand_profiles(
        warehouse, selected_lists, zones, basis=volume_basis
    )
    microzone_concentrations = tuple(
        profile.microzone_concentration for profile in macro_profiles
    )
    demand_entropy, _ = calculate_demand_entropy(warehouse, selected_lists)
    observed_workers = len({item.operator for item in selected_lists})
    effective_workers = observed_workers if total_workers is None else total_workers

    candidates = build_entropy_candidates(
        total_workers=effective_workers,
        workloads=workloads,
        entropy_weights=entropy_weights,
        microzone_concentrations=microzone_concentrations,
        minimum_per_active_zone=minimum_per_active_zone,
    )
    unique_candidates: dict[str, EntropyAllocationCandidate] = {}
    for candidate in candidates:
        unique_candidates.setdefault(candidate.allocation_id, candidate)
    unique_list = list(unique_candidates.values())

    simulations: dict[str, Phase3MethodResult] = {}
    date_base = 0.12 + 0.78 * (date_index / max(1, date_count))
    date_span = 0.78 / max(1, date_count)
    for run_index, candidate in enumerate(unique_list):
        run_base = date_base + date_span * (run_index / max(1, len(unique_list)))
        run_span = date_span / max(1, len(unique_list))
        report_every = max(1, len(selected_lists) // 10)

        def simulation_progress(
            completed: int,
            total: int,
            execution,
            *,
            _base=run_base,
            _span=run_span,
            _candidate=candidate,
        ) -> None:
            if progress is None:
                return
            if completed != 1 and completed != total and completed % report_every != 0:
                return
            progress.report(
                _base + _span * (completed / total),
                (
                    f"Calibration {selected_date.isoformat()} | "
                    f"λ={_candidate.entropy_weight:g}: {completed:,}/{total:,} lists"
                ),
                current=f"allocation={_candidate.allocation_id}, zone={execution.assigned_zone}",
            )

        if progress is not None:
            progress.report(
                run_base,
                f"Starting {selected_date.isoformat()} | λ={candidate.entropy_weight:g}",
                current=(
                    f"{candidate.allocation_id}: "
                    + ", ".join(
                        f"{zone.zone_id}={count}"
                        for zone, count in zip(zones, candidate.worker_counts, strict=True)
                    )
                    + (
                        f" | D={candidate.demand_mismatch:.6f}, "
                        f"R={candidate.congestion_risk:.6f}, "
                        f"J={candidate.objective_value:.6f}, "
                        f"move={candidate.moved_workers_from_volume}"
                    )
                ),
            )
        simulations[candidate.allocation_id] = run_phase3_method(
            warehouse,
            selected_lists,
            zones,
            assignments,
            method=f"entropy_lambda_{candidate.entropy_weight:g}",
            worker_counts=candidate.worker_counts,
            selected_date=selected_date,
            demand_entropy=demand_entropy,
            seed=seed,
            walking_speed_mps=walking_speed_mps,
            pick_seconds_per_unit=pick_seconds_per_unit,
            edge_capacity=edge_capacity,
            pick_node_capacity=pick_node_capacity,
            sample_seconds=sample_seconds,
            return_to_io=return_to_io,
            volume_basis=volume_basis,
            progress_callback=simulation_progress,
        )

    results = tuple(
        Phase4CandidateResult(candidate, simulations[candidate.allocation_id])
        for candidate in candidates
    )
    return Phase4CalibrationDateResult(
        selected_date=selected_date,
        picking_lists=tuple(selected_lists),
        assignments=assignments,
        workloads=workloads,
        microzone_concentrations=microzone_concentrations,
        demand_entropy=demand_entropy,
        observed_workers=observed_workers,
        effective_workers=effective_workers,
        results=results,
        unique_simulation_count=len(unique_list),
    )


def phase4_daily_records(run: Phase4MultiDateRun) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for date_result in run.calibration_results:
        for item in date_result.results:
            record: dict[str, object] = {
                "selected_date": date_result.selected_date.isoformat(),
                "entropy_weight": item.candidate.entropy_weight,
                "allocation_id": item.candidate.allocation_id,
                "worker_counts": "|".join(str(v) for v in item.candidate.worker_counts),
                "reused_allocation": item.candidate.reused_allocation,
                "D_demand_mismatch": item.candidate.demand_mismatch,
                "R_congestion_risk": item.candidate.congestion_risk,
                "J_objective": item.candidate.objective_value,
                "moved_workers_from_volume": item.candidate.moved_workers_from_volume,
                "observed_workers": date_result.observed_workers,
                "effective_workers": date_result.effective_workers,
            }
            summary = asdict(item.simulation.summary)
            summary.pop("method", None)
            summary.pop("selected_date", None)
            record.update(summary)
            records.append(record)
    return records


def phase4_allocation_records(run: Phase4MultiDateRun) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for date_result in run.calibration_results:
        total_workload = sum(date_result.workloads)
        for item in date_result.results:
            total_workers = sum(item.candidate.worker_counts)
            for zone, workload, concentration, workers in zip(
                run.zones,
                date_result.workloads,
                date_result.microzone_concentrations,
                item.candidate.worker_counts,
                strict=True,
            ):
                pair_risk_contribution = concentration * math.comb(workers, 2)
                records.append(
                    {
                        "selected_date": date_result.selected_date.isoformat(),
                        "entropy_weight": item.candidate.entropy_weight,
                        "allocation_id": item.candidate.allocation_id,
                        "zone_id": zone.zone_id,
                        "side": zone.side,
                        "support_labels": "|".join(zone.support_labels),
                        "workload": workload,
                        "workload_share": 0.0 if total_workload == 0 else workload / total_workload,
                        "microzone_concentration": concentration,
                        "microzone_entropy_normalized": 1.0 - concentration,
                        "pair_congestion_risk_contribution": pair_risk_contribution,
                        "workers": workers,
                        "worker_share": 0.0 if total_workers == 0 else workers / total_workers,
                        "D_demand_mismatch": item.candidate.demand_mismatch,
                        "R_congestion_risk": item.candidate.congestion_risk,
                        "J_objective": item.candidate.objective_value,
                        "moved_workers_from_volume": item.candidate.moved_workers_from_volume,
                    }
                )
    return records


def aggregate_phase4_lambda_records(
    daily: pd.DataFrame,
    *,
    metrics: Iterable[SelectionMetric] = PHASE4_COMPARISON_METRICS,
) -> list[dict[str, object]]:
    """Phase 4D descriptive statistics; every operating date has equal weight."""

    records: list[dict[str, object]] = []
    for entropy_weight, frame in daily.groupby("entropy_weight", sort=True):
        for metric in metrics:
            values = frame[str(metric)].astype(float)
            records.append(
                {
                    "entropy_weight": float(entropy_weight),
                    "metric": str(metric),
                    "direction": "maximize" if metric in MAXIMIZE_METRICS else "minimize",
                    "n_dates": int(values.size),
                    "mean": float(values.mean()),
                    "std": float(values.std(ddof=1)) if values.size > 1 else 0.0,
                    "median": float(values.median()),
                    "min": float(values.min()),
                    "max": float(values.max()),
                }
            )
    return records


def _paired_wilcoxon_pvalue(differences: pd.Series) -> float:
    values = differences.astype(float)
    if values.empty or bool((values.abs() <= 1e-12).all()):
        return 1.0
    try:
        return float(wilcoxon(values, zero_method="wilcox", alternative="two-sided").pvalue)
    except ValueError:
        return 1.0


def paired_phase4_lambda_records(
    daily: pd.DataFrame,
    *,
    reference_weight: float = 0.0,
    metrics: Iterable[SelectionMetric] = PHASE4_COMPARISON_METRICS,
) -> list[dict[str, object]]:
    """Phase 4D paired date-level comparisons against a reference lambda.

    The default reference is λ=0, which is exactly Volume Proportional before
    integer-allocation duplicate reuse.  Wilcoxon uses paired daily KPI values.
    """

    if not math.isclose(float(reference_weight), 0.0) and reference_weight not in set(
        daily["entropy_weight"].astype(float)
    ):
        raise ValueError("reference entropy weight가 daily 결과에 없습니다.")

    weights = tuple(sorted(float(v) for v in daily["entropy_weight"].unique()))
    if not any(math.isclose(value, reference_weight) for value in weights):
        raise ValueError("reference entropy weight가 daily 결과에 없습니다.")

    records: list[dict[str, object]] = []
    for metric in metrics:
        pivot = daily.pivot(index="selected_date", columns="entropy_weight", values=str(metric))
        reference_column = min(pivot.columns, key=lambda value: abs(float(value) - reference_weight))
        reference = pivot[reference_column].astype(float)
        for weight in weights:
            candidate_column = min(pivot.columns, key=lambda value: abs(float(value) - weight))
            candidate = pivot[candidate_column].astype(float)
            paired = pd.concat([reference.rename("reference"), candidate.rename("candidate")], axis=1).dropna()
            ref_values = paired["reference"]
            cand_values = paired["candidate"]
            differences = cand_values - ref_values
            maximize = metric in MAXIMIZE_METRICS
            tolerance = 1e-12
            if maximize:
                wins = int((differences > tolerance).sum())
                losses = int((differences < -tolerance).sum())
                improvement = (cand_values - ref_values) / ref_values.abs().replace(0.0, float("nan")) * 100.0
            else:
                wins = int((differences < -tolerance).sum())
                losses = int((differences > tolerance).sum())
                improvement = (ref_values - cand_values) / ref_values.abs().replace(0.0, float("nan")) * 100.0
            ties = int(len(paired) - wins - losses)
            records.append(
                {
                    "reference_entropy_weight": float(reference_weight),
                    "entropy_weight": weight,
                    "metric": str(metric),
                    "direction": "maximize" if maximize else "minimize",
                    "n_dates": int(len(paired)),
                    "reference_mean": float(ref_values.mean()) if len(paired) else float("nan"),
                    "candidate_mean": float(cand_values.mean()) if len(paired) else float("nan"),
                    "mean_difference_candidate_minus_reference": float(differences.mean()) if len(paired) else float("nan"),
                    "mean_improvement_pct": float(improvement.mean(skipna=True)) if len(paired) else float("nan"),
                    "wins": wins,
                    "ties": ties,
                    "losses": losses,
                    "wilcoxon_p_value": _paired_wilcoxon_pvalue(differences),
                }
            )
    return records


def selected_phase4_congestion_kpi_records(
    paired: pd.DataFrame,
    *,
    selected_weight: float,
) -> list[dict[str, object]]:
    """Return thesis-facing congestion significance rows for the selected λ*.

    Paired Wilcoxon/W-T-L values come directly from the date-level comparison
    against λ=0.  ``improve_pct`` is intentionally calculated from the two
    calibration-date means so it matches the reduction percentages shown in
    the Efficiency-Congestion Trade-off table.  Congestion delay ratio is
    exposed as percentage points only for display; the statistical test still
    uses the original paired ratio values.
    """

    if paired.empty:
        raise ValueError("Phase 4 paired result가 비어 있습니다.")

    records: list[dict[str, object]] = []
    for metric in PHASE4_CONGESTION_SIGNIFICANCE_METRICS:
        rows = paired[
            (paired["metric"] == str(metric))
            & paired["entropy_weight"].astype(float).map(
                lambda value: math.isclose(
                    value, float(selected_weight), rel_tol=1e-12, abs_tol=1e-12
                )
            )
        ]
        if rows.empty:
            raise ValueError(
                f"선택된 λ={selected_weight:g}의 paired congestion KPI가 없습니다: {metric}"
            )
        row = rows.iloc[0]
        reference_mean = float(row["reference_mean"])
        candidate_mean = float(row["candidate_mean"])
        improve_pct = -_percent_change(candidate_mean, reference_mean)
        display_scale = 100.0 if metric == "congestion_delay_ratio" else 1.0
        records.append(
            {
                "entropy_weight": float(selected_weight),
                "metric": str(metric),
                "metric_label": PHASE4_CONGESTION_METRIC_LABELS[str(metric)],
                "n_dates": int(row["n_dates"]),
                "reference_mean": reference_mean,
                "candidate_mean": candidate_mean,
                "reference_mean_display": reference_mean * display_scale,
                "candidate_mean_display": candidate_mean * display_scale,
                "improve_pct": float(improve_pct),
                "wins": int(row["wins"]),
                "ties": int(row["ties"]),
                "losses": int(row["losses"]),
                "wilcoxon_p_value": float(row["wilcoxon_p_value"]),
                "significant_at_0_05": bool(float(row["wilcoxon_p_value"]) < 0.05),
            }
        )
    return records


def select_phase4_entropy_weight_from_daily(
    daily: pd.DataFrame,
    *,
    metric: SelectionMetric = DEFAULT_SELECTION_METRIC,
) -> float:
    """Phase 4E: select λ* by the equal-weight mean across calibration dates."""

    if daily.empty:
        raise ValueError("Phase 4 daily result가 비어 있습니다.")
    grouped = daily.groupby("entropy_weight", sort=True)[str(metric)].mean()
    if metric in MAXIMIZE_METRICS:
        best_value = float(grouped.max())
        candidates = [float(weight) for weight, value in grouped.items() if math.isclose(float(value), best_value, rel_tol=1e-12, abs_tol=1e-12)]
    else:
        best_value = float(grouped.min())
        candidates = [float(weight) for weight, value in grouped.items() if math.isclose(float(value), best_value, rel_tol=1e-12, abs_tol=1e-12)]
    return min(candidates)


def _lambda0_relative_ratio(
    value: float,
    reference: float,
    *,
    fallback_scale: float,
) -> float:
    """Return a non-negative KPI ratio with λ=0 anchored at 1.0.

    Congestion KPIs are normally positive, so ``value / reference`` is the
    natural normalization.  The fallback only matters for degenerate runs in
    which λ=0 has exactly zero congestion; it keeps the Pareto diagnostic
    finite without inventing an epsilon-dependent percentage.
    """

    if abs(reference) > 1e-12:
        return value / reference
    if abs(value) <= 1e-12:
        return 1.0
    scale = fallback_scale if fallback_scale > 1e-12 else abs(value)
    return 1.0 + value / scale


def phase4_pareto_records(daily: pd.DataFrame) -> list[dict[str, object]]:
    """Build the Phase-4 efficiency/congestion Pareto diagnostic.

    The Pareto test itself uses the four *raw* calibration-date means and
    minimizes all of them:

    - mean flow time (processing efficiency),
    - congestion conflicts,
    - congestion wait seconds,
    - congestion delay ratio.

    For knee detection only, the three congestion KPIs are converted to a
    transparent equal-weight index after normalizing each KPI by its λ=0 mean.
    Thus ``congestion_index_lambda0 == 1`` at λ=0 and values below 1 represent
    aggregate congestion reduction.  The knee is the Pareto point with the
    largest perpendicular departure *toward the ideal point* from the chord
    connecting the best-flow and best-congestion endpoints after min-max
    normalization.  This avoids selecting λ from Flow Time alone while still
    exposing every raw congestion KPI in the output.
    """

    if daily.empty:
        raise ValueError("Phase 4 daily result가 비어 있습니다.")
    required = (
        "entropy_weight",
        str(PARETO_EFFICIENCY_METRIC),
        *(str(metric) for metric in PARETO_CONGESTION_METRICS),
    )
    missing = [column for column in required if column not in daily.columns]
    if missing:
        raise ValueError("Pareto 분석에 필요한 KPI가 없습니다: " + ", ".join(missing))

    mean_columns = [
        str(PARETO_EFFICIENCY_METRIC),
        *(str(metric) for metric in PARETO_CONGESTION_METRICS),
    ]
    means = (
        daily.groupby("entropy_weight", sort=True)[mean_columns]
        .mean()
        .reset_index()
    )
    zero_rows = means[
        means["entropy_weight"].astype(float).map(
            lambda value: math.isclose(value, 0.0, abs_tol=1e-12)
        )
    ]
    if zero_rows.empty:
        raise ValueError("Pareto 분석 기준을 위해 entropy_weight에 λ=0이 필요합니다.")
    reference = zero_rows.iloc[0]

    congestion_scales = {
        str(metric): max(abs(float(value)) for value in means[str(metric)])
        for metric in PARETO_CONGESTION_METRICS
    }

    rows: list[dict[str, object]] = []
    for _, item in means.iterrows():
        weight = float(item["entropy_weight"])
        flow = float(item[str(PARETO_EFFICIENCY_METRIC)])
        conflicts = float(item["congestion_conflicts"])
        wait_seconds = float(item["congestion_wait_seconds"])
        congestion_ratio = float(item["congestion_delay_ratio"])
        relative_ratios = [
            _lambda0_relative_ratio(
                float(item[str(metric)]),
                float(reference[str(metric)]),
                fallback_scale=congestion_scales[str(metric)],
            )
            for metric in PARETO_CONGESTION_METRICS
        ]
        congestion_index = sum(relative_ratios) / len(relative_ratios)
        rows.append(
            {
                "entropy_weight": weight,
                "mean_flow_time_seconds": flow,
                "flow_time_change_vs_lambda0_pct": _percent_change(
                    flow, float(reference[str(PARETO_EFFICIENCY_METRIC)])
                ),
                "congestion_conflicts": conflicts,
                "conflicts_reduction_vs_lambda0_pct": -_percent_change(
                    conflicts, float(reference["congestion_conflicts"])
                ),
                "congestion_wait_seconds": wait_seconds,
                "wait_reduction_vs_lambda0_pct": -_percent_change(
                    wait_seconds, float(reference["congestion_wait_seconds"])
                ),
                "congestion_delay_ratio": congestion_ratio,
                "congestion_percent": 100.0 * congestion_ratio,
                "congestion_reduction_vs_lambda0_pct": -_percent_change(
                    congestion_ratio, float(reference["congestion_delay_ratio"])
                ),
                "congestion_index_lambda0": congestion_index,
                "composite_congestion_reduction_vs_lambda0_pct": 100.0
                * (1.0 - congestion_index),
            }
        )

    objective_columns = (
        "mean_flow_time_seconds",
        "congestion_conflicts",
        "congestion_wait_seconds",
        "congestion_delay_ratio",
    )
    tolerance = 1e-12
    for index, row in enumerate(rows):
        dominated = False
        for other_index, other in enumerate(rows):
            if index == other_index:
                continue
            no_worse = all(
                float(other[column]) <= float(row[column]) + tolerance
                for column in objective_columns
            )
            strictly_better = any(
                float(other[column]) < float(row[column]) - tolerance
                for column in objective_columns
            )
            if no_worse and strictly_better:
                dominated = True
                break
        row["pareto_frontier"] = not dominated
        row["flow_normalized"] = float("nan")
        row["congestion_index_normalized"] = float("nan")
        row["knee_frontier"] = False
        row["knee_score"] = float("nan")
        row["selected_knee"] = False

    four_objective_frontier = [row for row in rows if bool(row["pareto_frontier"])]
    if not four_objective_frontier:
        raise ValueError("Pareto frontier를 계산할 수 없습니다.")

    # Knee geometry is two-dimensional (Flow Time vs aggregate congestion).
    # Remove points that are 4-D non-dominated only because one congestion KPI
    # moves differently, but are dominated after the three congestion KPIs are
    # summarized into the documented congestion index.
    frontier: list[dict[str, object]] = []
    for row in four_objective_frontier:
        dominated_2d = False
        for other in four_objective_frontier:
            if row is other:
                continue
            no_worse = (
                float(other["mean_flow_time_seconds"])
                <= float(row["mean_flow_time_seconds"]) + tolerance
                and float(other["congestion_index_lambda0"])
                <= float(row["congestion_index_lambda0"]) + tolerance
            )
            strictly_better = (
                float(other["mean_flow_time_seconds"])
                < float(row["mean_flow_time_seconds"]) - tolerance
                or float(other["congestion_index_lambda0"])
                < float(row["congestion_index_lambda0"]) - tolerance
            )
            if no_worse and strictly_better:
                dominated_2d = True
                break
        if not dominated_2d:
            row["knee_frontier"] = True
            frontier.append(row)

    flow_values = [float(row["mean_flow_time_seconds"]) for row in frontier]
    congestion_values = [float(row["congestion_index_lambda0"]) for row in frontier]
    min_flow, max_flow = min(flow_values), max(flow_values)
    min_congestion, max_congestion = min(congestion_values), max(congestion_values)
    flow_span = max_flow - min_flow
    congestion_span = max_congestion - min_congestion

    for row in frontier:
        row["flow_normalized"] = (
            0.0
            if flow_span <= tolerance
            else (float(row["mean_flow_time_seconds"]) - min_flow) / flow_span
        )
        row["congestion_index_normalized"] = (
            0.0
            if congestion_span <= tolerance
            else (float(row["congestion_index_lambda0"]) - min_congestion)
            / congestion_span
        )

    best_flow = min(
        frontier,
        key=lambda row: (
            float(row["mean_flow_time_seconds"]),
            float(row["entropy_weight"]),
        ),
    )
    best_congestion = min(
        frontier,
        key=lambda row: (
            float(row["congestion_index_lambda0"]),
            float(row["entropy_weight"]),
        ),
    )

    ax = float(best_flow["flow_normalized"])
    ay = float(best_flow["congestion_index_normalized"])
    bx = float(best_congestion["flow_normalized"])
    by = float(best_congestion["congestion_index_normalized"])
    vx, vy = bx - ax, by - ay
    chord_length = math.hypot(vx, vy)

    if best_flow is best_congestion or chord_length <= tolerance:
        selected = best_flow
        selected["knee_score"] = 0.0
    else:
        ideal_cross = vx * (0.0 - ay) - vy * (0.0 - ax)
        orientation = -1.0 if ideal_cross < 0.0 else 1.0
        for row in frontier:
            px = float(row["flow_normalized"])
            py = float(row["congestion_index_normalized"])
            cross = vx * (py - ay) - vy * (px - ax)
            row["knee_score"] = max(0.0, orientation * cross / chord_length)
        selected = min(
            frontier,
            key=lambda row: (
                -float(row["knee_score"]),
                float(row["entropy_weight"]),
            ),
        )

    selected["selected_knee"] = True
    return sorted(rows, key=lambda row: float(row["entropy_weight"]))


def select_phase4_pareto_knee_from_daily(daily: pd.DataFrame) -> float:
    """Return λ* selected by the efficiency/congestion Pareto knee rule."""

    records = phase4_pareto_records(daily)
    selected = next(record for record in records if bool(record["selected_knee"]))
    return float(selected["entropy_weight"])


def build_and_run_phase4_multidate(
    data_dir: str | Path,
    *,
    min_lists_per_date: int = DEFAULT_MIN_LISTS_PER_DATE,
    calibration_ratio: float = DEFAULT_CALIBRATION_RATIO,
    split_strategy: Literal["chronological", "random"] = DEFAULT_SPLIT_STRATEGY,
    max_lists: int | None = None,
    number_of_zones: int = 4,
    total_workers: int | None = None,
    volume_basis: Literal["tasks", "units"] = "tasks",
    minimum_per_active_zone: int = 1,
    entropy_weights: Iterable[float] = DEFAULT_ENTROPY_WEIGHTS,
    selection_rule: SelectionRule = DEFAULT_SELECTION_RULE,
    selection_metric: SelectionMetric = DEFAULT_SELECTION_METRIC,
    seed: int = 42,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    edge_capacity: int = 1,
    pick_node_capacity: int = 1,
    sample_seconds: float = 5.0,
    return_to_io: bool = True,
    progress: ConsoleProgress | None = None,
) -> Phase4MultiDateRun:
    """Execute Phase 4A~4E while leaving holdout dates completely unsimulated."""

    weights = _validate_entropy_weights(entropy_weights)
    if not any(math.isclose(value, 0.0) for value in weights):
        raise ValueError("다중 날짜 Phase 4 통계 비교를 위해 entropy_weights에 λ=0이 필요합니다.")
    if selection_rule not in {"pareto_knee", "single_metric"}:
        raise ValueError(f"지원하지 않는 selection_rule입니다: {selection_rule}")
    if selection_rule == "pareto_knee" and selection_metric != PARETO_EFFICIENCY_METRIC:
        raise ValueError(
            "pareto_knee 선택에서는 효율성 축을 mean_flow_time_seconds로 고정합니다. "
            "다른 단일 KPI를 사용하려면 --selection-rule single_metric을 사용하세요."
        )

    if progress is not None:
        progress.report(0.02, "Phase 4A | Loading input data")
    dataset = load_dataset(data_dir)
    if progress is not None:
        progress.report(0.06, "Phase 4A | Building deterministic warehouse graph")
    warehouse = WarehouseGraph.build(
        dataset.storage_locations,
        dataset.support_points,
        deterministic_order=True,
    )
    audit = audit_picking_locations(warehouse, dataset.picking_lists)
    if progress is not None:
        progress.report(0.09, "Phase 4A | Building 20 demand micro-zones / 4 workforce macro-zones")
    zones = build_aisle_zones(warehouse, number_of_zones=number_of_zones)
    profiles, selected_by_date = extract_phase4_date_profiles(
        warehouse,
        dataset.picking_lists,
        zones,
        min_lists_per_date=min_lists_per_date,
        max_lists=max_lists,
        total_workers=total_workers,
        volume_basis=volume_basis,
        minimum_per_active_zone=minimum_per_active_zone,
    )
    eligible_dates = tuple(profile.selected_date for profile in profiles if profile.eligible)
    if progress is not None:
        progress.report(
            0.10,
            "Phase 4B | Splitting calibration / holdout dates",
            current=f"eligible={len(eligible_dates):,}",
        )
    calibration_dates, holdout_dates = split_phase4_dates(
        eligible_dates,
        calibration_ratio=calibration_ratio,
        split_strategy=split_strategy,
        seed=seed,
    )

    calibration_results: list[Phase4CalibrationDateResult] = []
    for date_index, selected_date in enumerate(calibration_dates):
        calibration_results.append(
            _run_phase4_calibration_date(
                warehouse,
                zones,
                selected_date,
                selected_by_date[selected_date],
                total_workers=total_workers,
                volume_basis=volume_basis,
                minimum_per_active_zone=minimum_per_active_zone,
                entropy_weights=weights,
                seed=seed,
                walking_speed_mps=walking_speed_mps,
                pick_seconds_per_unit=pick_seconds_per_unit,
                edge_capacity=edge_capacity,
                pick_node_capacity=pick_node_capacity,
                sample_seconds=sample_seconds,
                return_to_io=return_to_io,
                progress=progress,
                date_index=date_index,
                date_count=len(calibration_dates),
            )
        )

    provisional = Phase4MultiDateRun(
        dataset=dataset,
        warehouse=warehouse,
        audit=audit,
        zones=zones,
        profiles=profiles,
        eligible_dates=eligible_dates,
        calibration_dates=calibration_dates,
        holdout_dates=holdout_dates,
        calibration_results=tuple(calibration_results),
        entropy_weights=weights,
        selection_rule=selection_rule,
        selection_metric=selection_metric,
        selected_entropy_weight=0.0,
        split_strategy=split_strategy,
        calibration_ratio=float(calibration_ratio),
    )
    daily = pd.DataFrame(phase4_daily_records(provisional))
    if selection_rule == "pareto_knee":
        selected_weight = select_phase4_pareto_knee_from_daily(daily)
    else:
        selected_weight = select_phase4_entropy_weight_from_daily(
            daily, metric=selection_metric
        )
    return replace(provisional, selected_entropy_weight=selected_weight)


def _date_profile_records(run: Phase4MultiDateRun) -> list[dict[str, object]]:
    calibration_set = set(run.calibration_dates)
    holdout_set = set(run.holdout_dates)
    records: list[dict[str, object]] = []
    for profile in run.profiles:
        if profile.selected_date in calibration_set:
            split = "calibration"
        elif profile.selected_date in holdout_set:
            split = "holdout"
        else:
            split = "ineligible"
        record = asdict(profile)
        record["selected_date"] = profile.selected_date.isoformat()
        record["split"] = split
        records.append(record)
    return records


def write_phase4_multidate_results(
    output_dir: str | Path,
    run: Phase4MultiDateRun,
    *,
    parameters: dict[str, object],
) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    daily = pd.DataFrame(phase4_daily_records(run))
    aggregate = pd.DataFrame(
        aggregate_phase4_lambda_records(daily, metrics=PHASE4_COMPARISON_METRICS)
    )
    paired = pd.DataFrame(
        paired_phase4_lambda_records(
            daily,
            reference_weight=0.0,
            metrics=PHASE4_COMPARISON_METRICS,
        )
    )
    pareto = pd.DataFrame(phase4_pareto_records(daily))
    selected_congestion = pd.DataFrame(
        selected_phase4_congestion_kpi_records(
            paired,
            selected_weight=run.selected_entropy_weight,
        )
    )
    date_profiles = pd.DataFrame(_date_profile_records(run))
    allocations = pd.DataFrame(phase4_allocation_records(run))

    date_profiles.to_csv(output_dir / "phase4_dates.csv", index=False)
    daily.to_csv(output_dir / "phase4_daily_results.csv", index=False)
    aggregate.to_csv(output_dir / "phase4_lambda_statistics.csv", index=False)
    paired.to_csv(output_dir / "phase4_pairwise_vs_lambda0.csv", index=False)
    pareto.to_csv(output_dir / "phase4_pareto_analysis.csv", index=False)
    selected_congestion.to_csv(
        output_dir / "phase4_selected_congestion_kpis_vs_lambda0.csv", index=False
    )
    allocations.to_csv(output_dir / "phase4_allocations.csv", index=False)

    primary = aggregate[aggregate["metric"] == run.selection_metric].copy()
    selected_stats = primary[
        primary["entropy_weight"].astype(float).map(
            lambda value: math.isclose(value, run.selected_entropy_weight)
        )
    ].iloc[0]
    selected_pair = paired[
        (paired["metric"] == run.selection_metric)
        & paired["entropy_weight"].astype(float).map(
            lambda value: math.isclose(value, run.selected_entropy_weight)
        )
    ].iloc[0]
    selected_pareto = pareto[
        pareto["entropy_weight"].astype(float).map(
            lambda value: math.isclose(value, run.selected_entropy_weight)
        )
    ].iloc[0]
    pareto_weights = [
        float(value)
        for value in pareto.loc[pareto["pareto_frontier"].astype(bool), "entropy_weight"]
    ]
    knee_frontier_weights = [
        float(value)
        for value in pareto.loc[pareto["knee_frontier"].astype(bool), "entropy_weight"]
    ]
    pareto_knee_weight = float(
        pareto.loc[pareto["selected_knee"].astype(bool), "entropy_weight"].iloc[0]
    )
    pareto_knee_row = pareto[pareto["selected_knee"].astype(bool)].iloc[0]
    selected_knee_score = float(selected_pareto["knee_score"])
    recommendation = {
        "phase": "4E",
        "model_revision": PHASE4_MODEL_REVISION,
        "allocation_objective": "J(n;lambda)=D(n)+lambda*R(n)",
        "selection_rule": run.selection_rule,
        "selection_metric": run.selection_metric,
        "direction": "maximize" if run.selection_metric in MAXIMIZE_METRICS else "minimize",
        "entropy_weight": run.selected_entropy_weight,
        "metric_mean": float(selected_stats["mean"]),
        "metric_median": float(selected_stats["median"]),
        "n_calibration_dates": int(selected_stats["n_dates"]),
        "reference_entropy_weight": 0.0,
        "mean_improvement_vs_lambda0_pct": float(selected_pair["mean_improvement_pct"]),
        "wins_vs_lambda0": int(selected_pair["wins"]),
        "ties_vs_lambda0": int(selected_pair["ties"]),
        "losses_vs_lambda0": int(selected_pair["losses"]),
        "wilcoxon_p_value_vs_lambda0": float(selected_pair["wilcoxon_p_value"]),
        "pareto_efficiency_metric": str(PARETO_EFFICIENCY_METRIC),
        "pareto_congestion_metrics": [str(metric) for metric in PARETO_CONGESTION_METRICS],
        "pareto_frontier_entropy_weights": pareto_weights,
        "knee_frontier_entropy_weights": knee_frontier_weights,
        "pareto_knee_entropy_weight": pareto_knee_weight,
        "pareto_knee_score": float(pareto_knee_row["knee_score"]),
        "selected_entropy_weight_knee_score": (
            selected_knee_score if math.isfinite(selected_knee_score) else None
        ),
        "flow_time_change_vs_lambda0_pct": float(
            selected_pareto["flow_time_change_vs_lambda0_pct"]
        ),
        "conflicts_reduction_vs_lambda0_pct": float(
            selected_pareto["conflicts_reduction_vs_lambda0_pct"]
        ),
        "wait_reduction_vs_lambda0_pct": float(
            selected_pareto["wait_reduction_vs_lambda0_pct"]
        ),
        "congestion_reduction_vs_lambda0_pct": float(
            selected_pareto["congestion_reduction_vs_lambda0_pct"]
        ),
        "composite_congestion_reduction_vs_lambda0_pct": float(
            selected_pareto["composite_congestion_reduction_vs_lambda0_pct"]
        ),
        "selected_congestion_kpis_vs_lambda0": selected_congestion.to_dict(orient="records"),
        "calibration_dates": [value.isoformat() for value in run.calibration_dates],
        "holdout_dates": [value.isoformat() for value in run.holdout_dates],
        "selection_rule_definition": (
            "pareto_knee: Calibration 날짜를 동일 가중치로 두고 Flow Time / Conflicts / Wait / "
            "Congestion ratio의 4개 평균을 모두 최소화하는 비지배해를 찾는다. 세 혼잡 KPI는 "
            "각각 λ=0 평균으로 정규화한 뒤 동일 가중 평균하여 congestion index를 만들고, "
            "Flow Time-혼잡 index Pareto 곡선의 양 끝점을 잇는 chord에서 ideal 방향으로 가장 "
            "멀리 떨어진 knee point를 λ*로 선택한다. single_metric: 기존 단일 KPI 평균 최적화를 "
            "사용한다. Wilcoxon은 λ=0 대비 통계적 비교용이며 선택의 강제 필터가 아니다."
        ),
    }
    with (output_dir / "phase4_recommendation.json").open("w", encoding="utf-8") as file:
        json.dump(recommendation, file, ensure_ascii=False, indent=2)

    metadata = {
        "phase": 4,
        "model_revision": PHASE4_MODEL_REVISION,
        "workflow": "4A_full_dates -> 4B_split -> 4C_calibration_DES -> 4D_statistics -> 4E_lambda_star",
        "input": {
            "storage_locations": len(run.dataset.storage_locations),
            "support_points": len(run.dataset.support_points),
            "picking_lists_total": len(run.dataset.picking_lists),
            "fully_resolvable_lists_total": run.audit.fully_resolvable_lists,
            "operating_dates_found": len(run.profiles),
            "eligible_dates": len(run.eligible_dates),
            "calibration_dates": len(run.calibration_dates),
            "holdout_dates": len(run.holdout_dates),
        },
        "parameters": parameters,
        "calibration_dates": [value.isoformat() for value in run.calibration_dates],
        "holdout_dates": [value.isoformat() for value in run.holdout_dates],
        "selected": recommendation,
        "definitions": {
            "phase4A": "timestamp가 있고 모든 picking location이 warehouse graph에서 resolve되며 최소 list/worker 조건을 만족하는 날짜를 적합 날짜로 정의한다.",
            "phase4B": "날짜 단위로 Calibration/Holdout을 분리하며 Holdout 날짜는 Phase 4C DES에 사용하지 않는다.",
            "phase4C": (
                "각 날짜에서 20개 micro-zone 수요를 4개 macro-zone 내부 Shannon entropy로 요약하고 "
                "C_z=1-H_z를 계산한다. 가능한 정수 worker vector n을 직접 열거하여 "
                "D(n)=0.5*Σ|n_z/N-V_z/ΣV|, R(n)=Σ C_z*C(n_z,2), "
                "J(n;λ)=D(n)+λR(n)을 최소화하며, "
                "동일 정수 worker allocation을 만드는 λ는 DES 결과를 재사용한다."
            ),
            "phase4D": (
                "각 날짜를 동일 가중치로 λ별 KPI 평균/표준편차/중앙값을 계산하고 λ=0과 paired "
                "two-sided Wilcoxon signed-rank 검정을 수행한다. 선택된 λ*에 대해서는 Conflicts, "
                "Wait, Congestion의 Mean(λ=0), Mean(λ*), 평균 기준 개선율, W/T/L, p-value를 별도 "
                "표와 CSV로 기록한다. Flow Time, Conflicts, Wait, Congestion ratio의 평균을 함께 "
                "사용해 4목적 Pareto 비지배 여부도 계산한다."
            ),
            "phase4E": (
                "기본 pareto_knee 규칙에서는 세 혼잡 KPI를 λ=0 기준으로 정규화한 동일가중 congestion "
                "index와 Mean Flow Time의 2차원 trade-off 곡선에서 knee point를 λ*로 선택한다. "
                "--selection-rule single_metric을 지정하면 기존 단일 KPI 평균 최적화도 재현할 수 있다."
            ),
            "lambda_zero": "λ=0은 Phase 3 Volume Proportional의 정수 배치를 정확히 control로 사용한다.",
            "entropy_concentration": "C_z=1-H_z. H_z는 macro-zone 내부 5개 micro-zone workload의 normalized Shannon entropy이다.",
            "D_demand_mismatch": "수요비중과 정수 작업자비중의 total-variation distance. 작을수록 Volume 배치에 가깝다.",
            "R_congestion_risk": "같은 macro-zone에 배치된 작업자 쌍 C(n_z,2)을 해당 zone의 수요 집중도 C_z로 가중한 정수 혼잡위험 지수.",
            "J_integer_objective": "D + lambda*R. lambda가 커질수록 수요 적합도 손실을 감수하고 집중 zone의 동시 작업자 쌍을 줄일 수 있다.",
            "pareto_objectives": "Calibration 평균 Mean Flow Time / Conflicts / Congestion Wait / Congestion Delay Ratio를 모두 최소화한다.",
            "congestion_index": "Conflicts, Wait, Congestion Ratio 각각을 λ=0 평균으로 나눈 뒤 동일 가중 평균한다. λ=0에서 1.0이며 작을수록 종합 혼잡이 낮다.",
            "knee_point": "Pareto frontier에서 best-flow와 best-congestion endpoint를 연결한 chord로부터 ideal 방향의 정규화 수직거리가 최대인 점.",
            "holdout_lock": "Holdout 날짜 KPI는 Phase 4에서 계산하지 않으며 이후 Phase 5 검증에만 사용한다.",
        },
    }
    with (output_dir / "phase4_metadata.json").open("w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2, default=str)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Entropy Thesis - Phase 4A~4E multi-date entropy calibration"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--date",
        dest="target_date",
        type=_parse_date,
        default=None,
        help=(
            "single-date integer-objective diagnostic mode. Example: 2023-01-05. "
            "When supplied, Phase 4A~4E multi-date calibration is not executed."
        ),
    )
    parser.add_argument("--min-lists", type=int, default=DEFAULT_MIN_LISTS_PER_DATE)
    parser.add_argument("--calibration-ratio", type=float, default=DEFAULT_CALIBRATION_RATIO)
    parser.add_argument(
        "--split-strategy",
        choices=("chronological", "random"),
        default=DEFAULT_SPLIT_STRATEGY,
    )
    parser.add_argument("--max-lists", type=int, default=None)
    parser.add_argument("--zones", type=int, default=4)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--volume-basis", choices=("tasks", "units"), default="tasks")
    parser.add_argument("--minimum-per-active-zone", type=int, default=1)
    parser.add_argument(
        "--entropy-weights",
        type=_parse_entropy_weights,
        default=DEFAULT_ENTROPY_WEIGHTS,
        help="comma-separated lambda values; default=0,0.05,0.1,0.25,0.5,0.75,1,2,4,8",
    )
    parser.add_argument(
        "--selection-rule",
        choices=("pareto_knee", "single_metric"),
        default=DEFAULT_SELECTION_RULE,
        help=(
            "lambda selection rule. default=pareto_knee (Flow Time vs congestion trade-off); "
            "single_metric reproduces the legacy one-KPI mean rule"
        ),
    )
    parser.add_argument(
        "--selection-metric",
        choices=tuple(PHASE4_COMPARISON_METRICS),
        default=DEFAULT_SELECTION_METRIC,
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--speed", type=float, default=1.2)
    parser.add_argument("--pick-seconds", type=float, default=3.0)
    parser.add_argument("--edge-capacity", type=int, default=1)
    parser.add_argument("--pick-node-capacity", type=int, default=1)
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--no-return-to-io", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("results/phase4"))
    args = parser.parse_args()

    if args.target_date is not None:
        progress = ConsoleProgress()
        progress.start(
            f"Phase 4 integer-objective single-date diagnostic ({args.target_date.isoformat()})"
        )
        (
            dataset,
            warehouse,
            audit,
            selected_date,
            selected_lists,
            zones,
            assignments,
            demand_entropy,
            results,
            selected,
            origin,
        ) = build_and_run_phase4(
            args.data_dir,
            target_date=args.target_date,
            max_lists=args.max_lists,
            number_of_zones=args.zones,
            total_workers=args.workers,
            volume_basis=args.volume_basis,
            minimum_per_active_zone=args.minimum_per_active_zone,
            entropy_weights=args.entropy_weights,
            selection_metric=args.selection_metric,
            seed=args.seed,
            walking_speed_mps=args.speed,
            pick_seconds_per_unit=args.pick_seconds,
            edge_capacity=args.edge_capacity,
            pick_node_capacity=args.pick_node_capacity,
            sample_seconds=args.sample_seconds,
            return_to_io=not args.no_return_to_io,
            progress=progress,
            print_objective_diagnostic=True,
        )
        workloads = zone_workload(zones, assignments, basis=args.volume_basis)
        macro_profiles = macro_zone_demand_profiles(
            warehouse, selected_lists, zones, basis=args.volume_basis
        )
        concentrations = tuple(
            profile.microzone_concentration for profile in macro_profiles
        )
        diagnostic_output_dir = args.output_dir / f"diagnostic_{selected_date.isoformat()}"
        parameters = {
            "selection_metric": args.selection_metric,
            "target_date": selected_date.isoformat(),
            "max_lists": args.max_lists,
            "zones": args.zones,
            "workers": args.workers,
            "volume_basis": args.volume_basis,
            "minimum_per_active_zone": args.minimum_per_active_zone,
            "entropy_weights": list(args.entropy_weights),
            "seed": args.seed,
            "walking_speed_mps": args.speed,
            "pick_seconds_per_unit": args.pick_seconds,
            "edge_capacity": args.edge_capacity,
            "pick_node_capacity": args.pick_node_capacity,
            "sample_seconds": args.sample_seconds,
            "return_to_io": not args.no_return_to_io,
        }
        metadata = {
            "phase": "4-diagnostic",
            "model_revision": PHASE4_MODEL_REVISION,
            "selected_date": selected_date.isoformat(),
            "parameters": parameters,
            "definitions": {
                "integer_objective": "J(n;lambda) = D(n) + lambda * R(n)",
                "D": "0.5 * sum_z |n_z/N - V_z/sum(V)|",
                "R": "sum_z (1-H_z) * choose(n_z, 2)",
                "lambda_zero": "lambda=0 uses the exact Phase-3 Volume Proportional integer allocation",
                "movement": "0.5 * L1 distance from the lambda=0 worker-count vector",
            },
            "input": {
                "storage_locations": len(dataset.storage_locations),
                "support_points": len(dataset.support_points),
                "fully_resolvable_lists_total": audit.fully_resolvable_lists,
                "selected_lists": len(selected_lists),
                "observed_workers": len({item.operator for item in selected_lists}),
            },
            "demand_entropy": asdict(demand_entropy),
        }
        write_phase4_results(
            diagnostic_output_dir,
            zones=zones,
            workloads=workloads,
            results=results,
            selected=selected,
            origin=origin,
            metadata=metadata,
        )
        progress.complete("Phase 4 single-date diagnostic completed")
        _print_single_date_diagnostic(
            selected_date=selected_date,
            results=results,
            selected=selected,
            output_dir=diagnostic_output_dir,
        )
        print(
            f"Total execution time  : {format_duration(progress.elapsed_seconds)} "
            f"({progress.elapsed_seconds:,.2f} s)"
        )
        return

    progress = ConsoleProgress()
    progress.start("Phase 4A~4E multi-date entropy calibration")
    run = build_and_run_phase4_multidate(
        args.data_dir,
        min_lists_per_date=args.min_lists,
        calibration_ratio=args.calibration_ratio,
        split_strategy=args.split_strategy,
        max_lists=args.max_lists,
        number_of_zones=args.zones,
        total_workers=args.workers,
        volume_basis=args.volume_basis,
        minimum_per_active_zone=args.minimum_per_active_zone,
        entropy_weights=args.entropy_weights,
        selection_rule=args.selection_rule,
        selection_metric=args.selection_metric,
        seed=args.seed,
        walking_speed_mps=args.speed,
        pick_seconds_per_unit=args.pick_seconds,
        edge_capacity=args.edge_capacity,
        pick_node_capacity=args.pick_node_capacity,
        sample_seconds=args.sample_seconds,
        return_to_io=not args.no_return_to_io,
        progress=progress,
    )

    parameters = {
        "min_lists_per_date": args.min_lists,
        "calibration_ratio": args.calibration_ratio,
        "split_strategy": args.split_strategy,
        "max_lists": args.max_lists,
        "zones": args.zones,
        "workers": args.workers,
        "volume_basis": args.volume_basis,
        "minimum_per_active_zone": args.minimum_per_active_zone,
        "entropy_weights": list(args.entropy_weights),
        "selection_rule": args.selection_rule,
        "selection_metric": args.selection_metric,
        "seed": args.seed,
        "walking_speed_mps": args.speed,
        "pick_seconds_per_unit": args.pick_seconds,
        "edge_capacity": args.edge_capacity,
        "pick_node_capacity": args.pick_node_capacity,
        "sample_seconds": args.sample_seconds,
        "return_to_io": not args.no_return_to_io,
    }
    progress.report(0.94, "Phase 4D/4E | Writing statistics and recommendation")
    write_phase4_multidate_results(args.output_dir, run, parameters=parameters)
    progress.complete("Phase 4A~4E processing completed")

    daily = pd.DataFrame(phase4_daily_records(run))
    statistics = pd.DataFrame(aggregate_phase4_lambda_records(daily))
    primary = statistics[statistics["metric"] == run.selection_metric].sort_values("entropy_weight")
    paired = pd.DataFrame(paired_phase4_lambda_records(daily))
    primary_paired = paired[paired["metric"] == run.selection_metric].sort_values("entropy_weight")
    selected_congestion = pd.DataFrame(
        selected_phase4_congestion_kpi_records(
            paired,
            selected_weight=run.selected_entropy_weight,
        )
    )
    pareto = pd.DataFrame(phase4_pareto_records(daily)).sort_values("entropy_weight")
    knee_weight = float(
        pareto.loc[pareto["selected_knee"].astype(bool), "entropy_weight"].iloc[0]
    )

    print()
    print("=== Phase 4A | Eligible Operating Dates ===")
    print(f"Operating dates found : {len(run.profiles):,}")
    print(f"Eligible dates        : {len(run.eligible_dates):,}")
    print()
    print("=== Phase 4B | Calibration / Holdout Split ===")
    print(f"Split strategy        : {run.split_strategy}")
    print(f"Calibration ratio     : {run.calibration_ratio:.0%}")
    print(f"Calibration dates     : {len(run.calibration_dates):,} ({run.calibration_dates[0]} .. {run.calibration_dates[-1]})")
    print(f"Holdout dates         : {len(run.holdout_dates):,} ({run.holdout_dates[0]} .. {run.holdout_dates[-1]})")
    print()
    print("=== Phase 4C/4D | Lambda Calibration ===")
    print(f"Lambda candidates     : {', '.join(f'{v:g}' for v in run.entropy_weights)}")
    print(f"Selection rule        : {run.selection_rule}")
    print(f"Efficiency metric     : {run.selection_metric}")
    print()
    print(
        f"=== Phase 4D | Efficiency-Congestion Trade-off "
        f"({len(run.calibration_dates):,} calibration dates) ==="
    )
    print(
        "Lambda   Flow(s)  FlowΔ%   Conflicts  Conf↓%    Wait(s)  Wait↓%  "
        "Cong(%)  Cong↓%   PF  KneeScore"
    )
    for _, row in pareto.iterrows():
        weight = float(row["entropy_weight"])
        marker = "*" if math.isclose(weight, run.selected_entropy_weight) else " "
        pf = "Y" if bool(row["pareto_frontier"]) else "N"
        knee_score = float(row["knee_score"])
        knee_text = f"{knee_score:.4f}" if math.isfinite(knee_score) else "-"
        print(
            f"{marker}{weight:<7g} "
            f"{float(row['mean_flow_time_seconds']):>8,.2f} "
            f"{float(row['flow_time_change_vs_lambda0_pct']):>7.2f} "
            f"{float(row['congestion_conflicts']):>10,.2f} "
            f"{float(row['conflicts_reduction_vs_lambda0_pct']):>7.2f} "
            f"{float(row['congestion_wait_seconds']):>10,.2f} "
            f"{float(row['wait_reduction_vs_lambda0_pct']):>7.2f} "
            f"{float(row['congestion_percent']):>8.2f} "
            f"{float(row['congestion_reduction_vs_lambda0_pct']):>7.2f}   "
            f"{pf:>1}   {knee_text:>9}"
        )
    print("  FlowΔ%: + means slower than λ=0; Conf↓/Wait↓/Cong↓: + means congestion reduction.")
    print("  PF=Y: non-dominated in Flow Time / Conflicts / Wait / Congestion ratio.")
    print()
    print(f"=== Phase 4D | Paired {run.selection_metric} vs λ=0 ===")
    print("Lambda   Mean              Std               W/T/L vs λ=0    p-value")
    for _, stat in primary.iterrows():
        weight = float(stat["entropy_weight"])
        pair = primary_paired[primary_paired["entropy_weight"].astype(float).map(lambda v: math.isclose(v, weight))].iloc[0]
        marker = "*" if math.isclose(weight, run.selected_entropy_weight) else " "
        print(
            f"{marker}{weight:<7g} "
            f"{float(stat['mean']):>16,.4f} "
            f"{float(stat['std']):>16,.4f}   "
            f"{int(pair['wins'])}/{int(pair['ties'])}/{int(pair['losses'])}"
            f"           {float(pair['wilcoxon_p_value']):.6g}"
        )
    print()
    print("=== Phase 4D | Paired Congestion KPIs vs λ=0 ===")
    print("Lambda   Metric          Mean(λ=0)   Mean(λ)   Improve%   W/T/L        p-value")
    for _, row in selected_congestion.iterrows():
        weight = float(row["entropy_weight"])
        marker = "*" if math.isclose(weight, run.selected_entropy_weight) else " "
        print(
            f"{marker}{weight:<7g} "
            f"{str(row['metric_label']):<15} "
            f"{float(row['reference_mean_display']):>10,.2f} "
            f"{float(row['candidate_mean_display']):>10,.2f} "
            f"{float(row['improve_pct']):>9.2f}   "
            f"{int(row['wins'])}/{int(row['ties'])}/{int(row['losses'])}"
            f"      {float(row['wilcoxon_p_value']):.6g}"
        )
    print("  Improve%: + means the selected λ* reduced the KPI relative to λ=0.")
    print("  W/T/L: calibration dates where λ* is better / tied / worse than λ=0.")
    print("  p-value: two-sided paired Wilcoxon signed-rank test; p<0.05 is statistically significant.")
    print()
    print("=== Phase 4E | Selected Lambda ===")
    print(f"Selection rule        : {run.selection_rule}")
    print(f"Pareto knee λ         : {knee_weight:g}")
    print(f"Selected λ*           : {run.selected_entropy_weight:g}")
    print(f"Holdout untouched     : yes ({len(run.holdout_dates):,} dates)")
    print(f"Results               : {args.output_dir}")
    print(
        f"Total execution time  : {format_duration(progress.elapsed_seconds)} "
        f"({progress.elapsed_seconds:,.2f} s)"
    )


if __name__ == "__main__":
    main()
