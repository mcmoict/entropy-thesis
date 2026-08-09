from types import SimpleNamespace

from entropy_thesis.simulation.spatial_metrics import sample_spatial_entropy
from entropy_thesis.simulation.worker import MovementEvent, PickEvent


def _worker(worker_id: str, *, node_id: str, start: float = 0.0, end: float = 10.0):
    return SimpleNamespace(
        movement_events=[],
        pick_events=[
            PickEvent(
                worker_id=worker_id,
                wave_number="W1",
                sequence=0,
                location_id=node_id,
                reference="P1",
                quantity_units=1.0,
                node_id=node_id,
                started_at=start,
                finished_at=end,
            )
        ],
    )


def test_spatial_entropy_is_zero_when_all_workers_share_one_cell():
    workers = {
        "A": _worker("A", node_id="N1"),
        "B": _worker("B", node_id="N1"),
    }
    samples = sample_spatial_entropy(workers, sample_seconds=5.0)
    assert samples
    assert all(sample.entropy_normalized == 0.0 for sample in samples)
    assert all(sample.max_concentration == 1.0 for sample in samples)
    assert all(sample.workers_in_shared_cells == 2 for sample in samples)


def test_spatial_entropy_is_one_when_two_workers_are_separated():
    workers = {
        "A": _worker("A", node_id="N1"),
        "B": _worker("B", node_id="N2"),
    }
    samples = sample_spatial_entropy(workers, sample_seconds=5.0)
    assert samples
    assert all(sample.entropy_normalized == 1.0 for sample in samples)
    assert all(sample.max_concentration == 0.5 for sample in samples)
    assert all(sample.workers_in_shared_cells == 0 for sample in samples)


def test_moving_worker_uses_undirected_edge_cell():
    worker = SimpleNamespace(
        movement_events=[
            MovementEvent("A", "W1", "N2", "N1", 1.0, 0.0, 5.0),
        ],
        pick_events=[],
    )
    samples = sample_spatial_entropy({"A": worker}, sample_seconds=1.0)
    assert len(samples) == 5
    assert all(sample.active_workers == 1 for sample in samples)


def test_cell_occupancy_detects_exact_shared_time():
    from entropy_thesis.simulation.spatial_metrics import aggregate_cell_occupancy

    workers = {
        "A": _worker("A", node_id="N1", start=0.0, end=10.0),
        "B": _worker("B", node_id="N1", start=2.0, end=8.0),
    }
    occupancy = aggregate_cell_occupancy(workers)
    node = next(cell for cell in occupancy if cell.cell_id == "NODE:N1")
    assert node.worker_seconds == 16.0
    assert node.occupied_seconds == 10.0
    assert node.congested_seconds == 6.0
    assert node.max_concurrent_workers == 2
