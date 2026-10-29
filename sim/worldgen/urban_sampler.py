"""Procedural 1.2 km city layout sampling.

``sample_urban_layout(seed, validate=...)`` draws an irregular road grid across the whole
:data:`~sim.worldgen.surface.PROCEDURAL_MAP` (blocks ~60-120 m with wider arterials), places
400-900 textured buildings, several plazas and parks, street furniture and the city's people
(plaza crowd, bus-stop group, park sitters, commuters, zebra crossers and joggers), then hands
the layout to the shared validator exactly like the canonical sampler does. Standard-library
only and deterministic in the seed.
"""

from __future__ import annotations

from collections.abc import Callable
import math
import random

from sim.worldgen.layout import (
    Bench, BuildingSpec, CitySpec, ParkSpec, ParkTree, ParkedCar, PlazaSpec, TreeSpec,
    UrbanLayout, VehiclePlacement, block_loop, distance_to_path, road_half_width,
)
from sim.worldgen.seeding import substream
from sim.worldgen.surface import PROCEDURAL_MAP
from sim.worldgen.urban_grid import (
    ACTOR_INSET_M, Block, BlockPicker, Point, city_blocks, home_block, junction_crossing_arms,
    lamp_offset_for, near_junction, polyline_clear_of, road_axis, sample_arterials,
    tree_offset_for,
)
from sim.worldgen.urban_people import (
    bus_stop_at, crossing_arms_near, sample_bus_waiting, sample_commuters, sample_crossings,
    sample_crowd, sample_joggers, sample_sitters,
)
from sim.worldgen.validation import SampleRejected, retry_sample
from sim.worldgen.vehicles import PARKED_TYPES, SAMPLED_TYPES, vehicle_type


PARKING_INSET_M = 0.15
HOME_PLAZA_MAX_HALF_M = 16.0
MAX_ATTEMPTS = 16


def _sample_buildings(rng: random.Random, blocks: list[Block], used: set[tuple[int, int]],
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
        raise SampleRejected("too few buildings")
    if len(buildings) > 900:
        raise SampleRejected("too many buildings")
    return tuple(buildings)


def _lamp_yaw(vertical: bool, side: float) -> float:
    """Yaw so the lamp's arm (local -y) points from the sidewalk back over the road."""
    if vertical:
        return -side * math.pi / 2
    return 0.0 if side > 0 else math.pi


def _crown_radius(species: str, height: float) -> float:
    if species == "oak":
        return 6.43360 * (height / 10.0003)
    return 3.74841 * (height / 11.9999)


def _sample_street_furniture(rng: random.Random, roads_x: tuple[float, ...],
                             roads_y: tuple[float, ...], arterials_x: tuple[float, ...],
                             arterials_y: tuple[float, ...], people_points: list[Point],
                             structures: list[tuple[float, float, float]],
                             person_paths: list[list[Point]], traffic_paths: list[list[Point]],
                             parked_points: list[Point]
                             ) -> tuple[list[tuple[float, float, float]], list[TreeSpec]]:
    """Lamps along the sidewalks and street trees on some segments.

    People who stay in place keep 3 m from lamps and their crown radius plus 2.8 m from trees;
    circulating vehicle lanes keep the crown plus 2.8 m, parked cars the crown plus 2.6 m and
    every person route the crown plus the walker envelope away from trees.
    """
    lamps: list[tuple[float, float, float]] = []
    trees: list[TreeSpec] = []
    for vertical, roads, arterials, perpendicular in (
        (True, roads_x, arterials_x, roads_y), (False, roads_y, arterials_y, roads_x),
    ):
        for road in roads:
            lamp_offset = lamp_offset_for(road, arterials)
            tree_offset = tree_offset_for(road, arterials)
            for side in (-1.0, 1.0):
                along = -ACTOR_INSET_M + 12.0
                while along < ACTOR_INSET_M - 12.0:
                    near_site = min((math.hypot(road + side * lamp_offset - x, along - y)
                                     for x, y in people_points), default=1e9)
                    spacing = 26.0 if near_site < 120.0 else 46.0
                    along += rng.uniform(spacing * 0.7, spacing * 1.3)
                    if along >= ACTOR_INSET_M - 12.0:
                        break
                    lx = road + side * lamp_offset if vertical else along
                    ly = along if vertical else road + side * lamp_offset
                    if near_junction(lx, ly, perpendicular, 10.0):
                        continue
                    if any(math.hypot(lx - x, ly - y) < 3.0 for x, y in people_points):
                        continue
                    if any(math.hypot(lx - x, ly - y) < keep for x, y, keep in structures):
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
                        if near_junction(tx, ty, perpendicular, 12.0):
                            continue
                        species = "oak" if rng.random() < 0.7 else "pine"
                        height = rng.uniform(5.0, 7.6) if species == "oak" else rng.uniform(6.5, 9.5)
                        crown = _crown_radius(species, height)
                        if not _tree_clear((tx, ty), crown, people_points, structures,
                                           person_paths, traffic_paths, parked_points):
                            continue
                        if any(math.hypot(tx - lamp[0], ty - lamp[1]) < 3.0 for lamp in lamps):
                            continue
                        if any(math.hypot(tx - other.x, ty - other.y) < crown * 0.8 + 1.0
                               for other in trees):
                            continue
                        trees.append(TreeSpec(round(tx, 3), round(ty, 3), round(height, 3), species, True))
    if not lamps:
        raise SampleRejected("no lamp positions")
    return lamps, trees


def _tree_clear(point: Point, crown: float, people_points: list[Point],
                structures: list[tuple[float, float, float]],
                person_paths: list[list[Point]], traffic_paths: list[list[Point]],
                parked_points: list[Point]) -> bool:
    """Every static clearance a placed tree must keep (see ``_sample_street_furniture``)."""
    if any(math.hypot(point[0] - x, point[1] - y) < crown + 2.8 for x, y in people_points):
        return False
    if any(math.hypot(point[0] - x, point[1] - y) < crown + keep for x, y, keep in structures):
        return False
    if not polyline_clear_of(point, person_paths, crown + 1.2):
        return False
    if not polyline_clear_of(point, traffic_paths, crown + 2.8):
        return False
    if any(math.hypot(point[0] - x, point[1] - y) < crown + 2.6 for x, y in parked_points):
        return False
    return True


def _sample_park_trees(rng: random.Random, park: ParkSpec, benches: list[Bench],
                       person_paths: list[list[Point]], traffic_paths: list[list[Point]],
                       parked_points: list[Point]) -> list[TreeSpec]:
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
        if any(math.hypot(x - bench.x, y - bench.y) < crown * 0.6 + 1.5 for bench in benches):
            continue
        if not polyline_clear_of((x, y), person_paths, crown + 1.2):
            continue
        if not polyline_clear_of((x, y), traffic_paths, crown + 2.8):
            continue
        if any(math.hypot(x - px, y - py) < crown + 2.6 for px, py in parked_points):
            continue
        if any(math.hypot(x - tree.x, y - tree.y) < crown * 0.8 + 1.0 for tree in trees):
            continue
        trees.append(TreeSpec(round(x, 3), round(y, 3), round(height, 3), species, False))
    return trees


def _sample_traffic(seed: int, attempt: int, blocks: list[Block], sites: list[Point]
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
            half = road_half_width(road, arterials)
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


def _sample_open_spaces(rng: random.Random, picker: BlockPicker, home_half: float
                        ) -> tuple[tuple[PlazaSpec, ...], tuple[ParkSpec, ...]]:
    """The crowd plaza (180-480 m out), 1-3 more plazas, 3-6 parks (the first one hosts the
    sitters) and, last, the landing-pad plaza at home."""
    crowd_block = picker.choose(180.0, 480.0)
    crowd_half = min(crowd_block.extent[0] / 2 - 0.5, crowd_block.extent[1] / 2 - 0.5,
                     rng.uniform(9.0, 15.0))
    plazas = [PlazaSpec(crowd_block.center[0], crowd_block.center[1], crowd_half, True)]
    for _ in range(rng.randint(1, 3)):
        block = picker.choose(70.0)
        half = min(block.extent[0] / 2 - 0.5, block.extent[1] / 2 - 0.5, rng.uniform(7.0, 13.0))
        plazas.append(PlazaSpec(block.center[0], block.center[1], half))
    parks: list[ParkSpec] = []
    for index in range(rng.randint(3, 6)):
        block = picker.choose(60.0)
        half = min(block.extent[0] / 2 - 0.5, block.extent[1] / 2 - 0.5, rng.uniform(7.0, 18.0))
        parks.append(ParkSpec(block.center[0], block.center[1], half, index == 0))
    plazas.append(PlazaSpec(0.0, 0.0, home_half))
    return tuple(plazas), tuple(parks)


def _sample_street_scenery(seed: int, attempt: int, rng: random.Random, roads, blocks,
                           scene_points: list[Point], people_points: list[Point],
                           structures: list[tuple[float, float, float]],
                           person_paths: list[list[Point]], parks: tuple[ParkSpec, ...],
                           benches: list[Bench]):
    """Traffic, parked cars, lamps, street trees and park trees, each clear of the others and
    of the city's people; returns ``(vehicles, parked, lamps, street_trees, park_trees)``."""
    roads_x, roads_y, arterials_x, arterials_y = roads
    vehicles = _sample_traffic(seed, attempt, blocks, scene_points)
    parked = _sample_parked(seed, attempt, roads_x, roads_y, arterials_x, arterials_y,
                            scene_points, vehicles)
    traffic_paths = [[tuple(point[:2]) for point in vehicle.waypoints]
                     + [tuple(vehicle.waypoints[0][:2])] for vehicle in vehicles]
    parked_points = [(car.x, car.y) for car in parked]
    lamps, street_trees = _sample_street_furniture(
        rng, roads_x, roads_y, arterials_x, arterials_y, people_points, structures,
        person_paths, traffic_paths, parked_points)
    park_trees: list[TreeSpec] = []
    for park in parks:
        in_park = [bench for bench in benches
                   if math.hypot(bench.x - park.x, bench.y - park.y) <= park.half_size + 2.0]
        park_trees.extend(_sample_park_trees(rng, park, in_park, person_paths, traffic_paths,
                                             parked_points))
    return vehicles, parked, lamps, street_trees, park_trees


def _sample_attempt(seed: int, attempt: int, rng: random.Random) -> UrbanLayout:
    roads_x = road_axis(rng)
    roads_y = road_axis(rng)
    arterials_x = sample_arterials(rng, roads_x)
    arterials_y = sample_arterials(rng, roads_y)
    roads = (roads_x, roads_y, arterials_x, arterials_y)
    blocks = city_blocks(roads_x, arterials_x, roads_y, arterials_y)
    home = home_block(blocks)
    home_half = min(HOME_PLAZA_MAX_HALF_M, (home.x1 - home.x0) / 2 - 1.0,
                    (home.y1 - home.y0) / 2 - 1.0)
    if home_half < 8.0:
        raise SampleRejected("home block too small")
    picker = BlockPicker(rng, blocks, home)
    plazas, parks = _sample_open_spaces(rng, picker, home_half)
    crowd_plaza, sitter_park = plazas[0], parks[0]

    downtown = picker.choose(200.0, 520.0, min_extent=24.0).center
    palette = ((0.66, 0.57, 0.48), (0.58, 0.62, 0.65), (0.70, 0.64, 0.55),
               (0.56, 0.63, 0.67), (0.62, 0.67, 0.68), (0.71, 0.66, 0.58))
    buildings = _sample_buildings(rng, blocks, picker.used, downtown, palette)

    bus_stops = [bus_stop_at(rng, picker.choose(90.0, 420.0), roads_x, arterials_x,
                             roads_y, arterials_y)]
    for _ in range(rng.randint(1, 2)):
        bus_stops.append(bus_stop_at(rng, picker.choose(80.0), roads_x, arterials_x,
                                     roads_y, arterials_y))
    bus_stops = tuple(bus_stops)

    arms = junction_crossing_arms(roads_x, arterials_x, roads_y, arterials_y)
    populated = [(crowd_plaza.x, crowd_plaza.y), (sitter_park.x, sitter_park.y),
                 (bus_stops[0].x, bus_stops[0].y)]
    crossing_arms = crossing_arms_near(rng, arms, populated)

    crowd = list(sample_crowd(rng, crowd_plaza, rng.randint(8, 16)))
    bus_waiting = sample_bus_waiting(rng, bus_stops[0])
    sitters, benches = sample_sitters(rng, sitter_park, parks)
    commuters = sample_commuters(rng, roads_x, roads_y, arterials_x, arterials_y, populated)
    crossings = sample_crossings(rng, crossing_arms)
    joggers = sample_joggers(rng, roads_x, roads_y, arterials_x, arterials_y)

    total = (len(commuters) + len(crossings) + len(bus_waiting) + len(joggers)
             + len(sitters) + len(crowd))
    if total > 40:
        trim = total - 40
        if len(crowd) - trim < 8:
            raise SampleRejected("too many people")
        crowd = crowd[:len(crowd) - trim]

    people_points = [person.position for person in (*crowd, *bus_waiting, *sitters)]
    structures = [(stop.x, stop.y, 5.5) for stop in bus_stops]
    person_paths = [[(float(p[0]), float(p[1])) for p in person.waypoints]
                    for person in (*commuters, *crossings, *joggers)
                    if person.waypoints]
    scene_points = [*populated, downtown]
    vehicles, parked, lamps, street_trees, park_trees = _sample_street_scenery(
        seed, attempt, rng, roads, blocks, scene_points, people_points, structures,
        person_paths, parks, benches)
    trees = tuple([*street_trees, *park_trees])
    # Rooftop equipment sits on the 0.45 m parapet ring, so the collision top of the tallest
    # building is height + 0.45 + equipment; the transit rule clears every proxy top.
    building_tops = [building.height + 0.45 + (1.2 if building.height > 18.0 else 0.9)
                     for building in buildings]
    max_top = round(max([*building_tops, *(tree.height for tree in trees), 0.0]), 3)
    if max_top + 5.9 > 48.0:
        raise SampleRejected("scenery too tall for the flight profile")

    city = CitySpec(arterial_roads_x=arterials_x, arterial_roads_y=arterials_y,
                    plazas=plazas, parks=parks, trees=trees, bus_stops=bus_stops,
                    lamps=tuple(lamps))
    return UrbanLayout(
        seed=seed, canonical=False, roads_x=roads_x, roads_y=roads_y,
        crosswalk_centres=tuple(arm["center"] for arm in crossing_arms), buildings=buildings,
        plaza_half_size=home_half, park_center=(sitter_park.x, sitter_park.y),
        park_half_size=sitter_park.half_size,
        park_trees=tuple(ParkTree(tree.x, tree.y, tree.height) for tree in park_trees),
        benches=tuple(benches), lamp_positions=tuple((lamp[0], lamp[1]) for lamp in lamps),
        utility_poles=(), trash_bins=(), bus_shelter_center=(bus_stops[0].x, bus_stops[0].y),
        parked_cars=tuple(parked), commuters=commuters, crossings=crossings,
        bus_waiting=bus_waiting, joggers=joggers, park_sitting=sitters,
        plaza_crowd=tuple(crowd), vehicles=vehicles, attempts=attempt,
        lamp_height_m=9.27, map=PROCEDURAL_MAP, actor_inset_m=ACTOR_INSET_M,
        scenery_max_top_m=max_top, city=city)


def sample_urban_layout(seed: int, *,
                        validate: Callable[[UrbanLayout], list[str]]
                        ) -> UrbanLayout:
    """Sample and validate one procedural 1.2 km city layout (plain ``random.Random(seed)``)."""
    rng = random.Random(seed)

    def attempt_layout(attempt: int) -> UrbanLayout:
        layout = _sample_attempt(seed, attempt, rng)
        if not _actor_inside_geofence(layout, ACTOR_INSET_M):
            raise SampleRejected("actor geofence")
        return layout

    return retry_sample(attempt_layout, validate, attempts=MAX_ATTEMPTS, kind="urban", seed=seed)


__all__ = ["sample_urban_layout"]
