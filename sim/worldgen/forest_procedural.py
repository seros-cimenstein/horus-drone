"""Sampler and static scenery for the 1.2 km procedural forest.

``sample_forest_layout(seed, validate=...)`` builds a :class:`~sim.worldgen.layout.ForestLayout`
on :data:`~sim.worldgen.surface.PROCEDURAL_MAP`:

* gentle terrain (relief inside ~+-3 m, flat 6 m pad at home) on 4 m cells;
* a trail network (a main trail passing within 15 m of home plus one loop per quadrant,
  several km total) rendered as per-tile textured dirt strips draped on the terrain;
* one or two creeks rendered as textured strips with ``rock_a`` instances along the banks;
* 4-8 clearings (radius 13-30 m) and 1-3 campsites;
* trees as instanced Fuel assets (``tree_pine`` / ``tree_oak``) with dense stands, sparse
  groves and open meadows, saplings as small instances of the same assets, no primitive
  spheres/cones anywhere;
* fallen logs as textured bark meshes with cylinder proxies.

All static scenery is handed to :func:`sim.worldgen.scenery.emit_scenery` (instanced visuals +
per-tile trimesh proxies + sidecar), so the physics budget stays at one trimesh per 100 m tile.

Mission content is spread across the map: the missing hiker lies off-trail under its own
canopy at least 150 m from home, the injured person and the casualty rest in the mission
clearing at least 120 m from home, the follow route starts at least 100 m from home and is
occluded by a shrub whose narrow base keeps walkers clear while its crown spans the camera
ray (2.6-3.9 s target, measured on the real proxies before the world is accepted).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math
import random
from typing import Callable, Sequence

from sim.worldgen.assets import ASSETS
from sim.worldgen.geometry import CollisionWorld, Primitive
from sim.worldgen.layout import (
    ForestLayout, PersonPlacement, distance_to_path, distance_to_paths, forest_surface,
    resample_path,
)
from sim.worldgen.plan import Camera, Ground, plan_follow, plan_vantage
from sim.worldgen.proxy import ProxyShape
from sim.worldgen.scenery import DEFAULT_TILE_M, SceneryInstance, SceneryMesh
from sim.worldgen.surface import PROCEDURAL_MAP

# ---------------------------------------------------------------- design constants
# Actor routes/wander areas stay 5 m inside the flight profile's +-585 m polygon.
PROCEDURAL_ACTOR_INSET_M = 580.0
# The planner limits of config/horus_procedural_forest.yaml (vantage polygon + max goto leg).
PROCEDURAL_VANTAGE_BOUNDS_M = 585.0
PROCEDURAL_MAX_LEG_M = 850.0
# Flight profile: forest trees must keep their tops below 23 m above the home datum.
TREE_TOP_LIMIT_M = 23.0
TREE_LIMIT = 9000                     # tree + sapling instance budget
# Full stand density within FOCUS_RADIUS_M of home and every mission site; elsewhere only
# FAR_TREE_DENSITY of it and no saplings. Every tree instance costs each drone render sensor a
# fixed per-item overhead regardless of distance (a 400 m camera far clip changed nothing):
# halving the instance count raised live RTF on Koko from 0.23 to 0.49.
FOCUS_RADIUS_M = 120.0
FAR_TREE_DENSITY = 0.3
TRAIL_HALF_WIDTH_M = 1.2              # materials.dirt_trail_texture spans 2.4 m
CREEK_HALF_WIDTH_M = 1.3              # materials.creek_texture spans 2.6 m
STRIP_LIFT_M = 0.03                   # strips ride on the terrain to avoid z-fighting
MISSION_CLEARING_MIN_M = 120.0        # injured + casualty clearing distance from home
MISSING_HIKER_MIN_M = 150.0           # missing hiker distance from home
FOLLOW_START_MIN_M = 100.0            # follow route start distance from home
OCCLUSION_BAND_S = (2.6, 3.9)         # sampler target inside the 2.5-4.0 s validation band
FOLLOWER_ALTITUDE_M = 8.0
FOLLOWER_HORIZONTAL_M = 7.0
TERRAIN_HALF_M = 560.0                # trail/creek/clearing/camp generation half extent
TREE_HALF_M = 570.0                   # tree field half extent
GRID_CELL_M = 4.0                     # exclusion raster cell


def _lattice(rng: random.Random, cells: int) -> list[list[float]]:
    return [[rng.random() for _ in range(cells)] for _ in range(cells)]


def _noise_at(grid: list[list[float]], u: float, v: float) -> float:
    """Smoothstep bilinear sample of a tiling lattice (u, v in [0, 1])."""
    cells = len(grid)
    gx, gy = u * cells, v * cells
    x0, y0 = int(gx) % cells, int(gy) % cells
    x1, y1 = (x0 + 1) % cells, (y0 + 1) % cells
    tx, ty = gx - int(gx), gy - int(gy)
    tx, ty = tx * tx * (3.0 - 2.0 * tx), ty * ty * (3.0 - 2.0 * ty)
    top = grid[y0][x0] * (1.0 - tx) + grid[y0][x1] * tx
    bottom = grid[y1][x0] * (1.0 - tx) + grid[y1][x1] * tx
    return top * (1.0 - ty) + bottom * ty


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


def _path_length(path: Sequence[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def _point_at_distance(path: Sequence[tuple[float, float]],
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
    total = _path_length(path)
    start_m = max(0.0, min(start_m, max(0.0, total - 1.0)))
    end_m = min(total, start_m + length_m)
    result = [_point_at_distance(path, start_m)[0]]
    travelled = 0.0
    for index in range(1, len(path) - 1):
        travelled += math.dist(path[index - 1], path[index])
        if start_m < travelled < end_m:
            result.append(path[index])
    result.append(_point_at_distance(path, end_m)[0])
    return tuple(result)


def _person(model: str, role: str, asset: str, xy: tuple[float, float], yaw: float = 0.0,
            behavior: str = "idle", group: str = "ambient",
            route: Sequence[tuple[float, float]] = (), speed: float = 1.25,
            mode: str = "loop") -> PersonPlacement:
    return PersonPlacement(model, role, asset, xy, yaw, behavior, group, tuple(route), speed, mode)


class _BlockGrid:
    """Coarse raster of no-place zones (home pad, trails, creeks, clearings, camps)."""

    def __init__(self, half_m: float, cell_m: float = GRID_CELL_M) -> None:
        self.half = half_m
        self.cell = cell_m
        self.side = int(2.0 * half_m / cell_m) + 1
        self.blocked = bytearray(self.side * self.side)

    def mark_disc(self, x: float, y: float, radius: float) -> None:
        if radius <= 0.0:
            return
        i0 = max(0, int((x - radius + self.half) / self.cell))
        i1 = min(self.side - 1, int((x + radius + self.half) / self.cell))
        j0 = max(0, int((y - radius + self.half) / self.cell))
        j1 = min(self.side - 1, int((y + radius + self.half) / self.cell))
        for j in range(j0, j1 + 1):
            cy = -self.half + j * self.cell
            row = j * self.side
            for i in range(i0, i1 + 1):
                cx = -self.half + i * self.cell
                if math.hypot(cx - x, cy - y) <= radius:
                    self.blocked[row + i] = 1

    def mark_path(self, path: Sequence[tuple[float, float]], radius: float) -> None:
        for x, y in path:
            self.mark_disc(x, y, radius)

    def blocked_at(self, x: float, y: float) -> bool:
        i = int((x + self.half) / self.cell)
        j = int((y + self.half) / self.cell)
        if i < 0 or j < 0 or i >= self.side or j >= self.side:
            return True
        return bool(self.blocked[j * self.side + i])


def _strip_meshes(name: str, path: Sequence[tuple[float, float]], half_width: float,
                  texture: str, surface, *, step_m: float = 2.0,
                  lift: float = STRIP_LIFT_M) -> list[SceneryMesh]:
    """Textured strip draped on the terrain, chunked per 100 m tile."""
    nodes = resample_path(path, step_m)
    chunks: dict[tuple[int, int], dict] = {}
    previous: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None
    distance = 0.0
    for index, point in enumerate(nodes):
        if index == 0:
            dx, dy = nodes[1][0] - point[0], nodes[1][1] - point[1]
        elif index == len(nodes) - 1:
            dx, dy = point[0] - nodes[-2][0], point[1] - nodes[-2][1]
        else:
            dx, dy = nodes[index + 1][0] - nodes[index - 1][0], nodes[index + 1][1] - nodes[index - 1][1]
        length = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / length, dx / length
        z = surface.height(point[0], point[1]) + lift
        normal = surface.normal(point[0], point[1])
        left = (point[0] + nx * half_width, point[1] + ny * half_width, z)
        right = (point[0] - nx * half_width, point[1] - ny * half_width, z)
        if previous is not None:
            distance += math.dist(nodes[index - 1], point)
            mid_x = (previous[0][0] + left[0]) / 2.0
            mid_y = (previous[0][1] + left[1]) / 2.0
            tile = (math.floor(mid_x / DEFAULT_TILE_M), math.floor(mid_y / DEFAULT_TILE_M))
            chunk = chunks.setdefault(tile, {"positions": [], "normals": [], "uvs": [], "faces": []})
            base = len(chunk["positions"])
            v = distance
            chunk["positions"].extend((previous[0], previous[1], right, left))
            chunk["normals"].extend((normal, normal, normal, normal))
            chunk["uvs"].extend(((0.0, v), (1.0, v), (1.0, v), (0.0, v)))
            chunk["faces"].extend(((base, base + 1, base + 2), (base, base + 2, base + 3)))
        previous = (left, right)
    meshes = []
    for (ix, iy), chunk in sorted(chunks.items()):
        meshes.append(SceneryMesh(f"{name}_{ix}_{iy}", texture,
                                  tuple(chunk["positions"]), tuple(chunk["normals"]),
                                  tuple(chunk["uvs"]), tuple(chunk["faces"]),
                                  proxies=(), texture=texture))
    return meshes


def _probe_world(proxies: Sequence[ProxyShape]) -> CollisionWorld:
    return CollisionWorld([
        Primitive(name=f"probe/{index}", model="probe", shape=proxy.shape,
                  transform=proxy.pose, size=proxy.size, static=True)
        for index, proxy in enumerate(proxies)
    ])


def _instance_proxies(instance: SceneryInstance) -> list[ProxyShape]:
    asset = ASSETS[instance.asset]
    return [proxy.transformed(instance.x, instance.y, instance.z, instance.yaw, instance.scale)
            for proxy in asset.proxies]


# ---------------------------------------------------------------- generation


def _terrain_fields(rng: random.Random) -> dict:
    return {
        "terrain_base": rng.uniform(0.9, 1.35),
        "terrain_amplitudes": (rng.uniform(0.5, 0.75), rng.uniform(0.3, 0.5),
                               rng.uniform(0.2, 0.4)),
        "terrain_phases": (rng.uniform(-math.pi, math.pi), rng.uniform(-math.pi, math.pi),
                           rng.uniform(-math.pi, math.pi)),
        "terrain_frequencies": (rng.uniform(0.0035, 0.0055), rng.uniform(0.003, 0.0045),
                                rng.uniform(0.004, 0.006), rng.uniform(0.0035, 0.005),
                                rng.uniform(0.0012, 0.0022)),
    }


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
        total = _path_length(spine)
        first = rng.uniform(0.22, 0.42) if sign_x < 0 else rng.uniform(0.58, 0.78)
        second = rng.uniform(0.30, 0.48) if sign_x < 0 else rng.uniform(0.66, 0.84)
        start = _point_at_distance(spine, first * total)[0]
        end = _point_at_distance(spine, second * total)[0]
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
        total = _path_length(trail)
        for fraction in (0.12, 0.3, 0.5, 0.7):
            length = rng.uniform(45.0, 80.0)
            route = _slice_path(trail, fraction * total, length)
            if len(route) < 3:
                continue
            mid = route[len(route) // 2]
            quadrant = (1.0 if mid[0] >= 0 else -1.0, 1.0 if mid[1] >= 0 else -1.0)
            candidates.append((route, quadrant))
    return candidates


def _missing_hiker(rng: random.Random, trails: Sequence[Sequence[tuple[float, float]]],
                   creeks: Sequence[Sequence[tuple[float, float]]],
                   clearings: Sequence[tuple[float, float, float]],
                   camps: Sequence[tuple[float, float]],
                   follow_route: Sequence[tuple[float, float]],
                   inset: float) -> tuple[tuple[float, float], tuple[float, float]] | None:
    for _ in range(240):
        trail = rng.choice(trails)
        total = _path_length(trail)
        along = rng.uniform(40.0, max(41.0, total - 40.0))
        node, direction = _point_at_distance(trail, along)
        side = rng.choice((-1.0, 1.0))
        normal = (-direction[1], direction[0])
        offset = rng.uniform(6.0, 15.0) * side
        hiker = (node[0] + normal[0] * offset, node[1] + normal[1] * offset)
        if math.hypot(*hiker) < MISSING_HIKER_MIN_M:
            continue
        if not 6.0 <= distance_to_paths(hiker, trails) <= 15.0:
            continue
        if distance_to_paths(hiker, creeks) < 14.0:
            continue
        if any(math.hypot(hiker[0] - cx, hiker[1] - cy) < cr + 15.0 for cx, cy, cr in clearings):
            continue
        if any(math.hypot(hiker[0] - cx, hiker[1] - cy) < 25.0 for cx, cy in camps):
            continue
        if distance_to_path(hiker, follow_route) < 25.0:
            continue
        if max(abs(hiker[0]), abs(hiker[1])) > inset - 25.0:
            continue
        canopy_distance = rng.uniform(2.4, 3.0)
        canopy = (hiker[0] + normal[0] * math.copysign(canopy_distance, side),
                  hiker[1] + normal[1] * math.copysign(canopy_distance, side))
        return hiker, canopy
    return None


# ---------------------------------------------------------------- scenery


def _tree_field(rng: random.Random, surface, tree_grid: _BlockGrid, sapling_grid: _BlockGrid,
                half: float = TREE_HALF_M,
                focus: Sequence[tuple[float, float]] = ((0.0, 0.0),),
                ) -> tuple[list[SceneryInstance], int, int, float]:
    """Instanced trees and saplings with local density variation; no primitive vegetation.

    Stands keep their full density within ``FOCUS_RADIUS_M`` of a ``focus`` point (home and the
    mission sites); farther away only ``FAR_TREE_DENSITY`` of the trees and no saplings remain.
    """
    noise = _lattice(rng, 12)
    cell = 10.0
    instances: list[SceneryInstance] = []
    occupied: dict[tuple[int, int], list[tuple[float, float]]] = {}
    trees = 0
    saplings = 0
    max_top = 0.0

    def too_close(x: float, y: float, spacing: float) -> bool:
        i, j = math.floor(x / 4.0), math.floor(y / 4.0)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for px, py in occupied.get((i + di, j + dj), ()):
                    if math.hypot(px - x, py - y) < spacing:
                        return True
        return False

    def remember(x: float, y: float) -> None:
        occupied.setdefault((math.floor(x / 4.0), math.floor(y / 4.0)), []).append((x, y))

    side = int(2.0 * half / cell)
    for j in range(side):
        if trees >= TREE_LIMIT:
            break
        for i in range(side):
            x = -half + (i + 0.5) * cell + rng.uniform(-3.5, 3.5)
            y = -half + (j + 0.5) * cell + rng.uniform(-3.5, 3.5)
            u = (x + half) / (2.0 * half)
            v = (y + half) / (2.0 * half)
            density = _noise_at(noise, u, v)
            probability = 0.16 if density < 0.40 else (0.55 if density < 0.62 else 0.92)
            near = any(math.hypot(x - fx, y - fy) <= FOCUS_RADIUS_M for fx, fy in focus)
            if not near:
                probability *= FAR_TREE_DENSITY
            if rng.random() > probability:
                continue
            if tree_grid.blocked_at(x, y) or too_close(x, y, 2.2):
                continue
            if rng.random() < 0.62:
                asset, native = "tree_pine", 12.0
                scale = rng.uniform(0.85, 1.6)
            else:
                asset, native = "tree_oak", 10.0
                scale = rng.uniform(0.55, 1.0)
            base = surface.height(x, y)
            top = base + native * scale
            if top > TREE_TOP_LIMIT_M - 0.4:
                continue
            yaw = rng.uniform(-math.pi, math.pi)
            instances.append(SceneryInstance(asset, x, y, base, yaw, scale))
            remember(x, y)
            trees += 1
            max_top = max(max_top, top)
            if near and density > 0.62 and rng.random() < 0.35 and trees + saplings < TREE_LIMIT:
                angle = rng.uniform(-math.pi, math.pi)
                radius = rng.uniform(2.5, 6.0)
                sx = x + radius * math.cos(angle)
                sy = y + radius * math.sin(angle)
                if sapling_grid.blocked_at(sx, sy) or too_close(sx, sy, 1.2):
                    continue
                sapling_asset = "tree_pine" if rng.random() < 0.6 else "tree_oak"
                sapling_scale = rng.uniform(0.22, 0.45)
                sapling_base = surface.height(sx, sy)
                sapling_top = sapling_base + (12.0 if sapling_asset == "tree_pine" else 10.0) * sapling_scale
                if sapling_top > TREE_TOP_LIMIT_M - 0.4:
                    continue
                instances.append(SceneryInstance(sapling_asset, sx, sy, sapling_base,
                                                 rng.uniform(-math.pi, math.pi), sapling_scale))
                remember(sx, sy)
                saplings += 1
                max_top = max(max_top, sapling_top)
    return instances, trees, saplings, max_top


def _bank_rocks(rng: random.Random, creeks: Sequence[Sequence[tuple[float, float]]], surface,
                rock_grid: _BlockGrid) -> tuple[list[SceneryInstance], float]:
    instances: list[SceneryInstance] = []
    max_top = 0.0
    for creek in creeks:
        total = _path_length(creek)
        along = rng.uniform(0.0, 10.0)
        side = 1.0
        while along < total:
            point, direction = _point_at_distance(creek, along)
            normal = (-direction[1], direction[0])
            side = -side
            lateral = rng.uniform(2.0, 4.2)
            x = point[0] + normal[0] * lateral * side
            y = point[1] + normal[1] * lateral * side
            if not rock_grid.blocked_at(x, y):
                scale = rng.uniform(0.35, 1.2)
                base = surface.height(x, y)
                instances.append(SceneryInstance("rock_a", x, y, base,
                                                 rng.uniform(-math.pi, math.pi), scale))
                max_top = max(max_top, base + ASSETS["rock_a"].native_height_m * scale)
            along += rng.uniform(9.0, 16.0)
    return instances, max_top


def _fallen_logs(rng: random.Random, surface, trails, creeks, clearings, camps,
                 half: float = TERRAIN_HALF_M) -> list[SceneryMesh]:
    """Textured bark logs with cylinder proxies (``dressing.deadfall_mesh``)."""
    from sim.worldgen.dressing import deadfall_mesh

    meshes: list[SceneryMesh] = []
    placed: list[tuple[float, float]] = []
    for _ in range(300):
        if len(meshes) >= 12:
            break
        x = rng.uniform(-half, half)
        y = rng.uniform(-half, half)
        if math.hypot(x, y) < 25.0:
            continue
        if distance_to_paths((x, y), trails) < 6.0:
            continue
        if distance_to_paths((x, y), creeks) < 4.0:
            continue
        if any(math.hypot(x - cx, y - cy) < cr + 3.0 for cx, cy, cr in clearings):
            continue
        if any(math.hypot(x - cx, y - cy) < 16.0 for cx, cy in camps):
            continue
        if any(math.hypot(x - px, y - py) < 9.0 for px, py in placed):
            continue
        length = rng.uniform(3.5, 7.5)
        yaw = rng.uniform(-math.pi, math.pi)
        z = surface.height(x, y) + 0.25
        meshes.append(deadfall_mesh(f"fallen_log_{len(meshes) + 1:02d}", x, y, z, yaw,
                                    length=length, radius=0.25))
        placed.append((x, y))
    return meshes


def _camp_family(camp: tuple[float, float], camp_proxies: Sequence[ProxyShape],
                 rng: random.Random) -> tuple[PersonPlacement, ...]:
    """Four family members around the camp, clear of the tents/fire ring/table geometry."""
    world = _probe_world(camp_proxies)
    ground = Ground.from_surface(_FlatSurface())
    box = (-0.45, 0.45, -0.45, 0.45, 0.05, 1.95)
    chosen: list[tuple[float, float]] = []
    angle_offset = rng.uniform(-math.pi, math.pi)
    for radius in (2.0, 2.6, 3.2, 3.8, 1.6):
        for step in range(16):
            angle = angle_offset + 2.0 * math.pi * step / 16.0
            x = camp[0] + radius * math.cos(angle)
            y = camp[1] + radius * math.sin(angle)
            if any(math.hypot(x - px, y - py) < 1.4 for px, py in chosen):
                continue
            clearance, _blocker = _box_clearance(world, ground, x, y, angle, box)
            if clearance < 0.2:
                continue
            chosen.append((x, y))
            if len(chosen) == 4:
                break
        if len(chosen) == 4:
            break
    if len(chosen) < 4:
        return ()
    assets = (("person_red_kinematic", 0.0), ("person_blue_kinematic", 0.7),
              ("person_green_kinematic", 2.1), ("person_dark", 2.8))
    return tuple(_person(f"civ_family_{index:02d}", "family", asset, point, yaw)
                 for index, ((asset, yaw), point) in enumerate(zip(assets, chosen), 1))


class _FlatSurface:
    """Flat z=0 surface for clearance probes that do not depend on the terrain."""

    def height(self, x: float, y: float) -> float:
        return 0.0

    def normal(self, x: float, y: float) -> tuple[float, float, float]:
        return (0.0, 0.0, 1.0)

    def segment_clearance(self, a, b) -> float:
        return min(float(a[2]), float(b[2]))


def _box_clearance(world: CollisionWorld, ground: Ground, x: float, y: float, yaw: float,
                   box: tuple[float, float, float, float, float, float]):
    from sim.worldgen.validation import box_clearance

    return box_clearance(world, ground, x, y, yaw, box)


def _follow_probe(route: Sequence[tuple[float, float]], speed: float, shrub: SceneryMesh,
                  surface, body_box: tuple[float, float, float, float, float, float],
                  torso_z: float, altitude: float, horizontal: float
                  ) -> tuple[float, float, float, float]:
    """(occlusion_s, lane_clearance, column_clearance, worst_body_clearance)."""
    world = _probe_world(shrub.proxies)
    ground = Ground.from_surface(surface)
    manifest = {
        "transit_altitude_m": 30.0,
        "follow": {
            "route": [[float(p[0]), float(p[1])] for p in route],
            "follower_altitude_m": altitude,
            "follower_horizontal_m": horizontal,
            "speed_m_s": speed,
            "model": "civ_hiker_04",
        },
        "people": [{"model": "civ_hiker_04", "position": [float(route[0][0]), float(route[0][1])],
                    "posture": "standing", "envelope": {"torso_z_m": torso_z}}],
    }
    plan = plan_follow(world, ground, manifest)
    worst = math.inf
    heading = 0.0
    samples = resample_path(route, 0.5)
    for index, (x, y) in enumerate(samples):
        if index + 1 < len(samples):
            heading = math.atan2(samples[index + 1][1] - y, samples[index + 1][0] - x)
        clearance, _ = _box_clearance(world, ground, x, y, heading, body_box)
        worst = min(worst, clearance)
    return plan.occluded_s, plan.lane_clearance_m, plan.column_clearance_m, worst


# Narrow base + wide crown: walkers pass the base while the camera ray hits the crown.
# Heights >= 5 m keep the crown's lower proxy (z + 0.42 * height) above a walker's head.
_OCCLUDER_SHAPES = ((5.4, 1.9, 0.5), (5.8, 2.1, 0.55), (6.2, 2.4, 0.6))
# (follower horizontal, follower altitude above the shrub top): shallower/higher rays shorten
# the occlusion window, and the slant stays inside the 12 m framing limit.
_OCCLUDER_VIEWS = ((4.5, 3.8), (4.5, 4.8), (3.5, 4.8), (3.5, 3.8), (4.0, 3.2), (5.0, 4.4))
_OCCLUDER_PROBE_LIMIT = 24  # bound the search cost per attempt


def _search_occluder(rng: random.Random, route: Sequence[tuple[float, float]], speed: float,
                     surface, seed: int, torso_z: float,
                     body_box: tuple[float, float, float, float, float, float]):
    """Place a shrub beside the route until the real proxies occlude the camera 2.6-3.9 s.

    The probe runs the same ``plan_follow`` the validator uses, on the shrub's real proxies,
    and also sweeps the walked body box along the whole route. Returns ``(mesh, xy,
    occlusion_s, altitude, horizontal, height, crown_radius)``.
    """
    from sim.worldgen.dressing import shrub_mesh

    total = _path_length(route)
    combos = []
    for along in (0.46, 0.40, 0.52, 0.58):
        for lateral in (1.3, 1.1):
            for side in (1.0, -1.0):
                combos.append((along, lateral * side))
    rng.shuffle(combos)
    probes = 0
    for index, (along, lateral) in enumerate(combos):
        point, direction = _point_at_distance(route, along * total)
        normal = (-direction[1], direction[0])
        x = point[0] + normal[0] * lateral
        y = point[1] + normal[1] * lateral
        z = surface.height(x, y)
        for height, crown, base_radius in _OCCLUDER_SHAPES:
            shrub = shrub_mesh("follow_occluder", x, y, z, height=height,
                               crown_radius=crown, base_radius=base_radius, seed=seed + index)
            top = max(proxy.pose[2] + proxy.size[-1] / 2.0 for proxy in shrub.proxies)
            for horizontal, rise in _OCCLUDER_VIEWS:
                probes += 1
                altitude = top + rise
                occluded, lane, column, body = _follow_probe(
                    route, speed, shrub, surface, body_box, torso_z, altitude, horizontal)
                if (OCCLUSION_BAND_S[0] <= occluded <= OCCLUSION_BAND_S[1]
                        and lane >= 2.4 and column >= 1.8 and body >= 0.15):
                    return shrub, (x, y), occluded, altitude, horizontal, height, crown
            if probes >= _OCCLUDER_PROBE_LIMIT:
                return None
    return None


def _vantage_probe(surface, target_proxies: Sequence[ProxyShape],
                   targets: Sequence[tuple[str, tuple[float, float, float], int]], transit: float,
                   seed: int) -> bool:
    """The validator's vantage search must succeed for every mission role."""
    world = _probe_world(target_proxies)
    ground = Ground.from_surface(surface)
    camera = Camera()
    for model, point, index in targets:
        rng = random.Random(seed * 17 + index)
        vantage = plan_vantage(world, ground, camera, model, point, rng, transit_alt=transit,
                               bounds=PROCEDURAL_VANTAGE_BOUNDS_M, attempts=200)
        if vantage is None:
            return False
    return True


# ---------------------------------------------------------------- sampler


def _transit_altitude_for(top: float) -> float:
    """Procedural forest rule: >= cruise_min 26 m, >= tallest top + 5.9 m, <= 36 m."""
    required = max(26.0, top + 5.9)
    if required > 36.0:
        raise ValueError(f"forest scenery needs {required:.2f} m transit altitude, above 36 m")
    return required


def _sample_attempt(seed: int, rng: random.Random, attempt: int,
                    rejections: Counter) -> ForestLayout | None:
    inset = PROCEDURAL_ACTOR_INSET_M
    fields = _terrain_fields(rng)
    trails = _trail_network(rng)
    if min(math.hypot(x, y) for x, y in trails[0]) > 15.0:
        rejections["main trail misses home"] += 1
        return None
    creeks = _creek_network(rng)
    if min(distance_to_paths((0.0, 0.0), creeks), 99.0) < 20.0:
        rejections["creek too close to home"] += 1
        return None
    clearings = _clearings(rng, creeks)
    if len(clearings) < 4:
        rejections["clearing placement"] += 1
        return None
    camps = _camps(rng, trails, creeks, clearings)
    if not camps:
        rejections["camp placement"] += 1
        return None

    # Follow route: a long trail slice whose start is at least 100 m from home.
    follow_route = None
    follow_speed = 1.25
    for _ in range(120):
        trail = rng.choice(trails)
        total = _path_length(trail)
        start = rng.uniform(FOLLOW_START_MIN_M + 5.0, max(FOLLOW_START_MIN_M + 6.0, total - 220.0))
        route = _slice_path(trail, start, rng.uniform(140.0, 210.0))
        if len(route) < 3 or _path_length(route) < 130.0:
            continue
        if math.hypot(*route[0]) < FOLLOW_START_MIN_M:
            continue
        if max(max(abs(x), abs(y)) for x, y in route) > inset - 5.0:
            continue
        follow_route = route
        follow_speed = rng.uniform(1.1, 1.35)
        break
    if follow_route is None:
        rejections["follow route placement"] += 1
        return None

    hiker_candidates = _route_candidates(rng, trails)
    hiker_routes: list[PersonPlacement] = []
    quadrants: set[tuple[float, float]] = set()
    for route, quadrant in hiker_candidates:
        if quadrant in quadrants or len(hiker_routes) >= 3:
            continue
        if math.hypot(*route[0]) < 70.0:
            continue
        if max(max(abs(x), abs(y)) for x, y in route) > inset - 5.0:
            continue
        speed = (1.25, 1.0, 1.15)[len(hiker_routes)]
        hiker_routes.append(_person(f"civ_hiker_{len(hiker_routes) + 1:02d}", "hiker",
                                    ("person_orange", "person_blue_kinematic",
                                     "person_green_kinematic")[len(hiker_routes)],
                                    route[0], 0.0, "route", route=route, speed=speed,
                                    mode="pingpong"))
        quadrants.add(quadrant)
    if len(hiker_routes) < 3:
        rejections["hiker quadrants"] += 1
        return None

    missing = _missing_hiker(rng, trails, creeks, clearings, camps, follow_route, inset)
    if missing is None:
        rejections["missing hiker placement"] += 1
        return None
    missing_xy, canopy_xy = missing
    canopy_scale = rng.uniform(1.5, 1.7)

    # Mission clearing: at least 120 m from home, big enough for the scan rectangle.
    mission = None
    for cx, cy, radius in clearings:
        if math.hypot(cx, cy) >= 150.0 and radius >= 16.0:
            mission = (cx, cy, radius)
            break
    if mission is None:
        for cx, cy, radius in clearings:
            if math.hypot(cx, cy) >= MISSION_CLEARING_MIN_M:
                mission = (cx, cy, radius)
                break
    if mission is None:
        rejections["mission clearing distance"] += 1
        return None
    mission_centre = (mission[0], mission[1])
    injured_xy = (mission[0] + rng.uniform(-3.5, 3.5), mission[1] + rng.uniform(-3.5, 3.5))
    casualty_angle = rng.uniform(-math.pi, math.pi)
    casualty_distance = rng.uniform(4.0, 8.0)
    casualty_xy = (injured_xy[0] + casualty_distance * math.cos(casualty_angle),
                   injured_xy[1] + casualty_distance * math.sin(casualty_angle))
    if (math.hypot(*injured_xy) < MISSION_CLEARING_MIN_M
            or math.hypot(*casualty_xy) < MISSION_CLEARING_MIN_M):
        rejections["mission people too close to home"] += 1
        return None

    ranger_route = _slice_path(trails[0], rng.uniform(0.2, 0.4) * _path_length(trails[0]), 90.0)
    if len(ranger_route) < 3 or max(max(abs(x), abs(y)) for x, y in ranger_route) > inset - 5.0:
        rejections["ranger route placement"] += 1
        return None

    main_trail = trails[0]
    base = ForestLayout(
        seed, False, main_trail, creeks[0], mission_centre, mission[2], camps[0],
        _person("civ_missing_hiker_01", "missing_hiker", "person_laying_kinematic", missing_xy,
                rng.uniform(-math.pi, math.pi)),
        canopy_xy,
        _person("civ_injured_01", "injured", "person_sitting", injured_xy,
                rng.uniform(-math.pi, math.pi)),
        tuple(hiker_routes),
        (_person("civ_ranger_01", "ranger", "person_hivis", ranger_route[0], 0.0, "route",
                 route=ranger_route, speed=1.0, mode="pingpong"),),
        (),  # family is placed once the camp scenery is known
        _person("civ_hiker_04", "hiker", "person_green_kinematic", follow_route[0], 0.0, "route",
                "follow_hiker", follow_route, follow_speed, "once"),
        (0.0, 0.0), "follow_occluder", follow_speed,
        FOLLOWER_ALTITUDE_M, FOLLOWER_HORIZONTAL_M, 0.0,
        0, (10.0, 20.0), (),  # trees are instanced scenery, not the canonical primitive field
        fields["terrain_base"], fields["terrain_amplitudes"], fields["terrain_phases"],
        fields["terrain_frequencies"], attempt,
        follow_occluder_radius_m=1.9, follow_occluder_height_m=4.6,
        casualty=_person("civ_casualty_01", "casualty", "person_laying_kinematic", casualty_xy,
                         rng.uniform(-math.pi, math.pi)),
        map=PROCEDURAL_MAP,
        trails=trails, creeks=creeks,
        clearings=tuple((cx, cy, cr) for cx, cy, cr in clearings),
        camps=camps,
        home_pad_radius_m=6.0,
        creek_terrain_path=resample_path(creeks[0], 30.0),
        actor_inset_m=inset,
    )
    surface = forest_surface(base)
    # Trail and creek strips: textured dirt/water draped on the terrain, chunked per tile.
    strip_meshes: list[SceneryMesh] = []
    for index, trail in enumerate(trails):
        strip_meshes.extend(_strip_meshes(f"trail_{index:02d}", trail, TRAIL_HALF_WIDTH_M,
                                          "dirt_trail", surface))
    for index, creek in enumerate(creeks):
        strip_meshes.extend(_strip_meshes(f"creek_{index:02d}", creek, CREEK_HALF_WIDTH_M,
                                          "creek", surface))
    dressing = _sample_forest_dressing(base)
    torso = {model: dressing.appearances[model].envelope(posture)["torso_z_m"]
             for model, posture in (("civ_hiker_04", "standing"), ("civ_missing_hiker_01", "lying"),
                                    ("civ_injured_01", "sitting"), ("civ_casualty_01", "lying"))}
    body_box = _envelope_box(dressing.appearances["civ_hiker_04"].envelope("standing"))

    # Camp scenery and family placement.
    from sim.worldgen.dressing import camp_scenery

    camp_instances: list[SceneryInstance] = []
    camp_meshes: list[SceneryMesh] = []
    family: tuple[PersonPlacement, ...] = ()
    camp_proxies: list[ProxyShape] = []
    for index, camp in enumerate(camps):
        pieces = camp_scenery(camp, 0.0, seed=seed + index)
        instances, meshes = pieces
        ground_z = surface.height(camp[0], camp[1])
        instances = [SceneryInstance(item.asset, item.x, item.y, item.z + ground_z, item.yaw, item.scale)
                     for item in instances]
        meshes = [_lift_mesh(mesh, ground_z, prefix=f"camp{index + 1}_") for mesh in meshes]
        camp_instances.extend(instances)
        camp_meshes.extend(meshes)
        for instance in instances:
            camp_proxies.extend(_instance_proxies(instance))
        for mesh in meshes:
            camp_proxies.extend(mesh.proxies)
        if index == 0:
            family = _camp_family(camp, camp_proxies, rng)
    if len(family) != 4:
        rejections["family placement"] += 1
        return None

    base = replace(base, family_positions=family, sites=_sites(clearings, camps, follow_route,
                                                               missing_xy, injured_xy, casualty_xy))

    # Follow occluder: search placements until the real proxies give the occlusion band.
    found = _search_occluder(rng, follow_route, follow_speed, surface, seed, torso["civ_hiker_04"],
                             body_box)
    if found is None:
        rejections["follow occlusion probe"] += 1
        return None
    shrub, occluder_xy, occluded, follower_altitude, follower_horizontal, occluder_height, \
        occluder_radius = found

    # Trees/rocks/logs.
    tree_grid = _BlockGrid(600.0)
    sapling_grid = _BlockGrid(600.0)
    tree_grid.mark_disc(0.0, 0.0, 14.0)
    sapling_grid.mark_disc(0.0, 0.0, 14.0)
    for trail in trails:
        tree_grid.mark_path(trail, 9.4 + GRID_CELL_M)
        sapling_grid.mark_path(trail, 5.2 + GRID_CELL_M)
    for creek in creeks:
        tree_grid.mark_path(creek, 4.5 + GRID_CELL_M)
        sapling_grid.mark_path(creek, 2.5 + GRID_CELL_M)
    for cx, cy, radius in clearings:
        clearance = 11.0 if (cx, cy) == mission_centre else 4.0
        tree_grid.mark_disc(cx, cy, radius + clearance + GRID_CELL_M)
        sapling_grid.mark_disc(cx, cy, radius + 2.0 + GRID_CELL_M)
    for cx, cy in camps:
        tree_grid.mark_disc(cx, cy, 16.0 + GRID_CELL_M)
        sapling_grid.mark_disc(cx, cy, 8.0 + GRID_CELL_M)
    tree_grid.mark_disc(canopy_xy[0], canopy_xy[1], 12.0 + GRID_CELL_M)
    sapling_grid.mark_disc(canopy_xy[0], canopy_xy[1], 12.0 + GRID_CELL_M)
    tree_grid.mark_disc(occluder_xy[0], occluder_xy[1], 8.0 + GRID_CELL_M)
    sapling_grid.mark_disc(occluder_xy[0], occluder_xy[1], 8.0 + GRID_CELL_M)
    focus = ((0.0, 0.0), *((x, y) for _name, _kind, x, y, _radius in base.sites))
    tree_instances, trees, saplings, tree_top = _tree_field(rng, surface, tree_grid, sapling_grid,
                                                            focus=focus)
    if trees + saplings > TREE_LIMIT:
        rejections["tree budget"] += 1
        return None

    rock_grid = _BlockGrid(600.0)
    rock_grid.mark_disc(0.0, 0.0, 10.0)
    for trail in trails:
        rock_grid.mark_path(trail, 4.5 + GRID_CELL_M)
    for cx, cy, radius in clearings:
        rock_grid.mark_disc(cx, cy, radius + 1.0 + GRID_CELL_M)
    for cx, cy in camps:
        rock_grid.mark_disc(cx, cy, 10.0 + GRID_CELL_M)
    bank_rocks, rock_top = _bank_rocks(rng, creeks, surface, rock_grid)
    logs = _fallen_logs(rng, surface, trails, creeks, clearings, camps)

    canopy = SceneryInstance("tree_pine", canopy_xy[0], canopy_xy[1], surface.height(*canopy_xy),
                             rng.uniform(-math.pi, math.pi), canopy_scale)
    instances = tuple([*tree_instances, canopy, *bank_rocks, *camp_instances])
    meshes = tuple([*strip_meshes, *camp_meshes, shrub, *logs])
    canopy_top = surface.height(*canopy_xy) + ASSETS["tree_pine"].native_height_m * canopy_scale
    max_top = max(tree_top, rock_top, canopy_top,
                  max((_mesh_top(mesh) for mesh in meshes), default=0.0))
    if max_top > TREE_TOP_LIMIT_M:
        rejections["scenery above the 23 m cap"] += 1
        return None

    # Vantage probe for the three mission roles, with the proxies that can actually block them.
    targets = (("civ_missing_hiker_01", 0), ("civ_injured_01", 1), ("civ_casualty_01", 2))
    people_points = {"civ_missing_hiker_01": (missing_xy, "lying"),
                     "civ_injured_01": (injured_xy, "sitting"),
                     "civ_casualty_01": (casualty_xy, "lying")}
    probe_proxies: list[ProxyShape] = list(_instance_proxies(canopy))
    probe_proxies.extend(shrub.proxies)
    for instance in camp_instances:
        probe_proxies.extend(_instance_proxies(instance))
    for mesh in [*camp_meshes, *logs]:
        probe_proxies.extend(mesh.proxies)
    for instance in [*tree_instances, *bank_rocks]:
        if any(math.hypot(instance.x - xy[0][0], instance.y - xy[0][1]) < 45.0
               for xy in people_points.values()):
            probe_proxies.extend(_instance_proxies(instance))
    transit = _transit_altitude_for(max_top)
    target_points = []
    for model, index in targets:
        xy, posture = people_points[model]
        target_points.append((model, (xy[0], xy[1], surface.height(*xy) + torso[model]), index))
    if not _vantage_probe(surface, probe_proxies, target_points, transit, seed):
        rejections["mission vantage probe"] += 1
        return None

    counts = (
        ("instances", len(instances)), ("meshes", len(meshes)), ("trees", trees),
        ("saplings", saplings), ("rocks", len(bank_rocks)),
        ("logs", len(logs)), ("camps", len(camps)),
        ("trail_length_m", int(round(sum(_path_length(trail) for trail in trails)))),
    )
    return replace(base, scenery_instances=instances, scenery_meshes=meshes,
                   scenery_max_top_m=max_top, scenery_counts=counts,
                   expected_occlusion_s=round(occluded, 2),
                   follow_occluder_xy=occluder_xy,
                   follow_occluder_radius_m=occluder_radius,
                   follow_occluder_height_m=occluder_height,
                   follower_altitude_m=follower_altitude,
                   follower_horizontal_m=follower_horizontal)


def _lift_mesh(mesh: SceneryMesh, dz: float, *, prefix: str = "") -> SceneryMesh:
    return SceneryMesh(f"{prefix}{mesh.name}", mesh.material,
                       tuple((x, y, z + dz) for x, y, z in mesh.positions), mesh.normals,
                       mesh.uvs, mesh.faces,
                       proxies=tuple(ProxyShape(proxy.shape,
                                                (proxy.pose[0], proxy.pose[1], proxy.pose[2] + dz)
                                                + proxy.pose[3:], proxy.size)
                                     for proxy in mesh.proxies),
                       texture=mesh.texture)


def _mesh_top(mesh: SceneryMesh) -> float:
    """Highest point of a scenery mesh: its vertices and its collision proxies."""
    top = max((position[2] for position in mesh.positions), default=0.0)
    for proxy in mesh.proxies:
        if proxy.shape == "cylinder":
            top = max(top, proxy.pose[2] + proxy.size[1] / 2.0)
        elif proxy.shape == "box":
            top = max(top, proxy.pose[2] + proxy.size[2] / 2.0)
        else:
            top = max(top, proxy.pose[2] + proxy.size[0])
    return top


def _envelope_box(envelope: dict) -> tuple[float, float, float, float, float, float]:
    return (envelope["x_min_m"], envelope["x_max_m"], envelope["y_min_m"], envelope["y_max_m"],
            max(0.05, envelope.get("z_min_m", 0.0)), envelope["top_z_m"])


def _sample_forest_dressing(layout: ForestLayout):
    from sim.worldgen.dressing import sample_dressing

    return sample_dressing("forest", layout)


def _sites(clearings, camps, follow_route, missing_xy, injured_xy,
           casualty_xy) -> tuple[tuple[str, str, float, float, float], ...]:
    sites = [("clearing_%02d" % (index + 1), "clearing", cx, cy, cr)
             for index, (cx, cy, cr) in enumerate(clearings)]
    sites.extend(("camp_%02d" % (index + 1), "camp", cx, cy, 12.0)
                 for index, (cx, cy) in enumerate(camps))
    sites.append(("follow_route", "follow", follow_route[0][0], follow_route[0][1], 8.0))
    sites.append(("missing_hiker", "person", missing_xy[0], missing_xy[1], 3.0))
    sites.append(("injured", "person", injured_xy[0], injured_xy[1], 3.0))
    sites.append(("casualty", "person", casualty_xy[0], casualty_xy[1], 3.0))
    return tuple(sites)


def sample_forest_layout(seed: int, *,
                         validate: Callable[[ForestLayout], float | None] | None = None
                         ) -> ForestLayout:
    """Sample a validated 1.2 km procedural forest layout for ``seed``."""
    rng = random.Random(seed)
    if validate is None:
        from sim.worldgen import _validate_candidate

        validate = lambda layout: _validate_candidate("forest", layout)
    rejections: Counter[str] = Counter()
    for attempt in range(1, 41):
        layout = _sample_attempt(seed, rng, attempt, rejections)
        if layout is None:
            continue
        accepted = validate(layout)
        if accepted is None:
            from sim.worldgen.validation import LAST_FAILURES

            rejections["validation"] += 1
            if LAST_FAILURES:
                rejections[f"validation: {LAST_FAILURES[0][:80]}"] += 1
            continue
        return replace(layout, expected_occlusion_s=accepted)
    top = ", ".join(f"{reason} x{count}" for reason, count in rejections.most_common(4))
    raise RuntimeError(f"could not sample valid forest layout for seed {seed} after "
                       f"{sum(rejections.values())} attempts; top rejections: {top}")


__all__ = ["PROCEDURAL_ACTOR_INSET_M", "PROCEDURAL_MAX_LEG_M", "PROCEDURAL_VANTAGE_BOUNDS_M",
           "TREE_LIMIT", "TREE_TOP_LIMIT_M", "sample_forest_layout"]
