"""Performance and spatial-distribution metrics for simulation runs.

The observation window is ``[warm_up, duration)``. Flow metrics and cohort
metrics intentionally use different populations:

* ``observation_arrivals`` and ``observation_completions`` count boundary
  crossings during the window. Throughput uses all observation completions,
  including jobs already in progress or queued at the end of warm-up.
* The arrival ``cohort`` contains jobs that arrive during the window.
  ``cohort_service_level`` is the fraction completed by the simulation
  horizon. Its unfinished jobs are right-censored. The value is ``NaN`` when
  the cohort is empty rather than treating an unobserved level as perfect.
* Cohort wait and system-time statistics contain only cohort jobs completed by
  the horizon. They are therefore conditional on completion and should be
  interpreted together with ``cohort_service_level``.

The flow identity ``wip_end = wip_start + observation_arrivals -
observation_completions`` holds for every zone and for the warehouse total.
Observation arrival/completion entropy is ``NaN`` when its event sample is
empty, because no spatial distribution was observed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

import numpy as np

from .entropy import normalized_shannon_entropy, shannon_entropy


def _distribution(values: list[float]) -> tuple[float, float, float, float]:
    if not values:
        nan = float("nan")
        return nan, nan, nan, nan
    data = np.asarray(values, dtype=np.float64)
    return (
        float(np.mean(data)),
        float(np.median(data)),
        float(np.percentile(data, 95.0)),
        float(np.max(data)),
    )


@dataclass(frozen=True, slots=True)
class ZoneMetrics:
    """Measured performance for one picking zone.

    ``queue_length_end`` counts waiting jobs, whereas ``wip_end`` also includes
    jobs being served at the horizon.
    """

    zone_id: str
    worker_count: int
    observation_arrivals: int
    observation_completions: int
    cohort_completions: int
    cohort_unfinished: int
    wip_start: int
    wip_end: int
    queue_length_end: int
    throughput: float
    cohort_service_level: float
    utilization: float
    busy_time: float
    cohort_mean_wait: float
    cohort_median_wait: float
    cohort_p95_wait: float
    cohort_max_wait: float
    cohort_mean_system_time: float
    cohort_median_system_time: float
    cohort_p95_system_time: float
    cohort_max_system_time: float

    def to_record(self) -> dict[str, Any]:
        """Return a flat dictionary suitable for a dataframe row."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class SimulationMetrics:
    """Warehouse-wide metrics plus the per-zone breakdown.

    Timing distributions use completed members of the observation-arrival
    cohort and are right-censored at the simulation horizon.
    """

    observation_arrivals: int
    observation_completions: int
    cohort_completions: int
    cohort_unfinished: int
    wip_start: int
    wip_end: int
    queue_length_end: int
    throughput: float
    cohort_service_level: float
    utilization: float
    busy_time: float
    cohort_mean_wait: float
    cohort_median_wait: float
    cohort_p95_wait: float
    cohort_max_wait: float
    cohort_mean_system_time: float
    cohort_median_system_time: float
    cohort_p95_system_time: float
    cohort_max_system_time: float
    allocation_entropy_bits: float
    allocation_entropy_normalized: float
    observation_arrival_entropy_bits: float
    observation_arrival_entropy_normalized: float
    observation_completion_entropy_bits: float
    observation_completion_entropy_normalized: float
    zones: tuple[ZoneMetrics, ...]

    def to_record(self) -> dict[str, Any]:
        """Return warehouse-wide scalar metrics, excluding nested zones."""

        record = asdict(self)
        record.pop("zones")
        return record


def calculate_zone_metrics(
    *,
    zone_id: str,
    worker_count: int,
    observation_arrivals: int,
    observation_completions: int,
    cohort_completions: int,
    wip_start: int,
    wip_end: int,
    queue_length_end: int,
    cohort_waiting_times: list[float],
    cohort_system_times: list[float],
    busy_time: float,
    measurement_duration: float,
) -> ZoneMetrics:
    """Build validated metrics for a single simulation zone."""

    if measurement_duration <= 0.0 or not math.isfinite(measurement_duration):
        raise ValueError("measurement_duration must be finite and positive")
    if worker_count < 0:
        raise ValueError("worker_count must be non-negative")
    counts = (
        observation_arrivals,
        observation_completions,
        cohort_completions,
        wip_start,
        wip_end,
        queue_length_end,
    )
    if any(count < 0 for count in counts):
        raise ValueError("flow and cohort counts must be non-negative")
    if cohort_completions > observation_arrivals:
        raise ValueError("cohort_completions cannot exceed observation_arrivals")
    if len(cohort_waiting_times) != cohort_completions or len(
        cohort_system_times
    ) != cohort_completions:
        raise ValueError("cohort timing counts must equal cohort_completions")
    if wip_end != wip_start + observation_arrivals - observation_completions:
        raise ValueError("WIP counts violate observation-window flow conservation")
    if queue_length_end > wip_end:
        raise ValueError("queue_length_end cannot exceed wip_end")

    wait_mean, wait_median, wait_p95, wait_max = _distribution(
        cohort_waiting_times
    )
    system_mean, system_median, system_p95, system_max = _distribution(
        cohort_system_times
    )
    capacity_time = worker_count * measurement_duration
    utilization = 0.0 if capacity_time == 0.0 else busy_time / capacity_time
    # Tiny floating-point excursions can occur at interval boundaries.
    utilization = float(np.clip(utilization, 0.0, 1.0))
    service_level = (
        float("nan")
        if observation_arrivals == 0
        else cohort_completions / observation_arrivals
    )

    return ZoneMetrics(
        zone_id=zone_id,
        worker_count=worker_count,
        observation_arrivals=observation_arrivals,
        observation_completions=observation_completions,
        cohort_completions=cohort_completions,
        cohort_unfinished=observation_arrivals - cohort_completions,
        wip_start=wip_start,
        wip_end=wip_end,
        queue_length_end=queue_length_end,
        throughput=observation_completions / measurement_duration,
        cohort_service_level=float(service_level),
        utilization=utilization,
        busy_time=float(busy_time),
        cohort_mean_wait=wait_mean,
        cohort_median_wait=wait_median,
        cohort_p95_wait=wait_p95,
        cohort_max_wait=wait_max,
        cohort_mean_system_time=system_mean,
        cohort_median_system_time=system_median,
        cohort_p95_system_time=system_p95,
        cohort_max_system_time=system_max,
    )


def calculate_simulation_metrics(
    *,
    zones: tuple[ZoneMetrics, ...],
    cohort_waiting_times: list[float],
    cohort_system_times: list[float],
    allocation: list[int],
    measurement_duration: float,
) -> SimulationMetrics:
    """Aggregate zone metrics and calculate spatial entropy measures."""

    if not zones:
        raise ValueError("at least one zone metric is required")
    wait_mean, wait_median, wait_p95, wait_max = _distribution(
        cohort_waiting_times
    )
    system_mean, system_median, system_p95, system_max = _distribution(
        cohort_system_times
    )
    observation_arrivals = sum(zone.observation_arrivals for zone in zones)
    observation_completions = sum(
        zone.observation_completions for zone in zones
    )
    cohort_completions = sum(zone.cohort_completions for zone in zones)
    observation_arrival_counts = [
        zone.observation_arrivals for zone in zones
    ]
    observation_completion_counts = [
        zone.observation_completions for zone in zones
    ]
    total_workers = sum(allocation)
    busy_time = sum(zone.busy_time for zone in zones)
    capacity_time = total_workers * measurement_duration

    return SimulationMetrics(
        observation_arrivals=observation_arrivals,
        observation_completions=observation_completions,
        cohort_completions=cohort_completions,
        cohort_unfinished=observation_arrivals - cohort_completions,
        wip_start=sum(zone.wip_start for zone in zones),
        wip_end=sum(zone.wip_end for zone in zones),
        queue_length_end=sum(zone.queue_length_end for zone in zones),
        throughput=observation_completions / measurement_duration,
        cohort_service_level=(
            float("nan")
            if observation_arrivals == 0
            else cohort_completions / observation_arrivals
        ),
        utilization=(
            0.0
            if capacity_time == 0.0
            else float(np.clip(busy_time / capacity_time, 0.0, 1.0))
        ),
        busy_time=float(busy_time),
        cohort_mean_wait=wait_mean,
        cohort_median_wait=wait_median,
        cohort_p95_wait=wait_p95,
        cohort_max_wait=wait_max,
        cohort_mean_system_time=system_mean,
        cohort_median_system_time=system_median,
        cohort_p95_system_time=system_p95,
        cohort_max_system_time=system_max,
        allocation_entropy_bits=shannon_entropy(allocation),
        allocation_entropy_normalized=normalized_shannon_entropy(allocation),
        observation_arrival_entropy_bits=(
            float("nan")
            if observation_arrivals == 0
            else shannon_entropy(observation_arrival_counts)
        ),
        observation_arrival_entropy_normalized=(
            float("nan")
            if observation_arrivals == 0
            else normalized_shannon_entropy(observation_arrival_counts)
        ),
        observation_completion_entropy_bits=(
            float("nan")
            if observation_completions == 0
            else shannon_entropy(observation_completion_counts)
        ),
        observation_completion_entropy_normalized=(
            float("nan")
            if observation_completions == 0
            else normalized_shannon_entropy(observation_completion_counts)
        ),
        zones=zones,
    )


__all__ = [
    "SimulationMetrics",
    "ZoneMetrics",
    "calculate_simulation_metrics",
    "calculate_zone_metrics",
]
