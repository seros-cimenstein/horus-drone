"""Every shipped config profile must load through the strict configuration loader.

``horus.config.load_config`` rejects unknown or misplaced keys, so a setting added under the
wrong section (e.g. ``min_ground_range_depression_deg`` under ``perception:`` instead of
``executor:``) makes the shipped profile unloadable. Tests that parse the YAML directly with
``yaml.safe_load`` cannot catch that; these tests go through the loader itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from horus.config import load_config

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
