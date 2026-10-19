"""Per-tile scenery of the procedural city: merged textured meshes (facades, roofs, roads,
sidewalks and kerbs, crosswalks, plazas, parks, shelters and their platforms), instanced
registry assets (benches, trees, lamps, parked cars) and the placed clutter, emitted through
:func:`sim.worldgen.scenery.emit_scenery` with one trimesh collision per tile.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import math
import random

from sim.worldgen.assets import ASSETS
from sim.worldgen.dressing import clutter_scenery
from sim.worldgen.layout import URBAN_SIDEWALK_WIDTH_M, UrbanLayout, road_half_width
from sim.worldgen.materials import (
    CROSSWALK_PITCH_M, FACADE_BAY_M, GROUND_TILE_M, PARK_PATH_WIDTH_M, PAVER_TILE_M, PLAZA_TILE_M,
    ROAD_TILE_M, ROOF_TILE_M, scene_textures,
)
from sim.worldgen.proxy import ProxyShape
from sim.worldgen.scenery import SceneryInstance, SceneryMesh, emit_scenery
from sim.worldgen.urban_grid import (
    CROSSWALK_ARM_M, CROSSWALK_HALF_LENGTH_M, MAP_HALF_M, SHELTER_LENGTH_M,
)
from sim.worldgen.vehicles import vehicle_asset


TILE_M = 100.0


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


_TileMeshes = dict[tuple[int, int], _Mesh]


def _building_meshes(layout: UrbanLayout, textures: bool
                     ) -> tuple[dict[tuple[tuple[int, int], str], _Mesh], _TileMeshes]:
    """Facade walls per tile and style, and roofs (parapets, rooftop equipment) per tile."""
    facades: dict[tuple[tuple[int, int], str], _Mesh] = {}
    roofs: _TileMeshes = {}
    style_rng = random.Random(layout.seed ^ 0x5EED)
    styles = ("brick", "concrete", "glass", "stucco", "panel")
    for building in layout.buildings:
        style = style_rng.choices(styles, (3, 3, 1, 2, 1))[0]
        key = _tile_key(building.x, building.y)
        mesh = facades.get((key, style))
        if mesh is None:
            mesh = _Mesh(f"facade_{style}_{key[0]}_{key[1]}", f"facade_{style}",
                         f"facade_{style}" if textures else None)
            facades[(key, style)] = mesh
        x, y, sx, sy, h = building.x, building.y, building.sx, building.sy, building.height
        for ax, ay, bx, by, nx, ny in (
            (x - sx / 2, y + sy / 2, x - sx / 2, y - sy / 2, -1.0, 0.0),
            (x + sx / 2, y - sy / 2, x + sx / 2, y + sy / 2, 1.0, 0.0),
            (x - sx / 2, y - sy / 2, x + sx / 2, y - sy / 2, 0.0, -1.0),
            (x + sx / 2, y + sy / 2, x - sx / 2, y + sy / 2, 0.0, 1.0),
        ):
            mesh.wall(ax, ay, bx, by, 0.0, h, nx, ny, FACADE_BAY_M)
        mesh.proxies.append(ProxyShape("box", (x, y, h / 2.0, 0.0, 0.0, 0.0), (sx, sy, h)))

        roof = roofs.get(key)
        if roof is None:
            roof = _Mesh(f"roof_{key[0]}_{key[1]}", "roof_gravel",
                         "roof_gravel" if textures else None)
            roofs[key] = roof
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
    return facades, roofs


def _road_meshes(layout: UrbanLayout, textures: bool) -> tuple[_TileMeshes, _TileMeshes]:
    """Asphalt carriageways and sidewalk pavers (with the kerb faces) per tile."""
    city = layout.city
    asphalt: _TileMeshes = {}
    pavement: _TileMeshes = {}
    asphalt_tex = "asphalt_center_dashed" if textures else None
    pavement_tex = "pavers" if textures else None
    for x in layout.roads_x:
        half = road_half_width(x, city.arterial_roads_x)
        _store_rect(asphalt, "asphalt", "asphalt_center_dashed", asphalt_tex,
                    x - half, x + half, -MAP_HALF_M, MAP_HALF_M, 0.035,
                    lambda px, py, x=x: ((px - x) / ROAD_TILE_M + 0.5, py / ROAD_TILE_M))
        for side in (-1.0, 1.0):
            inner, outer = x + side * half, x + side * (half + URBAN_SIDEWALK_WIDTH_M)
            _store_rect(pavement, "sidewalk", "pavers", pavement_tex, min(inner, outer),
                        max(inner, outer), -MAP_HALF_M, MAP_HALF_M, 0.06,
                        lambda px, py: (px / PAVER_TILE_M, py / PAVER_TILE_M))
    for y in layout.roads_y:
        half = road_half_width(y, city.arterial_roads_y)
        _store_rect(asphalt, "asphalt", "asphalt_center_dashed", asphalt_tex,
                    -MAP_HALF_M, MAP_HALF_M, y - half, y + half, 0.035,
                    lambda px, py, y=y: (px / ROAD_TILE_M, (py - y) / ROAD_TILE_M + 0.5))
        for side in (-1.0, 1.0):
            inner, outer = y + side * half, y + side * (half + URBAN_SIDEWALK_WIDTH_M)
            _store_rect(pavement, "sidewalk", "pavers", pavement_tex, -MAP_HALF_M, MAP_HALF_M,
                        min(inner, outer), max(inner, outer), 0.06,
                        lambda px, py: (px / PAVER_TILE_M, py / PAVER_TILE_M))

    # curbs: vertical faces at both sidewalk inner edges, facing the carriageway
    rows = [(iy, iy * TILE_M, (iy + 1) * TILE_M)
            for iy in range(math.floor(-MAP_HALF_M / TILE_M), math.floor(MAP_HALF_M / TILE_M))]
    for x in layout.roads_x:
        half = road_half_width(x, city.arterial_roads_x)
        for side in (-1.0, 1.0):
            inner = x + side * half
            for iy, cy0, cy1 in rows:
                mesh = pavement.get((math.floor(inner / TILE_M), iy))
                if mesh is None:
                    mesh = _Mesh(f"sidewalk_{math.floor(inner / TILE_M)}_{iy}", "pavers",
                                 pavement_tex)
                    pavement[(math.floor(inner / TILE_M), iy)] = mesh
                mesh.wall(inner, cy0, inner, cy1, 0.0, 0.06, -side, 0.0, PAVER_TILE_M)
    for y in layout.roads_y:
        half = road_half_width(y, city.arterial_roads_y)
        for side in (-1.0, 1.0):
            inner = y + side * half
            for ix in range(math.floor(-MAP_HALF_M / TILE_M), math.floor(MAP_HALF_M / TILE_M)):
                cx0, cx1 = ix * TILE_M, (ix + 1) * TILE_M
                mesh = pavement.get((ix, math.floor(inner / TILE_M)))
                if mesh is None:
                    mesh = _Mesh(f"sidewalk_{ix}_{math.floor(inner / TILE_M)}", "pavers",
                                 pavement_tex)
                    pavement[(ix, math.floor(inner / TILE_M))] = mesh
                mesh.wall(cx0, inner, cx1, inner, 0.0, 0.06, 0.0, -side, PAVER_TILE_M)
    return asphalt, pavement


def _crosswalk_meshes(layout: UrbanLayout, textures: bool) -> _TileMeshes:
    """Zebra patches on both arms of every junction, per tile."""
    city = layout.city
    zebra: _TileMeshes = {}
    zebra_tex = "crosswalk" if textures else None
    for x in layout.roads_x:
        for y in layout.roads_y:
            extent = max(road_half_width(x, city.arterial_roads_x),
                         road_half_width(y, city.arterial_roads_y))
            offset = extent + CROSSWALK_ARM_M
            half_x = road_half_width(x, city.arterial_roads_x)
            half_y = road_half_width(y, city.arterial_roads_y)
            for side in (-1.0, 1.0):
                cy = y + side * offset
                _store_rect(zebra, "crosswalk", "crosswalk", zebra_tex, x - half_x - 0.9,
                            x + half_x + 0.9, cy - CROSSWALK_HALF_LENGTH_M,
                            cy + CROSSWALK_HALF_LENGTH_M, 0.05,
                            lambda px, py: (px / CROSSWALK_PITCH_M, py / 1.0))
                cx = x + side * offset
                _store_rect(zebra, "crosswalk", "crosswalk", zebra_tex, cx - CROSSWALK_HALF_LENGTH_M,
                            cx + CROSSWALK_HALF_LENGTH_M, y - half_y - 0.9, y + half_y + 0.9, 0.05,
                            lambda px, py: (py / CROSSWALK_PITCH_M, px / 1.0))
    return zebra


def _open_space_meshes(layout: UrbanLayout, textures: bool
                       ) -> tuple[_TileMeshes, _TileMeshes, _TileMeshes]:
    """Plaza stone, park lawns and the cross of park paths, per tile."""
    city = layout.city
    plazas: _TileMeshes = {}
    lawns: _TileMeshes = {}
    paths: _TileMeshes = {}
    plaza_tex = "plaza_stone" if textures else None
    for plaza in city.plazas:
        _store_rect(plazas, "plaza", "plaza_stone", plaza_tex,
                    plaza.x - plaza.half_size, plaza.x + plaza.half_size,
                    plaza.y - plaza.half_size, plaza.y + plaza.half_size, 0.03,
                    lambda px, py: (px / PLAZA_TILE_M, py / PLAZA_TILE_M))
    lawn_tex = "park_lawn" if textures else None
    path_tex = "park_path" if textures else None
    for park in city.parks:
        _store_rect(lawns, "lawn", "park_lawn", lawn_tex,
                    park.x - park.half_size, park.x + park.half_size,
                    park.y - park.half_size, park.y + park.half_size, 0.015,
                    lambda px, py: (px / GROUND_TILE_M, py / GROUND_TILE_M))
        _store_rect(paths, "path", "park_path", path_tex, park.x - park.half_size,
                    park.x + park.half_size, park.y - 1.1, park.y + 1.1, 0.03,
                    lambda px, py: (py / PARK_PATH_WIDTH_M, px / 1.0))
        _store_rect(paths, "path", "park_path", path_tex, park.x - 1.1, park.x + 1.1,
                    park.y - park.half_size, park.y + park.half_size, 0.03,
                    lambda px, py: (px / PARK_PATH_WIDTH_M, py / 1.0))
    return plazas, lawns, paths


def _scenery_instances(layout: UrbanLayout) -> list[SceneryInstance]:
    """Benches, trees, lamps and parked cars as registry asset instances."""
    city = layout.city
    instances = [SceneryInstance("bench_a", bench.x, bench.y, 0.0, bench.yaw, 1.0)
                 for bench in layout.benches]
    for tree in city.trees:
        asset = ASSETS["tree_oak" if tree.species == "oak" else "tree_pine"]
        instances.append(SceneryInstance(asset.key, tree.x, tree.y, 0.0, 0.0,
                                         round(tree.height / asset.native_height_m, 4)))
    for x, y, yaw in city.lamps:
        instances.append(SceneryInstance("lamp_post", x, y, 0.0, yaw, 1.0))
    for car in layout.parked_cars:
        asset = vehicle_asset(car.type, car.variant)
        yaw = car.yaw + (math.pi / 2 if asset == "bus" else 0.0)
        instances.append(SceneryInstance(asset, car.x, car.y, 0.0, yaw, 1.0))
    return instances


def _build_scenery(layout: UrbanLayout, *, textures: bool
                   ) -> tuple[list[SceneryInstance], list[SceneryMesh], dict[str, bytes]]:
    bank = scene_textures(layout.seed) if textures else {}
    facades, roofs = _building_meshes(layout, textures)
    asphalt, pavement = _road_meshes(layout, textures)
    zebra = _crosswalk_meshes(layout, textures)
    plazas, lawns, paths = _open_space_meshes(layout, textures)
    meshes: list[SceneryMesh] = []
    for mesh in (*facades.values(), *roofs.values(), *asphalt.values(), *zebra.values(),
                 *pavement.values(), *plazas.values(), *lawns.values(), *paths.values()):
        _first(meshes, mesh)
    meshes.extend(_shelter_meshes(layout, textures))
    return _scenery_instances(layout), meshes, bank


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
            glass.box(px, py, 0.0, 2.2, sx, sy, PAVER_TILE_M, yaw)
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
                  (0.0, 0.0, 1.0), [(x0 / PAVER_TILE_M, y0 / PAVER_TILE_M),
                                    (x1 / PAVER_TILE_M, y0 / PAVER_TILE_M),
                                    (x1 / PAVER_TILE_M, y1 / PAVER_TILE_M),
                                    (x0 / PAVER_TILE_M, y1 / PAVER_TILE_M)])
        _first(out, mesh)


def city_scenery(layout: UrbanLayout, world_name: str, *, textures: bool,
                 clutter: list[dict]) -> tuple[str, dict[str, bytes | str]]:
    """Per-tile city scenery (roads, buildings, furniture and the placed clutter) as
    ``(sdf, files)``."""
    instances, meshes, bank = _build_scenery(layout, textures=textures)
    clutter_instances, clutter_meshes = clutter_scenery("urban", clutter)
    if not textures:
        clutter_meshes = [replace(mesh, texture=None) for mesh in clutter_meshes]
    instances.extend(clutter_instances)
    meshes.extend(clutter_meshes)
    bundle = emit_scenery(world_name, instances, meshes, tile_m=TILE_M, textures=bank,
                          stem=world_name)
    return bundle.sdf.rstrip("\n"), bundle.files
