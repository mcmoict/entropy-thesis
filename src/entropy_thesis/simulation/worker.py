from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

import simpy

from .data_loader import PickTask, PickingList
from .warehouse import WarehouseGraph


UnresolvedPolicy = Literal["skip", "raise"]


@dataclass(frozen=True)
class MovementEvent:
    worker_id: str
    wave_number: str
    from_node: str
    to_node: str
    distance_m: float
    started_at: float
    finished_at: float


@dataclass(frozen=True)
class PickEvent:
    worker_id: str
    wave_number: str
    sequence: int
    location_id: str
    reference: str
    quantity_units: float
    node_id: str
    started_at: float
    finished_at: float


@dataclass(frozen=True)
class UnresolvedPickEvent:
    worker_id: str
    wave_number: str
    sequence: int
    location_id: str
    reference: str
    quantity_units: float
    occurred_at: float
    reason: str


class Worker:
    """SimPy picker.

    이동은 목적지까지 한 번에 timeout하지 않고 graph edge 하나씩 진행한다.
    이 movement event가 Phase 2의 node occupancy / congestion / entropy 계산의
    입력이 된다.
    """

    def __init__(
        self,
        env: simpy.Environment,
        worker_id: str,
        warehouse: WarehouseGraph,
        *,
        start_node: str | None = None,
        walking_speed_mps: float = 1.2,
        pick_seconds_per_unit: float = 3.0,
        unresolved_policy: UnresolvedPolicy = "skip",
    ) -> None:
        if walking_speed_mps <= 0:
            raise ValueError("walking_speed_mps는 0보다 커야 합니다.")
        if pick_seconds_per_unit < 0:
            raise ValueError("pick_seconds_per_unit은 0 이상이어야 합니다.")
        if unresolved_policy not in {"skip", "raise"}:
            raise ValueError("unresolved_policy는 'skip' 또는 'raise'여야 합니다.")

        self.env = env
        self.worker_id = worker_id
        self.warehouse = warehouse
        self.current_node = start_node or warehouse.default_start_node()
        self.walking_speed_mps = walking_speed_mps
        self.pick_seconds_per_unit = pick_seconds_per_unit
        self.unresolved_policy = unresolved_policy

        self.total_distance_m = 0.0
        self.total_picked_units = 0.0
        self.movement_events: list[MovementEvent] = []
        self.pick_events: list[PickEvent] = []
        self.unresolved_pick_events: list[UnresolvedPickEvent] = []

    def move_to_node(self, target_node: str, *, wave_number: str):
        route = self.warehouse.shortest_route(self.current_node, target_node)

        for from_node, to_node in pairwise(route.nodes):
            distance_m = self.warehouse.edge_distance(from_node, to_node)
            travel_seconds = distance_m / self.walking_speed_mps
            started_at = float(self.env.now)

            yield self.env.timeout(travel_seconds)

            finished_at = float(self.env.now)
            self.current_node = to_node
            self.total_distance_m += distance_m
            self.movement_events.append(
                MovementEvent(
                    worker_id=self.worker_id,
                    wave_number=wave_number,
                    from_node=from_node,
                    to_node=to_node,
                    distance_m=distance_m,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )

    def move_to_location(self, location_id: str, *, wave_number: str):
        target_node = self.warehouse.node_for_location(location_id)
        yield self.env.process(self.move_to_node(target_node, wave_number=wave_number))

    def _record_unresolved(self, task: PickTask, reason: str) -> None:
        self.unresolved_pick_events.append(
            UnresolvedPickEvent(
                worker_id=self.worker_id,
                wave_number=task.wave_number,
                sequence=task.sequence,
                location_id=task.location_id,
                reference=task.reference,
                quantity_units=task.quantity_units,
                occurred_at=float(self.env.now),
                reason=reason,
            )
        )

    def pick_task(self, task: PickTask):
        resolution = self.warehouse.resolve_location(task.location_id)
        if not resolution.resolved or resolution.node_id is None:
            reason = resolution.reason or "unknown location"
            if self.unresolved_policy == "raise":
                raise KeyError(f"{task.location_id}: {reason}")
            self._record_unresolved(task, reason)
            return

        yield self.env.process(
            self.move_to_node(resolution.node_id, wave_number=task.wave_number)
        )

        pick_seconds = task.quantity_units * self.pick_seconds_per_unit
        started_at = float(self.env.now)
        yield self.env.timeout(pick_seconds)
        finished_at = float(self.env.now)

        self.total_picked_units += task.quantity_units
        self.pick_events.append(
            PickEvent(
                worker_id=self.worker_id,
                wave_number=task.wave_number,
                sequence=task.sequence,
                location_id=task.location_id,
                reference=task.reference,
                quantity_units=task.quantity_units,
                node_id=resolution.node_id,
                started_at=started_at,
                finished_at=finished_at,
            )
        )

    def pick(self, picking_list: PickingList):
        if picking_list.operator != self.worker_id:
            raise ValueError(
                f"PickingList operator={picking_list.operator}를 "
                f"Worker={self.worker_id}에게 실행할 수 없습니다."
            )
        for task in picking_list.picks:
            yield self.env.process(self.pick_task(task))


def create_workers_from_picking_lists(
    env: simpy.Environment,
    warehouse: WarehouseGraph,
    picking_lists: list[PickingList] | tuple[PickingList, ...],
    *,
    walking_speed_mps: float = 1.2,
    pick_seconds_per_unit: float = 3.0,
    unresolved_policy: UnresolvedPolicy = "skip",
) -> dict[str, Worker]:
    operator_ids = sorted({p.operator for p in picking_lists if p.operator})
    return {
        operator_id: Worker(
            env,
            worker_id=operator_id,
            warehouse=warehouse,
            walking_speed_mps=walking_speed_mps,
            pick_seconds_per_unit=pick_seconds_per_unit,
            unresolved_policy=unresolved_policy,
        )
        for operator_id in operator_ids
    }
