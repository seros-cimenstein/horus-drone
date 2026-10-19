from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import TypeAlias

from sim.worldgen import forest, forest_procedural, urban, urban_procedural
from sim.worldgen.appearance import Appearance
from sim.worldgen.dressing import Dressing, canonical_dressing, sample_dressing
from sim.worldgen.forest import build_forest
from sim.worldgen.forest_procedural import build_forest_procedural, sample_forest_layout
from sim.worldgen.layout import (
    ForestLayout, UrbanLayout, canonical_forest_layout, canonical_urban_layout,
)
from sim.worldgen.paths import FilletedPath
from sim.worldgen.scenario import scenario_text
from sim.worldgen.urban import build_urban
from sim.worldgen.urban_procedural import build_urban_procedural
from sim.worldgen.urban_sampler import sample_urban_layout
from sim.worldgen.validation import measure_follow, validate_world
from sim.worldgen.vehicles import vehicle_type

ROOT = Path(__file__).resolve().parents[2]
SIM_DIR = ROOT / "sim"
WORLD_DIR = SIM_DIR / "worlds"
MODEL_DIR = SIM_DIR / "models"
SCENARIO_DIR = SIM_DIR / "scenarios"
FileValue: TypeAlias = bytes | str
# Each kind's world modules provide ``transit_altitude(layout)`` and
# ``manifest_extras(layout, people) -> (follow, areas, extra fields)``.
_WORLD_MODULES = {(kind, canonical): module for kind, canonical, module in (
    ("forest", True, forest), ("forest", False, forest_procedural),
    ("urban", True, urban), ("urban", False, urban_procedural))}
_SAMPLERS = {"forest": sample_forest_layout, "urban": sample_urban_layout}


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


def _manifest_number(value: float) -> float | int:
    """A JSON number that keeps integral map extents ints (canonical manifests are byte-frozen)."""
    return int(value) if float(value).is_integer() else float(value)


def _vehicle_manifest(agent: dict) -> dict:
    return {
        "model": agent["model"], "type": agent["asset"],
        "behavior": agent["behavior"],
        "position": [float(agent["position"][0]), float(agent["position"][1])],
        "yaw_deg": math.degrees(agent.get("yaw", 0.0)),
        "footprint": agent["footprint"], "variant": agent["variant"],
        "route": [[float(p[0]), float(p[1])] for p in agent["waypoints"]],
        "speed_m_s": agent["speed"], "turn_radius_m": agent["turn_radius"],
    }


def _manifest(layout: ForestLayout | UrbanLayout, kind: str, stem: str,
              scenario_path: str, ground_path: str | None, sdf_path: str,
              agents: list[dict], dressing: Dressing) -> dict:
    people = [_person_manifest(agent) for agent in agents if agent.get("kind", "pedestrian") == "pedestrian"]
    animals = [_animal_manifest(agent) for agent in agents if agent.get("kind") == "animal"]
    vehicles = [_vehicle_manifest(agent) for agent in agents if agent.get("kind") == "vehicle"]
    world = _WORLD_MODULES[(kind, layout.canonical)]
    follow, areas, extras = world.manifest_extras(layout, {person["model"]: person for person in people})
    ground = None if kind == "urban" else {
        "file": ground_path,
        "origin_xy": [_manifest_number(layout.map.min), _manifest_number(layout.map.min)],
        "cell_m": layout.map.cell_m,
    }
    edge_min = _manifest_number(layout.map.min)
    edge_max = _manifest_number(layout.map.min + layout.map.size)
    return {
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
        "transit_altitude_m": world.transit_altitude(layout),
        "areas": areas,
        "attempts": layout.attempts,
        "dressing": dressing.manifest(),
        **extras,
    }


def _dressing(kind: str, layout: ForestLayout | UrbanLayout, stratum: str | None) -> Dressing:
    return canonical_dressing() if layout.canonical else sample_dressing(kind, layout, stratum=stratum)


def _build(kind: str, layout: ForestLayout | UrbanLayout, terrain_name: str,
           dressing: Dressing, *, textures: bool):
    """``(sdf, agents, ground rows or None, files)`` from the kind's canonical or generated
    world builder."""
    if kind == "forest":
        build = build_forest if layout.canonical else build_forest_procedural
        return build(layout, WORLD_DIR, terrain_name, textures=textures, dressing=dressing)
    build = build_urban if layout.canonical else build_urban_procedural
    sdf, agents, files = build(layout, WORLD_DIR, textures=textures, dressing=dressing)
    return sdf, agents, None, files


def _validate_candidate(kind: str, layout: ForestLayout | UrbanLayout,
                        stratum: str | None = None) -> tuple[float, list[str]]:
    stem = f"horus_{kind}_p{layout.seed}"
    dressing = _dressing(kind, layout, stratum)
    sdf, agents, _rows, files = _build(kind, layout, f"{stem}_terrain", dressing, textures=False)
    manifest = _manifest(layout, kind, stem, f"{stem}.yaml",
                         f"{stem}_ground.npy" if kind == "forest" else None, f"{stem}.sdf", agents, dressing)
    return validate_world(kind, layout, sdf, manifest, agents=agents, files=files,
                          model_dir=MODEL_DIR, world_dir=WORLD_DIR)


def _validate_canonical_routes(layout: ForestLayout | UrbanLayout) -> None:
    """Canonical routes are hand-authored; refuse to emit an undrivable vehicle loop."""
    for vehicle in getattr(layout, "vehicles", ()):
        points = [(point[0], point[1], point[2] if len(point) > 2 else 0.0)
                  for point in vehicle.waypoints]
        try:
            FilletedPath(points, vehicle_type(vehicle.type).turn_radius_m, closed=True,
                         name=vehicle.model)
        except ValueError as exc:
            raise ValueError(f"canonical vehicle route {vehicle.model} is not drivable: {exc}") from exc


def build_world(kind: str, seed: int, *, procedural: bool,
                textures: bool = True, stratum: str | None = None) -> WorldBundle:
    """Build a deterministic outdoor world without writing to disk."""
    if kind not in ("forest", "urban"):
        raise ValueError("kind must be 'forest' or 'urban'")
    if procedural:
        validator = lambda candidate: _validate_candidate(kind, candidate, stratum)
        layout = _SAMPLERS[kind](seed, validate=validator)
        stem = f"horus_{kind}_p{seed}"
    else:
        layout = canonical_forest_layout(seed) if kind == "forest" else canonical_urban_layout(seed)
        stem = f"horus_{kind}"
        _validate_canonical_routes(layout)
    terrain_name = (f"{stem}_terrain" if procedural else "horus_forest_terrain")
    dressing = _dressing(kind, layout, stratum)
    scenario_path = f"{stem}.yaml" if procedural else f"../scenarios/{stem}.yaml"
    sdf, agents, ground_rows, files = _build(kind, layout, terrain_name, dressing, textures=textures)
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
