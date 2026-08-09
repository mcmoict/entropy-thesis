# 최초버전으로 삭제해도 되는 소스임..
from pathlib import Path

import simpy

from entropy_thesis.simulation import (
    WarehouseGraph,
    create_workers_from_picking_lists,
    load_picking_lists,
    load_storage_locations,
    load_support_points,
)


DATA_DIR = Path("data/raw")

storage = load_storage_locations(DATA_DIR / "Storage_Location.csv")
supports = load_support_points(DATA_DIR / "Support_Points_Navigation.csv")
picking_lists = load_picking_lists(
    DATA_DIR / "Picking_Wave.csv",
    DATA_DIR / "Customer_Order.csv",
)

warehouse = WarehouseGraph.build(storage, supports)
env = simpy.Environment()
workers = create_workers_from_picking_lists(env, warehouse, picking_lists)

# Phase 1 검증용: 각 operator의 첫 wave 하나만 실행한다.
started: set[str] = set()
for picking_list in picking_lists:
    operator = picking_list.operator
    if operator is None or operator in started or not picking_list.picks:
        continue
    env.process(workers[operator].pick(picking_list))
    started.add(operator)

env.run()

for worker_id, worker in workers.items():
    if worker_id not in started:
        continue
    print(
        f"{worker_id}: distance={worker.total_distance_m:.2f}m, "
        f"picked={worker.total_picked_units:.0f}, "
        f"movement_events={len(worker.movement_events)}"
    )
