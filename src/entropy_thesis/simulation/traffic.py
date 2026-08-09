from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import simpy


ResourceKind = Literal["edge", "pick_node"]


@dataclass(frozen=True)
class CongestionWaitEvent:
    """A delay caused by contention for a shared movement/picking resource.

    The Phase 2 model does not claim that this is a physical collision.  A
    conflict is counted when a worker requests a capacity-limited edge or pick
    node and cannot enter immediately.  ``wait_seconds`` is the resulting
    simulated delay.
    """

    worker_id: str
    wave_number: str
    resource_kind: ResourceKind
    resource_id: str
    requested_at: float
    entered_at: float
    wait_seconds: float
    from_node: str | None = None
    to_node: str | None = None
    node_id: str | None = None


class TrafficController:
    """Shared SimPy resources used to model Phase 2 congestion.

    Edges are treated as undirected resources, so workers traversing the same
    aisle segment in opposite directions compete for the same capacity.  Pick
    nodes have a separate capacity used only while a pick is being performed.
    """

    def __init__(
        self,
        env: simpy.Environment,
        *,
        edge_capacity: int = 1,
        pick_node_capacity: int = 1,
        wait_epsilon: float = 1e-9,
    ) -> None:
        if edge_capacity <= 0:
            raise ValueError("edge_capacity는 1 이상이어야 합니다.")
        if pick_node_capacity <= 0:
            raise ValueError("pick_node_capacity는 1 이상이어야 합니다.")
        if wait_epsilon < 0:
            raise ValueError("wait_epsilon은 0 이상이어야 합니다.")

        self.env = env
        self.edge_capacity = int(edge_capacity)
        self.pick_node_capacity = int(pick_node_capacity)
        self.wait_epsilon = float(wait_epsilon)
        self._edge_resources: dict[tuple[str, str], simpy.Resource] = {}
        self._pick_node_resources: dict[str, simpy.Resource] = {}
        self.wait_events: list[CongestionWaitEvent] = []

    @staticmethod
    def edge_key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    @classmethod
    def edge_id(cls, a: str, b: str) -> str:
        left, right = cls.edge_key(a, b)
        return f"{left}<->{right}"

    def edge_resource(self, a: str, b: str) -> simpy.Resource:
        key = self.edge_key(a, b)
        resource = self._edge_resources.get(key)
        if resource is None:
            resource = simpy.Resource(self.env, capacity=self.edge_capacity)
            self._edge_resources[key] = resource
        return resource

    def pick_node_resource(self, node_id: str) -> simpy.Resource:
        resource = self._pick_node_resources.get(node_id)
        if resource is None:
            resource = simpy.Resource(self.env, capacity=self.pick_node_capacity)
            self._pick_node_resources[node_id] = resource
        return resource

    def record_edge_wait(
        self,
        *,
        worker_id: str,
        wave_number: str,
        from_node: str,
        to_node: str,
        requested_at: float,
        entered_at: float,
    ) -> None:
        self._record_wait(
            worker_id=worker_id,
            wave_number=wave_number,
            resource_kind="edge",
            resource_id=self.edge_id(from_node, to_node),
            requested_at=requested_at,
            entered_at=entered_at,
            from_node=from_node,
            to_node=to_node,
        )

    def record_pick_node_wait(
        self,
        *,
        worker_id: str,
        wave_number: str,
        node_id: str,
        requested_at: float,
        entered_at: float,
    ) -> None:
        self._record_wait(
            worker_id=worker_id,
            wave_number=wave_number,
            resource_kind="pick_node",
            resource_id=node_id,
            requested_at=requested_at,
            entered_at=entered_at,
            node_id=node_id,
        )

    def _record_wait(
        self,
        *,
        worker_id: str,
        wave_number: str,
        resource_kind: ResourceKind,
        resource_id: str,
        requested_at: float,
        entered_at: float,
        from_node: str | None = None,
        to_node: str | None = None,
        node_id: str | None = None,
    ) -> None:
        wait_seconds = float(entered_at - requested_at)
        if wait_seconds <= self.wait_epsilon:
            return
        self.wait_events.append(
            CongestionWaitEvent(
                worker_id=worker_id,
                wave_number=wave_number,
                resource_kind=resource_kind,
                resource_id=resource_id,
                requested_at=float(requested_at),
                entered_at=float(entered_at),
                wait_seconds=wait_seconds,
                from_node=from_node,
                to_node=to_node,
                node_id=node_id,
            )
        )


__all__ = ["CongestionWaitEvent", "ResourceKind", "TrafficController"]
