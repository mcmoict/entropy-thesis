from pathlib import Path

import pandas as pd
import pytest

from entropy_thesis.simulation.data_loader import (
    load_picking_lists,
    load_storage_locations,
    load_support_points,
)


def test_actual_storage_schema(tmp_path: Path):
    path = tmp_path / "Storage_Location.csv"
    pd.DataFrame(
        [{"originalLocation": "A-14-11", "position": "368, 0, 1", "x": 368, "y": 0, "z": 1}]
    ).to_csv(path, index=False)
    rows = load_storage_locations(path)
    assert rows[0].location_id == "A-14-11"
    assert rows[0].x_m == pytest.approx(9.3472)
    assert rows[0].level == 1


def test_actual_support_schema(tmp_path: Path):
    path = tmp_path / "Support_Points_Navigation.csv"
    pd.DataFrame(
        [
            {"points_specified": "(66.0, -29.0, 1.0)", "labels": "LC-01"},
            {"points_specified": "(403.0, -29.0, 1.0)", "labels": "CC-01"},
        ]
    ).to_csv(path, sep=";", index=False)
    rows = load_support_points(path)
    assert rows[0].point_id == "SUP:LC-01"
    assert rows[1].x_m == pytest.approx(10.2362)


def test_picking_list_is_split_by_operator_and_preserves_order(tmp_path: Path):
    path = tmp_path / "Picking_Wave.csv"
    pd.DataFrame(
        [
            {"waveNumber": 1, "reference": "P1", "Size (US)": 9, "quantityToPick (units)": 1, "locations": "A-01-11", "operator": "Operator_1"},
            {"waveNumber": 1, "reference": "P2", "Size (US)": 10, "quantityToPick (units)": 1, "locations": "B-01-11", "operator": "Operator_2"},
            {"waveNumber": 1, "reference": "P3", "Size (US)": 11, "quantityToPick (units)": 2, "locations": "C-01-11", "operator": "Operator_1"},
        ]
    ).to_csv(path, sep=";", index=False)

    lists = load_picking_lists(path)
    assert len(lists) == 2
    op1 = next(p for p in lists if p.operator == "Operator_1")
    assert [p.reference for p in op1.picks] == ["P1", "P3"]
    assert [p.wave_sequence for p in op1.picks] == [0, 2]
