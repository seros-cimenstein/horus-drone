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
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from sim.campaign.civilian_spec import load_scenario
from sim.scripts.civilian_life import MotionEngine
from sim.worldgen import build_world
from sim.worldgen.assets import ASSETS
from sim.worldgen.geometry import CollisionWorld, load_collisions
from sim.worldgen.layout import distance_to_paths
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
# Canonical worlds keep the 60 m actor inset and 60 m cruise legs they shipped with; generated
# worlds carry their flight profile's limits in the manifest (actor_geofence_m / flight.*).
GEOFENCE_INSET = 60.0
RTL_ALT_M = 45.0  # config/ardupilot/outdoor.parm
ROLES_OF_INTEREST = {"forest": ("missing_hiker", "injured"), "urban": ("red_shirt", "collapsed")}


def _profile(kind: str) -> dict:
    path = ROOT / "config" / f"horus_procedural_{kind}.yaml"
    return yaml.safe_load(path.read_text())["executor"]


PROFILES = {kind: _profile(kind) for kind in ("forest", "urban")}
MAX_ALTITUDE = {kind: float(PROFILES[kind]["max_altitude_m"]) for kind in PROFILES}
TRANSIT_MIN_M = {kind: float(PROFILES[kind]["cruise_min_altitude_m"]) for kind in PROFILES}
TREE_TOP_LIMIT_M = 23.0  # config comment: forest trees stay below 23 m (profile caps 36 m)


def _vantage_bounds(manifest: dict) -> float:
    flight = manifest.get("flight") or {}
    return float(flight.get("vantage_bounds_m", GEOFENCE_INSET))


def _actor_inset(manifest: dict) -> float:
    return float(manifest.get("actor_geofence_m", GEOFENCE_INSET))


def _manifest_bounds_radius(manifest: dict) -> float:
    bounds = manifest["bounds"]
    return math.hypot(bounds["x"][1], bounds["y"][1]) + 30.0


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


def _all(kind_seeds):
    """Parametrize over worlds (seed None is the canonical world)."""
    return pytest.mark.parametrize("kind,seed", [
        pytest.param(kind, seed, id=f"{kind}-{seed or 'canonical'}") for kind, seed in kind_seeds
    ])


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
    sdf, agents, _rows, files = _build(kind, layout, f"{stem}_terrain", dressing, textures=False)
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
    highest = w.world.max_top(0.0, 0.0, _manifest_bounds_radius(w.manifest))
    assert highest + 5.9 <= transit <= MAX_ALTITUDE[kind], f"{w.label}: transit {transit} vs top {highest}"
    assert highest <= RTL_ALT_M - 3.0, f"{w.label}: RTL altitude does not clear {highest:.1f} m obstacles"
    if seed is not None:
        assert transit >= TRANSIT_MIN_M[kind], f"{w.label}: transit {transit} below the profile cruise gate"


@_all(WORLDS + CANONICAL)
def test_people_and_vehicles_stay_inside_the_geofence_and_off_obstacles(worlds, kind, seed):
    w = worlds(kind, seed)
    engine = MotionEngine(w.scenario, groups=sorted({agent.group for agent in w.scenario.agents}))
    rng = random.Random(seed or 0)
    t = 0.0
    kinds = {agent.model: agent.kind for agent in w.scenario.agents}
    postures = {p["model"]: PERSON_ASSETS[p["asset"]]["posture"] for p in w.manifest["people"]}
    inset = _actor_inset(w.manifest)
    while t < 90.0:
        t += rng.uniform(0.05, 0.4)
        for model, (x, y, z, _yaw) in engine.update(t).items():
            assert max(abs(x), abs(y)) <= inset, f"{w.label}: {model} at ({x:.1f}, {y:.1f}) t={t:.1f}"
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
    bounds = _vantage_bounds(w.manifest)
    rng = random.Random(seed or 0)
    for person in w.manifest["people"]:
        if person["role"] not in ROLES_OF_INTEREST[kind]:
            continue
        posture = PERSON_ASSETS[person["asset"]]["posture"]
        target = person_point(person, w.ground, posture)
        vantage = plan_vantage(w.world, w.ground, camera, person["model"], target, rng,
                               transit_alt=transit, bounds=bounds)
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
    assert max(abs(plan.stage[0]), abs(plan.stage[1])) <= _actor_inset(w.manifest)
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
    bounds = _vantage_bounds(w.manifest)
    for step in loaded["steps"]:
        if step.get("tool") == "goto":
            pose = step["arguments"]["pose"]
            assert max(abs(pose["north_m"]), abs(pose["east_m"])) <= bounds
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


@_all([("forest", seed) for seed in [None] + list(SEEDS)])
def test_forest_posture_targets_are_observable(worlds, kind, seed):
    """Every posture the campaign targets needs a person the detector can actually see.

    The approach/orbit tests pick a lying or sitting person; the canopy-hidden missing hiker
    stays for the search mission, so a second prone casualty must sit in the open with a
    good-slant vantage (recall is poor beyond ~12 m).
    """
    import random as _random

    from sim.worldgen.plan import Camera, column_clearance, person_point, plan_vantage

    w = worlds(kind, seed)
    camera = Camera()
    transit = w.manifest["transit_altitude_m"]
    bounds = _vantage_bounds(w.manifest)
    for posture in ("lying", "sitting"):
        people = [person for person in w.manifest["people"] if person["posture"] == posture]
        assert people, f"{w.label}: no {posture} person"
        observable = []
        for person in people:
            point = person_point(person, w.ground, posture)
            if column_clearance(w.world, point[0], point[1], point[2] + 0.5, transit, 0.6) < 2.0:
                continue
            vantage = plan_vantage(w.world, w.ground, camera, person["model"], point,
                                   _random.Random(seed or 0), transit_alt=transit, bounds=bounds)
            if vantage is not None and vantage.slant_m <= 12.0:
                observable.append(person["model"])
        assert observable, f"{w.label}: no observable {posture} person (open sky + <= 12 m slant)"

    # The search mission keeps its canopy-hidden hiker alongside the open casualty.
    roles = {person["role"] for person in w.manifest["people"]}
    assert {"missing_hiker", "casualty"} <= roles, f"{w.label}: {sorted(roles)}"


def _scenery_tile_visuals(w: World) -> list[dict]:
    """Every instanced asset visual of the procedural scenery tiles, in world coordinates."""
    root = ET.parse(w.sdf_path).getroot()
    visuals = []
    for model in root.iter("model"):
        name = model.get("name") or ""
        if not name.startswith("scenery_") or name == "scenery_collision":
            continue  # the merged collision model carries no visuals and sits at the origin
        tile_pose = model.find("pose")
        ox, oy = (float(value) for value in (tile_pose.text or "0 0 0 0 0 0").split()[:2])
        for link in model.findall("link"):
            for visual in link.findall("visual"):
                pose = visual.find("pose")
                if pose is None:
                    continue
                px, py, pz = (float(value) for value in pose.text.split()[:3])
                mesh = visual.find("geometry/mesh")
                if mesh is None:
                    continue
                scale_text = mesh.findtext("scale")
                scale = float(scale_text.split()[0]) if scale_text else 1.0
                visuals.append({"name": visual.get("name"), "x": ox + px, "y": oy + py,
                                "z": pz, "mesh_scale": scale})
    return visuals


def _tree_instances(w: World) -> list[dict]:
    """Pine/oak instance visuals with their asset key and instance scale."""
    trees = []
    for visual in _scenery_tile_visuals(w):
        name = visual["name"] or ""
        for key in ("tree_pine", "tree_oak"):
            if name.startswith(key):
                registry_scale = float(ASSETS[key].visuals[0].scale)
                trees.append({**visual, "asset": key,
                              "instance_scale": visual["mesh_scale"] / registry_scale})
                break
    return trees


@_all([("forest", seed) for seed in SEEDS])
def test_procedural_forest_scenery_matches_the_design(worlds, kind, seed):
    """Real end-to-end checks over the emitted SDF and its scenery sidecar."""
    w = worlds(kind, seed)
    scenery = w.manifest.get("scenery")
    assert scenery, f"{w.label}: manifest carries no scenery summary"
    assert 1500 <= scenery["trees"] + scenery["saplings"] <= 9000, f"{w.label}: {scenery}"
    assert scenery["trail_length_m"] >= 2500, f"{w.label}: trail network {scenery['trail_length_m']} m"

    trails = w.manifest["areas"]["trails"]
    clearings = [(entry["center"][0], entry["center"][1], entry["radius"])
                 for entry in w.manifest["areas"]["clearings"]]
    assert len(trails) >= 5 and len(clearings) >= 4, f"{w.label}: network {len(trails)}/{len(clearings)}"
    assert 1 <= len(w.manifest["areas"]["camps"]) <= 3, f"{w.label}: camps"

    # Trees keep their tops under the 23 m cap and stay out of trails/clearings, except the
    # two designated mission pieces: the missing hiker's canopy and the follow occluder.
    missing = next(person for person in w.manifest["people"] if person["role"] == "missing_hiker")
    occluder = w.manifest["follow"]["occluder_xy"]
    trees = _tree_instances(w)
    assert len(trees) >= 1500, f"{w.label}: {len(trees)} tree visuals"
    for tree in trees:
        top = tree["z"] + ASSETS[tree["asset"]].native_height_m * tree["instance_scale"]
        assert top <= TREE_TOP_LIMIT_M, f"{w.label}: {tree['name']} top {top:.2f} m"
        if (math.hypot(tree["x"] - missing["position"][0], tree["y"] - missing["position"][1]) < 9.0
                or math.hypot(tree["x"] - occluder[0], tree["y"] - occluder[1]) < 8.0):
            continue
        sapling = tree["instance_scale"] < 0.5
        limit = 2.5 if sapling else 6.0
        distance = distance_to_paths((tree["x"], tree["y"]), trails)
        assert distance >= limit, f"{w.label}: {tree['name']} is {distance:.2f} m from a trail"
        for cx, cy, radius in clearings:
            assert math.hypot(tree["x"] - cx, tree["y"] - cy) >= radius, \
                f"{w.label}: {tree['name']} stands inside clearing ({cx:.0f}, {cy:.0f})"

    # The scenery sidecar covers every tree instance with at least one proxy.
    sidecar = json.loads((w.manifest_path.parent / f"{w.label}.scenery.json").read_text())
    proxies = sidecar["proxies"]
    assert len(proxies) >= len(trees), f"{w.label}: {len(proxies)} proxies for {len(trees)} trees"
    for tree in trees:
        assert any(math.hypot(proxy["transform"][0] - tree["x"],
                              proxy["transform"][1] - tree["y"]) < 0.05 for proxy in proxies), \
            f"{w.label}: no sidecar proxy at {tree['name']} ({tree['x']:.1f}, {tree['y']:.1f})"


@_all([("forest", seed) for seed in SEEDS])
def test_procedural_forest_mission_content_is_spread(worlds, kind, seed):
    """Mission targets live across the map, not next to home."""
    w = worlds(kind, seed)
    people = {person["role"]: person for person in w.manifest["people"]}
    missing = people["missing_hiker"]
    trails = w.manifest["areas"]["trails"]
    distance_home = math.hypot(*missing["position"])
    assert distance_home >= 150.0, f"{w.label}: missing hiker {distance_home:.1f} m from home"
    off_trail = distance_to_paths(missing["position"], trails)
    assert 6.0 <= off_trail <= 15.0, f"{w.label}: missing hiker {off_trail:.2f} m from a trail"

    clearing = w.manifest["areas"]["clearing"]
    assert math.hypot(*clearing["center"]) >= 120.0, f"{w.label}: mission clearing too close to home"
    for role in ("injured", "casualty"):
        assert math.hypot(*people[role]["position"]) >= 120.0, f"{w.label}: {role} near home"

    follow_start = w.manifest["follow"]["route"][0]
    assert math.hypot(*follow_start) >= 100.0, f"{w.label}: follow route starts {math.hypot(*follow_start):.1f} m out"
    quadrants = set()
    for person in w.manifest["people"]:
        if person["role"] == "hiker" and person.get("route"):
            midpoint = person["route"][len(person["route"]) // 2]
            quadrants.add((midpoint[0] >= 0, midpoint[1] >= 0))
    assert len(quadrants) >= 3, f"{w.label}: hikers only in {quadrants}"


# ---------------------------------------------------------------------------------------------
# Procedural 1.2 km city (urban) expectations
# ---------------------------------------------------------------------------------------------
def _urban_sidecar(w: World) -> list[dict]:
    """Every collision proxy of the world's scenery sidecar, in world coordinates."""
    path = w.manifest_path.parent / f"{w.label}.scenery.json"
    return json.loads(path.read_text())["proxies"]


def _urban_building_proxies(w: World) -> list[dict]:
    """The box proxies that stand in for buildings (rooftop equipment boxes are under 3 m)."""
    return [proxy for proxy in _urban_sidecar(w)
            if proxy["shape"] == "box" and proxy["size"][2] >= 3.0]


@_all([("urban", seed) for seed in SEEDS])
def test_procedural_urban_buildings_stay_clear_of_roads_and_sidewalks(worlds, kind, seed):
    """No building may reach into a carriageway or sidewalk band, and every building owns
    exactly one sidecar proxy while every collided tile is covered by the sidecar."""
    w = worlds(kind, seed)
    areas = w.manifest["areas"]
    assert areas["building_count"] >= 400, f"{w.label}: {areas['building_count']} buildings"
    buildings = _urban_building_proxies(w)
    assert len(buildings) == areas["building_count"], \
        f"{w.label}: {len(buildings)} proxies for {areas['building_count']} buildings"
    sidewalk = float(areas["sidewalk_width_m"])
    for axis, half_index in (("x", 0), ("y", 1)):
        for proxy in buildings:
            centre = proxy["transform"][0 if axis == "x" else 1]
            half_extent = proxy["size"][half_index] / 2
            for road, half in zip(areas["roads"][axis], areas["road_half_width_m"][axis]):
                assert abs(centre - road) >= half_extent + half + sidewalk - 1e-6, \
                    f"{w.label}: building {proxy['transform'][:2]} overlaps {axis}={road}"
    root = ET.parse(w.sdf_path).getroot()
    tiles = {model.get("name") for model in root.iter("model")
             if (model.get("name") or "").startswith("scenery_")
             and model.find('.//collision[@name="scenery_collision"]') is not None}
    covered = {proxy["model"].split("/")[0] for proxy in _urban_sidecar(w)}
    assert tiles == covered, f"{w.label}: tiles without sidecar proxies {sorted(tiles - covered)}"


@_all([("urban", seed) for seed in SEEDS])
def test_procedural_urban_building_tops_stay_within_the_cap(worlds, kind, seed):
    """Downtown tops (rooftop equipment included) stay under 34 m and clear of the transit."""
    w = worlds(kind, seed)
    tops = []
    for proxy in _urban_sidecar(w):
        if proxy["shape"] == "box":
            top = proxy["transform"][2] + proxy["size"][2] / 2
        elif proxy["shape"] == "cylinder":
            top = proxy["transform"][2] + proxy["size"][1] / 2
        else:
            top = proxy["transform"][2] + proxy["size"][0]
        tops.append(top)
    assert max(tops) <= 34.0, f"{w.label}: tallest scenery top {max(tops):.2f} m"
    tallest_building = max(proxy["transform"][2] + proxy["size"][2] / 2
                           for proxy in _urban_building_proxies(w))
    transit = w.manifest["transit_altitude_m"]
    assert tallest_building + 5.9 <= transit, \
        f"{w.label}: transit {transit} does not clear {tallest_building:.2f} m"


@_all([("urban", seed) for seed in SEEDS])
def test_procedural_urban_mission_content_is_spread_across_the_map(worlds, kind, seed):
    """Red shirt, casualty and follow route sit far from home; sites cover the map."""
    w = worlds(kind, seed)
    people = {person["role"]: person for person in w.manifest["people"]}
    for role, minimum in (("red_shirt", 150.0), ("collapsed", 120.0)):
        distance = math.hypot(*people[role]["position"])
        assert distance >= minimum, f"{w.label}: {role} {distance:.1f} m from home"
    follow_start = w.manifest["follow"]["route"][0]
    assert math.hypot(*follow_start) >= 100.0, \
        f"{w.label}: follow route starts {math.hypot(*follow_start):.1f} m out"

    sites = {site["name"]: site for site in w.manifest["sites"]}
    assert {"home_plaza", "mission_plaza", "mission_park", "downtown", "bus_stop", "collapse",
            "follow_occluder"} <= set(sites), f"{w.label}: sites {sorted(sites)}"
    assert math.hypot(*sites["mission_plaza"]["center"]) >= 150.0
    quadrants = {(site["center"][0] >= 0, site["center"][1] >= 0) for site in w.manifest["sites"]}
    assert len(quadrants) >= 3, f"{w.label}: mission sites only in {quadrants}"

    areas = w.manifest["areas"]
    assert len(areas["plazas"]) >= 2, f"{w.label}: {len(areas['plazas'])} plazas"
    assert 3 <= len(areas["parks"]) <= 6, f"{w.label}: {len(areas['parks'])} parks"
    mission_plaza = next(plaza for plaza in areas["plazas"] if plaza["mission"])
    for person in w.manifest["people"]:
        if person["role"] in ("plaza_crowd", "red_shirt"):
            dx = abs(person["position"][0] - mission_plaza["center"][0])
            dy = abs(person["position"][1] - mission_plaza["center"][1])
            assert dx <= mission_plaza["half_size"] and dy <= mission_plaza["half_size"], \
                f"{w.label}: {person['model']} outside the mission plaza"


@_all([("urban", seed) for seed in SEEDS])
def test_procedural_urban_traffic_is_mesh_vehicles_only(worlds, kind, seed):
    """Circulating vehicles are mesh-backed (a heavy bus loop included), never primitives."""
    w = worlds(kind, seed)
    from sim.worldgen.vehicles import vehicle_type

    types = [vehicle["type"] for vehicle in w.manifest["vehicles"]]
    assert types, f"{w.label}: no circulating traffic"
    for name in types:
        kind_spec = vehicle_type(name)
        assert kind_spec.mesh is not None and kind_spec.assets, \
            f"{w.label}: vehicle {name} is a primitive body"
    assert any(vehicle_type(name).heavy for name in types), f"{w.label}: no bus/truck loop"
    areas = w.manifest["areas"]
    assert areas["roads"]["x"] and areas["roads"]["y"]
    assert w.manifest["follow"]["occluder_models_or_collisions"], f"{w.label}: no occluder models"


@_all([("urban", seed) for seed in SEEDS])
def test_procedural_urban_scenery_matches_the_design(worlds, kind, seed):
    """Roads, sidewalks, zebra crossings, plazas, parks, trees, lamps and shelters are all
    merged/instanced scenery meshes."""
    w = worlds(kind, seed)
    names = [visual["name"] or "" for visual in _scenery_tile_visuals(w)]
    for prefix in ("facade_", "roof_", "asphalt_", "sidewalk_", "crosswalk_", "plaza_",
                   "lawn_", "path_"):
        assert any(name.startswith(prefix) for name in names), f"{w.label}: no {prefix}* mesh"
    trees = [name for name in names if name.startswith(("tree_oak", "tree_pine"))]
    assert len(trees) >= 50, f"{w.label}: {len(trees)} tree visuals"
    lamps = [name for name in names if name.startswith("lamp_post")]
    assert len(lamps) >= 100, f"{w.label}: {len(lamps)} lamps"
    benches = [name for name in names if name.startswith("bench_a")]
    assert len(benches) >= 4, f"{w.label}: {len(benches)} benches"
    assert any(name.startswith("bus_stop_shelter") for name in names), f"{w.label}: no bus shelter"
    assert any(name.startswith("follow_shelter") for name in names), f"{w.label}: no follow shelter"


@_all([(kind, seed) for kind in ("forest", "urban") for seed in SEEDS])
def test_procedural_scenery_tiles_contain_no_primitive_geometry(worlds, kind, seed):
    w = worlds(kind, seed)
    root = ET.parse(w.sdf_path).getroot()
    for model in root.iter("model"):
        if (model.get("name") or "").startswith("scenery_"):
            for tag in ("sphere", "cone", "cylinder", "box"):
                assert model.find(f".//{tag}") is None, \
                    f"{w.label}: scenery tile {model.get('name')} contains a primitive <{tag}>"
