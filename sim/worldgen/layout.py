from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence, TypeAlias

from sim.worldgen.surface import CANONICAL_MAP, MapSpec, TriangulatedGrid, quantize_rows

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
SIDEWALK_OFFSET_M = 5.65
LAMP_OFFSET_M = 6.85
# Procedural city cross-section (shared by the ``urban_*`` generators and the urban dressing):
# standard vs arterial carriageway half widths and the sidewalk width outside the kerb.
URBAN_STREET_HALF_WIDTH_M = 4.75
URBAN_ARTERIAL_HALF_WIDTH_M = 7.0
URBAN_SIDEWALK_WIDTH_M = 1.8
# Every recruited actor route and wander area stays inside this half-extent.
GEOFENCE_INSET_M = 60.0


def is_arterial(coordinate: float, arterials: Sequence[float]) -> bool:
    return any(abs(coordinate - value) < 1e-6 for value in arterials)


def road_half_width(coordinate: float, arterials: Sequence[float]) -> float:
    """Procedural carriageway half width of the road at ``coordinate``."""
    return URBAN_ARTERIAL_HALF_WIDTH_M if is_arterial(coordinate, arterials) else URBAN_STREET_HALF_WIDTH_M


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
    # A prone casualty in the open: the approach test targets a lying person, and a person under
    # a canopy (the missing hiker) is not observable from the transit-altitude vantages.
    casualty: PersonPlacement | None = None
    map: MapSpec = CANONICAL_MAP
    # --- procedural 1.2 km forest (empty / default on canonical worlds) ---
    # Full trail network (``trail`` is the main trail the mission content uses); ``trails``
    # holds every polyline, including ``trail`` itself. ``creeks`` likewise includes ``creek``.
    trails: tuple[tuple[Point, ...], ...] = ()
    creeks: tuple[tuple[Point, ...], ...] = ()
    clearings: tuple[tuple[Point, float], ...] = ()
    camps: tuple[Point, ...] = ()
    # Mission-relevant locations: (name, kind, x, y, radius).
    sites: tuple[tuple[str, str, float, float, float], ...] = ()
    # Static scenery handed to ``scenery.emit_scenery`` (instanced assets + merged meshes).
    scenery_instances: tuple[object, ...] = ()
    scenery_meshes: tuple[object, ...] = ()
    # Tallest static scenery top above the home datum (drives the transit altitude).
    scenery_max_top_m: float = 0.0
    # (name, count) summary of the emitted scenery, copied into the manifest.
    scenery_counts: tuple[tuple[str, int], ...] = ()
    # Flat terrain pad around the landing pad; canonical worlds keep the historical 4 m.
    home_pad_radius_m: float = 4.0
    # Coarse polyline the creek depression follows (dense ``creek`` nodes are for rendering).
    creek_terrain_path: tuple[Point, ...] = ()
    # Half-extent actor routes and wander areas must stay inside (procedural: 580, 5 m inside
    # the flight profile's +-585 m polygon).
    actor_inset_m: float = GEOFENCE_INSET_M

    @property
    def people(self) -> tuple[PersonPlacement, ...]:
        return (*self.hiker_routes, *self.ranger_routes, *self.family_positions,
                self.missing_hiker, self.injured_person, self.follow_hiker,
                *((self.casualty,) if self.casualty is not None else ()))


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


# ---------------------------------------------------------------------------------------------
# Procedural 1.2 km city data (see ``urban_sampler``). The canonical 150 m world leaves
# ``UrbanLayout.city`` as ``None`` and keeps using the flat fields above byte-for-byte.
@dataclass(frozen=True)
class PlazaSpec:
    """One paved plaza; ``mission`` marks the crowd/red-shirt plaza the manifest exposes."""

    x: float
    y: float
    half_size: float
    mission: bool = False


@dataclass(frozen=True)
class ParkSpec:
    """One park block; ``mission`` marks the park that hosts the sitting group."""

    x: float
    y: float
    half_size: float
    mission: bool = False


@dataclass(frozen=True)
class TreeSpec:
    """One placed tree: species picks the asset (``oak``/``pine``), height the instance scale."""

    x: float
    y: float
    height: float
    species: str
    street: bool = False


@dataclass(frozen=True)
class BusStopSpec:
    """One bus stop: the shelter centre and its length-axis yaw (back wall away from the road)."""

    x: float
    y: float
    yaw: float


@dataclass(frozen=True)
class CitySpec:
    """Everything the procedural city carries beyond the shared ``UrbanLayout`` fields."""

    arterial_roads_x: tuple[float, ...]
    arterial_roads_y: tuple[float, ...]
    plazas: tuple[PlazaSpec, ...]
    parks: tuple[ParkSpec, ...]
    trees: tuple[TreeSpec, ...]
    bus_stops: tuple[BusStopSpec, ...]
    sites: tuple[tuple[str, str, float, float, float], ...]
    lamps: tuple[tuple[float, float, float], ...] = ()


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
    map: MapSpec = CANONICAL_MAP
    # Procedural 1.2 km cities: actor geofence half-extent (inside the profile polygon) and the
    # tallest static scenery top (buildings incl. rooftop equipment, tree tops). Canonical
    # worlds keep the 60 m actor inset and never read ``scenery_max_top_m``.
    actor_inset_m: float = GEOFENCE_INSET_M
    scenery_max_top_m: float = 0.0
    city: CitySpec | None = None

    @property
    def people(self) -> tuple[PersonPlacement, ...]:
        return (*self.commuters, *self.crossings, *self.bus_waiting, *self.joggers,
                self.follow_jogger, *self.park_sitting, *self.plaza_crowd,
                *((self.red_shirt,) if self.canonical else ()), self.collapsed)

    @property
    def sites(self) -> tuple[tuple[str, str, float, float, float], ...]:
        """Mission/site table ``(name, kind, x, y, radius)``; canonical worlds have none."""
        return self.city.sites if self.city is not None else ()


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


def person_placement(model: str, role: str, asset: str, xy: Point, yaw: float = 0.0,
                     behavior: str = "idle", group: str = "ambient",
                     route: Sequence[Waypoint] = (), speed: float = 1.25,
                     mode: str = "loop", hold: float | None = None,
                     offset: float | None = None) -> PersonPlacement:
    return PersonPlacement(model, role, asset, xy, yaw, behavior, group, tuple(route),
                           speed, mode, None, hold, offset)


def canonical_forest_layout(seed: int = 2027) -> ForestLayout:
    hiker_routes = (
        person_placement("civ_hiker_01", "hiker", "person_orange", (-58, -47), 0.6,
                "route", route=((-58, -47), (-42, -35), (-25, -23), (-10, -7),
                                (11, 13), (31, 25), (48, 48)), speed=1.25, mode="pingpong"),
        person_placement("civ_hiker_02", "hiker", "person_blue_kinematic", (-25, -23), 0.7,
                "route", route=((-25, -23), (-10, -7), (11, 13), (31, 25), (48, 48)),
                speed=1.0, mode="pingpong"),
        person_placement("civ_hiker_03", "hiker", "person_green_kinematic", (11, 13), 0.5,
                "route", route=((11, 13), (22, 19), (31, 25), (39, 35), (48, 48)),
                speed=1.15, mode="pingpong"),
    )
    ranger = (person_placement("civ_ranger_01", "ranger", "person_hivis", (-10, -7), 0.7,
                      "route", route=((-10, -7), (-25, -23), (-37, -32), (-48, -39),
                                      (-58, -47)), speed=1.0, mode="pingpong"),)
    family = (
        person_placement("civ_family_01", "family", "person_red_kinematic", (25.2, 35.4), 0.1,
                hold=0.0),
        person_placement("civ_family_02", "family", "person_blue_kinematic", (26.4, 37.2), -0.5),
        person_placement("civ_family_03", "family", "person_green_kinematic", (31.4, 36.2), 2.7),
        person_placement("civ_family_04", "family", "person_dark", (32.2, 33.6), 2.9),
    )
    follow_route = tuple(_FOREST_TRAIL[5:10])
    return ForestLayout(
        seed=seed, canonical=True, trail=_FOREST_TRAIL, creek=_FOREST_CREEK,
        clearing=(-7.0, 31.0), clearing_radius=17.0, camp=(29.0, 35.0),
        missing_hiker=person_placement("civ_missing_hiker_01", "missing_hiker",
                              "person_laying_kinematic", (-42.0, 20.0), 0.4),
        missing_canopy_tree=(-45.2, 20.5),
        # The injured hiker rests in the open clearing: the campaign's scan test measures
        # detection coverage over the clearing scan rectangle, and an empty meadow can never
        # score. The canopy-covered spot (13, -43) offered no clear descent column.
        injured_person=person_placement("civ_injured_01", "injured", "person_sitting",
                               (-6.0, 30.0), 1.2),
        casualty=person_placement("civ_casualty_01", "casualty", "person_laying_kinematic",
                         (-9.5, 33.5), 2.4),
        hiker_routes=hiker_routes, ranger_routes=ranger, family_positions=family,
        follow_hiker=person_placement("civ_hiker_04", "hiker", "person_green_kinematic",
                             follow_route[0], 0.5, "route", "follow_hiker",
                             follow_route, 1.25, "once"),
        follow_occluder_xy=(-13.42, -16.164), follow_speed=1.25,
        # 8 m / 3 m frames the target inside the 84 deg / -45 deg camera and lets the enlarged
        # bush occlude the pursuit; a steeper, closer follower (6.8 m / 1.66 m looks 77 deg down)
        # leaves the target out of frame unless the bush sits in the follower lane.
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


def point_segment_distance(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> float:
    """2-D distance from point ``p`` to segment ``ab`` (a point when ``a == b``)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    denom = dx * dx + dy * dy
    t = 0.0 if denom == 0 else max(0.0, min(1.0,
        ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / denom))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def distance_to_path(p: Point, path: tuple[Point, ...] | list[Point]) -> float:
    return min(point_segment_distance(p, a, b) for a, b in zip(path, path[1:]))


def distance_to_paths(p: Point, paths: Sequence[Sequence[Point]]) -> float:
    """Distance to the nearest of several polylines (``inf`` for an empty network)."""
    return min((distance_to_path(p, path) for path in paths if len(path) >= 2), default=math.inf)


def resample_path(path: Sequence[Point], step_m: float) -> tuple[Point, ...]:
    """Uniform arc-length resampling of a polyline (keeps the first and last node)."""
    if len(path) < 2:
        return tuple(path)
    if step_m <= 0.0:
        raise ValueError("step_m must be positive")
    result: list[Point] = [path[0]]
    carried = 0.0
    for a, b in zip(path, path[1:]):
        length = _distance(a, b)
        if length <= 0.0:
            continue
        position = carried
        while position + step_m <= length:
            position += step_m
            t = position / length
            result.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        carried = position - length
    if result[-1] != path[-1]:
        result.append(path[-1])
    return tuple(result)


def forest_height(x: float, y: float, layout: ForestLayout) -> float:
    pad = layout.home_pad_radius_m
    radius = math.hypot(x, y)
    if radius <= pad:
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
    # The depression follows the coarse terrain polyline when the layout carries one (the
    # dense creek polyline would make the 301 x 301 grid build quadratic in its node count).
    creek_distance = distance_to_path((x, y), layout.creek_terrain_path or layout.creek)
    height -= 0.72 * math.exp(-((creek_distance / 5.3) ** 2))
    # Flatten the clearing with a smooth 4 m transition band; a hard edge left a 0.56–0.67 m
    # step in the terrain that the 2 m mesh rendered as a ramp under actors' feet.
    clearing_distance = math.hypot(x - layout.clearing[0], y - layout.clearing[1])
    flatten = max(0.0, min(1.0, (layout.clearing_radius + 2.0 - clearing_distance) / 4.0))
    flatten = flatten * flatten * (3.0 - 2.0 * flatten)
    height = height * (1.0 - 0.78 * flatten) + 0.43 * 0.78 * flatten
    blend = max(0.0, min(1.0, (radius - pad) / 10.0))
    blend = blend * blend * (3.0 - 2.0 * blend)
    value = max(-0.2, height * blend)
    # Generated 1.2 km worlds keep the relief inside ~+-3 m (the clamp is part of the sampler
    # contract); canonical worlds keep their historical +-0.2/2.0 window byte-for-byte.
    return value if layout.canonical else max(-1.5, min(3.2, value))


# The terrain grid is a pure function of the terrain fields, not of the mission content or
# scenery (which the sampler changes on every attempt). Keying the cache on those fields lets
# the sampler build a 301 x 301 grid once and reuse it for the validated layout, instead of
# recomputing it after every `dataclasses.replace`.
_GROUND_ROWS_CACHE: dict[tuple, tuple[tuple[float, ...], ...]] = {}


def _terrain_key(layout: ForestLayout) -> tuple:
    return (layout.canonical, layout.map, layout.home_pad_radius_m,
            layout.terrain_base, layout.terrain_amplitudes, layout.terrain_phases,
            layout.terrain_frequencies, layout.creek_terrain_path, layout.creek,
            layout.clearing, layout.clearing_radius)


def forest_ground_rows(layout: ForestLayout) -> tuple[tuple[float, ...], ...]:
    """The exported terrain height grid (rows along +y), quantized like the COLLADA mesh."""
    key = _terrain_key(layout)
    cached = _GROUND_ROWS_CACHE.get(key)
    if cached is not None:
        return cached
    spec = layout.map
    rows = tuple(tuple(row) for row in quantize_rows(
        [[forest_height(spec.min + col * spec.cell_m, spec.min + row * spec.cell_m, layout)
          for col in range(spec.grid_side)] for row in range(spec.grid_side)]))
    if len(_GROUND_ROWS_CACHE) >= 8:
        _GROUND_ROWS_CACHE.pop(next(iter(_GROUND_ROWS_CACHE)))
    _GROUND_ROWS_CACHE[key] = rows
    return rows


def forest_surface(layout: ForestLayout) -> TriangulatedGrid:
    """The physical/rendered forest ground that actors stand on (see ``sim.worldgen.surface``)."""
    spec = layout.map
    return TriangulatedGrid(forest_ground_rows(layout), spec.min, spec.min, spec.cell_m)


CLEARING_SCAN_ALTITUDE_M = 10.0
# The scan needs 2.0 m clearance; the extra 0.6 m drone radius and sampling slack are folded
# into this margin so canopies closer than this can put a scan leg inside its safety margin.
CLEARING_SCAN_MARGIN_M = 3.0
# People the forest planner must frame from a vantage: no canopy may cover their descent
# column, and every walker route keeps body clearance from canopy envelopes.
# Roles whose descent column must stay canopy-free: each needs a vantage the planner can use.
FOREST_VANTAGE_ROLES = ("missing_hiker", "injured", "casualty")


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


HOME_PAD_KEEPOUT_M = 2.0


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
        person_placement("civ_commuter_01", "commuter", "person_blue_kinematic", (-42, -9), 1.57, "route", route=((-42, -9), (-42, 7), (-42, 24), (-42, 40)), mode="pingpong"),
        person_placement("civ_commuter_02", "commuter", "person_green_kinematic", (-42, 8), -1.57, "route", route=((-42, 8), (-42, -5), (-42, -27)), mode="pingpong"),
        person_placement("civ_commuter_03", "commuter", "person_dark", (-8, 43), 0.0, "route", route=((-8, 43), (8, 43), (27, 43), (40, 43)), mode="pingpong"),
        person_placement("civ_commuter_04", "commuter", "person_orange", (40, 43), 3.14, "route", route=((40, 43), (23, 43), (6, 43), (-8, 43)), mode="pingpong"),
        person_placement("civ_commuter_05", "commuter", "person_dark", (-42, -40), 1.57, "route", route=((-42, -40), (-42, -20), (-42, -5)), mode="pingpong"),
        person_placement("civ_commuter_06", "commuter", "person_blue_kinematic", (-42, 38), -1.57, "route", route=((-42, 38), (-42, 25), (-42, 12)), mode="pingpong"),
        person_placement("civ_commuter_07", "commuter", "person_green_kinematic", (6, -42), 3.14, "route", route=((6, -42), (-11, -42), (-29, -42), (-41, -42)), mode="pingpong"),
        person_placement("civ_commuter_08", "commuter", "person_dark", (40, -42), 0.0, "route", route=((40, -42), (25, -42), (7, -42), (-7, -42)), mode="pingpong"),
        person_placement("civ_commuter_09", "commuter", "person_blue_kinematic", (12, 8), 1.57, "route", route=((12, 8), (12, 24), (12, 40)), mode="pingpong"),
        person_placement("civ_commuter_10", "commuter", "person_green_kinematic", (-8, -8), 0.0, "route", route=((-8, -8), (8, -8), (18, -8)), mode="pingpong"),
    )
    crossings = (
        person_placement("civ_crossing_01", "crossing", "person_orange", (-21.65, 16), 0, "route", route=((-21.65, 16, 10), (-10.35, 16, 12)), speed=1.0, mode="pingpong", offset=0.0),
        person_placement("civ_crossing_02", "crossing", "person_blue_kinematic", (-10.35, 18), math.pi, "route", route=((-10.35, 18, 8), (-21.65, 18, 14)), speed=1.0, mode="pingpong", offset=20),
        person_placement("civ_crossing_03", "crossing", "person_green_kinematic", (9, -16), 0, "route", route=((9, -16, 7), (23, -16, 15)), speed=1.0, mode="pingpong", offset=0.0),
        person_placement("civ_crossing_04", "crossing", "person_blue_kinematic", (23, -14), math.pi, "route", route=((23, -14, 9), (9, -14, 13)), speed=1.0, mode="pingpong", offset=20),
    )
    bus_waiting = tuple(person_placement(f"civ_bus_waiting_{i:02d}", "bus_waiting", asset, (x, y), yaw)
                        for i, (asset, x, y, yaw) in enumerate((
                            ("person_blue_kinematic", 7.4, -9.0, 1.57), ("person_green_kinematic", 8.2, -8.1, 2.5),
                            ("person_dark", 9.1, -9.2, 1.2), ("person_orange", 7.7, -10.1, -0.2)), 1))
    joggers = tuple(person_placement(f"civ_jogger_{i:02d}", "jogger", asset, (x, y), 1.57, "route",
                            route=((x, y), (x, y + 17), (x, y + 30), (x, y + 17)), speed=2.5, mode="pingpong")
                    for i, (asset, x, y) in enumerate((("person_orange", -42, -31), ("person_blue_kinematic", 43, 27)), 1))
    follow_route = ((21.65, -43), (21.65, -21.65), (18, -21.65), (18, -10.35), (21.65, -10.35), (21.65, 10))
    follow = person_placement("civ_jogger_03", "jogger", "person_red_kinematic", follow_route[0], math.pi / 2,
                     "route", "follow_jogger", follow_route, 2.5, "once")
    park_sitting = tuple(person_placement(f"civ_park_sitting_{i:02d}", "park_sitting", "person_sitting", (x, y), yaw)
                         for i, (x, y, yaw) in enumerate(((28, -1, 0), (34, 1.5, math.pi / 2),
                                                         (29, 4, 0), (35, 4, math.pi)), 1))
    plaza_crowd = tuple(person_placement(f"civ_plaza_crowd_{i:02d}", "plaza_crowd", asset, (x, y), yaw)
                        for i, (asset, x, y, yaw) in enumerate((
                            ("person_dark", -5.5, 3.3, 0.4), ("person_blue_kinematic", -3.5, 2.7, -0.5),
                            ("person_green_kinematic", -1.4, 4.2, 2.0), ("person_dark", 1.2, 3.6, -2.4),
                            ("person_orange", 3.8, 2.8, 1.1), ("person_blue_kinematic", 5.3, 0.8, 3.0),
                            ("person_green_kinematic", 4.2, -1.6, -0.6), ("person_dark", 2, -3.7, 0.5),
                            ("person_blue_kinematic", -0.5, -4.2, 1.8), ("person_orange", -3, -3.6, -2.1),
                            ("person_green_kinematic", -5.3, -1.8, 2.8), ("person_dark", -3.7, 0.3, -1.5)), 1))
    red = person_placement("civ_red_shirt_01", "red_shirt", "person_red_kinematic", (0.2, 5.6), -0.8)
    collapsed = person_placement("civ_collapsed_01", "collapsed", "person_collapsed_kinematic", (-8, 22), 0.2)
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
        # Each lamp stands behind the sidewalk on the outer side of its road (listed by its
        # road-centre-line point).
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



