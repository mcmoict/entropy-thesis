"""Configuration-driven experiment runner and command-line interface."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from .allocation import allocate_workers, normalize_strategy_name
from .simulation import SimulationConfig, WarehouseSimulation, ZoneConfig


class ConfigError(ValueError):
    """Raised when an experiment configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    """Validated experiment parameters from the YAML configuration."""

    seed: int
    replications: int
    total_workers: int
    zones: tuple[ZoneConfig, ...]
    duration: float
    warm_up: float
    arrival_rate: float
    methods: tuple[str, ...]
    entropy_weight: float
    minimum_per_zone: int
    output_directory: Path

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ExperimentConfig:
        """Validate and convert the canonical nested configuration mapping."""

        if not isinstance(raw, Mapping):
            raise ConfigError("configuration root must be a mapping")
        _reject_unknown(
            raw,
            {"experiment", "warehouse", "simulation", "allocation", "output"},
            path="configuration root",
        )
        experiment = _mapping_section(raw, "experiment")
        warehouse = _mapping_section(raw, "warehouse")
        simulation = _mapping_section(raw, "simulation")
        allocation = _mapping_section(raw, "allocation")
        output = _mapping_section(raw, "output")
        _reject_unknown(
            experiment,
            {"seed", "replications"},
            path="experiment",
        )
        _reject_unknown(
            warehouse,
            {"total_workers", "zones"},
            path="warehouse",
        )
        _reject_unknown(
            simulation,
            {"duration", "warm_up", "arrival_rate"},
            path="simulation",
        )
        _reject_unknown(
            allocation,
            {"methods", "entropy_weight", "minimum_per_zone"},
            path="allocation",
        )
        _reject_unknown(output, {"directory"}, path="output")

        seed = _integer(experiment, "seed", path="experiment.seed", minimum=0)
        replications = _integer(
            experiment,
            "replications",
            path="experiment.replications",
            minimum=1,
        )
        total_workers = _integer(
            warehouse,
            "total_workers",
            path="warehouse.total_workers",
            minimum=1,
        )

        raw_zones = warehouse.get("zones")
        if not isinstance(raw_zones, Sequence) or isinstance(raw_zones, (str, bytes)):
            raise ConfigError("warehouse.zones must be a non-empty list of zone mappings")
        if not raw_zones:
            raise ConfigError("warehouse.zones must contain at least one zone")
        zones: list[ZoneConfig] = []
        for index, raw_zone in enumerate(raw_zones):
            zone_path = f"warehouse.zones[{index}]"
            if not isinstance(raw_zone, Mapping):
                raise ConfigError(f"{zone_path} must be a mapping")
            _reject_unknown(
                raw_zone,
                {"id", "volume_share", "service_rate"},
                path=zone_path,
            )
            zone_id = raw_zone.get("id")
            if not isinstance(zone_id, str) or not zone_id.strip():
                raise ConfigError(f"{zone_path}.id must be a non-empty string")
            volume_share = _number(
                raw_zone,
                "volume_share",
                path=f"{zone_path}.volume_share",
                minimum=0.0,
            )
            service_rate = _number(
                raw_zone,
                "service_rate",
                path=f"{zone_path}.service_rate",
                exclusive_minimum=0.0,
            )
            zones.append(ZoneConfig(zone_id.strip(), volume_share, service_rate))
        if len({zone.zone_id for zone in zones}) != len(zones):
            raise ConfigError("warehouse.zones ids must be unique")
        volume_share_sum = sum(zone.volume_share for zone in zones)
        if not math.isclose(volume_share_sum, 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise ConfigError(
                "warehouse.zones volume_share values must sum to 1.0 "
                f"(received {volume_share_sum:.12g})"
            )

        duration = _number(
            simulation,
            "duration",
            path="simulation.duration",
            exclusive_minimum=0.0,
        )
        warm_up = _number(
            simulation,
            "warm_up",
            path="simulation.warm_up",
            minimum=0.0,
        )
        if warm_up >= duration:
            raise ConfigError("simulation.warm_up must be smaller than simulation.duration")
        arrival_rate = _number(
            simulation,
            "arrival_rate",
            path="simulation.arrival_rate",
            minimum=0.0,
        )

        raw_methods = allocation.get("methods")
        if not isinstance(raw_methods, Sequence) or isinstance(raw_methods, (str, bytes)):
            raise ConfigError("allocation.methods must be a non-empty list")
        if not raw_methods:
            raise ConfigError("allocation.methods must contain at least one method")
        methods: list[str] = []
        for index, method in enumerate(raw_methods):
            if not isinstance(method, str):
                raise ConfigError(f"allocation.methods[{index}] must be a string")
            try:
                normalized = normalize_strategy_name(method)
            except ValueError as error:
                raise ConfigError(f"allocation.methods[{index}]: {error}") from error
            if normalized in methods:
                raise ConfigError(f"allocation.methods contains duplicate method {normalized!r}")
            methods.append(normalized)

        entropy_weight = _optional_number(
            allocation,
            "entropy_weight",
            default=1.0,
            path="allocation.entropy_weight",
            minimum=0.0,
        )
        minimum_per_zone = _optional_integer(
            allocation,
            "minimum_per_zone",
            default=0,
            path="allocation.minimum_per_zone",
            minimum=0,
        )
        if minimum_per_zone * len(zones) > total_workers:
            raise ConfigError(
                "allocation.minimum_per_zone cannot be satisfied by warehouse.total_workers"
            )

        raw_output_directory = output.get("directory")
        if not isinstance(raw_output_directory, (str, Path)) or not str(
            raw_output_directory
        ).strip():
            raise ConfigError("output.directory must be a non-empty path")

        return cls(
            seed=seed,
            replications=replications,
            total_workers=total_workers,
            zones=tuple(zones),
            duration=duration,
            warm_up=warm_up,
            arrival_rate=arrival_rate,
            methods=tuple(methods),
            entropy_weight=entropy_weight,
            minimum_per_zone=minimum_per_zone,
            output_directory=Path(raw_output_directory),
        )

    def to_metadata(self) -> dict[str, Any]:
        """Return a JSON-serializable copy using the external config shape."""

        return {
            "experiment": {"seed": self.seed, "replications": self.replications},
            "warehouse": {
                "total_workers": self.total_workers,
                "zones": [
                    {
                        "id": zone.zone_id,
                        "volume_share": zone.volume_share,
                        "service_rate": zone.service_rate,
                    }
                    for zone in self.zones
                ],
            },
            "simulation": {
                "duration": self.duration,
                "warm_up": self.warm_up,
                "arrival_rate": self.arrival_rate,
            },
            "allocation": {
                "methods": list(self.methods),
                "entropy_weight": self.entropy_weight,
                "minimum_per_zone": self.minimum_per_zone,
            },
            "output": {"directory": str(self.output_directory)},
        }


@dataclass(slots=True)
class ExperimentResult:
    """Tabular run-level, zone-level, and aggregated experiment output."""

    config: ExperimentConfig
    runs: pd.DataFrame
    zones: pd.DataFrame
    summary: pd.DataFrame
    metadata: dict[str, Any]
    output_files: dict[str, Path] = field(default_factory=dict)


def _mapping_section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = raw.get(name)
    if not isinstance(section, Mapping):
        raise ConfigError(f"{name} must be a mapping")
    return section


def _reject_unknown(
    mapping: Mapping[Any, Any],
    allowed: set[str],
    *,
    path: str,
) -> None:
    unknown = [key for key in mapping if key not in allowed]
    if unknown:
        rendered = ", ".join(sorted(repr(key) for key in unknown))
        raise ConfigError(f"unknown field(s) in {path}: {rendered}")


def _integer(
    mapping: Mapping[str, Any],
    key: str,
    *,
    path: str,
    minimum: int,
) -> int:
    if key not in mapping:
        raise ConfigError(f"missing required field {path}")
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ConfigError(f"{path} must be an integer")
    result = int(value)
    if result < minimum:
        raise ConfigError(f"{path} must be at least {minimum}")
    return result


def _optional_integer(
    mapping: Mapping[str, Any],
    key: str,
    *,
    default: int,
    path: str,
    minimum: int,
) -> int:
    if key not in mapping:
        return default
    return _integer(mapping, key, path=path, minimum=minimum)


def _number(
    mapping: Mapping[str, Any],
    key: str,
    *,
    path: str,
    minimum: float | None = None,
    exclusive_minimum: float | None = None,
) -> float:
    if key not in mapping:
        raise ConfigError(f"missing required field {path}")
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise ConfigError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{path} must be finite")
    if minimum is not None and result < minimum:
        raise ConfigError(f"{path} must be at least {minimum}")
    if exclusive_minimum is not None and result <= exclusive_minimum:
        raise ConfigError(f"{path} must be greater than {exclusive_minimum}")
    return result


def _optional_number(
    mapping: Mapping[str, Any],
    key: str,
    *,
    default: float,
    path: str,
    minimum: float,
) -> float:
    if key not in mapping:
        return default
    return _number(mapping, key, path=path, minimum=minimum)


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    """Load and validate a YAML or JSON experiment configuration file."""

    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read configuration {config_path}: {error}") from error

    try:
        if config_path.suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            raw = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as error:
        raise ConfigError(f"cannot parse configuration {config_path}: {error}") from error
    if not isinstance(raw, Mapping):
        raise ConfigError("configuration root must be a mapping")
    return ExperimentConfig.from_mapping(raw)


def _coerce_config(
    config: ExperimentConfig | Mapping[str, Any] | str | Path,
) -> ExperimentConfig:
    if isinstance(config, ExperimentConfig):
        return config
    if isinstance(config, Mapping):
        return ExperimentConfig.from_mapping(config)
    return load_experiment_config(config)


def _derived_seed(base_seed: int, replication: int, stream: int) -> int:
    state = np.random.SeedSequence([base_seed, replication, stream]).generate_state(
        1, dtype=np.uint32
    )
    return int(state[0])


def _aggregate_runs(runs: pd.DataFrame) -> pd.DataFrame:
    numeric_columns = [
        column
        for column in runs.select_dtypes(include=[np.number]).columns
        if column not in {"replication", "simulation_seed", "allocation_seed"}
    ]
    records: list[dict[str, Any]] = []
    for method, group in runs.groupby("method", sort=False):
        record: dict[str, Any] = {
            "method": method,
            "replications": int(len(group)),
        }
        for column in numeric_columns:
            values = group[column].astype(float)
            record[f"{column}_mean"] = float(values.mean())
            record[f"{column}_std"] = (
                float(values.std(ddof=1)) if len(values) > 1 else 0.0
            )
        records.append(record)
    return pd.DataFrame.from_records(records)


def run_experiment(
    config: ExperimentConfig | Mapping[str, Any] | str | Path,
    *,
    save_results: bool = True,
    output_directory: str | Path | None = None,
) -> ExperimentResult:
    """Run all configured strategies and replications.

    Each strategy receives the same simulation seed within a replication,
    providing common random numbers for fair paired comparisons. Allocation
    randomness uses a separate seed stream.
    """

    settings = _coerce_config(config)
    volumes = [zone.volume_share for zone in settings.zones]
    run_records: list[dict[str, Any]] = []
    zone_records: list[dict[str, Any]] = []

    for replication in range(settings.replications):
        simulation_seed = _derived_seed(settings.seed, replication, 1)
        allocation_seed = _derived_seed(settings.seed, replication, 2)
        for method in settings.methods:
            allocation = allocate_workers(
                method,
                settings.total_workers,
                volumes,
                seed=allocation_seed,
                entropy_weight=settings.entropy_weight,
                minimum_per_zone=settings.minimum_per_zone,
            )
            simulation = WarehouseSimulation(
                settings.zones,
                allocation,
                SimulationConfig(
                    duration=settings.duration,
                    warm_up=settings.warm_up,
                    arrival_rate=settings.arrival_rate,
                    seed=simulation_seed,
                ),
            ).run()

            run_record: dict[str, Any] = {
                "method": method,
                "replication": replication + 1,
                "simulation_seed": simulation_seed,
                "allocation_seed": allocation_seed,
                **simulation.metrics.to_record(),
            }
            for zone, worker_count in zip(
                settings.zones, allocation.tolist(), strict=True
            ):
                run_record[f"workers_{zone.zone_id}"] = int(worker_count)
            run_records.append(run_record)

            for zone, metrics in zip(
                settings.zones, simulation.metrics.zones, strict=True
            ):
                zone_records.append(
                    {
                        "method": method,
                        "replication": replication + 1,
                        "simulation_seed": simulation_seed,
                        "allocation_seed": allocation_seed,
                        "volume_share": zone.volume_share,
                        "service_rate": zone.service_rate,
                        **metrics.to_record(),
                    }
                )

    runs = pd.DataFrame.from_records(run_records)
    zones = pd.DataFrame.from_records(zone_records)
    summary = _aggregate_runs(runs)
    metadata: dict[str, Any] = {
        "config": settings.to_metadata(),
        "methodology": {
            "common_random_numbers": True,
            "entropy_based_objective": "min KL(p || demand) - lambda * H(p)",
            "entropy_based_solution": "p_i proportional to demand_i ** (1 / (1 + lambda))",
            "constrained_apportionment": (
                "minimum lower bounds use continuous water-filling, followed "
                "by largest remainder; ties follow configured zone order"
            ),
            "entropy_log_base": 2,
            "observation_window": {
                "interval": "[warm_up, duration)",
                "throughput": "all completions during the observation window",
                "cohort": "jobs arriving during the observation window",
                "cohort_service_level": "cohort fraction completed by duration",
                "cohort_timing": (
                    "wait and system time for completed cohort members only; "
                    "unfinished members are right-censored"
                ),
                "flow_identity": (
                    "wip_end = wip_start + observation_arrivals "
                    "- observation_completions"
                ),
                "empty_sample_semantics": (
                    "cohort service level and observation event entropy are "
                    "NaN when their defining sample is empty"
                ),
            },
        },
    }
    result = ExperimentResult(settings, runs, zones, summary, metadata)
    if save_results:
        save_experiment_result(
            result,
            output_directory or settings.output_directory,
        )
    return result


def save_experiment_result(
    result: ExperimentResult,
    output_directory: str | Path | None = None,
) -> dict[str, Path]:
    """Write dataframes and metadata under the configured results directory."""

    directory = Path(output_directory or result.config.output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "runs": directory / "experiment_runs.csv",
        "zones": directory / "experiment_zones.csv",
        "summary": directory / "experiment_summary.csv",
        "metadata": directory / "experiment_metadata.json",
    }
    result.runs.to_csv(files["runs"], index=False, encoding="utf-8")
    result.zones.to_csv(files["zones"], index=False, encoding="utf-8")
    result.summary.to_csv(files["summary"], index=False, encoding="utf-8")
    files["metadata"].write_text(
        json.dumps(result.metadata, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    result.output_files = files
    return files


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run entropy-based warehouse worker-allocation experiments."
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="YAML or JSON experiment configuration path",
    )
    parser.add_argument(
        "--output-directory",
        "--output",
        type=Path,
        help="override output.directory from the configuration",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="run the experiment without writing result files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point used by ``python -m entropy_thesis.experiment``."""

    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        result = run_experiment(
            arguments.config,
            save_results=not arguments.no_save,
            output_directory=arguments.output_directory,
        )
    except (ConfigError, OSError, ValueError) as error:
        parser.error(str(error))

    print(result.summary.to_string(index=False))
    if result.output_files:
        print(f"Results written to {next(iter(result.output_files.values())).parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ConfigError",
    "ExperimentConfig",
    "ExperimentResult",
    "load_experiment_config",
    "main",
    "run_experiment",
    "save_experiment_result",
]
