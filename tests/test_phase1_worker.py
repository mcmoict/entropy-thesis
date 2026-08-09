import simpy

from entropy_thesis.simulation.data_loader import PickTask, PickingList
from entropy_thesis.simulation.worker import Worker

from test_phase1_warehouse import build_tiny_warehouse


def test_worker_moves_edge_by_edge_and_picks():
    env = simpy.Environment()
    warehouse = build_tiny_warehouse()
    picking_list = PickingList(
        wave_number="W1",
        operator="Operator_1",
        picks=(
            PickTask("W1", "Operator_1", 0, 0, "P1", 9.0, 1.0, "A-01-11"),
            PickTask("W1", "Operator_1", 1, 1, "P2", 10.0, 2.0, "B-01-11"),
        ),
    )

    worker = Worker(
        env,
        "Operator_1",
        warehouse,
        walking_speed_mps=1.0,
        pick_seconds_per_unit=1.0,
        unresolved_policy="raise",
    )
    env.process(worker.pick(picking_list))
    env.run()

    assert worker.total_distance_m > 0
    assert worker.total_picked_units == 3
    assert len(worker.movement_events) > 1
    assert len(worker.pick_events) == 2
    assert worker.unresolved_pick_events == []
