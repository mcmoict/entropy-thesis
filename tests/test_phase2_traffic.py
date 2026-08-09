import simpy

from entropy_thesis.simulation.data_loader import PickTask, PickingList
from entropy_thesis.simulation.traffic import TrafficController
from entropy_thesis.simulation.worker import Worker

from test_phase1_warehouse import build_tiny_warehouse


def test_two_workers_create_capacity_wait_on_shared_route():
    env = simpy.Environment()
    warehouse = build_tiny_warehouse()
    traffic = TrafficController(env, edge_capacity=1, pick_node_capacity=1)

    pick_a = PickTask("W1", "A", 0, 0, "P1", 9.0, 1.0, "A-01-11")
    pick_b = PickTask("W1", "B", 0, 0, "P1", 9.0, 1.0, "A-01-11")
    worker_a = Worker(
        env,
        "A",
        warehouse,
        walking_speed_mps=1.0,
        pick_seconds_per_unit=1.0,
        unresolved_policy="raise",
        traffic_controller=traffic,
    )
    worker_b = Worker(
        env,
        "B",
        warehouse,
        walking_speed_mps=1.0,
        pick_seconds_per_unit=1.0,
        unresolved_policy="raise",
        traffic_controller=traffic,
    )

    env.process(worker_a.pick(PickingList("W1", "A", (pick_a,))))
    env.process(worker_b.pick(PickingList("W1", "B", (pick_b,))))
    env.run()

    assert traffic.wait_events
    assert sum(event.wait_seconds for event in traffic.wait_events) > 0.0
    assert all(event.wait_seconds > 0.0 for event in traffic.wait_events)
