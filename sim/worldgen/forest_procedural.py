"""Sampler and static scenery for the 1.2 km procedural forest.

``sample_forest_layout(seed, validate=...)`` builds a :class:`~sim.worldgen.layout.ForestLayout`
on :data:`~sim.worldgen.surface.PROCEDURAL_MAP` (trail/creek network and the people on it come
from :mod:`sim.worldgen.forest_network`):

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
per-tile trimesh proxies + sidecar), so the physics budget stays at one trimesh per 100 m tile;
``build_forest_procedural`` emits the world from a sampled layout.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
import math
from pathlib import Path
import random

from sim.worldgen.assembly import assemble_world
from sim.worldgen.assets import ASSETS
from sim.worldgen.dressing import Dressing, camp_scenery, clutter_scenery, deadfall_mesh
from sim.worldgen.forest import forest_areas
from sim.worldgen.forest_network import (
    TERRAIN_HALF_M, path_length, point_at_distance, sample_hikers, sample_network, sample_ranger,
)
from sim.worldgen.geometry import CollisionWorld, Primitive
from sim.worldgen.layout import (
    ForestLayout, PersonPlacement, distance_to_paths, forest_ground_rows, forest_surface,
    person_placement, resample_path,
)
from sim.worldgen.materials import lattice_grid, lattice_noise, scene_textures
from sim.worldgen.plan import Ground, checked_transit_altitude
from sim.worldgen.png import solid_png
from sim.worldgen.proxy import ProxyShape
from sim.worldgen.scenario import procedural_manifest_fields
from sim.worldgen.scenery import DEFAULT_TILE_M, SceneryInstance, SceneryMesh, emit_scenery
from sim.worldgen.sdf import include
from sim.worldgen.surface import PROCEDURAL_MAP
from sim.worldgen.textures import terrain_model_files, write_npy
from sim.worldgen.validation import SampleRejected, box_clearance, retry_sample

# ---------------------------------------------------------------- design constants
# Actor routes/wander areas stay 5 m inside the flight profile's +-585 m polygon.
PROCEDURAL_ACTOR_INSET_M = 580.0
# Flight profile: forest trees must keep their tops below 23 m above the home datum.
TREE_TOP_LIMIT_M = 23.0
TREE_LIMIT = 9000                     # tree + sapling instance budget
# Full stand density within FOCUS_RADIUS_M of home; elsewhere only FAR_TREE_DENSITY of it and
# no saplings. Every tree instance costs each drone render sensor a fixed per-item overhead
# regardless of distance, so the far field stays sparse.
FOCUS_RADIUS_M = 120.0
FAR_TREE_DENSITY = 0.3
TRAIL_HALF_WIDTH_M = 1.2              # materials.dirt_trail_texture spans 2.4 m
CREEK_HALF_WIDTH_M = 1.3              # materials.creek_texture spans 2.6 m
STRIP_LIFT_M = 0.03                   # strips ride on the terrain to avoid z-fighting
TREE_HALF_M = 570.0                   # tree field half extent
GRID_CELL_M = 4.0                     # exclusion raster cell


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


# ---------------------------------------------------------------- scenery


def _tree_field(rng: random.Random, surface, tree_grid: _BlockGrid, sapling_grid: _BlockGrid,
                half: float = TREE_HALF_M) -> tuple[list[SceneryInstance], int, int, float]:
    """Instanced trees and saplings with local density variation; no primitive vegetation.

    Stands keep their full density within ``FOCUS_RADIUS_M`` of home; farther away only
    ``FAR_TREE_DENSITY`` of the trees and no saplings remain.
    """
    noise = lattice_grid(rng, 12)
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
            density = lattice_noise(noise, u, v)
            probability = 0.16 if density < 0.40 else (0.55 if density < 0.62 else 0.92)
            near = math.hypot(x, y) <= FOCUS_RADIUS_M
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
        total = path_length(creek)
        along = rng.uniform(0.0, 10.0)
        side = 1.0
        while along < total:
            point, direction = point_at_distance(creek, along)
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
            clearance, _blocker = box_clearance(world, ground, x, y, angle, box)
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
    return tuple(person_placement(f"civ_family_{index:02d}", "family", asset, point, yaw)
                 for index, ((asset, yaw), point) in enumerate(zip(assets, chosen), 1))


class _FlatSurface:
    """Flat z=0 surface for clearance probes that do not depend on the terrain."""

    def height(self, x: float, y: float) -> float:
        return 0.0

    def normal(self, x: float, y: float) -> tuple[float, float, float]:
        return (0.0, 0.0, 1.0)

    def segment_clearance(self, a, b) -> float:
        return min(float(a[2]), float(b[2]))


# ---------------------------------------------------------------- sampler


MAX_ATTEMPTS = 40


def _required_transit(top: float) -> float:
    """Procedural forest rule: >= cruise_min 26 m and >= tallest scenery top + 5.9 m."""
    return max(26.0, top + 5.9)


def transit_altitude(layout: ForestLayout) -> float:
    """The manifest transit altitude, refused above the 36 m transit ceiling."""
    return checked_transit_altitude(_required_transit(layout.scenery_max_top_m), 36.0,
                                    procedural=True)


def _network_strips(trails, creeks, surface) -> list[SceneryMesh]:
    """Trail and creek strips: textured dirt/water draped on the terrain, chunked per tile."""
    strips: list[SceneryMesh] = []
    for index, trail in enumerate(trails):
        strips.extend(_strip_meshes(f"trail_{index:02d}", trail, TRAIL_HALF_WIDTH_M,
                                    "dirt_trail", surface))
    for index, creek in enumerate(creeks):
        strips.extend(_strip_meshes(f"creek_{index:02d}", creek, CREEK_HALF_WIDTH_M,
                                    "creek", surface))
    return strips


def _camp_scenery_and_family(camps, surface, seed: int, rng: random.Random
                             ) -> tuple[list[SceneryInstance], list[SceneryMesh],
                                        tuple[PersonPlacement, ...]]:
    """Camp scenery lifted onto the terrain, and the family placed clear of the first camp."""
    camp_instances: list[SceneryInstance] = []
    camp_meshes: list[SceneryMesh] = []
    family: tuple[PersonPlacement, ...] = ()
    camp_proxies: list[ProxyShape] = []
    for index, camp in enumerate(camps):
        instances, meshes = camp_scenery(camp, 0.0, seed=seed + index)
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
        raise SampleRejected("family placement")
    return camp_instances, camp_meshes, family


def _exclusion_grids(trails, creeks, clearings, camps) -> tuple[_BlockGrid, _BlockGrid, _BlockGrid]:
    """No-place rasters for trees, saplings and creek-bank rocks."""
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
        tree_grid.mark_disc(cx, cy, radius + 4.0 + GRID_CELL_M)
        sapling_grid.mark_disc(cx, cy, radius + 2.0 + GRID_CELL_M)
    for cx, cy in camps:
        tree_grid.mark_disc(cx, cy, 16.0 + GRID_CELL_M)
        sapling_grid.mark_disc(cx, cy, 8.0 + GRID_CELL_M)

    rock_grid = _BlockGrid(600.0)
    rock_grid.mark_disc(0.0, 0.0, 10.0)
    for trail in trails:
        rock_grid.mark_path(trail, 4.5 + GRID_CELL_M)
    for cx, cy, radius in clearings:
        rock_grid.mark_disc(cx, cy, radius + 1.0 + GRID_CELL_M)
    for cx, cy in camps:
        rock_grid.mark_disc(cx, cy, 10.0 + GRID_CELL_M)
    return tree_grid, sapling_grid, rock_grid


def _sample_attempt(seed: int, rng: random.Random, attempt: int) -> ForestLayout:
    inset = PROCEDURAL_ACTOR_INSET_M
    fields = _terrain_fields(rng)
    trails, creeks, clearings, camps = sample_network(rng)
    hikers = sample_hikers(rng, trails, inset)
    ranger = sample_ranger(rng, trails, inset)
    base = ForestLayout(
        seed=seed, canonical=False, trail=trails[0], creek=creeks[0], clearing=clearings[0][:2],
        clearing_radius=clearings[0][2], camp=camps[0],
        hiker_routes=hikers, ranger_routes=(ranger,),
        family_positions=(),  # placed once the camp scenery is known
        # Trees are instanced scenery, not the canonical primitive field.
        tree_count=0, tree_height_range=(10.0, 20.0), fallen_logs=(),
        **fields, attempts=attempt, map=PROCEDURAL_MAP,
        trails=trails, creeks=creeks, clearings=clearings, camps=camps, home_pad_radius_m=6.0,
        creek_terrain_path=resample_path(creeks[0], 30.0), actor_inset_m=inset,
    )
    surface = forest_surface(base)
    strips = _network_strips(trails, creeks, surface)
    camp_instances, camp_meshes, family = _camp_scenery_and_family(camps, surface, seed, rng)
    base = replace(base, family_positions=family)

    tree_grid, sapling_grid, rock_grid = _exclusion_grids(trails, creeks, clearings, camps)
    tree_instances, trees, saplings, tree_top = _tree_field(rng, surface, tree_grid, sapling_grid)
    if trees + saplings > TREE_LIMIT:
        raise SampleRejected("tree budget")
    bank_rocks, rock_top = _bank_rocks(rng, creeks, surface, rock_grid)
    logs = _fallen_logs(rng, surface, trails, creeks, clearings, camps)

    instances = tuple([*tree_instances, *bank_rocks, *camp_instances])
    meshes = tuple([*strips, *camp_meshes, *logs])
    max_top = max(tree_top, rock_top, max((_mesh_top(mesh) for mesh in meshes), default=0.0))
    if max_top > TREE_TOP_LIMIT_M:
        raise SampleRejected("scenery above the 23 m cap")

    counts = (
        ("instances", len(instances)), ("meshes", len(meshes)), ("trees", trees),
        ("saplings", saplings), ("rocks", len(bank_rocks)),
        ("logs", len(logs)), ("camps", len(camps)),
        ("trail_length_m", int(round(sum(path_length(trail) for trail in trails)))),
    )
    return replace(base, scenery_instances=instances, scenery_meshes=meshes,
                   scenery_max_top_m=max_top, scenery_counts=counts)


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


def sample_forest_layout(seed: int, *,
                         validate: Callable[[ForestLayout], list[str]]
                         ) -> ForestLayout:
    """Sample a validated 1.2 km procedural forest layout for ``seed``."""
    rng = random.Random(seed)
    return retry_sample(lambda attempt: _sample_attempt(seed, rng, attempt), validate,
                        attempts=MAX_ATTEMPTS, kind="forest", seed=seed)


def _scenery_bundle(layout: ForestLayout, world_name: str, *, textures: bool,
                    clutter: list[dict]) -> tuple[str, dict[str, bytes | str]]:
    """Instanced scenery + per-tile trimesh collision + sidecar, as ``(sdf, files)``."""
    instances = tuple(layout.scenery_instances)
    meshes = tuple(layout.scenery_meshes)
    if clutter:
        clutter_instances, clutter_meshes = clutter_scenery("forest", clutter)
        instances = instances + tuple(clutter_instances)
        meshes = meshes + tuple(clutter_meshes)
    if textures:
        texture_bank = scene_textures(layout.seed)
    else:
        # Validation builds only need the geometry; the real PNGs are written by the final build.
        texture_bank = {mesh.texture: solid_png((96, 112, 72))
                        for mesh in meshes if mesh.texture is not None}
    bundle = emit_scenery(world_name, instances, meshes, textures=texture_bank, stem=world_name)
    return bundle.sdf.rstrip("\n"), bundle.files


def build_forest_procedural(layout: ForestLayout, world_dir: Path, terrain_model_name: str, *,
                            textures: bool = True, dressing: Dressing
                            ) -> tuple[str, list[dict], list[list[float]], dict[str, bytes | str]]:
    """The generated forest world: terrain mesh, actors and per-tile instanced scenery."""
    rows = [list(row) for row in forest_ground_rows(layout)]
    files = terrain_model_files(rows, layout.seed, terrain_model_name,
                                layout.trail, layout.clearing, textures=textures,
                                clearing_radius=layout.clearing_radius,
                                tint=dressing.environment.ground_tint, map_spec=layout.map,
                                uv_tile_m=8.0)
    files[f"horus_forest_p{layout.seed}_ground.npy"] = write_npy(rows)
    world_name = f"horus_forest_p{layout.seed}"
    world, agents, actor_files = assemble_world(
        "forest", layout, dressing, world_dir, world_name=world_name,
        static_models=[include(terrain_model_name, "forest_terrain", 0, 0, 0)], textures=textures,
        height=forest_surface(layout).height,
        scenery=lambda clutter: _scenery_bundle(layout, world_name, textures=textures,
                                                clutter=clutter))
    files.update(actor_files)
    return world, agents, rows, files


def manifest_extras(layout: ForestLayout) -> tuple[dict, dict]:
    """The generated forest's ``(areas, extra manifest fields)``."""
    areas = forest_areas(layout)
    areas["trails"] = [[[x, y] for x, y in trail] for trail in layout.trails]
    areas["creeks"] = [[[x, y] for x, y in creek] for creek in layout.creeks]
    areas["clearings"] = [{"center": [cx, cy], "radius": radius}
                          for cx, cy, radius in layout.clearings]
    areas["camps"] = [[x, y] for x, y in layout.camps]
    return areas, {**procedural_manifest_fields(layout), "scenery": dict(layout.scenery_counts)}


__all__ = ["PROCEDURAL_ACTOR_INSET_M", "TREE_LIMIT", "TREE_TOP_LIMIT_M", "build_forest_procedural",
           "sample_forest_layout"]
