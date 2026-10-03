"""Every shipped config profile must load through the strict configuration loader.

``horus.config.load_config`` rejects unknown or misplaced keys, so a setting added under the
wrong section (e.g. ``min_ground_range_depression_deg`` under ``perception:`` instead of
``executor:``) makes the shipped profile unloadable. Tests that parse the YAML directly with
``yaml.safe_load`` cannot catch that; these tests go through the loader itself.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from horus.config import ExecutorSettings, load_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG_REFERENCE = re.compile(r"config/[A-Za-z0-9_./-]+\.yaml")


def test_every_shipped_config_profile_loads() -> None:
    profiles = sorted((ROOT / "config").glob("*.yaml"))

    assert profiles, "no shipped config profiles found"

    for path in profiles:
        load_config(path)


def test_mission_referenced_configs_exist_and_load() -> None:
    missions = sorted((ROOT / "missions").glob("*.yaml"))

    assert missions, "no mission files found"

    references = {
        reference
        for mission in missions
        for reference in CONFIG_REFERENCE.findall(mission.read_text(encoding="utf-8"))
    }

    for reference in sorted(references):
        path = ROOT / reference
        assert path.is_file(), f"{reference} referenced by a mission does not exist"
        load_config(path)


PROCEDURAL_PROFILES = {
    "forest": (26.0, 36.0),
    "urban": (38.0, 48.0),
}


@pytest.mark.parametrize("kind", sorted(PROCEDURAL_PROFILES))
def test_procedural_profiles_enable_city_scale_cruise(kind: str) -> None:
    cruise_min, max_altitude = PROCEDURAL_PROFILES[kind]
    executor = load_config(ROOT / f"config/horus_procedural_{kind}.yaml").executor

    assert executor.max_speed_m_s == 3.0
    assert executor.cruise_speed_m_s == 8.0
    assert executor.cruise_speed_m_s > executor.max_speed_m_s
    assert executor.cruise_min_altitude_m == cruise_min
    assert executor.min_altitude_m <= cruise_min <= max_altitude
    assert executor.max_altitude_m == max_altitude
    assert executor.max_distance_m == 850.0
    assert executor.geofence_polygon_ned == (
        (-585.0, -585.0), (-585.0, 585.0), (585.0, 585.0), (585.0, -585.0),
    )
    # The circular fence backstop must clear the 600 m map corners...
    assert executor.geofence_radius_m >= 600.0 * math.sqrt(2.0)
    # ...and one command TTL must cover a worst-case 850 m goto in wall-clock time at the
    # RTF 0.35 campaign hosts sustain on the 1.2 km worlds (plus margin for ramp/arrival).
    assert executor.command_ttl_s == 360.0
    assert executor.command_ttl_s >= executor.max_distance_m / (executor.cruise_speed_m_s * 0.35)


def test_cruise_speed_must_exceed_max_speed() -> None:
    with pytest.raises(ValueError):
        ExecutorSettings(
            max_speed_m_s=3.0, cruise_speed_m_s=3.0, cruise_min_altitude_m=20.0, max_altitude_m=36.0
        )
    with pytest.raises(ValueError):
        ExecutorSettings(
            max_speed_m_s=3.0, cruise_speed_m_s=2.0, cruise_min_altitude_m=20.0, max_altitude_m=36.0
        )


def test_cruise_min_altitude_must_lie_within_the_altitude_limits() -> None:
    with pytest.raises(ValueError):
        ExecutorSettings(
            max_speed_m_s=3.0, cruise_speed_m_s=8.0, cruise_min_altitude_m=40.0, max_altitude_m=36.0
        )
    with pytest.raises(ValueError):
        ExecutorSettings(
            max_speed_m_s=3.0,
            cruise_speed_m_s=8.0,
            cruise_min_altitude_m=0.5,
            min_altitude_m=1.0,
            max_altitude_m=36.0,
        )


def test_cruise_min_altitude_requires_a_cruise_speed() -> None:
    with pytest.raises(ValueError):
        ExecutorSettings(cruise_min_altitude_m=20.0)


def test_zero_cruise_speed_disables_the_gate() -> None:
    settings = ExecutorSettings(max_speed_m_s=3.0, cruise_speed_m_s=0, max_altitude_m=36.0)
    assert settings.cruise_speed_m_s is None
    assert settings.cruise_min_altitude_m is None


def test_cruise_config_validation_goes_through_the_loader(tmp_path: Path) -> None:
    path = tmp_path / "cruise.yaml"
    path.write_text(
        "executor:\n"
        "  max_speed_m_s: 3.0\n"
        "  cruise_speed_m_s: 2.0\n"
        "  cruise_min_altitude_m: 30.0\n"
        "  max_altitude_m: 36.0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(path)
    path.write_text(
        "executor:\n"
        "  max_speed_m_s: 3.0\n"
        "  cruise_speed_m_s: 8.0\n"
        "  cruise_min_altitude_m: 30.0\n"
        "  max_altitude_m: 36.0\n",
        encoding="utf-8",
    )
    settings = load_config(path).executor
    assert settings.cruise_speed_m_s == 8.0
    assert settings.cruise_min_altitude_m == 30.0
