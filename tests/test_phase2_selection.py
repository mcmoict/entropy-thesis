from datetime import date

import pandas as pd

from entropy_thesis.simulation.data_loader import PickTask, PickingList
from entropy_thesis.simulation.phase2 import select_phase2_lists

from test_phase1_warehouse import build_tiny_warehouse


def _list(wave: str, operator: str, location: str, created: str) -> PickingList:
    task = PickTask(wave, operator, 0, 0, "P1", 9.0, 1.0, location)
    order_line = SimpleOrderLine(pd.Timestamp(created))
    return PickingList(wave, operator, (task,), (order_line,))


class SimpleOrderLine:
    def __init__(self, creation_date: pd.Timestamp):
        self.creation_date = creation_date
        self.order_number = "O1"


def test_phase2_selection_keeps_only_fully_resolvable_lists():
    warehouse = build_tiny_warehouse()
    lists = [
        _list("W1", "A", "A-01-11", "2023-01-05 08:00"),
        _list("W2", "A", "RC-01", "2023-01-05 08:05"),
    ]
    selected_date, selected = select_phase2_lists(
        warehouse, lists, target_date=date(2023, 1, 5)
    )
    assert selected_date == date(2023, 1, 5)
    assert [p.wave_number for p in selected] == ["W1"]
