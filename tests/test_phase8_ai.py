from __future__ import annotations

import pandas as pd
import pytest

from entropy_thesis.simulation.phase8 import (
    TARGET_METRICS,
    adaptive_selection_records,
    build_phase8_feature_dataset,
    chronological_date_split,
)


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    daily_rows = []
    allocation_rows = []
    for day_index in range(6):
        day = f"2023-01-{day_index + 1:02d}"
        for weight, allocation_id, counts, flow, conflicts, wait, ratio in (
            (0.0, "A001", (3, 2), 100.0, 20.0, 40.0, 0.10),
            (0.25, "A002", (2, 3), 105.0, 15.0, 25.0, 0.07),
        ):
            daily_rows.append(
                {
                    "selected_date": day,
                    "entropy_weight": weight,
                    "allocation_id": allocation_id,
                    "picking_lists": 20 + day_index,
                    "pick_tasks": 100 + day_index,
                    "picked_units": 100.0 + day_index,
                    "observed_workers": 5,
                    "effective_workers": 5,
                    "active_zones": 2,
                    "D_demand_mismatch": 0.1 if weight == 0.0 else 0.2,
                    "R_congestion_risk": 2.0 if weight == 0.0 else 1.0,
                    "moved_workers_from_volume": 0 if weight == 0.0 else 1,
                    "mean_flow_time_seconds": flow + day_index,
                    "congestion_conflicts": conflicts,
                    "congestion_wait_seconds": wait,
                    "congestion_delay_ratio": ratio,
                    "makespan_seconds": 1000.0 + day_index,
                    "mean_release_delay_seconds": 50.0,
                }
            )
            for zone_id, share, concentration, workers in zip(
                ("Z01", "Z02"),
                (0.6, 0.4),
                (0.1, 0.8),
                counts,
                strict=True,
            ):
                allocation_rows.append(
                    {
                        "selected_date": day,
                        "entropy_weight": weight,
                        "allocation_id": allocation_id,
                        "zone_id": zone_id,
                        "workload_share": share,
                        "microzone_concentration": concentration,
                        "workers": workers,
                        "worker_share": workers / 5.0,
                    }
                )
    return pd.DataFrame(daily_rows), pd.DataFrame(allocation_rows)


def test_feature_dataset_is_tabular_and_excludes_lambda_from_model_features() -> None:
    daily, allocations = _frames()
    frame, features = build_phase8_feature_dataset(daily, allocations)
    assert len(frame) == 12
    assert "entropy_weight" not in features
    assert "workers_Z01" in features
    assert "workload_share_Z02" in features
    assert "macro_workload_entropy_normalized" in features
    assert all(target in frame.columns for target in TARGET_METRICS)


def test_chronological_split_keeps_dates_disjoint() -> None:
    daily, allocations = _frames()
    frame, _ = build_phase8_feature_dataset(daily, allocations)
    train_dates, validation_dates = chronological_date_split(frame, validation_ratio=0.25)
    assert set(train_dates).isdisjoint(validation_dates)
    assert max(train_dates) < min(validation_dates)


def test_adaptive_selection_uses_predictions_but_evaluates_actual_rows() -> None:
    daily, allocations = _frames()
    frame, _ = build_phase8_feature_dataset(daily, allocations)
    one_day = frame[frame["selected_date"].eq("2023-01-01")].copy()
    predictions = one_day[["selected_date", "entropy_weight", "allocation_id"]].copy()
    # Make 0.25 the predicted knee/best trade-off candidate.
    for target in TARGET_METRICS:
        predictions[f"predicted_{target}"] = one_day[target].to_numpy()
    selection = adaptive_selection_records(one_day, predictions, fixed_lambda=0.25)
    assert len(selection) == 1
    assert selection.iloc[0]["fixed_entropy_weight"] == pytest.approx(0.25)
    assert selection.iloc[0]["ai_mean_flow_time_seconds"] in {100.0, 105.0}
    assert "oracle_entropy_weight" in selection.columns
