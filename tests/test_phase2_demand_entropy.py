from entropy_thesis.simulation.data_loader import PickTask, PickingList
from entropy_thesis.simulation.phase2 import calculate_demand_entropy

from test_phase1_warehouse import build_tiny_warehouse


def test_demand_entropy_reflects_pick_node_dispersion():
    warehouse = build_tiny_warehouse()
    dispersed = [
        PickingList(
            "W1",
            "A",
            (
                PickTask("W1", "A", 0, 0, "P1", 9.0, 1.0, "A-01-11"),
                PickTask("W1", "A", 1, 1, "P2", 9.0, 1.0, "B-01-11"),
            ),
        )
    ]
    concentrated = [
        PickingList(
            "W1",
            "A",
            (
                PickTask("W1", "A", 0, 0, "P1", 9.0, 1.0, "A-01-11"),
                PickTask("W1", "A", 1, 1, "P2", 9.0, 1.0, "A-01-11"),
            ),
        )
    ]

    dispersed_metrics, _ = calculate_demand_entropy(warehouse, dispersed)
    concentrated_metrics, _ = calculate_demand_entropy(warehouse, concentrated)

    # Normalization uses all warehouse pick nodes (4), not only the 2 demand-used nodes.
    assert dispersed_metrics.task_entropy_normalized == 0.5
    assert concentrated_metrics.task_entropy_normalized == 0.0
