from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import functools
import math
import random
from typing import Callable, Sequence, TypeAlias

from sim.worldgen.surface import GRID_SIDE, GROUND_CELL, MAP_MIN, TriangulatedGrid, quantize_rows

Point: TypeAlias = tuple[float, float]
Waypoint: TypeAlias = tuple[float, ...]


@dataclass(frozen=True)
class PersonPlacement:
    model: str
    role: str
    asset: str
    position: Point
    yaw: float = 0.0
    behavior: str = "idle"
    group: str = "ambient"
    waypoints: tuple[Waypoint, ...] = ()
    speed: float = 1.25
    mode: str = "loop"
    accel: float | None = None
    hold_s: float | None = None
    start_offset_s: float | None = None


# Urban carriageways are 9.5 m wide: one travel lane each way plus a kerbside parking lane.
DRIVE_LANE_OFFSET_M = 1.9
PARKING_LANE_OFFSET_M = 3.75
CARRIAGEWAY_HALF_WIDTH_M = 4.75
SIDEWALK_OFFSET_M = 5.65
LAMP_OFFSET_M = 6.85
CROSSWALK_OFFSET_M = 7.8
# Every recruited actor route and wander area stays inside this half-extent.
GEOFENCE_INSET_M = 60.0


def crosswalk_side(index: int) -> float:
    """Which side of the junction (along the crossed road) procedural crosswalk ``index`` uses."""
    return 1.0 if index % 2 else -1.0


def _on_sidewalk_or_road(point: tuple[float, float], roads_x, roads_y, margin: float) -> bool:
    """True when a point lies on any carriageway or sidewalk band (plus ``margin``)."""
    limit = SIDEWALK_OFFSET_M + 0.9 + margin
    return any(abs(point[0] - x) < limit for x in roads_x) or any(abs(point[1] - y) < limit for y in roads_y)
BIN_OFFSET_M = 7.05


@dataclass(frozen=True)
class VehiclePlacement:
    """A circulating vehicle; ``waypoints`` form a closed loop traversed in order."""

    model: str
    type: str
    position: Point
    yaw: float
    waypoints: tuple[Waypoint, ...]
    speed: float
    accel: float
    mode: str = "loop"
    start_offset_s: float = 0.0
    variant: int = 0

    @classmethod
    def loop(cls, model: str, vehicle_type: str, waypoints: tuple[Waypoint, ...],
             speed: float, accel: float, start_offset_s: float = 0.0,
             variant: int = 0) -> "VehiclePlacement":
        (x0, y0), (x1, y1) = waypoints[0][:2], waypoints[1][:2]
        return cls(model, vehicle_type, (x0, y0), math.atan2(y1 - y0, x1 - x0), waypoints,
                   speed, accel, "loop", start_offset_s, variant)


def block_loop(x0: float, x1: float, y0: float, y1: float, *, clockwise: bool,
               start_side: str, stop: tuple[float, float] | None = None,
               lane: float = DRIVE_LANE_OFFSET_M) -> tuple[Waypoint, ...]:
    """Right-hand-traffic lane loop around the block bounded by roads x0/x1 and y0/y1.

    Clockwise circulation keeps the vehicle on each road's block-side lane, anticlockwise on
    the far-side lane. The loop starts mid-way along ``start_side`` (north/south/east/west), or
    at ``stop = (coordinate along that side, dwell seconds)``.
    """
    inset = lane if clockwise else -lane
    west, east, south, north = x0 + inset, x1 - inset, y0 + inset, y1 - inset
    if clockwise:
        corners = {"west": (west, south), "north": (west, north),
                   "east": (east, north), "south": (east, south)}
        order = ("west", "north", "east", "south")
    else:
        corners = {"south": (west, south), "east": (east, south),
                   "north": (east, north), "west": (west, north)}
        order = ("south", "east", "north", "west")
    first = order.index(start_side)
    sides = order[first:] + order[:first]
    start_corner = corners[sides[0]]
    end_corner = corners[sides[1]]
    if stop is None:
        start: Waypoint = ((start_corner[0] + end_corner[0]) / 2, (start_corner[1] + end_corner[1]) / 2)
    elif start_side in ("north", "south"):
        start = (stop[0], start_corner[1], stop[1])
    else:
        start = (start_corner[0], stop[0], stop[1])
    return (start, *(corners[side] for side in sides[1:]), start_corner)


@dataclass(frozen=True)
class ForestLayout:
    seed: int
    canonical: bool
    trail: tuple[Point, ...]
    creek: tuple[Point, ...]
    clearing: Point
    clearing_radius: float
    camp: Point
    missing_hiker: PersonPlacement
    missing_canopy_tree: Point
    injured_person: PersonPlacement
    hiker_routes: tuple[PersonPlacement, ...]
    ranger_routes: tuple[PersonPlacement, ...]
    family_positions: tuple[PersonPlacement, ...]
    follow_hiker: PersonPlacement
    follow_occluder_xy: Point
    follow_occluder_name: str
    follow_speed: float
    follower_altitude_m: float
    follower_horizontal_m: float
    expected_occlusion_s: float
    tree_count: int
    tree_height_range: tuple[float, float]
    fallen_logs: tuple[tuple[float, float, float, float], ...]
    terrain_base: float
    terrain_amplitudes: tuple[float, float, float]
    terrain_phases: tuple[float, float, float]
    terrain_frequencies: tuple[float, float, float, float, float]
    attempts: int = 1
    follow_occluder_radius_m: float = 1.8
    follow_occluder_height_m: float = 2.3

    @property
    def people(self) -> tuple[PersonPlacement, ...]:
        return (*self.hiker_routes, *self.ranger_routes, *self.family_positions,
                self.missing_hiker, self.injured_person, self.follow_hiker)


@dataclass(frozen=True)
class BuildingSpec:
    x: float
    y: float
    sx: float
    sy: float
    height: float
    colour: tuple[float, float, float]


@dataclass(frozen=True)
class ParkTree:
    x: float
    y: float
    height: float


@dataclass(frozen=True)
class Bench:
    x: float
    y: float
    yaw: float


@dataclass(frozen=True)
class ParkedCar:
    x: float
    y: float
    yaw: float
    colour: tuple[float, float, float]
    type: str = "car"
    variant: int = 0


@dataclass(frozen=True)
class UrbanLayout:
    seed: int
    canonical: bool
    roads_x: tuple[float, ...]
    roads_y: tuple[float, ...]
    crosswalk_centres: tuple[Point, ...]
    buildings: tuple[BuildingSpec, ...]
    plaza_half_size: float
    park_center: Point
    park_half_size: float
    park_trees: tuple[ParkTree, ...]
    benches: tuple[Bench, ...]
    lamp_positions: tuple[Point, ...]
    utility_poles: tuple[Point, ...]
    trash_bins: tuple[Point, ...]
    bus_shelter_center: Point
    jogger_shelter_center: Point
    jogger_shelter_size: tuple[float, float]
    parked_cars: tuple[ParkedCar, ...]
    commuters: tuple[PersonPlacement, ...]
    crossings: tuple[PersonPlacement, ...]
    bus_waiting: tuple[PersonPlacement, ...]
    joggers: tuple[PersonPlacement, ...]
    follow_jogger: PersonPlacement
    park_sitting: tuple[PersonPlacement, ...]
    plaza_crowd: tuple[PersonPlacement, ...]
    red_shirt: PersonPlacement
    collapsed: PersonPlacement
    vehicles: tuple[VehiclePlacement, ...]
    follow_speed: float
    follower_altitude_m: float
    follower_horizontal_m: float
    expected_occlusion_s: float
    attempts: int = 1
    follow_occluder_kind: str = "shelter"
    follow_occluder_yaw: float = 0.0
    lamp_height_m: float = 8.6

    @property
    def people(self) -> tuple[PersonPlacement, ...]:
        return (*self.commuters, *self.crossings, *self.bus_waiting, *self.joggers,
                self.follow_jogger, *self.park_sitting, *self.plaza_crowd,
                *((self.red_shirt,) if self.canonical else ()), self.collapsed)


_FOREST_TRAIL = (
    (-69.0, -59.0), (-58.0, -47.0), (-48.0, -39.0), (-37.0, -32.0),
    (-27.0, -24.0), (-16.0, -18.0), (-10.0, -7.0), (-2.0, 3.0),
    (11.0, 13.0), (22.0, 19.0), (31.0, 25.0), (39.0, 35.0),
    (48.0, 48.0), (61.0, 63.0),
)
_FOREST_CREEK = (
    (-68.0, 50.0), (-55.0, 42.0), (-45.0, 33.0), (-35.0, 25.0),
    (-24.0, 17.0), (-13.0, 12.0), (-2.0, 11.0), (10.0, 7.0),
    (20.0, 0.0), (31.0, -6.0), (42.0, -15.0), (50.0, -27.0),
    (64.0, -39.0),
)


def _person(model: str, role: str, asset: str, xy: Point, yaw: float = 0.0,
            behavior: str = "idle", group: str = "ambient",
            route: tuple[Waypoint, ...] = (), speed: float = 1.25,
            mode: str = "loop", hold: float | None = None,
            offset: float | None = None) -> PersonPlacement:
    return PersonPlacement(model, role, asset, xy, yaw, behavior, group, route,
                           speed, mode, None, hold, offset)


def canonical_forest_layout(seed: int = 2027) -> ForestLayout:
    hiker_routes = (
        _person("civ_hiker_01", "hiker", "person_orange", (-58, -47), 0.6,
                "route", route=((-58, -47), (-42, -35), (-25, -23), (-10, -7),
                                (11, 13), (31, 25), (48, 48)), speed=1.25, mode="pingpong"),
        _person("civ_hiker_02", "hiker", "person_blue_kinematic", (-25, -23), 0.7,
                "route", route=((-25, -23), (-10, -7), (11, 13), (31, 25), (48, 48)),
                speed=1.0, mode="pingpong"),
        _person("civ_hiker_03", "hiker", "person_green_kinematic", (11, 13), 0.5,
                "route", route=((11, 13), (22, 19), (31, 25), (39, 35), (48, 48)),
                speed=1.15, mode="pingpong"),
    )
    ranger = (_person("civ_ranger_01", "ranger", "person_hivis", (-10, -7), 0.7,
                      "route", route=((-10, -7), (-25, -23), (-37, -32), (-48, -39),
                                      (-58, -47)), speed=1.0, mode="pingpong"),)
    family = (
        _person("civ_family_01", "family", "person_red_kinematic", (25.2, 35.4), 0.1,
                hold=0.0),
        _person("civ_family_02", "family", "person_blue_kinematic", (26.4, 37.2), -0.5),
        _person("civ_family_03", "family", "person_green_kinematic", (31.4, 36.2), 2.7),
        _person("civ_family_04", "family", "person_dark", (32.2, 33.6), 2.9),
    )
    follow_route = tuple(_FOREST_TRAIL[5:10])
    return ForestLayout(
        seed=seed, canonical=True, trail=_FOREST_TRAIL, creek=_FOREST_CREEK,
        clearing=(-7.0, 31.0), clearing_radius=17.0, camp=(29.0, 35.0),
        missing_hiker=_person("civ_missing_hiker_01", "missing_hiker",
                              "person_laying_kinematic", (-42.0, 20.0), 0.4),
        missing_canopy_tree=(-45.2, 20.5),
        # The injured hiker rests in the open clearing: the campaign's scan test measures
        # detection coverage over the clearing scan rectangle, and an empty meadow can never
        # score. (13, -43) sat under tree_conifer_canopy_collision_163 with no clear descent.
        injured_person=_person("civ_injured_01", "injured", "person_sitting",
                               (-6.0, 30.0), 1.2),
        hiker_routes=hiker_routes, ranger_routes=ranger, family_positions=family,
        follow_hiker=_person("civ_hiker_04", "hiker", "person_green_kinematic",
                             follow_route[0], 0.5, "route", "follow_hiker",
                             follow_route, 1.25, "once"),
        follow_occluder_xy=(-13.42, -16.164),
        follow_occluder_name="follow_occlusion_bush", follow_speed=1.25,
        # The old 6.8 m / 1.66 m geometry looked 77 deg down, outside the 84 deg / -45 deg
        # camera's frame, and could not be occluded without putting the bush in the follower
        # lane; 8 m / 3 m frames the target and lets the enlarged bush occlude the pursuit.
        follower_altitude_m=8.0, follower_horizontal_m=3.0,
        expected_occlusion_s=2.2, tree_count=280, tree_height_range=(10.0, 20.0),
        fallen_logs=((-43, 4, 5.4, 0.7), (4, -34, 4.6, -0.2),
                     (54, 12, 6.0, 1.1), (-14, 50, 4.2, -0.5),
                     (42, -49, 5.0, 0.4)),
        terrain_base=0.86, terrain_amplitudes=(0.42, 0.23, 0.17),
        terrain_phases=(0.4, 0.0, 0.0),
        terrain_frequencies=(0.043, 0.037, 0.081, 0.055, 0.019),
    )


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _distance_segment(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    denom = dx * dx + dy * dy
    t = 0.0 if denom == 0 else max(0.0, min(1.0,
        ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / denom))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def distance_to_path(p: Point, path: tuple[Point, ...] | list[Point]) -> float:
    return min(_distance_segment(p, a, b) for a, b in zip(path, path[1:]))


def forest_height(x: float, y: float, layout: ForestLayout) -> float:
    radius = math.hypot(x, y)
    if radius <= 4.0:
        return 0.0
    ax, ay, by, diagonal, cross = layout.terrain_frequencies
    amp0, amp1, amp2 = layout.terrain_amplitudes
    phase0, phase1, phase2 = layout.terrain_phases
    height = (
        layout.terrain_base
        + amp0 * math.sin(x * ax + phase0) * math.cos(y * ay)
        + amp1 * math.sin(y * by + x * cross + phase1)
        + amp2 * math.cos((x + y) * diagonal + phase2)
    )
    creek_distance = distance_to_path((x, y), layout.creek)
    height -= 0.72 * math.exp(-((creek_distance / 5.3) ** 2))
    # Flatten the clearing with a smooth 4 m transition band; a hard edge left a 0.56–0.67 m
    # step in the terrain that the 2 m mesh rendered as a ramp under actors' feet.
    clearing_distance = math.hypot(x - layout.clearing[0], y - layout.clearing[1])
    flatten = max(0.0, min(1.0, (layout.clearing_radius + 2.0 - clearing_distance) / 4.0))
    flatten = flatten * flatten * (3.0 - 2.0 * flatten)
    height = height * (1.0 - 0.78 * flatten) + 0.43 * 0.78 * flatten
    blend = max(0.0, min(1.0, (radius - 4.0) / 10.0))
    blend = blend * blend * (3.0 - 2.0 * blend)
    value = max(-0.2, height * blend)
    return value if layout.canonical else max(-0.2, min(2.0, value))


@functools.lru_cache(maxsize=8)
def forest_ground_rows(layout: ForestLayout) -> tuple[tuple[float, ...], ...]:
    """The exported terrain height grid (rows along +y), quantized like the COLLADA mesh."""
    return tuple(tuple(row) for row in quantize_rows(
        [[forest_height(MAP_MIN + col * GROUND_CELL, MAP_MIN + row * GROUND_CELL, layout)
          for col in range(GRID_SIDE)] for row in range(GRID_SIDE)]))


def forest_surface(layout: ForestLayout) -> TriangulatedGrid:
    """The physical/rendered forest ground that actors stand on (see ``sim.worldgen.surface``)."""
    return TriangulatedGrid(forest_ground_rows(layout), MAP_MIN, MAP_MIN, GROUND_CELL)


CLEARING_SCAN_ALTITUDE_M = 10.0
# The scan needs 2.0 m clearance; the extra 0.6 m drone radius and sampling slack are folded
# into this margin so canopies closer than this can put a scan leg inside its safety margin.
CLEARING_SCAN_MARGIN_M = 3.0
# People the forest planner must frame from a vantage: no canopy may cover their descent
# column, and every walker route keeps body clearance from canopy envelopes.
FOREST_VANTAGE_ROLES = ("missing_hiker", "injured")


def clearing_scan_rectangle(centre: Point, clearing_radius: float) -> tuple[float, float, float]:
    """(centre x, centre y, half extent) of the largest clearing scan rectangle; mirrors the
    campaign scan plan's square inscribed in the clearing circle."""
    return centre[0], centre[1], min(12.0, clearing_radius / math.sqrt(2.0) - 1.0)


def clears_clearing_scan(x: float, y: float, z_low: float, z_high: float, radius: float,
                         centre: Point, clearing_radius: float) -> bool:
    """True when a canopy (vertical span ``z_low..z_high`` at ``x, y`` with horizontal
    ``radius``) keeps the scan margin from the clearing raster at ``CLEARING_SCAN_ALTITUDE_M``."""
    centre_x, centre_y, half = clearing_scan_rectangle(centre, clearing_radius)
    dx = max(abs(x - centre_x) - half, 0.0)
    dy = max(abs(y - centre_y) - half, 0.0)
    vertical = max(0.0, z_low - CLEARING_SCAN_ALTITUDE_M, CLEARING_SCAN_ALTITUDE_M - z_high)
    return math.hypot(dx, dy, vertical) - radius >= CLEARING_SCAN_MARGIN_M


def clears_actor_columns(x: float, y: float, z_low: float, z_high: float, radius: float,
                         columns: Sequence[tuple[float, ...]], margin: float) -> bool:
    """True when a canopy envelope (vertical span ``z_low..z_high`` at ``x, y`` with horizontal
    ``radius``) keeps ``margin`` from every actor column in ``columns``.

    A column is ``(x, y)`` (body/route clearance) or ``(x, y, torso_z)``, in which case it
    extends upward from ``torso_z``: the descent corridor a vantage must use. Canopies sit
    above every torso, so a footprint that covers a column blocks it.
    """
    for column in columns:
        px, py = column[0], column[1]
        horizontal = math.hypot(px - x, py - y)
        if len(column) < 3 or z_high >= column[2]:
            if horizontal - radius < margin:
                return False
        elif math.hypot(horizontal, column[2] - z_high) - radius < margin:
            return False
    return True


def clears_actor_routes(x: float, y: float, radius: float, people: Sequence[Any],
                        margin: float) -> bool:
    """True when a canopy footprint at ``(x, y)`` with ``radius`` keeps ``margin`` from every
    person's position and from every route segment they execute (not just its waypoints)."""
    for person in people:
        px, py = person.position
        if math.hypot(px - x, py - y) - radius < margin:
            return False
        if person.waypoints and distance_to_path((x, y), person.waypoints) - radius < margin:
            return False
    return True


def forest_follow_occlusion(layout: ForestLayout, *, step_m: float = 0.01
                            ) -> tuple[float, float]:
    """Return the longest LOS break and closest follower-lane clearance."""
    path = layout.follow_hiker.waypoints
    height = forest_surface(layout).height
    path_length = _path_length(path)
    ox, oy = layout.follow_occluder_xy
    center = (ox, oy, height(ox, oy) + layout.follow_occluder_height_m)
    radius = layout.follow_occluder_radius_m
    longest = run = 0
    lane_clearance = math.inf
    for index in range(math.ceil(path_length / step_m) + 1):
        distance = min(path_length, index * step_m)
        (x, y), direction = _point_at_distance(path, distance)
        fx = x - layout.follower_horizontal_m * direction[0]
        fy = y - layout.follower_horizontal_m * direction[1]
        eye = (fx, fy, height(fx, fy) + layout.follower_altitude_m)
        target = (x, y, height(x, y) + 1.1)
        ray = tuple(target[axis] - eye[axis] for axis in range(3))
        to_center = tuple(center[axis] - eye[axis] for axis in range(3))
        fraction = max(0.0, min(1.0, sum(to_center[axis] * ray[axis] for axis in range(3))
                                 / sum(value * value for value in ray)))
        ray_distance = math.sqrt(sum(
            (center[axis] - eye[axis] - fraction * ray[axis]) ** 2 for axis in range(3)))
        if ray_distance <= radius:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
        lane_distance = math.sqrt(sum((center[axis] - eye[axis]) ** 2 for axis in range(3)))
        lane_clearance = min(lane_clearance, lane_distance - radius)
    duration_s = max(0, longest - 1) * step_m / layout.follow_speed
    return duration_s, lane_clearance


def _catmull_rom(points: list[Point], subdivisions: int = 3) -> tuple[Point, ...]:
    result: list[Point] = []
    extended = [points[0], *points, points[-1]]
    for i in range(1, len(extended) - 2):
        p0, p1, p2, p3 = extended[i - 1:i + 3]
        for step in range(subdivisions):
            t = step / subdivisions
            t2, t3 = t * t, t * t * t
            values = []
            for axis in range(2):
                values.append(0.5 * ((2 * p1[axis]) + (-p0[axis] + p2[axis]) * t
                    + (2 * p0[axis] - 5 * p1[axis] + 4 * p2[axis] - p3[axis]) * t2
                    + (-p0[axis] + 3 * p1[axis] - 3 * p2[axis] + p3[axis]) * t3))
            result.append((values[0], values[1]))
    result.append(points[-1])
    return tuple(result)


def _path_length(path: tuple[Point, ...]) -> float:
    return sum(_distance(a, b) for a, b in zip(path, path[1:]))


def _point_at_distance(path: tuple[Point, ...], distance: float) -> tuple[Point, Point]:
    travelled = 0.0
    for a, b in zip(path, path[1:]):
        segment = _distance(a, b)
        if travelled + segment >= distance:
            t = (distance - travelled) / segment if segment else 0.0
            point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            direction = ((b[0] - a[0]) / segment, (b[1] - a[1]) / segment)
            return point, direction
        travelled += segment
    a, b = path[-2:]
    length = _distance(a, b)
    return b, ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _route_slice(path: tuple[Point, ...], start_distance: float, length: float) -> tuple[Point, ...]:
    end = start_distance + length
    selected = [_point_at_distance(path, start_distance)[0]]
    travelled = 0.0
    for index, point in enumerate(path[1:-1], start=1):
        travelled += _distance(path[index - 1], point)
        if start_distance < travelled < end:
            selected.append(point)
    selected.append(_point_at_distance(path, end)[0])
    return tuple(selected)


def sample_forest_layout(seed: int, *,
                         validate: Callable[[ForestLayout], float | None] | None = None
                         ) -> ForestLayout:
    rng = random.Random(seed)
    if validate is None:
        from sim.worldgen import _validate_candidate

        validate = lambda layout: _validate_candidate("forest", layout)
    rejections: Counter[str] = Counter()
    for attempt in range(1, 101):
        start = (-66.0, rng.uniform(-58, -48))
        near_home = (rng.uniform(-8, 8), rng.uniform(-8, 8))
        end = (66.0, rng.uniform(48, 60))
        controls = [start]
        for fraction in (0.22, 0.42, 0.61, 0.80):
            x = start[0] + (end[0] - start[0]) * fraction
            y = start[1] + (end[1] - start[1]) * fraction + rng.uniform(-9, 9)
            if abs(x) < 14:
                y = near_home[1] + rng.uniform(-2, 2)
            controls.append((x, y))
        controls.insert(3, near_home)
        trail = _catmull_rom(controls, 2)
        if distance_to_path((0.0, 0.0), trail) > 15 or any(
                abs(x) > 68 or abs(y) > 68 for x, y in trail):
            rejections["trail geometry"] += 1
            continue
        creek_controls = [(-67.0, rng.uniform(42, 56)),
                          (-34.0, rng.uniform(36, 55)),
                          (5.0, rng.uniform(30, 50)),
                          (38.0, rng.uniform(34, 56)),
                          (67.0, rng.uniform(40, 57))]
        creek = _catmull_rom(creek_controls, 2)
        if distance_to_path((0.0, 0.0), creek) < 14:
            rejections["creek placement"] += 1
            continue
        clear = None
        for _ in range(200):
            candidate = (rng.uniform(-30, 30), rng.uniform(-40, 48))
            if (distance_to_path(candidate, trail) >= 14 and distance_to_path(candidate, creek) >= 10
                    and _distance(candidate, (0, 0)) >= 16):
                clear = candidate
                break
        if clear is None:
            rejections["clearing placement"] += 1
            continue
        camp = None
        for _ in range(200):
            candidate = (rng.uniform(-42, 45), rng.uniform(-42, 50))
            if (distance_to_path(candidate, trail) >= 14 and distance_to_path(candidate, creek) >= 10
                    and _distance(candidate, (0, 0)) >= 16 and _distance(candidate, clear) >= 28):
                camp = candidate
                break
        if camp is None:
            rejections["camp placement"] += 1
            continue
        nearest = min(range(len(trail)), key=lambda i: _distance(trail[i], (0, 0)))
        start_i = max(0, nearest - 2)
        follow = trail[start_i:]
        if len(follow) < 3 or _path_length(follow) < 48:
            rejections["follow route too short"] += 1
            continue
        follow_start = follow[0]
        if _distance(follow_start, (0, 0)) > 45:
            rejections["follow route too short"] += 1
            continue
        follow_length = min(52.0, _path_length(follow))
        follow_route = _route_slice(follow, 0, follow_length)
        if _path_length(follow_route) < 40:
            rejections["follow route too short"] += 1
            continue
        # A low, compact shrub interrupts the descending camera ray, while its top stays
        # well below the high follower lane.
        target_distance = _path_length(follow_route) * rng.uniform(0.40, 0.65)
        occluder_distance = max(0.0, target_distance - 3.5)
        occ, direction = _point_at_distance(follow_route, occluder_distance)
        normal = (-direction[1], direction[0])
        side = rng.choice((-1.0, 1.0)) * rng.uniform(0.3, 0.95)
        occluder = (occ[0] + normal[0] * side, occ[1] + normal[1] * side)
        missing_at = min(_path_length(trail) - 5.0, _path_length(trail) * rng.uniform(0.30, 0.55))
        near, direction = _point_at_distance(trail, missing_at)
        normal = (-direction[1], direction[0])
        offset = rng.uniform(7.0, 12.5) * rng.choice((-1.0, 1.0))
        missing_xy = (near[0] + normal[0] * offset, near[1] + normal[1] * offset)
        canopy_dist = rng.uniform(1.8, 3.0)
        canopy_xy = (missing_xy[0] + normal[0] * math.copysign(canopy_dist, offset),
                     missing_xy[1] + normal[1] * math.copysign(canopy_dist, offset))
        if distance_to_path(canopy_xy, trail) < 5.5 or _distance(canopy_xy, (0, 0)) < 7.8:
            continue
        # The injured hiker rests in the open clearing so the campaign's scan test has a person
        # of interest inside its rectangle (an empty meadow can never score detection coverage).
        injured_xy = (clear[0] + rng.uniform(-3.5, 3.5), clear[1] + rng.uniform(-3.5, 3.5))
        # Route users follow compact trail segments and stay inside the geofence.
        hiker_segments = []
        for index, speed in enumerate((1.25, 1.0, 1.15), start=1):
            first = min(len(trail) - 3, nearest + (index - 2) * 4)
            route = tuple(trail[first:first + 7])
            if len(route) >= 2:
                hiker_segments.append(_person(
                    f"civ_hiker_{index:02d}", "hiker",
                    ("person_orange", "person_blue_kinematic", "person_green_kinematic")[index - 1],
                    route[0], 0.0, "route", route=route, speed=speed, mode="pingpong"))
        ranger_route = tuple(reversed([p for p in trail
                                       if abs(p[0]) <= 58 and abs(p[1]) <= 58][:8]))
        family = tuple(_person(f"civ_family_{i:02d}", "family", asset,
                               (camp[0] + dx, camp[1] + dy), yaw)
                       for i, (asset, dx, dy, yaw) in enumerate((
                           ("person_red_kinematic", -2.0, -1.2, 0.0),
                           ("person_blue_kinematic", -1.0, 1.2, 0.7),
                           ("person_green_kinematic", 1.3, 1.2, 2.1),
                           ("person_dark", 2.0, -1.0, 2.8)), 1))
        follow_speed = 1.25
        follow_person = _person("civ_hiker_04", "hiker", "person_green_kinematic",
                                follow_route[0], 0.0, "route", "follow_hiker",
                                follow_route, follow_speed, "once")
        clothing = rng.choice(("orange", "dark"))
        missing_asset = ("person_laying_kinematic" if clothing == "orange"
                         else "person_laying_dark_kinematic")
        missing = _person("civ_missing_hiker_01", "missing_hiker", missing_asset,
                          missing_xy, rng.uniform(-math.pi, math.pi))
        injured = _person("civ_injured_01", "injured", "person_sitting",
                          injured_xy, rng.uniform(-math.pi, math.pi))
        layout = ForestLayout(
            seed, False, trail, creek, clear, rng.uniform(13, 19), camp, missing,
            canopy_xy, injured, tuple(hiker_segments),
            (_person("civ_ranger_01", "ranger", "person_hivis", ranger_route[0],
                     0.0, "route", route=ranger_route, speed=1.0, mode="pingpong"),),
            family, follow_person, occluder, "follow_occlusion_bush", follow_speed,
            9.0, 7.0, 3.0,
            rng.randint(185, 300), (8.0, 22.0), (),
            rng.uniform(0.65, 1.05),
            (rng.uniform(0.25, 0.55), rng.uniform(0.12, 0.34), rng.uniform(0.08, 0.25)),
            (rng.uniform(-math.pi, math.pi), rng.uniform(-math.pi, math.pi),
             rng.uniform(-math.pi, math.pi)),
            (rng.uniform(0.035, 0.05), rng.uniform(0.03, 0.045),
             rng.uniform(0.07, 0.095), rng.uniform(0.045, 0.065),
             rng.uniform(0.012, 0.025)), attempt,
            follow_occluder_radius_m=1.6, follow_occluder_height_m=4.0)
        # The missing hiker's canopy tree is placed outside the tree loop; reject layouts
        # where its canopy would hang over the clearing scan corridor.
        canopy_h = max(8.0, min(22.0, (canopy_xy[0] + canopy_xy[1]) % 14 + 8.0))
        canopy_base = forest_surface(layout).height(*canopy_xy)
        canopy_z = canopy_base + canopy_h * 0.72
        if not clears_clearing_scan(canopy_xy[0], canopy_xy[1], canopy_z - 4.8, canopy_z + 4.8,
                                    4.8, layout.clearing, layout.clearing_radius):
            rejections["missing tree over clearing scan"] += 1
            continue
        people = layout.people
        all_points = [p.position for p in people]
        all_points.extend(point for person in people for point in person.waypoints)
        if any(abs(x) > GEOFENCE_INSET_M or abs(y) > GEOFENCE_INSET_M for x, y in all_points):
            rejections["geofence"] += 1
            continue
        if not (6 <= distance_to_path(missing_xy, trail) <= 15):
            rejections["missing hiker offset"] += 1
            continue
        if _distance(occluder, follow_start) < 8:
            rejections["occluder too close to follow start"] += 1
            continue
        occlusion_s, lane_clearance = forest_follow_occlusion(layout)
        if not 2.5 <= occlusion_s <= 4.0:
            rejections["follow occlusion outside 2.5-4.0 s"] += 1
            continue
        if lane_clearance < 2.6:
            rejections["follow lane clearance"] += 1
            continue
        accepted_occlusion = validate(layout)
        if accepted_occlusion is None:
            from sim.worldgen.validation import LAST_FAILURES

            rejections["validation"] += 1
            if LAST_FAILURES:
                rejections[f"validation: {LAST_FAILURES[0][:80]}"] += 1
            continue
        return replace(layout, expected_occlusion_s=accepted_occlusion)
    top = ", ".join(f"{reason} x{count}" for reason, count in rejections.most_common(4))
    raise RuntimeError(f"could not sample valid forest layout for seed {seed} after "
                       f"{sum(rejections.values())} attempts; top rejections: {top}")


def _road_grid(rng: random.Random) -> tuple[tuple[float, ...], tuple[float, ...]]:
    def axis() -> tuple[float, ...]:
        for _ in range(100):
            count = rng.randint(3, 5)
            high = 17.2 if count == 5 else 20.0
            left, right = -rng.uniform(16.5, high), rng.uniform(16.5, high)
            if not 33.0 <= right - left <= 40.0:
                continue
            extras = count - 2
            if extras == 1:
                sides = [rng.choice((-1, 1))]
            elif extras == 2:
                sides = [-1, 1]
                rng.shuffle(sides)
            else:
                repeated = rng.choice((-1, 1))
                sides = [repeated, repeated, -repeated]
                rng.shuffle(sides)
            values = [left, right]
            for index, side in enumerate(sides):
                same_side_remaining = sum(item == side for item in sides[index + 1:])
                current = min((v for v in values if v < 0), default=left) if side < 0 else max(v for v in values if v > 0)
                room = 69.0 - abs(current) - 26.0 * same_side_remaining
                if room < 26.0:
                    break
                gap = rng.uniform(26.0, min(40.0, room))
                values.append(current - gap if side < 0 else current + gap)
            else:
                values.sort()
                mirror = tuple(-v for v in reversed(values))
                if (any(abs(v) < 12.0 or abs(v) > 69.0 for v in values)
                        or any(not 26.0 <= b - a <= 40.0 for a, b in zip(values, values[1:]))
                        or max(abs(a - b) for a, b in zip(values, mirror)) < 1.0):
                    continue
                return tuple(values)
        raise ValueError("could not sample a non-symmetric 3–5 road axis")
    return axis(), axis()




HOME_PAD_KEEPOUT_M = 2.0


def _off_home_pad(point: Point) -> Point:
    """Push a standing spot radially out of the drone's take-off/landing pad keep-out."""
    distance = math.hypot(*point)
    if distance >= HOME_PAD_KEEPOUT_M:
        return point
    angle = math.atan2(point[1], point[0]) if distance > 1e-9 else 0.0
    return (HOME_PAD_KEEPOUT_M * math.cos(angle), HOME_PAD_KEEPOUT_M * math.sin(angle))


def _urban_people(roads_x: tuple[float, ...], roads_y: tuple[float, ...],
                  crosswalks: tuple[Point, ...], plaza_half: float, park_center: Point,
                  park_half: float, bus_center: Point, rng: random.Random,
                  follow_route: tuple[Waypoint, ...]
                  ) -> tuple[tuple[PersonPlacement, ...], ...]:
    sidewalk = 5.65
    safe_x = tuple(x for x in roads_x if abs(x) <= 54.0)
    safe_y = tuple(y for y in roads_y if abs(y) <= 54.0)
    assets = ("person_blue_kinematic", "person_green_kinematic", "person_dark", "person_orange", "person_hivis")

    def side_route(axis: str, start: float, end: float, road: float, side: float):
        if axis == "x":
            lane = road + side * sidewalk
            return ((lane, start), (lane, end))
        lane = road + side * sidewalk
        return ((start, lane), (end, lane))

    commuters = []
    for index in range(rng.randint(7, 10)):
        axis = rng.choice(("x", "y"))
        roads = safe_x if axis == "x" else safe_y
        road, side = rng.choice(roads), rng.choice((-1.0, 1.0))
        start = rng.uniform(-54.0, 54.0)
        direction = rng.choice((-1.0, 1.0))
        end = max(-56.0, min(56.0, start + direction * rng.uniform(24.0, 48.0)))
        if abs(end - start) < 22.0:
            start, end = end, start
        route = side_route(axis, start, end, road, side)
        yaw = math.atan2(route[1][1] - route[0][1], route[1][0] - route[0][0])
        commuters.append(_person(f"civ_commuter_{index + 1:02d}", "commuter",
                                 rng.choice(assets), route[0], yaw, "route",
                                 route=route, speed=rng.uniform(1.1, 1.6), mode="pingpong"))

    crossings = []
    for index, (cx, cy) in enumerate(crosswalks, 1):
        direction = rng.choice((-1.0, 1.0))
        # Zebra crossing over the vertical road just beyond the junction (not through it),
        # from one sidewalk to the other.
        lane_y = cy + crosswalk_side(index) * CROSSWALK_OFFSET_M
        start, end = (cx - SIDEWALK_OFFSET_M, lane_y), (cx + SIDEWALK_OFFSET_M, lane_y)
        if direction < 0:
            start, end = end, start
        crossings.append(_person(f"civ_crossing_{index:02d}", "crossing",
                                 rng.choice(assets), start, 0.0, "route",
                                 route=(start, end), speed=1.0, mode="pingpong",
                                 offset=rng.choice((0.0, 12.0, 20.0))))

    bus_waiting = tuple(_person(
        f"civ_bus_waiting_{i:02d}", "bus_waiting", rng.choice(assets),
        # Standing on the open side of the shelter, in front of its bench.
        (bus_center[0] + rng.uniform(-1.4, 1.4), bus_center[1] + 0.45 + (rng.uniform(0.1, 0.7) - 0.1) * 0.6),
        rng.uniform(-math.pi, math.pi))
        for i in range(1, rng.randint(3, 5) + 1))

    joggers = []
    for index in range(2):
        axis = rng.choice(("x", "y"))
        roads = safe_x if axis == "x" else safe_y
        road, side = rng.choice(roads), rng.choice((-1.0, 1.0))
        start = rng.uniform(-52.0, 5.0)
        end = min(54.0, start + rng.uniform(24.0, 42.0))
        route = side_route(axis, start, end, road, side)
        yaw = math.atan2(route[1][1] - route[0][1], route[1][0] - route[0][0])
        joggers.append(_person(f"civ_jogger_{index + 1:02d}", "jogger",
                               rng.choice(assets), route[0], yaw, "route",
                               route=route, speed=2.5, mode="pingpong"))
    follow_yaw = math.atan2(follow_route[1][1] - follow_route[0][1],
                            follow_route[1][0] - follow_route[0][0])
    follow = _person("civ_jogger_03", "jogger", "person_red_kinematic",
                     follow_route[0], follow_yaw, "route", "follow_jogger",
                     follow_route, 2.5, "once")

    park_sitting = tuple(_person(
        f"civ_park_sitting_{i:02d}", "park_sitting", "person_sitting",
        (park_center[0] + rng.uniform(-park_half + 1.5, park_half - 1.5),
         park_center[1] + rng.uniform(-park_half + 1.5, park_half - 1.5)),
        rng.uniform(-math.pi, math.pi))
        for i in range(1, rng.randint(2, 4) + 1))

    crowd_count = rng.randint(8, 16)
    edge = plaza_half - 1.0
    red_index = rng.randrange(crowd_count)
    non_red_indices = [index for index in range(crowd_count) if index != red_index]
    plaza_crowd = []
    red_shirt = None
    for index in range(crowd_count):
        position = _off_home_pad((rng.uniform(-edge, edge), rng.uniform(-edge, edge)))
        yaw = rng.uniform(-math.pi, math.pi)
        model = f"civ_plaza_crowd_{index + 1:02d}"
        if index == red_index:
            red_shirt = _person("civ_red_shirt_01", "red_shirt", "person_red_kinematic",
                                position, yaw)
            person = red_shirt
        else:
            if index == non_red_indices[0]:
                asset = "person_orange"
            elif index == non_red_indices[-1]:
                asset = "person_hivis"
            else:
                asset = rng.choice(assets)
            person = _person(model, "plaza_crowd", asset, position, yaw)
        plaza_crowd.append(person)

    collapsed = None
    for _ in range(200):
        axis = rng.choice(("x", "y"))
        roads = safe_x if axis == "x" else safe_y
        road, side = rng.choice(roads), rng.choice((-1.0, 1.0))
        along = rng.uniform(-54.0, 54.0)
        point = ((road + side * sidewalk, along) if axis == "x"
                 else (along, road + side * sidewalk))
        distance = math.hypot(*point)
        if 15.0 <= distance <= 45.0:
            collapsed = _person("civ_collapsed_01", "collapsed",
                                "person_collapsed_kinematic", point,
                                rng.uniform(-math.pi, math.pi))
            break
    if collapsed is None:
        raise ValueError("could not sample a sidewalk collapse location")
    return (tuple(commuters), tuple(crossings), bus_waiting, tuple(joggers), (follow,),
            park_sitting, tuple(plaza_crowd), red_shirt, collapsed)


def canonical_urban_layout(seed: int = 2028) -> UrbanLayout:
    roads = (-48.0, -16.0, 16.0, 48.0)
    buildings = tuple(BuildingSpec(*item) for item in (
        (-33, -33, 14, 12, 17, (0.66, 0.57, 0.48)), (-25, -30, 9, 13, 10, (0.69, 0.64, 0.56)),
        (-2, -32, 13, 14, 24, (0.58, 0.62, 0.65)), (7, -28, 8, 12, 12, (0.73, 0.69, 0.60)),
        (31, -31, 13, 15, 21, (0.64, 0.56, 0.49)), (37, -29, 8, 13, 14, (0.70, 0.70, 0.66)),
        (-34, 3, 14, 13, 28, (0.58, 0.61, 0.63)), (-26, 4, 8, 11, 13, (0.70, 0.61, 0.52)),
        (-33, 34, 16, 13, 31, (0.65, 0.62, 0.56)), (-24, 31, 8, 13, 16, (0.56, 0.63, 0.67)),
        (-3, 32, 14, 13, 20, (0.70, 0.64, 0.55)), (6, 34, 9, 12, 15, (0.63, 0.66, 0.62)),
        (29, 33, 14, 14, 26, (0.58, 0.61, 0.66)), (38, 34, 8, 12, 12, (0.71, 0.66, 0.58)),
        (-34, -5, 15, 9, 12, (0.62, 0.67, 0.68)), (31, -4, 12, 9, 15, (0.67, 0.64, 0.59)),
    ))
    park_trees = tuple(ParkTree(x, y, 6.5 + (i % 3) * 1.1) for i, (x, y) in enumerate(
        ((26, -5), (31, -6), (37, -4), (25, 2), (37, 3), (29, 6), (35, 7), (24, 7))))
    benches = (Bench(28, -1, 0), Bench(34, 1.5, math.pi / 2), Bench(29, 4, 0))
    crosswalks = ((-16.0, 16.0), (16.0, -16.0), (48.0, 16.0), (-48.0, -16.0))
    commuters = (
        _person("civ_commuter_01", "commuter", "person_blue_kinematic", (-42, -9), 1.57, "route", route=((-42, -9), (-42, 7), (-42, 24), (-42, 40)), mode="pingpong"),
        _person("civ_commuter_02", "commuter", "person_green_kinematic", (-42, 8), -1.57, "route", route=((-42, 8), (-42, -5), (-42, -27)), mode="pingpong"),
        _person("civ_commuter_03", "commuter", "person_dark", (-8, 43), 0.0, "route", route=((-8, 43), (8, 43), (27, 43), (40, 43)), mode="pingpong"),
        _person("civ_commuter_04", "commuter", "person_orange", (40, 43), 3.14, "route", route=((40, 43), (23, 43), (6, 43), (-8, 43)), mode="pingpong"),
        _person("civ_commuter_05", "commuter", "person_dark", (-42, -40), 1.57, "route", route=((-42, -40), (-42, -20), (-42, -5)), mode="pingpong"),
        _person("civ_commuter_06", "commuter", "person_blue_kinematic", (-42, 38), -1.57, "route", route=((-42, 38), (-42, 25), (-42, 12)), mode="pingpong"),
        _person("civ_commuter_07", "commuter", "person_green_kinematic", (6, -42), 3.14, "route", route=((6, -42), (-11, -42), (-29, -42), (-41, -42)), mode="pingpong"),
        _person("civ_commuter_08", "commuter", "person_dark", (40, -42), 0.0, "route", route=((40, -42), (25, -42), (7, -42), (-7, -42)), mode="pingpong"),
        _person("civ_commuter_09", "commuter", "person_blue_kinematic", (12, 8), 1.57, "route", route=((12, 8), (12, 24), (12, 40)), mode="pingpong"),
        _person("civ_commuter_10", "commuter", "person_green_kinematic", (-8, -8), 0.0, "route", route=((-8, -8), (8, -8), (18, -8)), mode="pingpong"),
    )
    crossings = (
        _person("civ_crossing_01", "crossing", "person_orange", (-21.65, 16), 0, "route", route=((-21.65, 16, 10), (-10.35, 16, 12)), speed=1.0, mode="pingpong", offset=0.0),
        _person("civ_crossing_02", "crossing", "person_blue_kinematic", (-10.35, 18), math.pi, "route", route=((-10.35, 18, 8), (-21.65, 18, 14)), speed=1.0, mode="pingpong", offset=20),
        _person("civ_crossing_03", "crossing", "person_green_kinematic", (9, -16), 0, "route", route=((9, -16, 7), (23, -16, 15)), speed=1.0, mode="pingpong", offset=0.0),
        _person("civ_crossing_04", "crossing", "person_blue_kinematic", (23, -14), math.pi, "route", route=((23, -14, 9), (9, -14, 13)), speed=1.0, mode="pingpong", offset=20),
    )
    bus_waiting = tuple(_person(f"civ_bus_waiting_{i:02d}", "bus_waiting", asset, (x, y), yaw)
                        for i, (asset, x, y, yaw) in enumerate((
                            ("person_blue_kinematic", 7.4, -9.0, 1.57), ("person_green_kinematic", 8.2, -8.1, 2.5),
                            ("person_dark", 9.1, -9.2, 1.2), ("person_orange", 7.7, -10.1, -0.2)), 1))
    joggers = tuple(_person(f"civ_jogger_{i:02d}", "jogger", asset, (x, y), 1.57, "route",
                            route=((x, y), (x, y + 17), (x, y + 30), (x, y + 17)), speed=2.5, mode="pingpong")
                    for i, (asset, x, y) in enumerate((("person_orange", -42, -31), ("person_blue_kinematic", 43, 27)), 1))
    follow_route = ((21.65, -43), (21.65, -21.65), (18, -21.65), (18, -10.35), (21.65, -10.35), (21.65, 10))
    follow = _person("civ_jogger_03", "jogger", "person_red_kinematic", follow_route[0], math.pi / 2,
                     "route", "follow_jogger", follow_route, 2.5, "once")
    park_sitting = tuple(_person(f"civ_park_sitting_{i:02d}", "park_sitting", "person_sitting", (x, y), yaw)
                         for i, (x, y, yaw) in enumerate(((28, -1, 0), (34, 1.5, math.pi / 2),
                                                         (29, 4, 0), (35, 4, math.pi)), 1))
    plaza_crowd = tuple(_person(f"civ_plaza_crowd_{i:02d}", "plaza_crowd", asset, (x, y), yaw)
                        for i, (asset, x, y, yaw) in enumerate((
                            ("person_dark", -5.5, 3.3, 0.4), ("person_blue_kinematic", -3.5, 2.7, -0.5),
                            ("person_green_kinematic", -1.4, 4.2, 2.0), ("person_dark", 1.2, 3.6, -2.4),
                            ("person_orange", 3.8, 2.8, 1.1), ("person_blue_kinematic", 5.3, 0.8, 3.0),
                            ("person_green_kinematic", 4.2, -1.6, -0.6), ("person_dark", 2, -3.7, 0.5),
                            ("person_blue_kinematic", -0.5, -4.2, 1.8), ("person_orange", -3, -3.6, -2.1),
                            ("person_green_kinematic", -5.3, -1.8, 2.8), ("person_dark", -3.7, 0.3, -1.5)), 1))
    red = _person("civ_red_shirt_01", "red_shirt", "person_red_kinematic", (0.2, 5.6), -0.8)
    collapsed = _person("civ_collapsed_01", "collapsed", "person_collapsed_kinematic", (-8, 22), 0.2)
    # Right-hand traffic: vehicles circulate around blocks (clockwise loops use the inner lanes,
    # anticlockwise loops the outer lanes) instead of reversing in place at a route end.
    vehicles = (
        VehiclePlacement.loop("veh_car_01", "car", block_loop(-48, -16, -16, 16, clockwise=False, start_side="south"), 6.0, 2.0),
        VehiclePlacement.loop("veh_car_02", "car", block_loop(-16, 16, 16, 48, clockwise=True, start_side="west"), 7.0, 2.0),
        VehiclePlacement.loop("veh_car_03", "car", block_loop(16, 48, -48, -16, clockwise=False, start_side="north"), 5.5, 1.8),
        VehiclePlacement.loop("veh_van_01", "van", block_loop(-48, -16, 16, 48, clockwise=True, start_side="east"), 5.0, 1.6),
        # The bus dwells at the stop beside the shelter on the plaza block's south road; the
        # stop sits 8.1 m from the south-east corner so the 7 m turn fits both segments.
        VehiclePlacement.loop("veh_bus_01", "bus", block_loop(-16, 16, -16, 16, clockwise=True, start_side="south",
                                                              stop=(6.0, 8.0)), 5.0, 1.4),
    )
    # Kerbside parking lanes, facing the adjacent traffic direction and clear of junctions and
    # of every circulating vehicle's lane.
    parking = PARKING_LANE_OFFSET_M
    parked = tuple(ParkedCar(*p) for p in (
        (-38, -16 + parking, math.pi, (0.42, 0.19, 0.11)), (-32.5, -16 + parking, math.pi, (0.16, 0.25, 0.43)),
        (40, 16 - parking, 0.0, (0.68, 0.68, 0.63)), (34.5, 16 - parking, 0.0, (0.25, 0.33, 0.25)),
        (48 + parking, 36, math.pi / 2, (0.58, 0.15, 0.12))))
    return UrbanLayout(
        seed=seed, canonical=True, roads_x=roads, roads_y=roads,
        crosswalk_centres=crosswalks, buildings=buildings, plaza_half_size=9.0,
        park_center=(32, 0), park_half_size=9.0, park_trees=park_trees,
        benches=benches,
        # These lamps formerly stood on the road centre lines; they now stand behind the
        # sidewalk on the outer side of the same road.
        lamp_positions=tuple((x + math.copysign(LAMP_OFFSET_M, x), y) for x, y in (
            (-48, -45), (-48, 0), (-48, 45), (-16, -45), (-16, 43),
            (16, -45), (16, 45), (48, -45), (48, 0), (48, 45))),
        utility_poles=tuple((x, y) for x in (-65, 65) for y in (-46, -17, 18, 49)),
        trash_bins=((-43.0, 16.0), (-21, -43), (20, 42.0), (44, -21), (5, -10.0), (44, 7), (-7, 42.0), (9, -44)),
        bus_shelter_center=(8.5, -9.8), jogger_shelter_center=(21.65, -5.0),
        jogger_shelter_size=(3.6, 8.0), parked_cars=parked,
        commuters=commuters, crossings=crossings, bus_waiting=bus_waiting,
        joggers=joggers, follow_jogger=follow, park_sitting=park_sitting,
        plaza_crowd=plaza_crowd, red_shirt=red, collapsed=collapsed,
        vehicles=vehicles, follow_speed=2.5, follower_altitude_m=10.0,
        follower_horizontal_m=5.0, expected_occlusion_s=8.0 / 2.5)


def _consume_legacy_vehicle_draws(rng: random.Random, safe_x: tuple[float, ...],
                                  safe_y: tuple[float, ...]) -> None:
    """Advance the layout stream exactly as the retired straight-line vehicle sampler did."""
    kinds = ["car", "car", "car", "van", "bus", "car"]
    rng.shuffle(kinds)
    for _ in kinds[:rng.randint(3, 6)]:
        turn = rng.random() < 0.25
        vertical = rng.choice((True, False))
        rng.choice(safe_x), rng.choice(safe_y)
        rng.choice((-1.0, 1.0)), rng.choice((-1.0, 1.0))
        if turn:
            rng.choice((-56.0, 56.0)), rng.choice((-56.0, 56.0))
        elif vertical:
            rng.choice((-56.0, 56.0))
        else:
            rng.choice((-56.0, 56.0))
        rng.uniform(4.0, 8.0), rng.uniform(1.4, 2.0)


def sample_traffic(seed: int, attempt: int, roads_x: tuple[float, ...],
                   roads_y: tuple[float, ...]) -> tuple[VehiclePlacement, ...]:
    """Circulating right-hand traffic around city blocks, from an independent stream.

    Heavy vehicles use anticlockwise (outer-lane, left-turn) loops, whose corners leave room
    for their larger turning radius; cars may circulate either way.
    """
    from sim.worldgen.seeding import substream
    from sim.worldgen.vehicles import SAMPLED_TYPES, vehicle_type

    rng = substream(seed, "traffic", str(attempt))
    xs = [x for x in roads_x if abs(x) <= 54.0]
    ys = [y for y in roads_y if abs(y) <= 54.0]
    blocks = [(x0, x1, y0, y1) for x0, x1 in zip(xs, xs[1:]) for y0, y1 in zip(ys, ys[1:])]
    if not blocks:
        return ()
    vehicles: list[VehiclePlacement] = []
    counts: dict[str, int] = {}
    for _ in range(rng.randint(3, 7)):
        type_name = rng.choices(SAMPLED_TYPES, (4, 4, 3, 2, 1, 2, 2, 1))[0]
        kind = vehicle_type(type_name)
        clockwise = False if kind.heavy else rng.random() < 0.5
        x0, x1, y0, y1 = rng.choice(blocks)
        side = rng.choice(("north", "south", "east", "west"))
        route = block_loop(x0, x1, y0, y1, clockwise=clockwise, start_side=side)
        counts[type_name] = counts.get(type_name, 0) + 1
        speed = round(rng.uniform(*kind.max_speed_m_s), 2)
        accel = round(rng.uniform(*kind.accel_m_s2), 2)
        offset = round(rng.uniform(0.0, 20.0), 2)
        vehicles.append(VehiclePlacement.loop(
            f"veh_{type_name}_{counts[type_name]:02d}", type_name, route, speed, accel,
            start_offset_s=offset, variant=rng.randrange(8)))
    return tuple(vehicles)


def _dress_parked(seed: int, attempt: int, parked: list[ParkedCar],
                  vehicles: tuple[VehiclePlacement, ...]) -> list[ParkedCar]:
    """Give parked cars sampled types and drop any whose footprint would meet a traffic lane."""
    from sim.worldgen.seeding import substream
    from sim.worldgen.vehicles import PARKED_TYPES, vehicle_type

    rng = substream(seed, "parked", str(attempt))
    lanes = []
    for vehicle in vehicles:
        points = [point[:2] for point in vehicle.waypoints] + [vehicle.waypoints[0][:2]]
        lanes.append((points, vehicle_type(vehicle.type).width_m / 2))
    dressed = []
    for car in parked:
        kind = vehicle_type(rng.choice(PARKED_TYPES))
        variant = rng.randrange(8)
        if any(distance_to_path((car.x, car.y), path) < half + kind.width_m / 2 + 0.3
               for path, half in lanes):
            continue
        # Park with the kerb-side flank 0.15 m inside the 4.75 m carriageway edge.
        shift = (CARRIAGEWAY_HALF_WIDTH_M - 0.15 - kind.width_m / 2) - PARKING_LANE_OFFSET_M
        right = (math.sin(car.yaw), -math.cos(car.yaw))
        dressed.append(replace(car, x=car.x + right[0] * shift, y=car.y + right[1] * shift,
                               type=kind.name, variant=variant))
    return dressed


def sample_urban_layout(seed: int, *,
                        validate: Callable[[UrbanLayout], float | None] | None = None
                        ) -> UrbanLayout:
    rng = random.Random(seed)
    if validate is None:
        from sim.worldgen import _validate_candidate

        validate = lambda layout: _validate_candidate("urban", layout)
    rejections: Counter[str] = Counter()
    for attempt in range(1, 101):
        try:
            roads_x, roads_y = _road_grid(rng)
        except ValueError:
            continue

        home_ix = sum(road < 0 for road in roads_x)
        home_iy = sum(road < 0 for road in roads_y)
        home_x = (roads_x[home_ix] - roads_x[home_ix - 1]) - 13.1
        home_y = (roads_y[home_iy] - roads_y[home_iy - 1]) - 13.1
        plaza_half = rng.uniform(8.0, min(10.0, home_x / 2 - 0.3, home_y / 2 - 0.3))
        safe_x = tuple(road for road in roads_x if abs(road) <= 54.0)
        safe_y = tuple(road for road in roads_y if abs(road) <= 54.0)
        cross_candidates = [(x, y) for x in roads_x if abs(x) <= 52.0
                            for y in roads_y if abs(y) <= 52.0]
        if not safe_x or not safe_y or len(cross_candidates) < 2:
            continue
        rng.shuffle(cross_candidates)
        crosswalk_centres = tuple(cross_candidates[:rng.randint(2, min(4, len(cross_candidates)))])

        follow_route: tuple[Point, ...] | None = None
        for _ in range(100):
            vertical = rng.choice((True, False))
            roads, axis = (safe_x, "x") if vertical else (safe_y, "y")
            road, side = rng.choice(roads), rng.choice((-1.0, 1.0))
            lane = road + side * 5.65
            start_along = rng.uniform(-52.0, 52.0)
            start = (lane, start_along) if vertical else (start_along, lane)
            if not 20.0 <= math.hypot(*start) <= 45.0:
                continue
            directions = [direction for direction in (-1.0, 1.0)
                          if -58.0 <= start_along + direction * 45.0 <= 58.0]
            if not directions:
                continue
            direction = rng.choice(directions)
            max_length = min(58.0, 58.0 - direction * start_along)
            if max_length < 45.0:
                continue
            end_along = start_along + direction * rng.uniform(45.0, max_length)
            end = (lane, end_along) if vertical else (end_along, lane)
            follow_route = (start, end)
            break
        if follow_route is None:
            continue
        route_length = _path_length(follow_route)
        follow_start = follow_route[0]
        occluder_xy, direction = _point_at_distance(
            follow_route, route_length * rng.uniform(0.40, 0.60))
        occluder_yaw = math.atan2(direction[1], direction[0]) - math.pi / 2
        occluder_kind = "shelter"
        shelter_length = rng.uniform(7.5, 9.0)

        blocks_x = [(a + 6.55, b - 6.55)
                    for a, b in zip((-75.0, *roads_x), (*roads_x, 75.0))]
        blocks_y = [(a + 6.55, b - 6.55)
                    for a, b in zip((-75.0, *roads_y), (*roads_y, 75.0))]
        candidates = []
        for ix, (x0, x1) in enumerate(blocks_x):
            for iy, (y0, y1) in enumerate(blocks_y):
                if (ix, iy) == (home_ix, home_iy) or x1 - x0 < 16 or y1 - y0 < 16:
                    continue
                center = ((x0 + x1) / 2, (y0 + y1) / 2)
                if distance_to_path(center, follow_route) >= 10.0:
                    candidates.append((ix, iy, x0, x1, y0, y1))
        if not candidates:
            continue
        pix, piy, px0, px1, py0, py1 = rng.choice(candidates)
        park_center = ((px0 + px1) / 2, (py0 + py1) / 2)
        park_half = rng.uniform(5.5, min(9.0, (px1 - px0) / 2 - 1.0,
                                         (py1 - py0) / 2 - 1.0))
        park_trees = []
        for _ in range(rng.randint(5, 12)):
            angle = rng.uniform(0.0, 2 * math.pi)
            radius = (park_half - 1.0) * math.sqrt(rng.random())
            park_trees.append(ParkTree(park_center[0] + math.cos(angle) * radius,
                                       park_center[1] + math.sin(angle) * radius,
                                       rng.uniform(6.0, 10.0)))
        benches = tuple(Bench(park_center[0] + rng.uniform(-park_half + 1.5, park_half - 1.5),
                              park_center[1] + rng.uniform(-park_half + 1.5, park_half - 1.5),
                              rng.uniform(-math.pi, math.pi)) for _ in range(3))

        palette = ((0.66, 0.57, 0.48), (0.58, 0.62, 0.65), (0.70, 0.64, 0.55),
                   (0.56, 0.63, 0.67), (0.62, 0.67, 0.68), (0.71, 0.66, 0.58))
        buildings = []
        for ix, (x0, x1) in enumerate(blocks_x):
            for iy, (y0, y1) in enumerate(blocks_y):
                if (ix, iy) in ((home_ix, home_iy), (pix, piy)):
                    continue
                width, depth = x1 - x0, y1 - y0
                if width < 10.0 or depth < 10.0:
                    continue
                max_columns = min(2, max(1, int(width // 10)))
                max_rows = min(2, max(1, int(depth // 10)))
                lots = rng.randint(1, max_columns * max_rows)
                columns = min(max_columns, lots)
                rows = math.ceil(lots / columns)
                lot_w, lot_h = width / columns, depth / rows
                for lot in range(lots):
                    col, row = lot % columns, lot // columns
                    max_w, max_h = lot_w - 4.0, lot_h - 4.0
                    sx, sy = rng.uniform(6.0, max_w), rng.uniform(6.0, max_h)
                    cx = x0 + (col + 0.5) * lot_w + rng.uniform(-0.5, 0.5)
                    cy = y0 + (row + 0.5) * lot_h + rng.uniform(-0.5, 0.5)
                    buildings.append(BuildingSpec(cx, cy, sx, sy, rng.uniform(6.0, 34.0),
                                                  rng.choice(palette)))

        # Lamps stand behind the sidewalk (which spans 4.75–6.55 m from the road centre).
        lamp_positions = tuple((x + sx * LAMP_OFFSET_M, y + sy * LAMP_OFFSET_M)
                               for x in roads_x for y in roads_y
                               for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)))
        utility_poles = tuple((x, y) for x in (-65.0, 65.0) for y in (-48.0, -16.0, 16.0, 48.0))
        trash_bins = []
        for _ in range(rng.randint(6, 9)):
            vertical = rng.choice((True, False))
            roads, axis = (safe_x, "x") if vertical else (safe_y, "y")
            road, side = rng.choice(roads), rng.choice((-1.0, 1.0))
            along = rng.uniform(-54.0, 54.0)
            trash_bins.append((road + side * BIN_OFFSET_M, along) if vertical
                              else (along, road + side * BIN_OFFSET_M))

        bus_x = roads_x[home_ix] - 10.5  # clear of the perpendicular road's sidewalk
        # The shelter stands behind the sidewalk (its back wall clear of the walking lanes).
        bus_y = roads_y[home_iy - 1] + 7.75
        # The former one-way/turning vehicle draws are consumed unchanged so each seed's parked
        # cars, people and occluders stay as they were; circulating traffic is sampled from an
        # independent stream below.
        _consume_legacy_vehicle_draws(rng, safe_x, safe_y)

        parked = []
        parked_count = rng.randint(3, 5)
        for _ in range(parked_count):
            for _ in range(200):
                vertical = rng.choice((True, False))
                roads, axis = (safe_x, "x") if vertical else (safe_y, "y")
                road, side = rng.choice(roads), rng.choice((-1.0, 1.0))
                along = rng.uniform(-52.0, 52.0)
                intersections = roads_y if vertical else roads_x
                if any(abs(along - cross) < 8.0 for cross in intersections):
                    continue
                # Kerbside, facing the traffic of its side of the road.
                if vertical:
                    parked.append(ParkedCar(road + side * PARKING_LANE_OFFSET_M, along,
                                            math.pi / 2 if side > 0 else -math.pi / 2,
                                            rng.choice(palette)))
                else:
                    parked.append(ParkedCar(along, road + side * PARKING_LANE_OFFSET_M,
                                            math.pi if side > 0 else 0.0, rng.choice(palette)))
                break
            else:
                break
        if len(parked) != parked_count:
            continue
        vehicles = sample_traffic(seed, attempt, roads_x, roads_y)
        parked = _dress_parked(seed, attempt, parked, vehicles)

        people = _urban_people(roads_x, roads_y, crosswalk_centres, plaza_half,
                               park_center, park_half, (bus_x, bus_y), rng, follow_route)
        commuters, crossings, bus_waiting, joggers, follow_arr, sitting, crowd, red, collapsed = people
        follow = follow_arr[0]
        if (math.dist(red.position, follow.position) < 25.0
                or distance_to_path(red.position, follow.waypoints) < 15.0):
            continue
        persons = (*commuters, *crossings, *bus_waiting, *joggers, follow,
                   *sitting, *crowd, collapsed)
        all_xy = [p.position for p in persons]
        all_xy.extend(point[:2] for person in persons for point in person.waypoints)
        all_xy.extend(v.position for v in vehicles)
        all_xy.extend(point[:2] for v in vehicles for point in v.waypoints)
        all_xy.extend((car.x, car.y) for car in parked)
        all_xy.extend(trash_bins)
        if any(abs(x) > GEOFENCE_INSET_M or abs(y) > GEOFENCE_INSET_M for x, y in all_xy):
            continue

        layout = UrbanLayout(
            seed, False, roads_x, roads_y, crosswalk_centres, tuple(buildings), plaza_half,
            park_center, park_half, tuple(park_trees), benches, lamp_positions, utility_poles,
            tuple(bin_xy for bin_xy in trash_bins if not _on_sidewalk_or_road(bin_xy, roads_x, roads_y, 0.35)),
            (bus_x, bus_y), occluder_xy, (3.4, shelter_length),
            tuple(parked), commuters, crossings, bus_waiting, joggers, follow, sitting, crowd,
            red, collapsed, tuple(vehicles), 2.5, 9.0, 5.5, 0.0, attempt,
            follow_occluder_kind=occluder_kind, follow_occluder_yaw=occluder_yaw,
            lamp_height_m=5.4)
        accepted_occlusion = validate(layout)
        if accepted_occlusion is not None:
            return replace(layout, expected_occlusion_s=accepted_occlusion)
        from sim.worldgen.validation import LAST_FAILURES

        rejections["validation"] += 1
        if LAST_FAILURES:
            rejections[f"validation: {LAST_FAILURES[0][:80]}"] += 1
    top = ", ".join(f"{reason} x{count}" for reason, count in rejections.most_common(4))
    raise RuntimeError(f"could not sample valid urban layout for seed {seed} after "
                       f"{sum(rejections.values())} attempts; top rejections: {top or 'pre-checks'}")
