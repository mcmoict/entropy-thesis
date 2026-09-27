from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .phase8 import REPORT_METRICS, run_phase8
from .progress import ConsoleProgress

BOOSTING_MODELS = ("xgboost", "lightgbm", "catboost")


def _collect_holdout_summary(output_root: Path) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for model_name in BOOSTING_MODELS:
        path = output_root / model_name / "phase8_holdout_comparison.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        adaptive = frame[frame["method"].eq("ai_adaptive_ewa")]
        for record in adaptive.to_dict("records"):
            rows.append({"model": model_name, **record})
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare XGBoost / LightGBM / CatBoost under the same Phase-8 protocol."
    )
    parser.add_argument("--phase4-dir", type=Path, default=Path("results/phase4"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--phase5-dir", type=Path, default=Path("results/phase5"))
    parser.add_argument("--output-dir", type=Path, default=Path("results/phase8_boosting"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--train-only", action="store_true")
    mode.add_argument("--selection-only", action="store_true")
    parser.add_argument("--validation-ratio", type=float, default=0.20)
    parser.add_argument("--ai-seed", type=int, default=42)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for model_name in BOOSTING_MODELS:
        progress = ConsoleProgress()
        progress.start(f"Phase 8 Boosting Comparison | {model_name}")
        run_phase8(
            phase4_dir=args.phase4_dir,
            data_dir=args.data_dir,
            phase5_dir=args.phase5_dir,
            output_dir=args.output_dir / model_name,
            train_only=args.train_only,
            selection_only=args.selection_only,
            ai_model=model_name,
            validation_ratio=args.validation_ratio,
            ai_seed=args.ai_seed,
            progress=progress,
        )
        progress.complete(f"{model_name} completed")

    # The chronological benchmark is identical in protocol for each run; keep one
    # consolidated copy for thesis tables.
    benchmark_path = args.output_dir / "xgboost" / "phase8_model_benchmark.csv"
    if benchmark_path.exists():
        benchmark = pd.read_csv(benchmark_path)
        benchmark.to_csv(args.output_dir / "boosting_model_benchmark.csv", index=False)
        boosting_only = benchmark[benchmark["model"].isin(BOOSTING_MODELS)].copy()
        boosting_only.to_csv(args.output_dir / "boosting_only_benchmark.csv", index=False)

    if not args.train_only and not args.selection_only:
        holdout = _collect_holdout_summary(args.output_dir)
        if not holdout.empty:
            holdout.to_csv(args.output_dir / "boosting_holdout_comparison.csv", index=False)
            print("\n=== Boosting Holdout Comparison | AI-Adaptive EWA ===")
            pivot = holdout.pivot(index="metric", columns="model", values="mean")
            for metric in REPORT_METRICS:
                if metric in pivot.index:
                    values = " | ".join(
                        f"{name}={float(pivot.loc[metric, name]):,.3f}"
                        for name in BOOSTING_MODELS
                        if name in pivot.columns
                    )
                    print(f"{metric:<30} {values}")

    print(f"\nResults: {args.output_dir}")


if __name__ == "__main__":
    main()
