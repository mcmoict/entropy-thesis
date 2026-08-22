from __future__ import annotations

import pandas as pd
import pytest

from entropy_thesis.simulation.data_loader import PickTask, PickingList
from entropy_thesis.simulation.phase3 import (
    allocate_phase3_workers,
    build_aisle_zones,
    build_micro_zones,
    classify_picking_lists_by_zone,
    macro_zone_demand_profiles,
    microzone_for_location,
    zone_workload,
)

from test_phase1_warehouse import build_tiny_warehouse


class SimpleOrderLine:
    def __init__(self, creation_date: pd.Timestamp):
        self.creation_date = creation_date
        self.order_number = "O1"


def _list(wave: str, operator: str, locations: list[str]) -> PickingList:
    picks = tuple(
        PickTask(
            wave,
            operator,
            sequence,
            sequence,
            f"P{sequence}",
            9.0,
            1.0,
            location,
        )
        for sequence, location in enumerate(locations)
    )
    return PickingList(
        wave,
        operator,
        picks,
        (SimpleOrderLine(pd.Timestamp("2023-01-05 08:00")),),
    )


def test_build_aisle_zones_uses_20_microzones_and_four_fixed_macrozones():
    warehouse = build_tiny_warehouse()
    microzones = build_micro_zones(warehouse)
    zones = build_aisle_zones(warehouse, number_of_zones=4)

    assert len(microzones) == 20
    assert microzones[0].support_label == "LC-08"
    assert microzones[9].support_label == "LC-17"
    assert microzones[10].support_label == "RC-08"
    assert microzones[-1].support_label == "RC-17"
    assert [zone.zone_id for zone in zones] == ["Z01", "Z02", "Z03", "Z04"]
    assert zones[0].support_labels == ("LC-08", "LC-09", "LC-10", "LC-11", "LC-12")
    assert zones[1].support_labels == ("LC-13", "LC-14", "LC-15", "LC-16", "LC-17")
    assert zones[2].support_labels == ("RC-08", "RC-09", "RC-10", "RC-11", "RC-12")
    assert zones[3].support_labels == ("RC-13", "RC-14", "RC-15", "RC-16", "RC-17")


def test_microzone_mapping_separates_left_and_right_of_cc_line():
    warehouse = build_tiny_warehouse()
    microzones = build_micro_zones(warehouse)
    assert microzone_for_location(warehouse, microzones, "A-01-11") == "M01"
    assert microzone_for_location(warehouse, microzones, "B-01-11") == "M12"


def test_classification_keeps_list_intact_and_uses_dominant_macrozone():
    warehouse = build_tiny_warehouse()
    zones = build_aisle_zones(warehouse, number_of_zones=4)
    lists = [
        _list("W1", "A", ["A-01-11", "A-01-11", "B-01-11"]),
        _list("W2", "B", ["B-01-11"]),
    ]

    assignments = classify_picking_lists_by_zone(warehouse, lists, zones)

    assert assignments[0].zone_id == "Z01"
    assert assignments[0].pick_tasks == 3
    assert assignments[0].physical_zone_count == 2
    assert assignments[0].physical_microzone_count == 2
    assert assignments[0].dominant_zone_tasks == 2
    assert assignments[1].zone_id == "Z03"
    assert zone_workload(zones, assignments, basis="tasks") == (3.0, 0.0, 1.0, 0.0)


def test_macro_profile_reports_low_entropy_for_concentrated_micro_demand():
    warehouse = build_tiny_warehouse()
    zones = build_aisle_zones(warehouse)
    lists = [_list("W1", "A", ["A-01-11", "A-01-11", "A-01-11"])]
    profiles = macro_zone_demand_profiles(warehouse, lists, zones)
    z01 = profiles[0]
    assert z01.microzone_entropy_normalized == pytest.approx(0.0)
    assert z01.microzone_concentration == pytest.approx(1.0)


def test_equal_and_volume_allocations_only_use_active_zones():
    workloads = (0.0, 20.0, 10.0, 0.0)

    equal = allocate_phase3_workers(
        "equal",
        total_workers=6,
        workloads=workloads,
        minimum_per_active_zone=1,
    )
    proportional = allocate_phase3_workers(
        "volume_proportional",
        total_workers=6,
        workloads=workloads,
        minimum_per_active_zone=1,
    )

    assert equal == (0, 3, 3, 0)
    assert proportional == (0, 4, 2, 0)
    assert sum(equal) == 6
    assert sum(proportional) == 6


def test_random_allocation_is_reproducible_and_preserves_minimum():
    first = allocate_phase3_workers(
        "random",
        total_workers=8,
        workloads=(1.0, 2.0, 3.0),
        seed=123,
        minimum_per_active_zone=1,
    )
    second = allocate_phase3_workers(
        "random",
        total_workers=8,
        workloads=(1.0, 2.0, 3.0),
        seed=123,
        minimum_per_active_zone=1,
    )

    assert first == second
    assert sum(first) == 8
    assert all(count >= 1 for count in first)


def test_phase3_rejects_entropy_method_until_phase4():
    with pytest.raises(ValueError, match="Phase 3"):
        allocate_phase3_workers(
            "entropy_based",
            total_workers=4,
            workloads=(1.0, 1.0),
        )


def test_tiny_phase3_method_executes_every_original_list():
    from datetime import date

    from entropy_thesis.simulation.phase2 import calculate_demand_entropy
    from entropy_thesis.simulation.phase3 import run_phase3_method

    warehouse = build_tiny_warehouse()
    zones = build_aisle_zones(warehouse, number_of_zones=4)
    lists = [
        _list("W1", "ORIGINAL_A", ["A-01-11", "A-01-11"]),
        _list("W2", "ORIGINAL_B", ["B-01-11"]),
        _list("W3", "ORIGINAL_A", ["A-01-11", "B-01-11"]),
    ]
    assignments = classify_picking_lists_by_zone(warehouse, lists, zones)
    workloads = zone_workload(zones, assignments, basis="tasks")
    worker_counts = allocate_phase3_workers(
        "equal",
        total_workers=2,
        workloads=workloads,
        minimum_per_active_zone=1,
    )
    demand_entropy, _ = calculate_demand_entropy(warehouse, lists)

    result = run_phase3_method(
        warehouse,
        lists,
        zones,
        assignments,
        method="equal",
        worker_counts=worker_counts,
        selected_date=date(2023, 1, 5),
        demand_entropy=demand_entropy,
        walking_speed_mps=1.0,
        pick_seconds_per_unit=1.0,
        edge_capacity=1,
        pick_node_capacity=1,
        sample_seconds=0.5,
        return_to_io=True,
    )

    assert len(result.executions) == len(lists)
    assert result.summary.picking_lists == len(lists)
    assert result.summary.pick_tasks == sum(len(p.picks) for p in lists)
    assert result.summary.picked_units == pytest.approx(5.0)
    expected_flow_times = [
        event.finished_at_seconds - event.released_at_seconds
        for event in result.executions
    ]
    expected_makespan = (
        max(event.finished_at_seconds for event in result.executions)
        - min(event.released_at_seconds for event in result.executions)
    )
    assert result.summary.mean_flow_time_seconds == pytest.approx(
        sum(expected_flow_times) / len(expected_flow_times)
    )
    assert result.summary.makespan_seconds == pytest.approx(expected_makespan)
    assert all(event.release_delay_seconds >= 0 for event in result.executions)
    assert all(event.assigned_worker.startswith("EQUAL:") for event in result.executions)
    assert [p.operator for p in lists] == ["ORIGINAL_A", "ORIGINAL_B", "ORIGINAL_A"]


def test_observed_baseline_preserves_original_operator_assignment():
    from datetime import date

    from entropy_thesis.simulation.phase2 import calculate_demand_entropy
    from entropy_thesis.simulation.phase3 import run_phase3_observed_baseline

    warehouse = build_tiny_warehouse()
    zones = build_aisle_zones(warehouse, number_of_zones=4)
    lists = [
        _list("W1", "ORIGINAL_A", ["A-01-11", "A-01-11"]),
        _list("W2", "ORIGINAL_B", ["B-01-11"]),
        _list("W3", "ORIGINAL_A", ["A-01-11", "B-01-11"]),
    ]
    assignments = classify_picking_lists_by_zone(warehouse, lists, zones)
    demand_entropy, _ = calculate_demand_entropy(warehouse, lists)

    result = run_phase3_observed_baseline(
        warehouse,
        lists,
        zones,
        assignments,
        selected_date=date(2023, 1, 5),
        demand_entropy=demand_entropy,
        walking_speed_mps=1.0,
        pick_seconds_per_unit=1.0,
        edge_capacity=1,
        pick_node_capacity=1,
        sample_seconds=0.5,
        return_to_io=True,
    )

    assert result.method == "baseline"
    assert result.worker_counts == ()
    assert result.summary.total_workers == 2
    assert len(result.executions) == len(lists)
    assert {event.assigned_worker for event in result.executions} == {
        "ORIGINAL_A",
        "ORIGINAL_B",
    }
    assert all(
        event.assigned_worker == event.original_operator for event in result.executions
    )
    assert result.summary.mean_flow_time_seconds > 0.0
    assert result.summary.makespan_seconds > 0.0
