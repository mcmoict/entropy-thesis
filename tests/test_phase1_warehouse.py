from entropy_thesis.simulation.data_loader import StorageLocation, SupportPoint
from entropy_thesis.simulation.warehouse import WarehouseGraph


def storage(location_id: str, x: float, y: float, level: int = 1) -> StorageLocation:
    return StorageLocation(location_id, x, y, level, x / 0.0254, y / 0.0254, float(level))


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
        raw_x=x / 0.0254,
        raw_y=y / 0.0254,
        raw_z=1.0,
    )


def build_tiny_warehouse() -> WarehouseGraph:
    # Tiny synthetic graph that still contains the thesis model's required
    # active anchors LC/CC/RC-08..17 and the fixed CC-08 depot.
    supports = []
    for index in range(8, 18):
        y = float(index - 8)
        supports.extend(
            [
                support(f"LC-{index:02d}", 0.0, y),
                support(f"CC-{index:02d}", 4.0, y),
                support(f"RC-{index:02d}", 8.0, y),
            ]
        )
    locations = [
        storage("A-01-11", 1.0, 0.1),   # left / near -> Z01
        storage("B-01-11", 7.0, 0.9),   # right / near -> Z03
        storage("C-01-11", 1.0, 7.9),   # left / far -> Z02
        storage("D-01-11", 7.0, 8.1),   # right / far -> Z04
    ]
    return WarehouseGraph.build(locations, supports)


def test_graph_is_connected_and_routes_along_aisles():
    warehouse = build_tiny_warehouse()
    assert warehouse.stats().connected_components == 1
    route = warehouse.route_between_locations("A-01-11", "B-01-11")
    assert route.distance_m > 0
    assert warehouse.default_start_node() == "SUP:CC-08"


def test_unknown_location_is_not_invented():
    warehouse = build_tiny_warehouse()
    result = warehouse.resolve_location("RC-01")
    assert not result.resolved
    assert result.node_id is None


def test_missing_cc08_is_rejected_as_invalid_dataset_model():
    warehouse = WarehouseGraph.build(
        [storage("A-01-11", 1.0, 0.1)],
        [support("LC-01", 0.0, 0.0), support("CC-01", 4.0, 0.0)],
    )
    try:
        warehouse.default_start_node()
    except ValueError as exc:
        assert "CC-08" in str(exc)
    else:
        raise AssertionError("CC-08가 없으면 fallback하지 않고 오류가 발생해야 합니다.")


def test_deterministic_build_is_stable_when_storage_input_order_changes():
    supports = []
    for index in range(8, 18):
        y = float(index - 8)
        supports.extend(
            [
                support(f"LC-{index:02d}", 0.0, y),
                support(f"CC-{index:02d}", 4.0, y),
                support(f"RC-{index:02d}", 8.0, y),
            ]
        )
    locations = [
        storage("A-01-11", 1.0, 0.1),
        storage("A-01-12", 1.1, 0.1),
        storage("B-01-11", 7.0, 0.9),
    ]
    first = WarehouseGraph.build(locations, supports, deterministic_order=True)
    second = WarehouseGraph.build(
        list(reversed(locations)), supports, deterministic_order=True
    )

    first_edges = {frozenset(edge) for edge in first.graph.edges}
    second_edges = {frozenset(edge) for edge in second.graph.edges}
    assert first_edges == second_edges
