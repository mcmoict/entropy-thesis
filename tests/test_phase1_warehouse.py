from entropy_thesis.simulation.data_loader import StorageLocation, SupportPoint
from entropy_thesis.simulation.warehouse import WarehouseGraph


def storage(location_id: str, x: float, y: float, level: int = 1) -> StorageLocation:
    return StorageLocation(location_id, x, y, level, x * 100, y * 100, float(level))


def support(label: str, x: float, y: float) -> SupportPoint:
    corridor, index = label.split("-")
    return SupportPoint(
        point_id=f"SUP:{label}",
        label=label,
        corridor=corridor,
        corridor_index=int(index),
        x_m=x,
        y_m=y,
        level=1,
        raw_x=x * 100,
        raw_y=y * 100,
        raw_z=1.0,
    )


def build_tiny_warehouse() -> WarehouseGraph:
    supports = [
        support("LC-01", 0.0, 0.0),
        support("CC-01", 4.0, 0.0),
        support("LC-02", 0.0, 1.0),
        support("CC-02", 4.0, 1.0),
    ]
    locations = [
        storage("A-01-11", 1.0, 0.1),
        storage("B-01-11", 3.0, 0.9),
    ]
    return WarehouseGraph.build(locations, supports)


def test_graph_is_connected_and_routes_along_aisles():
    warehouse = build_tiny_warehouse()
    assert warehouse.stats().connected_components == 1
    route = warehouse.route_between_locations("A-01-11", "B-01-11")
    assert route.distance_m > 0
    assert warehouse.default_start_node() == "SUP:CC-01"


def test_unknown_location_is_not_invented():
    warehouse = build_tiny_warehouse()
    result = warehouse.resolve_location("RC-01")
    assert not result.resolved
    assert result.node_id is None


def test_deterministic_build_is_stable_when_storage_input_order_changes():
    supports = [
        support("LC-01", 0.0, 0.0),
        support("CC-01", 4.0, 0.0),
        support("LC-02", 0.0, 1.0),
        support("CC-02", 4.0, 1.0),
    ]
    locations = [
        storage("A-01-11", 1.0, 0.1),
        storage("A-01-12", 1.0, 0.1),
        storage("B-01-11", 3.0, 0.9),
    ]
    first = WarehouseGraph.build(locations, supports, deterministic_order=True)
    second = WarehouseGraph.build(
        list(reversed(locations)), supports, deterministic_order=True
    )

    first_edges = {frozenset(edge) for edge in first.graph.edges}
    second_edges = {frozenset(edge) for edge in second.graph.edges}
    assert first_edges == second_edges
