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

from sim.scripts.civilian_spec import load_scenario
from sim.scripts.civilian_life import MotionEngine
from sim.worldgen import build_world
from sim.worldgen.assets import ASSETS
from sim.worldgen.geometry import CollisionWorld, load_collisions
from sim.worldgen.layout import distance_to_paths
from sim.worldgen.people import PERSON_ASSETS
from sim.worldgen.plan import Ground

ROOT = Path(__file__).resolve().parents[1]
SEEDS = range(1, int(os.environ.get("HORUS_WORLDGEN_SEEDS", "8")) + 1)
WORLDS = [(kind, seed) for kind in ("forest", "urban") for seed in SEEDS]
CANONICAL = [("forest", None), ("urban", None)]
# Canonical worlds keep the 60 m actor inset they shipped with; generated worlds carry their
# flight profile's actor inset in the manifest (actor_geofence_m).
ACTOR_INSET_M = 60.0
RTL_ALT_M = 45.0  # config/ardupilot/outdoor.parm


def _profile(kind: str) -> dict:
    path = ROOT / "config" / f"horus_procedural_{kind}.yaml"
    return yaml.safe_load(path.read_text())["executor"]


PROFILES = {kind: _profile(kind) for kind in ("forest", "urban")}
# The generators' transit-altitude ceilings (sim/worldgen/*: checked_transit_altitude).
MAX_ALTITUDE = {"forest": 36.0, "urban": 48.0}
TRANSIT_MIN_M = {kind: float(PROFILES[kind]["cruise_min_altitude_m"]) for kind in PROFILES}
TREE_TOP_LIMIT_M = 23.0  # config comment: forest trees stay below 23 m (transit ceiling 36 m)


def _actor_inset(manifest: dict) -> float:
    return float(manifest.get("actor_geofence_m", ACTOR_INSET_M))


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
def test_people_and_vehicles_stay_inside_the_actor_area_and_off_obstacles(worlds, kind, seed):
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

    # Trees keep their tops under the 23 m cap and stay out of trails/clearings.
    trees = _tree_instances(w)
    assert len(trees) >= 1500, f"{w.label}: {len(trees)} tree visuals"
    for tree in trees:
        top = tree["z"] + ASSETS[tree["asset"]].native_height_m * tree["instance_scale"]
        assert top <= TREE_TOP_LIMIT_M, f"{w.label}: {tree['name']} top {top:.2f} m"
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
def test_procedural_forest_hikers_are_spread_across_quadrants(worlds, kind, seed):
    w = worlds(kind, seed)
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
def test_procedural_urban_open_spaces_host_their_people(worlds, kind, seed):
    """Plazas and parks exist, and the standing crowd stays inside its plaza."""
    w = worlds(kind, seed)
    areas = w.manifest["areas"]
    assert len(areas["plazas"]) >= 2, f"{w.label}: {len(areas['plazas'])} plazas"
    assert 3 <= len(areas["parks"]) <= 6, f"{w.label}: {len(areas['parks'])} parks"
    crowd_plaza = next(plaza for plaza in areas["plazas"] if plaza["crowd"])
    for person in w.manifest["people"]:
        if person["role"] == "plaza_crowd":
            dx = abs(person["position"][0] - crowd_plaza["center"][0])
            dy = abs(person["position"][1] - crowd_plaza["center"][1])
            assert dx <= crowd_plaza["half_size"] and dy <= crowd_plaza["half_size"], \
                f"{w.label}: {person['model']} outside the crowd plaza"


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


@_all([(kind, seed) for kind in ("forest", "urban") for seed in SEEDS])
def test_procedural_scenery_tiles_contain_no_primitive_geometry(worlds, kind, seed):
    w = worlds(kind, seed)
    root = ET.parse(w.sdf_path).getroot()
    for model in root.iter("model"):
        if (model.get("name") or "").startswith("scenery_"):
            for tag in ("sphere", "cone", "cylinder", "box"):
                assert model.find(f".//{tag}") is None, \
                    f"{w.label}: scenery tile {model.get('name')} contains a primitive <{tag}>"
