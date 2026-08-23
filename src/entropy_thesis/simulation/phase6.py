from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
from statistics import mean
from typing import Iterable, Literal

import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr, wilcoxon


Direction = Literal["minimize", "maximize"]

PHASE6_REVISION = "phase6-holdout-tradeoff-mechanism-v1"
DEFAULT_BOOTSTRAP_SAMPLES = 10_000
DEFAULT_SEED = 42

PARETO_METRICS: tuple[tuple[str, Direction], ...] = (
    ("mean_flow_time_seconds", "minimize"),
    ("congestion_conflicts", "minimize"),
    ("congestion_wait_seconds", "minimize"),
    ("congestion_delay_ratio", "minimize"),
)

CONGESTION_METRICS: tuple[str, ...] = (
    "congestion_conflicts",
    "congestion_wait_seconds",
    "congestion_delay_ratio",
)

CHANGED_DATE_KPIS: tuple[str, ...] = (
    "mean_flow_time_seconds",
    "congestion_conflicts",
    "congestion_wait_seconds",
    "congestion_delay_ratio",
    "mean_release_delay_seconds",
    "makespan_seconds",
    "mean_spatial_entropy_multiworker",
    "mean_spatial_entropy_normalized",
)


@dataclass(frozen=True)
class Phase6Inputs:
    recommendation: dict[str, object]
    phase5_metadata: dict[str, object]
    daily: pd.DataFrame
    allocations: pd.DataFrame
    equivalence: pd.DataFrame
    phase4_dir: Path
    phase5_dir: Path

    @property
    def entropy_weight(self) -> float:
        return float(self.recommendation["entropy_weight"])

    @property
    def holdout_dates(self) -> tuple[str, ...]:
        values = self.recommendation["holdout_dates"]
        assert isinstance(values, list)
        return tuple(str(value) for value in values)


@dataclass(frozen=True)
class Phase6Analysis:
    inputs: Phase6Inputs
    pareto_statistics: pd.DataFrame
    generalization: pd.DataFrame
    changed_dates: pd.DataFrame
    changed_zone_details: pd.DataFrame
    proxy_validation: pd.DataFrame


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object가 필요합니다: {path}")
    return payload


def _required_columns(frame: pd.DataFrame, columns: Iterable[str], *, source: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{source}에 필요한 컬럼이 없습니다: {', '.join(missing)}")


def load_phase6_inputs(
    *,
    phase4_dir: str | Path = Path("results/phase4"),
    phase5_dir: str | Path = Path("results/phase5"),
    require_complete_holdout: bool = True,
) -> Phase6Inputs:
    phase4_dir = Path(phase4_dir)
    phase5_dir = Path(phase5_dir)

    recommendation = _read_json(phase4_dir / "phase4_recommendation.json")
    phase5_metadata = _read_json(phase5_dir / "phase5_metadata.json")

    if str(recommendation.get("phase")) != "4E":
        raise ValueError("Phase 6는 최신 Phase 4E recommendation이 필요합니다.")
    if int(phase5_metadata.get("phase", -1)) != 5:
        raise ValueError("Phase 6는 Phase 5 metadata가 필요합니다.")

    phase4_revision = str(recommendation.get("model_revision", ""))
    phase5_revision = str(phase5_metadata.get("model_revision", ""))
    if not phase4_revision or phase4_revision != phase5_revision:
        raise ValueError(
            "Phase 4/5 model_revision이 일치하지 않습니다. "
            f"phase4={phase4_revision!r}, phase5={phase5_revision!r}"
        )

    if str(recommendation.get("selection_rule", "")) != "pareto_knee":
        raise ValueError("Phase 6 trade-off 분석은 pareto_knee Phase 4 결과를 요구합니다.")

    phase5_entropy = phase5_metadata.get("entropy")
    if not isinstance(phase5_entropy, dict):
        raise ValueError("Phase 5 metadata의 entropy 정보가 없습니다.")
    phase4_lambda = float(recommendation["entropy_weight"])
    phase5_lambda = float(phase5_entropy.get("weight", float("nan")))
    if not math.isclose(phase4_lambda, phase5_lambda, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError(
            "Phase 4 λ*와 Phase 5 고정 λ가 다릅니다. "
            f"phase4={phase4_lambda:g}, phase5={phase5_lambda:g}"
        )

    frozen = tuple(str(value) for value in phase5_metadata.get("holdout_dates_frozen", []))
    recommendation_holdout = tuple(str(value) for value in recommendation.get("holdout_dates", []))
    if not recommendation_holdout or frozen != recommendation_holdout:
        raise ValueError("Phase 4/5 frozen Holdout 날짜가 일치하지 않습니다.")

    completed = tuple(str(value) for value in phase5_metadata.get("holdout_dates_completed", []))
    skipped = phase5_metadata.get("holdout_dates_skipped", [])
    if require_complete_holdout and (completed != frozen or skipped):
        raise ValueError(
            "Phase 6 최종 분석은 전체 frozen Holdout 완료 결과가 필요합니다. "
            f"frozen={len(frozen)}, completed={len(completed)}, skipped={len(skipped) if isinstance(skipped, list) else '?'}"
        )

    daily = pd.read_csv(phase5_dir / "phase5_daily_summary.csv")
    allocations = pd.read_csv(phase5_dir / "phase5_allocations.csv")
    equivalence = pd.read_csv(phase5_dir / "phase5_allocation_equivalence.csv")

    _required_columns(
        daily,
        (
            "selected_date",
            "method",
            "mean_flow_time_seconds",
            "congestion_conflicts",
            "congestion_wait_seconds",
            "congestion_delay_ratio",
            "mean_release_delay_seconds",
            "makespan_seconds",
            "mean_spatial_entropy_multiworker",
            "mean_spatial_entropy_normalized",
        ),
        source="phase5_daily_summary.csv",
    )
    _required_columns(
        allocations,
        (
            "selected_date",
            "method",
            "zone_id",
            "workload",
            "workload_share",
            "microzone_concentration",
            "microzone_entropy_normalized",
            "workers",
        ),
        source="phase5_allocations.csv",
    )
    _required_columns(
        equivalence,
        (
            "selected_date",
            "entropy_worker_counts",
            "volume_worker_counts",
            "same_as_volume",
        ),
        source="phase5_allocation_equivalence.csv",
    )

    expected = set(completed if completed else frozen)
    daily_dates = set(daily["selected_date"].astype(str))
    equivalence_dates = set(equivalence["selected_date"].astype(str))
    if not expected.issubset(daily_dates) or not expected.issubset(equivalence_dates):
        raise ValueError("Phase 5 CSV에 completed Holdout 날짜가 모두 존재하지 않습니다.")

    return Phase6Inputs(
        recommendation=recommendation,
        phase5_metadata=phase5_metadata,
        daily=daily,
        allocations=allocations,
        equivalence=equivalence,
        phase4_dir=phase4_dir,
        phase5_dir=phase5_dir,
    )


def _method_frame(daily: pd.DataFrame, method: str) -> pd.DataFrame:
    subset = daily[daily["method"].eq(method)].copy()
    if subset.empty:
        raise ValueError(f"Phase 5 daily summary에 method={method!r}가 없습니다.")
    if subset["selected_date"].duplicated().any():
        raise ValueError(f"method={method!r}에 중복 selected_date가 있습니다.")
    return subset.set_index("selected_date").sort_index()


def _oriented_improvement(entropy_value: float, comparator_value: float, direction: Direction) -> float:
    return comparator_value - entropy_value if direction == "minimize" else entropy_value - comparator_value


def _improvement_pct_from_means(entropy_mean: float, comparator_mean: float, direction: Direction) -> float:
    if math.isclose(comparator_mean, 0.0, abs_tol=1e-15):
        return float("nan")
    delta = _oriented_improvement(entropy_mean, comparator_mean, direction)
    return 100.0 * delta / abs(comparator_mean)


def _paired_wilcoxon(entropy_values: np.ndarray, comparator_values: np.ndarray) -> tuple[float, float]:
    differences = entropy_values - comparator_values
    nonzero = differences[~np.isclose(differences, 0.0, rtol=1e-12, atol=1e-9)]
    if nonzero.size < 2:
        return 0.0, 1.0
    try:
        result = wilcoxon(
            entropy_values,
            comparator_values,
            alternative="two-sided",
            zero_method="wilcox",
        )
        return float(result.statistic), float(result.pvalue)
    except ValueError:
        return 0.0, 1.0


def holm_adjust(p_values: Iterable[float]) -> list[float]:
    values = [float(value) for value in p_values]
    if not values:
        return []
    order = sorted(range(len(values)), key=values.__getitem__)
    adjusted = [1.0] * len(values)
    running = 0.0
    m = len(values)
    for rank, index in enumerate(order):
        candidate = min(1.0, (m - rank) * values[index])
        running = max(running, candidate)
        adjusted[index] = running
    return adjusted


def _bootstrap_mean_ci(
    oriented_differences: np.ndarray,
    *,
    samples: int,
    seed: int,
    confidence: float = 0.95,
) -> tuple[float, float]:
    if samples <= 0:
        raise ValueError("bootstrap samples는 1 이상이어야 합니다.")
    if oriented_differences.size == 0:
        return float("nan"), float("nan")
    if oriented_differences.size == 1:
        value = float(oriented_differences[0])
        return value, value

    rng = np.random.default_rng(seed)
    n = oriented_differences.size
    means = np.empty(samples, dtype=float)
    batch = min(samples, 2_000)
    offset = 0
    while offset < samples:
        size = min(batch, samples - offset)
        indexes = rng.integers(0, n, size=(size, n))
        means[offset : offset + size] = oriented_differences[indexes].mean(axis=1)
        offset += size
    alpha = 1.0 - confidence
    low, high = np.quantile(means, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(low), float(high)


def pareto_metric_statistics(
    daily: pd.DataFrame,
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = DEFAULT_SEED,
) -> pd.DataFrame:
    entropy = _method_frame(daily, "entropy_based")
    volume = _method_frame(daily, "volume_proportional")
    common = entropy.index.intersection(volume.index)
    if common.empty:
        raise ValueError("Entropy/Volume 공통 Holdout 날짜가 없습니다.")

    records: list[dict[str, object]] = []
    raw_p_values: list[float] = []
    for metric_index, (metric, direction) in enumerate(PARETO_METRICS):
        entropy_values = pd.to_numeric(entropy.loc[common, metric], errors="coerce").to_numpy(float)
        volume_values = pd.to_numeric(volume.loc[common, metric], errors="coerce").to_numpy(float)
        valid = np.isfinite(entropy_values) & np.isfinite(volume_values)
        entropy_values = entropy_values[valid]
        volume_values = volume_values[valid]
        if entropy_values.size == 0:
            continue

        oriented = (
            volume_values - entropy_values
            if direction == "minimize"
            else entropy_values - volume_values
        )
        ties_mask = np.isclose(oriented, 0.0, rtol=1e-12, atol=1e-9)
        wins = int(np.count_nonzero((oriented > 0.0) & ~ties_mask))
        losses = int(np.count_nonzero((oriented < 0.0) & ~ties_mask))
        ties = int(np.count_nonzero(ties_mask))

        statistic, p_value = _paired_wilcoxon(entropy_values, volume_values)
        ci_low, ci_high = _bootstrap_mean_ci(
            oriented,
            samples=bootstrap_samples,
            seed=seed + metric_index,
        )
        entropy_mean = float(entropy_values.mean())
        volume_mean = float(volume_values.mean())
        records.append(
            {
                "metric": metric,
                "direction": direction,
                "n_days": int(entropy_values.size),
                "entropy_mean": entropy_mean,
                "volume_mean": volume_mean,
                "oriented_mean_improvement_native": float(oriented.mean()),
                "bootstrap_95_ci_low_native": ci_low,
                "bootstrap_95_ci_high_native": ci_high,
                "improvement_pct_from_means": _improvement_pct_from_means(
                    entropy_mean, volume_mean, direction
                ),
                "wins": wins,
                "ties": ties,
                "losses": losses,
                "wilcoxon_statistic": statistic,
                "wilcoxon_p_value_two_sided": p_value,
            }
        )
        raw_p_values.append(p_value)

    adjusted = holm_adjust(raw_p_values)
    for record, p_adjusted in zip(records, adjusted, strict=True):
        record["holm_p_value"] = p_adjusted
        record["significant_raw_0_05"] = bool(record["wilcoxon_p_value_two_sided"] < 0.05)
        record["significant_holm_0_05"] = bool(p_adjusted < 0.05)
    return pd.DataFrame(records)


def _mean_ratio(entropy: pd.DataFrame, volume: pd.DataFrame, dates: pd.Index, metric: str) -> float:
    e = float(pd.to_numeric(entropy.loc[dates, metric], errors="coerce").mean())
    v = float(pd.to_numeric(volume.loc[dates, metric], errors="coerce").mean())
    if math.isclose(v, 0.0, abs_tol=1e-15):
        return float("nan")
    return e / v


def generalization_summary(inputs: Phase6Inputs) -> pd.DataFrame:
    recommendation = inputs.recommendation
    entropy = _method_frame(inputs.daily, "entropy_based")
    volume = _method_frame(inputs.daily, "volume_proportional")
    common = entropy.index.intersection(volume.index)

    holdout_ratios = {metric: _mean_ratio(entropy, volume, common, metric) for metric in CONGESTION_METRICS}
    holdout_composite_ratio = mean(holdout_ratios.values())
    flow_ratio = _mean_ratio(entropy, volume, common, "mean_flow_time_seconds")

    calibration_record = {
        "split": "calibration",
        "entropy_weight": float(recommendation["entropy_weight"]),
        "n_dates": int(recommendation.get("n_calibration_dates", 0)),
        "flow_time_cost_pct": float(recommendation["flow_time_change_vs_lambda0_pct"]),
        "conflicts_reduction_pct": float(recommendation["conflicts_reduction_vs_lambda0_pct"]),
        "wait_reduction_pct": float(recommendation["wait_reduction_vs_lambda0_pct"]),
        "congestion_ratio_reduction_pct": float(
            recommendation["congestion_reduction_vs_lambda0_pct"]
        ),
        "composite_congestion_reduction_pct": float(
            recommendation["composite_congestion_reduction_vs_lambda0_pct"]
        ),
        "reference_method": "lambda=0 (exact Volume control)",
    }
    holdout_record = {
        "split": "holdout",
        "entropy_weight": float(recommendation["entropy_weight"]),
        "n_dates": int(len(common)),
        "flow_time_cost_pct": 100.0 * (flow_ratio - 1.0),
        "conflicts_reduction_pct": 100.0 * (1.0 - holdout_ratios["congestion_conflicts"]),
        "wait_reduction_pct": 100.0 * (1.0 - holdout_ratios["congestion_wait_seconds"]),
        "congestion_ratio_reduction_pct": 100.0
        * (1.0 - holdout_ratios["congestion_delay_ratio"]),
        "composite_congestion_reduction_pct": 100.0 * (1.0 - holdout_composite_ratio),
        "reference_method": "volume_proportional",
    }
    return pd.DataFrame([calibration_record, holdout_record])


def _parse_counts(value: object) -> tuple[int, ...]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return tuple()
    parts = [part.strip() for part in str(value).split("|") if part.strip()]
    return tuple(int(part) for part in parts)


def _moved_worker_count(left: Iterable[int], right: Iterable[int]) -> int:
    a = tuple(int(value) for value in left)
    b = tuple(int(value) for value in right)
    if len(a) != len(b) or sum(a) != sum(b):
        raise ValueError("비교할 worker count 벡터가 호환되지 않습니다.")
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) // 2


def _score_allocation(
    *,
    counts: Iterable[int],
    workloads: Iterable[float],
    concentrations: Iterable[float],
    entropy_weight: float,
) -> tuple[float, float, float]:
    counts = tuple(int(value) for value in counts)
    workloads = tuple(float(value) for value in workloads)
    concentrations = tuple(float(value) for value in concentrations)
    total_workers = sum(counts)
    total_workload = sum(workloads)
    if total_workers <= 0 or total_workload <= 0:
        raise ValueError("objective 계산에 양의 worker/workload가 필요합니다.")
    demand_mismatch = 0.5 * sum(
        abs(count / total_workers - workload / total_workload)
        for count, workload in zip(counts, workloads, strict=True)
    )
    congestion_risk = sum(
        concentration * math.comb(count, 2)
        for count, concentration in zip(counts, concentrations, strict=True)
    )
    return demand_mismatch, congestion_risk, demand_mismatch + entropy_weight * congestion_risk


def _date_method_row(frame: pd.DataFrame, selected_date: str, method: str) -> pd.Series:
    subset = frame[
        frame["selected_date"].astype(str).eq(selected_date) & frame["method"].eq(method)
    ]
    if len(subset) != 1:
        raise ValueError(f"{selected_date} method={method} 행이 정확히 1개가 아닙니다.")
    return subset.iloc[0]


def changed_zone_detail_records(inputs: Phase6Inputs) -> pd.DataFrame:
    eq = inputs.equivalence.copy()
    changed_dates = tuple(eq.loc[~eq["same_as_volume"].astype(bool), "selected_date"].astype(str))
    records: list[dict[str, object]] = []
    for selected_date in changed_dates:
        date_rows = inputs.allocations[inputs.allocations["selected_date"].astype(str).eq(selected_date)]
        entropy_rows = date_rows[date_rows["method"].eq("entropy_based")].sort_values("zone_id")
        volume_rows = date_rows[date_rows["method"].eq("volume_proportional")].sort_values("zone_id")
        if entropy_rows.empty or volume_rows.empty:
            raise ValueError(f"{selected_date} allocation 상세가 없습니다.")
        merged = entropy_rows.merge(
            volume_rows[["zone_id", "workers"]].rename(columns={"workers": "volume_workers"}),
            on="zone_id",
            how="inner",
            validate="one_to_one",
        )
        for row in merged.itertuples(index=False):
            entropy_workers = int(row.workers)
            volume_workers = int(row.volume_workers)
            concentration = float(row.microzone_concentration)
            workload_share = float(row.workload_share)
            total_workers = int(merged["workers"].sum())
            records.append(
                {
                    "selected_date": selected_date,
                    "zone_id": str(row.zone_id),
                    "workload": float(row.workload),
                    "workload_share": workload_share,
                    "microzone_concentration": concentration,
                    "microzone_entropy_normalized": float(row.microzone_entropy_normalized),
                    "volume_workers": volume_workers,
                    "entropy_workers": entropy_workers,
                    "worker_delta_entropy_minus_volume": entropy_workers - volume_workers,
                    "volume_worker_share": volume_workers / total_workers,
                    "entropy_worker_share": entropy_workers / total_workers,
                    "volume_abs_demand_gap": abs(volume_workers / total_workers - workload_share),
                    "entropy_abs_demand_gap": abs(entropy_workers / total_workers - workload_share),
                    "volume_pair_risk_contribution": concentration * math.comb(volume_workers, 2),
                    "entropy_pair_risk_contribution": concentration * math.comb(entropy_workers, 2),
                }
            )
    return pd.DataFrame(records)


def changed_date_records(inputs: Phase6Inputs, zone_details: pd.DataFrame) -> pd.DataFrame:
    eq = inputs.equivalence.copy()
    changed = eq.loc[~eq["same_as_volume"].astype(bool)].copy()
    if changed.empty:
        return pd.DataFrame()

    volume_all = _method_frame(inputs.daily, "volume_proportional")
    global_volume_means = {
        metric: float(pd.to_numeric(volume_all[metric], errors="coerce").mean())
        for metric in CONGESTION_METRICS
    }

    records: list[dict[str, object]] = []
    for eq_row in changed.itertuples(index=False):
        selected_date = str(eq_row.selected_date)
        entropy_counts = _parse_counts(eq_row.entropy_worker_counts)
        volume_counts = _parse_counts(eq_row.volume_worker_counts)
        date_zones = zone_details[zone_details["selected_date"].eq(selected_date)].sort_values("zone_id")
        workloads = tuple(float(value) for value in date_zones["workload"])
        concentrations = tuple(float(value) for value in date_zones["microzone_concentration"])
        entropy_D, entropy_R, entropy_J = _score_allocation(
            counts=entropy_counts,
            workloads=workloads,
            concentrations=concentrations,
            entropy_weight=inputs.entropy_weight,
        )
        volume_D, volume_R, volume_J = _score_allocation(
            counts=volume_counts,
            workloads=workloads,
            concentrations=concentrations,
            entropy_weight=inputs.entropy_weight,
        )

        removed = date_zones[date_zones["worker_delta_entropy_minus_volume"] < 0]
        added = date_zones[date_zones["worker_delta_entropy_minus_volume"] > 0]
        removed_weight = -removed["worker_delta_entropy_minus_volume"].astype(float)
        added_weight = added["worker_delta_entropy_minus_volume"].astype(float)
        removed_concentration = (
            float(np.average(removed["microzone_concentration"], weights=removed_weight))
            if not removed.empty
            else float("nan")
        )
        added_concentration = (
            float(np.average(added["microzone_concentration"], weights=added_weight))
            if not added.empty
            else float("nan")
        )

        entropy_row = _date_method_row(inputs.daily, selected_date, "entropy_based")
        volume_row = _date_method_row(inputs.daily, selected_date, "volume_proportional")

        record: dict[str, object] = {
            "selected_date": selected_date,
            "entropy_weight": inputs.entropy_weight,
            "entropy_worker_counts": "|".join(str(value) for value in entropy_counts),
            "volume_worker_counts": "|".join(str(value) for value in volume_counts),
            "moved_workers": _moved_worker_count(entropy_counts, volume_counts),
            "D_volume": volume_D,
            "D_entropy": entropy_D,
            "delta_D_entropy_minus_volume": entropy_D - volume_D,
            "R_volume": volume_R,
            "R_entropy": entropy_R,
            "delta_R_entropy_minus_volume": entropy_R - volume_R,
            "R_reduction_pct": (
                float("nan") if math.isclose(volume_R, 0.0, abs_tol=1e-15) else 100.0 * (volume_R - entropy_R) / volume_R
            ),
            "J_volume": volume_J,
            "J_entropy": entropy_J,
            "delta_J_entropy_minus_volume": entropy_J - volume_J,
            "objective_preference_verified": bool(entropy_J <= volume_J + 1e-12),
            "mean_concentration_workers_removed_from": removed_concentration,
            "mean_concentration_workers_added_to": added_concentration,
            "concentration_shift_added_minus_removed": added_concentration - removed_concentration,
        }

        normalized_congestion_delta_terms: list[float] = []
        for metric in CHANGED_DATE_KPIS:
            entropy_value = float(entropy_row[metric])
            volume_value = float(volume_row[metric])
            record[f"entropy_{metric}"] = entropy_value
            record[f"volume_{metric}"] = volume_value
            record[f"delta_{metric}"] = entropy_value - volume_value
            if metric in CONGESTION_METRICS:
                reference = global_volume_means[metric]
                if not math.isclose(reference, 0.0, abs_tol=1e-15):
                    normalized_congestion_delta_terms.append((entropy_value - volume_value) / reference)

        congestion_delta_index = (
            mean(normalized_congestion_delta_terms) if normalized_congestion_delta_terms else float("nan")
        )
        flow_delta = float(record["delta_mean_flow_time_seconds"])
        record["normalized_congestion_delta_index"] = congestion_delta_index
        if math.isclose(flow_delta, 0.0, abs_tol=1e-9) and math.isclose(
            congestion_delta_index, 0.0, abs_tol=1e-12
        ):
            mechanism = "neutral"
        elif flow_delta <= 0.0 and congestion_delta_index <= 0.0:
            mechanism = "win_win"
        elif flow_delta > 0.0 and congestion_delta_index <= 0.0:
            mechanism = "congestion_gain_efficiency_cost"
        elif flow_delta <= 0.0 and congestion_delta_index > 0.0:
            mechanism = "efficiency_gain_congestion_cost"
        else:
            mechanism = "loss_loss"
        record["mechanism_class"] = mechanism
        records.append(record)

    return pd.DataFrame(records).sort_values("selected_date").reset_index(drop=True)



def proxy_validation_records(changed_dates: pd.DataFrame) -> pd.DataFrame:
    """Exploratory check that Phase-4 proxy terms track DES outcomes.

    The allocation-changed subset is selected by the treatment mechanism itself,
    so these correlations are diagnostic rather than confirmatory evidence.
    """

    if changed_dates.empty:
        return pd.DataFrame()

    comparisons = (
        (
            "R_congestion_risk_reduction",
            changed_dates["R_volume"] - changed_dates["R_entropy"],
            "congestion_conflicts_reduction",
            changed_dates["volume_congestion_conflicts"]
            - changed_dates["entropy_congestion_conflicts"],
        ),
        (
            "R_congestion_risk_reduction",
            changed_dates["R_volume"] - changed_dates["R_entropy"],
            "congestion_wait_seconds_reduction",
            changed_dates["volume_congestion_wait_seconds"]
            - changed_dates["entropy_congestion_wait_seconds"],
        ),
        (
            "R_congestion_risk_reduction",
            changed_dates["R_volume"] - changed_dates["R_entropy"],
            "congestion_delay_ratio_reduction",
            changed_dates["volume_congestion_delay_ratio"]
            - changed_dates["entropy_congestion_delay_ratio"],
        ),
        (
            "D_demand_mismatch_increase",
            changed_dates["D_entropy"] - changed_dates["D_volume"],
            "mean_flow_time_increase",
            changed_dates["entropy_mean_flow_time_seconds"]
            - changed_dates["volume_mean_flow_time_seconds"],
        ),
    )
    records: list[dict[str, object]] = []
    for proxy_name, proxy, target_name, target in comparisons:
        x = pd.to_numeric(proxy, errors="coerce").to_numpy(float)
        y = pd.to_numeric(target, errors="coerce").to_numpy(float)
        valid = np.isfinite(x) & np.isfinite(y)
        x = x[valid]
        y = y[valid]
        if x.size < 3 or np.allclose(x, x[0]) or np.allclose(y, y[0]):
            pearson_r = pearson_p = spearman_r = spearman_p = float("nan")
        else:
            pearson = pearsonr(x, y)
            spearman = spearmanr(x, y)
            pearson_r, pearson_p = float(pearson.statistic), float(pearson.pvalue)
            spearman_r, spearman_p = float(spearman.statistic), float(spearman.pvalue)
        records.append(
            {
                "proxy": proxy_name,
                "target": target_name,
                "n_changed_dates": int(x.size),
                "pearson_r": pearson_r,
                "pearson_p_value": pearson_p,
                "spearman_rho": spearman_r,
                "spearman_p_value": spearman_p,
                "interpretation": "exploratory changed-allocation subset only",
            }
        )
    return pd.DataFrame(records)

def build_phase6_analysis(
    inputs: Phase6Inputs,
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = DEFAULT_SEED,
) -> Phase6Analysis:
    zone_details = changed_zone_detail_records(inputs)
    changed_dates = changed_date_records(inputs, zone_details)
    return Phase6Analysis(
        inputs=inputs,
        pareto_statistics=pareto_metric_statistics(
            inputs.daily,
            bootstrap_samples=bootstrap_samples,
            seed=seed,
        ),
        generalization=generalization_summary(inputs),
        changed_dates=changed_dates,
        changed_zone_details=zone_details,
        proxy_validation=proxy_validation_records(changed_dates),
    )


def write_phase6_results(
    output_dir: str | Path,
    analysis: Phase6Analysis,
    *,
    bootstrap_samples: int,
    seed: int,
) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    analysis.pareto_statistics.to_csv(
        output_dir / "phase6_pareto_metric_statistics.csv", index=False
    )
    analysis.generalization.to_csv(
        output_dir / "phase6_calibration_holdout_generalization.csv", index=False
    )
    analysis.changed_dates.to_csv(output_dir / "phase6_changed_dates.csv", index=False)
    analysis.changed_zone_details.to_csv(
        output_dir / "phase6_changed_date_zone_details.csv", index=False
    )
    analysis.proxy_validation.to_csv(
        output_dir / "phase6_proxy_validation.csv", index=False
    )

    same_as_volume = analysis.inputs.equivalence["same_as_volume"].astype(bool)
    metadata = {
        "phase": 6,
        "revision": PHASE6_REVISION,
        "purpose": (
            "post-hoc robustness and mechanism analysis of the frozen Phase 5 holdout; "
            "no lambda reselection and no holdout resampling"
        ),
        "source": {
            "phase4_dir": str(analysis.inputs.phase4_dir),
            "phase5_dir": str(analysis.inputs.phase5_dir),
            "model_revision": analysis.inputs.recommendation.get("model_revision"),
            "entropy_weight": analysis.inputs.entropy_weight,
            "selection_rule": analysis.inputs.recommendation.get("selection_rule"),
        },
        "holdout": {
            "n_dates": len(analysis.inputs.holdout_dates),
            "same_as_volume_dates": int(same_as_volume.sum()),
            "changed_vs_volume_dates": int((~same_as_volume).sum()),
        },
        "statistics": {
            "paired_test": "Wilcoxon signed-rank, two-sided",
            "multiple_testing": "Holm correction across the four Phase-4 Pareto metrics",
            "bootstrap": "paired nonparametric bootstrap of oriented mean difference",
            "bootstrap_samples": int(bootstrap_samples),
            "bootstrap_seed": int(seed),
            "confidence_level": 0.95,
        },
        "mechanism_diagnostics": {
            "objective_preference_verified_dates": (
                int(analysis.changed_dates["objective_preference_verified"].sum())
                if not analysis.changed_dates.empty
                else 0
            ),
            "shifted_toward_lower_concentration_dates": (
                int((analysis.changed_dates["concentration_shift_added_minus_removed"] < 0).sum())
                if not analysis.changed_dates.empty
                else 0
            ),
            "actual_composite_congestion_improved_dates": (
                int((analysis.changed_dates["normalized_congestion_delta_index"] < 0).sum())
                if not analysis.changed_dates.empty
                else 0
            ),
            "proxy_correlations": "exploratory only because n is the allocation-changed subset",
        },
        "interpretation_rules": {
            "positive_improvement_pct": "Entropy is better than Volume for the metric",
            "flow_time_cost_pct": "positive means Entropy is slower than Volume/lambda=0",
            "congestion_reduction_pct": "positive means Entropy reduces congestion",
            "changed_date_subset": (
                "descriptive mechanism analysis only; it is selected by allocation difference and is not "
                "used to retune lambda or replace the full-holdout inferential result"
            ),
        },
    }
    with (output_dir / "phase6_metadata.json").open("w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)


def _format_pct(value: float) -> str:
    return "nan" if not math.isfinite(value) else f"{value:+.2f}%"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 6 frozen-holdout Pareto trade-off robustness and mechanism analysis"
    )
    parser.add_argument("--phase4-dir", type=Path, default=Path("results/phase4"))
    parser.add_argument("--phase5-dir", type=Path, default=Path("results/phase5"))
    parser.add_argument("--output-dir", type=Path, default=Path("results/phase6"))
    parser.add_argument("--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP_SAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--allow-partial-holdout",
        action="store_true",
        help="smoke-test용 partial Phase 5 결과 허용; 최종 논문 분석에는 사용하지 않는다.",
    )
    args = parser.parse_args()

    inputs = load_phase6_inputs(
        phase4_dir=args.phase4_dir,
        phase5_dir=args.phase5_dir,
        require_complete_holdout=not args.allow_partial_holdout,
    )
    analysis = build_phase6_analysis(
        inputs,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
    )
    write_phase6_results(
        args.output_dir,
        analysis,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
    )

    eq = inputs.equivalence["same_as_volume"].astype(bool)
    general = analysis.generalization.set_index("split")
    holdout = general.loc["holdout"]
    calibration = general.loc["calibration"]

    print("=== Phase 6 | Frozen Holdout Trade-off & Mechanism Analysis ===")
    print(f"Fixed entropy lambda  : {inputs.entropy_weight:g}")
    print(f"Holdout dates         : {len(inputs.holdout_dates):,}")
    print(f"Same as Volume        : {int(eq.sum()):,}/{len(eq):,} dates")
    print(f"Changed vs Volume     : {int((~eq).sum()):,}/{len(eq):,} dates")
    print("Lambda reselection    : no")
    print("Holdout resampling    : no")
    print()
    print("=== Calibration -> Holdout Generalization ===")
    print(
        "Calibration          : "
        f"Flow cost {_format_pct(float(calibration['flow_time_cost_pct']))}, "
        f"Composite congestion reduction {_format_pct(float(calibration['composite_congestion_reduction_pct']))}"
    )
    print(
        "Holdout              : "
        f"Flow cost {_format_pct(float(holdout['flow_time_cost_pct']))}, "
        f"Conflicts reduction {_format_pct(float(holdout['conflicts_reduction_pct']))}, "
        f"Wait reduction {_format_pct(float(holdout['wait_reduction_pct']))}, "
        f"Congestion ratio reduction {_format_pct(float(holdout['congestion_ratio_reduction_pct']))}, "
        f"Composite reduction {_format_pct(float(holdout['composite_congestion_reduction_pct']))}"
    )
    print()
    print("=== Entropy vs Volume | Four Pareto Metrics ===")
    print(
        f"{'Metric':28s} {'Improve(mean)':>14s} {'W/T/L':>10s} "
        f"{'Wilcoxon p':>12s} {'Holm p':>12s} {'Bootstrap 95% CI (native)':>28s}"
    )
    for row in analysis.pareto_statistics.itertuples(index=False):
        ci = f"[{row.bootstrap_95_ci_low_native:.4g}, {row.bootstrap_95_ci_high_native:.4g}]"
        print(
            f"{row.metric:28s} {row.improvement_pct_from_means:>+13.3f}% "
            f"{row.wins:>2d}/{row.ties:d}/{row.losses:<2d} "
            f"{row.wilcoxon_p_value_two_sided:>12.6g} {row.holm_p_value:>12.6g} {ci:>28s}"
        )
    print()
    if not analysis.changed_dates.empty:
        counts = analysis.changed_dates["mechanism_class"].value_counts().to_dict()
        print("=== Allocation-Changed Dates | Mechanism ===")
        print(f"Changed dates         : {len(analysis.changed_dates):,}")
        print(
            "Mechanism classes    : "
            + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        )
        print(
            "Objective check      : "
            f"{int(analysis.changed_dates['objective_preference_verified'].sum())}/"
            f"{len(analysis.changed_dates)} dates J_entropy <= J_volume"
        )
        print(
            "Lower-C shift        : "
            f"{int((analysis.changed_dates['concentration_shift_added_minus_removed'] < 0).sum())}/"
            f"{len(analysis.changed_dates)} dates move workers toward lower concentration"
        )
        print(
            "DES congestion gain  : "
            f"{int((analysis.changed_dates['normalized_congestion_delta_index'] < 0).sum())}/"
            f"{len(analysis.changed_dates)} changed dates"
        )
    print()
    print(f"Results               : {args.output_dir}")


if __name__ == "__main__":
    main()
