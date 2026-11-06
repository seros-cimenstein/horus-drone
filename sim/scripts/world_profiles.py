#!/usr/bin/env python3
"""World -> flight profile: the executor config and the SITL parameter fragments.

The one table every launcher reads: ``horus_world_profile`` in sim/scripts/procedural_world.sh
(start_sim.sh, run.sh, demo.sh, scripts/koko/remote_up.sh, the profiling harness) runs this
file. Paths are container paths; the parameter files form
a colon-separated list that start_sim.sh concatenates in order (the HORUS_SITL_PARAM_FILE
format). Generated worlds (horus_<kind>_p<seed>) get the procedural profile, the canonical
horus_forest/horus_urban worlds the outdoor one and horus_indoor the indoor one; ``gta`` (the
GTA V world, ``run.sh start --gta``, whose start_gta_sitl.sh writes its own SITL overrides)
gets horus_gta and no parameter fragments. Other worlds (e.g. upstream iris_runway) have no
profile.

Usage:
  world_profiles.py WORLD                     prints "CONFIG PARAM_FILES" on one line, or
                                              nothing when WORLD (stem, file name or SDF path)
                                              has no profile
  world_profiles.py check-config WORLD CONFIG exits 1 with a message when the HORUS config
                                              (container path) belongs to the other world
"""
from __future__ import annotations

import re
import sys
from pathlib import Path, PurePosixPath
from typing import NamedTuple

CONFIG_DIR = "/horus/config"
PARAM_DIR = "/horus/config/ardupilot"
PROJECT_DIR = Path(__file__).resolve().parents[2]
GTA_WORLD = "gta"
GTA_SOURCE_SCHEME = "gta://"


class WorldProfile(NamedTuple):
    config: str
    param_files: str


# (world stem pattern, config stem, parameter fragments in load order); first match wins.
_PROFILES = (
    (re.compile(GTA_WORLD), "horus_gta", ()),
    (re.compile(r"horus_indoor"), "horus", ("common", "indoor")),
    (re.compile(r"horus_(?P<kind>forest|urban)_p.*"), "horus_procedural_{kind}", ("common", "outdoor")),
    (re.compile(r"horus_(?P<kind>forest|urban).*"), "horus_{kind}", ("common", "outdoor")),
)


def world_profile(world: str) -> WorldProfile | None:
    """The profile for a world stem, file name or SDF path; None when it has none."""
    stem = PurePosixPath(world).name.removesuffix(".sdf")
    for pattern, config, fragments in _PROFILES:
        match = pattern.fullmatch(stem)
        if match is not None:
            return WorldProfile(
                f"{CONFIG_DIR}/{config.format(**match.groupdict())}.yaml",
                ":".join(f"{PARAM_DIR}/{fragment}.parm" for fragment in fragments),
            )
    return None


def config_world_mismatch(world: str, config: str) -> str | None:
    """Why ``config`` (a /horus container path) cannot run in ``world``; None when it can.

    GTA V frames arrive only from a ``gta://`` source and its map data only from the ``gta``
    provider; a Gazebo world streams its camera elsewhere and has no GTA map.
    """
    import yaml  # only the config check needs it; the profile lookup stays dependency-free

    path = Path(config)
    if path.is_relative_to("/horus"):
        path = PROJECT_DIR / path.relative_to("/horus")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        return f"cannot read HORUS config {config}: {exc}"
    source = str((data.get("perception") or {}).get("source", ""))
    provider = str((data.get("map") or {}).get("provider", "none"))
    is_gta_config = source.startswith(GTA_SOURCE_SCHEME)
    if world == GTA_WORLD and not is_gta_config:
        shown = source or "the default Gazebo UDP video"
        return (f"HORUS config {config} streams perception.source {shown}; "
                f"the GTA world needs a {GTA_SOURCE_SCHEME} source (config/horus_gta.yaml)")
    if world != GTA_WORLD and is_gta_config:
        return f"HORUS config {config} is a GTA config ({source}); Gazebo world {world} cannot feed it"
    if world != GTA_WORLD and provider == "gta":
        return f"HORUS config {config} uses the GTA map provider; Gazebo world {world} has no GTA map"
    return None


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "check-config":
        problem = config_world_mismatch(argv[1], argv[2])
        if problem is not None:
            print(problem, file=sys.stderr)
            return 1
        return 0
    if len(argv) != 1:
        print("usage: world_profiles.py WORLD | check-config WORLD CONFIG", file=sys.stderr)
        return 2
    profile = world_profile(argv[0])
    if profile is not None:
        print(profile.config, profile.param_files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
