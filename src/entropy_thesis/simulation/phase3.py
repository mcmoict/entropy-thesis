from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path
from statistics import mean
from typing import Callable, Iterable, Literal

import pandas as pd
import simpy

from ..allocation import allocate_workers, normalize_strategy_name
from ..entropy import normalized_shannon_entropy
from .data_loader import DatasetBundle, PickingList, load_dataset
from .phase1 import Phase1Audit, audit_picking_locations
from .phase2 import (
    DemandEntropyMetrics,
    PickingListExecution,
    Phase2Summary,
    calculate_demand_entropy,
    select_phase2_lists,
    summarize_phase2,
)
from .progress import ConsoleProgress, format_duration
from .spatial_metrics import (
    CellOccupancyMetrics,
    SpatialEntropySample,
    aggregate_cell_occupancy,
    sample_spatial_entropy,
)
from .traffic import CongestionWaitEvent, TrafficController
from .warehouse import WarehouseGraph
from .worker import Worker


Phase3Method = Literal["random", "equal", "volume_proportional"]
PHASE3_METHODS: tuple[Phase3Method, ...] = (
    "random",
    "equal",
    "volume_proportional",
)


@dataclass(frozen=True)
class AisleZone:
    zone_id: str
    aisle_y_values: tuple[float, ...]
    y_min_m: float
    y_max_m: float


@dataclass(frozen=True)
class PickingListZoneAssignment:
    list_index: int
    wave_number: str
    original_operator: str
    zone_id: str
    pick_tasks: int
    pick_units: float
    physical_zone_count: int
    dominant_zone_tasks: int
    dominant_zone_units: float


@dataclass(frozen=True)
class Phase3ListExecution:
    method: str
    wave_number: str
    original_operator: str
    assigned_zone: str
    assigned_worker: str
    released_at_seconds: float
    started_at_seconds: float
    finished_at_seconds: float
    release_delay_seconds: float
    pick_tasks: int
    pick_units: float
    physical_zone_count: int


@dataclass(frozen=True)
class Phase3RunSummary:
    method: str
    seed: int
    selected_date: str
    total_workers: int
    zones: int
    active_zones: int
    worker_allocation_entropy_normalized: float
    demand_worker_l1_gap: float
    picking_lists: int
    pick_tasks: int
    picked_units: float
    total_distance_m: float
    movement_events: int
    movement_seconds: float
    congestion_conflicts: int
    edge_conflicts: int
    pick_node_conflicts: int
    congestion_wait_seconds: float
    mean_conflict_wait_seconds: float
    p95_conflict_wait_seconds: float
    max_conflict_wait_seconds: float
    congestion_delay_ratio: float
    mean_release_delay_seconds: float
    max_release_delay_seconds: float
    mean_flow_time_seconds: float
    makespan_seconds: float
    entropy_samples: int
    mean_spatial_entropy_normalized: float
    mean_spatial_entropy_multiworker: float
    min_spatial_entropy_normalized: float
    max_spatial_entropy_normalized: float
    mean_max_concentration: float
    shared_worker_ratio: float
    occupied_spatial_cells: int
    congested_cell_seconds: float
    max_cell_occupancy: int
    simulation_elapsed_seconds: float


@dataclass(frozen=True)
class Phase3MethodResult:
    method: Phase3Method
    seed: int
    worker_counts: tuple[int, ...]
    workers: dict[str, Worker]
    traffic: TrafficController
    executions: tuple[Phase3ListExecution, ...]
    entropy_samples: tuple[SpatialEntropySample, ...]
    occupancy: tuple[CellOccupancyMetrics, ...]
    phase2_summary: Phase2Summary
    summary: Phase3RunSummary


def build_aisle_zones(
    warehouse: WarehouseGraph,
    *,
    number_of_zones: int = 4,
) -> tuple[AisleZone, ...]:
    """Partition horizontal picking aisles into contiguous balanced zones.

    Phase 1 projects every storage location onto a support-point y coordinate.
    Phase 3 therefore partitions the sorted unique support y coordinates, not
    arbitrary storage prefixes.  With 17 horizontal aisles and four zones the
    groups contain 5/4/4/4 aisle levels.
    """

    if isinstance(number_of_zones, bool) or not isinstance(number_of_zones, int):
        raise TypeError("number_of_zones는 정수여야 합니다.")
    if number_of_zones <= 0:
        raise ValueError("number_of_zones는 1 이상이어야 합니다.")

    aisle_y = sorted(
        {
            round(float(attrs["y_m"]), 9)
            for _, attrs in warehouse.graph.nodes(data=True)
            if attrs.get("kind") == "support"
        }
    )
    if not aisle_y:
        raise ValueError("warehouse graph에 support aisle y 좌표가 없습니다.")
    if number_of_zones > len(aisle_y):
        raise ValueError(
            "number_of_zones는 horizontal aisle 수보다 클 수 없습니다. "
            f"zones={number_of_zones}, aisles={len(aisle_y)}"
        )

    quotient, remainder = divmod(len(aisle_y), number_of_zones)
    zones: list[AisleZone] = []
    start = 0
    for index in range(number_of_zones):
        size = quotient + (1 if index < remainder else 0)
        values = tuple(aisle_y[start : start + size])
        start += size
        zones.append(
            AisleZone(
                zone_id=f"Z{index + 1:02d}",
                aisle_y_values=values,
                y_min_m=min(values),
                y_max_m=max(values),
            )
        )
    return tuple(zones)


def _aisle_zone_lookup(zones: Iterable[AisleZone]) -> dict[float, str]:
    lookup: dict[float, str] = {}
    for zone in zones:
        for y in zone.aisle_y_values:
            if y in lookup:
                raise ValueError(f"aisle y={y}가 둘 이상의 zone에 포함됩니다.")
            lookup[y] = zone.zone_id
    return lookup


def zone_for_location(
    warehouse: WarehouseGraph,
    zones: Iterable[AisleZone],
    location_id: str,
) -> str:
    node_id = warehouse.node_for_location(location_id)
    y_value = round(float(warehouse.graph.nodes[node_id]["y_m"]), 9)
    lookup = _aisle_zone_lookup(zones)
    try:
        return lookup[y_value]
    except KeyError as exc:
        raise KeyError(
            f"location={location_id}의 aisle y={y_value}를 Phase 3 zone에 매핑할 수 없습니다."
        ) from exc


def classify_picking_lists_by_zone(
    warehouse: WarehouseGraph,
    picking_lists: Iterable[PickingList],
    zones: Iterable[AisleZone],
) -> tuple[PickingListZoneAssignment, ...]:
    """Assign each intact picking list to its dominant physical aisle zone.

    The original list is *not split*.  The zone containing the largest number
    of pick tasks is selected.  Ties are resolved by picked units and then by
    the configured zone order.  Keeping the list intact preserves the Phase 2
    route, task order and return-to-I/O semantics so allocation policy remains
    the principal experimental change.
    """

    zone_tuple = tuple(zones)
    if not zone_tuple:
        raise ValueError("zones가 비어 있습니다.")
    zone_order = {zone.zone_id: index for index, zone in enumerate(zone_tuple)}
    lookup = _aisle_zone_lookup(zone_tuple)

    assignments: list[PickingListZoneAssignment] = []
    for list_index, picking_list in enumerate(picking_lists):
        task_counts: Counter[str] = Counter()
        unit_counts: defaultdict[str, float] = defaultdict(float)
        for task in picking_list.picks:
            node_id = warehouse.node_for_location(task.location_id)
            y_value = round(float(warehouse.graph.nodes[node_id]["y_m"]), 9)
            try:
                zone_id = lookup[y_value]
            except KeyError as exc:
                raise KeyError(
                    f"location={task.location_id}의 aisle y={y_value}를 zone에 매핑할 수 없습니다."
                ) from exc
            task_counts[zone_id] += 1
            unit_counts[zone_id] += float(task.quantity_units)

        if not task_counts:
            raise ValueError(
                f"빈 picking list는 Phase 3에서 분류할 수 없습니다: "
                f"wave={picking_list.wave_number}, operator={picking_list.operator}"
            )

        dominant_zone = max(
            task_counts,
            key=lambda zone_id: (
                task_counts[zone_id],
                unit_counts[zone_id],
                -zone_order[zone_id],
            ),
        )
        assignments.append(
            PickingListZoneAssignment(
                list_index=list_index,
                wave_number=picking_list.wave_number,
                original_operator=picking_list.operator,
                zone_id=dominant_zone,
                pick_tasks=len(picking_list.picks),
                pick_units=sum(float(task.quantity_units) for task in picking_list.picks),
                physical_zone_count=len(task_counts),
                dominant_zone_tasks=task_counts[dominant_zone],
                dominant_zone_units=unit_counts[dominant_zone],
            )
        )
    return tuple(assignments)


def zone_workload(
    zones: Iterable[AisleZone],
    assignments: Iterable[PickingListZoneAssignment],
    *,
    basis: Literal["tasks", "units"] = "tasks",
) -> tuple[float, ...]:
    if basis not in {"tasks", "units"}:
        raise ValueError("basis는 'tasks' 또는 'units'여야 합니다.")
    zone_ids = [zone.zone_id for zone in zones]
    workload = {zone_id: 0.0 for zone_id in zone_ids}
    for assignment in assignments:
        workload[assignment.zone_id] += (
            float(assignment.pick_tasks)
            if basis == "tasks"
            else float(assignment.pick_units)
        )
    return tuple(workload[zone_id] for zone_id in zone_ids)


def allocate_phase3_workers(
    method: str,
    *,
    total_workers: int,
    workloads: Iterable[float],
    seed: int = 42,
    minimum_per_active_zone: int = 1,
) -> tuple[int, ...]:
    """Allocate workers only among zones that actually own Phase 3 work."""

    canonical = normalize_strategy_name(method)
    if canonical not in PHASE3_METHODS:
        raise ValueError(
            "Phase 3는 기존 baseline만 비교합니다. "
            "사용 가능: random, equal, volume_proportional"
        )
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

    values = tuple(float(value) for value in workloads)
    if not values:
        raise ValueError("workloads가 비어 있습니다.")
    if any(value < 0 for value in values):
        raise ValueError("workloads는 음수일 수 없습니다.")

    active_indices = [index for index, value in enumerate(values) if value > 0]
    if not active_indices:
        raise ValueError("양의 workload를 가진 zone이 없습니다.")
    required = len(active_indices) * minimum_per_active_zone
    if total_workers < required:
        raise ValueError(
            "활성 zone 최소 작업자 수를 만족할 수 없습니다. "
            f"workers={total_workers}, active_zones={len(active_indices)}, "
            f"minimum={minimum_per_active_zone}"
        )

    active_workloads = [values[index] for index in active_indices]
    active_counts = allocate_workers(
        canonical,
        total_workers,
        active_workloads,
        seed=seed,
        minimum_per_zone=minimum_per_active_zone,
    )
    result = [0] * len(values)
    for index, count in zip(active_indices, active_counts, strict=True):
        result[index] = int(count)
    return tuple(result)


def _adapt_list_for_worker(picking_list: PickingList, worker_id: str) -> PickingList:
    return PickingList(
        wave_number=picking_list.wave_number,
        operator=worker_id,
        picks=picking_list.picks,
        order_lines=picking_list.order_lines,
    )


def _run_zone_worker(
    env: simpy.Environment,
    worker: Worker,
    queue: simpy.Store,
    *,
    method: str,
    zone_id: str,
    origin: pd.Timestamp,
    return_to_io: bool,
    executions: list[Phase3ListExecution],
    progress_callback: Callable[[int, int, Phase3ListExecution], None] | None,
    total_lists: int,
    sentinel: object,
):
    while True:
        item = yield queue.get()
        if item is sentinel:
            return

        picking_list, assignment = item
        assert isinstance(picking_list, PickingList)
        assert isinstance(assignment, PickingListZoneAssignment)
        assert picking_list.created_at is not None

        started_at = float(env.now)
        adapted = _adapt_list_for_worker(picking_list, worker.worker_id)
        yield env.process(worker.pick(adapted))
        if return_to_io and worker.current_node != worker.warehouse.default_start_node():
            yield env.process(
                worker.move_to_node(
                    worker.warehouse.default_start_node(),
                    wave_number=picking_list.wave_number,
                )
            )
        finished_at = float(env.now)
        release_seconds = float((picking_list.created_at - origin).total_seconds())
        execution = Phase3ListExecution(
            method=method,
            wave_number=picking_list.wave_number,
            original_operator=picking_list.operator,
            assigned_zone=zone_id,
            assigned_worker=worker.worker_id,
            released_at_seconds=release_seconds,
            started_at_seconds=started_at,
            finished_at_seconds=finished_at,
            release_delay_seconds=max(0.0, started_at - release_seconds),
            pick_tasks=len(picking_list.picks),
            pick_units=sum(float(task.quantity_units) for task in picking_list.picks),
            physical_zone_count=assignment.physical_zone_count,
        )
        executions.append(execution)
        if progress_callback is not None:
            progress_callback(len(executions), total_lists, execution)


def _release_zone_jobs(
    env: simpy.Environment,
    queue: simpy.Store,
    jobs: list[tuple[PickingList, PickingListZoneAssignment]],
    *,
    origin: pd.Timestamp,
    worker_count: int,
    sentinel: object,
):
    for picking_list, assignment in jobs:
        assert picking_list.created_at is not None
        release_seconds = float((picking_list.created_at - origin).total_seconds())
        if env.now < release_seconds:
            yield env.timeout(release_seconds - env.now)
        yield queue.put((picking_list, assignment))

    for _ in range(worker_count):
        yield queue.put(sentinel)


def run_phase3_method(
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList] | tuple[PickingList, ...],
    zones: tuple[AisleZone, ...],
    assignments: tuple[PickingListZoneAssignment, ...],
    *,
    method: Phase3Method,
    worker_counts: tuple[int, ...],
    selected_date: date,
    demand_entropy: DemandEntropyMetrics,
    seed: int = 42,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    edge_capacity: int = 1,
    pick_node_capacity: int = 1,
    sample_seconds: float = 5.0,
    return_to_io: bool = True,
    volume_basis: Literal["tasks", "units"] = "tasks",
    progress_callback: Callable[[int, int, Phase3ListExecution], None] | None = None,
) -> Phase3MethodResult:
    if len(zones) != len(worker_counts):
        raise ValueError("zones와 worker_counts 길이가 다릅니다.")
    if len(picking_lists) != len(assignments):
        raise ValueError("picking_lists와 assignments 길이가 다릅니다.")
    if sum(worker_counts) <= 0:
        raise ValueError("Phase 3 worker 수가 0입니다.")
    if any(p.created_at is None for p in picking_lists):
        raise ValueError("Phase 3 picking list에는 created_at이 필요합니다.")

    ordered_indices = sorted(
        range(len(picking_lists)),
        key=lambda index: (
            picking_lists[index].created_at,
            picking_lists[index].wave_number,
            picking_lists[index].operator,
        ),
    )
    origin = min(
        picking_lists[index].created_at
        for index in ordered_indices
        if picking_lists[index].created_at is not None
    )

    jobs_by_zone: dict[str, list[tuple[PickingList, PickingListZoneAssignment]]] = {
        zone.zone_id: [] for zone in zones
    }
    for index in ordered_indices:
        assignment = assignments[index]
        jobs_by_zone[assignment.zone_id].append((picking_lists[index], assignment))

    env = simpy.Environment()
    traffic = TrafficController(
        env,
        edge_capacity=edge_capacity,
        pick_node_capacity=pick_node_capacity,
    )
    workers: dict[str, Worker] = {}
    executions: list[Phase3ListExecution] = []
    sentinel = object()

    for zone, worker_count in zip(zones, worker_counts, strict=True):
        jobs = jobs_by_zone[zone.zone_id]
        if jobs and worker_count <= 0:
            raise ValueError(
                f"workload가 있는 {zone.zone_id}에 작업자가 배정되지 않았습니다."
            )
        if worker_count <= 0:
            continue

        queue = simpy.Store(env)
        for worker_index in range(worker_count):
            worker_id = f"{method.upper()}:{zone.zone_id}:W{worker_index + 1:02d}"
            worker = Worker(
                env,
                worker_id,
                warehouse,
                walking_speed_mps=walking_speed_mps,
                pick_seconds_per_unit=pick_seconds_per_unit,
                unresolved_policy="raise",
                traffic_controller=traffic,
            )
            workers[worker_id] = worker
            env.process(
                _run_zone_worker(
                    env,
                    worker,
                    queue,
                    method=method,
                    zone_id=zone.zone_id,
                    origin=origin,
                    return_to_io=return_to_io,
                    executions=executions,
                    progress_callback=progress_callback,
                    total_lists=len(picking_lists),
                    sentinel=sentinel,
                )
            )
        env.process(
            _release_zone_jobs(
                env,
                queue,
                jobs,
                origin=origin,
                worker_count=worker_count,
                sentinel=sentinel,
            )
        )

    env.run()
    executions.sort(
        key=lambda event: (
            event.started_at_seconds,
            event.wave_number,
            event.original_operator,
            event.assigned_worker,
        )
    )
    entropy_samples = sample_spatial_entropy(
        workers,
        traffic.wait_events,
        sample_seconds=sample_seconds,
    )
    occupancy = aggregate_cell_occupancy(workers, traffic.wait_events)

    phase2_executions = [
        PickingListExecution(
            wave_number=event.wave_number,
            operator=event.assigned_worker,
            released_at_seconds=event.released_at_seconds,
            started_at_seconds=event.started_at_seconds,
            finished_at_seconds=event.finished_at_seconds,
            release_delay_seconds=event.release_delay_seconds,
            pick_tasks=event.pick_tasks,
        )
        for event in executions
    ]
    phase2_summary = summarize_phase2(
        selected_date=selected_date,
        picking_lists=list(picking_lists),
        workers=workers,
        wait_events=traffic.wait_events,
        executions=phase2_executions,
        entropy_samples=entropy_samples,
        occupancy=occupancy,
        demand_entropy=demand_entropy,
        simulation_elapsed_seconds=float(env.now),
    )

    workloads = zone_workload(zones, assignments, basis=volume_basis)
    total_workload = sum(workloads)
    total_workers = sum(worker_counts)
    demand_shares = [0.0 if total_workload == 0 else value / total_workload for value in workloads]
    worker_shares = [count / total_workers for count in worker_counts]
    l1_gap = 0.5 * sum(
        abs(worker_share - demand_share)
        for worker_share, demand_share in zip(worker_shares, demand_shares, strict=True)
    )

    flow_times = [
        max(0.0, event.finished_at_seconds - event.released_at_seconds)
        for event in executions
    ]
    makespan_seconds = (
        max(event.finished_at_seconds for event in executions)
        - min(event.released_at_seconds for event in executions)
        if executions
        else 0.0
    )

    summary = Phase3RunSummary(
        method=method,
        seed=seed,
        selected_date=selected_date.isoformat(),
        total_workers=total_workers,
        zones=len(zones),
        active_zones=sum(value > 0 for value in workloads),
        worker_allocation_entropy_normalized=normalized_shannon_entropy(worker_counts),
        demand_worker_l1_gap=l1_gap,
        picking_lists=phase2_summary.picking_lists,
        pick_tasks=phase2_summary.pick_tasks,
        picked_units=phase2_summary.picked_units,
        total_distance_m=phase2_summary.total_distance_m,
        movement_events=phase2_summary.movement_events,
        movement_seconds=phase2_summary.movement_seconds,
        congestion_conflicts=phase2_summary.congestion_conflicts,
        edge_conflicts=phase2_summary.edge_conflicts,
        pick_node_conflicts=phase2_summary.pick_node_conflicts,
        congestion_wait_seconds=phase2_summary.congestion_wait_seconds,
        mean_conflict_wait_seconds=phase2_summary.mean_conflict_wait_seconds,
        p95_conflict_wait_seconds=phase2_summary.p95_conflict_wait_seconds,
        max_conflict_wait_seconds=phase2_summary.max_conflict_wait_seconds,
        congestion_delay_ratio=phase2_summary.congestion_delay_ratio,
        mean_release_delay_seconds=phase2_summary.mean_release_delay_seconds,
        max_release_delay_seconds=phase2_summary.max_release_delay_seconds,
        mean_flow_time_seconds=mean(flow_times) if flow_times else 0.0,
        makespan_seconds=makespan_seconds,
        entropy_samples=phase2_summary.entropy_samples,
        mean_spatial_entropy_normalized=phase2_summary.mean_spatial_entropy_normalized,
        mean_spatial_entropy_multiworker=phase2_summary.mean_spatial_entropy_multiworker,
        min_spatial_entropy_normalized=phase2_summary.min_spatial_entropy_normalized,
        max_spatial_entropy_normalized=phase2_summary.max_spatial_entropy_normalized,
        mean_max_concentration=phase2_summary.mean_max_concentration,
        shared_worker_ratio=phase2_summary.shared_worker_ratio,
        occupied_spatial_cells=phase2_summary.occupied_spatial_cells,
        congested_cell_seconds=phase2_summary.congested_cell_seconds,
        max_cell_occupancy=phase2_summary.max_cell_occupancy,
        simulation_elapsed_seconds=phase2_summary.simulation_elapsed_seconds,
    )
    return Phase3MethodResult(
        method=method,
        seed=seed,
        worker_counts=worker_counts,
        workers=workers,
        traffic=traffic,
        executions=tuple(executions),
        entropy_samples=tuple(entropy_samples),
        occupancy=tuple(occupancy),
        phase2_summary=phase2_summary,
        summary=summary,
    )


def _worker_records(result: Phase3MethodResult) -> list[dict[str, object]]:
    waits_by_worker: dict[str, list[CongestionWaitEvent]] = defaultdict(list)
    for event in result.traffic.wait_events:
        waits_by_worker[event.worker_id].append(event)

    records: list[dict[str, object]] = []
    for worker_id, worker in sorted(result.workers.items()):
        parts = worker_id.split(":")
        zone_id = parts[1] if len(parts) >= 2 else ""
        waits = waits_by_worker.get(worker_id, [])
        records.append(
            {
                "method": result.method,
                "operator": worker_id,
                "zone_id": zone_id,
                "distance_m": worker.total_distance_m,
                "picked_units": worker.total_picked_units,
                "movement_events": len(worker.movement_events),
                "pick_events": len(worker.pick_events),
                "congestion_conflicts": len(waits),
                "congestion_wait_seconds": sum(e.wait_seconds for e in waits),
            }
        )
    return records


def _congestion_records(result: Phase3MethodResult) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[CongestionWaitEvent]] = defaultdict(list)
    for event in result.traffic.wait_events:
        grouped[(event.resource_kind, event.resource_id)].append(event)

    records: list[dict[str, object]] = []
    for (resource_kind, resource_id), events in sorted(grouped.items()):
        waits = [event.wait_seconds for event in events]
        records.append(
            {
                "method": result.method,
                "resource_kind": resource_kind,
                "resource_id": resource_id,
                "conflicts": len(events),
                "affected_workers": len({event.worker_id for event in events}),
                "total_wait_seconds": sum(waits),
                "mean_wait_seconds": mean(waits),
                "max_wait_seconds": max(waits),
            }
        )
    return records


def _zone_records(
    zones: tuple[AisleZone, ...],
    assignments: tuple[PickingListZoneAssignment, ...],
    results: Iterable[Phase3MethodResult],
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList],
    *,
    volume_basis: Literal["tasks", "units"],
) -> list[dict[str, object]]:
    assignment_lists: dict[str, int] = Counter(a.zone_id for a in assignments)
    assignment_tasks: defaultdict[str, int] = defaultdict(int)
    assignment_units: defaultdict[str, float] = defaultdict(float)
    cross_zone_lists: defaultdict[str, int] = defaultdict(int)
    for assignment in assignments:
        assignment_tasks[assignment.zone_id] += assignment.pick_tasks
        assignment_units[assignment.zone_id] += assignment.pick_units
        if assignment.physical_zone_count > 1:
            cross_zone_lists[assignment.zone_id] += 1

    physical_tasks: defaultdict[str, int] = defaultdict(int)
    physical_units: defaultdict[str, float] = defaultdict(float)
    lookup = _aisle_zone_lookup(zones)
    for picking_list in picking_lists:
        for task in picking_list.picks:
            node_id = warehouse.node_for_location(task.location_id)
            y_value = round(float(warehouse.graph.nodes[node_id]["y_m"]), 9)
            zone_id = lookup[y_value]
            physical_tasks[zone_id] += 1
            physical_units[zone_id] += float(task.quantity_units)

    workloads = zone_workload(zones, assignments, basis=volume_basis)
    total_workload = sum(workloads)
    records: list[dict[str, object]] = []
    for result in results:
        for zone, workers, workload in zip(zones, result.worker_counts, workloads, strict=True):
            records.append(
                {
                    "method": result.method,
                    "zone_id": zone.zone_id,
                    "aisle_count": len(zone.aisle_y_values),
                    "aisle_y_min_m": zone.y_min_m,
                    "aisle_y_max_m": zone.y_max_m,
                    "assigned_lists": assignment_lists.get(zone.zone_id, 0),
                    "assigned_list_tasks": assignment_tasks.get(zone.zone_id, 0),
                    "assigned_list_units": assignment_units.get(zone.zone_id, 0.0),
                    "cross_zone_lists": cross_zone_lists.get(zone.zone_id, 0),
                    "physical_pick_tasks": physical_tasks.get(zone.zone_id, 0),
                    "physical_pick_units": physical_units.get(zone.zone_id, 0.0),
                    "allocation_workload": workload,
                    "allocation_workload_share": 0.0 if total_workload == 0 else workload / total_workload,
                    "workers": workers,
                    "worker_share": workers / sum(result.worker_counts),
                }
            )
    return records


def write_phase3_results(
    output_dir: str | Path,
    *,
    zones: tuple[AisleZone, ...],
    assignments: tuple[PickingListZoneAssignment, ...],
    results: tuple[Phase3MethodResult, ...],
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList],
    origin: pd.Timestamp,
    volume_basis: Literal["tasks", "units"],
    metadata: dict[str, object],
) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame([asdict(result.summary) for result in results]).to_csv(
        output_dir / "phase3_summary.csv", index=False
    )
    pd.DataFrame(
        _zone_records(
            zones,
            assignments,
            results,
            warehouse,
            picking_lists,
            volume_basis=volume_basis,
        )
    ).to_csv(output_dir / "phase3_zones.csv", index=False)

    worker_records = [record for result in results for record in _worker_records(result)]
    pd.DataFrame(worker_records).to_csv(output_dir / "phase3_workers.csv", index=False)

    list_records = [asdict(event) for result in results for event in result.executions]
    pd.DataFrame(list_records).to_csv(output_dir / "phase3_lists.csv", index=False)

    congestion_records = [
        record for result in results for record in _congestion_records(result)
    ]
    pd.DataFrame(congestion_records).to_csv(
        output_dir / "phase3_congestion.csv", index=False
    )

    entropy_records: list[dict[str, object]] = []
    for result in results:
        for sample in result.entropy_samples:
            record = asdict(sample)
            record["method"] = result.method
            record["timestamp"] = origin + pd.to_timedelta(sample.time_seconds, unit="s")
            entropy_records.append(record)
    pd.DataFrame(entropy_records).to_csv(output_dir / "phase3_entropy.csv", index=False)

    occupancy_records: list[dict[str, object]] = []
    for result in results:
        for cell in result.occupancy:
            record = asdict(cell)
            record["method"] = result.method
            occupancy_records.append(record)
    pd.DataFrame(occupancy_records).to_csv(
        output_dir / "phase3_occupancy.csv", index=False
    )

    assignment_records = [asdict(assignment) for assignment in assignments]
    pd.DataFrame(assignment_records).to_csv(
        output_dir / "phase3_list_zones.csv", index=False
    )

    with (output_dir / "phase3_metadata.json").open("w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2, default=str)


def build_and_run_phase3(
    data_dir: str | Path,
    *,
    target_date: date | None = None,
    max_lists: int | None = None,
    methods: Iterable[str] = PHASE3_METHODS,
    number_of_zones: int = 4,
    total_workers: int | None = None,
    volume_basis: Literal["tasks", "units"] = "tasks",
    minimum_per_active_zone: int = 1,
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
    tuple[Phase3MethodResult, ...],
    pd.Timestamp,
]:
    dataset = load_dataset(data_dir)
    if progress is not None:
        progress.report(0.12, "Building deterministic warehouse graph")
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
        progress.report(0.20, "Building physical aisle zones")
    zones = build_aisle_zones(warehouse, number_of_zones=number_of_zones)
    assignments = classify_picking_lists_by_zone(warehouse, selected_lists, zones)
    workloads = zone_workload(zones, assignments, basis=volume_basis)
    demand_entropy, _ = calculate_demand_entropy(warehouse, selected_lists)

    inferred_workers = len({p.operator for p in selected_lists})
    workers_to_use = inferred_workers if total_workers is None else total_workers
    if workers_to_use <= 0:
        raise ValueError("Phase 3 total_workers는 1 이상이어야 합니다.")

    canonical_methods: list[Phase3Method] = []
    for method in methods:
        canonical = normalize_strategy_name(method)
        if canonical not in PHASE3_METHODS:
            raise ValueError(f"Phase 3 baseline method가 아닙니다: {method}")
        if canonical not in canonical_methods:
            canonical_methods.append(canonical)
    if not canonical_methods:
        raise ValueError("Phase 3 method가 하나 이상 필요합니다.")

    origin = min(p.created_at for p in selected_lists if p.created_at is not None)
    results: list[Phase3MethodResult] = []
    for method_index, method in enumerate(canonical_methods):
        counts = allocate_phase3_workers(
            method,
            total_workers=workers_to_use,
            workloads=workloads,
            seed=seed,
            minimum_per_active_zone=minimum_per_active_zone,
        )
        base = 0.25 + 0.65 * (method_index / len(canonical_methods))
        span = 0.65 / len(canonical_methods)
        report_every = max(1, len(selected_lists) // 10)

        def simulation_progress(
            completed: int,
            total: int,
            execution: Phase3ListExecution,
            *,
            _base: float = base,
            _span: float = span,
            _method: str = method,
        ) -> None:
            if progress is None:
                return
            if completed != 1 and completed != total and completed % report_every != 0:
                return
            progress.report(
                _base + _span * (completed / total),
                f"Simulating {_method}: {completed:,}/{total:,} lists",
                current=f"zone={execution.assigned_zone}, worker={execution.assigned_worker}",
            )

        if progress is not None:
            progress.report(
                base,
                f"Starting {method} allocation simulation",
                current=", ".join(
                    f"{zone.zone_id}={count}"
                    for zone, count in zip(zones, counts, strict=True)
                ),
            )
        results.append(
            run_phase3_method(
                warehouse,
                selected_lists,
                zones,
                assignments,
                method=method,
                worker_counts=counts,
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
        )

    return (
        dataset,
        warehouse,
        audit,
        selected_date,
        selected_lists,
        zones,
        assignments,
        demand_entropy,
        tuple(results),
        origin,
    )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def _parse_methods(value: str) -> tuple[str, ...]:
    methods = tuple(part.strip() for part in value.split(",") if part.strip())
    if not methods:
        raise argparse.ArgumentTypeError("--methods에는 하나 이상의 방법이 필요합니다.")
    return methods


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Entropy Thesis - Phase 3 real-data baseline worker allocation comparison"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--date", dest="target_date", type=_parse_date, default=None)
    parser.add_argument("--max-lists", type=int, default=None)
    parser.add_argument(
        "--methods",
        type=_parse_methods,
        default=PHASE3_METHODS,
        help="comma-separated: random,equal,volume_proportional",
    )
    parser.add_argument("--zones", type=int, default=4)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--volume-basis", choices=("tasks", "units"), default="tasks")
    parser.add_argument("--minimum-per-active-zone", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--speed", type=float, default=1.2)
    parser.add_argument("--pick-seconds", type=float, default=3.0)
    parser.add_argument("--edge-capacity", type=int, default=1)
    parser.add_argument("--pick-node-capacity", type=int, default=1)
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--no-return-to-io", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("results/phase3"))
    args = parser.parse_args()

    progress = ConsoleProgress()
    progress.start("Phase 3 real-data baseline allocation comparison")
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
        origin,
    ) = build_and_run_phase3(
        args.data_dir,
        target_date=args.target_date,
        max_lists=args.max_lists,
        methods=args.methods,
        number_of_zones=args.zones,
        total_workers=args.workers,
        volume_basis=args.volume_basis,
        minimum_per_active_zone=args.minimum_per_active_zone,
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
    metadata = {
        "phase": 3,
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
            "methods": list(args.methods),
            "zones": args.zones,
            "workers": args.workers,
            "effective_workers": sum(results[0].worker_counts),
            "volume_basis": args.volume_basis,
            "minimum_per_active_zone": args.minimum_per_active_zone,
            "seed": args.seed,
            "walking_speed_mps": args.speed,
            "pick_seconds_per_unit": args.pick_seconds,
            "edge_capacity": args.edge_capacity,
            "pick_node_capacity": args.pick_node_capacity,
            "sample_seconds": args.sample_seconds,
            "return_to_io": not args.no_return_to_io,
            "max_lists": args.max_lists,
        },
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
        "allocations": {
            result.method: dict(
                zip(
                    [zone.zone_id for zone in zones],
                    result.worker_counts,
                    strict=True,
                )
            )
            for result in results
        },
        "definitions": {
            "zone_partition": (
                "Phase 1 graph의 horizontal support-point aisle y 좌표를 정렬한 뒤 "
                "zone 수만큼 연속적이고 가능한 균등한 aisle 개수로 분할한다."
            ),
            "list_zone_assignment": (
                "각 fully-valid picking list를 분할하지 않고, pick task 수가 가장 많은 "
                "physical aisle zone에 귀속한다. 동률이면 pick units, 이후 zone 순서로 결정한다."
            ),
            "allocation_workload": (
                "zone에 귀속된 전체 picking list workload. tasks 선택 시 list 내 pick task 수, "
                "units 선택 시 quantity units 합을 사용한다."
            ),
            "active_zone": "allocation_workload가 0보다 큰 zone",
            "minimum_per_active_zone": (
                "workload가 있는 zone만 대상으로 적용하는 최소 작업자 수. "
                "workload가 0인 zone에는 작업자를 강제로 두지 않는다."
            ),
            "fair_comparison": (
                "모든 방법은 동일한 selected picking lists, release time, 원래 pick 순서, "
                "warehouse graph, 이동속도, 피킹시간, congestion resource 정의를 사용한다. "
                "주요 변경 변수는 zone별 worker count이다."
            ),
            "random": "활성 zone에 최소 인원을 둔 뒤 잔여 작업자를 seed 기반 균등 무작위 배치",
            "equal": "활성 zone에 작업자를 가능한 균등하게 배치",
            "volume_proportional": "활성 zone의 allocation workload 비중에 비례하여 작업자 배치",
            "demand_worker_l1_gap": (
                "0.5 * sum(|worker_share - workload_share|). 0이면 작업자 비중과 workload 비중이 동일하다."
            ),
            "worker_allocation_entropy_normalized": (
                "zone별 작업자 수 분포의 normalized Shannon entropy. 전체 zone 수를 범주 수로 사용한다."
            ),
            "congestion_conflict": (
                "Phase 2와 동일: capacity-limited edge 또는 pick node에 즉시 진입하지 못해 "
                "양의 대기가 발생한 resource contention event"
            ),
            "mean_flow_time_seconds": (
                "각 picking list의 release 시점부터 완료 시점까지 걸린 시간 "
                "finished_at_seconds - released_at_seconds의 평균"
            ),
            "makespan_seconds": (
                "선택된 picking list 중 최초 release 시점부터 마지막 완료 시점까지의 전체 처리 시간"
            ),
        },
        "phase_boundary": (
            "Phase 3는 random/equal/volume_proportional baseline 비교까지만 수행한다. "
            "entropy_based allocation은 Phase 4에서 동일 실데이터 프레임워크에 추가한다."
        ),
        "demand_entropy": asdict(demand_entropy),
    }

    progress.report(0.94, "Writing Phase 3 result files", current=str(args.output_dir))
    write_phase3_results(
        args.output_dir,
        zones=zones,
        assignments=assignments,
        results=results,
        warehouse=warehouse,
        picking_lists=selected_lists,
        origin=origin,
        volume_basis=args.volume_basis,
        metadata=metadata,
    )
    progress.complete("Phase 3 processing completed")

    print()
    print("=== Phase 3 Baseline Allocation Comparison ===")
    print(f"Selected date        : {selected_date.isoformat()}")
    print(f"Picking lists        : {len(selected_lists):,}")
    print(f"Observed operators   : {len({p.operator for p in selected_lists}):,}")
    print(f"Effective workers    : {sum(results[0].worker_counts):,}")
    print(f"Aisle zones          : {len(zones):,}")
    print(f"Volume basis         : {args.volume_basis}")
    print()
    print("=== Zone Workload / Worker Allocation ===")
    header = "Zone   Workload   " + "   ".join(f"{result.method:>19}" for result in results)
    print(header)
    for zone_index, zone in enumerate(zones):
        counts = "   ".join(
            f"{result.worker_counts[zone_index]:>19,}" for result in results
        )
        print(f"{zone.zone_id:<5} {workloads[zone_index]:>10,.1f}   {counts}")
    print()
    print("=== Comparison ===")
    print(
        "Method               Distance(m)   Conflicts   Wait(s)   Mean release delay(s)   "
        "Mean flow time(s)   Makespan(s)   Mean spatial H"
    )
    for result in results:
        summary = result.summary
        print(
            f"{summary.method:<20} "
            f"{summary.total_distance_m:>11,.2f}   "
            f"{summary.congestion_conflicts:>9,}   "
            f"{summary.congestion_wait_seconds:>7,.2f}   "
            f"{summary.mean_release_delay_seconds:>21,.2f}   "
            f"{summary.mean_flow_time_seconds:>17,.2f}   "
            f"{summary.makespan_seconds:>11,.2f}   "
            f"{summary.mean_spatial_entropy_normalized:>14.4f}"
        )
    print()
    print(f"Results              : {args.output_dir}")
    print(
        f"Total execution time : {format_duration(progress.elapsed_seconds)} "
        f"({progress.elapsed_seconds:,.2f} s)"
    )


if __name__ == "__main__":
    main()
