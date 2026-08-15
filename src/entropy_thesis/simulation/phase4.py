from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
import math
from pathlib import Path
from typing import Iterable, Literal

import pandas as pd

from ..allocation import allocate_workers
from ..entropy import normalized_shannon_entropy
from .data_loader import DatasetBundle, PickingList, load_dataset
from .phase1 import Phase1Audit, audit_picking_locations
from .phase2 import DemandEntropyMetrics, calculate_demand_entropy, select_phase2_lists
from .phase3 import (
    AisleZone,
    Phase3MethodResult,
    PickingListZoneAssignment,
    _congestion_records,
    _worker_records,
    build_aisle_zones,
    classify_picking_lists_by_zone,
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

DEFAULT_ENTROPY_WEIGHTS: tuple[float, ...] = (0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
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
    minimum_per_active_zone: int = 1,
) -> tuple[int, ...]:
    """Allocate workers to active zones using entropy regularization.

    Zero-demand zones remain at zero, matching the Phase 3 fairness rule.
    Within active zones, lambda=0 is exactly volume-proportional allocation.
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

    active_counts = allocate_workers(
        "entropy_based",
        total_workers,
        [values[index] for index in active_indices],
        entropy_weight=regularization,
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
        progress.report(0.17, "Building physical aisle zones")
    zones = build_aisle_zones(warehouse, number_of_zones=number_of_zones)
    assignments = classify_picking_lists_by_zone(warehouse, selected_lists, zones)
    workloads = zone_workload(zones, assignments, basis=volume_basis)
    demand_entropy, _ = calculate_demand_entropy(warehouse, selected_lists)

    inferred_workers = len({p.operator for p in selected_lists})
    workers_to_use = inferred_workers if total_workers is None else total_workers
    if workers_to_use <= 0:
        raise ValueError("Phase 4 total_workers는 1 이상이어야 합니다.")

    candidates = build_entropy_candidates(
        total_workers=workers_to_use,
        workloads=workloads,
        entropy_weights=entropy_weights,
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Entropy Thesis - Phase 4 entropy-based worker allocation tuning"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--date", dest="target_date", type=_parse_date, default=None)
    parser.add_argument("--max-lists", type=int, default=None)
    parser.add_argument("--zones", type=int, default=4)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--volume-basis", choices=("tasks", "units"), default="tasks")
    parser.add_argument("--minimum-per-active-zone", type=int, default=1)
    parser.add_argument(
        "--entropy-weights",
        type=_parse_entropy_weights,
        default=DEFAULT_ENTROPY_WEIGHTS,
        help="comma-separated lambda values, e.g. 0,0.25,0.5,1,2,4,8",
    )
    parser.add_argument(
        "--selection-metric",
        choices=(
            "mean_flow_time_seconds",
            "makespan_seconds",
            "congestion_wait_seconds",
            "congestion_conflicts",
            "total_distance_m",
            "mean_release_delay_seconds",
            "mean_spatial_entropy_normalized",
        ),
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
    progress.start("Phase 4 entropy-based allocation tuning")
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
    )

    workloads = zone_workload(zones, assignments, basis=args.volume_basis)
    unique_allocations = {item.candidate.allocation_id for item in results}
    metadata = {
        "phase": 4,
        "selected_date": selected_date.isoformat(),
        "origin_timestamp": origin.isoformat(),
        "input": {
            "storage_locations": len(dataset.storage_locations),
            "support_points": len(dataset.support_points),
            "picking_lists_total": len(dataset.picking_lists),
            "fully_resolvable_lists_total": audit.fully_resolvable_lists,
            "selected_lists": len(selected_lists),
            "observed_operators": len({p.operator for p in selected_lists}),
        },
        "parameters": {
            "zones": args.zones,
            "workers": args.workers,
            "effective_workers": sum(results[0].candidate.worker_counts),
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
            "max_lists": args.max_lists,
        },
        "candidate_count": len(results),
        "unique_simulation_count": len(unique_allocations),
        "zones": [
            {
                "zone_id": zone.zone_id,
                "aisle_y_values_m": list(zone.aisle_y_values),
                "y_min_m": zone.y_min_m,
                "y_max_m": zone.y_max_m,
                "allocation_workload": workload,
            }
            for zone, workload in zip(zones, workloads, strict=True)
        ],
        "definitions": {
            "entropy_objective": "min KL(p || d) - lambda * H(p)",
            "closed_form": "p_i proportional to d_i ** (1 / (1 + lambda))",
            "lambda_zero": (
                "lambda=0은 Phase 3 Volume Proportional Allocation과 동일한 연속 비율을 만들며 "
                "같은 minimum/water-filling/largest-remainder 규칙으로 정수화한다."
            ),
            "lambda_effect": (
                "lambda가 커질수록 양의 수요가 있는 zone의 worker share가 균등 분포 쪽으로 이동한다."
            ),
            "duplicate_allocation_cache": (
                "서로 다른 lambda가 동일한 정수 worker allocation을 만들면 DES 입력이 동일하므로 "
                "한 번만 시뮬레이션하고 결과를 해당 lambda 후보들이 공유한다."
            ),
            "selection_rule": (
                "--selection-metric으로 지정한 단일 KPI를 기준으로 lambda를 선택한다. "
                "비용/시간 지표는 최소화하고 mean_spatial_entropy_normalized는 최대화한다. "
                "정확한 동률이면 더 작은 lambda를 선택한다."
            ),
            "default_selection_metric": (
                "mean_flow_time_seconds: 각 picking list의 release부터 completion까지 flow time 평균"
            ),
            "phase4_role": (
                "한 calibration date에서 entropy lambda를 탐색한다. 여러 날짜에 대한 일반화 검증은 Phase 5에서 수행한다."
            ),
        },
        "selected": {
            "entropy_weight": selected.candidate.entropy_weight,
            "allocation_id": selected.candidate.allocation_id,
            "worker_counts": list(selected.candidate.worker_counts),
            "selection_metric": args.selection_metric,
            "selection_metric_value": _selection_value(selected, args.selection_metric),
        },
        "demand_entropy": asdict(demand_entropy),
    }

    progress.report(0.94, "Writing Phase 4 result files", current=str(args.output_dir))
    write_phase4_results(
        args.output_dir,
        zones=zones,
        workloads=workloads,
        results=results,
        selected=selected,
        origin=origin,
        metadata=metadata,
    )
    progress.complete("Phase 4 processing completed")

    print()
    print("=== Phase 4 Entropy Allocation Tuning ===")
    print(f"Selected date        : {selected_date.isoformat()}")
    print(f"Picking lists        : {len(selected_lists):,}")
    print(f"Observed operators   : {len({p.operator for p in selected_lists}):,}")
    print(f"Effective workers    : {sum(results[0].candidate.worker_counts):,}")
    print(f"Aisle zones          : {len(zones):,}")
    print(f"Lambda candidates    : {len(results):,}")
    print(f"Unique DES runs      : {len(unique_allocations):,}")
    print(f"Selection metric     : {args.selection_metric}")
    print()
    print("=== Candidate Comparison ===")
    print(
        "Lambda   Allocation   Workers       Alloc H   L1 gap   Mean flow(s)   "
        "Makespan(s)   Wait(s)   Conflicts   Spatial H"
    )
    for item in results:
        summary = item.simulation.summary
        marker = "*" if item.candidate == selected.candidate else " "
        workers_text = "/".join(str(v) for v in item.candidate.worker_counts)
        print(
            f"{marker}{item.candidate.entropy_weight:<7g} "
            f"{item.candidate.allocation_id:<11} "
            f"{workers_text:<13} "
            f"{summary.worker_allocation_entropy_normalized:>7.4f}   "
            f"{summary.demand_worker_l1_gap:>6.4f}   "
            f"{summary.mean_flow_time_seconds:>12,.2f}   "
            f"{summary.makespan_seconds:>11,.2f}   "
            f"{summary.congestion_wait_seconds:>7,.2f}   "
            f"{summary.congestion_conflicts:>9,}   "
            f"{summary.mean_spatial_entropy_normalized:>9.4f}"
        )
    print()
    print(
        f"Selected lambda      : {selected.candidate.entropy_weight:g} "
        f"({selected.candidate.allocation_id})"
    )
    print(f"Selected workers     : {'/'.join(str(v) for v in selected.candidate.worker_counts)}")
    print(f"Results              : {args.output_dir}")
    print(
        f"Total execution time : {format_duration(progress.elapsed_seconds)} "
        f"({progress.elapsed_seconds:,.2f} s)"
    )


if __name__ == "__main__":
    main()
