from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from entropy_thesis.simulation.phase6 import (
    Phase6Inputs,
    changed_date_records,
    changed_zone_detail_records,
    generalization_summary,
    holm_adjust,
    pareto_metric_statistics,
    proxy_validation_records,
)


def _daily() -> pd.DataFrame:
    rows = []
    for day, flow_e, flow_v, conflicts_e, conflicts_v, wait_e, wait_v, ratio_e, ratio_v in (
        ("2023-07-19", 110.0, 100.0, 8.0, 10.0, 16.0, 20.0, 0.08, 0.10),
        ("2023-07-20", 90.0, 90.0, 9.0, 9.0, 18.0, 18.0, 0.09, 0.09),
        ("2023-07-21", 95.0, 100.0, 12.0, 10.0, 22.0, 20.0, 0.11, 0.10),
    ):
        common = {
            "mean_release_delay_seconds": 50.0,
            "makespan_seconds": 1000.0,
            "mean_spatial_entropy_multiworker": 0.9,
            "mean_spatial_entropy_normalized": 0.8,
        }
        rows.append(
            {
                "selected_date": day,
                "method": "entropy_based",
                "mean_flow_time_seconds": flow_e,
                "congestion_conflicts": conflicts_e,
                "congestion_wait_seconds": wait_e,
                "congestion_delay_ratio": ratio_e,
                **common,
            }
        )
        rows.append(
            {
                "selected_date": day,
                "method": "volume_proportional",
                "mean_flow_time_seconds": flow_v,
                "congestion_conflicts": conflicts_v,
                "congestion_wait_seconds": wait_v,
                "congestion_delay_ratio": ratio_v,
                **common,
            }
        )
    return pd.DataFrame(rows)


def _allocations() -> pd.DataFrame:
    rows = []
    for method, workers in (("volume_proportional", (1, 2)), ("entropy_based", (2, 1))):
        for zone_id, workload, share, concentration, worker in zip(
            ("Z01", "Z02"),
            (60.0, 40.0),
            (0.6, 0.4),
            (0.1, 0.8),
            workers,
            strict=True,
        ):
            rows.append(
                {
                    "selected_date": "2023-07-19",
                    "method": method,
                    "zone_id": zone_id,
                    "workload": workload,
                    "workload_share": share,
                    "microzone_concentration": concentration,
                    "microzone_entropy_normalized": 1.0 - concentration,
                    "workers": worker,
                }
            )
    return pd.DataFrame(rows)


def _inputs() -> Phase6Inputs:
    recommendation = {
        "phase": "4E",
        "model_revision": "rev",
        "selection_rule": "pareto_knee",
        "entropy_weight": 0.25,
        "n_calibration_dates": 10,
        "holdout_dates": ["2023-07-19", "2023-07-20", "2023-07-21"],
        "flow_time_change_vs_lambda0_pct": 4.0,
        "conflicts_reduction_vs_lambda0_pct": 10.0,
        "wait_reduction_vs_lambda0_pct": 20.0,
        "congestion_reduction_vs_lambda0_pct": 15.0,
        "composite_congestion_reduction_vs_lambda0_pct": 15.0,
    }
    phase5_metadata = {
        "phase": 5,
        "model_revision": "rev",
        "holdout_dates_frozen": recommendation["holdout_dates"],
        "holdout_dates_completed": recommendation["holdout_dates"],
        "holdout_dates_skipped": [],
        "entropy": {"weight": 0.25},
    }
    equivalence = pd.DataFrame(
        [
            {
                "selected_date": "2023-07-19",
                "entropy_worker_counts": "2|1",
                "volume_worker_counts": "1|2",
                "same_as_volume": False,
            },
            {
                "selected_date": "2023-07-20",
                "entropy_worker_counts": "1|2",
                "volume_worker_counts": "1|2",
                "same_as_volume": True,
            },
            {
                "selected_date": "2023-07-21",
                "entropy_worker_counts": "1|2",
                "volume_worker_counts": "1|2",
                "same_as_volume": True,
            },
        ]
    )
    return Phase6Inputs(
        recommendation=recommendation,
        phase5_metadata=phase5_metadata,
        daily=_daily(),
        allocations=_allocations(),
        equivalence=equivalence,
        phase4_dir=Path("results/phase4"),
        phase5_dir=Path("results/phase5"),
    )


def test_holm_adjust_is_monotone_in_sorted_p_values() -> None:
    adjusted = holm_adjust([0.01, 0.04, 0.03, 0.20])
    assert adjusted == pytest.approx([0.04, 0.09, 0.09, 0.20])


def test_pareto_metric_statistics_reports_directional_improvement() -> None:
    result = pareto_metric_statistics(_daily(), bootstrap_samples=200, seed=7)
    conflicts = result[result["metric"].eq("congestion_conflicts")].iloc[0]
    assert conflicts["n_days"] == 3
    assert conflicts["wins"] == 1
    assert conflicts["ties"] == 1
    assert conflicts["losses"] == 1
    assert conflicts["improvement_pct_from_means"] == pytest.approx(0.0)
    assert "holm_p_value" in result.columns


def test_generalization_summary_keeps_calibration_and_holdout_separate() -> None:
    result = generalization_summary(_inputs()).set_index("split")
    assert result.loc["calibration", "flow_time_cost_pct"] == pytest.approx(4.0)
    assert result.loc["holdout", "flow_time_cost_pct"] == pytest.approx(5.0 / 290.0 * 100.0)
    assert result.loc["holdout", "reference_method"] == "volume_proportional"


def test_changed_zone_details_show_worker_shift_to_lower_concentration() -> None:
    inputs = _inputs()
    zones = changed_zone_detail_records(inputs)
    z1 = zones[zones["zone_id"].eq("Z01")].iloc[0]
    z2 = zones[zones["zone_id"].eq("Z02")].iloc[0]
    assert z1["worker_delta_entropy_minus_volume"] == 1
    assert z2["worker_delta_entropy_minus_volume"] == -1
    changed = changed_date_records(inputs, zones)
    row = changed.iloc[0]
    assert row["moved_workers"] == 1
    assert row["delta_R_entropy_minus_volume"] < 0
    assert row["concentration_shift_added_minus_removed"] < 0
    assert row["objective_preference_verified"]


def test_proxy_validation_is_exploratory_and_has_four_rows() -> None:
    frame = pd.DataFrame(
        {
            "R_volume": [3.0, 4.0, 5.0, 6.0],
            "R_entropy": [2.0, 2.5, 3.0, 3.5],
            "volume_congestion_conflicts": [20.0, 30.0, 40.0, 50.0],
            "entropy_congestion_conflicts": [18.0, 25.0, 32.0, 39.0],
            "volume_congestion_wait_seconds": [100.0, 120.0, 150.0, 180.0],
            "entropy_congestion_wait_seconds": [95.0, 108.0, 130.0, 150.0],
            "volume_congestion_delay_ratio": [0.10, 0.12, 0.15, 0.18],
            "entropy_congestion_delay_ratio": [0.09, 0.10, 0.12, 0.14],
            "D_entropy": [0.2, 0.3, 0.4, 0.5],
            "D_volume": [0.1, 0.1, 0.1, 0.1],
            "entropy_mean_flow_time_seconds": [110.0, 130.0, 150.0, 180.0],
            "volume_mean_flow_time_seconds": [100.0, 110.0, 120.0, 140.0],
        }
    )
    result = proxy_validation_records(frame)
    assert len(result) == 4
    assert set(result["interpretation"]) == {"exploratory changed-allocation subset only"}
    assert result["pearson_r"].notna().all()
