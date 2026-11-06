"""Every shipped config profile must load through the strict configuration loader.

``horus.config.load_config`` rejects unknown or misplaced keys, so a setting added under the
wrong section (e.g. ``camera_horizontal_fov_deg`` under ``perception:`` instead of
``executor:``) makes the shipped profile unloadable. Tests that parse the YAML directly with
``yaml.safe_load`` cannot catch that; these tests go through the loader itself.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from horus.config import ExecutorSettings, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_every_shipped_config_profile_loads() -> None:
    profiles = sorted((ROOT / "config").glob("*.yaml"))

    assert profiles, "no shipped config profiles found"

    for path in profiles:
        load_config(path)


def test_only_the_gta_profile_reads_gta_frames_and_the_gta_map() -> None:
    gta_profiles = {
        path.name
        for path in (ROOT / "config").glob("*.yaml")
        if (config := load_config(path)).perception.source.startswith("gta://")
        or config.map.provider == "gta"
    }
    assert gta_profiles == {"horus_gta.yaml"}
    gta = load_config(ROOT / "config" / "horus_gta.yaml")
    assert gta.perception.source.startswith("gta://") and gta.map.provider == "gta"
    # HorusGta.dll's gimbal answers through horus-gta-gimbal on the same port as Gazebo's device.
    assert gta.gimbal.connection == "udpin:127.0.0.1:14600" and gta.executor.default_gimbal_pitch_deg == -45.0


MAP_HALF_EXTENT_M = 600.0  # generated worlds span 1.2 km


@pytest.mark.parametrize("kind", ["forest", "urban"])
def test_procedural_profiles_enable_city_scale_cruise_inside_the_map(kind: str) -> None:
    executor = load_config(ROOT / f"config/horus_procedural_{kind}.yaml").executor

    assert executor.cruise_speed_m_s > executor.speed_m_s
    assert executor.cruise_min_altitude_m > 0.0
    # One command TTL must cover a goto from home to a map corner in wall-clock time at RTF 0.35
    # on the 1.2 km worlds.
    corner_m = MAP_HALF_EXTENT_M * math.sqrt(2.0)
    assert executor.command_ttl_s >= corner_m / (executor.cruise_speed_m_s * 0.35)


def test_cruise_speed_must_exceed_speed_m_s() -> None:
    with pytest.raises(ValueError):
        ExecutorSettings(
            speed_m_s=3.0, cruise_speed_m_s=3.0, cruise_min_altitude_m=20.0
        )
    with pytest.raises(ValueError):
        ExecutorSettings(
            speed_m_s=3.0, cruise_speed_m_s=2.0, cruise_min_altitude_m=20.0
        )


def test_cruise_min_altitude_must_not_be_negative() -> None:
    with pytest.raises(ValueError):
        ExecutorSettings(speed_m_s=3.0, cruise_speed_m_s=8.0, cruise_min_altitude_m=-1.0)


def test_cruise_min_altitude_requires_a_cruise_speed() -> None:
    with pytest.raises(ValueError):
        ExecutorSettings(cruise_min_altitude_m=20.0)


def test_zero_cruise_speed_disables_the_gate() -> None:
    settings = ExecutorSettings(speed_m_s=3.0, cruise_speed_m_s=0)
    assert settings.cruise_speed_m_s is None
    assert settings.cruise_min_altitude_m is None


@pytest.mark.parametrize(
    "values",
    [
        {"control_hz": 1000.0},
        {"control_hz": 9.9},
        {"command_ttl_s": 0.0},
        {"camera_horizontal_fov_deg": 180.0},
        {"telemetry_max_age_s": float("nan")},
        {"camera_horizontal_fov_deg": 0.0},
        {"command_ttl_s": True},
    ],
)
def test_executor_settings_reject_out_of_range_limits(values) -> None:
    with pytest.raises(ValueError):
        ExecutorSettings(**values)


def test_executor_settings_accept_the_range_edges() -> None:
    settings = ExecutorSettings(control_hz=10, default_gimbal_pitch_deg=45)
    assert (settings.control_hz, settings.default_gimbal_pitch_deg) == (10.0, 45.0)
    assert isinstance(settings.control_hz, float)


def test_cruise_config_validation_goes_through_the_loader(tmp_path: Path) -> None:
    path = tmp_path / "cruise.yaml"
    path.write_text(
        "executor:\n"
        "  speed_m_s: 3.0\n"
        "  cruise_speed_m_s: 2.0\n"
        "  cruise_min_altitude_m: 30.0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(path)
    path.write_text(
        "executor:\n"
        "  speed_m_s: 3.0\n"
        "  cruise_speed_m_s: 8.0\n"
        "  cruise_min_altitude_m: 30.0\n",
        encoding="utf-8",
    )
    settings = load_config(path).executor
    assert settings.cruise_speed_m_s == 8.0
    assert settings.cruise_min_altitude_m == 30.0


def test_gimbal_config_goes_through_the_loader(tmp_path: Path) -> None:
    path = tmp_path / "gimbal.yaml"
    path.write_text(
        "gimbal:\n"
        "  connection: udpin:0.0.0.0:14600\n"
        "  pitch_min_deg: -60.0\n"
        "  status_body_frame: true\n",
        encoding="utf-8",
    )
    gimbal = load_config(path).gimbal
    assert (gimbal.connection, gimbal.pitch_min_deg, gimbal.status_body_frame) == ("udpin:0.0.0.0:14600", -60.0, True)
    # Without a section the camera is fixed.
    path.write_text("executor: {}\n", encoding="utf-8")
    assert load_config(path).gimbal.connection is None

    path.write_text("gimbal:\n  pitch_min_deg: 10.0\n  pitch_max_deg: 0.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid gimbal configuration"):
        load_config(path)
    # The gimbal is no longer the autopilot's: its old flight-section key is refused, not ignored.
    path.write_text("flight:\n  gimbal_status_body_frame: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown flight setting"):
        load_config(path)
