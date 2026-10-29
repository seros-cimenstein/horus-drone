"""Trail/creek network and the people placed on it, for the 1.2 km procedural forest.

Catmull-Rom trails (a main trail within 15 m of home plus one loop per quadrant), one or two
creeks, clearings and campsites; then the people sampled against that network: three ambient
hikers in different quadrants and the ranger. :mod:`sim.worldgen.forest_procedural`
assembles them into a layout.
"""

from __future__ import annotations

from collections.abc import Sequence
import math
import random

from sim.worldgen.layout import PersonPlacement, distance_to_paths, person_placement, resample_path
from sim.worldgen.validation import SampleRejected


TERRAIN_HALF_M = 560.0                # trail/creek/clearing/camp generation half extent


def _curve(controls: Sequence[tuple[float, float]], step_m: float = 3.0) -> tuple[tuple[float, float], ...]:
    """Catmull-Rom curve through the control points, resampled at ``step_m``."""
    points: list[tuple[float, float]] = []
    extended = [controls[0], *controls, controls[-1]]
    for i in range(1, len(extended) - 2):
        p0, p1, p2, p3 = extended[i - 1:i + 3]
        segment = math.dist(p1, p2)
        subdivisions = max(2, int(segment / 2.0))
        for step in range(subdivisions):
            t = step / subdivisions
            t2, t3 = t * t, t * t * t
            values = [0.5 * ((2.0 * p1[axis]) + (-p0[axis] + p2[axis]) * t
                             + (2.0 * p0[axis] - 5.0 * p1[axis] + 4.0 * p2[axis] - p3[axis]) * t2
                             + (-p0[axis] + 3.0 * p1[axis] - 3.0 * p2[axis] + p3[axis]) * t3)
                      for axis in range(2)]
            points.append((values[0], values[1]))
    points.append(tuple(controls[-1]))
    return resample_path(points, step_m)


def path_length(path: Sequence[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def point_at_distance(path: Sequence[tuple[float, float]],
                      distance: float) -> tuple[tuple[float, float], tuple[float, float]]:
    travelled = 0.0
    for a, b in zip(path, path[1:]):
        segment = math.dist(a, b)
        if travelled + segment >= distance or b is path[-1]:
            t = 0.0 if segment == 0.0 else max(0.0, min(1.0, (distance - travelled) / segment))
            point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            direction = ((b[0] - a[0]) / segment, (b[1] - a[1]) / segment) if segment else (1.0, 0.0)
            return point, direction
        travelled += segment
    a, b = path[-2:]
    length = math.dist(a, b)
    return b, ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _slice_path(path: Sequence[tuple[float, float]], start_m: float,
                length_m: float) -> tuple[tuple[float, float], ...]:
    """Sub-polyline of ``path`` from arc length ``start_m`` to ``start_m + length_m``."""
    total = path_length(path)
    start_m = max(0.0, min(start_m, max(0.0, total - 1.0)))
    end_m = min(total, start_m + length_m)
    result = [point_at_distance(path, start_m)[0]]
    travelled = 0.0
    for index in range(1, len(path) - 1):
        travelled += math.dist(path[index - 1], path[index])
        if start_m < travelled < end_m:
            result.append(path[index])
    result.append(point_at_distance(path, end_m)[0])
    return tuple(result)


def _trail_network(rng: random.Random, half: float = TERRAIN_HALF_M
                   ) -> tuple[tuple[tuple[float, float], ...], ...]:
    """Main trail (within 15 m of home) plus one loop into each map quadrant."""
    y_west, y_east = rng.uniform(-0.35, 0.35) * half, rng.uniform(-0.35, 0.35) * half
    spine_controls = [
        (-half, y_west),
        (-half * 0.62, y_west * 0.6 + rng.uniform(-80, 80)),
        (-half * 0.25, rng.uniform(-5, 5)),
        (0.0, rng.uniform(-3, 3)),
        (half * 0.25, rng.uniform(-5, 5)),
        (half * 0.62, y_east * 0.6 + rng.uniform(-80, 80)),
        (half, y_east),
    ]
    spine = _curve(spine_controls)
    trails = [spine]
    for sign_x, sign_y in ((1.0, 1.0), (1.0, -1.0), (-1.0, 1.0), (-1.0, -1.0)):
        total = path_length(spine)
        first = rng.uniform(0.22, 0.42) if sign_x < 0 else rng.uniform(0.58, 0.78)
        second = rng.uniform(0.30, 0.48) if sign_x < 0 else rng.uniform(0.66, 0.84)
        start = point_at_distance(spine, first * total)[0]
        end = point_at_distance(spine, second * total)[0]
        controls = [
            start,
            (sign_x * rng.uniform(160, 240), sign_y * rng.uniform(160, 240)),
            (sign_x * rng.uniform(300, 400), sign_y * rng.uniform(280, 420)),
            (sign_x * rng.uniform(400, 520), sign_y * rng.uniform(120, 280)),
            end,
        ]
        trails.append(_curve(controls))
    return tuple(trails)


def _creek_network(rng: random.Random, half: float = TERRAIN_HALF_M
                   ) -> tuple[tuple[tuple[float, float], ...], ...]:
    """One or two meandering creeks crossing the map (kept clear of the home pad)."""
    creeks = []
    base = rng.choice((-1.0, 1.0)) * rng.uniform(130, 320)
    controls = []
    for index in range(6):
        y = -half + 2.0 * half * index / 5.0
        controls.append((base + rng.uniform(-90, 90) + 0.25 * y * rng.uniform(-1.0, 1.0), y))
    creeks.append(_curve(controls))
    if rng.random() < 0.55:
        base = rng.choice((-1.0, 1.0)) * rng.uniform(140, 330)
        controls = []
        for index in range(6):
            x = -half + 2.0 * half * index / 5.0
            controls.append((x, base + rng.uniform(-90, 90) + 0.25 * x * rng.uniform(-1.0, 1.0)))
        creeks.append(_curve(controls))
    return tuple(creeks)


def _clearings(rng: random.Random, creeks: Sequence[Sequence[tuple[float, float]]],
               half: float = TERRAIN_HALF_M) -> tuple[tuple[float, float, float], ...]:
    clearings: list[tuple[float, float, float]] = []
    for _ in range(rng.randint(4, 8)):
        for _attempt in range(400):
            x = rng.uniform(-half + 80, half - 80)
            y = rng.uniform(-half + 80, half - 80)
            radius = rng.uniform(13.0, 30.0)
            if math.hypot(x, y) < 70.0:
                continue
            if any(math.hypot(x - cx, y - cy) < radius + cr + 35.0 for cx, cy, cr in clearings):
                continue
            if distance_to_paths((x, y), creeks) < radius + 18.0:
                continue
            clearings.append((x, y, radius))
            break
    return tuple(clearings)


def _camps(rng: random.Random, trails: Sequence[Sequence[tuple[float, float]]],
           creeks: Sequence[Sequence[tuple[float, float]]],
           clearings: Sequence[tuple[float, float, float]],
           half: float = TERRAIN_HALF_M) -> tuple[tuple[float, float], ...]:
    camps: list[tuple[float, float]] = []
    for _ in range(rng.randint(1, 3)):
        for _attempt in range(400):
            x = rng.uniform(-half + 60, half - 60)
            y = rng.uniform(-half + 60, half - 60)
            if math.hypot(x, y) < 170.0:
                continue
            if distance_to_paths((x, y), trails) < 12.0:
                continue
            if distance_to_paths((x, y), creeks) < 15.0:
                continue
            if any(math.hypot(x - cx, y - cy) < cr + 30.0 for cx, cy, cr in clearings):
                continue
            if any(math.hypot(x - cx, y - cy) < 70.0 for cx, cy in camps):
                continue
            camps.append((x, y))
            break
    return tuple(camps)


def _route_candidates(rng: random.Random, trails: Sequence[Sequence[tuple[float, float]]]
                      ) -> list[tuple[tuple[tuple[float, float], ...], tuple[float, float]]]:
    """Compact trail slices tagged with the quadrant of their midpoint."""
    candidates = []
    for trail in trails:
        total = path_length(trail)
        for fraction in (0.12, 0.3, 0.5, 0.7):
            length = rng.uniform(45.0, 80.0)
            route = _slice_path(trail, fraction * total, length)
            if len(route) < 3:
                continue
            mid = route[len(route) // 2]
            quadrant = (1.0 if mid[0] >= 0 else -1.0, 1.0 if mid[1] >= 0 else -1.0)
            candidates.append((route, quadrant))
    return candidates


def sample_network(rng: random.Random):
    """Trails, creeks, clearings and camps."""
    trails = _trail_network(rng)
    if min(math.hypot(x, y) for x, y in trails[0]) > 15.0:
        raise SampleRejected("main trail misses home")
    creeks = _creek_network(rng)
    if min(distance_to_paths((0.0, 0.0), creeks), 99.0) < 20.0:
        raise SampleRejected("creek too close to home")
    clearings = _clearings(rng, creeks)
    if len(clearings) < 4:
        raise SampleRejected("clearing placement")
    camps = _camps(rng, trails, creeks, clearings)
    if not camps:
        raise SampleRejected("camp placement")
    return trails, creeks, clearings, camps


def sample_hikers(rng: random.Random, trails, inset: float) -> tuple[PersonPlacement, ...]:
    """Three ambient hikers, each on a trail slice in a different map quadrant."""
    hikers: list[PersonPlacement] = []
    quadrants: set[tuple[float, float]] = set()
    for route, quadrant in _route_candidates(rng, trails):
        if quadrant in quadrants or len(hikers) >= 3:
            continue
        if math.hypot(*route[0]) < 70.0:
            continue
        if max(max(abs(x), abs(y)) for x, y in route) > inset - 5.0:
            continue
        hikers.append(person_placement(f"civ_hiker_{len(hikers) + 1:02d}", "hiker",
                                       ("person_orange", "person_blue_kinematic",
                                        "person_green_kinematic")[len(hikers)],
                                       route[0], 0.0, "route", route=route,
                                       speed=(1.25, 1.0, 1.15)[len(hikers)], mode="pingpong"))
        quadrants.add(quadrant)
    if len(hikers) < 3:
        raise SampleRejected("hiker quadrants")
    return tuple(hikers)


def sample_ranger(rng: random.Random, trails, inset: float) -> PersonPlacement:
    """The ranger patrolling a slice of the main trail."""
    ranger_route = _slice_path(trails[0], rng.uniform(0.2, 0.4) * path_length(trails[0]), 90.0)
    if len(ranger_route) < 3 or max(max(abs(x), abs(y)) for x, y in ranger_route) > inset - 5.0:
        raise SampleRejected("ranger route placement")
    return person_placement("civ_ranger_01", "ranger", "person_hivis", ranger_route[0], 0.0,
                            "route", route=ranger_route, speed=1.0, mode="pingpong")
