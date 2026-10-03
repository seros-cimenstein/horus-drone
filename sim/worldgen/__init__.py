from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import TypeAlias

from sim.worldgen.appearance import Appearance
from sim.worldgen.dressing import Dressing, canonical_dressing, sample_dressing
from sim.worldgen.forest import build_forest
from sim.worldgen.forest_procedural import sample_forest_layout
from sim.worldgen.layout import (
    ForestLayout, UrbanLayout, canonical_forest_layout, canonical_urban_layout,
)
from sim.worldgen.scenario import scenario_text
from sim.worldgen.urban import build_urban
from sim.worldgen.urban_procedural import sample_urban_layout

ROOT = Path(__file__).resolve().parents[2]
SIM_DIR = ROOT / "sim"
WORLD_DIR = SIM_DIR / "worlds"
MODEL_DIR = SIM_DIR / "models"
SCENARIO_DIR = SIM_DIR / "scenarios"
FileValue: TypeAlias = bytes | str


@dataclass
class WorldBundle:
    stem: str
    sdf: str
    scenario_yaml: str
    manifest: dict
    ground_rows: list[list[float]] | None
    files: dict[str, FileValue]

    def write(self, out_dir: Path | str) -> list[Path]:
        """Write this bundle; canonical paths mirror the checked-in sim tree."""
        destination = Path(out_dir)
        canonical = bool(self.manifest["canonical"])
        if canonical:
            world_root = destination / "sim" / "worlds"
            scenario_root = destination / "sim" / "scenarios"
            model_root = destination / "sim" / "models"
        else:
            world_root = destination / self.stem
            scenario_root = world_root
            model_root = world_root / "models"
            model_root.mkdir(parents=True, exist_ok=True)
        world_path = world_root / f"{self.stem}.sdf"
        scenario_path = scenario_root / f"{self.stem}.yaml"
        manifest_path = world_root / f"{self.stem}.layout.json"
        for path, content in ((world_path, self.sdf),
                              (scenario_path, self.scenario_yaml),
                              (manifest_path, json.dumps(self.manifest, indent=2) + "\n")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        written = [world_path, scenario_path, manifest_path]
        for relative, content in self.files.items():
            if relative.startswith("models/"):
                path = model_root / relative.removeprefix("models/")
            elif relative.endswith(".npy"):
                path = scenario_root / relative if canonical else world_root / relative
            else:
                path = world_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
            written.append(path)
        return written


def _person_manifest(person: dict) -> dict:
    record = {
        "model": person["model"], "role": person["role"], "asset": person["asset"],
        "group": person.get("group", "ambient"),
        "behavior": person["behavior"],
        "position": [float(person["position"][0]), float(person["position"][1])],
        "yaw_deg": math.degrees(person.get("yaw", 0.0)),
        "posture": person["posture"],
        "clothing": person["clothing"],
        "shirt_colour_attribute": person["shirt_colour_attribute"],
        "shirt_colour_source": person["shirt_colour_source"],
        "possible_shirt_colours": person["possible_shirt_colours"],
        "envelope": person["envelope"],
    }
    appearance = person.get("appearance")
    if isinstance(appearance, Appearance):
        record["appearance"] = appearance.manifest()
    if person.get("waypoints"):
        record["route"] = [[float(p[0]), float(p[1])] for p in person["waypoints"]]
        record["route_mode"] = person.get("mode", "loop")
        record["speed_m_s"] = person["speed"]
    return record


def _animal_manifest(animal: dict) -> dict:
    record = {key: animal[key] for key in ("model", "species", "behavior", "scale", "coat", "envelope")}
    record["position"] = [float(animal["position"][0]), float(animal["position"][1])]
    for key in ("leader", "area"):
        if key in animal:
            record[key] = animal[key]
    return record


def _route(person) -> list[list[float]]:
    return [[float(point[0]), float(point[1])] for point in person.waypoints]


def _transit_altitude(layout: ForestLayout | UrbanLayout) -> float:
    procedural = not layout.canonical
    if isinstance(layout, ForestLayout):
        if layout.canonical:
            required = 2.2 + layout.tree_height_range[1] * 1.18 + 6.0
        else:
            # Procedural rule (config/horus_procedural_forest.yaml): >= cruise_min 26 m,
            # >= tallest scenery top + 5.9 m, <= max_altitude 36 m.
            required = max(26.0, layout.scenery_max_top_m + 5.9)
        limit = 36.0
    elif layout.canonical:
        building_tops = [b.height + (1.23 if b.height > 18 else 0.58)
                         for b in layout.buildings]
        park_tops = [tree.height * 1.08 for tree in layout.park_trees]
        required = max([9.6, *building_tops, *park_tops]) + 6.0
        limit = 45.0
    else:
        # Procedural urban rule (config/horus_procedural_urban.yaml): >= 38 m, >= tallest
        # scenery top + 5.9 m, <= 48 m.
        required = max(38.0, getattr(layout, "scenery_max_top_m", 0.0) + 5.9)
        limit = 48.0
    if required > limit:
        raise ValueError(f"layout needs {required:.2f} m transit altitude, above {limit:.0f} m limit")
    if procedural:
        # The ceiling keeps the invariant transit >= tallest top + 5.9 m exact (round()
        # could fall up to 5 mm short of it).
        return math.ceil(required * 100.0) / 100.0
    return round(required, 2)


def _manifest_number(value: float) -> float | int:
    """A JSON number that keeps integral map extents ints (canonical manifests are byte-frozen)."""
    return int(value) if float(value).is_integer() else float(value)


def _manifest(layout: ForestLayout | UrbanLayout, kind: str, stem: str,
              scenario_path: str, ground_path: str | None, sdf_path: str,
              agents: list[dict], dressing: Dressing) -> dict:
    people = [_person_manifest(agent) for agent in agents if agent.get("kind", "pedestrian") == "pedestrian"]
    animals = [_animal_manifest(agent) for agent in agents if agent.get("kind") == "animal"]
    vehicles = [{
        "model": agent["model"], "type": agent["asset"],
        "behavior": agent["behavior"],
        "position": [float(agent["position"][0]), float(agent["position"][1])],
        "yaw_deg": math.degrees(agent.get("yaw", 0.0)),
        "footprint": agent["footprint"], "variant": agent["variant"],
        "route": [[float(p[0]), float(p[1])] for p in agent["waypoints"]],
        "speed_m_s": agent["speed"], "turn_radius_m": agent["turn_radius"],
    } for agent in agents if agent.get("kind") == "vehicle"]
    by_model = {person["model"]: person for person in people}
    if isinstance(layout, ForestLayout):
        follow_person = layout.follow_hiker
        occluders = (["follow_occlusion_bush_lower_collision",
                      "follow_occlusion_bush_middle_collision",
                      "follow_occlusion_bush_upper_collision"] if layout.canonical else
                     # Procedural forests occlude with a foliage SceneryMesh; its collision
                     # proxies live in the scenery sidecar under this mesh name.
                     ["follow_occluder"])
        expected_occlusion = layout.expected_occlusion_s
        follow = {
            "model": follow_person.model, "group": follow_person.group,
            "route": _route(follow_person), "speed_m_s": layout.follow_speed,
            "occluder_models_or_collisions": occluders,
            "occluder_xy": [layout.follow_occluder_xy[0], layout.follow_occluder_xy[1]],
            "occluder_radius_m": (None if layout.canonical else
                                  layout.follow_occluder_radius_m),
            "follower_altitude_m": layout.follower_altitude_m,
            "follower_horizontal_m": layout.follower_horizontal_m,
            "expected_occlusion_s": expected_occlusion,
            "shirt_colour_attribute": by_model[follow_person.model]["shirt_colour_attribute"],
        }
        if layout.canonical:
            follow["stage_xy"] = [-12.1006, -15.7487]
        areas = {
            "trail": [[x, y] for x, y in layout.trail],
            "creek": [[x, y] for x, y in layout.creek],
            "clearing": {"center": list(layout.clearing), "radius": layout.clearing_radius},
            "camp": list(layout.camp),
        }
        if not layout.canonical:
            areas["trails"] = [[[x, y] for x, y in trail] for trail in layout.trails]
            areas["creeks"] = [[[x, y] for x, y in creek] for creek in layout.creeks]
            areas["clearings"] = [{"center": [cx, cy], "radius": radius}
                                  for cx, cy, radius in layout.clearings]
            areas["camps"] = [[x, y] for x, y in layout.camps]
        ground = {"file": ground_path,
                  "origin_xy": [_manifest_number(layout.map.min), _manifest_number(layout.map.min)],
                  "cell_m": layout.map.cell_m}
    else:
        follow_person = layout.follow_jogger
        occluder_names = {
            "shelter": "jogger_shelter_roof_collision",
            "awning": "jogger_awning_collision",
            "tree": "jogger_tree_crown_collision",
        }
        follow = {
            "model": follow_person.model, "group": follow_person.group,
            "route": _route(follow_person), "speed_m_s": layout.follow_speed,
            "occluder_models_or_collisions": [occluder_names[layout.follow_occluder_kind]],
            "occluder_type": layout.follow_occluder_kind,
            "occluder_xy": [layout.jogger_shelter_center[0], layout.jogger_shelter_center[1]],
            "follower_altitude_m": layout.follower_altitude_m,
            "follower_horizontal_m": layout.follower_horizontal_m,
            "expected_occlusion_s": layout.expected_occlusion_s,
            "shirt_colour_attribute": by_model[follow_person.model]["shirt_colour_attribute"],
        }
        if layout.canonical:
            follow["stage_xy"] = [17.32, -40.5]
        areas = {
            "roads": {"x": list(layout.roads_x), "y": list(layout.roads_y)},
            "sidewalk_offset_m": 5.65,
            "plaza": {"center": [0, 0], "half_size": layout.plaza_half_size},
            "bus_stop": list(layout.bus_shelter_center),
            "park": {"center": list(layout.park_center), "half_size": layout.park_half_size},
            "crosswalk_centres": [list(p) for p in layout.crosswalk_centres],
        }
        if layout.city is not None:
            from sim.worldgen.urban_procedural import STREET_HALF_M, SIDEWALK_WIDTH_M, ARTERIAL_HALF_M

            city = layout.city
            mission = next(plaza for plaza in city.plazas if plaza.mission)
            areas["plaza"] = {"center": [mission.x, mission.y], "half_size": mission.half_size}
            areas["plazas"] = [{"center": [p.x, p.y], "half_size": p.half_size, "mission": p.mission}
                               for p in city.plazas]
            areas["parks"] = [{"center": [p.x, p.y], "half_size": p.half_size, "mission": p.mission}
                              for p in city.parks]
            areas["arterial_roads"] = {"x": list(city.arterial_roads_x),
                                       "y": list(city.arterial_roads_y)}
            areas["road_half_width_m"] = {
                "x": [ARTERIAL_HALF_M if road in city.arterial_roads_x else STREET_HALF_M
                      for road in layout.roads_x],
                "y": [ARTERIAL_HALF_M if road in city.arterial_roads_y else STREET_HALF_M
                      for road in layout.roads_y],
            }
            areas["sidewalk_width_m"] = SIDEWALK_WIDTH_M
            areas["building_count"] = len(layout.buildings)
            occluder_names = ["follow_shelter_glass", "follow_shelter_roof"]
            follow["occluder_models_or_collisions"] = occluder_names
        ground = None
    edge_min = _manifest_number(layout.map.min)
    edge_max = _manifest_number(layout.map.min + layout.map.size)
    manifest = {
        "world": stem,
        "kind": kind,
        "seed": layout.seed,
        "canonical": layout.canonical,
        "bounds": {"x": [edge_min, edge_max], "y": [edge_min, edge_max]},
        "home": [0, 0],
        "scenario": scenario_path,
        "sdf": sdf_path,
        "ground": ground,
        "people": people,
        "vehicles": vehicles,
        "animals": animals,
        "follow": follow,
        "transit_altitude_m": _transit_altitude(layout),
        "areas": areas,
        "attempts": layout.attempts,
        "dressing": dressing.manifest(),
    }
    if not layout.canonical:
        # Procedural-only fields: mission sites, the actor geofence inset and the planner
        # limits of the world's flight profile (config/horus_procedural_<kind>.yaml).
        manifest["sites"] = [{"name": name, "kind": site_kind, "center": [x, y], "radius": radius}
                             for name, site_kind, x, y, radius in layout.sites]
        manifest["actor_geofence_m"] = layout.actor_inset_m
        manifest["flight"] = {"vantage_bounds_m": 585.0, "max_leg_m": 850.0}
        if isinstance(layout, ForestLayout):
            manifest["scenery"] = dict(layout.scenery_counts)
    return manifest


def _dressing(kind: str, layout: ForestLayout | UrbanLayout, stratum: str | None) -> Dressing:
    return canonical_dressing() if layout.canonical else sample_dressing(kind, layout, stratum=stratum)


def _build(kind: str, layout: ForestLayout | UrbanLayout, stem: str, terrain_name: str,
           dressing: Dressing, *, textures: bool):
    if kind == "forest":
        sdf, agents, rows, files = build_forest(layout, WORLD_DIR, terrain_name, textures=textures,
                                                dressing=dressing)
    else:
        sdf, agents, files = build_urban(layout, WORLD_DIR, textures=textures, dressing=dressing)
        rows = None
    return sdf, agents, rows, files


def _validate_candidate(kind: str, layout: ForestLayout | UrbanLayout,
                        stratum: str | None = None) -> float | None:
    stem = f"horus_{kind}_p{layout.seed}"
    dressing = _dressing(kind, layout, stratum)
    sdf, agents, _rows, files = _build(kind, layout, stem, f"{stem}_terrain", dressing, textures=False)
    manifest = _manifest(layout, kind, stem, f"{stem}.yaml",
                         f"{stem}_ground.npy" if kind == "forest" else None, f"{stem}.sdf", agents, dressing)
    from sim.worldgen.validation import validate_world

    return validate_world(kind, layout, sdf, manifest, agents=agents, files=files,
                          model_dir=MODEL_DIR, world_dir=WORLD_DIR)


def _validate_canonical_routes(layout: ForestLayout | UrbanLayout) -> None:
    """Canonical routes are hand-authored; refuse to emit an undrivable vehicle loop."""
    from sim.worldgen.paths import FilletedPath
    from sim.worldgen.vehicles import vehicle_type

    for vehicle in getattr(layout, "vehicles", ()):
        points = [(point[0], point[1], point[2] if len(point) > 2 else 0.0)
                  for point in vehicle.waypoints]
        try:
            FilletedPath(points, vehicle_type(vehicle.type).turn_radius_m, closed=True,
                         name=vehicle.model)
        except ValueError as exc:
            raise ValueError(f"canonical vehicle route {vehicle.model} is not drivable: {exc}") from exc


def _procedural_sampler(kind: str):
    """The 1.2 km sampler for one world kind (each kind owns its module)."""
    if kind == "forest":
        from sim.worldgen.forest_procedural import sample_forest_layout

        return sample_forest_layout
    from sim.worldgen.urban_procedural import sample_urban_layout

    return sample_urban_layout


def build_world(kind: str, seed: int, *, procedural: bool,
                textures: bool = True, stratum: str | None = None) -> WorldBundle:
    """Build a deterministic outdoor world without writing to disk."""
    if kind not in ("forest", "urban"):
        raise ValueError("kind must be 'forest' or 'urban'")
    if procedural:
        validator = lambda candidate: _validate_candidate(kind, candidate, stratum)
        layout = _procedural_sampler(kind)(seed, validate=validator)
        stem = f"horus_{kind}_p{seed}"
    else:
        layout = canonical_forest_layout(seed) if kind == "forest" else canonical_urban_layout(seed)
        stem = f"horus_{kind}"
        _validate_canonical_routes(layout)
    terrain_name = (f"{stem}_terrain" if procedural else "horus_forest_terrain")
    dressing = _dressing(kind, layout, stratum)
    scenario_path = f"{stem}.yaml" if procedural else f"../scenarios/{stem}.yaml"
    sdf, agents, ground_rows, files = _build(kind, layout, stem, terrain_name, dressing, textures=textures)
    if kind == "forest":
        ground_name = "horus_forest_ground.npy" if not procedural else f"{stem}_ground.npy"
        ground_path = ground_name if procedural else f"../scenarios/{ground_name}"
    else:
        ground_name = ground_path = None
    scenario = scenario_text(stem, layout.seed, agents, ground_name, events=dressing.events,
                             interactions=True, ground_map=layout.map)
    manifest = _manifest(layout, kind, stem, scenario_path,
                         ground_path, f"{stem}.sdf", agents, dressing)
    if not procedural:
        from sim.worldgen.validation import measure_follow

        plan = measure_follow(kind, layout, sdf, manifest,
                              model_dir=MODEL_DIR, world_dir=WORLD_DIR)
        manifest["follow"]["expected_occlusion_s"] = plan.occluded_s
    return WorldBundle(stem, sdf, scenario, manifest, ground_rows, files)


def write_outdoor_drone_model(model_dir: Path) -> list[Path]:
    model = model_dir / "horus_iris_outdoor"
    model.mkdir(parents=True, exist_ok=True)
    sdf_path = model / "model.sdf"
    config_path = model / "model.config"
    sdf_path.write_text('''<?xml version="1.0"?>
<sdf version="1.9"><model name="horus_iris_outdoor"><include><uri>model://horus_iris_with_gimbal_outdoor</uri><name>iris_with_gimbal</name></include></model></sdf>
''', encoding="utf-8")
    config_path.write_text('''<?xml version="1.0"?>
<model><name>horus_iris_outdoor</name><version>1.0</version><sdf version="1.9">model.sdf</sdf><author><name>HORUS</name></author><description>Outdoor Iris camera, gimbal and ToF proximity payload.</description></model>
''', encoding="utf-8")
    return [sdf_path, config_path]


def main(argv: list[str] | None = None) -> None:
    import argparse
    from sim.worldgen.people import write_person_models

    parser = argparse.ArgumentParser(description="Generate reproducible or procedural outdoor Gazebo worlds.")
    parser.add_argument("--seed", type=int, default=2026,
                        help="base canonical seed, or direct procedural seed (default: 2026)")
    parser.add_argument("--procedural", action="store_true",
                        help="sample a new layout from the supplied seed")
    parser.add_argument("--kind", choices=("forest", "urban"),
                        help="generate one kind (default: both)")
    parser.add_argument("--out", type=Path, default=SIM_DIR / "generated",
                        help="procedural output root (default: sim/generated)")
    parser.add_argument("--stratum", choices=("baseline", "chaotic"),
                        help="force a procedural dressing stratum (default: sampled from the seed)")
    args = parser.parse_args(argv)
    if args.stratum and not args.procedural:
        parser.error("--stratum applies to --procedural worlds only")
    write_person_models(MODEL_DIR)
    shared_paths = write_outdoor_drone_model(MODEL_DIR)
    kinds = (args.kind,) if args.kind else ("forest", "urban")
    for kind in kinds:
        world_seed = args.seed if args.procedural else args.seed + (1 if kind == "forest" else 2)
        try:
            bundle = build_world(kind, world_seed, procedural=args.procedural, stratum=args.stratum)
        except RuntimeError as exc:
            parser.exit(1, f"error: {exc}\n")
        paths = bundle.write(args.out if args.procedural else ROOT)
        print(f"Generated {kind} seed {world_seed} ({bundle.stem}):")
        for path in [*paths, *shared_paths]:
            print(f"  {path}")


__all__ = ["WorldBundle", "build_world", "canonical_forest_layout",
           "canonical_urban_layout", "sample_forest_layout", "sample_urban_layout"]
