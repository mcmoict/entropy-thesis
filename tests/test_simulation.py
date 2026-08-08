"""Tests for the SimPy warehouse model and its public configuration types."""

from __future__ import annotations

import math

import numpy as np
import pytest

from entropy_thesis.simulation import (
    SimulationConfig,
    WarehouseSimulation,
    ZoneConfig,
    simulate_warehouse,
)


def _zones() -> tuple[ZoneConfig, ...]:
    return (
        ZoneConfig("A", volume_share=0.6, service_rate=4.0),
        ZoneConfig("B", volume_share=0.4, service_rate=3.0),
    )


def _config(seed: int = 31415) -> SimulationConfig:
    return SimulationConfig(
        duration=60.0,
        warm_up=5.0,
        arrival_rate=1.0,
        seed=seed,
    )


def test_simulation_is_deterministic_for_a_fixed_seed() -> None:
    simulation = WarehouseSimulation(_zones(), [2, 2], _config())
    first = simulation.run()
    second = simulation.run()
    assert first == second


def test_convenience_function_matches_explicit_simulation() -> None:
    expected = WarehouseSimulation(_zones(), {"A": 2, "B": 1}, _config(12)).run()
    actual = simulate_warehouse(
        _zones(),
        {"A": 2, "B": 1},
        duration=60,
        warm_up=5,
        arrival_rate=1,
        seed=12,
    )
    assert actual == expected


def test_simulation_result_preserves_configuration_and_allocation() -> None:
    zones = _zones()
    config = _config()
    result = WarehouseSimulation(zones, {"B": 1, "A": 3}, config).run()
    assert result.zones == zones
    assert result.config == config
    assert result.allocation == (3, 1)
    assert result.allocation_by_zone == {"A": 3, "B": 1}


def test_simulation_metrics_satisfy_count_rate_and_range_invariants() -> None:
    config = _config()
    result = WarehouseSimulation(_zones(), [2, 2], config).run()
    metrics = result.metrics
    measurement_duration = config.duration - config.warm_up

    assert metrics.observation_arrivals == sum(
        zone.observation_arrivals for zone in metrics.zones
    )
    assert metrics.observation_completions == sum(
        zone.observation_completions for zone in metrics.zones
    )
    assert metrics.cohort_completions == sum(
        zone.cohort_completions for zone in metrics.zones
    )
    assert metrics.cohort_unfinished == (
        metrics.observation_arrivals - metrics.cohort_completions
    )
    assert metrics.wip_end == (
        metrics.wip_start
        + metrics.observation_arrivals
        - metrics.observation_completions
    )
    assert metrics.queue_length_end == sum(
        zone.queue_length_end for zone in metrics.zones
    )
    assert 0 <= metrics.cohort_completions <= metrics.observation_arrivals
    assert metrics.throughput == pytest.approx(
        metrics.observation_completions / measurement_duration
    )
    assert 0.0 <= metrics.cohort_service_level <= 1.0
    assert 0.0 <= metrics.utilization <= 1.0
    assert 0.0 <= metrics.allocation_entropy_normalized <= 1.0
    assert 0.0 <= metrics.observation_arrival_entropy_normalized <= 1.0
    assert 0.0 <= metrics.observation_completion_entropy_normalized <= 1.0

    for zone, workers in zip(metrics.zones, result.allocation, strict=True):
        assert zone.worker_count == workers
        assert zone.cohort_unfinished == (
            zone.observation_arrivals - zone.cohort_completions
        )
        assert 0 <= zone.cohort_completions <= zone.observation_arrivals
        assert zone.wip_end == (
            zone.wip_start
            + zone.observation_arrivals
            - zone.observation_completions
        )
        assert zone.queue_length_end >= 0
        assert zone.wip_end >= zone.queue_length_end
        assert zone.throughput == pytest.approx(
            zone.observation_completions / measurement_duration
        )
        assert 0.0 <= zone.cohort_service_level <= 1.0
        assert 0.0 <= zone.utilization <= 1.0
        if zone.cohort_completions:
            assert zone.cohort_mean_wait >= 0.0
            assert zone.cohort_mean_system_time >= zone.cohort_mean_wait


def test_zero_arrival_rate_produces_valid_empty_metrics() -> None:
    result = simulate_warehouse(
        _zones(),
        [1, 1],
        duration=10,
        arrival_rate=0,
        seed=1,
    )
    metrics = result.metrics
    assert metrics.observation_arrivals == 0
    assert metrics.observation_completions == 0
    assert metrics.cohort_completions == 0
    assert metrics.throughput == 0.0
    assert math.isnan(metrics.cohort_service_level)
    assert metrics.utilization == 0.0
    assert math.isnan(metrics.cohort_mean_wait)
    assert math.isnan(metrics.observation_arrival_entropy_bits)
    assert math.isnan(metrics.observation_completion_entropy_normalized)
    assert all(zone.observation_arrivals == 0 for zone in metrics.zones)
    assert all(math.isnan(zone.cohort_service_level) for zone in metrics.zones)


def test_zone_with_no_workers_accumulates_work_without_completions() -> None:
    result = simulate_warehouse(
        _zones(),
        [2, 0],
        duration=30,
        arrival_rate=2,
        seed=9,
    )
    unstaffed = result.metrics.zones[1]
    assert unstaffed.observation_arrivals > 0
    assert unstaffed.observation_completions == 0
    assert unstaffed.cohort_completions == 0
    assert unstaffed.worker_count == 0
    assert unstaffed.utilization == 0.0
    assert unstaffed.wip_start == 0
    assert unstaffed.wip_end == unstaffed.observation_arrivals
    assert unstaffed.queue_length_end == unstaffed.wip_end


def test_warmup_backlog_is_counted_in_observation_throughput() -> None:
    result = simulate_warehouse(
        [ZoneConfig("A", volume_share=1.0, service_rate=1.0)],
        [1],
        duration=30,
        warm_up=20,
        arrival_rate=5,
        seed=9,
    )
    metrics = result.metrics

    assert metrics.wip_start > 0
    assert metrics.observation_completions > 0
    assert metrics.observation_completions > metrics.cohort_completions
    assert metrics.throughput == pytest.approx(
        metrics.observation_completions / 10
    )
    assert metrics.utilization == pytest.approx(1.0)
    assert metrics.wip_end == (
        metrics.wip_start
        + metrics.observation_arrivals
        - metrics.observation_completions
    )
    assert metrics.wip_end == metrics.queue_length_end + 1
    assert metrics.cohort_service_level == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"zone_id": "", "volume_share": 1.0, "service_rate": 1.0},
        {"zone_id": "   ", "volume_share": 1.0, "service_rate": 1.0},
        {"zone_id": "A", "volume_share": -0.1, "service_rate": 1.0},
        {"zone_id": "A", "volume_share": math.nan, "service_rate": 1.0},
        {"zone_id": "A", "volume_share": 1.0, "service_rate": 0.0},
        {"zone_id": "A", "volume_share": 1.0, "service_rate": math.inf},
    ],
)
def test_zone_config_rejects_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ZoneConfig(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"duration": 0, "arrival_rate": 1},
        {"duration": math.inf, "arrival_rate": 1},
        {"duration": 10, "warm_up": -1, "arrival_rate": 1},
        {"duration": 10, "warm_up": 10, "arrival_rate": 1},
        {"duration": 10, "arrival_rate": -1},
        {"duration": 10, "arrival_rate": math.nan},
        {"duration": 10, "arrival_rate": 1, "seed": -1},
    ],
)
def test_simulation_config_rejects_invalid_values(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        SimulationConfig(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("seed", [True, 1.5, "42"])
def test_simulation_config_requires_an_integer_seed(seed: object) -> None:
    with pytest.raises(TypeError):
        SimulationConfig(duration=10, arrival_rate=1, seed=seed)  # type: ignore[arg-type]


def test_simulation_accepts_numpy_integer_allocation_and_seed() -> None:
    simulation = WarehouseSimulation(
        _zones(),
        [np.int64(1), np.int64(1)],
        SimulationConfig(duration=10, arrival_rate=1, seed=np.int64(7)),
    )
    assert simulation.allocation == (1, 1)


def test_simulation_rejects_empty_duplicate_and_zero_demand_zones() -> None:
    config = _config()
    with pytest.raises(ValueError, match="at least one zone"):
        WarehouseSimulation([], [], config)
    with pytest.raises(ValueError, match="unique"):
        WarehouseSimulation(
            [ZoneConfig("A", 1, 1), ZoneConfig("A", 1, 1)],
            [1, 1],
            config,
        )
    with pytest.raises(ValueError, match="positive volume_share"):
        WarehouseSimulation(
            [ZoneConfig("A", 0, 1), ZoneConfig("B", 0, 1)],
            [1, 1],
            config,
        )


@pytest.mark.parametrize(
    "allocation",
    [
        [1],
        [1, 1, 1],
        [1, -1],
        [1, 1.5],
        [True, 1],
        [0, 0],
        {"A": 1},
        {"A": 1, "B": 1, "C": 1},
    ],
)
def test_simulation_rejects_invalid_allocations(allocation: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        WarehouseSimulation(_zones(), allocation, _config())  # type: ignore[arg-type]
