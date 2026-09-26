"""Focused validation checks for perception runtime budgets."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from horus.config import PerceptionSettings
from horus.perception.detector import DetectorConfig


@pytest.mark.parametrize(
    ("config_type", "field_name"),
    (
        (PerceptionSettings, "onnx_intra_op_num_threads"),
        (PerceptionSettings, "onnx_inter_op_num_threads"),
        (DetectorConfig, "onnx_intra_op_num_threads"),
        (DetectorConfig, "onnx_inter_op_num_threads"),
    ),
)
@pytest.mark.parametrize("value", (0, -1, True, 1.5, None, "2"))
def test_onnx_thread_counts_require_positive_integers(config_type, field_name, value):
    with pytest.raises(ValueError):
        config_type(**{field_name: value})


def test_onnx_thread_count_defaults() -> None:
    perception = PerceptionSettings()
    detector = DetectorConfig()

    assert (perception.onnx_intra_op_num_threads, perception.onnx_inter_op_num_threads) == (2, 1)
    assert (detector.onnx_intra_op_num_threads, detector.onnx_inter_op_num_threads) == (2, 1)


def test_default_and_simulation_configs_use_five_hz() -> None:
    assert PerceptionSettings().max_fps == 5.0

    for config_path in (Path("config/horus.yaml"), Path("config/horus_s3.yaml")):
        with config_path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        assert config["perception"]["max_fps"] == 5.0
