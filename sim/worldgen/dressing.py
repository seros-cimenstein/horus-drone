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
    GEOFENCE_INSET_M, HOME_PAD_KEEPOUT_M, ForestLayout, UrbanLayout, distance_to_path,
)
from sim.worldgen.people import PERSON_ASSETS, actor_model_files, legacy_envelope
from sim.worldgen.seeding import substream
from sim.worldgen.sdf import box_geometry, collision, cylinder_geometry, sphere_geometry, static_model, visual

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
                        asset_prefix=f"h{kind[0]}{seed}_")
    constraints, reserved = _person_constraints(kind, layout, substream(seed, "roles"))
    for person in layout.people:
        rules = constraints.get(person.model, {})
        rng = substream(seed, "appearance", person.model)
        nominal = rules.get("nominal")
        dressing.appearances[person.model] = sample_appearance(
            rng, nominal=nominal, child=rules.get("child", False), outdoor=kind == "forest",
            hivis=rules.get("hivis", False), exclude=frozenset() if nominal else reserved)
    return dressing


def person_asset(dressing: Dressing, model: str, legacy_asset: str) -> str:
    return legacy_asset if dressing.canonical else f"{dressing.asset_prefix}{model}"


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
    record["asset"] = person_asset(dressing, record["model"], legacy)
    record["appearance"] = appearance
    record["envelope"] = appearance.envelope(posture)
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
            if all(abs(point[0]) <= GEOFENCE_INSET_M and abs(point[1]) <= GEOFENCE_INSET_M
                   for point in shifted):
                record["waypoints"] = shifted
                record["mode"] = "loop"
    record["radius"] = round(max(record["envelope"]["x_max_m"] - record["envelope"]["x_min_m"],
                                 record["envelope"]["y_max_m"] - record["envelope"]["y_min_m"]) / 2, 3)
    # Generated actors join the person-asset registry so posture and the design classifier
    # labels (including every colour the shirt could be reported as) can be looked up by
    # asset name, exactly like the legacy assets.
    PERSON_ASSETS[record["asset"]] = {
        "posture": posture,
        "shirt_colour_attribute": appearance.shirt_colour_attribute,
        "possible_shirt_colours": list(appearance.possible_shirt_colours),
    }
    return record


def person_files(dressing: Dressing, records: list[dict], *, textures: bool) -> dict[str, bytes | str]:
    files: dict[str, bytes | str] = {}
    if dressing.canonical:
        return files
    for index, record in enumerate(records):
        files.update(actor_model_files(record["asset"], record["appearance"], record["posture"],
                                       textures=textures, seed=index + 1))
    return files


def _loop_length(points) -> float:
    return sum(math.dist(a[:2], b[:2]) for a, b in zip(points, (*points[1:], points[0])))


def add_fauna(dressing: Dressing, kind: str, layout: Any, people: list[dict],
              areas: dict[str, list[tuple[float, float]]], height) -> list[dict]:
    """Animal agents: dogs walking with route walkers, and wandering herds/flocks in areas."""
    if dressing.canonical:
        return []
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
        if kind == "forest" and area_name == "meadow":
            herd, bird, count_range = "deer", "crow", ((1, 2), (2, 4))
        elif area_name == "plaza":
            herd, bird, count_range = None, "pigeon", ((0, 0), (3, 7) if chaotic else (2, 5))
        elif area_name == "lawn":
            herd, bird, count_range = "cat", None, ((1, 1), (0, 0))
        else:
            continue
        for name, (low, high) in ((herd, count_range[0]), (bird, count_range[1])):
            if name is None:
                continue
            for index in range(rng.randint(low, high)):
                model = f"ani_{name}_{sum(a['species'] == name for a in animals) + 1:02d}"
                centre = (sum(p[0] for p in polygon) / len(polygon), sum(p[1] for p in polygon) / len(polygon))
                speed = round(rng.uniform(*species(name).walk_m_s), 2)
                add(model, name, centre, rng.uniform(-math.pi, math.pi), behavior="wander",
                    area=[list(point) for point in polygon], speed=speed,
                    pause=[1.0, 6.0] if name in ("deer", "cat") else [0.5, 3.0], avoid=True)
    return animals


def animal_models(animals: list[dict], height) -> list[str]:
    return [animal_model(a["model"], a["species"], a["scale"], a["coat"], a["position"][0], a["position"][1],
                         height(*a["position"]), a["yaw"]) for a in animals]


def fauna_areas(kind: str, layout: Any) -> dict[str, list[tuple[float, float]]]:
    """Obstacle-free wander polygons (validated against real geometry before acceptance)."""
    if kind == "forest":
        cx, cy = layout.clearing
        r = layout.clearing_radius * 0.6
        return {"meadow": [(cx + r * math.cos(a), cy + r * math.sin(a))
                           for a in (i * math.pi / 4 for i in range(8))]}
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


def add_clutter(dressing: Dressing, kind: str, layout: Any, agents: list[dict], height) -> list[str]:
    """Static clutter with collisions, kept clear of every actor route, area and the home pad."""
    if dressing.canonical:
        return []
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
    for _ in range(count * 30):
        if len(placed) >= count:
            break
        item = rng.choice(kinds)
        x, y = rng.uniform(-56, 56), rng.uniform(-56, 56)
        size = {"stump": 0.45, "boulder": rng.uniform(0.5, 1.1), "deadfall": 2.4, "cairn": 0.5,
                "planter": 0.7, "bollard": 0.2, "cone": 0.25, "bike_rack": 1.0, "crate_stack": 0.8}[item]
        if math.hypot(x, y) < 12 or not _clear_of((x, y), paths, size + 0.9):
            continue
        if kind == "urban" and any(abs(x - b.x) < b.sx / 2 + size + 0.5 and abs(y - b.y) < b.sy / 2 + size + 0.5
                                   for b in layout.buildings):
            continue
        if kind == "urban" and any(abs(x - road) < 4.9 + size for road in layout.roads_x) \
                or kind == "urban" and any(abs(y - road) < 4.9 + size for road in layout.roads_y):
            continue
        if any(math.hypot(x - other["x"], y - other["y"]) < size + other["size"] + 0.5 for other in placed):
            continue
        yaw = rng.uniform(-math.pi, math.pi)
        placed.append({"name": f"clutter_{item}_{len(placed) + 1:02d}", "kind": item, "x": round(x, 3),
                       "y": round(y, 3), "yaw": round(yaw, 3), "size": round(size, 3)})
    dressing.clutter = placed
    return [static_model(item["name"], _clutter_sdf(item, height(item["x"], item["y"]))) for item in placed]


def _clutter_sdf(item: dict, z: float) -> str:
    x, y, yaw, size, kind = item["x"], item["y"], item["yaw"], item["size"], item["kind"]
    parts: list[str] = []

    def solid(name: str, dx: float, dy: float, dz: float, geometry: str, rgb) -> None:
        c, s = math.cos(yaw), math.sin(yaw)
        px, py = x + dx * c - dy * s, y + dx * s + dy * c
        parts.append(visual(name, px, py, z + dz, yaw, geometry, rgb))
        parts.append(collision(f"{name}_collision", px, py, z + dz, yaw, geometry))

    if kind == "stump":
        solid("stump", 0, 0, 0.3, cylinder_geometry(size, 0.6), (0.36, 0.24, 0.14))
    elif kind == "boulder":
        solid("boulder", 0, 0, size * 0.55, sphere_geometry(size), (0.40, 0.39, 0.36))
    elif kind == "deadfall":
        parts_pose = f"{x:.3f} {y:.3f} {z + 0.3:.3f} {math.pi / 2:.4f} 0 {yaw:.3f}"
        geometry = cylinder_geometry(0.25, size * 2)
        parts.append(f'<visual name="deadfall"><pose>{parts_pose}</pose><geometry>{geometry}</geometry>'
                     "<material><ambient>0.32 0.22 0.14 1</ambient><diffuse>0.32 0.22 0.14 1</diffuse></material></visual>")
        parts.append(f'<collision name="deadfall_collision"><pose>{parts_pose}</pose><geometry>{geometry}</geometry></collision>')
    elif kind == "cairn":
        for level, radius in enumerate((0.45, 0.33, 0.22)):
            solid(f"cairn_{level}", 0, 0, 0.2 + level * 0.3, sphere_geometry(radius), (0.52, 0.50, 0.46))
    elif kind == "planter":
        solid("planter_box", 0, 0, 0.35, box_geometry(1.2, 1.2, 0.7), (0.55, 0.52, 0.47))
        solid("planter_shrub", 0, 0, 1.0, sphere_geometry(0.55), (0.25, 0.42, 0.20))
    elif kind == "bollard":
        solid("bollard", 0, 0, 0.5, cylinder_geometry(0.12, 1.0), (0.2, 0.2, 0.22))
    elif kind == "cone":
        solid("cone", 0, 0, 0.36, "<cone><radius>0.18</radius><length>0.72</length></cone>", (0.95, 0.42, 0.08))
    elif kind == "bike_rack":
        solid("bike_rack_rail", 0, 0, 0.75, box_geometry(2.0, 0.06, 0.06), (0.55, 0.57, 0.6))
        for side in (-0.9, 0.9):
            solid(f"bike_rack_post_{side:+.0f}", side, 0, 0.37, box_geometry(0.06, 0.06, 0.74), (0.55, 0.57, 0.6))
    elif kind == "crate_stack":
        solid("crate_low", 0, 0, 0.3, box_geometry(0.9, 0.7, 0.6), (0.62, 0.48, 0.30))
        solid("crate_high", 0.1, 0.05, 0.85, box_geometry(0.7, 0.6, 0.5), (0.58, 0.44, 0.28))
    else:
        raise ValueError(f"unknown clutter {kind!r}")
    return "".join(parts)


# Sim-time disturbance schedule. Every event keeps the agent within its declared speed cap and
# acceleration limits; the scenario driver applies them at fixed 0.05 s steps.
def sample_events(dressing: Dressing, agents: list[dict], seed: int, horizon_s: float = 600.0) -> list[dict]:
    if dressing.canonical:
        return []
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
