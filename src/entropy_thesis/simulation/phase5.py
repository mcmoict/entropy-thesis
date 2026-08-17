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
    DEFAULT_SELECTION_METRIC,
    MAXIMIZE_METRICS,
    allocate_phase4_workers,
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

DEFAULT_VALIDATION_DAYS = 12  # legacy helper/CLI compatibility only
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
class Phase4HoldoutSpec:
    entropy_weight: float
    selection_metric: str
    calibration_dates: tuple[date, ...]
    holdout_dates: tuple[date, ...]
    source_path: Path


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
    calibration_dates: tuple[date, ...]
    holdout_dates: tuple[date, ...]
    entropy_weight: float
    entropy_source: str
    selection_metric: str
    requested_dates: tuple[date, ...]
    results: tuple[Phase5DateResult, ...]
    skipped: tuple[dict[str, object], ...]

    @property
    def calibration_date(self) -> date:
        """Backward-compatible representative date; Phase 5 uses calibration_dates."""
        return self.calibration_dates[0]


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


def _parse_iso_date_list(payload: object, *, field: str) -> tuple[date, ...]:
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"Phase 4 recommendation에 {field}가 비어 있거나 없습니다.")
    try:
        values = tuple(date.fromisoformat(str(value)) for value in payload)
    except ValueError as exc:
        raise ValueError(f"Phase 4 recommendation의 {field} 날짜 형식이 잘못되었습니다.") from exc
    if len(values) != len(set(values)):
        raise ValueError(f"Phase 4 recommendation의 {field}에 중복 날짜가 있습니다.")
    return tuple(sorted(values))


def load_phase4_holdout_spec(path: str | Path) -> Phase4HoldoutSpec:
    """Load the immutable Phase 4E lambda and date split used by Phase 5.

    Phase 5 must not resample dates or recalibrate lambda. The recommendation
    file is therefore the single source of truth for both λ* and the holdout set.
    """

    recommendation = Path(path)
    if not recommendation.exists():
        raise FileNotFoundError(
            "Phase 5 requires the Phase 4 recommendation file. "
            f"Run Phase 4 first or provide --recommendation: {recommendation}"
        )
    with recommendation.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if str(payload.get("phase", "")) != "4E":
        raise ValueError(
            "Phase 5에는 최신 Phase 4A~4E recommendation이 필요합니다 "
            f"(phase={payload.get('phase')!r})."
        )

    entropy_weight = float(payload["entropy_weight"])
    if not math.isfinite(entropy_weight) or entropy_weight < 0:
        raise ValueError(f"잘못된 Phase 4 entropy_weight입니다: {entropy_weight}")

    selection_metric = str(payload.get("selection_metric", DEFAULT_SELECTION_METRIC))
    if selection_metric not in COMPARISON_METRICS:
        raise ValueError(f"지원하지 않는 Phase 4 selection_metric입니다: {selection_metric}")

    calibration_dates = _parse_iso_date_list(payload.get("calibration_dates"), field="calibration_dates")
    holdout_dates = _parse_iso_date_list(payload.get("holdout_dates"), field="holdout_dates")
    overlap = set(calibration_dates).intersection(holdout_dates)
    if overlap:
        raise ValueError(
            "Phase 4 recommendation의 Calibration/Holdout 날짜가 겹칩니다: "
            + ", ".join(value.isoformat() for value in sorted(overlap))
        )

    return Phase4HoldoutSpec(
        entropy_weight=entropy_weight,
        selection_metric=selection_metric,
        calibration_dates=calibration_dates,
        holdout_dates=holdout_dates,
        source_path=recommendation,
    )


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
    validation_dates: Iterable[date] | None = None,
    max_lists: int | None = None,
    number_of_zones: int = 4,
    total_workers: int | None = None,
    volume_basis: Literal["tasks", "units"] = "tasks",
    minimum_per_active_zone: int = 1,
    seed: int = 42,
    recommendation_path: str | Path = Path("results/phase4/phase4_recommendation.json"),
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    edge_capacity: int = 1,
    pick_node_capacity: int = 1,
    sample_seconds: float = 5.0,
    return_to_io: bool = True,
    progress: ConsoleProgress | None = None,
) -> Phase5Run:
    """Run the frozen Phase 4 holdout set using five allocation methods.

    The Phase 4 recommendation is authoritative for λ*, calibration dates, and
    holdout dates. Optional validation_dates can only select a subset of the
    frozen holdout set (useful for smoke tests); it cannot introduce new dates.
    """

    if max_lists is not None and max_lists <= 0:
        raise ValueError("max_lists는 1 이상이어야 합니다.")

    spec = load_phase4_holdout_spec(recommendation_path)
    if progress is not None:
        progress.report(
            0.02,
            "Loading Phase 4E frozen holdout specification",
            current=(
                f"lambda={spec.entropy_weight:g}, "
                f"calibration={len(spec.calibration_dates):,}, holdout={len(spec.holdout_dates):,}"
            ),
        )
        progress.report(0.04, "Loading Phase 5 input data")
    dataset = load_dataset(data_dir)
    if progress is not None:
        progress.report(0.06, "Building deterministic warehouse graph")
    warehouse = WarehouseGraph.build(
        dataset.storage_locations,
        dataset.support_points,
        deterministic_order=True,
    )
    audit = audit_picking_locations(warehouse, dataset.picking_lists)
    if progress is not None:
        progress.report(0.08, "Building physical aisle zones")
    zones = build_aisle_zones(warehouse, number_of_zones=number_of_zones)
    grouped = _date_map(warehouse, dataset.picking_lists)
    available = set(grouped)

    missing_holdout = [value for value in spec.holdout_dates if value not in available]
    if missing_holdout:
        raise ValueError(
            "Phase 4에서 고정한 Holdout 날짜가 현재 데이터에 없습니다. "
            "데이터셋이 Phase 4 이후 변경되었는지 확인하십시오: "
            + ", ".join(value.isoformat() for value in missing_holdout)
        )

    if validation_dates is None:
        requested = spec.holdout_dates
    else:
        requested = tuple(sorted(dict.fromkeys(validation_dates)))
        outside = [value for value in requested if value not in set(spec.holdout_dates)]
        if outside:
            raise ValueError(
                "--dates에는 Phase 4에서 고정된 Holdout 날짜만 지정할 수 있습니다: "
                + ", ".join(value.isoformat() for value in outside)
            )
        if not requested:
            raise ValueError("Phase 5 Holdout 날짜가 비어 있습니다.")

    results: list[Phase5DateResult] = []
    skipped: list[dict[str, object]] = []
    for date_index, selected_date in enumerate(requested):
        selected_lists = list(grouped[selected_date])
        if max_lists is not None:
            selected_lists = selected_lists[:max_lists]
        if not selected_lists:
            skipped.append(
                {
                    "selected_date": selected_date.isoformat(),
                    "reason": "no_lists_after_limit",
                    "picking_lists": 0,
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
                    entropy_weight=spec.entropy_weight,
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
        raise ValueError("Phase 5 Holdout 검증을 완료한 날짜가 없습니다. skipped 결과를 확인하십시오.")

    return Phase5Run(
        dataset=dataset,
        warehouse=warehouse,
        audit=audit,
        zones=zones,
        calibration_dates=spec.calibration_dates,
        holdout_dates=spec.holdout_dates,
        entropy_weight=spec.entropy_weight,
        entropy_source=f"phase4_recommendation:{spec.source_path}",
        selection_metric=spec.selection_metric,
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


def phase5_allocation_equivalence_records(run: Phase5Run) -> list[dict[str, object]]:
    """Record whether Entropy(λ*) collapses to another integer allocation."""

    records: list[dict[str, object]] = []
    for result in run.results:
        counts_by_method = {item.method: item.worker_counts for item in result.methods}
        entropy_counts = result.entropy_worker_counts
        records.append(
            {
                "selected_date": result.selected_date.isoformat(),
                "entropy_weight": run.entropy_weight,
                "entropy_worker_counts": "|".join(str(v) for v in entropy_counts),
                "random_worker_counts": "|".join(str(v) for v in counts_by_method["random"]),
                "equal_worker_counts": "|".join(str(v) for v in counts_by_method["equal"]),
                "volume_worker_counts": "|".join(str(v) for v in counts_by_method["volume_proportional"]),
                "same_as_random": entropy_counts == counts_by_method["random"],
                "same_as_equal": entropy_counts == counts_by_method["equal"],
                "same_as_volume": entropy_counts == counts_by_method["volume_proportional"],
                "des_reused_from": result.entropy_reused_from,
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
    pd.DataFrame(phase5_allocation_equivalence_records(run)).to_csv(
        output_dir / "phase5_allocation_equivalence.csv", index=False
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
        "purpose": "out-of-sample holdout validation of Baseline/Random/Equal/Volume/Entropy(lambda*)",
        "calibration_dates": [value.isoformat() for value in run.calibration_dates],
        "holdout_dates_frozen": [value.isoformat() for value in run.holdout_dates],
        "holdout_dates_requested": [value.isoformat() for value in run.requested_dates],
        "holdout_dates_completed": [value.selected_date.isoformat() for value in run.results],
        "holdout_dates_skipped": list(run.skipped),
        "entropy": {
            "weight": run.entropy_weight,
            "source": run.entropy_source,
            "selection_metric": run.selection_metric,
            "fixed_across_holdout_dates": True,
            "interpretation": (
                "lambda is selected only from Phase 4 calibration dates and held fixed across "
                "the untouched Phase 4 holdout dates; zone worker counts are recalculated from "
                "each holdout date's workload distribution"
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
        "holdout_integrity": (
            "Phase 5 does not recalibrate lambda, resplit dates, or substitute non-holdout dates. "
            "A CLI --dates subset is allowed only for smoke tests and must remain inside the frozen holdout set."
        ),
        "allocation_equivalence_note": (
            "Because worker counts are integers, Entropy(lambda*) may produce the same allocation as "
            "Volume/Equal/Random on some dates. phase5_allocation_equivalence.csv records this explicitly."
        ),
    }
    with (output_dir / "phase5_metadata.json").open("w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)


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


def _fmt_metric(value: object) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{float(value):,.3f}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Entropy Thesis - Phase 5 frozen holdout validation: "
            "Baseline / Random / Equal / Volume / Entropy(lambda*)"
        )
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--dates",
        type=_parse_dates,
        default=None,
        help=(
            "optional smoke-test subset; every date must belong to the Phase 4 frozen holdout set "
            "(YYYY-MM-DD,YYYY-MM-DD,...)"
        ),
    )
    parser.add_argument("--max-lists", type=int, default=None)
    parser.add_argument("--zones", type=int, default=4)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--volume-basis", choices=("tasks", "units"), default="tasks")
    parser.add_argument("--minimum-per-active-zone", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--recommendation",
        type=Path,
        default=Path("results/phase4/phase4_recommendation.json"),
        help="Phase 4E recommendation JSON containing lambda*, calibration dates, and frozen holdout dates",
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
    progress.start("Phase 5 frozen holdout validation")
    run = build_and_run_phase5(
        args.data_dir,
        validation_dates=args.dates,
        max_lists=args.max_lists,
        number_of_zones=args.zones,
        total_workers=args.workers,
        volume_basis=args.volume_basis,
        minimum_per_active_zone=args.minimum_per_active_zone,
        seed=args.seed,
        recommendation_path=args.recommendation,
        walking_speed_mps=args.speed,
        pick_seconds_per_unit=args.pick_seconds,
        edge_capacity=args.edge_capacity,
        pick_node_capacity=args.pick_node_capacity,
        sample_seconds=args.sample_seconds,
        return_to_io=not args.no_return_to_io,
        progress=progress,
    )

    parameters = {
        "holdout_source": str(args.recommendation),
        "explicit_holdout_subset": [value.isoformat() for value in args.dates] if args.dates else None,
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
    method_summary = pd.DataFrame(aggregate_method_records(daily))
    paired = pd.DataFrame(paired_comparison_records(daily))
    primary = paired[paired["metric"] == run.selection_metric]
    primary_methods = method_summary[method_summary["metric"] == run.selection_metric]
    equivalence = phase5_allocation_equivalence_records(run)
    same_as_volume = sum(bool(row["same_as_volume"]) for row in equivalence)

    print()
    print("=== Phase 5 | Frozen Holdout Validation ===")
    print(f"Phase 4 calibration   : {len(run.calibration_dates):,} dates ({run.calibration_dates[0]} .. {run.calibration_dates[-1]})")
    print(f"Frozen holdout        : {len(run.holdout_dates):,} dates ({run.holdout_dates[0]} .. {run.holdout_dates[-1]})")
    print(f"Holdout executed      : {len(run.results):,} completed / {len(run.requested_dates):,} requested")
    print(f"Skipped dates         : {len(run.skipped):,}")
    print(f"Fixed entropy lambda  : {run.entropy_weight:g}")
    print(f"Lambda source         : {run.entropy_source}")
    print(f"Primary metric        : {run.selection_metric}")
    print("Lambda recalibration  : no")
    print("Holdout resampling    : no")
    print()
    print("=== Primary KPI | Method Statistics ===")
    print("Method                  Mean              Std")
    for row in primary_methods.to_dict("records"):
        print(
            f"{str(row['method']):<22} "
            f"{_fmt_metric(row['mean']):>12}   "
            f"{_fmt_metric(row['std']):>14}"
        )
    print()
    print("=== Primary KPI | Entropy vs Comparators ===")
    print("Comparator            Entropy mean   Comparator mean   Improve(%)   W/T/L   p-value")
    for row in primary.to_dict("records"):
        print(
            f"{str(row['comparator']):<20} "
            f"{_fmt_metric(row['entropy_mean']):>12}   "
            f"{_fmt_metric(row['comparator_mean']):>15}   "
            f"{_fmt_metric(row['mean_improvement_pct']):>10}   "
            f"{int(row['wins'])}/{int(row['ties'])}/{int(row['losses'])}   "
            f"{float(row['wilcoxon_p_value_two_sided']):.6g}"
        )
    print()
    print("=== Integer Allocation Equivalence ===")
    print(
        f"Entropy allocation == Volume allocation : "
        f"{same_as_volume:,}/{len(equivalence):,} holdout dates"
    )
    print()
    print(f"Results               : {args.output_dir}")
    print(
        f"Total execution time  : {format_duration(progress.elapsed_seconds)} "
        f"({progress.elapsed_seconds:,.2f} s)"
    )


if __name__ == "__main__":
    main()
