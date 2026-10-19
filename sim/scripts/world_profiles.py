#!/usr/bin/env python3
"""World -> flight profile: the executor config and the SITL parameter fragments.

The one table every launcher reads: ``horus_world_profile`` in sim/scripts/procedural_world.sh
(start_sim.sh, run.sh, demo.sh, scripts/koko/remote_up.sh, the profiling harness) runs this
file, and campaign_episode.py imports it. Paths are container paths; the parameter files form
a colon-separated list that start_sim.sh concatenates in order (the HORUS_SITL_PARAM_FILE
format). Generated worlds (horus_<kind>_p<seed>) get the procedural profile, the canonical
horus_forest/horus_urban worlds the outdoor one and horus_indoor the indoor one; other worlds
(e.g. upstream iris_runway) have no profile.

Usage: world_profiles.py WORLD (stem, file name or SDF path). Prints "CONFIG PARAM_FILES" on
one line, or nothing when the world has no profile.
"""
from __future__ import annotations

import re
import sys
from pathlib import PurePosixPath
from typing import NamedTuple

CONFIG_DIR = "/horus/config"
PARAM_DIR = "/horus/config/ardupilot"


class WorldProfile(NamedTuple):
    config: str
    param_files: str


# (world stem pattern, config stem, parameter fragments in load order); first match wins.
_PROFILES = (
    (re.compile(r"horus_indoor"), "horus", ("common", "indoor")),
    (re.compile(r"horus_(?P<kind>forest|urban)_p.*"), "horus_procedural_{kind}",
     ("common", "outdoor", "procedural_fence")),
    (re.compile(r"horus_(?P<kind>forest|urban).*"), "horus_{kind}",
     ("common", "outdoor", "canonical_fence")),
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


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: world_profiles.py WORLD", file=sys.stderr)
        return 2
    profile = world_profile(argv[0])
    if profile is not None:
        print(profile.config, profile.param_files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
