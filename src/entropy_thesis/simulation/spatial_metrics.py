from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from typing import Iterable, Mapping

from ..entropy import shannon_entropy
from .traffic import CongestionWaitEvent
from .worker import Worker


@dataclass(frozen=True)
class ActivityInterval:
    worker_id: str
    started_at: float
    finished_at: float
    cell_id: str
    activity: str


@dataclass(frozen=True)
class SpatialEntropySample:
    time_seconds: float
    active_workers: int
    occupied_cells: int
    entropy_bits: float
    entropy_normalized: float
    max_concentration: float
    congested_cells: int
    workers_in_shared_cells: int
    excess_workers: int


@dataclass(frozen=True)
class CellOccupancyMetrics:
    cell_id: str
    cell_type: str
    interval_count: int
    unique_workers: int
    worker_seconds: float
    move_worker_seconds: float
    pick_worker_seconds: float
    wait_worker_seconds: float
    occupied_seconds: float
    congested_seconds: float
    max_concurrent_workers: int


def _edge_cell(a: str, b: str) -> str:
    left, right = (a, b) if a <= b else (b, a)
    return f"EDGE:{left}<->{right}"


def build_activity_intervals(
    workers: Mapping[str, Worker],
    wait_events: Iterable[CongestionWaitEvent] = (),
) -> dict[str, tuple[ActivityInterval, ...]]:
    """Convert movement/pick/wait events into non-overlapping spatial intervals."""

    by_worker: dict[str, list[ActivityInterval]] = {
        worker_id: [] for worker_id in workers
    }

    for worker_id, worker in workers.items():
        intervals = by_worker[worker_id]
        for event in worker.movement_events:
            if event.finished_at > event.started_at:
                intervals.append(
                    ActivityInterval(
                        worker_id=worker_id,
                        started_at=event.started_at,
                        finished_at=event.finished_at,
                        cell_id=_edge_cell(event.from_node, event.to_node),
                        activity="move",
                    )
                )
        for event in worker.pick_events:
            if event.finished_at > event.started_at:
                intervals.append(
                    ActivityInterval(
                        worker_id=worker_id,
                        started_at=event.started_at,
                        finished_at=event.finished_at,
                        cell_id=f"NODE:{event.node_id}",
                        activity="pick",
                    )
                )

    for event in wait_events:
        if event.entered_at <= event.requested_at:
            continue
        if event.resource_kind == "edge":
            # While an aisle segment is occupied, the delayed worker remains
            # at the segment's origin node.
            cell_id = f"NODE:{event.from_node}"
        else:
            cell_id = f"NODE:{event.node_id}"
        by_worker.setdefault(event.worker_id, []).append(
            ActivityInterval(
                worker_id=event.worker_id,
                started_at=event.requested_at,
                finished_at=event.entered_at,
                cell_id=cell_id,
                activity="wait",
            )
        )

    return {
        worker_id: tuple(
            sorted(intervals, key=lambda x: (x.started_at, x.finished_at, x.activity))
        )
        for worker_id, intervals in by_worker.items()
    }


def _normalized_worker_entropy(counts: list[int], active_workers: int) -> float:
    if active_workers <= 1:
        return 0.0
    # With enough warehouse cells, the maximum entropy for W active workers
    # is log2(W): every worker occupies a different cell.  Normalizing by the
    # number of currently occupied cells would incorrectly label two crowded
    # cells with equal counts as maximally dispersed.
    maximum_entropy = math.log2(active_workers)
    if maximum_entropy <= 0.0:
        return 0.0
    return min(1.0, shannon_entropy(counts) / maximum_entropy)


def aggregate_cell_occupancy(
    workers: Mapping[str, Worker],
    wait_events: Iterable[CongestionWaitEvent] = (),
) -> list[CellOccupancyMetrics]:
    """Aggregate exact worker-time occupancy for every visited spatial cell."""

    intervals_by_worker = build_activity_intervals(workers, wait_events)
    by_cell: dict[str, list[ActivityInterval]] = {}
    for intervals in intervals_by_worker.values():
        for interval in intervals:
            by_cell.setdefault(interval.cell_id, []).append(interval)

    result: list[CellOccupancyMetrics] = []
    for cell_id, intervals in sorted(by_cell.items()):
        worker_seconds = sum(
            interval.finished_at - interval.started_at for interval in intervals
        )
        by_activity = {"move": 0.0, "pick": 0.0, "wait": 0.0}
        for interval in intervals:
            by_activity[interval.activity] = by_activity.get(interval.activity, 0.0) + (
                interval.finished_at - interval.started_at
            )

        changes: dict[float, int] = {}
        for interval in intervals:
            changes[interval.started_at] = changes.get(interval.started_at, 0) + 1
            changes[interval.finished_at] = changes.get(interval.finished_at, 0) - 1

        active = 0
        maximum = 0
        occupied_seconds = 0.0
        congested_seconds = 0.0
        previous_time: float | None = None
        for current_time in sorted(changes):
            if previous_time is not None and current_time > previous_time:
                duration = current_time - previous_time
                if active > 0:
                    occupied_seconds += duration
                if active > 1:
                    congested_seconds += duration
            active += changes[current_time]
            maximum = max(maximum, active)
            previous_time = current_time

        result.append(
            CellOccupancyMetrics(
                cell_id=cell_id,
                cell_type="edge" if cell_id.startswith("EDGE:") else "node",
                interval_count=len(intervals),
                unique_workers=len({interval.worker_id for interval in intervals}),
                worker_seconds=float(worker_seconds),
                move_worker_seconds=float(by_activity.get("move", 0.0)),
                pick_worker_seconds=float(by_activity.get("pick", 0.0)),
                wait_worker_seconds=float(by_activity.get("wait", 0.0)),
                occupied_seconds=float(occupied_seconds),
                congested_seconds=float(congested_seconds),
                max_concurrent_workers=maximum,
            )
        )
    return result


def sample_spatial_entropy(
    workers: Mapping[str, Worker],
    wait_events: Iterable[CongestionWaitEvent] = (),
    *,
    sample_seconds: float = 5.0,
) -> list[SpatialEntropySample]:
    """Sample worker spatial distribution while at least one worker is active.

    A spatial cell is either a navigation edge (during movement) or a graph
    node (during picking/waiting).  Normalization uses the theoretical maximum
    for the number of workers active at that sample; therefore 1.0 means the
    active workers are as dispersed as possible and 0.0 means complete
    concentration (or only one active worker).
    """

    if sample_seconds <= 0:
        raise ValueError("sample_seconds는 0보다 커야 합니다.")

    intervals_by_worker = build_activity_intervals(workers, wait_events)
    all_intervals = [
        interval
        for intervals in intervals_by_worker.values()
        for interval in intervals
    ]
    if not all_intervals:
        return []

    start = min(interval.started_at for interval in all_intervals)
    end = max(interval.finished_at for interval in all_intervals)
    worker_ids = sorted(intervals_by_worker)
    positions = {worker_id: 0 for worker_id in worker_ids}
    samples: list[SpatialEntropySample] = []

    t = float(start)
    # Include a tiny tolerance to avoid accumulating binary floating error but
    # keep the activity intervals half-open [start, end).
    while t < end - 1e-12:
        cell_counts: Counter[str] = Counter()
        for worker_id in worker_ids:
            intervals = intervals_by_worker[worker_id]
            idx = positions[worker_id]
            while idx < len(intervals) and intervals[idx].finished_at <= t:
                idx += 1
            positions[worker_id] = idx
            if idx >= len(intervals):
                continue
            interval = intervals[idx]
            if interval.started_at <= t < interval.finished_at:
                cell_counts[interval.cell_id] += 1

        active_workers = sum(cell_counts.values())
        if active_workers > 0:
            counts = list(cell_counts.values())
            shared_counts = [count for count in counts if count > 1]
            samples.append(
                SpatialEntropySample(
                    time_seconds=float(t),
                    active_workers=active_workers,
                    occupied_cells=len(cell_counts),
                    entropy_bits=shannon_entropy(counts),
                    entropy_normalized=_normalized_worker_entropy(counts, active_workers),
                    max_concentration=max(counts) / active_workers,
                    congested_cells=len(shared_counts),
                    workers_in_shared_cells=sum(shared_counts),
                    excess_workers=sum(count - 1 for count in shared_counts),
                )
            )
        t += sample_seconds

    return samples


__all__ = [
    "ActivityInterval",
    "CellOccupancyMetrics",
    "SpatialEntropySample",
    "aggregate_cell_occupancy",
    "build_activity_intervals",
    "sample_spatial_entropy",
]
