"""Procedural 1.2 km cities: layout sampling plus instanced/textured scenery emission.

``sample_urban_layout(seed, validate=...)`` draws an irregular road grid across the whole
:data:`~sim.worldgen.surface.PROCEDURAL_MAP` (blocks ~60-120 m with wider arterials), places
400-900 textured buildings, several plazas and parks, street furniture and the mission content
(crowd plaza with the red shirt, collapsed sidewalk person, bus-stop group, park sitters and
the follow-jogger route with its bus-shelter occluder), then hands the layout to the shared
validator exactly like the canonical sampler does.

``build_urban_procedural(layout, world_dir, ...)`` emits the world: the ground model, per-tile
scenery from :func:`sim.worldgen.scenery.emit_scenery` (instanced asset visuals, merged
textured meshes, one trimesh collision per tile and the sidecar proxies) and the kinematic
agents. Everything here is standard-library only and deterministic in the seed.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from sim.worldgen.assets import ASSETS
from sim.worldgen.dressing import (
    Dressing, add_clutter, add_fauna, animal_models, dress_person, fauna_areas, person_files,
    sample_events,
)
from sim.worldgen.layout import (
    URBAN_ARTERIAL_HALF_WIDTH_M, URBAN_SIDEWALK_WIDTH_M, URBAN_STREET_HALF_WIDTH_M, Bench,
    BuildingSpec, BusStopSpec, CitySpec, ParkSpec, ParkTree, ParkedCar, PersonPlacement,
    PlazaSpec, TreeSpec, UrbanLayout, VehiclePlacement, block_loop, distance_to_path,
)
from sim.worldgen.materials import facade_uv, scene_textures
from sim.worldgen.proxy import ProxyShape
from sim.worldgen.scenery import SceneryInstance, SceneryMesh, emit_scenery
from sim.worldgen.scenario import person_record
from sim.worldgen.sdf import (
    PAD_COLLIDE_BITMASK, PROCEDURAL_DRONE_MODEL, SURFACE_COLOURS, TERRAIN_COLLIDE_BITMASK, home_pad,
    include, pedestrian_include, static_model, visual, world_header,
)
from sim.worldgen.seeding import substream
from sim.worldgen.surface import PROCEDURAL_MAP
from sim.worldgen.vehicles import PARKED_TYPES, SAMPLED_TYPES, vehicle_model, vehicle_type

Point = tuple[float, float]

# --- city proportions (metres) ---------------------------------------------------------------
MAP_HALF_M = PROCEDURAL_MAP.half_m                 # 600
ACTOR_INSET_M = 580.0                              # 5 m inside the profile's +-585 polygon
STREET_HALF_M = URBAN_STREET_HALF_WIDTH_M          # standard carriageway half width
ARTERIAL_HALF_M = URBAN_ARTERIAL_HALF_WIDTH_M      # wider arterial carriageway half width
SIDEWALK_WIDTH_M = URBAN_SIDEWALK_WIDTH_M
LAMP_OFFSET_M = 7.0
ARTERIAL_LAMP_OFFSET_M = 9.25
STREET_TREE_OFFSET_M = 7.7
ARTERIAL_TREE_OFFSET_M = 9.9
BLOCK_SETBACK_M = 9.6                              # road centre -> nearest building face
ARTERIAL_SETBACK_M = 11.8
MAP_EDGE_MARGIN_M = 12.0
PARKING_LANE_M = 3.75
PARKING_INSET_M = 0.15
CROSSWALK_HALF_LENGTH_M = 1.8                      # half width of one zebra patch along the road
CROSSWALK_ARM_M = 2.4                              # junction extent -> zebra centre
SHELTER_LENGTH_M = 8.0                             # follow occluder (shelter) length along the lane
HOME_PLAZA_MAX_HALF_M = 16.0
FOLLOW_SPEED_M_S = 2.5
FOLLOWER_ALTITUDE_M = 10.0
FOLLOWER_HORIZONTAL_M = 5.0
MAX_ATTEMPTS = 16
TILE_M = 100.0
FACADE_TILE_M = 3.2
ROOF_TILE_M = 4.0
PAVE_TILE_M = 1.2
ZEBRA_PITCH_M = 1.2


class _Reject(Exception):
    """A sampled attempt that cannot be validated; the reason is counted and retried."""


def _person(model: str, role: str, asset: str, xy: Point, yaw: float = 0.0,
            behavior: str = "idle", group: str = "ambient",
            route: tuple[tuple[float, ...], ...] = (), speed: float = 1.25,
            mode: str = "loop", hold: float | None = None,
            offset: float | None = None) -> PersonPlacement:
    return PersonPlacement(model, role, asset, xy, yaw, behavior, group, route, speed, mode,
                           None, hold, offset)


# ---------------------------------------------------------------------------------------------
# Layout sampling
# ---------------------------------------------------------------------------------------------
def _is_arterial(coordinate: float, arterials: tuple[float, ...]) -> bool:
    return any(abs(coordinate - value) < 1e-6 for value in arterials)


def _half_width(coordinate: float, arterials: tuple[float, ...]) -> float:
    return ARTERIAL_HALF_M if _is_arterial(coordinate, arterials) else STREET_HALF_M


def _setback(coordinate: float, arterials: tuple[float, ...]) -> float:
    return ARTERIAL_SETBACK_M if _is_arterial(coordinate, arterials) else BLOCK_SETBACK_M


def _lamp_offset(coordinate: float, arterials: tuple[float, ...]) -> float:
    return ARTERIAL_LAMP_OFFSET_M if _is_arterial(coordinate, arterials) else LAMP_OFFSET_M


def _tree_offset(coordinate: float, arterials: tuple[float, ...]) -> float:
    return ARTERIAL_TREE_OFFSET_M if _is_arterial(coordinate, arterials) else STREET_TREE_OFFSET_M


def _inside_roads(roads: tuple[float, ...], arterials: tuple[float, ...]) -> tuple[float, ...]:
    """Roads whose both sidewalks (half width + kerb inset) stay inside the actor inset."""
    inside = tuple(road for road in roads
                   if abs(road) + _half_width(road, arterials) + 0.9 <= ACTOR_INSET_M)
    if not inside:
        raise _Reject("no road inside the actor inset")
    return inside

def _road_axis(rng: random.Random) -> tuple[float, ...]:
    """Jittered road coordinates: a 60-80 m home block at the origin, 60-120 m blocks outside."""
    left = -rng.uniform(30.0, 40.0)
    right = rng.uniform(30.0, 40.0)
    coordinates = [left, right]
    position = left
    while True:
        position -= rng.uniform(60.0, 120.0)
        if position < -MAP_HALF_M + MAP_EDGE_MARGIN_M:
            break
        coordinates.append(position)
    position = right
    while True:
        position += rng.uniform(60.0, 120.0)
        if position > MAP_HALF_M - MAP_EDGE_MARGIN_M:
            break
        coordinates.append(position)
    coordinates.sort()
    return tuple(round(value, 3) for value in coordinates)


def _arterials(rng: random.Random, coordinates: tuple[float, ...]) -> tuple[float, ...]:
    candidates = [index for index, value in enumerate(coordinates) if abs(value) > 55.0]
    if len(candidates) < 2:
        candidates = list(range(len(coordinates)))
    count = rng.randint(2, min(4, len(candidates)))
    return tuple(sorted(coordinates[index] for index in rng.sample(candidates, count)))


def _zones(roads: tuple[float, ...], arterials: tuple[float, ...]
           ) -> list[tuple[float, float, float, float]]:
    """``(road_a, road_b, zone_a, zone_b)`` for every interval between consecutive roads."""
    edges = (-MAP_HALF_M, *roads, MAP_HALF_M)
    road_set = set(roads)
    zones = []
    for a, b in zip(edges, edges[1:]):
        margin_a = _setback(a, arterials) if a in road_set else MAP_EDGE_MARGIN_M
        margin_b = _setback(b, arterials) if b in road_set else MAP_EDGE_MARGIN_M
        zones.append((a, b, a + margin_a, b - margin_b))
    return zones


@dataclass(frozen=True)
class _Block:
    ix: int
    iy: int
    x0: float
    x1: float
    y0: float
    y1: float
    rx0: float
    rx1: float
    ry0: float
    ry1: float

    @property
    def center(self) -> Point:
        return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2

    @property
    def extent(self) -> tuple[float, float]:
        return self.x1 - self.x0, self.y1 - self.y0


def _blocks(x_roads: tuple[float, ...], x_arterials: tuple[float, ...],
            y_roads: tuple[float, ...], y_arterials: tuple[float, ...]) -> list[_Block]:
    x_zones = _zones(x_roads, x_arterials)
    y_zones = _zones(y_roads, y_arterials)
    out = []
    for ix, (rx0, rx1, x0, x1) in enumerate(x_zones):
        for iy, (ry0, ry1, y0, y1) in enumerate(y_zones):
            out.append(_Block(ix, iy, x0, x1, y0, y1, rx0, rx1, ry0, ry1))
    return out


def _home_block(blocks: list[_Block]) -> _Block:
    return min(blocks, key=lambda block: math.hypot(*block.center))


def _sample_buildings(rng: random.Random, blocks: list[_Block], used: set[tuple[int, int]],
                      downtown: Point, palette: tuple[tuple[float, float, float], ...]
                      ) -> tuple[BuildingSpec, ...]:
    buildings: list[BuildingSpec] = []
    for block in blocks:
        if (block.ix, block.iy) in used:
            continue
        width, depth = block.extent
        if width < 12.0 or depth < 12.0:
            continue
        center = block.center
        downtown_block = math.hypot(center[0] - downtown[0], center[1] - downtown[1]) <= 150.0
        max_columns = 2 if width >= 24.0 else 1
        max_rows = 2 if depth >= 24.0 else 1
        lots = rng.randint(1, max_columns * max_rows)
        columns = min(max_columns, lots)
        rows = math.ceil(lots / columns)
        lot_w, lot_d = width / columns, depth / rows
        for column in range(columns):
            for row in range(rows):
                if rng.random() > 0.86:
                    continue
                gap = rng.uniform(1.2, min(4.0, lot_w / 4.0, lot_d / 4.0))
                sx = max(7.0, lot_w - 2.0 * gap)
                sy = max(7.0, lot_d - 2.0 * gap)
                cx = block.x0 + (column + 0.5) * lot_w + rng.uniform(-0.6, 0.6)
                cy = block.y0 + (row + 0.5) * lot_d + rng.uniform(-0.6, 0.6)
                if downtown_block:
                    height = rng.uniform(17.0, 31.0)
                elif rng.random() < 0.35:
                    height = rng.uniform(11.0, 20.0)
                else:
                    height = rng.uniform(5.5, 12.5)
                buildings.append(BuildingSpec(cx, cy, sx, sy, height, rng.choice(palette)))
    if len(buildings) < 400:
        raise _Reject("too few buildings")
    if len(buildings) > 900:
        raise _Reject("too many buildings")
    return tuple(buildings)


def _crossing_arms(x_roads: tuple[float, ...], x_arterials: tuple[float, ...],
                   y_roads: tuple[float, ...], y_arterials: tuple[float, ...]) -> list[dict]:
    """Zebra arms at every junction (two per road) and the walking geometry across them."""
    arms = []
    for x in x_roads:
        for y in y_roads:
            extent = max(_half_width(x, x_arterials), _half_width(y, y_arterials))
            offset = extent + CROSSWALK_ARM_M
            half_x = _half_width(x, x_arterials)
            half_y = _half_width(y, y_arterials)
            for side in (-1.0, 1.0):
                arms.append({"center": (x, y + side * offset), "axis": "x",
                             "half": half_x, "side": side})
                arms.append({"center": (x + side * offset, y), "axis": "y",
                             "half": half_y, "side": side})
    return arms


def _mission_crossings(rng: random.Random, arms: list[dict], sites: list[Point]) -> list[dict]:
    """Two to four crossing arms near mission sites (the zebra the crossing people use)."""
    def inside(arm: dict) -> bool:
        cx, cy = arm["center"]
        reach = arm["half"] + 0.9 + 1.0
        x = abs(cx) + (reach if arm["axis"] == "x" else 0.0)
        y = abs(cy) + (reach if arm["axis"] == "y" else 0.0)
        return x <= ACTOR_INSET_M and y <= ACTOR_INSET_M

    usable = [arm for arm in arms if inside(arm)]
    scored = sorted(usable, key=lambda arm: min(
        math.hypot(arm["center"][0] - site[0], arm["center"][1] - site[1]) for site in sites))
    pool = scored[:12]
    rng.shuffle(pool)
    count = rng.randint(2, min(4, len(pool)))
    chosen: list[dict] = []
    for arm in pool:
        if all(math.hypot(arm["center"][0] - other["center"][0],
                          arm["center"][1] - other["center"][1]) > 30.0 for other in chosen):
            chosen.append(arm)
        if len(chosen) == count:
            break
    if not chosen:
        raise _Reject("no crossing arms inside the actor inset")
    return chosen


def _segment_clear_of(point: Point, segments: list[tuple[Point, Point]], radius: float) -> bool:
    for a, b in segments:
        ax, ay = a
        dx, dy = b[0] - ax, b[1] - ay
        denom = dx * dx + dy * dy
        t = 0.0 if denom == 0.0 else max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / denom))
        if math.hypot(point[0] - ax - t * dx, point[1] - ay - t * dy) < radius:
            return False
    return True


def _polyline_clear_of(point: Point, paths: list[list[Point]], radius: float) -> bool:
    segments: list[tuple[Point, Point]] = []
    for path in paths:
        segments.extend(zip(path, path[1:]))
    return _segment_clear_of(point, segments, radius)


def _sidewalk_interval(rng: random.Random, perpendicular: tuple[float, ...], *,
                       min_length: float, margin: float = 12.0, ) -> tuple[float, float] | None:
    """An along-road interval on one sidewalk between two junctions, inside the actor inset."""
    edges = [-ACTOR_INSET_M, *perpendicular, ACTOR_INSET_M]
    candidates = []
    for a, b in zip(edges, edges[1:]):
        low = max(a + margin, -ACTOR_INSET_M)
        high = min(b - margin, ACTOR_INSET_M)
        if high - low >= min_length:
            candidates.append((low, high))
    if not candidates:
        return None
    low, high = rng.choice(candidates)
    start = low + rng.uniform(0.0, (high - low) - min_length)
    end = start + rng.uniform(min_length, high - start)
    return start, end


def _sample_follow(rng: random.Random, roads_x: tuple[float, ...], roads_y: tuple[float, ...],
                   arterials_x: tuple[float, ...], arterials_y: tuple[float, ...]) -> dict:
    """A sidewalk follow route >= 100 m from home with a bus-shelter occluder on it."""
    for _ in range(400):
        vertical = rng.random() < 0.5
        arterials = arterials_x if vertical else arterials_y
        perpendicular = roads_y if vertical else roads_x
        roads = _inside_roads(roads_x if vertical else roads_y, arterials)
        road = rng.choice(roads)
        half = _half_width(road, arterials)
        lane = road + rng.choice((-1.0, 1.0)) * (half + 0.9)
        interval = _sidewalk_interval(rng, perpendicular, min_length=64.0)
        if interval is None:
            continue
        low, high = interval
        route = ((lane, low), (lane, high)) if vertical else ((low, lane), (high, lane))
        if min(math.hypot(*route[0]), math.hypot(*route[1])) < 100.0:
            continue
        fraction = rng.uniform(0.45, 0.60)
        sx = route[0][0] + (route[1][0] - route[0][0]) * fraction
        sy = route[0][1] + (route[1][1] - route[0][1]) * fraction
        side = 1.0 if lane > road else -1.0
        back = (side, 0.0) if vertical else (0.0, side)
        return {"route": route, "start": route[0], "shelter": (sx, sy),
                "shelter_yaw": math.atan2(back[1], back[0])}
    raise _Reject("no follow route")


def _lamp_yaw(vertical: bool, side: float) -> float:
    """Yaw so the lamp's arm (local -y) points from the sidewalk back over the road."""
    if vertical:
        return -side * math.pi / 2
    return 0.0 if side > 0 else math.pi


def _crown_radius(species: str, height: float) -> float:
    if species == "oak":
        return 6.43360 * (height / 10.0003)
    return 3.74841 * (height / 11.9999)


def _near_junction(x: float, y: float, roads: tuple[float, ...], margin: float) -> bool:
    return any(abs(value - x) < margin or abs(value - y) < margin for value in roads)


def _sample_street_furniture(rng: random.Random, roads_x: tuple[float, ...],
                             roads_y: tuple[float, ...], arterials_x: tuple[float, ...],
                             arterials_y: tuple[float, ...], follow_route: tuple[Point, Point],
                             targets: list[Point], structures: list[tuple[float, float, float]],
                             person_paths: list[list[Point]], traffic_paths: list[list[Point]],
                             parked_points: list[Point], scan_zone: tuple[float, float, float]
                             ) -> tuple[list[tuple[float, float, float]], list[TreeSpec]]:
    """Lamps along the sidewalks and street trees on some segments.

    Mission targets keep 3 m from lamps and their crown radius plus 2.8 m from trees; the
    follow route (drone path at 10 m) keeps 6 m from lamps and the crown radius plus 2.8 m
    from trees; circulating vehicle lanes keep the crown plus 2.8 m, parked cars the crown
    plus 2.6 m and every person route the crown plus the walker envelope away from trees.
    ``scan_zone`` (the campaign's plaza scan rectangle) stays completely clear.
    """
    lamps: list[tuple[float, float, float]] = []
    trees: list[TreeSpec] = []
    zone_x, zone_y, zone_r = scan_zone
    for vertical, roads, arterials, perpendicular in (
        (True, roads_x, arterials_x, roads_y), (False, roads_y, arterials_y, roads_x),
    ):
        for road in roads:
            lamp_offset = _lamp_offset(road, arterials)
            tree_offset = _tree_offset(road, arterials)
            for side in (-1.0, 1.0):
                along = -ACTOR_INSET_M + 12.0
                while along < ACTOR_INSET_M - 12.0:
                    near_site = min((math.hypot(road + side * lamp_offset - x, along - y)
                                     for x, y in targets), default=1e9)
                    spacing = 26.0 if near_site < 120.0 else 46.0
                    along += rng.uniform(spacing * 0.7, spacing * 1.3)
                    if along >= ACTOR_INSET_M - 12.0:
                        break
                    lx = road + side * lamp_offset if vertical else along
                    ly = along if vertical else road + side * lamp_offset
                    if _near_junction(lx, ly, perpendicular, 10.0):
                        continue
                    if math.hypot(lx - zone_x, ly - zone_y) < zone_r + 2.8:
                        continue
                    if any(math.hypot(lx - x, ly - y) < 3.0 for x, y in targets):
                        continue
                    if any(math.hypot(lx - x, ly - y) < keep for x, y, keep in structures):
                        continue
                    if not _segment_clear_of((lx, ly), [follow_route], 6.0):
                        continue
                    if any(math.hypot(lx - other[0], ly - other[1]) < 6.0 for other in lamps):
                        continue
                    lamps.append((round(lx, 3), round(ly, 3), round(_lamp_yaw(vertical, side), 4)))
                if rng.random() < 0.62:
                    along = -ACTOR_INSET_M + 14.0
                    while along < ACTOR_INSET_M - 14.0:
                        along += rng.uniform(18.0, 30.0)
                        if along >= ACTOR_INSET_M - 14.0:
                            break
                        tx = road + side * tree_offset if vertical else along
                        ty = along if vertical else road + side * tree_offset
                        if _near_junction(tx, ty, perpendicular, 12.0):
                            continue
                        species = "oak" if rng.random() < 0.7 else "pine"
                        height = rng.uniform(5.0, 7.6) if species == "oak" else rng.uniform(6.5, 9.5)
                        crown = _crown_radius(species, height)
                        if not _tree_clear((tx, ty), crown, follow_route, targets, structures,
                                           person_paths, traffic_paths, parked_points,
                                           scan_zone):
                            continue
                        if any(math.hypot(tx - lamp[0], ty - lamp[1]) < 3.0 for lamp in lamps):
                            continue
                        if any(math.hypot(tx - other.x, ty - other.y) < crown * 0.8 + 1.0
                               for other in trees):
                            continue
                        trees.append(TreeSpec(round(tx, 3), round(ty, 3), round(height, 3), species, True))
    if not lamps:
        raise _Reject("no lamp positions")
    return lamps, trees


def _tree_clear(point: Point, crown: float, follow_route: tuple[Point, Point],
                targets: list[Point], structures: list[tuple[float, float, float]],
                person_paths: list[list[Point]], traffic_paths: list[list[Point]],
                parked_points: list[Point], scan_zone: tuple[float, float, float]) -> bool:
    """Every static clearance a placed tree must keep (see ``_sample_street_furniture``)."""
    if math.hypot(point[0] - scan_zone[0], point[1] - scan_zone[1]) < scan_zone[2] + crown:
        return False  # the campaign's plaza scan rectangle stays canopy-free
    if not _segment_clear_of(point, [follow_route], crown + 2.8):
        return False
    # 2.8 m keeps the crown clear of the target's 0.6 m vantage column (2.0 m clearance).
    if any(math.hypot(point[0] - x, point[1] - y) < crown + 2.8 for x, y in targets):
        return False
    if any(math.hypot(point[0] - x, point[1] - y) < crown + keep for x, y, keep in structures):
        return False
    if not _polyline_clear_of(point, person_paths, crown + 1.2):
        return False
    if not _polyline_clear_of(point, traffic_paths, crown + 2.8):
        return False
    if any(math.hypot(point[0] - x, point[1] - y) < crown + 2.6 for x, y in parked_points):
        return False
    return True


def _sample_park_trees(rng: random.Random, park: ParkSpec, benches: list[Bench],
                       person_paths: list[list[Point]], traffic_paths: list[list[Point]],
                       parked_points: list[Point], scan_zone: tuple[float, float, float]
                       ) -> list[TreeSpec]:
    trees: list[TreeSpec] = []
    target_count = max(4, min(16, int(park.half_size * park.half_size / 22.0)))
    for _ in range(target_count * 40):
        if len(trees) >= target_count:
            break
        x = park.x + rng.uniform(-park.half_size + 1.5, park.half_size - 1.5)
        y = park.y + rng.uniform(-park.half_size + 1.5, park.half_size - 1.5)
        if abs(x - park.x) < 3.0 or abs(y - park.y) < 3.0:  # keep the path cross open
            continue
        species = "oak" if rng.random() < 0.6 else "pine"
        height = rng.uniform(6.5, 10.0) if species == "oak" else rng.uniform(8.0, 12.0)
        crown = _crown_radius(species, height)
        if math.hypot(x - scan_zone[0], y - scan_zone[1]) < scan_zone[2] + crown:
            continue
        if any(math.hypot(x - bench.x, y - bench.y) < crown * 0.6 + 1.5 for bench in benches):
            continue
        if not _polyline_clear_of((x, y), person_paths, crown + 1.2):
            continue
        if not _polyline_clear_of((x, y), traffic_paths, crown + 2.8):
            continue
        if any(math.hypot(x - px, y - py) < crown + 2.6 for px, py in parked_points):
            continue
        if any(math.hypot(x - tree.x, y - tree.y) < crown * 0.8 + 1.0 for tree in trees):
            continue
        trees.append(TreeSpec(round(x, 3), round(y, 3), round(height, 3), species, False))
    return trees


def _sites_list(plazas: tuple[PlazaSpec, ...], parks: tuple[ParkSpec, ...], downtown: Point,
                bus_stop: Point, collapsed: Point, follow: dict, home_half: float
                ) -> tuple[tuple[str, str, float, float, float], ...]:
    entries: list[tuple[str, str, float, float, float]] = [
        ("home_plaza", "plaza", 0.0, 0.0, round(home_half, 3)),
        ("downtown", "district", round(downtown[0], 3), round(downtown[1], 3), 150.0),
        ("bus_stop", "bus_stop", round(bus_stop[0], 3), round(bus_stop[1], 3), 8.0),
        ("collapse", "casualty", round(collapsed[0], 3), round(collapsed[1], 3), 3.0),
        ("follow_start", "follow", round(follow["start"][0], 3), round(follow["start"][1], 3), 5.0),
        ("follow_occluder", "follow", round(follow["shelter"][0], 3), round(follow["shelter"][1], 3),
         5.0),
    ]
    for index, plaza in enumerate(plazas):
        entries.append(("mission_plaza" if plaza.mission else f"plaza_{index:02d}", "plaza",
                        round(plaza.x, 3), round(plaza.y, 3), round(plaza.half_size, 3)))
    for index, park in enumerate(parks):
        entries.append(("mission_park" if park.mission else f"park_{index:02d}", "park",
                        round(park.x, 3), round(park.y, 3), round(park.half_size, 3)))
    return tuple(entries)


def _sample_crowd(rng: random.Random, plaza: PlazaSpec,
                  count: int) -> tuple[PersonPlacement, ...]:
    assets = ("person_blue_kinematic", "person_green_kinematic", "person_dark",
              "person_orange", "person_hivis")
    edge = plaza.half_size - 1.0
    people = []
    for index in range(count):
        x = plaza.x + rng.uniform(-edge, edge)
        y = plaza.y + rng.uniform(-edge, edge)
        people.append(_person(f"civ_plaza_crowd_{index + 1:02d}", "plaza_crowd",
                              assets[index % len(assets)], (x, y),
                              rng.uniform(-math.pi, math.pi)))
    return tuple(people)


def _bus_stop_at(rng: random.Random, block: _Block, roads_x: tuple[float, ...],
                 arterials_x: tuple[float, ...], roads_y: tuple[float, ...],
                 arterials_y: tuple[float, ...], avoid: list[Point]) -> BusStopSpec:
    """A shelter on the block's outer sidewalk platform: back wall away from the road.

    ``avoid`` points (the collapsed casualty, the follow occluder) keep 14 m: a shelter's
    glass end walls and roof must never crowd a mission target's vantage column.
    """
    candidates = []
    for vertical, road, arterials in ((True, block.rx0, arterials_x), (True, block.rx1, arterials_x),
                                      (False, block.ry0, arterials_y), (False, block.ry1, arterials_y)):
        half = _half_width(road, arterials)
        center_offset = half + SIDEWALK_WIDTH_M + 1.55
        for side in (-1.0, 1.0):
            if vertical:
                x = road + side * center_offset
                y = (block.y0 + block.y1) / 2 + rng.uniform(-6.0, 6.0)
                back = (side, 0.0)
            else:
                x = (block.x0 + block.x1) / 2 + rng.uniform(-6.0, 6.0)
                y = road + side * center_offset
                back = (0.0, side)
            if any(math.hypot(x - px, y - py) < 14.0 for px, py in avoid):
                continue
            candidates.append(BusStopSpec(round(x, 3), round(y, 3),
                                          round(math.atan2(back[1], back[0]), 4)))
    if not candidates:
        raise _Reject("no bus stop clear of mission targets")
    return rng.choice(candidates)


def _sample_bus_waiting(rng: random.Random, stop: BusStopSpec) -> tuple[PersonPlacement, ...]:
    assets = ("person_blue_kinematic", "person_green_kinematic", "person_dark", "person_orange")
    back = (math.cos(stop.yaw), math.sin(stop.yaw))
    along = (-back[1], back[0])
    facing = math.atan2(-back[1], -back[0])
    count = rng.randint(3, 5)
    people = []
    for index in range(count):
        u = -1.6 + 3.2 * (index + 0.5) / count + rng.uniform(-0.15, 0.15)
        offset = rng.uniform(0.72, 1.05)
        x = stop.x - back[0] * offset + along[0] * u
        y = stop.y - back[1] * offset + along[1] * u
        people.append(_person(f"civ_bus_waiting_{index + 1:02d}", "bus_waiting",
                              assets[index % len(assets)], (x, y),
                              facing + rng.uniform(-0.3, 0.3)))
    return tuple(people)


def _sample_sitters(rng: random.Random, mission_park: ParkSpec,
                    parks: tuple[ParkSpec, ...]) -> tuple[tuple[PersonPlacement, ...], list[Bench]]:
    """Sitting people beside the mission park's paths, each on its own bench."""
    count = rng.randint(2, 4)
    sitters: list[PersonPlacement] = []
    benches: list[Bench] = []
    slots: list[Point] = []
    edge = mission_park.half_size - 1.5
    for index in range(count):
        for _ in range(200):
            side = rng.choice((-1.0, 1.0))
            if rng.random() < 0.5:
                x = mission_park.x + side * rng.uniform(2.0, edge)
                y = mission_park.y + rng.uniform(-edge, edge)
            else:
                x = mission_park.x + rng.uniform(-edge, edge)
                y = mission_park.y + side * rng.uniform(2.0, edge)
            if any(math.hypot(x - px, y - py) < 4.0 for px, py in slots):
                continue
            slots.append((x, y))
            dx, dy = mission_park.x - x, mission_park.y - y
            norm = math.hypot(dx, dy) or 1.0
            facing = math.atan2(dy / norm, dx / norm)
            sitters.append(_person(f"civ_park_sitting_{index + 1:02d}", "park_sitting",
                                   "person_sitting", (x, y), facing))
            benches.append(Bench(round(x - math.cos(facing) * 0.55, 3),
                                 round(y - math.sin(facing) * 0.55, 3), round(facing, 4)))
            break
    if len(sitters) < 2:
        raise _Reject("no park sitter slots")
    for park in parks[1:]:
        for _ in range(rng.randint(1, 2)):
            for _ in range(50):
                bx = park.x + rng.uniform(-park.half_size + 2.0, park.half_size - 2.0)
                by = park.y + rng.uniform(-park.half_size + 2.0, park.half_size - 2.0)
                if abs(bx - park.x) < 2.5 or abs(by - park.y) < 2.5:
                    continue
                benches.append(Bench(round(bx, 3), round(by, 3), round(rng.uniform(-math.pi, math.pi), 4)))
                break
    return tuple(sitters), benches


def _sample_commuters(rng: random.Random, roads_x: tuple[float, ...], roads_y: tuple[float, ...],
                      arterials_x: tuple[float, ...], arterials_y: tuple[float, ...],
                      sites: list[Point]) -> tuple[PersonPlacement, ...]:
    assets = ("person_blue_kinematic", "person_green_kinematic", "person_dark",
              "person_orange", "person_hivis")
    people = []
    count = rng.randint(6, 10)
    for index in range(count):
        for _ in range(200):
            vertical = rng.random() < 0.5
            arterials = arterials_x if vertical else arterials_y
            perpendicular = roads_y if vertical else roads_x
            roads = _inside_roads(roads_x if vertical else roads_y, arterials)
            road = rng.choice(roads)
            half = _half_width(road, arterials)
            lane = road + rng.choice((-1.0, 1.0)) * (half + 0.9)
            interval = _sidewalk_interval(rng, perpendicular, min_length=24.0)
            if interval is None:
                continue
            low, high = interval
            if rng.random() < 0.6:
                site = sites[rng.randrange(len(sites))]
                site_axis = site[1] if vertical else site[0]
                if not (low - 70.0 <= site_axis <= high + 70.0):
                    continue
            length = rng.uniform(24.0, min(48.0, high - low))
            if rng.random() < 0.5:
                start, end = low, low + length
            else:
                start, end = high, high - length
            route = ((lane, start), (lane, end)) if vertical else ((start, lane), (end, lane))
            yaw = math.atan2(route[1][1] - route[0][1], route[1][0] - route[0][0])
            people.append(_person(f"civ_commuter_{index + 1:02d}", "commuter",
                                  assets[rng.randrange(len(assets))], route[0], yaw, "route",
                                  route=route, speed=rng.uniform(1.1, 1.6), mode="pingpong"))
            break
    if len(people) < 6:
        raise _Reject("too few commuters")
    return tuple(people)


def _sample_crossings(rng: random.Random, arms: list[dict]) -> tuple[PersonPlacement, ...]:
    assets = ("person_blue_kinematic", "person_green_kinematic", "person_orange")
    people = []
    for index, arm in enumerate(arms, 1):
        cx, cy = arm["center"]
        reach = arm["half"] + 0.9
        if arm["axis"] == "x":
            start, end = (cx - reach, cy), (cx + reach, cy)
        else:
            start, end = (cx, cy - reach), (cx, cy + reach)
        if rng.random() < 0.5:
            start, end = end, start
        people.append(_person(f"civ_crossing_{index:02d}", "crossing",
                              assets[index % len(assets)], start, 0.0, "route",
                              route=(start, end), speed=1.0, mode="pingpong",
                              offset=rng.choice((0.0, 12.0, 20.0))))
    return tuple(people)


def _sample_joggers(rng: random.Random, roads_x: tuple[float, ...], roads_y: tuple[float, ...],
                    arterials_x: tuple[float, ...], arterials_y: tuple[float, ...]
                    ) -> tuple[PersonPlacement, ...]:
    assets = ("person_orange", "person_blue_kinematic", "person_hivis")
    people = []
    for index in range(2):
        for _ in range(200):
            vertical = rng.random() < 0.5
            arterials = arterials_x if vertical else arterials_y
            perpendicular = roads_y if vertical else roads_x
            roads = _inside_roads(roads_x if vertical else roads_y, arterials)
            road = rng.choice(roads)
            half = _half_width(road, arterials)
            lane = road + rng.choice((-1.0, 1.0)) * (half + 0.9)
            interval = _sidewalk_interval(rng, perpendicular, min_length=24.0)
            if interval is None:
                continue
            low, high = interval
            route = ((lane, low), (lane, high)) if vertical else ((low, lane), (high, lane))
            yaw = math.atan2(route[1][1] - route[0][1], route[1][0] - route[0][0])
            people.append(_person(f"civ_jogger_{index + 1:02d}", "jogger",
                                  assets[index % len(assets)], route[0], yaw, "route",
                                  route=route, speed=2.5, mode="pingpong"))
            break
    if len(people) < 2:
        raise _Reject("too few joggers")
    return tuple(people)


def _sample_collapsed(rng: random.Random, roads_x: tuple[float, ...], roads_y: tuple[float, ...],
                      arterials_x: tuple[float, ...], arterials_y: tuple[float, ...],
                      follow_route: tuple[Point, Point]) -> PersonPlacement:
    for _ in range(400):
        vertical = rng.random() < 0.5
        arterials = arterials_x if vertical else arterials_y
        perpendicular = roads_y if vertical else roads_x
        roads = _inside_roads(roads_x if vertical else roads_y, arterials)
        road = rng.choice(roads)
        half = _half_width(road, arterials)
        lane = road + rng.choice((-1.0, 1.0)) * (half + 0.9)
        interval = _sidewalk_interval(rng, perpendicular, min_length=4.0, margin=14.0)
        if interval is None:
            continue
        along = rng.uniform(*interval)
        point = (lane, along) if vertical else (along, lane)
        if math.hypot(*point) < 120.0:
            continue
        if not _segment_clear_of(point, [follow_route], 30.0):
            continue
        return _person("civ_collapsed_01", "collapsed", "person_collapsed_kinematic", point,
                       rng.uniform(-math.pi, math.pi))
    raise _Reject("no collapsed location")


def _sample_traffic(seed: int, attempt: int, blocks: list[_Block], sites: list[Point]
                    ) -> tuple[VehiclePlacement, ...]:
    rng = substream(seed, "traffic", str(attempt))
    moving = [name for name in SAMPLED_TYPES if vehicle_type(name).mesh is not None]
    if not moving:
        return ()
    heavy = [name for name in moving if vehicle_type(name).heavy]
    candidates = [block for block in blocks if block.extent[0] >= 40.0 and block.extent[1] >= 40.0
                  and max(abs(block.rx0), abs(block.rx1), abs(block.ry0), abs(block.ry1))
                  <= ACTOR_INSET_M - 4.0]
    near = sorted(candidates, key=lambda block: min(
        math.hypot(block.center[0] - site[0], block.center[1] - site[1]) for site in sites))[:8]
    if not near:
        return ()
    vehicles: list[VehiclePlacement] = []
    counts: dict[str, int] = {}
    for index in range(rng.randint(3, 7)):
        if index == 0 and heavy:
            type_name = rng.choice(heavy)
        else:
            type_name = rng.choices(moving, [1 + (3 if vehicle_type(name).heavy else 0)
                                             for name in moving])[0]
        kind = vehicle_type(type_name)
        clockwise = False if kind.heavy else rng.random() < 0.5
        block = rng.choice(near)
        # One lane offset for every road class: the inner lane keeps every vehicle clear of
        # the kerbside parking lane and of the sidewalk furniture (shelter roofs included).
        lane = 1.9
        side = rng.choice(("north", "south", "east", "west"))
        route = block_loop(block.rx0, block.rx1, block.ry0, block.ry1, clockwise=clockwise,
                           start_side=side, lane=lane)
        counts[type_name] = counts.get(type_name, 0) + 1
        speed = round(rng.uniform(*kind.max_speed_m_s), 2)
        accel = round(rng.uniform(*kind.accel_m_s2), 2)
        offset = round(rng.uniform(0.0, 20.0), 2)
        vehicles.append(VehiclePlacement.loop(
            f"veh_{type_name}_{counts[type_name]:02d}", type_name, route, speed, accel,
            start_offset_s=offset, variant=rng.randrange(8)))
    return tuple(vehicles)


def _sample_parked(seed: int, attempt: int, roads_x: tuple[float, ...],
                   roads_y: tuple[float, ...], arterials_x: tuple[float, ...],
                   arterials_y: tuple[float, ...], sites: list[Point],
                   vehicles: tuple[VehiclePlacement, ...]) -> list[ParkedCar]:
    rng = substream(seed, "parked", str(attempt))
    types = [name for name in PARKED_TYPES if vehicle_type(name).mesh is not None]
    if not types:
        return []
    palette = ((0.42, 0.19, 0.11), (0.16, 0.25, 0.43), (0.68, 0.68, 0.63))
    parked: list[ParkedCar] = []
    for _ in range(rng.randint(4, 10)):
        for _ in range(200):
            vertical = rng.random() < 0.5
            roads = roads_x if vertical else roads_y
            arterials = arterials_x if vertical else arterials_y
            perpendicular = roads_y if vertical else roads_x
            site = sites[rng.randrange(len(sites))]
            road = min(roads, key=lambda value: abs(value - (site[0] if vertical else site[1])))
            half = _half_width(road, arterials)
            along = (site[1] if vertical else site[0]) + rng.uniform(-90.0, 90.0)
            along = max(-ACTOR_INSET_M + 20.0, min(ACTOR_INSET_M - 20.0, along))
            # 14 m keeps cars clear of every zebra arm (offset ~7-10 m plus the 1.8 m patch).
            if any(abs(along - cross) < 14.0 for cross in perpendicular):
                continue
            type_name = rng.choice(types)
            kind = vehicle_type(type_name)
            side = rng.choice((-1.0, 1.0))
            if vertical:
                yaw = math.pi / 2 if side > 0 else -math.pi / 2
                x, y = road + side * (half - 1.0), along
            else:
                yaw = math.pi if side > 0 else 0.0
                x, y = along, road + side * (half - 1.0)
            shift = (half - PARKING_INSET_M - kind.width_m / 2) - (half - 1.0)
            right = (math.sin(yaw), -math.cos(yaw))
            candidate = ParkedCar(round(x + right[0] * shift, 3), round(y + right[1] * shift, 3),
                                  yaw, palette[len(parked) % len(palette)], type_name,
                                  rng.randrange(8))
            if _parked_meets_traffic(candidate, vehicles):
                continue
            parked.append(candidate)
            break
    return parked


def _parked_meets_traffic(car: ParkedCar, vehicles: tuple[VehiclePlacement, ...]) -> bool:
    width = vehicle_type(car.type).width_m / 2
    for vehicle in vehicles:
        points = [tuple(point[:2]) for point in vehicle.waypoints] + [tuple(vehicle.waypoints[0][:2])]
        half = vehicle_type(vehicle.type).width_m / 2
        if distance_to_path((car.x, car.y), points) < half + width + 0.3:
            return True
    return False


def _actor_inside_geofence(layout: UrbanLayout, inset: float) -> bool:
    for person in layout.people:
        points = [person.position, *((p[0], p[1]) for p in person.waypoints)]
        if any(abs(x) > inset or abs(y) > inset for x, y in points):
            return False
    for vehicle in layout.vehicles:
        if any(abs(p[0]) > inset or abs(p[1]) > inset for p in vehicle.waypoints):
            return False
    return True


def _sample_attempt(seed: int, attempt: int, rng: random.Random) -> UrbanLayout:
    roads_x = _road_axis(rng)
    roads_y = _road_axis(rng)
    arterials_x = _arterials(rng, roads_x)
    arterials_y = _arterials(rng, roads_y)
    blocks = _blocks(roads_x, arterials_x, roads_y, arterials_y)
    home = _home_block(blocks)
    used: set[tuple[int, int]] = {(home.ix, home.iy)}
    home_half = min(HOME_PLAZA_MAX_HALF_M, (home.x1 - home.x0) / 2 - 1.0,
                    (home.y1 - home.y0) / 2 - 1.0)
    if home_half < 8.0:
        raise _Reject("home block too small")

    def choose_block(min_distance: float, max_distance: float | None = None,
                     min_extent: float = 18.0) -> _Block:
        candidates = [block for block in blocks if (block.ix, block.iy) not in used
                      and block.extent[0] >= min_extent and block.extent[1] >= min_extent
                      and min_distance <= math.hypot(*block.center)
                      and (max_distance is None or math.hypot(*block.center) <= max_distance)]
        if not candidates:
            raise _Reject("no block available")
        block = rng.choice(candidates)
        used.add((block.ix, block.iy))
        return block

    mission_block = choose_block(180.0, 480.0)
    mission_half = min(mission_block.extent[0] / 2 - 0.5, mission_block.extent[1] / 2 - 0.5,
                       rng.uniform(9.0, 15.0))
    plazas = [PlazaSpec(mission_block.center[0], mission_block.center[1], mission_half, True)]
    for _ in range(rng.randint(1, 3)):
        block = choose_block(70.0)
        half = min(block.extent[0] / 2 - 0.5, block.extent[1] / 2 - 0.5, rng.uniform(7.0, 13.0))
        plazas.append(PlazaSpec(block.center[0], block.center[1], half))
    plazas = tuple(plazas)

    park_count = rng.randint(3, 6)
    parks: list[ParkSpec] = []
    for index in range(park_count):
        block = choose_block(60.0)
        half = min(block.extent[0] / 2 - 0.5, block.extent[1] / 2 - 0.5, rng.uniform(7.0, 18.0))
        parks.append(ParkSpec(block.center[0], block.center[1], half, index == 0))
    parks = tuple(parks)
    mission_park = parks[0]
    plazas = (*plazas, PlazaSpec(0.0, 0.0, home_half))  # the landing-pad plaza at home

    downtown_block = choose_block(200.0, 520.0, min_extent=24.0)
    downtown = downtown_block.center
    palette = ((0.66, 0.57, 0.48), (0.58, 0.62, 0.65), (0.70, 0.64, 0.55),
               (0.56, 0.63, 0.67), (0.62, 0.67, 0.68), (0.71, 0.66, 0.58))
    buildings = _sample_buildings(rng, blocks, used, downtown, palette)

    follow = _sample_follow(rng, roads_x, roads_y, arterials_x, arterials_y)
    follow_route = (follow["start"], follow["route"][1])
    collapsed = _sample_collapsed(rng, roads_x, roads_y, arterials_x, arterials_y, follow_route)

    avoid_targets = [collapsed.position, follow["shelter"]]
    bus_stops = [_bus_stop_at(rng, choose_block(90.0, 420.0), roads_x, arterials_x,
                              roads_y, arterials_y, avoid_targets)]
    for _ in range(rng.randint(1, 2)):
        bus_stops.append(_bus_stop_at(rng, choose_block(80.0), roads_x, arterials_x,
                                      roads_y, arterials_y, avoid_targets))
    bus_stops = tuple(bus_stops)

    arms = _crossing_arms(roads_x, arterials_x, roads_y, arterials_y)
    mission_sites = [(plazas[0].x, plazas[0].y), (mission_park.x, mission_park.y),
                     (bus_stops[0].x, bus_stops[0].y), collapsed.position]
    crossing_arms = _mission_crossings(rng, arms, mission_sites)

    crowd_count = rng.randint(8, 16)
    crowd = list(_sample_crowd(rng, plazas[0], crowd_count))
    red_index = rng.randrange(crowd_count)
    red_shirt = _person("civ_red_shirt_01", "red_shirt", "person_red_kinematic",
                        crowd[red_index].position, rng.uniform(-math.pi, math.pi))
    crowd[red_index] = red_shirt

    bus_waiting = _sample_bus_waiting(rng, bus_stops[0])
    sitters, benches = _sample_sitters(rng, mission_park, parks)
    commuters = _sample_commuters(rng, roads_x, roads_y, arterials_x, arterials_y,
                                  [(plazas[0].x, plazas[0].y), (mission_park.x, mission_park.y),
                                   (bus_stops[0].x, bus_stops[0].y), collapsed.position])
    crossings = _sample_crossings(rng, crossing_arms)
    joggers = _sample_joggers(rng, roads_x, roads_y, arterials_x, arterials_y)
    follow_yaw = math.atan2(follow_route[1][1] - follow_route[0][1],
                            follow_route[1][0] - follow_route[0][0])
    follow_person = _person("civ_jogger_03", "jogger", "person_red_kinematic", follow_route[0],
                            follow_yaw, "route", "follow_jogger", follow_route,
                            FOLLOW_SPEED_M_S, "once")

    if math.dist(red_shirt.position, follow_person.position) < 25.0 \
            or distance_to_path(red_shirt.position, follow_route) < 15.0:
        raise _Reject("red shirt too close to the follow route")

    total = (len(commuters) + len(crossings) + len(bus_waiting) + len(joggers) + 1
             + len(sitters) + len(crowd) + 1)
    if total > 40:
        trim = total - 40
        if len(crowd) - trim < 8:
            raise _Reject("too many people")
        crowd = [person for person in crowd if person is not red_shirt][:len(crowd) - trim]
        crowd.append(red_shirt)

    targets = [person.position for person in (*crowd, *bus_waiting, *sitters, collapsed)]
    structures = [(stop.x, stop.y, 5.5) for stop in bus_stops]
    structures.append((follow["shelter"][0], follow["shelter"][1], 5.5))
    person_paths: list[list[Point]] = []
    for person in (*commuters, *crossings, *joggers, follow_person):
        if person.waypoints:
            person_paths.append([(float(p[0]), float(p[1])) for p in person.waypoints])

    scene_points = [(plazas[0].x, plazas[0].y), (mission_park.x, mission_park.y),
                    (bus_stops[0].x, bus_stops[0].y), collapsed.position, follow["shelter"],
                    downtown]
    vehicles = _sample_traffic(seed, attempt, blocks, scene_points)
    parked = _sample_parked(seed, attempt, roads_x, roads_y, arterials_x, arterials_y,
                            scene_points, vehicles)
    traffic_paths = [[tuple(point[:2]) for point in vehicle.waypoints]
                     + [tuple(vehicle.waypoints[0][:2])] for vehicle in vehicles]
    parked_points = [(car.x, car.y) for car in parked]

    scan_zone = (plazas[0].x, plazas[0].y, plazas[0].half_size + 5.65 + 1.0)
    lamps, street_trees = _sample_street_furniture(
        rng, roads_x, roads_y, arterials_x, arterials_y, follow_route, targets, structures,
        person_paths, traffic_paths, parked_points, scan_zone)
    park_trees: list[TreeSpec] = []
    for park in parks:
        in_park = [bench for bench in benches
                   if math.hypot(bench.x - park.x, bench.y - park.y) <= park.half_size + 2.0]
        park_trees.extend(_sample_park_trees(rng, park, in_park, person_paths, traffic_paths,
                                             parked_points, scan_zone))
    trees = tuple([*street_trees, *park_trees])

    # Rooftop equipment sits on the 0.45 m parapet ring, so the collision top of the tallest
    # building is height + 0.45 + equipment; the transit rule clears every proxy top.
    building_tops = [building.height + 0.45 + (1.2 if building.height > 18.0 else 0.9)
                     for building in buildings]
    max_top = round(max([*building_tops, *(tree.height for tree in trees), 0.0]), 3)
    if max_top + 5.9 > 48.0:
        raise _Reject("scenery too tall for the flight profile")

    sites = _sites_list(plazas, parks, downtown, (bus_stops[0].x, bus_stops[0].y),
                        collapsed.position, follow, home_half)
    city = CitySpec(actor_inset_m=ACTOR_INSET_M, max_top_m=max_top,
                    arterial_roads_x=arterials_x, arterial_roads_y=arterials_y,
                    plazas=plazas, parks=parks, trees=trees, bus_stops=bus_stops, sites=sites,
                    lamps=tuple(lamps))
    return UrbanLayout(
        seed=seed, canonical=False, roads_x=roads_x, roads_y=roads_y,
        crosswalk_centres=tuple(arm["center"] for arm in crossing_arms), buildings=buildings,
        plaza_half_size=home_half, park_center=(mission_park.x, mission_park.y),
        park_half_size=mission_park.half_size,
        park_trees=tuple(ParkTree(tree.x, tree.y, tree.height) for tree in park_trees),
        benches=tuple(benches), lamp_positions=tuple((lamp[0], lamp[1]) for lamp in lamps),
        utility_poles=(), trash_bins=(), bus_shelter_center=(bus_stops[0].x, bus_stops[0].y),
        jogger_shelter_center=follow["shelter"], jogger_shelter_size=(3.3, SHELTER_LENGTH_M),
        parked_cars=tuple(parked), commuters=commuters, crossings=crossings,
        bus_waiting=bus_waiting, joggers=joggers, follow_jogger=follow_person,
        park_sitting=sitters, plaza_crowd=tuple(crowd), red_shirt=red_shirt, collapsed=collapsed,
        vehicles=vehicles, follow_speed=FOLLOW_SPEED_M_S, follower_altitude_m=FOLLOWER_ALTITUDE_M,
        follower_horizontal_m=FOLLOWER_HORIZONTAL_M, expected_occlusion_s=0.0, attempts=attempt,
        follow_occluder_kind="shelter", follow_occluder_yaw=follow["shelter_yaw"],
        lamp_height_m=9.27, map=PROCEDURAL_MAP, actor_inset_m=ACTOR_INSET_M,
        scenery_max_top_m=max_top, city=city)


def sample_urban_layout(seed: int, *,
                        validate: Callable[[UrbanLayout], float | None] | None = None
                        ) -> UrbanLayout:
    """Sample and validate one procedural 1.2 km city layout (plain ``random.Random(seed)``)."""
    if validate is None:
        from sim.worldgen import _validate_candidate

        validate = lambda layout: _validate_candidate("urban", layout)  # noqa: E731
    rng = random.Random(seed)
    rejections: Counter[str] = Counter()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            layout = _sample_attempt(seed, attempt, rng)
        except _Reject as exc:
            rejections[str(exc)] += 1
            continue
        if not _actor_inside_geofence(layout, ACTOR_INSET_M):
            rejections["actor geofence"] += 1
            continue
        occlusion = validate(layout)
        if occlusion is not None:
            return replace(layout, expected_occlusion_s=occlusion)
        from sim.worldgen.validation import LAST_FAILURES

        rejections["validation"] += 1
        if LAST_FAILURES:
            rejections[f"validation: {LAST_FAILURES[0][:90]}"] += 1
    top = ", ".join(f"{reason} x{count}" for reason, count in rejections.most_common(4))
    raise RuntimeError(f"could not sample valid urban layout for seed {seed} after "
                       f"{sum(rejections.values())} attempts; top rejections: {top or 'pre-checks'}")


# ---------------------------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------------------------
class _Mesh:
    """A merged textured mesh under construction (world coordinates)."""

    def __init__(self, name: str, material: str, texture: str | None):
        self.name = name
        self.material = material
        self.texture = texture
        self.positions: list[tuple[float, float, float]] = []
        self.normals: list[tuple[float, float, float]] = []
        self.uvs: list[tuple[float, float]] = []
        self.faces: list[tuple[int, int, int]] = []
        self.proxies: list[ProxyShape] = []

    def quad(self, corners, normal, uvs) -> None:
        base = len(self.positions)
        self.positions.extend(corners)
        self.normals.extend([tuple(normal)] * 4)
        self.uvs.extend(uvs)
        self.faces.extend(((base, base + 1, base + 2), (base, base + 2, base + 3)))

    def top(self, x0: float, x1: float, y0: float, y1: float, z: float, tile: float,
            u_offset: float = 0.0, v_offset: float = 0.0) -> None:
        self.quad(((x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)), (0.0, 0.0, 1.0),
                  [(x0 / tile + u_offset, y0 / tile + v_offset),
                   (x1 / tile + u_offset, y0 / tile + v_offset),
                   (x1 / tile + u_offset, y1 / tile + v_offset),
                   (x0 / tile + u_offset, y1 / tile + v_offset)])

    def wall(self, ax: float, ay: float, bx: float, by: float, z0: float, z1: float,
             nx: float, ny: float, tile: float, u_offset: float = 0.0) -> None:
        """A vertical quad from base point a to b; ``(nx, ny)`` selects the facing side."""
        gx, gy = by - ay, -(bx - ax)  # cross(b - a, up): the quad's geometric normal
        if gx * nx + gy * ny < 0.0:
            ax, ay, bx, by = bx, by, ax, ay
        length = math.hypot(bx - ax, by - ay) or 1.0
        tx, ty = (bx - ax) / length, (by - ay) / length

        def uv(px: float, py: float, pz: float) -> tuple[float, float]:
            return (((px - ax) * tx + (py - ay) * ty) / tile + u_offset, pz / tile)

        self.quad(((ax, ay, z0), (bx, by, z0), (bx, by, z1), (ax, ay, z1)), (nx, ny, 0.0),
                  [uv(ax, ay, z0), uv(bx, by, z0), uv(bx, by, z1), uv(ax, ay, z1)])

    def box(self, cx: float, cy: float, z0: float, z1: float, sx: float, sy: float,
            tile: float, yaw: float = 0.0) -> None:
        """An axis-aligned box, optionally yawed about its centre (shelter glass and roofs)."""
        cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
        hx, hy = sx / 2.0, sy / 2.0
        corners = [(cx + dx * cos_yaw - dy * sin_yaw, cy + dx * sin_yaw + dy * cos_yaw)
                   for dx, dy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))]
        self.quad(((corners[0][0], corners[0][1], z1), (corners[1][0], corners[1][1], z1),
                   (corners[2][0], corners[2][1], z1), (corners[3][0], corners[3][1], z1)),
                  (0.0, 0.0, 1.0), [(x / tile, y / tile) for x, y in corners])
        for index in range(4):
            ax, ay = corners[index]
            bx, by = corners[(index + 1) % 4]
            nx, ny = by - ay, -(bx - ax)
            length = math.hypot(nx, ny) or 1.0
            self.wall(ax, ay, bx, by, z0, z1, nx / length, ny / length, tile)

    def build(self) -> SceneryMesh | None:
        if not self.faces:
            return None
        return SceneryMesh(self.name, self.material, tuple(self.positions), tuple(self.normals),
                           tuple(self.uvs), tuple(self.faces), tuple(self.proxies), self.texture)


def _tile_key(x: float, y: float) -> tuple[int, int]:
    return math.floor(x / TILE_M), math.floor(y / TILE_M)


def _clipped_rects(x0: float, x1: float, y0: float, y1: float):
    if x1 <= x0 or y1 <= y0:
        return
    ix0, ix1 = math.floor(x0 / TILE_M), math.floor((x1 - 1e-9) / TILE_M)
    iy0, iy1 = math.floor(y0 / TILE_M), math.floor((y1 - 1e-9) / TILE_M)
    for ix in range(ix0, ix1 + 1):
        for iy in range(iy0, iy1 + 1):
            yield ((ix, iy), max(x0, ix * TILE_M), min(x1, (ix + 1) * TILE_M),
                   max(y0, iy * TILE_M), min(y1, (iy + 1) * TILE_M))


def _store_rect(store: dict, prefix: str, material: str, texture: str | None, x0: float,
                x1: float, y0: float, y1: float, z: float,
                uv: Callable[[float, float], tuple[float, float]]) -> None:
    for key, cx0, cx1, cy0, cy1 in _clipped_rects(x0, x1, y0, y1):
        mesh = store.get(key)
        if mesh is None:
            mesh = _Mesh(f"{prefix}_{key[0]}_{key[1]}", material, texture)
            store[key] = mesh
        mesh.quad(((cx0, cy0, z), (cx1, cy0, z), (cx1, cy1, z), (cx0, cy1, z)), (0.0, 0.0, 1.0),
                  [uv(cx0, cy0), uv(cx1, cy0), uv(cx1, cy1), uv(cx0, cy1)])


def _first(meshes: list[SceneryMesh], mesh: _Mesh) -> None:
    built = mesh.build()
    if built is not None:
        meshes.append(built)


def _build_scenery(layout: UrbanLayout, *, textures: bool
                   ) -> tuple[list[SceneryInstance], list[SceneryMesh], dict[str, bytes]]:
    city = layout.city
    assert city is not None
    bank = scene_textures(layout.seed) if textures else {}
    instances: list[SceneryInstance] = []
    meshes: list[SceneryMesh] = []

    facade_tex = lambda style: f"facade_{style}" if textures else None
    facades: dict[tuple[tuple[int, int], str], _Mesh] = {}
    roofs: dict[tuple[int, int], _Mesh] = {}
    asphalt: dict[tuple[int, int], _Mesh] = {}
    zebra: dict[tuple[int, int], _Mesh] = {}
    pavement: dict[tuple[int, int], _Mesh] = {}
    plazas: dict[tuple[int, int], _Mesh] = {}
    lawns: dict[tuple[int, int], _Mesh] = {}
    paths: dict[tuple[int, int], _Mesh] = {}

    style_rng = random.Random(layout.seed ^ 0x5EED)
    styles = ("brick", "concrete", "glass", "stucco", "panel")
    for building in layout.buildings:
        style = style_rng.choices(styles, (3, 3, 1, 2, 1))[0]
        key = _tile_key(building.x, building.y)
        mesh = facades.get((key, style))
        if mesh is None:
            mesh = _Mesh(f"facade_{style}_{key[0]}_{key[1]}", f"facade_{style}", facade_tex(style))
            facades[(key, style)] = mesh
        x, y, sx, sy, h = building.x, building.y, building.sx, building.sy, building.height
        for ax, ay, bx, by, nx, ny in (
            (x - sx / 2, y + sy / 2, x - sx / 2, y - sy / 2, -1.0, 0.0),
            (x + sx / 2, y - sy / 2, x + sx / 2, y + sy / 2, 1.0, 0.0),
            (x - sx / 2, y - sy / 2, x + sx / 2, y - sy / 2, 0.0, -1.0),
            (x + sx / 2, y + sy / 2, x - sx / 2, y + sy / 2, 0.0, 1.0),
        ):
            mesh.wall(ax, ay, bx, by, 0.0, h, nx, ny, FACADE_TILE_M)
        mesh.proxies.append(ProxyShape("box", (x, y, h / 2.0, 0.0, 0.0, 0.0), (sx, sy, h)))

        roof_key = _tile_key(building.x, building.y)
        roof = roofs.get(roof_key)
        if roof is None:
            roof = _Mesh(f"roof_{roof_key[0]}_{roof_key[1]}", "roof_gravel",
                         "roof_gravel" if textures else None)
            roofs[roof_key] = roof
        roof.top(x - sx / 2, x + sx / 2, y - sy / 2, y + sy / 2, h, ROOF_TILE_M)
        for dx, dy, px, py in ((0.0, -sy / 2, sx, 0.25), (0.0, sy / 2, sx, 0.25),
                               (-sx / 2, 0.0, 0.25, sy), (sx / 2, 0.0, 0.25, sy)):
            roof.box(x + dx, y + dy, h, h + 0.45, px, py, ROOF_TILE_M)
        equipment_h = 1.2 if h > 18.0 else 0.9
        roof.box(x + sx * 0.22, y - sy * 0.18, h + 0.45, h + 0.45 + equipment_h, 1.4, 1.1,
                 ROOF_TILE_M)
        roof.proxies.append(ProxyShape("box", (x + sx * 0.22, y - sy * 0.18,
                                               h + 0.45 + equipment_h / 2.0, 0.0, 0.0, 0.0),
                                       (1.4, 1.1, equipment_h)))
        roof.box(x - sx * 0.24, y + sy * 0.2, h + 0.45, h + 0.45 + 0.8, 0.9, 0.9, ROOF_TILE_M)

    asphalt_tex = "asphalt_center_dashed" if textures else None
    pavement_tex = "pavers" if textures else None
    for x in layout.roads_x:
        half = _half_width(x, city.arterial_roads_x)
        _store_rect(asphalt, "asphalt", "asphalt_center_dashed", asphalt_tex,
                    x - half, x + half, -MAP_HALF_M, MAP_HALF_M, 0.035,
                    lambda px, py, x=x: ((px - x) / 8.0 + 0.5, py / 8.0))
        for side in (-1.0, 1.0):
            inner, outer = x + side * half, x + side * (half + SIDEWALK_WIDTH_M)
            _store_rect(pavement, "sidewalk", "pavers", pavement_tex, min(inner, outer),
                        max(inner, outer), -MAP_HALF_M, MAP_HALF_M, 0.06,
                        lambda px, py: (px / PAVE_TILE_M, py / PAVE_TILE_M))
    for y in layout.roads_y:
        half = _half_width(y, city.arterial_roads_y)
        _store_rect(asphalt, "asphalt", "asphalt_center_dashed", asphalt_tex,
                    -MAP_HALF_M, MAP_HALF_M, y - half, y + half, 0.035,
                    lambda px, py, y=y: (px / 8.0, (py - y) / 8.0 + 0.5))
        for side in (-1.0, 1.0):
            inner, outer = y + side * half, y + side * (half + SIDEWALK_WIDTH_M)
            _store_rect(pavement, "sidewalk", "pavers", pavement_tex, -MAP_HALF_M, MAP_HALF_M,
                        min(inner, outer), max(inner, outer), 0.06,
                        lambda px, py: (px / PAVE_TILE_M, py / PAVE_TILE_M))

    # curbs: vertical faces at both sidewalk inner edges, facing the carriageway
    rows = [(iy, iy * TILE_M, (iy + 1) * TILE_M)
            for iy in range(math.floor(-MAP_HALF_M / TILE_M), math.floor(MAP_HALF_M / TILE_M))]
    for x in layout.roads_x:
        half = _half_width(x, city.arterial_roads_x)
        for side in (-1.0, 1.0):
            inner = x + side * half
            for iy, cy0, cy1 in rows:
                mesh = pavement.get((math.floor(inner / TILE_M), iy))
                if mesh is None:
                    mesh = _Mesh(f"sidewalk_{math.floor(inner / TILE_M)}_{iy}", "pavers",
                                 pavement_tex)
                    pavement[(math.floor(inner / TILE_M), iy)] = mesh
                mesh.wall(inner, cy0, inner, cy1, 0.0, 0.06, -side, 0.0, PAVE_TILE_M)
    for y in layout.roads_y:
        half = _half_width(y, city.arterial_roads_y)
        for side in (-1.0, 1.0):
            inner = y + side * half
            for ix in range(math.floor(-MAP_HALF_M / TILE_M), math.floor(MAP_HALF_M / TILE_M)):
                cx0, cx1 = ix * TILE_M, (ix + 1) * TILE_M
                mesh = pavement.get((ix, math.floor(inner / TILE_M)))
                if mesh is None:
                    mesh = _Mesh(f"sidewalk_{ix}_{math.floor(inner / TILE_M)}", "pavers",
                                 pavement_tex)
                    pavement[(ix, math.floor(inner / TILE_M))] = mesh
                mesh.wall(cx0, inner, cx1, inner, 0.0, 0.06, 0.0, -side, PAVE_TILE_M)

    zebra_tex = "crosswalk" if textures else None
    for x in layout.roads_x:
        for y in layout.roads_y:
            extent = max(_half_width(x, city.arterial_roads_x),
                         _half_width(y, city.arterial_roads_y))
            offset = extent + CROSSWALK_ARM_M
            half_x = _half_width(x, city.arterial_roads_x)
            half_y = _half_width(y, city.arterial_roads_y)
            for side in (-1.0, 1.0):
                cy = y + side * offset
                _store_rect(zebra, "crosswalk", "crosswalk", zebra_tex, x - half_x - 0.9,
                            x + half_x + 0.9, cy - CROSSWALK_HALF_LENGTH_M,
                            cy + CROSSWALK_HALF_LENGTH_M, 0.05,
                            lambda px, py: (px / ZEBRA_PITCH_M, py / 1.0))
                cx = x + side * offset
                _store_rect(zebra, "crosswalk", "crosswalk", zebra_tex, cx - CROSSWALK_HALF_LENGTH_M,
                            cx + CROSSWALK_HALF_LENGTH_M, y - half_y - 0.9, y + half_y + 0.9, 0.05,
                            lambda px, py: (py / ZEBRA_PITCH_M, px / 1.0))

    plaza_tex = "plaza_stone" if textures else None
    for plaza in city.plazas:
        _store_rect(plazas, "plaza", "plaza_stone", plaza_tex,
                    plaza.x - plaza.half_size, plaza.x + plaza.half_size,
                    plaza.y - plaza.half_size, plaza.y + plaza.half_size, 0.03,
                    lambda px, py: (px / 2.4, py / 2.4))
    lawn_tex = "park_lawn" if textures else None
    path_tex = "park_path" if textures else None
    for park in city.parks:
        _store_rect(lawns, "lawn", "park_lawn", lawn_tex,
                    park.x - park.half_size, park.x + park.half_size,
                    park.y - park.half_size, park.y + park.half_size, 0.015,
                    lambda px, py: (px / 8.0, py / 8.0))
        _store_rect(paths, "path", "park_path", path_tex, park.x - park.half_size,
                    park.x + park.half_size, park.y - 1.1, park.y + 1.1, 0.03,
                    lambda px, py: (py / 2.4, px / 1.0))
        _store_rect(paths, "path", "park_path", path_tex, park.x - 1.1, park.x + 1.1,
                    park.y - park.half_size, park.y + park.half_size, 0.03,
                    lambda px, py: (px / 2.4, py / 1.0))

    for bench in layout.benches:
        instances.append(SceneryInstance("bench_a", bench.x, bench.y, 0.0, bench.yaw, 1.0))
    for tree in city.trees:
        asset = ASSETS["tree_oak" if tree.species == "oak" else "tree_pine"]
        instances.append(SceneryInstance(asset.key, tree.x, tree.y, 0.0, 0.0,
                                         round(tree.height / asset.native_height_m, 4)))
    for x, y, yaw in city.lamps:
        instances.append(SceneryInstance("lamp_post", x, y, 0.0, yaw, 1.0))

    from sim.worldgen.vehicles import vehicle_asset

    for car in layout.parked_cars:
        asset = vehicle_asset(car.type, car.variant)
        yaw = car.yaw + (math.pi / 2 if asset == "bus" else 0.0)
        instances.append(SceneryInstance(asset, car.x, car.y, 0.0, yaw, 1.0))

    for mesh in (*facades.values(), *roofs.values(), *asphalt.values(), *zebra.values(),
                 *pavement.values(), *plazas.values(), *lawns.values(), *paths.values()):
        _first(meshes, mesh)
    meshes.extend(_shelter_meshes(layout, textures))
    return instances, meshes, bank


def _shelter_meshes(layout: UrbanLayout, textures: bool) -> list[SceneryMesh]:
    city = layout.city
    assert city is not None
    tex_glass = "bus_shelter_glass" if textures else None
    tex_roof = "roof_membrane" if textures else None
    shelters: list[tuple[str, float, float, float, float, bool]] = [
        (f"bus_stop_shelter_{index + 1:02d}", stop.x, stop.y, stop.yaw, SHELTER_LENGTH_M, True)
        for index, stop in enumerate(city.bus_stops)
    ]
    shelters.append(("follow_shelter", layout.jogger_shelter_center[0],
                     layout.jogger_shelter_center[1], layout.follow_occluder_yaw,
                     layout.jogger_shelter_size[1], False))
    out: list[SceneryMesh] = []
    for name, cx, cy, yaw, length, platform in shelters:
        cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)

        def place(dx: float, dy: float, _cx=cx, _cy=cy, _c=cos_yaw, _s=sin_yaw):
            return (_cx + dx * _c - dy * _s, _cy + dx * _s + dy * _c)

        glass = _Mesh(f"{name}_glass", "bus_shelter_glass", tex_glass)
        roof = _Mesh(f"{name}_roof", "roof_membrane", tex_roof)
        panels = [(1.55, 0.0, 0.08, length - 0.16)]
        if platform:
            # Bus stops have closed ends; the follow occluder keeps them open so the jogger
            # runs through under the roof (the roof is what blocks the camera ray).
            panels += [(0.0, length / 2 - 0.04, 3.3, 0.08),
                       (0.0, -length / 2 + 0.04, 3.3, 0.08)]
        for dx, dy, sx, sy in panels:
            px, py = place(dx, dy)
            glass.box(px, py, 0.0, 2.2, sx, sy, PAVE_TILE_M, yaw)
            glass.proxies.append(ProxyShape("box", (px, py, 1.1, 0.0, 0.0, yaw), (sx, sy, 2.2)))
        px, py = place(-0.05, 0.0)
        roof.box(px, py, 2.2, 2.38, 3.5, length + 0.2, ROOF_TILE_M, yaw)
        roof.proxies.append(ProxyShape("box", (px, py, 2.29, 0.0, 0.0, yaw), (3.5, length + 0.2, 0.18)))
        for mesh in (glass, roof):
            _first(out, mesh)
        if platform:
            _platform_meshes(name, cx, cy, yaw, length, textures, out)
    return out


def _platform_meshes(name: str, cx: float, cy: float, yaw: float, length: float,
                     textures: bool, out: list[SceneryMesh]) -> None:
    """The paver platform the bus shelters stand on (block-side apron of the sidewalk)."""
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    corners = []
    for dx, dy in ((-1.55, -(length / 2 + 1.2)), (1.25, -(length / 2 + 1.2)),
                   (1.25, length / 2 + 1.2), (-1.55, length / 2 + 1.2)):
        corners.append((cx + dx * cos_yaw - dy * sin_yaw, cy + dx * sin_yaw + dy * cos_yaw))
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    for key, x0, x1, y0, y1 in _clipped_rects(min(xs), max(xs), min(ys), max(ys)):
        mesh = _Mesh(f"{name}_platform_{key[0]}_{key[1]}", "pavers",
                     "pavers" if textures else None)
        mesh.quad(((x0, y0, 0.06), (x1, y0, 0.06), (x1, y1, 0.06), (x0, y1, 0.06)),
                  (0.0, 0.0, 1.0), [(x0 / PAVE_TILE_M, y0 / PAVE_TILE_M),
                                    (x1 / PAVE_TILE_M, y0 / PAVE_TILE_M),
                                    (x1 / PAVE_TILE_M, y1 / PAVE_TILE_M),
                                    (x0 / PAVE_TILE_M, y1 / PAVE_TILE_M)])
        _first(out, mesh)


def build_urban_procedural(layout: UrbanLayout, world_dir: Path, *, textures: bool = True,
                           dressing: Dressing) -> tuple[str, list[dict], dict[str, bytes | str]]:
    from sim.worldgen.dressing import clutter_scenery
    from sim.worldgen.urban import vehicle_record

    city = layout.city
    assert city is not None
    env = dressing.environment
    world_name = f"horus_urban_p{layout.seed}"
    height = lambda x, y: 0.0  # noqa: E731

    # Agents first: clutter placement avoids every actor route, and its scenery pieces join
    # the same per-tile emission as roads, buildings and street furniture.
    agents: list[dict] = []
    people_models: list[str] = []
    for person in layout.people:
        record = dress_person(dressing, person_record(person))
        people_models.append(pedestrian_include(record))
        agents.append(record)
    files = person_files(dressing, agents, textures=textures)
    vehicle_models: list[str] = []
    for vehicle in layout.vehicles:
        record = vehicle_record(vehicle)
        vehicle_models.append(vehicle_model(vehicle.model, vehicle.type, vehicle.position[0],
                                            vehicle.position[1], 0.0, vehicle.yaw, static=False,
                                            variant=vehicle.variant))
        agents.append(record)
    animals = add_fauna(dressing, "urban", layout, agents, fauna_areas("urban", layout), height)
    animal_sdf = animal_models(animals, height)
    agents.extend(animals)
    add_clutter(dressing, "urban", layout, agents, height)  # placement only
    sample_events(dressing, agents, layout.seed)

    instances, meshes, bank = _build_scenery(layout, textures=textures)
    clutter_instances, clutter_meshes = clutter_scenery("urban", dressing.clutter)
    if not textures:
        clutter_meshes = [replace(mesh, texture=None) for mesh in clutter_meshes]
    instances.extend(clutter_instances)
    meshes.extend(clutter_meshes)
    bundle = emit_scenery(world_name, instances, meshes, tile_m=TILE_M, textures=bank,
                          stem=world_name)

    ground = (
        f'<collision name="terrain_collision"><pose>0 0 -0.5 0 0 0</pose><geometry>'
        f"<box><size>{2 * MAP_HALF_M} {2 * MAP_HALF_M} 1</size></box></geometry>"
        f"<surface><contact><collide_bitmask>{TERRAIN_COLLIDE_BITMASK:#06x}</collide_bitmask>"
        "</contact></surface></collision>"
        + visual("ground", 0, 0, -0.02, 0,
                 f"<plane><normal>0 0 1</normal><size>{2 * MAP_HALF_M} {2 * MAP_HALF_M}</size></plane>",
                 env.tint(SURFACE_COLOURS["urban_ground"], "ground"), cast_shadows=False)
    )
    models = [static_model("urban_ground", ground), bundle.sdf.rstrip("\n"),
              *people_models, *vehicle_models, *animal_sdf]
    pad = home_pad(world_dir, PAD_COLLIDE_BITMASK)
    drone = include(PROCEDURAL_DRONE_MODEL, "horus_iris", 0, 0, 0.195, math.pi / 2)
    world = (world_header(world_name, env) + "\n".join(models) + "\n" + pad + "\n" + drone
             + "\n  </world></sdf>\n")
    files.update(bundle.files)
    return world, agents, files


__all__ = ["build_urban_procedural", "sample_urban_layout"]
