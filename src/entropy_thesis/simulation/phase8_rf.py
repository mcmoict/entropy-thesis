from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .phase8 import (
    REPORT_METRICS,
    _fmt,
    run_phase8,
)
from .progress import ConsoleProgress, format_duration


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Entropy Thesis - Phase 8 Random Forest comparison: "
            "RF KPI prediction + fixed-EWA flow-guarded adaptive lambda selection"
        )
    )
    parser.add_argument("--phase4-dir", type=Path, default=Path("results/phase4"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--phase5-dir", type=Path, default=Path("results/phase5"))
    parser.add_argument("--output-dir", type=Path, default=Path("results/phase8_rf"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--train-only",
        action="store_true",
        help="Phase 4 calibration 학습/내부검증만 실행",
    )
    mode.add_argument(
        "--selection-only",
        action="store_true",
        help="Calibration 학습 + frozen Holdout λ 예측/선택까지만 실행",
    )
    parser.add_argument("--validation-ratio", type=float, default=0.20)
    parser.add_argument("--ai-seed", type=int, default=42)
    args = parser.parse_args()

    progress = ConsoleProgress()
    progress.start("Phase 8 Random Forest Adaptive EWA")
    metadata = run_phase8(
        phase4_dir=args.phase4_dir,
        data_dir=args.data_dir,
        phase5_dir=args.phase5_dir,
        output_dir=args.output_dir,
        train_only=args.train_only,
        selection_only=args.selection_only,
        ai_model="random_forest",
        validation_ratio=args.validation_ratio,
        ai_seed=args.ai_seed,
        progress=progress,
    )
    progress.complete("Phase 8 Random Forest processing completed")

    benchmark = pd.read_csv(args.output_dir / "phase8_model_benchmark.csv")
    rf = benchmark[benchmark["model"].eq("random_forest")]

    print()
    print("=== Phase 8 | Random Forest Adaptive EWA ===")
    print(f"Fixed EWA lambda        : {float(metadata['fixed_ewa_lambda']):g}")
    print(f"Calibration dates       : {len(metadata['calibration_dates']):,}")
    print(f"Internal train dates    : {len(metadata['internal_train_dates']):,}")
    print(f"Internal validation     : {len(metadata['internal_validation_dates']):,}")
    print("AI model                : Random Forest")
    print("Lambda direct feature   : no (candidate allocation features only)")
    print("DES parameters          : frozen from Phase 4 metadata")
    print()
    print("=== Random Forest | Chronological Internal Validation ===")
    print("Target                               MAE          RMSE        R2")
    for row in rf.to_dict("records"):
        print(
            f"{str(row['target']):<30} "
            f"{_fmt(row['mae']):>11}   {_fmt(row['rmse']):>11}   {_fmt(row['r2']):>8}"
        )

    if args.selection_only:
        print()
        print("=== Frozen Holdout | RF Selection Only (No New DES) ===")
        print(f"Adaptive lambda counts  : {metadata.get('adaptive_lambda_counts', {})}")
        print(f"Selected result sources : {metadata.get('predicted_selected_actual_source_counts', {})}")

    if not args.train_only and not args.selection_only:
        comparison = pd.read_csv(args.output_dir / "phase8_holdout_comparison.csv")
        print()
        print("=== Frozen Holdout | RF-Adaptive EWA vs Fixed EWA ===")
        pivot = comparison.pivot(index="metric", columns="method", values="mean")
        for metric in REPORT_METRICS:
            if metric not in pivot.index:
                continue
            ai_value = float(pivot.loc[metric, "ai_adaptive_ewa"])
            fixed_value = float(pivot.loc[metric, "fixed_ewa"])
            change = (
                100.0 * (ai_value - fixed_value) / fixed_value
                if abs(fixed_value) > 1e-12
                else float("nan")
            )
            print(
                f"{metric:<30} AI={ai_value:>11,.3f} | Fixed={fixed_value:>11,.3f} | "
                f"change={change:>8.3f}%"
            )
        print(f"Adaptive lambda counts  : {metadata.get('adaptive_lambda_counts', {})}")

    print()
    print(f"Results                 : {args.output_dir}")
    print(
        f"Total execution time    : {format_duration(progress.elapsed_seconds)} "
        f"({progress.elapsed_seconds:,.2f} s)"
    )


if __name__ == "__main__":
    main()
