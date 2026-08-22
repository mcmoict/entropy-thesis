from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path
from statistics import mean
from typing import Callable

import pandas as pd
import simpy

from .data_loader import DatasetBundle, PickingList, load_dataset
from .phase1 import Phase1Audit, audit_picking_locations, fully_resolvable_lists
from ..entropy import normalized_shannon_entropy, shannon_entropy
from .spatial_metrics import (
    CellOccupancyMetrics,
    SpatialEntropySample,
    aggregate_cell_occupancy,
    sample_spatial_entropy,
)
from .traffic import CongestionWaitEvent, TrafficController
from .progress import ConsoleProgress, format_duration
from .warehouse import WarehouseGraph
from .worker import Worker, create_workers_from_picking_lists


@dataclass(frozen=True)
class PickingListExecution:
    wave_number: str
    operator: str
    released_at_seconds: float
    started_at_seconds: float
    finished_at_seconds: float
    release_delay_seconds: float
    pick_tasks: int


@dataclass(frozen=True)
class DemandEntropyMetrics:
    warehouse_pick_nodes: int
    demand_nodes_used: int
    task_entropy_bits: float
    task_entropy_normalized: float
    unit_entropy_bits: float
    unit_entropy_normalized: float


@dataclass(frozen=True)
class Phase2Summary:
    selected_date: str
    picking_lists: int
    operators: int
    pick_tasks: int
    picked_units: float
    warehouse_pick_nodes: int
    demand_nodes_used: int
    demand_task_entropy_bits: float
    demand_task_entropy_normalized: float
    demand_unit_entropy_bits: float
    demand_unit_entropy_normalized: float
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


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * percentile / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return float(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction)


def calculate_demand_entropy(
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList] | tuple[PickingList, ...],
) -> tuple[DemandEntropyMetrics, list[dict[str, object]]]:
    """Calculate static spatial demand entropy over all warehouse pick nodes."""

    pick_nodes = sorted(set(warehouse.location_nodes.values()))
    if not pick_nodes:
        raise ValueError("Warehouse graph에 pick node가 없습니다.")

    task_counts = {node_id: 0 for node_id in pick_nodes}
    unit_counts = {node_id: 0.0 for node_id in pick_nodes}
    for picking_list in picking_lists:
        for task in picking_list.picks:
            node_id = warehouse.node_for_location(task.location_id)
            task_counts[node_id] += 1
            unit_counts[node_id] += task.quantity_units

    task_vector = [task_counts[node_id] for node_id in pick_nodes]
    unit_vector = [unit_counts[node_id] for node_id in pick_nodes]
    used = sum(count > 0 for count in task_vector)
    metrics = DemandEntropyMetrics(
        warehouse_pick_nodes=len(pick_nodes),
        demand_nodes_used=used,
        task_entropy_bits=shannon_entropy(task_vector),
        task_entropy_normalized=normalized_shannon_entropy(task_vector),
        unit_entropy_bits=shannon_entropy(unit_vector),
        unit_entropy_normalized=normalized_shannon_entropy(unit_vector),
    )

    total_tasks = sum(task_vector)
    total_units = sum(unit_vector)
    records: list[dict[str, object]] = []
    for node_id in pick_nodes:
        attrs = warehouse.graph.nodes[node_id]
        records.append(
            {
                "node_id": node_id,
                "x_m": float(attrs["x_m"]),
                "y_m": float(attrs["y_m"]),
                "location_count": len(attrs.get("location_ids", [])),
                "pick_tasks": task_counts[node_id],
                "pick_units": unit_counts[node_id],
                "task_share": (
                    0.0 if total_tasks == 0 else task_counts[node_id] / total_tasks
                ),
                "unit_share": (
                    0.0 if total_units == 0 else unit_counts[node_id] / total_units
                ),
            }
        )
    return metrics, records


def available_phase2_dates(
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList] | tuple[PickingList, ...],
) -> tuple[date, ...]:
    valid = fully_resolvable_lists(warehouse, picking_lists)
    return tuple(sorted({p.created_at.date() for p in valid if p.created_at is not None}))


def select_phase2_lists(
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList] | tuple[PickingList, ...],
    *,
    target_date: date | None = None,
    max_lists: int | None = None,
) -> tuple[date, list[PickingList]]:
    """Select timestamped, fully-resolvable real picking lists for one date.

    Phase 1 established that unresolved Picking_Wave locations must not be
    invented.  Phase 2 therefore uses only lists whose every location can be
    resolved to the warehouse graph.
    """

    if max_lists is not None and max_lists <= 0:
        raise ValueError("max_lists는 1 이상이어야 합니다.")

    valid = [
        p
        for p in fully_resolvable_lists(warehouse, picking_lists)
        if p.created_at is not None
    ]
    if not valid:
        raise ValueError("Phase 2에서 사용할 timestamped fully-valid list가 없습니다.")

    dates = sorted({p.created_at.date() for p in valid if p.created_at is not None})
    selected_date = target_date or dates[0]
    if selected_date not in dates:
        raise ValueError(
            f"선택한 날짜 {selected_date.isoformat()}에 fully-valid list가 없습니다. "
            f"available={dates[0].isoformat()}..{dates[-1].isoformat()}"
        )

    selected = [
        p
        for p in valid
        if p.created_at is not None and p.created_at.date() == selected_date
    ]
    selected.sort(
        key=lambda p: (
            p.created_at,
            p.wave_number,
            p.operator,
        )
    )
    if max_lists is not None:
        selected = selected[:max_lists]
    return selected_date, selected


def _run_operator_schedule(
    env: simpy.Environment,
    worker: Worker,
    picking_lists: list[PickingList],
    *,
    origin: pd.Timestamp,
    return_to_io: bool,
    executions: list[PickingListExecution],
    total_lists: int,
    progress_callback: Callable[[int, int, PickingList], None] | None = None,
):
    for picking_list in picking_lists:
        assert picking_list.created_at is not None
        release_seconds = float((picking_list.created_at - origin).total_seconds())
        if env.now < release_seconds:
            yield env.timeout(release_seconds - env.now)

        started_at = float(env.now)
        yield env.process(worker.pick(picking_list))
        if return_to_io and worker.current_node != worker.warehouse.default_start_node():
            yield env.process(
                worker.move_to_node(
                    worker.warehouse.default_start_node(),
                    wave_number=picking_list.wave_number,
                )
            )
        finished_at = float(env.now)
        executions.append(
            PickingListExecution(
                wave_number=picking_list.wave_number,
                operator=picking_list.operator,
                released_at_seconds=release_seconds,
                started_at_seconds=started_at,
                finished_at_seconds=finished_at,
                release_delay_seconds=max(0.0, started_at - release_seconds),
                pick_tasks=len(picking_list.picks),
            )
        )
        if progress_callback is not None:
            progress_callback(len(executions), total_lists, picking_list)


def run_phase2_simulation(
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList] | tuple[PickingList, ...],
    *,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    edge_capacity: int = 1,
    pick_node_capacity: int = 1,
    sample_seconds: float = 5.0,
    return_to_io: bool = True,
    progress_callback: Callable[[int, int, PickingList], None] | None = None,
) -> tuple[
    dict[str, Worker],
    TrafficController,
    list[PickingListExecution],
    list[SpatialEntropySample],
    list[CellOccupancyMetrics],
    pd.Timestamp,
    float,
]:
    if not picking_lists:
        raise ValueError("Phase 2 picking_lists가 비어 있습니다.")
    if any(p.created_at is None for p in picking_lists):
        raise ValueError("Phase 2 picking_lists에는 created_at이 필요합니다.")

    ordered = sorted(
        picking_lists,
        key=lambda p: (p.created_at, p.wave_number, p.operator),
    )
    origin = min(p.created_at for p in ordered if p.created_at is not None)

    env = simpy.Environment()
    traffic = TrafficController(
        env,
        edge_capacity=edge_capacity,
        pick_node_capacity=pick_node_capacity,
    )
    workers = create_workers_from_picking_lists(
        env,
        warehouse,
        ordered,
        walking_speed_mps=walking_speed_mps,
        pick_seconds_per_unit=pick_seconds_per_unit,
        unresolved_policy="raise",
        traffic_controller=traffic,
    )

    lists_by_operator: dict[str, list[PickingList]] = defaultdict(list)
    for picking_list in ordered:
        lists_by_operator[picking_list.operator].append(picking_list)

    executions: list[PickingListExecution] = []
    for operator in sorted(lists_by_operator):
        env.process(
            _run_operator_schedule(
                env,
                workers[operator],
                lists_by_operator[operator],
                origin=origin,
                return_to_io=return_to_io,
                executions=executions,
                total_lists=len(ordered),
                progress_callback=progress_callback,
            )
        )

    env.run()
    executions.sort(key=lambda e: (e.started_at_seconds, e.wave_number, e.operator))
    entropy_samples = sample_spatial_entropy(
        workers,
        traffic.wait_events,
        sample_seconds=sample_seconds,
    )
    occupancy = aggregate_cell_occupancy(workers, traffic.wait_events)
    return (
        workers,
        traffic,
        executions,
        entropy_samples,
        occupancy,
        origin,
        float(env.now),
    )


def summarize_phase2(
    *,
    selected_date: date,
    picking_lists: list[PickingList],
    workers: dict[str, Worker],
    wait_events: list[CongestionWaitEvent],
    executions: list[PickingListExecution],
    entropy_samples: list[SpatialEntropySample],
    occupancy: list[CellOccupancyMetrics],
    demand_entropy: DemandEntropyMetrics,
    simulation_elapsed_seconds: float,
) -> Phase2Summary:
    waits = [event.wait_seconds for event in wait_events]
    total_wait = sum(waits)
    movement_seconds = sum(
        event.finished_at - event.started_at
        for worker in workers.values()
        for event in worker.movement_events
    )
    movement_plus_wait = movement_seconds + total_wait
    entropy_values = [s.entropy_normalized for s in entropy_samples]
    multiworker_entropy = [
        s.entropy_normalized for s in entropy_samples if s.active_workers >= 2
    ]
    total_sampled_workers = sum(s.active_workers for s in entropy_samples)
    shared_sampled_workers = sum(s.workers_in_shared_cells for s in entropy_samples)
    release_delays = [event.release_delay_seconds for event in executions]

    return Phase2Summary(
        selected_date=selected_date.isoformat(),
        picking_lists=len(picking_lists),
        operators=len(workers),
        pick_tasks=sum(len(p.picks) for p in picking_lists),
        picked_units=sum(worker.total_picked_units for worker in workers.values()),
        warehouse_pick_nodes=demand_entropy.warehouse_pick_nodes,
        demand_nodes_used=demand_entropy.demand_nodes_used,
        demand_task_entropy_bits=demand_entropy.task_entropy_bits,
        demand_task_entropy_normalized=demand_entropy.task_entropy_normalized,
        demand_unit_entropy_bits=demand_entropy.unit_entropy_bits,
        demand_unit_entropy_normalized=demand_entropy.unit_entropy_normalized,
        total_distance_m=sum(worker.total_distance_m for worker in workers.values()),
        movement_events=sum(len(worker.movement_events) for worker in workers.values()),
        movement_seconds=movement_seconds,
        congestion_conflicts=len(wait_events),
        edge_conflicts=sum(e.resource_kind == "edge" for e in wait_events),
        pick_node_conflicts=sum(e.resource_kind == "pick_node" for e in wait_events),
        congestion_wait_seconds=total_wait,
        mean_conflict_wait_seconds=mean(waits) if waits else 0.0,
        p95_conflict_wait_seconds=_percentile(waits, 95.0),
        max_conflict_wait_seconds=max(waits, default=0.0),
        congestion_delay_ratio=(
            0.0 if movement_plus_wait <= 0 else total_wait / movement_plus_wait
        ),
        mean_release_delay_seconds=mean(release_delays) if release_delays else 0.0,
        max_release_delay_seconds=max(release_delays, default=0.0),
        entropy_samples=len(entropy_samples),
        mean_spatial_entropy_normalized=(
            mean(entropy_values) if entropy_values else 0.0
        ),
        mean_spatial_entropy_multiworker=(
            mean(multiworker_entropy) if multiworker_entropy else 0.0
        ),
        min_spatial_entropy_normalized=min(entropy_values, default=0.0),
        max_spatial_entropy_normalized=max(entropy_values, default=0.0),
        mean_max_concentration=(
            mean(s.max_concentration for s in entropy_samples)
            if entropy_samples
            else 0.0
        ),
        shared_worker_ratio=(
            0.0
            if total_sampled_workers == 0
            else shared_sampled_workers / total_sampled_workers
        ),
        occupied_spatial_cells=len(occupancy),
        congested_cell_seconds=sum(cell.congested_seconds for cell in occupancy),
        max_cell_occupancy=max(
            (cell.max_concurrent_workers for cell in occupancy), default=0
        ),
        simulation_elapsed_seconds=simulation_elapsed_seconds,
    )


def _worker_records(
    workers: dict[str, Worker], wait_events: list[CongestionWaitEvent]
) -> list[dict[str, object]]:
    waits_by_worker: dict[str, list[CongestionWaitEvent]] = defaultdict(list)
    for event in wait_events:
        waits_by_worker[event.worker_id].append(event)

    records: list[dict[str, object]] = []
    for worker_id in sorted(workers):
        worker = workers[worker_id]
        waits = waits_by_worker.get(worker_id, [])
        records.append(
            {
                "operator": worker_id,
                "distance_m": worker.total_distance_m,
                "picked_units": worker.total_picked_units,
                "movement_events": len(worker.movement_events),
                "pick_events": len(worker.pick_events),
                "congestion_conflicts": len(waits),
                "edge_conflicts": sum(e.resource_kind == "edge" for e in waits),
                "pick_node_conflicts": sum(
                    e.resource_kind == "pick_node" for e in waits
                ),
                "congestion_wait_seconds": sum(e.wait_seconds for e in waits),
            }
        )
    return records


def _congestion_resource_records(
    wait_events: list[CongestionWaitEvent],
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[CongestionWaitEvent]] = defaultdict(list)
    for event in wait_events:
        grouped[(event.resource_kind, event.resource_id)].append(event)

    records: list[dict[str, object]] = []
    for (resource_kind, resource_id), events in sorted(grouped.items()):
        waits = [event.wait_seconds for event in events]
        records.append(
            {
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


def write_phase2_results(
    output_dir: str | Path,
    *,
    summary: Phase2Summary,
    workers: dict[str, Worker],
    wait_events: list[CongestionWaitEvent],
    executions: list[PickingListExecution],
    entropy_samples: list[SpatialEntropySample],
    occupancy: list[CellOccupancyMetrics],
    demand_records: list[dict[str, object]],
    origin: pd.Timestamp,
    metadata: dict[str, object],
    progress_callback: Callable[[str, int, int, Path], None] | None = None,
) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    total_files = 8
    completed = 0

    def notify(state: str, path: Path) -> None:
        if progress_callback is not None:
            progress_callback(state, completed, total_files, path)

    def write_csv(path: Path, frame: pd.DataFrame) -> None:
        nonlocal completed
        notify("start", path)
        frame.to_csv(path, index=False)
        completed += 1
        notify("done", path)

    write_csv(output_dir / "phase2_summary.csv", pd.DataFrame([asdict(summary)]))
    write_csv(
        output_dir / "phase2_workers.csv",
        pd.DataFrame(_worker_records(workers, wait_events)),
    )
    write_csv(
        output_dir / "phase2_congestion.csv",
        pd.DataFrame(_congestion_resource_records(wait_events)),
    )
    write_csv(
        output_dir / "phase2_lists.csv",
        pd.DataFrame([asdict(event) for event in executions]),
    )

    entropy_records: list[dict[str, object]] = []
    for sample in entropy_samples:
        record = asdict(sample)
        record["timestamp"] = origin + pd.to_timedelta(
            sample.time_seconds, unit="s"
        )
        entropy_records.append(record)
    write_csv(output_dir / "phase2_entropy.csv", pd.DataFrame(entropy_records))
    write_csv(
        output_dir / "phase2_occupancy.csv",
        pd.DataFrame([asdict(cell) for cell in occupancy]),
    )
    write_csv(output_dir / "phase2_demand.csv", pd.DataFrame(demand_records))

    metadata_path = output_dir / "phase2_metadata.json"
    notify("start", metadata_path)
    with metadata_path.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2, default=str)
    completed += 1
    notify("done", metadata_path)


def build_and_run_phase2(
    data_dir: str | Path,
    *,
    target_date: date | None = None,
    max_lists: int | None = None,
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
    dict[str, Worker],
    TrafficController,
    list[PickingListExecution],
    list[SpatialEntropySample],
    list[CellOccupancyMetrics],
    DemandEntropyMetrics,
    list[dict[str, object]],
    pd.Timestamp,
    Phase2Summary,
]:
    def data_progress(state: str, completed: int, total: int, path: Path) -> None:
        if progress is None:
            return
        fraction = 0.02 + 0.13 * (completed / total)
        action = "Loading input file" if state == "start" else "Loaded input file"
        progress.report(fraction, action, current=str(path))

    dataset = load_dataset(data_dir, progress_callback=data_progress)
    if progress is not None:
        progress.report(0.18, "Building warehouse graph")
    warehouse = WarehouseGraph.build(
        dataset.storage_locations,
        dataset.support_points,
        deterministic_order=True,
    )
    if progress is not None:
        progress.report(0.23, "Auditing picking locations")
    audit = audit_picking_locations(warehouse, dataset.picking_lists)
    if progress is not None:
        progress.report(0.28, "Selecting fully-valid Phase 2 picking lists")
    selected_date, selected_lists = select_phase2_lists(
        warehouse,
        dataset.picking_lists,
        target_date=target_date,
        max_lists=max_lists,
    )
    simulation_report_every = max(1, len(selected_lists) // 20)

    def simulation_progress(
        completed: int, total: int, picking_list: PickingList
    ) -> None:
        if progress is None:
            return
        if completed != 1 and completed != total and completed % simulation_report_every != 0:
            return
        fraction = 0.30 + 0.50 * (completed / total)
        progress.report(
            fraction,
            f"Simulating picking lists {completed:,}/{total:,}",
            current=f"wave={picking_list.wave_number}, operator={picking_list.operator}",
        )

    if progress is not None:
        progress.report(
            0.30,
            f"Starting discrete-event simulation ({len(selected_lists):,} lists)",
            current=selected_date.isoformat(),
        )
    (
        workers,
        traffic,
        executions,
        entropy_samples,
        occupancy,
        origin,
        elapsed,
    ) = run_phase2_simulation(
        warehouse,
        selected_lists,
        walking_speed_mps=walking_speed_mps,
        pick_seconds_per_unit=pick_seconds_per_unit,
        edge_capacity=edge_capacity,
        pick_node_capacity=pick_node_capacity,
        sample_seconds=sample_seconds,
        return_to_io=return_to_io,
        progress_callback=simulation_progress,
    )
    if progress is not None:
        progress.report(0.84, "Calculating demand entropy")
    demand_entropy, demand_records = calculate_demand_entropy(
        warehouse, selected_lists
    )
    if progress is not None:
        progress.report(0.89, "Summarizing congestion / entropy / occupancy metrics")
    summary = summarize_phase2(
        selected_date=selected_date,
        picking_lists=selected_lists,
        workers=workers,
        wait_events=traffic.wait_events,
        executions=executions,
        entropy_samples=entropy_samples,
        occupancy=occupancy,
        demand_entropy=demand_entropy,
        simulation_elapsed_seconds=elapsed,
    )
    return (
        dataset,
        warehouse,
        audit,
        selected_date,
        selected_lists,
        workers,
        traffic,
        executions,
        entropy_samples,
        occupancy,
        demand_entropy,
        demand_records,
        origin,
        summary,
    )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Entropy Thesis - Phase 2 distance/congestion/spatial entropy"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--date", dest="target_date", type=_parse_date, default=None)
    parser.add_argument("--max-lists", type=int, default=None)
    parser.add_argument("--speed", type=float, default=1.2)
    parser.add_argument("--pick-seconds", type=float, default=3.0)
    parser.add_argument("--edge-capacity", type=int, default=1)
    parser.add_argument("--pick-node-capacity", type=int, default=1)
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--no-return-to-io", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("results/phase2"))
    args = parser.parse_args()

    progress = ConsoleProgress()
    progress.start("Phase 2 real-data simulation")

    (
        dataset,
        warehouse,
        audit,
        selected_date,
        selected_lists,
        workers,
        traffic,
        executions,
        entropy_samples,
        occupancy,
        demand_entropy,
        demand_records,
        origin,
        summary,
    ) = build_and_run_phase2(
        args.data_dir,
        target_date=args.target_date,
        max_lists=args.max_lists,
        walking_speed_mps=args.speed,
        pick_seconds_per_unit=args.pick_seconds,
        edge_capacity=args.edge_capacity,
        pick_node_capacity=args.pick_node_capacity,
        sample_seconds=args.sample_seconds,
        return_to_io=not args.no_return_to_io,
        progress=progress,
    )

    metadata = {
        "phase": 2,
        "physical_model": {
            "source_coordinate_unit": "inch",
            "coordinate_scale_to_meter": 0.0254,
            "default_io_node": warehouse.default_start_node(),
            "default_io_label": "CC-08",
        },
        "selected_date": selected_date.isoformat(),
        "origin_timestamp": origin.isoformat(),
        "input": {
            "storage_locations": len(dataset.storage_locations),
            "support_points": len(dataset.support_points),
            "picking_lists_total": len(dataset.picking_lists),
            "fully_resolvable_lists_total": audit.fully_resolvable_lists,
            "selected_lists": len(selected_lists),
        },
        "parameters": {
            "walking_speed_mps": args.speed,
            "pick_seconds_per_unit": args.pick_seconds,
            "edge_capacity": args.edge_capacity,
            "pick_node_capacity": args.pick_node_capacity,
            "sample_seconds": args.sample_seconds,
            "return_to_io": not args.no_return_to_io,
            "max_lists": args.max_lists,
        },
        "definitions": {
            "release_time": (
                "Customer_Order.creationDate의 wave별 최초시각. 같은 wave의 복수 operator는 "
                "동일 release time을 사용한다."
            ),
            "unresolved_policy": (
                "Storage_Location 좌표가 없는 Picking_Wave 위치는 Phase 1 결정에 따라 "
                "임의 좌표를 생성하지 않고 Phase 2 대상에서 제외한다."
            ),
            "congestion_conflict": (
                "capacity-limited edge 또는 pick node 요청 시 즉시 진입하지 못해 "
                "양의 대기시간이 발생한 사건. 실제 물리적 충돌 횟수를 의미하지 않는다."
            ),
            "congestion_delay_ratio": (
                "congestion_wait_seconds / (movement_seconds + congestion_wait_seconds)"
            ),
            "spatial_cell": (
                "이동 중에는 undirected graph edge, 피킹/대기 중에는 graph node"
            ),
            "demand_entropy": (
                "선택된 날짜의 fully-valid pick task를 warehouse pick node별로 집계한 "
                "Shannon entropy. normalized 값은 전체 warehouse pick node 수를 기준으로 한다."
            ),
            "spatial_entropy": (
                "각 표본시각 active worker의 spatial-cell 분포에 대한 Shannon entropy. "
                "normalized 값은 log2(active_workers) 기준 최대 분산을 1로 둔다."
            ),
            "cell_occupancy": (
                "movement/pick/wait interval을 spatial cell별로 합산한 정확한 worker-time. "
                "congested_seconds는 동시 occupancy가 2 이상인 시간이다."
            ),
            "active_worker": (
                "해당 표본시각에 이동, 피킹 또는 congestion wait 상태인 작업자. "
                "wave 사이 idle 시간은 entropy 계산에서 제외한다."
            ),
        },
    }
    def output_progress(state: str, completed: int, total: int, path: Path) -> None:
        fraction = 0.90 + 0.09 * (completed / total)
        action = "Generating result file" if state == "start" else "Generated result file"
        progress.report(fraction, action, current=str(path))

    progress.report(0.90, "Preparing Phase 2 result files", current=str(args.output_dir))
    write_phase2_results(
        args.output_dir,
        summary=summary,
        workers=workers,
        wait_events=traffic.wait_events,
        executions=executions,
        entropy_samples=entropy_samples,
        occupancy=occupancy,
        demand_records=demand_records,
        origin=origin,
        metadata=metadata,
        progress_callback=output_progress,
    )
    progress.complete("Phase 2 processing completed")
    total_wall_clock_seconds = progress.elapsed_seconds

    print()
    print("=== Phase 2 Real-Data Simulation ===")
    print(f"Selected date        : {summary.selected_date}")
    print(f"Picking lists        : {summary.picking_lists:,}")
    print(f"Operators            : {summary.operators:,}")
    print(f"Pick tasks           : {summary.pick_tasks:,}")
    print(f"Picked units         : {summary.picked_units:,.0f}")
    print(f"Total distance       : {summary.total_distance_m:,.2f} m")
    print(f"Movement events      : {summary.movement_events:,}")
    print()
    print("=== Demand Entropy ===")
    print(f"Pick nodes used      : {summary.demand_nodes_used:,}/{summary.warehouse_pick_nodes:,}")
    print(f"Task normalized H    : {summary.demand_task_entropy_normalized:.4f}")
    print(f"Unit normalized H    : {summary.demand_unit_entropy_normalized:.4f}")
    print()
    print("=== Congestion ===")
    print(f"Conflict events      : {summary.congestion_conflicts:,}")
    print(f"  edge conflicts     : {summary.edge_conflicts:,}")
    print(f"  pick-node conflicts: {summary.pick_node_conflicts:,}")
    print(f"Total wait           : {summary.congestion_wait_seconds:,.2f} s")
    print(f"Mean wait/conflict   : {summary.mean_conflict_wait_seconds:,.2f} s")
    print(f"P95 wait/conflict    : {summary.p95_conflict_wait_seconds:,.2f} s")
    print(f"Max wait/conflict    : {summary.max_conflict_wait_seconds:,.2f} s")
    print(f"Congestion delay     : {summary.congestion_delay_ratio:.2%}")
    print()
    print("=== Spatial Entropy ===")
    print(f"Samples              : {summary.entropy_samples:,}")
    print(f"Mean normalized H    : {summary.mean_spatial_entropy_normalized:.4f}")
    print(f"Mean H (2+ workers)  : {summary.mean_spatial_entropy_multiworker:.4f}")
    print(f"Min / Max H          : {summary.min_spatial_entropy_normalized:.4f} / {summary.max_spatial_entropy_normalized:.4f}")
    print(f"Mean concentration   : {summary.mean_max_concentration:.4f}")
    print(f"Shared-worker ratio  : {summary.shared_worker_ratio:.2%}")
    print(f"Visited spatial cells: {summary.occupied_spatial_cells:,}")
    print(f"Congested cell time  : {summary.congested_cell_seconds:,.2f} s")
    print(f"Max cell occupancy   : {summary.max_cell_occupancy:,}")
    print()
    print(f"Results              : {args.output_dir}")
    print(f"Total execution time : {format_duration(total_wall_clock_seconds)} ({total_wall_clock_seconds:,.2f} s)")


if __name__ == "__main__":
    main()
