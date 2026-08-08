"""SimPy discrete-event model of a zoned warehouse picking operation.

Orders arrive according to independent Poisson processes whose rates are the
global arrival rate multiplied by each zone's normalized volume share. Every
allocated worker is a parallel server, and service times are exponentially
distributed with the zone's per-worker service rate.

Arrival and service requirements are sampled when a job arrives, using
separate seeded random streams per zone. Consequently, running different
allocation strategies with the same simulation seed uses common demand and
service-time samples, which reduces noise in strategy comparisons.

Metrics use the half-open observation window ``[warm_up, duration)``.
Throughput counts every completion in that window, including warm-up backlog.
Service level and wait/system-time distributions instead use the cohort that
arrives in the observation window; unfinished cohort members are right-censored
at ``duration``.
"""

from __future__ import annotations

from collections.abc import Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import math
from typing import Any

import numpy as np
import simpy

from ..metrics import (
    SimulationMetrics,
    calculate_simulation_metrics,
    calculate_zone_metrics,
)


@dataclass(frozen=True, slots=True)
class ZoneConfig:
    """Demand and service parameters for one warehouse zone."""

    zone_id: str
    volume_share: float
    service_rate: float

    def __post_init__(self) -> None:
        if not isinstance(self.zone_id, str) or not self.zone_id.strip():
            raise ValueError("zone_id must be a non-empty string")
        if not math.isfinite(self.volume_share) or self.volume_share < 0.0:
            raise ValueError(f"zone {self.zone_id!r} volume_share must be finite and non-negative")
        if not math.isfinite(self.service_rate) or self.service_rate <= 0.0:
            raise ValueError(f"zone {self.zone_id!r} service_rate must be finite and positive")


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Time horizon and random seed for one simulation replication."""

    duration: float
    arrival_rate: float
    warm_up: float = 0.0
    seed: int = 42

    def __post_init__(self) -> None:
        if not math.isfinite(self.duration) or self.duration <= 0.0:
            raise ValueError("duration must be finite and positive")
        if not math.isfinite(self.warm_up) or self.warm_up < 0.0:
            raise ValueError("warm_up must be finite and non-negative")
        if self.warm_up >= self.duration:
            raise ValueError("warm_up must be smaller than duration")
        if not math.isfinite(self.arrival_rate) or self.arrival_rate < 0.0:
            raise ValueError("arrival_rate must be finite and non-negative")
        if isinstance(self.seed, bool) or not isinstance(self.seed, (int, np.integer)):
            raise TypeError("seed must be an integer")
        if int(self.seed) < 0:
            raise ValueError("seed must be non-negative")


@dataclass(frozen=True, slots=True)
class WarehouseSimulationResult:
    """Configuration, allocation, and metrics from one replication."""

    zones: tuple[ZoneConfig, ...]
    allocation: tuple[int, ...]
    config: SimulationConfig
    metrics: SimulationMetrics

    @property
    def allocation_by_zone(self) -> dict[str, int]:
        """Return the integer allocation keyed by zone identifier."""

        return {
            zone.zone_id: workers
            for zone, workers in zip(self.zones, self.allocation, strict=True)
        }


@dataclass(slots=True)
class _Job:
    arrival_time: float
    service_time: float
    in_observation_cohort: bool


@dataclass(slots=True)
class _ZoneState:
    config: ZoneConfig
    store: simpy.Store
    arrivals_before_observation: int = 0
    completions_before_observation: int = 0
    observation_arrivals: int = 0
    observation_completions: int = 0
    cohort_completions: int = 0
    cohort_waiting_times: list[float] = field(default_factory=list)
    cohort_system_times: list[float] = field(default_factory=list)
    busy_time: float = 0.0
    active_starts: dict[int, float] = field(default_factory=dict)


def _interval_overlap(start: float, end: float, lower: float, upper: float) -> float:
    return max(0.0, min(end, upper) - max(start, lower))


class WarehouseSimulation:
    """A reusable, deterministic SimPy warehouse simulation.

    The object is safe to run repeatedly: each call to :meth:`run` creates a
    new SimPy environment and reinitializes all seeded random streams.
    """

    def __init__(
        self,
        zones: Iterable[ZoneConfig],
        allocation: Mapping[str, int] | Sequence[int],
        config: SimulationConfig,
    ) -> None:
        self.zones = tuple(zones)
        if not self.zones:
            raise ValueError("zones must contain at least one zone")
        identifiers = [zone.zone_id for zone in self.zones]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("zone identifiers must be unique")
        if sum(zone.volume_share for zone in self.zones) <= 0.0:
            raise ValueError("at least one zone must have a positive volume_share")
        self.allocation = self._coerce_allocation(allocation, identifiers)
        self.config = config

    @staticmethod
    def _coerce_allocation(
        allocation: Mapping[str, int] | Sequence[int],
        zone_ids: list[str],
    ) -> tuple[int, ...]:
        if isinstance(allocation, Mapping):
            missing = set(zone_ids).difference(allocation)
            extra = set(allocation).difference(zone_ids)
            if missing or extra:
                details: list[str] = []
                if missing:
                    details.append(f"missing zones: {sorted(missing)}")
                if extra:
                    details.append(f"unknown zones: {sorted(extra)}")
                raise ValueError("allocation keys do not match zones (" + "; ".join(details) + ")")
            values = tuple(allocation[zone_id] for zone_id in zone_ids)
        else:
            values = tuple(allocation)
            if len(values) != len(zone_ids):
                raise ValueError("allocation length must equal the number of zones")

        result: list[int] = []
        for zone_id, value in zip(zone_ids, values, strict=True):
            if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
                raise TypeError(f"worker allocation for zone {zone_id!r} must be an integer")
            count = int(value)
            if count < 0:
                raise ValueError(f"worker allocation for zone {zone_id!r} must be non-negative")
            result.append(count)
        if sum(result) <= 0:
            raise ValueError("allocation must contain at least one worker")
        return tuple(result)

    def _arrival_process(
        self,
        environment: simpy.Environment,
        state: _ZoneState,
        zone_arrival_rate: float,
        arrival_rng: np.random.Generator,
        service_rng: np.random.Generator,
    ) -> Generator[Any, Any, None]:
        while True:
            delay = float(arrival_rng.exponential(1.0 / zone_arrival_rate))
            yield environment.timeout(delay)
            arrival_time = float(environment.now)
            job = _Job(
                arrival_time=arrival_time,
                service_time=float(
                    service_rng.exponential(1.0 / state.config.service_rate)
                ),
                in_observation_cohort=arrival_time >= self.config.warm_up,
            )
            if job.in_observation_cohort:
                state.observation_arrivals += 1
            else:
                state.arrivals_before_observation += 1
            yield state.store.put(job)

    def _worker_process(
        self,
        environment: simpy.Environment,
        state: _ZoneState,
        worker_id: int,
    ) -> Generator[Any, Any, None]:
        while True:
            job: _Job = yield state.store.get()
            service_start = float(environment.now)
            state.active_starts[worker_id] = service_start
            wait = service_start - job.arrival_time
            yield environment.timeout(job.service_time)
            completion = float(environment.now)
            state.busy_time += _interval_overlap(
                service_start,
                completion,
                self.config.warm_up,
                self.config.duration,
            )
            state.active_starts.pop(worker_id, None)
            if completion >= self.config.warm_up:
                state.observation_completions += 1
            else:
                state.completions_before_observation += 1
            if job.in_observation_cohort:
                state.cohort_completions += 1
                state.cohort_waiting_times.append(wait)
                state.cohort_system_times.append(completion - job.arrival_time)

    def run(self) -> WarehouseSimulationResult:
        """Execute one replication and return all aggregate metrics."""

        environment = simpy.Environment()
        states = tuple(_ZoneState(zone, simpy.Store(environment)) for zone in self.zones)

        seed_sequence = np.random.SeedSequence(int(self.config.seed))
        random_streams = seed_sequence.spawn(len(self.zones) * 2)
        volume_total = sum(zone.volume_share for zone in self.zones)

        for index, (state, worker_count) in enumerate(
            zip(states, self.allocation, strict=True)
        ):
            zone_rate = (
                self.config.arrival_rate * state.config.volume_share / volume_total
            )
            if zone_rate > 0.0:
                environment.process(
                    self._arrival_process(
                        environment,
                        state,
                        zone_rate,
                        np.random.default_rng(random_streams[index * 2]),
                        np.random.default_rng(random_streams[index * 2 + 1]),
                    )
                )
            for worker_id in range(worker_count):
                environment.process(
                    self._worker_process(environment, state, worker_id)
                )

        environment.run(until=self.config.duration)
        measurement_duration = self.config.duration - self.config.warm_up
        zone_metrics = []
        all_cohort_waiting_times: list[float] = []
        all_cohort_system_times: list[float] = []

        for state, worker_count in zip(states, self.allocation, strict=True):
            busy_time = state.busy_time + sum(
                _interval_overlap(
                    start,
                    self.config.duration,
                    self.config.warm_up,
                    self.config.duration,
                )
                for start in state.active_starts.values()
            )
            wip_start = (
                state.arrivals_before_observation
                - state.completions_before_observation
            )
            wip_end = len(state.store.items) + len(state.active_starts)
            zone_metrics.append(
                calculate_zone_metrics(
                    zone_id=state.config.zone_id,
                    worker_count=worker_count,
                    observation_arrivals=state.observation_arrivals,
                    observation_completions=state.observation_completions,
                    cohort_completions=state.cohort_completions,
                    wip_start=wip_start,
                    wip_end=wip_end,
                    queue_length_end=len(state.store.items),
                    cohort_waiting_times=state.cohort_waiting_times,
                    cohort_system_times=state.cohort_system_times,
                    busy_time=busy_time,
                    measurement_duration=measurement_duration,
                )
            )
            all_cohort_waiting_times.extend(state.cohort_waiting_times)
            all_cohort_system_times.extend(state.cohort_system_times)

        metrics = calculate_simulation_metrics(
            zones=tuple(zone_metrics),
            cohort_waiting_times=all_cohort_waiting_times,
            cohort_system_times=all_cohort_system_times,
            allocation=list(self.allocation),
            measurement_duration=measurement_duration,
        )
        return WarehouseSimulationResult(
            zones=self.zones,
            allocation=self.allocation,
            config=self.config,
            metrics=metrics,
        )


def simulate_warehouse(
    zones: Iterable[ZoneConfig],
    allocation: Mapping[str, int] | Sequence[int],
    *,
    duration: float,
    arrival_rate: float,
    warm_up: float = 0.0,
    seed: int = 42,
) -> WarehouseSimulationResult:
    """Convenience function for constructing and running a simulation."""

    config = SimulationConfig(
        duration=duration,
        arrival_rate=arrival_rate,
        warm_up=warm_up,
        seed=seed,
    )
    return WarehouseSimulation(zones, allocation, config).run()


__all__ = [
    "SimulationConfig",
    "WarehouseSimulation",
    "WarehouseSimulationResult",
    "ZoneConfig",
    "simulate_warehouse",
]
