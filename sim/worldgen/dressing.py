"""Seeded world dressing layered over a validated layout.

Everything here draws from independent hashed streams (``seeding.substream``) so the layout
sampler's own stream, and therefore every existing seed's roads, trails, routes and occluders,
is untouched. Canonical worlds keep their fixed legacy dressing.

Strata: ``baseline`` worlds get mild everyday variation; ``chaotic`` worlds (about a third of
seeds, or forced by the caller) add harsher light, more animals and clutter, a milling crowd
and a denser schedule of sim-time disturbances (vehicle breakdowns, startles, reversals).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import random
from typing import Any

from sim.worldgen.appearance import Appearance, sample_appearance
from sim.worldgen.environment import CANONICAL_ENVIRONMENT, Environment, sample_environment
from sim.worldgen.fauna import animal_envelope, animal_model, sample_individual, species
from sim.worldgen.layout import (
    GEOFENCE_INSET_M, HOME_PAD_KEEPOUT_M, URBAN_SIDEWALK_WIDTH_M, ForestLayout, UrbanLayout,
    distance_to_path, distance_to_paths, point_segment_distance, road_half_width,
)
from sim.worldgen.people import (
    PERSON_ASSETS, SITTING_CLOTHING, SITTING_POSSIBLE_SHIRT_COLOURS, SITTING_SHIRT_COLOUR,
    actor_model_files, legacy_envelope,
)
from sim.worldgen.meshes import LoftMesh, rotate_xy
from sim.worldgen.proxy import ProxyShape
from sim.worldgen.scenery import SceneryInstance, SceneryMesh
from sim.worldgen.seeding import substream

STRATA = ("baseline", "chaotic")
KEEP_RIGHT_M = 0.4
PEDESTRIAN_ACCEL_M_S2 = 1.2
JOGGER_ACCEL_M_S2 = 2.0


@dataclass
class Dressing:
    stratum: str
    environment: Environment
    appearances: dict[str, Appearance] = field(default_factory=dict)
    animals: list[dict] = field(default_factory=list)
    clutter: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    asset_prefix: str = ""
    # Half-extent actor routes/areas must stay inside (procedural worlds: the flight profile
    # polygon minus a margin; canonical worlds keep the historical 60 m inset).
    actor_inset_m: float = GEOFENCE_INSET_M

    @property
    def canonical(self) -> bool:
        return self.stratum == "canonical"

    def manifest(self) -> dict:
        return {"stratum": self.stratum, "environment": self.environment.manifest(),
                "clutter": [{key: value for key, value in item.items() if key != "sdf"} for item in self.clutter],
                "events": self.events}


def canonical_dressing() -> Dressing:
    return Dressing("canonical", CANONICAL_ENVIRONMENT)


def _stratum(seed: int, override: str | None) -> str:
    if override is not None:
        if override not in STRATA:
            raise ValueError(f"stratum must be one of {STRATA}")
        return override
    return "chaotic" if substream(seed, "stratum").random() < 0.33 else "baseline"


def keep_right_loop(points: tuple[tuple[float, ...], ...], offset: float) -> tuple[tuple[float, ...], ...]:
    """Out-and-back walk as a loop: each direction keeps ``offset`` metres to its right."""
    def shifted(path: list[tuple[float, ...]]) -> list[tuple[float, ...]]:
        result = []
        for index, point in enumerate(path):
            before = path[max(0, index - 1)]
            after = path[min(len(path) - 1, index + 1)]
            normals = []
            for a, b in ((before, point), (point, after)):
                length = math.hypot(b[0] - a[0], b[1] - a[1])
                if length > 1e-9:
                    normals.append(((b[1] - a[1]) / length, -(b[0] - a[0]) / length))
            nx = sum(n[0] for n in normals) / len(normals)
            ny = sum(n[1] for n in normals) / len(normals)
            norm = math.hypot(nx, ny) or 1.0
            # Mitre so both adjoining offset segments stay ``offset`` from the centre line.
            scale = offset / max(0.5, norm) if len(normals) == 2 else offset
            result.append((point[0] + nx / norm * scale, point[1] + ny / norm * scale, *point[2:]))
        return result

    forward = list(points)
    return tuple(shifted(forward) + shifted(forward[::-1]))


def _person_constraints(kind: str, layout: Any, stream: random.Random) -> tuple[dict[str, dict], frozenset[str]]:
    """Role constraints: follow/select targets get a unique solid primary colour."""
    constraints: dict[str, dict] = {}
    if kind == "forest":
        nominal = stream.choice(("red", "green", "blue"))
        constraints[layout.follow_hiker.model] = {"nominal": nominal}
        for ranger in layout.ranger_routes:
            constraints[ranger.model] = {"hivis": True}
        family = [person.model for person in layout.family_positions]
        # Up to two children, never more than leaves two adults; zero means no children.
        children = min(stream.randint(0, 2), max(0, len(family) - 2))
        for model in family[len(family) - children:]:
            constraints.setdefault(model, {})["child"] = True
        reserved = frozenset({nominal})
    else:
        constraints[layout.red_shirt.model] = {"nominal": "red"}
        follow = stream.choice(("green", "blue"))
        constraints[layout.follow_jogger.model] = {"nominal": follow}
        reserved = frozenset({"red", follow})
    return constraints, reserved


def _clear_of(point: tuple[float, float], paths: list[tuple[list, float]], margin: float) -> bool:
    return all(distance_to_path(point, path) >= radius + margin for path, radius in paths)


def sample_dressing(kind: str, layout: ForestLayout | UrbanLayout, *, stratum: str | None = None) -> Dressing:
    seed = layout.seed
    chosen = _stratum(seed, stratum)
    chaotic = chosen == "chaotic"
    dressing = Dressing(chosen, sample_environment(substream(seed, "environment"), chaotic=chaotic),
                        asset_prefix=f"h{kind[0]}{seed}_",
                        actor_inset_m=float(layout.actor_inset_m))
    constraints, reserved = _person_constraints(kind, layout, substream(seed, "roles"))
    for person in layout.people:
        rules = constraints.get(person.model, {})
        rng = substream(seed, "appearance", person.model)
        nominal = rules.get("nominal")
        dressing.appearances[person.model] = sample_appearance(
            rng, nominal=nominal, child=rules.get("child", False), outdoor=kind == "forest",
            hivis=rules.get("hivis", False), exclude=frozenset() if nominal else reserved)
    return dressing


def dress_person(dressing: Dressing, record: dict) -> dict:
    """Asset, posture, appearance, envelope and executed route for one layout person."""
    legacy = record["asset"]
    posture = PERSON_ASSETS[legacy]["posture"]
    record["posture"] = posture
    if dressing.canonical:
        record["envelope"] = legacy_envelope(legacy)
        record["shirt_colour_attribute"] = PERSON_ASSETS[legacy]["shirt_colour_attribute"]
        record["shirt_colour_source"] = "measured_legacy_asset"
        record["possible_shirt_colours"] = [PERSON_ASSETS[legacy]["shirt_colour_attribute"]]
        record["clothing"] = PERSON_ASSETS[legacy]["clothing"]
        return record
    appearance = dressing.appearances[record["model"]]
    record["asset"] = f"{dressing.asset_prefix}{record['model']}"
    record["appearance"] = appearance
    record["envelope"] = appearance.envelope(posture)
    if posture == "sitting":
        # Seated actors render the person_sitting_a hi-vis rescue jacket, so the manifest
        # records what the shirt-colour classifier measured on a rendered drone view of that
        # asset (see people.SITTING_SHIRT_COLOUR) instead of the sampled standing garment.
        record["shirt_colour_attribute"] = SITTING_SHIRT_COLOUR
        record["shirt_colour_source"] = "measured_rendered_asset"
        record["possible_shirt_colours"] = list(SITTING_POSSIBLE_SHIRT_COLOURS)
        record["clothing"] = SITTING_CLOTHING
    else:
        record["shirt_colour_attribute"] = appearance.shirt_colour_attribute
        record["shirt_colour_source"] = "design_nominal" if appearance.shirt_colour_attribute else "none_indeterminate"
        record["possible_shirt_colours"] = list(appearance.possible_shirt_colours)
        record["clothing"] = f"{appearance.top.style}:{'+'.join(appearance.top.colours)}"
    jogger = record["role"] == "jogger"
    if record.get("waypoints"):
        record["accel"] = JOGGER_ACCEL_M_S2 if jogger else PEDESTRIAN_ACCEL_M_S2
        record["max_speed"] = round(record["speed"] * (1.35 if jogger else 1.5), 3)
        # Sidewalk/trail walkers pass oncoming walkers on the right instead of through them.
        # The lateral shift must not push a boundary route outside the geofence, so routes
        # that would leave it keep their original out-and-back path.
        if record.get("mode") == "pingpong" and record.get("group", "ambient") == "ambient":
            shifted = keep_right_loop(tuple(record["waypoints"]), KEEP_RIGHT_M)
            inset = dressing.actor_inset_m
            if all(abs(point[0]) <= inset and abs(point[1]) <= inset for point in shifted):
                record["waypoints"] = shifted
                record["mode"] = "loop"
    record["radius"] = round(max(record["envelope"]["x_max_m"] - record["envelope"]["x_min_m"],
                                 record["envelope"]["y_max_m"] - record["envelope"]["y_min_m"]) / 2, 3)
    # Generated actors join the person-asset registry so posture and the design classifier
    # labels (including every colour the shirt could be reported as) can be looked up by
    # asset name, exactly like the legacy assets.
    PERSON_ASSETS[record["asset"]] = {
        "posture": posture,
        "shirt_colour_attribute": record["shirt_colour_attribute"],
        "possible_shirt_colours": list(record["possible_shirt_colours"]),
    }
    return record


def person_files(dressing: Dressing, records: list[dict], *, textures: bool) -> dict[str, bytes | str]:
    files: dict[str, bytes | str] = {}
    for index, record in enumerate(records):
        files.update(actor_model_files(record["asset"], record["appearance"], record["posture"],
                                       textures=textures, seed=index + 1))
    return files


def _loop_length(points) -> float:
    return sum(math.dist(a[:2], b[:2]) for a, b in zip(points, (*points[1:], points[0])))


def add_fauna(dressing: Dressing, kind: str, layout: Any, people: list[dict],
              areas: dict[str, list[tuple[float, float]]], height) -> list[dict]:
    """Animal agents: dogs walking with route walkers, and wandering herds/flocks in areas."""
    rng = substream(layout.seed, "fauna")
    chaotic = dressing.stratum == "chaotic"
    animals: list[dict] = []

    def add(model: str, name: str, position: tuple[float, float], yaw: float, **extra) -> None:
        scale, coat = sample_individual(rng, name)
        record = {"model": model, "kind": "animal", "species": name, "asset": name, "role": name,
                  "group": "ambient", "position": position, "yaw": yaw, "scale": scale, "coat": coat,
                  "envelope": animal_envelope(name, scale), **extra}
        kind_spec = species(name)
        record["radius"] = round(kind_spec.radius_m * scale, 3)
        record["accel"] = kind_spec.accel_m_s2
        record["max_speed"] = kind_spec.max_speed_m_s
        animals.append(record)

    walkers = [p for p in people if p.get("mode") == "loop" and p.get("waypoints")
               and p.get("kind", "pedestrian") == "pedestrian" and p.get("role") in ("commuter", "jogger", "hiker") and _loop_length(p["waypoints"]) >= 20.0]
    rng.shuffle(walkers)
    for index, leader in enumerate(walkers[:rng.randint(1, 3 if chaotic else 2)], 1):
        add(f"ani_dog_{index:02d}", "dog", tuple(leader["waypoints"][0][:2]), leader.get("yaw", 0.0),
            behavior="follow", leader=leader["model"],
            gap=[1.2, round(rng.uniform(1.8, 2.6), 2)],
            speed=round(min(species("dog").max_speed_m_s, leader["speed"] * 1.6), 2))
    for area_name, polygon in areas.items():
        if kind == "forest" and area_name.startswith("meadow"):
            herd, bird, count_range = "deer", "crow", ((1, 2), (2, 4))
        elif area_name == "plaza":
            herd, bird, count_range = None, "pigeon", ((0, 0), (3, 7) if chaotic else (2, 5))
        elif area_name == "lawn":
            herd, bird, count_range = "cat", None, ((1, 1), (0, 0))
        else:
            continue
        centre = (sum(p[0] for p in polygon) / len(polygon), sum(p[1] for p in polygon) / len(polygon))
        placed: list[tuple[float, float, float]] = []
        for name, (low, high) in ((herd, count_range[0]), (bird, count_range[1])):
            if name is None:
                continue
            for index in range(rng.randint(low, high)):
                model = f"ani_{name}_{sum(a['species'] == name for a in animals) + 1:02d}"
                speed = round(rng.uniform(*species(name).walk_m_s), 2)
                add(model, name, centre, rng.uniform(-math.pi, math.pi), behavior="wander",
                    area=[list(point) for point in polygon], speed=speed,
                    pause=[1.0, 6.0] if name in ("deer", "cat") else [0.5, 3.0], avoid=True)
                record = animals[-1]
                record["position"] = _free_spawn(centre, polygon, record["envelope"], placed)
    return animals


def _free_spawn(centre: tuple[float, float], polygon: list[tuple[float, float]], envelope: dict,
                placed: list[tuple[float, float, float]]) -> tuple[float, float]:
    """A spawn point in the area whose envelope circle clears every animal already placed there.

    Kinematic envelopes that start interpenetrating generate contact constraints every physics
    step, which stalls dartsim, so herds and flocks are laid out on rings around the area centre.
    """
    radius = max(math.hypot(x, y) for x in (envelope["x_min_m"], envelope["x_max_m"])
                 for y in (envelope["y_min_m"], envelope["y_max_m"]))
    inradius = min(point_segment_distance(centre, a, b) for a, b in zip(polygon, polygon[1:] + polygon[:1]))
    step = 2 * radius + 0.4
    candidates = [centre]
    ring = 1
    while ring * step + radius <= inradius:
        count = max(6, int(2 * math.pi * ring))
        candidates += [(centre[0] + ring * step * math.cos(2 * math.pi * k / count),
                        centre[1] + ring * step * math.sin(2 * math.pi * k / count)) for k in range(count)]
        ring += 1
    for x, y in candidates:
        if all(math.hypot(x - px, y - py) >= radius + pr + 0.3 for px, py, pr in placed):
            placed.append((x, y, radius))
            return (round(x, 4), round(y, 4))
    raise ValueError(f"no non-overlapping spawn point for an animal in area around {centre}")


def animal_models(animals: list[dict], height) -> list[str]:
    return [animal_model(a["model"], a["species"], a["scale"], a["coat"], a["position"][0], a["position"][1],
                         height(*a["position"]), a["yaw"]) for a in animals]


def fauna_areas(kind: str, layout: Any) -> dict[str, list[tuple[float, float]]]:
    """Obstacle-free wander polygons (validated against real geometry before acceptance)."""
    if kind == "forest":
        # Generated 1.2 km forests have several clearings; graze the first two (open,
        # tree-free meadow, validated by the sampler's clearing exclusion).
        areas: dict[str, list[tuple[float, float]]] = {}
        for index, (cx, cy, radius) in enumerate(layout.clearings[:2]):
            r = radius * 0.6
            name = "meadow" if index == 0 else f"meadow_{index + 1}"
            areas[name] = [(cx + r * math.cos(a), cy + r * math.sin(a))
                           for a in (i * math.pi / 4 for i in range(8))]
        return areas
    half = layout.plaza_half_size - 1.0
    # The landing pad sits at the plaza centre; keep wandering animals north of the pad's
    # keep-out instead of letting them graze on the pad collision.
    pad = HOME_PAD_KEEPOUT_M + 0.5
    areas = {"plaza": [(-half, pad), (half, pad), (half, half), (-half, half)]}
    rng = substream(layout.seed, "fauna_areas")
    pcx, pcy = layout.park_center
    for _ in range(200):
        x = pcx + rng.uniform(-layout.park_half_size + 2.5, layout.park_half_size - 2.5)
        y = pcy + rng.uniform(-layout.park_half_size + 2.5, layout.park_half_size - 2.5)
        obstacles = [(tree.x, tree.y, 0.8) for tree in layout.park_trees] + [(b.x, b.y, 1.3) for b in layout.benches]
        obstacles += [(p.position[0], p.position[1], 0.9) for p in layout.park_sitting]
        if all(math.hypot(x - ox, y - oy) >= radius + 2.2 for ox, oy, radius in obstacles):
            areas["lawn"] = [(x - 1.5, y - 1.5), (x + 1.5, y - 1.5), (x + 1.5, y + 1.5), (x - 1.5, y + 1.5)]
            break
    return areas


def _forest_clutter_clear(layout: Any, point: tuple[float, float], size: float) -> bool:
    """True when forest clutter at ``point`` clears trails, creeks, clearings, camps and the
    mission people (the missing hiker's canopy and the follow occluder included)."""
    if distance_to_paths(point, layout.trails) < size + 3.5:
        return False
    if distance_to_paths(point, layout.creeks) < size + 2.5:
        return False
    if any(math.hypot(point[0] - cx, point[1] - cy) < radius + size + 2.0
           for cx, cy, radius in layout.clearings):
        return False
    if any(math.hypot(point[0] - cx, point[1] - cy) < 15.0 + size for cx, cy in layout.camps):
        return False
    if math.hypot(point[0] - layout.follow_occluder_xy[0], point[1] - layout.follow_occluder_xy[1]) \
            < size + 4.0:
        return False
    for person in layout.people:
        if math.hypot(point[0] - person.position[0], point[1] - person.position[1]) < size + 8.0:
            return False
    return True


def add_clutter(dressing: Dressing, kind: str, layout: Any, agents: list[dict], height) -> None:
    """Place static clutter clear of every actor route, area and the home pad.

    Placement only: the items land in ``dressing.clutter`` and the world builders emit them
    as scenery pieces (``clutter_scenery``) in their scenery tiles.
    """
    rng = substream(layout.seed, "clutter")
    chaotic = dressing.stratum == "chaotic"
    paths: list[tuple[list, float]] = []
    for agent in agents:
        radius = agent.get("radius", 0.4)
        if agent.get("footprint"):
            radius = agent["footprint"]["width_m"] / 2 + 0.6
        points = [tuple(p[:2]) for p in agent.get("waypoints", ())] or [tuple(agent["position"])]
        if agent.get("mode") == "loop" and len(points) > 1:
            points = points + [points[0]]
        if len(points) == 1:
            points = points * 2
        paths.append((points, radius + (6.0 if agent.get("kind") == "vehicle" else 0.0)))
        if agent.get("area"):
            polygon = [tuple(p) for p in agent["area"]]
            paths.append((polygon + [polygon[0]], 1.0))
    placed: list[dict] = []
    count = rng.randint(10, 16) if chaotic else rng.randint(5, 10)
    kinds = (("stump", "boulder", "deadfall", "cairn") if kind == "forest"
             else ("planter", "bollard", "cone", "bike_rack", "crate_stack"))
    half = float(layout.map.half_m) - 45.0
    city = layout.city if kind == "urban" else None
    for _ in range(count * 30):
        if len(placed) >= count:
            break
        item = rng.choice(kinds)
        x, y = rng.uniform(-half, half), rng.uniform(-half, half)
        size = {"stump": 0.45, "boulder": rng.uniform(0.5, 1.1), "deadfall": 2.4, "cairn": 0.5,
                "planter": 0.7, "bollard": 0.2, "cone": 0.25, "bike_rack": 1.0, "crate_stack": 0.8}[item]
        if math.hypot(x, y) < 12 or not _clear_of((x, y), paths, size + 0.9):
            continue
        if kind == "forest" and not _forest_clutter_clear(layout, (x, y), size):
            continue
        if kind == "urban" and any(abs(x - b.x) < b.sx / 2 + size + 0.5 and abs(y - b.y) < b.sy / 2 + size + 0.5
                                   for b in layout.buildings):
            continue
        if kind == "urban":
            # Wider arterials need the road's own half width; keep clutter off the pavement
            # and out of every plaza and park (the mission crowds live there).
            clearance = size + 0.6 + URBAN_SIDEWALK_WIDTH_M
            if any(abs(x - road) < clearance + road_half_width(road, city.arterial_roads_x)
                   for road in layout.roads_x):
                continue
            if any(abs(y - road) < clearance + road_half_width(road, city.arterial_roads_y)
                   for road in layout.roads_y):
                continue
            if any(math.hypot(x - plaza.x, y - plaza.y) < plaza.half_size + size + 1.0
                   for plaza in city.plazas):
                continue
            if any(math.hypot(x - park.x, y - park.y) < park.half_size + size + 1.0
                   for park in city.parks):
                continue
        if any(math.hypot(x - other["x"], y - other["y"]) < size + other["size"] + 0.5 for other in placed):
            continue
        yaw = rng.uniform(-math.pi, math.pi)
        placed.append({"name": f"clutter_{item}_{len(placed) + 1:02d}", "kind": item, "x": round(x, 3),
                       "y": round(y, 3), "z": round(height(x, y), 3), "yaw": round(yaw, 3),
                       "size": round(size, 3)})
    dressing.clutter = placed


def _mesh_piece(name: str, texture: str, mesh: LoftMesh,
                proxies: list[ProxyShape]) -> SceneryMesh:
    """A world-space ``SceneryMesh`` from a loft mesh (texture doubles as the material name)."""
    positions, normals, uvs, faces = mesh.triangles()
    return SceneryMesh(name, texture, tuple(positions), tuple(normals), tuple(uvs), tuple(faces),
                       proxies=tuple(proxies), texture=texture)


def deadfall_mesh(name: str, x: float, y: float, z: float, yaw: float, *, length: float,
                  radius: float = 0.25) -> SceneryMesh:
    """A bark-textured fallen log along the item yaw, with one box proxy."""
    mesh = LoftMesh()
    dx, dy = rotate_xy(length / 2, 0.0, yaw)
    mesh.add_tube([(x - dx, y - dy, z + radius), (x + dx, y + dy, z + radius)],
                  [(radius, radius), (radius, radius)], segments=10, tile_m=0.45)
    proxy = ProxyShape("box", (x, y, z + radius, 0.0, 0.0, yaw), (length, 2 * radius, 2 * radius))
    return _mesh_piece(name, "bark", mesh, [proxy])


def shrub_mesh(name: str, x: float, y: float, z: float, *, height: float, crown_radius: float,
               base_radius: float, seed: int = 0) -> SceneryMesh:
    """A dense bush: narrow base, wide irregular top, foliage-textured loft plus crown blobs.

    Two cylinder proxies (narrow lower, wide upper) keep the collision budget cheap; a
    follow-hiker occluder uses 4-5 m of height with a 0.5-0.9 m base and a 2-3 m crown.
    """
    rng = random.Random(seed)
    mesh = LoftMesh()
    sections = 7
    points, radii = [], []
    for index in range(sections):
        t = index / (sections - 1)
        blend = t * t * (3.0 - 2.0 * t)
        radius = base_radius + (crown_radius - base_radius) * blend
        radius *= 0.88 + 0.24 * rng.random()
        points.append((x + (rng.random() - 0.5) * crown_radius * 0.12,
                       y + (rng.random() - 0.5) * crown_radius * 0.12,
                       z + 0.05 + (height - 0.05) * t))
        radii.append((radius, radius * (0.85 + 0.3 * rng.random())))
    mesh.add_tube(points, radii, segments=10, tile_m=0.6)
    for _ in range(3):
        blob = crown_radius * (0.30 + 0.18 * rng.random())
        center_z = z + height * (0.50 + 0.30 * rng.random())
        center_z = min(center_z, z + height - blob * 0.8)
        mesh.add_ellipsoid((x + (rng.random() - 0.5) * crown_radius * 1.1,
                            y + (rng.random() - 0.5) * crown_radius * 1.1, center_z),
                           (blob, blob, blob * 0.8), segments=10, rings=4, tile_m=0.6)
    # Wobbled rings can tilt; clamp every vertex into the declared envelope.
    mesh.positions = [(px, py, min(max(pz, z), z + height)) for px, py, pz in mesh.positions]
    proxies = [ProxyShape("cylinder", (x, y, z + height * 0.22, 0.0, 0.0, 0.0),
                          (base_radius * 1.3, height * 0.45)),
               ProxyShape("cylinder", (x, y, z + height * 0.72, 0.0, 0.0, 0.0),
                          (crown_radius * 1.1, height * 0.6))]
    return _mesh_piece(name, "foliage", mesh, proxies)


def _stump_mesh(name: str, x: float, y: float, z: float, yaw: float, size: float) -> SceneryMesh:
    mesh = LoftMesh()
    mesh.add_tube([(x, y, z), (x, y, z + 0.42), (x, y, z + 0.6)],
                  [(size, size), (size * 1.05, size * 1.05), (size * 0.92, size * 0.92)],
                  segments=12, tile_m=0.4)
    return _mesh_piece(name, "bark", mesh,
                       [ProxyShape("cylinder", (x, y, z + 0.3, 0.0, 0.0, 0.0), (size, 0.6))])


def _bollard_mesh(name: str, x: float, y: float, z: float, yaw: float, size: float) -> SceneryMesh:
    mesh = LoftMesh()
    radius = 0.12
    mesh.add_tube([(x, y, z), (x, y, z + 0.9), (x, y, z + 0.97)],
                  [(radius, radius), (radius * 1.05, radius * 1.05), (radius * 0.55, radius * 0.55)],
                  segments=10, tile_m=0.5)
    mesh.add_ellipsoid((x, y, z + 0.97), (radius * 0.55, radius * 0.55, radius * 0.55),
                       segments=10, rings=3, tile_m=0.5)
    return _mesh_piece(name, "metal_paint", mesh,
                       [ProxyShape("cylinder", (x, y, z + 0.5, 0.0, 0.0, 0.0), (radius * 1.15, 1.0))])


def _cone_mesh(name: str, x: float, y: float, z: float, yaw: float, size: float) -> SceneryMesh:
    mesh = LoftMesh()
    mesh.add_box((x, y, z + 0.015), (0.36, 0.36, 0.03), yaw=yaw, tile_m=0.36)
    # The texture's reflective band sits at v 0.44-0.62; v spans the cone height once.
    mesh.add_tube([(x, y, z + 0.03), (x, y, z + 0.40), (x, y, z + 0.72)],
                  [(0.16, 0.16), (0.085, 0.085), (0.02, 0.02)], segments=12, tile_m=0.72)
    return _mesh_piece(name, "traffic_cone", mesh,
                       [ProxyShape("cylinder", (x, y, z + 0.38, 0.0, 0.0, 0.0), (0.19, 0.76))])


def _bike_rack_mesh(name: str, x: float, y: float, z: float, yaw: float, size: float) -> SceneryMesh:
    mesh = LoftMesh()
    mesh.add_box((x, y, z + 0.75), (2.0, 0.06, 0.06), yaw=yaw, tile_m=1.0)
    proxies = [ProxyShape("box", (x, y, z + 0.75, 0.0, 0.0, yaw), (2.0, 0.12, 0.12))]
    for side in (-0.9, 0.9):
        px, py = rotate_xy(side, 0.0, yaw)
        mesh.add_box((x + px, y + py, z + 0.37), (0.06, 0.06, 0.74), yaw=yaw, tile_m=0.5)
        proxies.append(ProxyShape("box", (x + px, y + py, z + 0.37, 0.0, 0.0, yaw),
                                  (0.10, 0.10, 0.74)))
    return _mesh_piece(name, "metal_paint", mesh, proxies)


def _planter_meshes(name: str, x: float, y: float, z: float, yaw: float,
                    size: float) -> list[SceneryMesh]:
    box = LoftMesh()
    box.add_box((x, y, z + 0.35), (1.2, 1.2, 0.7), yaw=yaw, tile_m=0.5)
    box.add_box((x, y, z + 0.71), (1.24, 1.24, 0.06), yaw=yaw, tile_m=0.5)
    shrub = shrub_mesh(f"{name}_shrub", x, y, z + 0.74, height=0.85, crown_radius=0.55,
                       base_radius=0.3, seed=sum(map(ord, name)))
    return [_mesh_piece(name, "rock", box,
                        [ProxyShape("box", (x, y, z + 0.38, 0.0, 0.0, yaw), (1.3, 1.3, 0.76))]),
            shrub]


def _crate_stack_mesh(name: str, x: float, y: float, z: float, yaw: float,
                      size: float) -> SceneryMesh:
    mesh = LoftMesh()
    mesh.add_box((x, y, z + 0.3), (0.9, 0.7, 0.6), yaw=yaw, tile_m=0.45)
    dx, dy = rotate_xy(0.1, 0.05, yaw)
    mesh.add_box((x + dx, y + dy, z + 0.85), (0.7, 0.6, 0.5), yaw=yaw + 0.35, tile_m=0.45)
    proxies = [ProxyShape("box", (x, y, z + 0.3, 0.0, 0.0, yaw), (0.9, 0.7, 0.6)),
               ProxyShape("box", (x + dx, y + dy, z + 0.85, 0.0, 0.0, yaw + 0.35), (0.7, 0.6, 0.5))]
    return _mesh_piece(name, "wood_planks", mesh, proxies)


def clutter_scenery(kind: str, items: list[dict]) -> tuple[list[SceneryInstance], list[SceneryMesh]]:
    """Scenery pieces for sampled clutter items (placement stays in ``add_clutter``).

    ``kind`` is ``"forest"`` or ``"urban"``; ``items`` are the ``{"name", "kind", "x", "y",
    "yaw", "size", "z"}`` dicts from ``add_clutter``. Boulders and cairns become ``rock_a``
    instances; the rest are textured loft meshes from ``scene_textures``.
    """
    if kind not in ("forest", "urban"):
        raise ValueError(f"unknown clutter kind {kind!r}")
    instances: list[SceneryInstance] = []
    meshes: list[SceneryMesh] = []
    for item in items:
        name, x, y, yaw, size = item["name"], item["x"], item["y"], item["yaw"], item["size"]
        z = float(item.get("z", 0.0))
        ckind = item["kind"]
        if ckind == "boulder":
            instances.append(SceneryInstance("rock_a", x, y, z, yaw, scale=round(size / 0.7889, 4)))
        elif ckind == "cairn":
            scale = size / 0.5
            for level, (rock_scale, lift) in enumerate(((0.65, 0.0), (0.48, 0.40), (0.34, 0.70))):
                instances.append(SceneryInstance("rock_a", x, y, z + lift * scale,
                                                 yaw + level * 0.7, scale=round(rock_scale * scale, 4)))
        elif ckind == "stump":
            meshes.append(_stump_mesh(name, x, y, z, yaw, size))
        elif ckind == "deadfall":
            meshes.append(deadfall_mesh(name, x, y, z, yaw, length=size * 2))
        elif ckind == "bollard":
            meshes.append(_bollard_mesh(name, x, y, z, yaw, size))
        elif ckind == "cone":
            meshes.append(_cone_mesh(name, x, y, z, yaw, size))
        elif ckind == "bike_rack":
            meshes.append(_bike_rack_mesh(name, x, y, z, yaw, size))
        elif ckind == "planter":
            meshes.extend(_planter_meshes(name, x, y, z, yaw, size))
        elif ckind == "crate_stack":
            meshes.append(_crate_stack_mesh(name, x, y, z, yaw, size))
        else:
            raise ValueError(f"unknown clutter {ckind!r}")
    return instances, meshes


def _tent_mesh(name: str, x: float, y: float, z: float, yaw: float, *, length: float = 2.8,
               width: float = 1.8, height: float = 1.15) -> SceneryMesh:
    """A ridge tent (two roof panels, two triangular ends) with a box proxy."""
    mesh = LoftMesh()
    half_l, half_w = length / 2, width / 2

    def point(lx: float, ly: float, lz: float) -> tuple[float, float, float]:
        dx, dy = rotate_xy(lx, ly, yaw)
        return (x + dx, y + dy, z + lz)

    slope = math.hypot(half_w, height)
    for side in (1, -1):
        outward = rotate_xy(0.0, float(side), yaw)
        mesh.add_quad([point(-half_l, side * half_w, 0.0), point(half_l, side * half_w, 0.0),
                       point(half_l, 0.0, height), point(-half_l, 0.0, height)],
                      outward=(outward[0], outward[1], half_w), u_repeat=length, v_repeat=slope)
    for end, outward in ((-half_l, (-1.0, 0.0)), (half_l, (1.0, 0.0))):
        direction = rotate_xy(outward[0], outward[1], yaw)
        mesh.add_triangle([point(end, -half_w, 0.0), point(end, half_w, 0.0),
                           point(end, 0.0, height)], outward=(direction[0], direction[1], 0.2),
                          u_repeat=width, v_repeat=height)
    proxy = ProxyShape("box", (x, y, z + height / 2, 0.0, 0.0, yaw), (length, width, height))
    return _mesh_piece(name, "tent_fabric", mesh, [proxy])


def _picnic_table_mesh(name: str, x: float, y: float, z: float, yaw: float) -> SceneryMesh:
    mesh = LoftMesh()
    proxies: list[ProxyShape] = []

    def box(lx: float, ly: float, lz: float, size: tuple[float, float, float]) -> None:
        dx, dy = rotate_xy(lx, ly, yaw)
        mesh.add_box((x + dx, y + dy, z + lz), size, yaw=yaw, tile_m=0.5)
        proxies.append(ProxyShape("box", (x + dx, y + dy, z + lz, 0.0, 0.0, yaw), size))

    box(0.0, 0.0, 0.75, (1.8, 0.8, 0.06))
    for side in (-0.62, 0.62):
        box(0.0, side, 0.45, (1.8, 0.28, 0.05))
    for lx in (-0.7, 0.7):
        box(lx, 0.0, 0.36, (0.08, 0.7, 0.72))
    return _mesh_piece(name, "wood_planks", mesh, proxies)


def camp_scenery(camp_xy: tuple[float, float], ground_z: float, *,
                 seed: int) -> tuple[list[SceneryInstance], list[SceneryMesh]]:
    """Camp dressing in world coordinates (positions match the historic primitive campsite).

    Two ridge tents at ``(cx, cy +/- 1.1)``, a stone fire ring with a pit at ``(cx - 3.7,
    cy + 0.1)`` and a picnic table at ``(cx + 4.1, cy + 1.0)``.
    """
    cx, cy = camp_xy
    rng = random.Random(seed)
    instances: list[SceneryInstance] = []
    meshes: list[SceneryMesh] = []
    for index, (oy, yaw) in enumerate(((-1.1, 0.0), (1.1, math.pi))):
        meshes.append(_tent_mesh(f"camp_tent_{index + 1}", cx, cy + oy, ground_z, yaw,
                                 length=round(rng.uniform(2.6, 3.0), 2)))
    for index in range(9):
        angle = 2.0 * math.pi * index / 9
        instances.append(SceneryInstance("rock_a", cx - 3.7 + 0.78 * math.cos(angle),
                                         cy + 0.1 + 0.78 * math.sin(angle), ground_z, angle,
                                         scale=0.30))
    pit = LoftMesh()
    pit.add_tube([(cx - 3.7, cy + 0.1, ground_z + 0.02), (cx - 3.7, cy + 0.1, ground_z + 0.15)],
                 [(0.45, 0.45), (0.42, 0.42)], segments=12, tile_m=0.5)
    meshes.append(_mesh_piece("camp_fire_pit", "bark", pit,
                              [ProxyShape("cylinder", (cx - 3.7, cy + 0.1, ground_z + 0.08,
                                                       0.0, 0.0, 0.0), (0.45, 0.16))]))
    meshes.append(_picnic_table_mesh("camp_table", cx + 4.1, cy + 1.0, ground_z, 0.0))
    return instances, meshes


# Sim-time disturbance schedule. Every event keeps the agent within its declared speed cap and
# acceleration limits; the scenario driver applies them at fixed 0.05 s steps.
def sample_events(dressing: Dressing, agents: list[dict], seed: int, horizon_s: float = 600.0) -> list[dict]:
    rng = substream(seed, "events")
    chaotic = dressing.stratum == "chaotic"
    events: list[dict] = []
    movers = [a for a in agents if a.get("behavior") in ("route", "wander", "follow")
              and a.get("group", "ambient") == "ambient"]
    count = rng.randint(12, 24) if chaotic else rng.randint(3, 8)
    for _ in range(count):
        if not movers:
            break
        agent = rng.choice(movers)
        t = round(rng.uniform(5.0, horizon_s), 2)
        kind = agent.get("kind", "pedestrian")
        options = ["hesitate", "speed_change"]
        if kind == "vehicle" and chaotic:
            options.append("breakdown")
        if agent.get("behavior") == "wander":
            options.append("startle")
        if agent.get("behavior") == "route" and kind == "pedestrian" and agent.get("mode") == "loop":
            options.append("reverse")
        event_type = rng.choice(options)
        event = {"t_s": t, "model": agent["model"], "type": event_type}
        if event_type == "hesitate":
            event["duration_s"] = round(rng.uniform(1.5, 8.0), 2)
        elif event_type == "speed_change":
            cap = agent.get("max_speed", agent.get("speed", 1.0)) / max(1e-6, agent.get("speed", 1.0))
            event["factor"] = round(rng.uniform(0.5, max(0.5, min(1.4, cap))), 3)
            event["duration_s"] = round(rng.uniform(5.0, 30.0), 2)
        elif event_type == "breakdown":
            event["duration_s"] = round(rng.uniform(40.0, 120.0), 2)
        elif event_type == "startle":
            event["duration_s"] = round(rng.uniform(2.0, 5.0), 2)
        elif event_type == "reverse":
            event["duration_s"] = round(rng.uniform(2.0, 6.0), 2)
        events.append(event)
    events.sort(key=lambda item: (item["t_s"], item["model"]))
    dressing.events = events
    return events
