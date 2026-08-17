from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
import json
import math
from pathlib import Path
from statistics import mean
from typing import Iterable, Literal

import pandas as pd
from scipy.stats import wilcoxon

from .data_loader import DatasetBundle, PickingList, load_dataset
from .phase1 import Phase1Audit, audit_picking_locations, fully_resolvable_lists
from .phase2 import DemandEntropyMetrics, calculate_demand_entropy
from .phase3 import (
    PHASE3_METHODS,
    AisleZone,
    Phase3MethodResult,
    PickingListZoneAssignment,
    allocate_phase3_workers,
    build_aisle_zones,
    classify_picking_lists_by_zone,
    run_phase3_method,
    run_phase3_observed_baseline,
    zone_workload,
)
from .phase4 import (
    DEFAULT_ENTROPY_WEIGHTS,
    DEFAULT_SELECTION_METRIC,
    MAXIMIZE_METRICS,
    SelectionMetric,
    allocate_phase4_workers,
    build_and_run_phase4,
)
from .progress import ConsoleProgress, format_duration
from .warehouse import WarehouseGraph


Phase5Method = Literal[
    "baseline",
    "random",
    "equal",
    "volume_proportional",
    "entropy_based",
]

PHASE5_METHODS: tuple[Phase5Method, ...] = (
    "baseline",
    "random",
    "equal",
    "volume_proportional",
    "entropy_based",
)

DEFAULT_VALIDATION_DAYS = 12
DEFAULT_MIN_LISTS_PER_DATE = 20

COMPARISON_METRICS: tuple[str, ...] = (
    "mean_flow_time_seconds",
    "makespan_seconds",
    "congestion_wait_seconds",
    "congestion_conflicts",
    "total_distance_m",
    "mean_release_delay_seconds",
    "mean_spatial_entropy_normalized",
)

AGGREGATE_METRICS: tuple[str, ...] = (
    "mean_flow_time_seconds",
    "makespan_seconds",
    "congestion_wait_seconds",
    "congestion_conflicts",
    "total_distance_m",
    "mean_release_delay_seconds",
    "mean_spatial_entropy_normalized",
    "worker_allocation_entropy_normalized",
    "demand_worker_l1_gap",
)


@dataclass(frozen=True)
class Phase5DateProfile:
    selected_date: str
    picking_lists: int
    pick_tasks: int
    picked_units: float
    observed_workers: int
    active_zones: int
    demand_task_entropy_normalized: float
    demand_unit_entropy_normalized: float


@dataclass(frozen=True)
class Phase5DateResult:
    selected_date: date
    picking_lists: tuple[PickingList, ...]
    assignments: tuple[PickingListZoneAssignment, ...]
    workloads: tuple[float, ...]
    demand_entropy: DemandEntropyMetrics
    observed_workers: int
    effective_workers: int
    baseline: Phase3MethodResult
    methods: tuple[Phase3MethodResult, ...]
    entropy_result: Phase3MethodResult
    entropy_worker_counts: tuple[int, ...]
    entropy_reused_from: str | None


@dataclass(frozen=True)
class Phase5Run:
    dataset: DatasetBundle
    warehouse: WarehouseGraph
    audit: Phase1Audit
    zones: tuple[AisleZone, ...]
    calibration_date: date
    entropy_weight: float
    entropy_source: str
    selection_metric: str
    requested_dates: tuple[date, ...]
    results: tuple[Phase5DateResult, ...]
    skipped: tuple[dict[str, object], ...]


def available_phase5_dates(
    warehouse: WarehouseGraph,
    picking_lists: Iterable[PickingList],
) -> tuple[date, ...]:
    dates = {
        picking_list.created_at.date()
        for picking_list in fully_resolvable_lists(warehouse, tuple(picking_lists))
        if picking_list.created_at is not None
    }
    return tuple(sorted(dates))


def _date_map(
    warehouse: WarehouseGraph,
    picking_lists: Iterable[PickingList],
) -> dict[date, list[PickingList]]:
    grouped: defaultdict[date, list[PickingList]] = defaultdict(list)
    for picking_list in fully_resolvable_lists(warehouse, tuple(picking_lists)):
        if picking_list.created_at is None:
            continue
        grouped[picking_list.created_at.date()].append(picking_list)
    for items in grouped.values():
        items.sort(key=lambda item: (item.created_at, item.wave_number, item.operator))
    return dict(grouped)


def _evenly_spaced_dates(dates: Iterable[date], count: int) -> tuple[date, ...]:
    values = tuple(sorted(dict.fromkeys(dates)))
    if count <= 0:
        raise ValueError("validation_days는 1 이상이어야 합니다.")
    if len(values) <= count:
        return values
    if count == 1:
        return (values[len(values) // 2],)

    indexes = [round(index * (len(values) - 1) / (count - 1)) for index in range(count)]
    selected: list[date] = []
    for index in indexes:
        value = values[index]
        if value not in selected:
            selected.append(value)
    return tuple(selected)


def select_validation_dates(
    available_dates: Iterable[date],
    *,
    calibration_date: date,
    explicit_dates: Iterable[date] | None = None,
    validation_days: int = DEFAULT_VALIDATION_DAYS,
    all_dates: bool = False,
) -> tuple[date, ...]:
    available = tuple(sorted(dict.fromkeys(available_dates)))
    available_set = set(available)
    if calibration_date not in available_set:
        raise ValueError(
            f"calibration date {calibration_date.isoformat()}에 fully-valid list가 없습니다."
        )

    candidates = tuple(value for value in available if value != calibration_date)
    if explicit_dates is not None:
        requested = tuple(sorted(dict.fromkeys(explicit_dates)))
        missing = [value for value in requested if value not in available_set]
        if missing:
            raise ValueError(
                "fully-valid list가 없는 validation date가 있습니다: "
                + ", ".join(value.isoformat() for value in missing)
            )
        return tuple(value for value in requested if value != calibration_date)
    if all_dates:
        return candidates
    return _evenly_spaced_dates(candidates, validation_days)


def _parse_phase4_recommendation(path: Path) -> tuple[float, str]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    entropy_weight = float(payload["entropy_weight"])
    if not math.isfinite(entropy_weight) or entropy_weight < 0:
        raise ValueError(f"잘못된 Phase 4 entropy_weight입니다: {entropy_weight}")
    selection_metric = str(payload.get("selection_metric", DEFAULT_SELECTION_METRIC))
    return entropy_weight, selection_metric


def _metadata_calibration_date(recommendation_path: Path) -> date | None:
    metadata_path = recommendation_path.with_name("phase4_metadata.json")
    if not metadata_path.exists():
        return None
    try:
        with metadata_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return date.fromisoformat(str(payload["selected_date"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def resolve_entropy_weight(
    data_dir: str | Path,
    *,
    calibration_date: date | None,
    entropy_weight: float | None,
    recommendation_path: str | Path | None,
    recalibrate: bool,
    number_of_zones: int,
    total_workers: int | None,
    volume_basis: Literal["tasks", "units"],
    minimum_per_active_zone: int,
    entropy_weights: Iterable[float],
    selection_metric: SelectionMetric,
    seed: int,
    walking_speed_mps: float,
    pick_seconds_per_unit: float,
    edge_capacity: int,
    pick_node_capacity: int,
    sample_seconds: float,
    return_to_io: bool,
    max_lists: int | None,
) -> tuple[date | None, float, str, str]:
    if entropy_weight is not None:
        numeric = float(entropy_weight)
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError("entropy_weight는 0 이상의 유한한 수여야 합니다.")
        return calibration_date, numeric, "cli", selection_metric

    recommendation = Path(recommendation_path) if recommendation_path is not None else None
    if not recalibrate and recommendation is not None and recommendation.exists():
        numeric, stored_metric = _parse_phase4_recommendation(recommendation)
        stored_date = _metadata_calibration_date(recommendation)
        resolved_date = calibration_date or stored_date
        if calibration_date is not None and stored_date is not None and calibration_date != stored_date:
            raise ValueError(
                "Phase 4 recommendation의 calibration date가 요청값과 다릅니다. "
                f"recommendation={stored_date.isoformat()}, requested={calibration_date.isoformat()}"
            )
        return resolved_date, numeric, f"phase4_recommendation:{recommendation}", stored_metric

    (
        _,
        _,
        _,
        selected_date,
        _,
        _,
        _,
        _,
        _,
        selected,
        _,
    ) = build_and_run_phase4(
        data_dir,
        target_date=calibration_date,
        max_lists=max_lists,
        number_of_zones=number_of_zones,
        total_workers=total_workers,
        volume_basis=volume_basis,
        minimum_per_active_zone=minimum_per_active_zone,
        entropy_weights=entropy_weights,
        selection_metric=selection_metric,
        seed=seed,
        walking_speed_mps=walking_speed_mps,
        pick_seconds_per_unit=pick_seconds_per_unit,
        edge_capacity=edge_capacity,
        pick_node_capacity=pick_node_capacity,
        sample_seconds=sample_seconds,
        return_to_io=return_to_io,
        progress=None,
    )
    return selected_date, selected.candidate.entropy_weight, "phase4_recalibration", selection_metric


def _run_one_date(
    warehouse: WarehouseGraph,
    zones: tuple[AisleZone, ...],
    selected_date: date,
    selected_lists: list[PickingList],
    *,
    entropy_weight: float,
    total_workers: int | None,
    volume_basis: Literal["tasks", "units"],
    minimum_per_active_zone: int,
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
) -> Phase5DateResult:
    assignments = classify_picking_lists_by_zone(warehouse, selected_lists, zones)
    workloads = zone_workload(zones, assignments, basis=volume_basis)
    demand_entropy, _ = calculate_demand_entropy(warehouse, selected_lists)
    observed_workers = len({item.operator for item in selected_lists})
    effective_workers = observed_workers if total_workers is None else total_workers
    active_zones = sum(value > 0 for value in workloads)
    required_workers = active_zones * minimum_per_active_zone
    if effective_workers < required_workers:
        raise ValueError(
            "활성 zone 최소 작업자 수를 만족할 수 없습니다. "
            f"workers={effective_workers}, active_zones={active_zones}, minimum={minimum_per_active_zone}"
        )

    date_base = 0.08 + 0.86 * (date_index / max(1, date_count))
    date_span = 0.86 / max(1, date_count)

    def report_method(method_index: int, method: str, worker_counts: tuple[int, ...] | None = None) -> None:
        if progress is None:
            return
        allocation_text = ""
        if worker_counts is not None:
            allocation_text = ", ".join(
                f"{zone.zone_id}={count}"
                for zone, count in zip(zones, worker_counts, strict=True)
            )
        progress.report(
            date_base + date_span * (method_index / 5.0),
            f"{selected_date.isoformat()} | {method}",
            current=allocation_text or f"lists={len(selected_lists):,}, workers={observed_workers}",
        )

    report_method(0, "baseline")
    baseline = run_phase3_observed_baseline(
        warehouse,
        selected_lists,
        zones,
        assignments,
        selected_date=selected_date,
        demand_entropy=demand_entropy,
        walking_speed_mps=walking_speed_mps,
        pick_seconds_per_unit=pick_seconds_per_unit,
        edge_capacity=edge_capacity,
        pick_node_capacity=pick_node_capacity,
        sample_seconds=sample_seconds,
        return_to_io=return_to_io,
        volume_basis=volume_basis,
    )

    method_results: list[Phase3MethodResult] = []
    result_by_counts: dict[tuple[int, ...], Phase3MethodResult] = {}
    for method_index, method in enumerate(PHASE3_METHODS, start=1):
        worker_counts = allocate_phase3_workers(
            method,
            total_workers=effective_workers,
            workloads=workloads,
            seed=seed,
            minimum_per_active_zone=minimum_per_active_zone,
        )
        report_method(method_index, method, worker_counts)
        result = run_phase3_method(
            warehouse,
            selected_lists,
            zones,
            assignments,
            method=method,
            worker_counts=worker_counts,
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
        )
        method_results.append(result)
        result_by_counts.setdefault(worker_counts, result)

    entropy_counts = allocate_phase4_workers(
        total_workers=effective_workers,
        workloads=workloads,
        entropy_weight=entropy_weight,
        minimum_per_active_zone=minimum_per_active_zone,
    )
    reused = result_by_counts.get(entropy_counts)
    if reused is not None:
        entropy_result = reused
        entropy_reused_from = reused.method
        report_method(4, f"entropy λ={entropy_weight:g} (reuse {reused.method})", entropy_counts)
    else:
        entropy_reused_from = None
        report_method(4, f"entropy λ={entropy_weight:g}", entropy_counts)
        entropy_result = run_phase3_method(
            warehouse,
            selected_lists,
            zones,
            assignments,
            method=f"entropy_lambda_{entropy_weight:g}",
            worker_counts=entropy_counts,
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
        )

    if progress is not None:
        progress.report(
            date_base + date_span,
            f"Completed validation date {selected_date.isoformat()}",
            current=f"lists={len(selected_lists):,}",
        )

    return Phase5DateResult(
        selected_date=selected_date,
        picking_lists=tuple(selected_lists),
        assignments=assignments,
        workloads=workloads,
        demand_entropy=demand_entropy,
        observed_workers=observed_workers,
        effective_workers=effective_workers,
        baseline=baseline,
        methods=tuple(method_results),
        entropy_result=entropy_result,
        entropy_worker_counts=entropy_counts,
        entropy_reused_from=entropy_reused_from,
    )


def build_and_run_phase5(
    data_dir: str | Path,
    *,
    calibration_date: date | None = None,
    validation_dates: Iterable[date] | None = None,
    validation_days: int = DEFAULT_VALIDATION_DAYS,
    all_dates: bool = False,
    min_lists_per_date: int = DEFAULT_MIN_LISTS_PER_DATE,
    max_lists: int | None = None,
    number_of_zones: int = 4,
    total_workers: int | None = None,
    volume_basis: Literal["tasks", "units"] = "tasks",
    minimum_per_active_zone: int = 1,
    seed: int = 42,
    entropy_weight: float | None = None,
    recommendation_path: str | Path | None = Path("results/phase4/phase4_recommendation.json"),
    recalibrate: bool = False,
    entropy_weights: Iterable[float] = DEFAULT_ENTROPY_WEIGHTS,
    selection_metric: SelectionMetric = DEFAULT_SELECTION_METRIC,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    edge_capacity: int = 1,
    pick_node_capacity: int = 1,
    sample_seconds: float = 5.0,
    return_to_io: bool = True,
    progress: ConsoleProgress | None = None,
) -> Phase5Run:
    if min_lists_per_date <= 0:
        raise ValueError("min_lists_per_date는 1 이상이어야 합니다.")
    if max_lists is not None and max_lists <= 0:
        raise ValueError("max_lists는 1 이상이어야 합니다.")

    if progress is not None:
        progress.report(0.02, "Loading Phase 5 input data")
    dataset = load_dataset(data_dir)
    if progress is not None:
        progress.report(0.04, "Building deterministic warehouse graph")
    warehouse = WarehouseGraph.build(
        dataset.storage_locations,
        dataset.support_points,
        deterministic_order=True,
    )
    audit = audit_picking_locations(warehouse, dataset.picking_lists)
    zones = build_aisle_zones(warehouse, number_of_zones=number_of_zones)
    grouped = _date_map(warehouse, dataset.picking_lists)
    available = tuple(sorted(grouped))
    if not available:
        raise ValueError("Phase 5에서 사용할 fully-valid operating date가 없습니다.")

    resolved_calibration, selected_lambda, entropy_source, resolved_metric = resolve_entropy_weight(
        data_dir,
        calibration_date=calibration_date,
        entropy_weight=entropy_weight,
        recommendation_path=recommendation_path,
        recalibrate=recalibrate,
        number_of_zones=number_of_zones,
        total_workers=total_workers,
        volume_basis=volume_basis,
        minimum_per_active_zone=minimum_per_active_zone,
        entropy_weights=entropy_weights,
        selection_metric=selection_metric,
        seed=seed,
        walking_speed_mps=walking_speed_mps,
        pick_seconds_per_unit=pick_seconds_per_unit,
        edge_capacity=edge_capacity,
        pick_node_capacity=pick_node_capacity,
        sample_seconds=sample_seconds,
        return_to_io=return_to_io,
        max_lists=max_lists,
    )
    calibration = resolved_calibration or available[0]

    requested = select_validation_dates(
        available,
        calibration_date=calibration,
        explicit_dates=validation_dates,
        validation_days=validation_days,
        all_dates=all_dates,
    )
    if not requested:
        raise ValueError("calibration date를 제외한 validation date가 없습니다.")

    # For automatic sampling, remove dates that are too small before spreading
    # dates across the entire observation period. Explicit dates are retained and
    # reported as skipped when they fail the minimum-size rule.
    if validation_dates is None:
        eligible_for_size = [
            value for value in available
            if value != calibration and len(grouped[value]) >= min_lists_per_date
        ]
        requested = (
            tuple(eligible_for_size)
            if all_dates
            else _evenly_spaced_dates(eligible_for_size, validation_days)
        )
        if not requested:
            raise ValueError("min_lists_per_date 조건을 만족하는 validation date가 없습니다.")

    results: list[Phase5DateResult] = []
    skipped: list[dict[str, object]] = []
    for date_index, selected_date in enumerate(requested):
        selected_lists = list(grouped[selected_date])
        if max_lists is not None:
            selected_lists = selected_lists[:max_lists]
        if len(selected_lists) < min_lists_per_date:
            skipped.append(
                {
                    "selected_date": selected_date.isoformat(),
                    "reason": "too_few_lists",
                    "picking_lists": len(selected_lists),
                    "minimum_required": min_lists_per_date,
                }
            )
            continue
        try:
            results.append(
                _run_one_date(
                    warehouse,
                    zones,
                    selected_date,
                    selected_lists,
                    entropy_weight=selected_lambda,
                    total_workers=total_workers,
                    volume_basis=volume_basis,
                    minimum_per_active_zone=minimum_per_active_zone,
                    seed=seed,
                    walking_speed_mps=walking_speed_mps,
                    pick_seconds_per_unit=pick_seconds_per_unit,
                    edge_capacity=edge_capacity,
                    pick_node_capacity=pick_node_capacity,
                    sample_seconds=sample_seconds,
                    return_to_io=return_to_io,
                    progress=progress,
                    date_index=date_index,
                    date_count=len(requested),
                )
            )
        except ValueError as exc:
            skipped.append(
                {
                    "selected_date": selected_date.isoformat(),
                    "reason": "simulation_ineligible",
                    "detail": str(exc),
                    "picking_lists": len(selected_lists),
                }
            )

    if not results:
        raise ValueError("Phase 5 validation을 완료한 날짜가 없습니다. skipped 결과를 확인하십시오.")

    return Phase5Run(
        dataset=dataset,
        warehouse=warehouse,
        audit=audit,
        zones=zones,
        calibration_date=calibration,
        entropy_weight=selected_lambda,
        entropy_source=entropy_source,
        selection_metric=resolved_metric,
        requested_dates=tuple(requested),
        results=tuple(results),
        skipped=tuple(skipped),
    )


def _method_entries(result: Phase5DateResult) -> list[tuple[str, Phase3MethodResult, tuple[int, ...], str | None]]:
    rows: list[tuple[str, Phase3MethodResult, tuple[int, ...], str | None]] = [
        ("baseline", result.baseline, (), None)
    ]
    rows.extend((item.method, item, item.worker_counts, None) for item in result.methods)
    rows.append(
        (
            "entropy_based",
            result.entropy_result,
            result.entropy_worker_counts,
            result.entropy_reused_from,
        )
    )
    return rows


def phase5_daily_records(run: Phase5Run) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for result in run.results:
        for method, simulation, worker_counts, reused_from in _method_entries(result):
            record: dict[str, object] = {
                "selected_date": result.selected_date.isoformat(),
                "method": method,
                "entropy_weight": run.entropy_weight if method == "entropy_based" else None,
                "worker_counts": "|".join(str(value) for value in worker_counts),
                "entropy_reused_from": reused_from if method == "entropy_based" else None,
                "observed_workers": result.observed_workers,
                "effective_workers": result.effective_workers,
            }
            summary = asdict(simulation.summary)
            summary.pop("method", None)
            summary.pop("selected_date", None)
            record.update(summary)
            records.append(record)
    return records


def phase5_date_profiles(run: Phase5Run) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for result in run.results:
        records.append(
            asdict(
                Phase5DateProfile(
                    selected_date=result.selected_date.isoformat(),
                    picking_lists=len(result.picking_lists),
                    pick_tasks=sum(len(item.picks) for item in result.picking_lists),
                    picked_units=sum(
                        task.quantity_units
                        for item in result.picking_lists
                        for task in item.picks
                    ),
                    observed_workers=result.observed_workers,
                    active_zones=sum(value > 0 for value in result.workloads),
                    demand_task_entropy_normalized=result.demand_entropy.task_entropy_normalized,
                    demand_unit_entropy_normalized=result.demand_entropy.unit_entropy_normalized,
                )
            )
        )
    return records


def phase5_allocation_records(run: Phase5Run) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for result in run.results:
        total_workload = sum(result.workloads)
        method_counts: list[tuple[str, tuple[int, ...]]] = [
            (item.method, item.worker_counts) for item in result.methods
        ]
        method_counts.append(("entropy_based", result.entropy_worker_counts))
        for method, worker_counts in method_counts:
            total_workers = sum(worker_counts)
            for zone, workload, workers in zip(
                run.zones, result.workloads, worker_counts, strict=True
            ):
                records.append(
                    {
                        "selected_date": result.selected_date.isoformat(),
                        "method": method,
                        "entropy_weight": run.entropy_weight if method == "entropy_based" else None,
                        "zone_id": zone.zone_id,
                        "workload": workload,
                        "workload_share": 0.0 if total_workload == 0 else workload / total_workload,
                        "workers": workers,
                        "worker_share": 0.0 if total_workers == 0 else workers / total_workers,
                    }
                )
    return records


def aggregate_method_records(daily: pd.DataFrame) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for method in PHASE5_METHODS:
        subset = daily[daily["method"] == method]
        if subset.empty:
            continue
        for metric in AGGREGATE_METRICS:
            values = pd.to_numeric(subset[metric], errors="coerce").dropna()
            if values.empty:
                continue
            records.append(
                {
                    "method": method,
                    "metric": metric,
                    "n_days": int(values.size),
                    "mean": float(values.mean()),
                    "std": float(values.std(ddof=1)) if values.size > 1 else 0.0,
                    "median": float(values.median()),
                    "min": float(values.min()),
                    "max": float(values.max()),
                }
            )
    return records


def _paired_wilcoxon(entropy_values: list[float], comparator_values: list[float]) -> tuple[float, float]:
    differences = [left - right for left, right in zip(entropy_values, comparator_values, strict=True)]
    nonzero = [value for value in differences if not math.isclose(value, 0.0, abs_tol=1e-12)]
    if len(nonzero) < 2:
        return 0.0, 1.0
    try:
        result = wilcoxon(entropy_values, comparator_values, alternative="two-sided", zero_method="wilcox")
        return float(result.statistic), float(result.pvalue)
    except ValueError:
        return 0.0, 1.0


def paired_comparison_records(daily: pd.DataFrame) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    entropy = daily[daily["method"] == "entropy_based"].set_index("selected_date")
    for comparator in ("baseline", "random", "equal", "volume_proportional"):
        other = daily[daily["method"] == comparator].set_index("selected_date")
        common_dates = entropy.index.intersection(other.index)
        for metric in COMPARISON_METRICS:
            entropy_values = [float(entropy.loc[value, metric]) for value in common_dates]
            comparator_values = [float(other.loc[value, metric]) for value in common_dates]
            if not entropy_values:
                continue
            maximize = metric in MAXIMIZE_METRICS
            wins = ties = losses = 0
            improvements: list[float] = []
            for entropy_value, comparator_value in zip(
                entropy_values, comparator_values, strict=True
            ):
                difference = entropy_value - comparator_value
                if math.isclose(difference, 0.0, rel_tol=1e-12, abs_tol=1e-9):
                    ties += 1
                elif (difference > 0) if maximize else (difference < 0):
                    wins += 1
                else:
                    losses += 1

                if math.isclose(comparator_value, 0.0, abs_tol=1e-12):
                    if math.isclose(entropy_value, 0.0, abs_tol=1e-12):
                        improvements.append(0.0)
                    continue
                if maximize:
                    improvements.append(100.0 * (entropy_value - comparator_value) / abs(comparator_value))
                else:
                    improvements.append(100.0 * (comparator_value - entropy_value) / abs(comparator_value))

            statistic, p_value = _paired_wilcoxon(entropy_values, comparator_values)
            n = len(entropy_values)
            records.append(
                {
                    "comparator": comparator,
                    "metric": metric,
                    "direction": "maximize" if maximize else "minimize",
                    "n_days": n,
                    "entropy_mean": mean(entropy_values),
                    "comparator_mean": mean(comparator_values),
                    "mean_delta_entropy_minus_comparator": mean(
                        left - right for left, right in zip(entropy_values, comparator_values, strict=True)
                    ),
                    "mean_improvement_pct": mean(improvements) if improvements else float("nan"),
                    "wins": wins,
                    "ties": ties,
                    "losses": losses,
                    "win_rate_excluding_ties": 0.0 if wins + losses == 0 else wins / (wins + losses),
                    "wilcoxon_statistic": statistic,
                    "wilcoxon_p_value_two_sided": p_value,
                    "significant_at_0_05": bool(p_value < 0.05),
                }
            )
    return records


def write_phase5_results(output_dir: str | Path, run: Phase5Run, *, parameters: dict[str, object]) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    daily = pd.DataFrame(phase5_daily_records(run))
    daily.to_csv(output_dir / "phase5_daily_summary.csv", index=False)
    pd.DataFrame(phase5_date_profiles(run)).to_csv(
        output_dir / "phase5_date_profiles.csv", index=False
    )
    pd.DataFrame(phase5_allocation_records(run)).to_csv(
        output_dir / "phase5_allocations.csv", index=False
    )
    pd.DataFrame(aggregate_method_records(daily)).to_csv(
        output_dir / "phase5_method_summary.csv", index=False
    )
    paired = pd.DataFrame(paired_comparison_records(daily))
    paired.to_csv(output_dir / "phase5_paired_comparison.csv", index=False)

    primary = paired[paired["metric"] == run.selection_metric].copy()
    primary.to_csv(output_dir / "phase5_primary_comparison.csv", index=False)

    skipped_columns = [
        "selected_date",
        "reason",
        "detail",
        "picking_lists",
        "minimum_required",
    ]
    pd.DataFrame(list(run.skipped), columns=skipped_columns).to_csv(
        output_dir / "phase5_skipped_dates.csv", index=False
    )

    metadata = {
        "phase": 5,
        "purpose": "multi-date out-of-sample validation of Phase 3 baselines and the Phase 4 selected entropy allocation",
        "calibration_date": run.calibration_date.isoformat(),
        "validation_dates_requested": [value.isoformat() for value in run.requested_dates],
        "validation_dates_completed": [value.selected_date.isoformat() for value in run.results],
        "validation_dates_skipped": list(run.skipped),
        "entropy": {
            "weight": run.entropy_weight,
            "source": run.entropy_source,
            "selection_metric": run.selection_metric,
            "fixed_across_validation_dates": True,
            "interpretation": (
                "lambda is calibrated once and held fixed across validation dates; "
                "zone worker counts are recalculated from each date's workload distribution"
            ),
        },
        "input": {
            "storage_locations": len(run.dataset.storage_locations),
            "support_points": len(run.dataset.support_points),
            "picking_lists_total": len(run.dataset.picking_lists),
            "fully_resolvable_lists_total": run.audit.fully_resolvable_lists,
        },
        "parameters": parameters,
        "methods": list(PHASE5_METHODS),
        "statistical_validation": {
            "paired_test": "Wilcoxon signed-rank, two-sided",
            "alpha": 0.05,
            "pairing_unit": "operating date",
            "primary_metric": run.selection_metric,
        },
        "important_note": (
            "If selected lambda is 0, entropy_based is mathematically equivalent to "
            "volume_proportional under the same workload and integer allocation rules. "
            "Phase 5 reports this as a valid tie rather than forcing an entropy advantage."
        ),
    }
    with (output_dir / "phase5_metadata.json").open("w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def _parse_dates(value: str) -> tuple[date, ...]:
    try:
        result = tuple(
            date.fromisoformat(part.strip())
            for part in value.split(",")
            if part.strip()
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--dates는 YYYY-MM-DD,YYYY-MM-DD 형식이어야 합니다."
        ) from exc
    if not result:
        raise argparse.ArgumentTypeError("--dates에는 하나 이상의 날짜가 필요합니다.")
    return result


def _parse_entropy_weights(value: str) -> tuple[float, ...]:
    try:
        values = tuple(float(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("entropy weight는 숫자여야 합니다.") from exc
    if not values or any(not math.isfinite(item) or item < 0 for item in values):
        raise argparse.ArgumentTypeError("entropy weight는 0 이상의 유한한 수여야 합니다.")
    return tuple(sorted(dict.fromkeys(values)))


def _fmt_metric(value: object) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{float(value):,.3f}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Entropy Thesis - Phase 5 multi-date validation"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--calibration-date", type=_parse_date, default=None)
    parser.add_argument(
        "--dates",
        type=_parse_dates,
        default=None,
        help="explicit validation dates: YYYY-MM-DD,YYYY-MM-DD,...",
    )
    parser.add_argument("--validation-days", type=int, default=DEFAULT_VALIDATION_DAYS)
    parser.add_argument("--all-dates", action="store_true")
    parser.add_argument("--min-lists", type=int, default=DEFAULT_MIN_LISTS_PER_DATE)
    parser.add_argument("--max-lists", type=int, default=None)
    parser.add_argument("--zones", type=int, default=4)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--volume-basis", choices=("tasks", "units"), default="tasks")
    parser.add_argument("--minimum-per-active-zone", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--entropy-weight", type=float, default=None)
    parser.add_argument(
        "--recommendation",
        type=Path,
        default=Path("results/phase4/phase4_recommendation.json"),
        help="Phase 4 recommendation JSON used unless --entropy-weight or --recalibrate is given",
    )
    parser.add_argument("--recalibrate", action="store_true")
    parser.add_argument(
        "--entropy-weights",
        type=_parse_entropy_weights,
        default=DEFAULT_ENTROPY_WEIGHTS,
    )
    parser.add_argument(
        "--selection-metric",
        choices=tuple(COMPARISON_METRICS),
        default=DEFAULT_SELECTION_METRIC,
    )
    parser.add_argument("--speed", type=float, default=1.2)
    parser.add_argument("--pick-seconds", type=float, default=3.0)
    parser.add_argument("--edge-capacity", type=int, default=1)
    parser.add_argument("--pick-node-capacity", type=int, default=1)
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--no-return-to-io", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("results/phase5"))
    args = parser.parse_args()

    progress = ConsoleProgress()
    progress.start("Phase 5 multi-date experiment and validation")
    run = build_and_run_phase5(
        args.data_dir,
        calibration_date=args.calibration_date,
        validation_dates=args.dates,
        validation_days=args.validation_days,
        all_dates=args.all_dates,
        min_lists_per_date=args.min_lists,
        max_lists=args.max_lists,
        number_of_zones=args.zones,
        total_workers=args.workers,
        volume_basis=args.volume_basis,
        minimum_per_active_zone=args.minimum_per_active_zone,
        seed=args.seed,
        entropy_weight=args.entropy_weight,
        recommendation_path=args.recommendation,
        recalibrate=args.recalibrate,
        entropy_weights=args.entropy_weights,
        selection_metric=args.selection_metric,
        walking_speed_mps=args.speed,
        pick_seconds_per_unit=args.pick_seconds,
        edge_capacity=args.edge_capacity,
        pick_node_capacity=args.pick_node_capacity,
        sample_seconds=args.sample_seconds,
        return_to_io=not args.no_return_to_io,
        progress=progress,
    )

    parameters = {
        "validation_days": args.validation_days,
        "all_dates": args.all_dates,
        "explicit_dates": [value.isoformat() for value in args.dates] if args.dates else None,
        "min_lists_per_date": args.min_lists,
        "max_lists": args.max_lists,
        "zones": args.zones,
        "workers": args.workers,
        "volume_basis": args.volume_basis,
        "minimum_per_active_zone": args.minimum_per_active_zone,
        "seed": args.seed,
        "walking_speed_mps": args.speed,
        "pick_seconds_per_unit": args.pick_seconds,
        "edge_capacity": args.edge_capacity,
        "pick_node_capacity": args.pick_node_capacity,
        "sample_seconds": args.sample_seconds,
        "return_to_io": not args.no_return_to_io,
    }
    progress.report(0.96, "Writing Phase 5 result files", current=str(args.output_dir))
    write_phase5_results(args.output_dir, run, parameters=parameters)
    progress.complete("Phase 5 processing completed")

    daily = pd.DataFrame(phase5_daily_records(run))
    paired = pd.DataFrame(paired_comparison_records(daily))
    primary = paired[paired["metric"] == run.selection_metric]

    print()
    print("=== Phase 5 Multi-Date Validation ===")
    print(f"Calibration date     : {run.calibration_date.isoformat()}")
    print(f"Fixed entropy lambda : {run.entropy_weight:g}")
    print(f"Lambda source        : {run.entropy_source}")
    print(f"Primary metric       : {run.selection_metric}")
    print(f"Validation dates     : {len(run.results):,} completed / {len(run.requested_dates):,} requested")
    print(f"Skipped dates        : {len(run.skipped):,}")
    print()
    print("=== Primary KPI: Entropy vs Comparators ===")
    print("Comparator            Entropy mean   Comparator mean   Improve(%)   W/T/L   p-value")
    for row in primary.to_dict("records"):
        print(
            f"{str(row['comparator']):<20} "
            f"{_fmt_metric(row['entropy_mean']):>12}   "
            f"{_fmt_metric(row['comparator_mean']):>15}   "
            f"{_fmt_metric(row['mean_improvement_pct']):>10}   "
            f"{int(row['wins'])}/{int(row['ties'])}/{int(row['losses'])}   "
            f"{float(row['wilcoxon_p_value_two_sided']):.4f}"
        )
    if math.isclose(run.entropy_weight, 0.0, abs_tol=1e-12):
        print()
        print(
            "NOTE: selected lambda=0 -> entropy_based is equivalent to "
            "volume_proportional under the Phase 4 formulation."
        )
    print()
    print(f"Results              : {args.output_dir}")
    print(
        f"Total execution time : {format_duration(progress.elapsed_seconds)} "
        f"({progress.elapsed_seconds:,.2f} s)"
    )


if __name__ == "__main__":
    main()
