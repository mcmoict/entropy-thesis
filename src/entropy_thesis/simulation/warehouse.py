from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
import heapq
from typing import Iterable

import networkx as nx

from .data_loader import StorageLocation, SupportPoint


@dataclass(frozen=True)
class Route:
    nodes: tuple[str, ...]
    distance_m: float


@dataclass(frozen=True)
class LocationResolution:
    location_id: str
    node_id: str | None
    resolved: bool
    reason: str | None = None


@dataclass(frozen=True)
class WarehouseGraphStats:
    storage_locations: int
    support_points: int
    navigation_nodes: int
    navigation_edges: int
    connected_components: int


class WarehouseGraph:
    """Storage_Location + Support_Points 기반의 작업자 보행 Graph.

    핵심 모델링 규칙
    ---------------
    1. Support Point(LC/CC/RC)는 corridor 교차점이다.
    2. 각 storage location은 가장 가까운 support-point y 좌표의 picking aisle로
       투영한다. 즉 작업자는 랙 좌표 자체를 관통하지 않고 통로 중앙을 이동한다.
    3. 같은 picking aisle(y)에 있는 access node와 support point를 x순으로 연결한다.
    4. 같은 corridor(LC/CC/RC)의 support point는 y순으로 연결한다.
    5. z는 랙 level이므로 보행거리 계산에서는 사용하지 않는다.

    이 방식은 'parallel sorting aisles + cross aisles + central cross aisle' 구조를
    Support_Points_Navigation.csv만으로 재구성하기 위한 Phase-1 모델이다.
    """

    def __init__(
        self,
        graph: nx.Graph,
        location_nodes: dict[str, str],
        support_nodes: dict[str, str],
        storage_by_id: dict[str, StorageLocation],
    ) -> None:
        self.graph = graph
        self.location_nodes = location_nodes
        self.support_nodes = support_nodes
        self.storage_by_id = storage_by_id

    @classmethod
    def build(
        cls,
        storage_locations: Iterable[StorageLocation],
        support_points: Iterable[SupportPoint],
        *,
        deterministic_order: bool = False,
    ) -> "WarehouseGraph":
        storage = list(storage_locations)
        supports = list(support_points)
        if not supports:
            raise ValueError("Support Point가 하나도 없습니다.")

        graph = nx.Graph()
        support_nodes: dict[str, str] = {}
        location_nodes: dict[str, str] = {}
        storage_by_id = {loc.location_id: loc for loc in storage}

        # 1) Support points
        for point in supports:
            graph.add_node(
                point.point_id,
                kind="support",
                label=point.label,
                corridor=point.corridor,
                corridor_index=point.corridor_index,
                x_m=point.x_m,
                y_m=point.y_m,
                level=point.level,
            )
            support_nodes[point.label] = point.point_id

        # 2) LC/CC/RC 세로 corridor 연결
        corridors: dict[str, list[SupportPoint]] = {}
        for point in supports:
            corridors.setdefault(point.corridor, []).append(point)
        for points in corridors.values():
            points.sort(key=lambda p: (p.y_m, p.corridor_index))
            for a, b in pairwise(points):
                cls._add_edge(graph, a.point_id, b.point_id, edge_kind="corridor")

        # 3) 각 Storage Location을 가장 가까운 support y(피킹 aisle)에 투영한다.
        aisle_y_values = sorted({round(point.y_m, 6) for point in supports})
        aisle_nodes_by_y: dict[float, set[str]] = {}

        # support도 horizontal aisle 후보에 포함
        for point in supports:
            key = round(point.y_m, 6)
            aisle_nodes_by_y.setdefault(key, set()).add(point.point_id)

        access_node_cache: dict[tuple[float, float], str] = {}
        for loc in storage:
            aisle_y = min(aisle_y_values, key=lambda y: abs(y - loc.y_m))
            key = (round(loc.x_m, 6), round(aisle_y, 6))
            node_id = access_node_cache.get(key)
            if node_id is None:
                node_id = f"AISLE:x={key[0]:.6f}:y={key[1]:.6f}"
                access_node_cache[key] = node_id
                graph.add_node(
                    node_id,
                    kind="aisle_access",
                    x_m=loc.x_m,
                    y_m=aisle_y,
                    location_ids=[],
                )
                aisle_nodes_by_y.setdefault(round(aisle_y, 6), set()).add(node_id)

            graph.nodes[node_id]["location_ids"].append(loc.location_id)
            location_nodes[loc.location_id] = node_id

        # 4) 같은 horizontal aisle에서 x 순서로 인접 노드 연결
        for _, node_ids in aisle_nodes_by_y.items():
            ordered = sorted(
                node_ids,
                key=(
                    (lambda n: (graph.nodes[n]["x_m"], n))
                    if deterministic_order
                    else (lambda n: graph.nodes[n]["x_m"])
                ),
            )
            for a, b in pairwise(ordered):
                if a != b:
                    cls._add_edge(graph, a, b, edge_kind="picking_aisle")

        # 연결성 검증. Storage가 존재하는 access node가 메인 graph에서 끊어지면
        # Phase1 자체가 성립하지 않으므로 조용히 nearest-edge를 만들지 않고 실패시킨다.
        if not nx.is_connected(graph):
            components = list(nx.connected_components(graph))
            sizes = sorted((len(c) for c in components), reverse=True)
            raise ValueError(
                "Warehouse graph가 연결되어 있지 않습니다. "
                f"components={len(components)}, sizes={sizes[:10]}"
            )

        return cls(
            graph=graph,
            location_nodes=location_nodes,
            support_nodes=support_nodes,
            storage_by_id=storage_by_id,
        )

    @staticmethod
    def _add_edge(graph: nx.Graph, a: str, b: str, *, edge_kind: str) -> None:
        ax = float(graph.nodes[a]["x_m"])
        ay = float(graph.nodes[a]["y_m"])
        bx = float(graph.nodes[b]["x_m"])
        by = float(graph.nodes[b]["y_m"])
        distance_m = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
        # 동일 좌표 support/access 중복이 있어도 graph weight 0은 정상이다.
        graph.add_edge(
            a,
            b,
            distance_m=distance_m,
            weight=distance_m,
            kind=edge_kind,
        )

    def resolve_location(self, location_id: str) -> LocationResolution:
        location_id = str(location_id).strip()
        node_id = self.location_nodes.get(location_id)
        if node_id is None:
            return LocationResolution(
                location_id=location_id,
                node_id=None,
                resolved=False,
                reason="Storage_Location.csv에 좌표가 없는 Picking_Wave location",
            )
        return LocationResolution(location_id, node_id, True, None)

    def has_location(self, location_id: str) -> bool:
        return str(location_id).strip() in self.location_nodes

    def node_for_location(self, location_id: str) -> str:
        resolved = self.resolve_location(location_id)
        if not resolved.resolved or resolved.node_id is None:
            raise KeyError(f"알 수 없는 Storage Location: {location_id}")
        return resolved.node_id

    def default_start_node(self) -> str:
        """Phase 1 기본 I/O 지점.

        데이터 논문은 Warehouse I/O를 central hub로 설명한다. 데이터에 명시적인
        I/O label은 없으므로 중앙 corridor의 최하단 CC-01을 우선 사용한다.
        """

        if "CC-01" in self.support_nodes:
            return self.support_nodes["CC-01"]
        if "LC-01" in self.support_nodes:
            return self.support_nodes["LC-01"]
        return min(
            self.support_nodes.values(),
            key=lambda node: (self.graph.nodes[node]["y_m"], self.graph.nodes[node]["x_m"]),
        )

    def shortest_route(self, start_node: str, end_node: str) -> Route:
        nodes = nx.shortest_path(self.graph, start_node, end_node, weight="weight")
        distance = nx.shortest_path_length(
            self.graph, start_node, end_node, weight="weight"
        )
        return Route(nodes=tuple(nodes), distance_m=float(distance))

    def deterministic_shortest_route(self, start_node: str, end_node: str) -> Route:
        """Return a reproducible shortest route for edge-level Phase 2 metrics.

        The primary objective is the original metric distance.  When multiple
        paths have exactly the same distance, the route with fewer edges is
        preferred and then the lexicographically smaller node sequence is
        chosen.  This prevents Python hash/set iteration order from changing
        Phase 2 congestion results while leaving the Phase 1 routing method
        untouched.
        """

        if start_node not in self.graph or end_node not in self.graph:
            raise nx.NodeNotFound(f"unknown route endpoint: {start_node}, {end_node}")
        if start_node == end_node:
            return Route(nodes=(start_node,), distance_m=0.0)

        start_path = (start_node,)
        best: dict[str, tuple[float, int, tuple[str, ...]]] = {
            start_node: (0.0, 0, start_path)
        }
        queue: list[tuple[float, int, tuple[str, ...], str]] = [
            (0.0, 0, start_path, start_node)
        ]

        while queue:
            distance, hops, path, node = heapq.heappop(queue)
            state = (distance, hops, path)
            if best.get(node) != state:
                continue
            if node == end_node:
                return Route(nodes=path, distance_m=distance)

            for neighbor in sorted(self.graph.neighbors(node)):
                edge_distance = float(self.graph.edges[node, neighbor]["distance_m"])
                candidate = (
                    distance + edge_distance,
                    hops + 1,
                    path + (neighbor,),
                )
                previous = best.get(neighbor)
                if previous is None or candidate < previous:
                    best[neighbor] = candidate
                    heapq.heappush(
                        queue,
                        (candidate[0], candidate[1], candidate[2], neighbor),
                    )

        raise nx.NetworkXNoPath(f"No path between {start_node} and {end_node}")

    def route_between_locations(self, start_location: str, end_location: str) -> Route:
        return self.shortest_route(
            self.node_for_location(start_location),
            self.node_for_location(end_location),
        )

    def edge_distance(self, a: str, b: str) -> float:
        return float(self.graph.edges[a, b]["distance_m"])

    def stats(self) -> WarehouseGraphStats:
        return WarehouseGraphStats(
            storage_locations=len(self.location_nodes),
            support_points=len(self.support_nodes),
            navigation_nodes=self.graph.number_of_nodes(),
            navigation_edges=self.graph.number_of_edges(),
            connected_components=nx.number_connected_components(self.graph),
        )
