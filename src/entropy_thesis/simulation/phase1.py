from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import simpy

from .data_loader import (
    DEFAULT_COORDINATE_UNIT,
    DatasetBundle,
    PickingList,
    coordinate_scale_to_meter,
    load_dataset,
)
from .warehouse import WarehouseGraph
from .worker import Worker, create_workers_from_picking_lists


@dataclass(frozen=True)
class Phase1Audit:
    picking_lists: int
    pick_tasks: int
    resolved_pick_tasks: int
    unresolved_pick_tasks: int
    fully_resolvable_lists: int
    picking_level_above_2_tasks: int
    unresolved_locations: tuple[tuple[str, int], ...]

    @property
    def resolution_rate(self) -> float:
        if self.pick_tasks == 0:
            return 0.0
        return self.resolved_pick_tasks / self.pick_tasks


def audit_picking_locations(
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList] | tuple[PickingList, ...],
) -> Phase1Audit:
    total = 0
    resolved = 0
    fully_resolvable = 0
    unresolved_counter: Counter[str] = Counter()
    level_above_2_tasks = 0

    for picking_list in picking_lists:
        list_ok = True
        for task in picking_list.picks:
            total += 1
            if warehouse.has_location(task.location_id):
                resolved += 1
                if warehouse.storage_by_id[task.location_id].level > 2:
                    level_above_2_tasks += 1
            else:
                list_ok = False
                unresolved_counter[task.location_id] += 1
        if list_ok:
            fully_resolvable += 1

    return Phase1Audit(
        picking_lists=len(picking_lists),
        pick_tasks=total,
        resolved_pick_tasks=resolved,
        unresolved_pick_tasks=total - resolved,
        fully_resolvable_lists=fully_resolvable,
        picking_level_above_2_tasks=level_above_2_tasks,
        unresolved_locations=tuple(unresolved_counter.most_common()),
    )


def fully_resolvable_lists(
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList] | tuple[PickingList, ...],
) -> list[PickingList]:
    return [
        p
        for p in picking_lists
        if p.picks and all(warehouse.has_location(task.location_id) for task in p.picks)
    ]


def run_one_list_per_operator(
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList] | tuple[PickingList, ...],
    *,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
) -> dict[str, Worker]:
    """Phase 1 smoke test용.

    fully-resolvable list만 대상으로 각 operator의 첫 list 하나를 동시에 실행한다.
    실제 wave 시작시각/대기열/혼잡 반영은 Phase 2에서 추가한다.
    """

    candidates = fully_resolvable_lists(warehouse, picking_lists)
    env = simpy.Environment()
    workers = create_workers_from_picking_lists(
        env,
        warehouse,
        candidates,
        walking_speed_mps=walking_speed_mps,
        pick_seconds_per_unit=pick_seconds_per_unit,
        unresolved_policy="raise",
    )

    started: set[str] = set()
    for picking_list in candidates:
        if picking_list.operator in started:
            continue
        env.process(workers[picking_list.operator].pick(picking_list))
        started.add(picking_list.operator)

    env.run()
    return {operator: workers[operator] for operator in sorted(started)}


def build_phase1(data_dir: str | Path) -> tuple[DatasetBundle, WarehouseGraph, Phase1Audit]:
    dataset = load_dataset(data_dir)
    warehouse = WarehouseGraph.build(dataset.storage_locations, dataset.support_points)
    audit = audit_picking_locations(warehouse, dataset.picking_lists)
    return dataset, warehouse, audit


def main() -> None:
    parser = argparse.ArgumentParser(description="Entropy Thesis - Phase 1 validation")
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--speed", type=float, default=1.2)
    parser.add_argument("--pick-seconds", type=float, default=3.0)
    args = parser.parse_args()

    dataset, warehouse, audit = build_phase1(args.data_dir)
    stats = warehouse.stats()

    print("=== Phase 1 Dataset ===")
    print(f"Storage locations : {len(dataset.storage_locations):,}")
    print(f"Support points    : {len(dataset.support_points):,}")
    print(f"Products          : {len(dataset.products):,}")
    print(f"Customer lines    : {len(dataset.customer_orders):,}")
    print(f"Picking lists     : {len(dataset.picking_lists):,}")
    print()
    print("=== Warehouse Graph ===")
    print(f"Navigation nodes  : {stats.navigation_nodes:,}")
    print(f"Navigation edges  : {stats.navigation_edges:,}")
    print(f"Components        : {stats.connected_components}")
    io_node = warehouse.default_start_node()
    io_attrs = warehouse.graph.nodes[io_node]
    print(f"Default I/O node  : {io_node}")
    print(f"I/O coordinate(m) : ({io_attrs['x_m']:.4f}, {io_attrs['y_m']:.4f})")
    print(f"Source coord unit : {DEFAULT_COORDINATE_UNIT}")
    print(f"Scale to meter    : {coordinate_scale_to_meter(DEFAULT_COORDINATE_UNIT):.4f}")
    print()
    print("=== Picking Location Audit ===")
    print(f"Pick tasks        : {audit.pick_tasks:,}")
    print(f"Resolved          : {audit.resolved_pick_tasks:,}")
    print(f"Unresolved        : {audit.unresolved_pick_tasks:,}")
    print(f"Resolution rate   : {audit.resolution_rate:.2%}")
    excluded_lists = audit.picking_lists - audit.fully_resolvable_lists
    exclusion_rate = 0.0 if audit.picking_lists == 0 else excluded_lists / audit.picking_lists
    print(f"Fully valid lists : {audit.fully_resolvable_lists:,}/{audit.picking_lists:,}")
    print(f"Excluded lists    : {excluded_lists:,} ({exclusion_rate:.2%})")
    print(f"Level > 2 tasks   : {audit.picking_level_above_2_tasks:,}")
    print("Top unresolved    :", audit.unresolved_locations[:10])
    print()

    workers = run_one_list_per_operator(
        warehouse,
        dataset.picking_lists,
        walking_speed_mps=args.speed,
        pick_seconds_per_unit=args.pick_seconds,
    )
    print("=== SimPy Smoke Test: first valid list per operator ===")
    for operator, worker in workers.items():
        print(
            f"{operator:>11} | distance={worker.total_distance_m:7.2f} m "
            f"| picked={worker.total_picked_units:5.0f} "
            f"| moves={len(worker.movement_events):4d} "
            f"| picks={len(worker.pick_events):3d}"
        )


if __name__ == "__main__":
    main()
