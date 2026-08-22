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
    "total_distance_m",
    "mean_release_delay_seconds",
    "mean_spatial_entropy_normalized",
]

DEFAULT_ENTROPY_WEIGHTS: tuple[float, ...] = (0.0, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 4.0, 8.0)
DEFAULT_SELECTION_METRIC: SelectionMetric = "mean_flow_time_seconds"
MAXIMIZE_METRICS = {"mean_spatial_entropy_normalized"}


@dataclass(frozen=True)
class EntropyAllocationCandidate:
    entropy_weight: float
    allocation_id: str
    worker_counts: tuple[int, ...]
    reused_allocation: bool


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
    """Allocate workers using macro workload plus within-zone demand concentration.

    For macro-zone ``z`` the adjusted allocation weight is

        adjusted_z = workload_z * (1 + lambda * concentration_z)

    where ``concentration_z = 1 - H_z`` and ``H_z`` is normalized Shannon
    entropy of the five micro-zone workloads inside that macro-zone.  Thus
    ``lambda=0`` is exactly Phase-3 volume proportional allocation. Positive
    lambda gives additional weight to macro-zones whose demand is spatially
    concentrated, which is the proposed entropy-aware mechanism.
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

    adjusted = [
        values[index] * (1.0 + regularization * concentrations[index])
        for index in active_indices
    ]
    active_counts = allocate_workers(
        "volume_proportional",
        total_workers,
        adjusted,
        minimum_per_zone=minimum_per_active_zone,
    )
    result = [0] * len(values)
    for index, count in zip(active_indices, active_counts, strict=True):
        result[index] = int(count)
    return tuple(result)

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
    allocation_ids: dict[tuple[int, ...], str] = {}
    candidates: list[EntropyAllocationCandidate] = []
    for entropy_weight in weights:
        counts = allocate_phase4_workers(
            total_workers=total_workers,
            workloads=workload_values,
            entropy_weight=entropy_weight,
            microzone_concentrations=microzone_concentrations,
            minimum_per_active_zone=minimum_per_active_zone,
        )
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
    "total_distance_m",
    "mean_release_delay_seconds",
    "mean_spatial_entropy_normalized",
)


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
                adjusted_workload = workload * (
                    1.0 + item.candidate.entropy_weight * concentration
                )
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
                        "entropy_adjusted_workload": adjusted_workload,
                        "workers": workers,
                        "worker_share": 0.0 if total_workers == 0 else workers / total_workers,
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
        selection_metric=selection_metric,
        selected_entropy_weight=0.0,
        split_strategy=split_strategy,
        calibration_ratio=float(calibration_ratio),
    )
    daily = pd.DataFrame(phase4_daily_records(provisional))
    selected_weight = select_phase4_entropy_weight_from_daily(daily, metric=selection_metric)
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
    date_profiles = pd.DataFrame(_date_profile_records(run))
    allocations = pd.DataFrame(phase4_allocation_records(run))

    date_profiles.to_csv(output_dir / "phase4_dates.csv", index=False)
    daily.to_csv(output_dir / "phase4_daily_results.csv", index=False)
    aggregate.to_csv(output_dir / "phase4_lambda_statistics.csv", index=False)
    paired.to_csv(output_dir / "phase4_pairwise_vs_lambda0.csv", index=False)
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
    recommendation = {
        "phase": "4E",
        "model_revision": THESIS_MODEL_REVISION,
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
        "calibration_dates": [value.isoformat() for value in run.calibration_dates],
        "holdout_dates": [value.isoformat() for value in run.holdout_dates],
        "selection_rule": (
            "Calibration 날짜를 동일 가중치로 두고 primary KPI의 날짜 평균이 최적인 λ를 선택한다. "
            "정확한 동률이면 더 작은 λ를 선택한다. Wilcoxon 검정은 λ=0 대비 통계적 비교용이며 "
            "λ* 선택 자체의 유의성 필터로 사용하지 않는다."
        ),
    }
    with (output_dir / "phase4_recommendation.json").open("w", encoding="utf-8") as file:
        json.dump(recommendation, file, ensure_ascii=False, indent=2)

    metadata = {
        "phase": 4,
        "model_revision": THESIS_MODEL_REVISION,
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
                "C_z=1-H_z를 계산한다. 각 λ에 대해 A_z=V_z*(1+λ*C_z) 가중치로 작업자를 배치하며, "
                "동일 정수 worker allocation을 만드는 λ는 DES 결과를 재사용한다."
            ),
            "phase4D": "각 날짜를 동일 가중치로 λ별 KPI 평균/표준편차/중앙값을 계산하고 λ=0과 paired Wilcoxon signed-rank 검정을 수행한다.",
            "phase4E": "primary KPI의 Calibration 날짜 평균을 최적화하는 λ를 λ*로 선택하며 동률이면 더 작은 λ를 선택한다.",
            "lambda_zero": "λ=0이면 A_z=V_z이므로 Volume Proportional Allocation과 정확히 동일하다.",
            "entropy_concentration": "C_z=1-H_z. H_z는 macro-zone 내부 5개 micro-zone workload의 normalized Shannon entropy이다.",
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
    print(f"Selection metric      : {run.selection_metric}")
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
    print("=== Phase 4E | Selected Lambda ===")
    print(f"Selected λ*           : {run.selected_entropy_weight:g}")
    print(f"Holdout untouched     : yes ({len(run.holdout_dates):,} dates)")
    print(f"Results               : {args.output_dir}")
    print(
        f"Total execution time  : {format_duration(progress.elapsed_seconds)} "
        f"({progress.elapsed_seconds:,.2f} s)"
    )


if __name__ == "__main__":
    main()
