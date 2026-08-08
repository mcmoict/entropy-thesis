"""Parity checks between the published JSON Schema and runtime validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import pytest
import yaml

from entropy_thesis.experiment import ConfigError, ExperimentConfig


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "data" / "schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


def _baseline() -> dict[str, Any]:
    return yaml.safe_load(
        (ROOT / "configs" / "baseline.yaml").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "config_path",
    sorted((ROOT / "configs").glob("*.yaml")),
    ids=lambda path: path.name,
)
def test_every_repository_config_matches_schema_and_runtime(
    config_path: Path,
) -> None:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    VALIDATOR.validate(raw)
    ExperimentConfig.from_mapping(raw)


def test_schema_and_runtime_allow_documented_zero_and_defaults() -> None:
    raw = _baseline()
    raw["simulation"]["arrival_rate"] = 0
    raw["warehouse"]["zones"][0]["volume_share"] = 0
    raw["warehouse"]["zones"][1]["volume_share"] = 0.8
    del raw["allocation"]["entropy_weight"]
    del raw["allocation"]["minimum_per_zone"]

    VALIDATOR.validate(raw)
    config = ExperimentConfig.from_mapping(raw)

    assert config.arrival_rate == 0
    assert config.entropy_weight == 1.0
    assert config.minimum_per_zone == 0


def test_schema_and_runtime_both_reject_unknown_fields() -> None:
    raw = _baseline()
    raw["allocation"]["entopy_weight"] = 9

    assert list(VALIDATOR.iter_errors(raw))
    with pytest.raises(ConfigError, match="entopy_weight"):
        ExperimentConfig.from_mapping(raw)
