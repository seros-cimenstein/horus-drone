"""Road grid of the procedural 1.2 km city: proportions, jittered road axes, blocks,
junction crossings and sidewalk clearance helpers.

The base layer of the urban generator: :mod:`sim.worldgen.urban_people`,
:mod:`sim.worldgen.urban_sampler` and :mod:`sim.worldgen.urban_scenery` import from it.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random

from sim.worldgen.layout import is_arterial, point_segment_distance, road_half_width
from sim.worldgen.surface import PROCEDURAL_MAP
from sim.worldgen.validation import SampleRejected


Point = tuple[float, float]


# --- city proportions (metres) ---------------------------------------------------------------
MAP_HALF_M = PROCEDURAL_MAP.half_m                 # 600
ACTOR_INSET_M = 580.0                              # 5 m inside the profile's +-585 polygon
LAMP_OFFSET_M = 7.0
ARTERIAL_LAMP_OFFSET_M = 9.25
STREET_TREE_OFFSET_M = 7.7
ARTERIAL_TREE_OFFSET_M = 9.9
BLOCK_SETBACK_M = 9.6                              # road centre -> nearest building face
ARTERIAL_SETBACK_M = 11.8
MAP_EDGE_MARGIN_M = 12.0
CROSSWALK_HALF_LENGTH_M = 1.8                      # half width of one zebra patch along the road
CROSSWALK_ARM_M = 2.4                              # junction extent -> zebra centre
SHELTER_LENGTH_M = 8.0                             # follow occluder (shelter) length along the lane


def _setback(coordinate: float, arterials: tuple[float, ...]) -> float:
    return ARTERIAL_SETBACK_M if is_arterial(coordinate, arterials) else BLOCK_SETBACK_M


def lamp_offset_for(coordinate: float, arterials: tuple[float, ...]) -> float:
    return ARTERIAL_LAMP_OFFSET_M if is_arterial(coordinate, arterials) else LAMP_OFFSET_M


def tree_offset_for(coordinate: float, arterials: tuple[float, ...]) -> float:
    return ARTERIAL_TREE_OFFSET_M if is_arterial(coordinate, arterials) else STREET_TREE_OFFSET_M


def inside_roads(roads: tuple[float, ...], arterials: tuple[float, ...]) -> tuple[float, ...]:
    """Roads whose both sidewalks (half width + kerb inset) stay inside the actor inset."""
    inside = tuple(road for road in roads
                   if abs(road) + road_half_width(road, arterials) + 0.9 <= ACTOR_INSET_M)
    if not inside:
        raise SampleRejected("no road inside the actor inset")
    return inside


def road_axis(rng: random.Random) -> tuple[float, ...]:
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


def sample_arterials(rng: random.Random, coordinates: tuple[float, ...]) -> tuple[float, ...]:
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
class Block:
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


def city_blocks(x_roads: tuple[float, ...], x_arterials: tuple[float, ...],
                y_roads: tuple[float, ...], y_arterials: tuple[float, ...]) -> list[Block]:
    x_zones = _zones(x_roads, x_arterials)
    y_zones = _zones(y_roads, y_arterials)
    out = []
    for ix, (rx0, rx1, x0, x1) in enumerate(x_zones):
        for iy, (ry0, ry1, y0, y1) in enumerate(y_zones):
            out.append(Block(ix, iy, x0, x1, y0, y1, rx0, rx1, ry0, ry1))
    return out


def home_block(blocks: list[Block]) -> Block:
    return min(blocks, key=lambda block: math.hypot(*block.center))


def junction_crossing_arms(x_roads: tuple[float, ...], x_arterials: tuple[float, ...],
                           y_roads: tuple[float, ...], y_arterials: tuple[float, ...]
                           ) -> list[dict]:
    """Zebra arms at every junction (two per road) and the walking geometry across them."""
    arms = []
    for x in x_roads:
        for y in y_roads:
            extent = max(road_half_width(x, x_arterials), road_half_width(y, y_arterials))
            offset = extent + CROSSWALK_ARM_M
            half_x = road_half_width(x, x_arterials)
            half_y = road_half_width(y, y_arterials)
            for side in (-1.0, 1.0):
                arms.append({"center": (x, y + side * offset), "axis": "x",
                             "half": half_x, "side": side})
                arms.append({"center": (x + side * offset, y), "axis": "y",
                             "half": half_y, "side": side})
    return arms


def segment_clear_of(point: Point, segments: list[tuple[Point, Point]], radius: float) -> bool:
    return all(point_segment_distance(point, a, b) >= radius for a, b in segments)


def polyline_clear_of(point: Point, paths: list[list[Point]], radius: float) -> bool:
    segments: list[tuple[Point, Point]] = []
    for path in paths:
        segments.extend(zip(path, path[1:]))
    return segment_clear_of(point, segments, radius)


def sidewalk_interval(rng: random.Random, perpendicular: tuple[float, ...], *,
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


def near_junction(x: float, y: float, roads: tuple[float, ...], margin: float) -> bool:
    return any(abs(value - x) < margin or abs(value - y) < margin for value in roads)


class BlockPicker:
    """Draws unused city blocks by distance from home; each block hosts one feature."""

    def __init__(self, rng: random.Random, blocks: list[Block], home: Block) -> None:
        self.rng = rng
        self.blocks = blocks
        self.used: set[tuple[int, int]] = {(home.ix, home.iy)}

    def choose(self, min_distance: float, max_distance: float | None = None,
               min_extent: float = 18.0) -> Block:
        candidates = [block for block in self.blocks if (block.ix, block.iy) not in self.used
                      and block.extent[0] >= min_extent and block.extent[1] >= min_extent
                      and min_distance <= math.hypot(*block.center)
                      and (max_distance is None or math.hypot(*block.center) <= max_distance)]
        if not candidates:
            raise SampleRejected("no block available")
        block = self.rng.choice(candidates)
        self.used.add((block.ix, block.iy))
        return block
