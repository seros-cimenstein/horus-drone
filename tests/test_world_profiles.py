from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sim.scripts.world_profiles import config_world_mismatch, world_profile

ROOT = Path(__file__).resolve().parents[1]
PARAMS = "/horus/config/ardupilot"


def _params(*fragments: str) -> str:
    return ":".join(f"{PARAMS}/{fragment}.parm" for fragment in fragments)


@pytest.mark.parametrize(("world", "config", "params"), [
    ("gta", "horus_gta", _params("gta")),
    ("horus_indoor", "horus", _params("common", "indoor")),
    ("horus_indoor.sdf", "horus", _params("common", "indoor")),
    ("horus_forest.sdf", "horus_forest", _params("common", "outdoor", "canonical_fence")),
    ("/horus/sim/worlds/horus_urban.sdf", "horus_urban",
     _params("common", "outdoor", "canonical_fence")),
    ("/horus/sim/generated/horus_forest_p21/horus_forest_p21.sdf", "horus_procedural_forest",
     _params("common", "outdoor", "procedural_fence")),
    ("horus_urban_p7", "horus_procedural_urban", _params("common", "outdoor", "procedural_fence")),
])
def test_world_profiles_pick_the_config_and_parameter_files(world, config, params):
    profile = world_profile(world)
    assert profile is not None
    assert profile.config == f"/horus/config/{config}.yaml"
    assert profile.param_files == params
    assert (ROOT / profile.config.removeprefix("/horus/")).is_file()
    assert all((ROOT / path.removeprefix("/horus/")).is_file() for path in params.split(":"))


def test_worlds_without_a_profile_get_none_and_the_cli_prints_nothing():
    assert world_profile("iris_runway.sdf") is None
    script = ROOT / "sim" / "scripts" / "procedural_world.sh"
    output = subprocess.run(
        ["bash", "-c", f'source "{script}"; horus_world_profile iris_runway.sdf; '
                       'horus_world_profile horus_forest_p3'],
        check=True, capture_output=True, text=True,
    ).stdout
    profile = world_profile("horus_forest_p3")
    assert output == f"{profile.config} {profile.param_files}\n"


def _param_keys(param_file: str) -> list[str]:
    lines = (ROOT / param_file.removeprefix("/horus/")).read_text().splitlines()
    return [key[0] for key in (line.split("#", 1)[0].split()[:1] for line in lines) if key]


# Every SITL load order: the three world profiles plus the GPS-denied stack
# (config/ardupilot/nogps.parm). Alternatives for one slot (indoor|outdoor,
# canonical_fence|procedural_fence) set the same keys to different values, so the invariant is
# per load order: no fragment overrides a key an earlier fragment set.
@pytest.mark.parametrize("param_files", [
    *(world_profile(world).param_files for world in ("horus_indoor", "horus_forest", "horus_urban_p1")),
    _params("common", "indoor", "nogps"),
])
def test_no_load_order_sets_a_parameter_twice(param_files):
    keys = [key for param_file in param_files.split(":") for key in _param_keys(param_file)]
    assert len(keys) == len(set(keys)), sorted(key for key in set(keys) if keys.count(key) > 1)


@pytest.mark.parametrize(("world", "config"), [
    ("gta", "/horus/config/horus_gta.yaml"),
    ("horus_indoor.sdf", "/horus/config/horus.yaml"),
    ("/horus/sim/generated/horus_urban_p7/horus_urban_p7.sdf", "/horus/config/horus_procedural_urban.yaml"),
])
def test_each_world_accepts_its_own_profile_config(world, config):
    assert config_world_mismatch(world, config) is None


@pytest.mark.parametrize(("world", "config", "reason"), [
    # A Gazebo config in a GTA run would wait forever for UDP video that GTA never sends...
    ("gta", "/horus/config/horus_forest.yaml", "gta://"),
    ("gta", "/horus/config/horus.yaml", "gta://"),
    # ...and the GTA config in a Gazebo run for GTA frames.
    ("horus_indoor.sdf", "/horus/config/horus_gta.yaml", "GTA config"),
    ("horus_forest.sdf", "/horus/config/missing.yaml", "cannot read"),
])
def test_a_config_from_the_other_world_is_refused(world, config, reason):
    assert reason in config_world_mismatch(world, config)


def test_a_gazebo_config_with_the_gta_map_is_refused(tmp_path):
    config = tmp_path / "gazebo_with_gta_map.yaml"
    config.write_text("map:\n  provider: gta\n  data_path: map.json\n", encoding="utf-8")
    assert "GTA map provider" in config_world_mismatch("horus_urban.sdf", str(config))


def test_check_config_cli_exits_nonzero_with_the_reason():
    script = ROOT / "sim" / "scripts" / "procedural_world.sh"
    refused = subprocess.run(
        ["bash", "-c", f'source "{script}"; horus_check_world_config gta /horus/config/horus_forest.yaml'],
        capture_output=True, text=True,
    )
    assert refused.returncode == 1 and "gta://" in refused.stderr
    accepted = subprocess.run(
        ["bash", "-c", f'source "{script}"; horus_check_world_config gta /horus/config/horus_gta.yaml'],
        capture_output=True, text=True,
    )
    assert accepted.returncode == 0 and accepted.stderr == ""
