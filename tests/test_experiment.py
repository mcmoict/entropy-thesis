"""Configuration, orchestration, and persistence tests for experiments."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from entropy_thesis.experiment import (
    ConfigError,
    ExperimentConfig,
    load_experiment_config,
    run_experiment,
    save_experiment_result,
)


def _config_mapping(output_directory: str | Path = "results") -> dict[str, Any]:
    return {
        "experiment": {"seed": 123, "replications": 2},
        "warehouse": {
            "total_workers": 4,
            "zones": [
                {"id": "A", "volume_share": 0.7, "service_rate": 4.0},
                {"id": "B", "volume_share": 0.3, "service_rate": 3.0},
            ],
        },
        "simulation": {"duration": 15.0, "warm_up": 2.0, "arrival_rate": 1.0},
        "allocation": {
            "methods": [
                "random",
                "equal",
                "volume_proportional",
                "entropy_based",
            ],
            "entropy_weight": 1.5,
            "minimum_per_zone": 1,
        },
        "output": {"directory": str(output_directory)},
    }


def test_load_yaml_config_validates_types_and_normalizes_methods(
    tmp_path: Path,
) -> None:
    raw = _config_mapping(tmp_path / "output")
    raw["allocation"]["methods"] = ["Random Allocation", "Entropy"]
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_experiment_config(path)

    assert isinstance(config, ExperimentConfig)
    assert config.seed == 123
    assert config.replications == 2
    assert config.total_workers == 4
    assert [zone.zone_id for zone in config.zones] == ["A", "B"]
    assert config.methods == ("random", "entropy_based")
    assert config.output_directory == tmp_path / "output"


def test_load_json_config(tmp_path: Path) -> None:
    raw = _config_mapping()
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_experiment_config(path)
    assert config.to_metadata() == raw


def test_optional_allocation_values_have_documented_defaults() -> None:
    raw = _config_mapping()
    del raw["allocation"]["entropy_weight"]
    del raw["allocation"]["minimum_per_zone"]
    config = ExperimentConfig.from_mapping(raw)
    assert config.entropy_weight == 1.0
    assert config.minimum_per_zone == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.pop("simulation"),
        lambda raw: raw["experiment"].__setitem__("seed", True),
        lambda raw: raw["experiment"].__setitem__("replications", 0),
        lambda raw: raw["warehouse"].__setitem__("total_workers", 0),
        lambda raw: raw["warehouse"].__setitem__("zones", []),
        lambda raw: raw["warehouse"]["zones"].append(
            {"id": "A", "volume_share": 0.1, "service_rate": 1.0}
        ),
        lambda raw: raw["warehouse"]["zones"][0].__setitem__(
            "volume_share", -0.1
        ),
        lambda raw: raw["warehouse"]["zones"][0].__setitem__("service_rate", 0),
        lambda raw: raw["simulation"].__setitem__("warm_up", 15.0),
        lambda raw: raw["simulation"].__setitem__("arrival_rate", -1),
        lambda raw: raw["allocation"].__setitem__("methods", []),
        lambda raw: raw["allocation"].__setitem__(
            "methods", ["equal", "Equal Allocation"]
        ),
        lambda raw: raw["allocation"].__setitem__("methods", ["unsupported"]),
        lambda raw: raw["allocation"].__setitem__("entropy_weight", -1),
        lambda raw: raw["allocation"].__setitem__("minimum_per_zone", 3),
        lambda raw: raw["output"].__setitem__("directory", ""),
        lambda raw: raw.__setitem__("simulaton", {}),
        lambda raw: raw["simulation"].__setitem__("arrvial_rate", 1.0),
        lambda raw: raw["warehouse"]["zones"][0].__setitem__("volum_share", 0.7),
    ],
)
def test_config_validation_rejects_invalid_settings(
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    raw = _config_mapping()
    mutate(raw)
    with pytest.raises(ConfigError):
        ExperimentConfig.from_mapping(raw)


def test_load_config_reports_missing_malformed_and_non_mapping_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_experiment_config(tmp_path / "missing.yaml")

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("warehouse: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot parse"):
        load_experiment_config(malformed)

    non_mapping = tmp_path / "list.json"
    non_mapping.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="root must be a mapping"):
        load_experiment_config(non_mapping)


def test_config_allows_empty_demand_samples_and_zero_demand_zones() -> None:
    raw = _config_mapping()
    raw["simulation"]["arrival_rate"] = 0
    raw["warehouse"]["zones"][0]["volume_share"] = 0
    raw["warehouse"]["zones"][1]["volume_share"] = 1

    config = ExperimentConfig.from_mapping(raw)

    assert config.arrival_rate == 0
    assert [zone.volume_share for zone in config.zones] == [0, 1]


def test_tiny_experiment_is_deterministic_and_preserves_core_invariants(
    tmp_path: Path,
) -> None:
    raw = _config_mapping(tmp_path / "unused-output")

    first = run_experiment(raw, save_results=False)
    second = run_experiment(raw, save_results=False)

    pd.testing.assert_frame_equal(first.runs, second.runs)
    pd.testing.assert_frame_equal(first.zones, second.zones)
    pd.testing.assert_frame_equal(first.summary, second.summary)
    assert first.output_files == {}
    assert not (tmp_path / "unused-output").exists()

    method_count = len(raw["allocation"]["methods"])
    replication_count = raw["experiment"]["replications"]
    zone_count = len(raw["warehouse"]["zones"])
    assert len(first.runs) == method_count * replication_count
    assert len(first.zones) == method_count * replication_count * zone_count
    assert len(first.summary) == method_count
    assert set(first.runs["method"]) == set(raw["allocation"]["methods"])
    assert set(first.summary["method"]) == set(raw["allocation"]["methods"])
    assert first.runs.groupby("replication")["simulation_seed"].nunique().eq(1).all()
    assert first.runs[["workers_A", "workers_B"]].sum(axis=1).eq(4).all()
    assert first.runs[["workers_A", "workers_B"]].ge(1).all(axis=None)
    assert first.runs["cohort_completions"].le(
        first.runs["observation_arrivals"]
    ).all()
    assert (
        first.runs["wip_end"]
        == first.runs["wip_start"]
        + first.runs["observation_arrivals"]
        - first.runs["observation_completions"]
    ).all()
    assert first.runs["cohort_service_level"].dropna().between(0, 1).all()
    assert first.runs["utilization"].between(0, 1).all()
    assert first.metadata["methodology"]["common_random_numbers"] is True


def test_run_experiment_accepts_a_config_path_and_writes_all_outputs(
    tmp_path: Path,
) -> None:
    raw = _config_mapping(tmp_path / "configured-output")
    config_path = tmp_path / "tiny.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    override = tmp_path / "actual-output"

    result = run_experiment(
        config_path,
        save_results=True,
        output_directory=override,
    )

    assert set(result.output_files) == {"runs", "zones", "summary", "metadata"}
    assert all(path.parent == override for path in result.output_files.values())
    assert all(path.is_file() for path in result.output_files.values())
    assert len(pd.read_csv(result.output_files["runs"])) == len(result.runs)
    assert len(pd.read_csv(result.output_files["zones"])) == len(result.zones)
    assert len(pd.read_csv(result.output_files["summary"])) == len(result.summary)
    metadata = json.loads(result.output_files["metadata"].read_text(encoding="utf-8"))
    assert metadata == result.metadata


def test_save_experiment_result_can_write_to_a_new_directory(tmp_path: Path) -> None:
    result = run_experiment(_config_mapping(), save_results=False)
    destination = tmp_path / "nested" / "experiment"
    files = save_experiment_result(result, destination)
    assert result.output_files == files
    assert all(path.exists() for path in files.values())
