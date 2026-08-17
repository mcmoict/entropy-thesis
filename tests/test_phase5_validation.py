from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from entropy_thesis.simulation.phase5 import (
    _evenly_spaced_dates,
    aggregate_method_records,
    holdout_comparison_records,
    load_phase4_holdout_spec,
    paired_comparison_records,
    select_validation_dates,
)


def test_evenly_spaced_dates_is_deterministic_and_covers_endpoints() -> None:
    values = tuple(date(2023, 1, day) for day in range(1, 11))
    selected = _evenly_spaced_dates(values, 4)
    assert selected[0] == values[0]
    assert selected[-1] == values[-1]
    assert len(selected) == 4


def test_validation_dates_exclude_calibration_date() -> None:
    available = (
        date(2023, 1, 5),
        date(2023, 1, 6),
        date(2023, 1, 9),
        date(2023, 1, 10),
    )
    selected = select_validation_dates(
        available,
        calibration_date=date(2023, 1, 5),
        validation_days=3,
    )
    assert date(2023, 1, 5) not in selected
    assert selected == available[1:]


def test_explicit_missing_validation_date_is_rejected() -> None:
    with pytest.raises(ValueError, match="validation date"):
        select_validation_dates(
            (date(2023, 1, 5), date(2023, 1, 6)),
            calibration_date=date(2023, 1, 5),
            explicit_dates=(date(2023, 2, 1),),
        )


def _daily_frame() -> pd.DataFrame:
    rows = []
    methods = ["baseline", "random", "equal", "volume_proportional", "entropy_based"]
    for day, entropy_flow in [("2023-01-06", 90.0), ("2023-01-09", 80.0), ("2023-01-10", 70.0)]:
        for method in methods:
            flow = 100.0
            if method == "entropy_based":
                flow = entropy_flow
            elif method == "volume_proportional":
                flow = 95.0
            rows.append(
                {
                    "selected_date": day,
                    "method": method,
                    "mean_flow_time_seconds": flow,
                    "makespan_seconds": flow * 10,
                    "congestion_wait_seconds": flow,
                    "congestion_conflicts": flow,
                    "congestion_delay_ratio": flow / 1000.0,
                    "total_distance_m": flow,
                    "mean_release_delay_seconds": flow,
                    "mean_spatial_entropy_normalized": 1.0 / flow,
                    "worker_allocation_entropy_normalized": 0.5,
                    "demand_worker_l1_gap": 0.1,
                }
            )
    return pd.DataFrame(rows)


def test_aggregate_method_records_outputs_long_format_statistics() -> None:
    records = aggregate_method_records(_daily_frame())
    item = next(
        record
        for record in records
        if record["method"] == "entropy_based"
        and record["metric"] == "mean_flow_time_seconds"
    )
    assert item["n_days"] == 3
    assert item["mean"] == pytest.approx(80.0)


def test_holdout_comparison_records_outputs_phase3_style_means() -> None:
    records = holdout_comparison_records(_daily_frame())
    entropy = next(record for record in records if record["method"] == "entropy_based")
    volume = next(record for record in records if record["method"] == "volume_proportional")

    assert entropy["n_days"] == 3
    assert entropy["mean_flow_time_seconds"] == pytest.approx(80.0)
    assert entropy["congestion_conflicts"] == pytest.approx(80.0)
    assert entropy["congestion_delay_ratio"] == pytest.approx(0.08)
    assert volume["mean_flow_time_seconds"] == pytest.approx(95.0)


def test_paired_comparison_counts_entropy_wins_for_minimize_metric() -> None:
    records = paired_comparison_records(_daily_frame())
    item = next(
        record
        for record in records
        if record["comparator"] == "baseline"
        and record["metric"] == "mean_flow_time_seconds"
    )
    assert item["wins"] == 3
    assert item["ties"] == 0
    assert item["losses"] == 0
    assert item["mean_improvement_pct"] == pytest.approx(20.0)


def test_load_phase4_holdout_spec_reads_frozen_split(tmp_path) -> None:
    path = tmp_path / "phase4_recommendation.json"
    path.write_text(
        """{
  "phase": "4E",
  "selection_metric": "mean_flow_time_seconds",
  "entropy_weight": 0.05,
  "calibration_dates": ["2023-01-05", "2023-01-06"],
  "holdout_dates": ["2023-07-19", "2023-07-24"]
}
""",
        encoding="utf-8",
    )
    spec = load_phase4_holdout_spec(path)
    assert spec.entropy_weight == pytest.approx(0.05)
    assert spec.calibration_dates == (date(2023, 1, 5), date(2023, 1, 6))
    assert spec.holdout_dates == (date(2023, 7, 19), date(2023, 7, 24))


def test_load_phase4_holdout_spec_rejects_overlap(tmp_path) -> None:
    path = tmp_path / "phase4_recommendation.json"
    path.write_text(
        """{
  "phase": "4E",
  "selection_metric": "mean_flow_time_seconds",
  "entropy_weight": 0.05,
  "calibration_dates": ["2023-01-05"],
  "holdout_dates": ["2023-01-05"]
}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="겹칩니다"):
        load_phase4_holdout_spec(path)
