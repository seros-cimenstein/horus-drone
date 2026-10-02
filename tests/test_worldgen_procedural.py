"""Procedural invariants for generated outdoor worlds.

Every property is checked over many seeds of both world kinds (plus the canonical worlds),
so a regression in any sampler, builder, scenario writer or planner shows up as a concrete
failing seed. ``HORUS_WORLDGEN_SEEDS`` widens the sweep (e.g. ``=200`` for harsh runs).
"""

from __future__ import annotations

import functools
import json
import math
import os
import random
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from sim.scripts.civilian_life import MotionEngine, load_scenario
from sim.worldgen import build_world
from sim.worldgen.geometry import CollisionWorld, load_collisions
from sim.worldgen.people import PERSON_ASSETS
from sim.worldgen.plan import (
    Camera,
    Ground,
    cruise_legs,
    follow_mission,
    legs_clearance,
    person_point,
    plan_follow,
    plan_vantage,
)

ROOT = Path(__file__).resolve().parents[1]
SEEDS = range(1, int(os.environ.get("HORUS_WORLDGEN_SEEDS", "8")) + 1)
WORLDS = [(kind, seed) for kind in ("forest", "urban") for seed in SEEDS]
CANONICAL = [("forest", None), ("urban", None)]
MAX_ALTITUDE = {"forest": 36.0, "urban": 48.0}  # config/horus_<kind>.yaml executor.max_altitude_m
RTL_ALT_M = 45.0  # config/ardupilot/horus_sitl_outdoor.parm
GEOFENCE_INSET = 60.0
ROLES_OF_INTEREST = {"forest": ("missing_hiker", "injured"), "urban": ("red_shirt", "collapsed")}


class World:
    """A generated world written to disk with its geometry, ground and scenario loaded."""

    def __init__(self, kind: str, seed: int | None, root: Path) -> None:
        self.kind = kind
        if seed is not None:
            bundle = build_world(kind, seed, procedural=True, textures=False)
            bundle.write(root)
            self.manifest_path = root / bundle.stem / f"{bundle.stem}.layout.json"
        else:
            self.manifest_path = ROOT / "sim" / "worlds" / f"horus_{kind}.layout.json"
        self.manifest = json.loads(self.manifest_path.read_text())
        self.sdf_path = self.manifest_path.parent / self.manifest["sdf"]
        self.scenario_path = self.manifest_path.parent / self.manifest["scenario"]
        resources = [self.manifest_path.parent / "models", ROOT / "sim" / "models", ROOT / "sim" / "worlds"]
        self.world = CollisionWorld(load_collisions(self.sdf_path, resource_paths=resources))
        self.ground = Ground(self.manifest, self.manifest_path)
        self.scenario = load_scenario(self.scenario_path)

    @property
    def label(self) -> str:
        return self.manifest["world"]


@pytest.fixture(scope="session")
def worlds(tmp_path_factory):
    root = tmp_path_factory.mktemp("worldgen")
    return functools.cache(lambda kind, seed: World(kind, seed, root))


def _all(kind_seeds, canonical_defects: dict[str, str] | None = None):
    """Parametrize over worlds; known canonical-world defects are strict expected failures.

    The canonical worlds are frozen (missions S9–S13 depend on their geometry), so defects
    found in them stay visible here instead of being silently exempted.
    """
    defects = canonical_defects or {}
    params = [
        pytest.param(kind, seed, id=f"{kind}-{seed or 'canonical'}",
                     marks=[pytest.mark.xfail(strict=True, reason=defects[kind])]
                     if seed is None and kind in defects else [])
        for kind, seed in kind_seeds
    ]
    return pytest.mark.parametrize("kind,seed", params)


@pytest.mark.parametrize("kind", ["forest", "urban"])
def test_canonical_worlds_pass_the_procedural_validation(kind):
    """Canonical worlds must satisfy the same world_report gates the sampler applies."""
    from sim.worldgen import MODEL_DIR, WORLD_DIR, _build, _manifest
    from sim.worldgen.dressing import canonical_dressing
    from sim.worldgen.layout import canonical_forest_layout, canonical_urban_layout
    from sim.worldgen.validation import _load_world, world_report

    seed = 2027 if kind == "forest" else 2028
    layout = canonical_forest_layout(seed) if kind == "forest" else canonical_urban_layout(seed)
    stem = f"horus_{kind}"
    dressing = canonical_dressing()
    sdf, agents, _rows, files = _build(kind, layout, stem, f"{stem}_terrain", dressing, textures=False)
    manifest = _manifest(layout, kind, stem, f"../scenarios/{stem}.yaml",
                         "../scenarios/horus_forest_ground.npy" if kind == "forest" else None,
                         f"{stem}.sdf", agents, dressing)
    world, ground = _load_world(kind, layout, sdf, model_dir=MODEL_DIR, world_dir=WORLD_DIR, files=files)
    report = world_report(world, ground, manifest, agents)
    assert report["failures"] == [], f"{stem}: {report['failures']}"


@pytest.mark.parametrize("kind", ["forest", "urban"])
def test_canonical_build_reproduces_committed_world(kind):
    bundle = build_world(kind, 2027 if kind == "forest" else 2028, procedural=False, textures=False)
    manifest_path = ROOT / "sim" / "worlds" / f"horus_{kind}.layout.json"
    assert bundle.sdf == (ROOT / "sim" / "worlds" / f"horus_{kind}.sdf").read_text()
    assert bundle.scenario_yaml == (ROOT / "sim" / "scenarios" / f"horus_{kind}.yaml").read_text()
    assert bundle.manifest == json.loads(manifest_path.read_text())


@pytest.mark.parametrize("kind", ["forest", "urban"])
def test_generation_is_deterministic_and_seed_sensitive(kind):
    first = build_world(kind, 11, procedural=True, textures=False)
    again = build_world(kind, 11, procedural=True, textures=False)
    other = build_world(kind, 12, procedural=True, textures=False)
    assert (first.sdf, first.scenario_yaml, first.manifest) == (again.sdf, again.scenario_yaml, again.manifest)
    assert first.manifest["follow"]["route"] != other.manifest["follow"]["route"]
    assert [p["position"] for p in first.manifest["people"]] != [p["position"] for p in other.manifest["people"]]


@_all(WORLDS + CANONICAL)
def test_sdf_models_scenario_and_manifest_agree(worlds, kind, seed):
    w = worlds(kind, seed)
    root = ET.parse(w.sdf_path).getroot()
    world = root.find("world")
    assert world.get("name") == w.label
    names = [m.get("name") for m in world.findall("model")] + [i.findtext("name") for i in world.findall("include")]
    assert len(names) == len(set(names)), f"{w.label}: duplicate model names"
    agents = {agent.model for agent in w.scenario.agents}
    assert agents <= set(names), f"{w.label}: scenario agents missing from SDF: {sorted(agents - set(names))}"
    manifest_people = ({p["model"] for p in w.manifest["people"]}
                       | {v["model"] for v in w.manifest["vehicles"]}
                       | {a["model"] for a in w.manifest["animals"]})
    assert manifest_people == agents, f"{w.label}: manifest/scenario agent sets differ"
    for person in w.manifest["people"]:
        assert person["asset"] in PERSON_ASSETS, f"{w.label}: {person['model']} uses unknown asset"
    assert w.manifest["follow"]["group"] in {agent.group for agent in w.scenario.agents}


@_all(WORLDS + CANONICAL)
def test_home_pad_is_clear_and_transit_altitude_is_safe(worlds, kind, seed):
    w = worlds(kind, seed)
    transit = w.manifest["transit_altitude_m"]
    clearance, blocker = w.world.segment_clearance((0, 0, 3.5), (0, 0, transit), radius=0.6)
    assert clearance >= 2.0, f"{w.label}: take-off column blocked by {blocker and blocker.name}"
    highest = w.world.max_top(0.0, 0.0, 110.0)
    assert highest + 5.9 <= transit <= MAX_ALTITUDE[kind], f"{w.label}: transit {transit} vs top {highest}"
    assert highest <= RTL_ALT_M - 3.0, f"{w.label}: RTL altitude does not clear {highest:.1f} m obstacles"


@_all(WORLDS + CANONICAL)
def test_people_and_vehicles_stay_inside_the_geofence_and_off_obstacles(worlds, kind, seed):
    w = worlds(kind, seed)
    engine = MotionEngine(w.scenario, groups=sorted({agent.group for agent in w.scenario.agents}))
    rng = random.Random(seed or 0)
    t = 0.0
    kinds = {agent.model: agent.kind for agent in w.scenario.agents}
    postures = {p["model"]: PERSON_ASSETS[p["asset"]]["posture"] for p in w.manifest["people"]}
    while t < 90.0:
        t += rng.uniform(0.05, 0.4)
        for model, (x, y, z, _yaw) in engine.update(t).items():
            assert max(abs(x), abs(y)) <= GEOFENCE_INSET, f"{w.label}: {model} at ({x:.1f}, {y:.1f}) t={t:.1f}"
            if kinds[model] == "vehicle":
                clearance, blocker = w.world.clearance((x, y, 1.0))
                assert clearance >= 0.9, f"{w.label}: {model} hits {blocker.name} at t={t:.1f}"
            elif postures.get(model) == "standing":
                assert abs(z - w.ground.height(x, y)) <= 0.05, f"{w.label}: {model} floats at t={t:.1f}"
                clearance, blocker = w.world.clearance((x, y, z + 1.0))
                assert clearance >= 0.1, f"{w.label}: {model} walks through {blocker.name} at t={t:.1f}"


@_all(WORLDS + CANONICAL)
def test_every_person_of_interest_has_a_safe_line_of_sight_vantage(worlds, kind, seed):
    w = worlds(kind, seed)
    camera = Camera()
    transit = w.manifest["transit_altitude_m"]
    rng = random.Random(seed or 0)
    for person in w.manifest["people"]:
        if person["role"] not in ROLES_OF_INTEREST[kind]:
            continue
        posture = PERSON_ASSETS[person["asset"]]["posture"]
        target = person_point(person, w.ground, posture)
        vantage = plan_vantage(w.world, w.ground, camera, person["model"], target, rng, transit_alt=transit)
        assert vantage is not None, f"{w.label}: no safe vantage sees {person['model']}"
        legs = cruise_legs((0.0, 0.0, 0.0), vantage.eye, vantage.heading_deg, transit)
        assert legs_clearance(w.world, (0.0, 0.0, 3.5), legs) >= 1.5, f"{w.label}: route to {person['model']}"
        assert camera.in_frame(vantage.eye, vantage.yaw_rad, target)


@_all([("forest", seed) for seed in [None] + list(SEEDS)])
def test_forest_clearing_scan_rectangle_contains_a_person_of_interest(worlds, kind, seed):
    # The campaign scan flies the largest clearing rectangle and scores detection coverage
    # over the people inside it; a forest world whose meadow is empty can never score.
    from sim.worldgen.validation import clearing_scan_rectangle

    w = worlds(kind, seed)
    rect = clearing_scan_rectangle(w.manifest["areas"])
    assert rect is not None, f"{w.label}: no clearing scan rectangle"
    x0, x1, y0, y1 = rect
    inside = [person for person in w.manifest["people"]
              if x0 <= person["position"][0] <= x1 and y0 <= person["position"][1] <= y1]
    assert any(person["role"] in ROLES_OF_INTEREST["forest"] for person in inside), \
        f"{w.label}: clearing {rect} holds {[person['model'] for person in inside]}"


@_all(WORLDS)
def test_follow_target_is_selectable_occluded_and_safe_to_follow(worlds, kind, seed):
    w = worlds(kind, seed)
    follow = w.manifest["follow"]
    plan = plan_follow(w.world, w.ground, w.manifest)
    assert plan.column_clearance_m >= 1.5, f"{w.label}: stage column clearance {plan.column_clearance_m:.2f}"
    assert plan.lane_clearance_m >= 1.5, f"{w.label}: follow lane clearance {plan.lane_clearance_m:.2f}"
    assert 2.0 <= plan.occluded_s <= 5.0, f"{w.label}: longest occlusion {plan.occluded_s} s"
    assert max(abs(plan.stage[0]), abs(plan.stage[1])) <= GEOFENCE_INSET
    camera = Camera()
    people = {p["model"]: p for p in w.manifest["people"]}
    target = people[follow["model"]]
    target_point = person_point(target, w.ground, "standing")
    assert camera.in_frame(plan.stage, plan.yaw_rad, target_point, margin_px=20.0)

    def reported_colours(asset: str) -> set[str]:
        """Every colour the detector could report for this person's top (orange reads red)."""
        record = PERSON_ASSETS[asset]
        if record.get("possible_shirt_colours"):
            return set(record["possible_shirt_colours"])
        return {record["shirt_colour_attribute"]} if record["shirt_colour_attribute"] else set()

    target_reads = reported_colours(target["asset"])
    assert follow["shirt_colour_attribute"] in target_reads
    for other in people.values():
        if other["model"] == target["model"]:
            continue
        posture = PERSON_ASSETS[other["asset"]]["posture"]
        point = person_point(other, w.ground, posture)
        if (reported_colours(other["asset"]) & target_reads
                and camera.in_frame(plan.stage, plan.yaw_rad, point)
                and w.world.blocking(plan.stage, point) is None):
            pytest.fail(f"{w.label}: {other['model']} also reports {follow['shirt_colour_attribute']} at the stage")


@_all(WORLDS + CANONICAL)
def test_generated_follow_mission_is_runnable(worlds, kind, seed):
    w = worlds(kind, seed)
    plan = plan_follow(w.world, w.ground, w.manifest)
    repo_root = ROOT if seed is None else w.manifest_path.parents[1]
    mission_path = repo_root / "runs" / "campaign" / "check" / "follow.yaml"
    mission = follow_mission(w.manifest, plan, manifest_path=w.manifest_path, mission_path=mission_path,
                             repo_root=repo_root, trace_path="runs/trace.jsonl", output_dir=".")
    loaded = yaml.safe_load(yaml.safe_dump(mission, sort_keys=False))
    driver = (mission_path.parent / loaded["driver"]).resolve()
    assert driver == (repo_root / "sim" / "scripts" / "civilian_life.py").resolve()
    args = loaded["driver_args"]
    assert (repo_root / args[args.index("--scenario") + 1]).resolve() == w.scenario_path.resolve()
    assert any(agent.group == args[args.index("--group") + 1] for agent in w.scenario.agents)
    tools = [step["tool"] for step in loaded["steps"] if "tool" in step]
    assert set(tools) <= {"take_off", "goto", "follow"}
    for step in loaded["steps"]:
        if step.get("tool") == "goto":
            pose = step["arguments"]["pose"]
            assert max(abs(pose["north_m"]), abs(pose["east_m"])) <= GEOFENCE_INSET
            assert pose["up_m"] <= MAX_ALTITUDE[kind]


@_all([(k, s) for k, s in WORLDS if k == "urban"])
def test_urban_red_shirt_is_unique_in_its_crowd(worlds, kind, seed):
    w = worlds(kind, seed)
    people = w.manifest["people"]
    red = next(p for p in people if p["role"] == "red_shirt")

    def reads_red(person: dict) -> bool:
        """True when the detector could report this person's top as red (orange reads red)."""
        record = PERSON_ASSETS[person["asset"]]
        return "red" in (set(record["possible_shirt_colours"])
                         if record.get("possible_shirt_colours")
                         else {record["shirt_colour_attribute"]} if record["shirt_colour_attribute"] else set())

    near = [p["model"] for p in people if reads_red(p)
            and math.dist(p["position"], red["position"]) <= 15.0]
    assert near == [red["model"]], f"{w.label}: red clothing near the target: {near}"
