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
from sim.scripts.world_profiles import world_profile

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


MAP_HALF_EXTENT_M = 600.0  # generated worlds span 1.2 km


@pytest.mark.parametrize("kind", ["forest", "urban"])
def test_procedural_profiles_enable_city_scale_cruise_inside_the_map(kind: str) -> None:
    executor = load_config(ROOT / f"config/horus_procedural_{kind}.yaml").executor

    assert executor.cruise_speed_m_s > executor.max_speed_m_s
    assert executor.min_altitude_m <= executor.cruise_min_altitude_m <= executor.max_altitude_m
    # The polygon is the only executor fence and stays inside the map; the circular backstop
    # is the profile's SITL FENCE_RADIUS, which must clear the map corners...
    assert max(abs(c) for vertex in executor.geofence_polygon_ned for c in vertex) < MAP_HALF_EXTENT_M
    assert executor.geofence_radius_m == ExecutorSettings().geofence_radius_m
    fence_radii = [
        float(line.split()[1])
        for param_file in world_profile(f"horus_{kind}_p1").param_files.split(":")
        for line in (ROOT / param_file.removeprefix("/horus/")).read_text(encoding="utf-8").splitlines()
        if line.split()[:1] == ["FENCE_RADIUS"]
    ]
    assert fence_radii and fence_radii[-1] >= MAP_HALF_EXTENT_M * math.sqrt(2.0)
    # ...and one command TTL must cover a worst-case goto in wall-clock time at the RTF 0.35
    # campaign hosts sustain on the 1.2 km worlds.
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


@pytest.mark.parametrize(
    "values",
    [
        {"control_hz": 1000.0},
        {"control_hz": 9.9},
        {"command_ttl_s": 0.0},
        {"max_altitude_m": 0.4, "min_altitude_m": 0.5},
        {"camera_horizontal_fov_deg": 180.0},
        {"target_match_threshold": 1.5},
        {"telemetry_max_age_s": float("nan")},
        {"target_width_m": 0.0},
        {"geofence_radius_m": True},
    ],
)
def test_executor_settings_reject_out_of_range_limits(values) -> None:
    with pytest.raises(ValueError):
        ExecutorSettings(**values)


def test_executor_settings_accept_the_range_edges() -> None:
    settings = ExecutorSettings(control_hz=10, min_altitude_m=0, max_altitude_m=0, target_match_threshold=1)
    assert (settings.control_hz, settings.max_altitude_m, settings.target_match_threshold) == (10.0, 0.0, 1.0)
    assert isinstance(settings.control_hz, float)


def test_geofence_radius_and_polygon_are_mutually_exclusive(tmp_path: Path) -> None:
    square = ((-4.0, -4.0), (-4.0, 4.0), (4.0, 4.0), (4.0, -4.0))
    with pytest.raises(ValueError, match="geofence_radius_m applies only without geofence_polygon_ned"):
        ExecutorSettings(geofence_radius_m=95.0, geofence_polygon_ned=square)
    path = tmp_path / "both.yaml"
    path.write_text(
        "executor:\n"
        "  geofence_radius_m: 95.0\n"
        "  geofence_polygon_ned: [[-4, -4], [-4, 4], [4, 4], [4, -4]]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="geofence_radius_m applies only without"):
        load_config(path)
    # Either one alone is accepted; the radius default does not count as set.
    assert ExecutorSettings(geofence_polygon_ned=square).geofence_polygon_ned == square
    assert ExecutorSettings(geofence_radius_m=95.0).geofence_radius_m == 95.0


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
