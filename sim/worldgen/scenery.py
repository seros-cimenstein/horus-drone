"""Scenery for city-scale generated worlds: instanced asset visuals + one merged trimesh collision.

A procedural world's cheap surroundings (trees, rocks, lamps, buildings, roads, trail strips)
are drawn by one ``static`` visual-only model per 100 m tile. Each tile model carries

* one ``<visual>`` per placed asset instance, referencing the shared asset mesh from the asset
  registry (Ogre-Next auto-instances repeated mesh URIs) with the instance pose, yaw and scale
  composed with the registry visual's canonical ``scale``, submesh and PBR material;
* one ``<visual>`` per merged, textured :class:`SceneryMesh` (OBJ + MTL with ``map_Kd``).

Every :class:`~sim.worldgen.proxy.ProxyShape` of the world is triangulated into a single
``<collision name="scenery_collision">`` OBJ trimesh in one static ``scenery_collision`` model.
One merged mesh rather than one per tile: on Koko, 144 overlapping per-tile collision meshes
stalled the world at RTF ~0 as soon as rendering sensors ran (9 tiles: 0.44, 72: 0.27), while
the single merged mesh measured RTF 0.41, the same as no scenery collision at all.

Collision OBJs carry per-vertex normals (``vn`` + ``f a//a``) because dartsim silently drops
normal-less submeshes and then segfaults. The same proxies are written to
``<stem>.scenery.json`` next to the world SDF, where
:func:`sim.worldgen.geometry.load_collisions` turns them into static
:class:`~sim.worldgen.geometry.Primitive` entries named
``scenery_collision/scenery_collision/<label>_<n>`` so clearance, line-of-sight and max-top
queries keep working on procedural worlds.

Everything in this module is deterministic: tiles are emitted in ``(ix, iy)`` order and keep
the caller's instance/mesh order within a tile; proxies follow the same tile order.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Mapping, Sequence

from sim.worldgen.geometry import Primitive, scenery_sidecar_text
from sim.worldgen.proxy import ProxyShape
from sim.worldgen.sdf import SCENERY_COLLIDE_BITMASK, collide_surface, f

SCENERY_COLLISION_MODEL = "scenery_collision"

DEFAULT_TILE_M = 100.0
CYLINDER_SIDES = 8  # octagonal prisms for cylinder proxies; cheap and hull-conservative
_NAME = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class SceneryInstance:
    """One asset placement: ``asset`` is a key of the asset registry (``assets.ASSETS``)."""

    asset: str
    x: float
    y: float
    z: float = 0.0
    yaw: float = 0.0
    scale: float | tuple[float, float, float] = 1.0


@dataclass(frozen=True, slots=True)
class SceneryMesh:
    """A merged, textured mesh in world coordinates (building, road, sidewalk, trail strip...).

    ``positions``/``normals``/``uvs`` are parallel per-vertex arrays; ``faces`` holds
    zero-based triangle indices. ``material`` names the OBJ/MTL material; ``texture`` is the
    key of a PNG in the textures mapping given to :func:`emit_scenery` (``None`` renders the
    material's plain colour). ``proxies`` are world-space collision/oracle hulls; empty means
    visual-only scenery (road paint, water surfaces).
    """

    name: str
    material: str
    positions: tuple[tuple[float, float, float], ...]
    normals: tuple[tuple[float, float, float], ...]
    uvs: tuple[tuple[float, float], ...]
    faces: tuple[tuple[int, int, int], ...]
    proxies: tuple[ProxyShape, ...] = ()
    texture: str | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"mesh name {self.name!r} must be a plain file name")
        if not _NAME.fullmatch(self.material):
            raise ValueError(f"material name {self.material!r} must be a plain file name")
        object.__setattr__(self, "positions",
                           tuple(tuple(float(c) for c in p) for p in self.positions))
        object.__setattr__(self, "normals",
                           tuple(tuple(float(c) for c in n) for n in self.normals))
        object.__setattr__(self, "uvs", tuple(tuple(float(c) for c in uv) for uv in self.uvs))
        object.__setattr__(self, "faces",
                           tuple(tuple(int(i) for i in face) for face in self.faces))
        count = len(self.positions)
        if count < 3:
            raise ValueError(f"mesh {self.name!r} needs at least one triangle")
        if len(self.normals) != count or len(self.uvs) != count:
            raise ValueError(f"mesh {self.name!r} needs one normal and one uv per position")
        if not self.faces:
            raise ValueError(f"mesh {self.name!r} needs at least one triangle")
        for face in self.faces:
            if len(face) != 3 or any(index < 0 or index >= count for index in face):
                raise ValueError(f"mesh {self.name!r} has an invalid face {face!r}")


@dataclass(frozen=True, slots=True)
class SceneryBundle:
    """Generated scenery: tile models, world files and the world-space collision proxies."""

    sdf: str
    files: dict[str, bytes | str]
    proxies: tuple[Primitive, ...]


def _registry(assets: Mapping[str, object] | None) -> Mapping[str, object]:
    if assets is not None:
        return assets
    from sim.worldgen.assets import ASSETS  # imported lazily: the registry is data, not a dependency

    return ASSETS


def _tile_of(x: float, y: float, tile_m: float) -> tuple[int, int]:
    return math.floor(x / tile_m), math.floor(y / tile_m)


def _rotation(roll: float, pitch: float, yaw: float) -> tuple[tuple[float, float, float], ...]:
    """SDF's Rz(yaw) Ry(pitch) Rx(roll) rotation matrix (same convention as geometry)."""
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _coord(value: float) -> str:
    """SDF number text for a pose coordinate (never writes ``-0``)."""
    return f(value if value != 0.0 else 0.0)


def _scale_parts(scale: float | tuple[float, float, float]) -> tuple[float, float, float]:
    if isinstance(scale, (int, float)):
        value = float(scale)
        if value <= 0.0:
            raise ValueError(f"instance scale must be positive, got {scale!r}")
        return value, value, value
    parts = tuple(float(value) for value in scale)
    if len(parts) != 3 or any(value <= 0.0 for value in parts):
        raise ValueError(f"instance scale must be a positive float or 3-tuple, got {scale!r}")
    return parts  # type: ignore[return-value]


def _hull(proxy: ProxyShape) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Triangulated hull of a proxy in world coordinates (deterministic vertex/face order)."""
    rotation = _rotation(*proxy.pose[3:])
    cx, cy, cz = proxy.pose[:3]

    def place(local: tuple[float, float, float]) -> tuple[float, float, float]:
        x, y, z = local
        return (cx + rotation[0][0] * x + rotation[0][1] * y + rotation[0][2] * z,
                cy + rotation[1][0] * x + rotation[1][1] * y + rotation[1][2] * z,
                cz + rotation[2][0] * x + rotation[2][1] * y + rotation[2][2] * z)

    if proxy.shape == "box":
        hx, hy, hz = (value / 2.0 for value in proxy.size)
        corners = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
                   (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]
        quads = ((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7))
        faces = [triangle for quad in quads for triangle in
                 ((quad[0], quad[1], quad[2]), (quad[0], quad[2], quad[3]))]
        return [place(corner) for corner in corners], faces

    if proxy.shape == "cylinder":
        radius, length = proxy.size
        half = length / 2.0
        ring = [(radius * math.cos(2.0 * math.pi * k / CYLINDER_SIDES),
                 radius * math.sin(2.0 * math.pi * k / CYLINDER_SIDES)) for k in range(CYLINDER_SIDES)]
        bottom = [place((x, y, -half)) for x, y in ring]
        top = [place((x, y, half)) for x, y in ring]
        faces = []
        for k in range(CYLINDER_SIDES):
            nxt = (k + 1) % CYLINDER_SIDES
            faces.extend(((k, nxt, CYLINDER_SIDES + nxt), (k, CYLINDER_SIDES + nxt, CYLINDER_SIDES + k)))
        for k in range(1, CYLINDER_SIDES - 1):
            faces.append((0, k + 1, k))                                  # bottom cap (faces -z)
            faces.append((CYLINDER_SIDES, CYLINDER_SIDES + k, CYLINDER_SIDES + k + 1))  # top cap
        return bottom + top, faces

    # sphere: an octahedron hull (6 vertices, 8 closed triangles)
    radius = proxy.size[0]
    axes = [(radius, 0.0, 0.0), (-radius, 0.0, 0.0), (0.0, radius, 0.0), (0.0, -radius, 0.0),
            (0.0, 0.0, radius), (0.0, 0.0, -radius)]
    faces = [(0, 2, 4), (2, 1, 4), (1, 3, 4), (3, 0, 4),
             (2, 0, 5), (1, 2, 5), (3, 1, 5), (0, 3, 5)]
    return [place(axis) for axis in axes], faces


def collision_obj(proxies: Sequence[ProxyShape]) -> str:
    """A merged collision OBJ (tile-local triangles) with one normal per vertex.

    The output always contains ``vn`` entries and ``f a//a`` faces: dartsim drops submeshes
    without normals and then segfaults on them.
    """
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for proxy in proxies:
        base = len(vertices)
        hull_vertices, hull_faces = _hull(proxy)
        vertices.extend(hull_vertices)
        faces.extend(tuple(base + index for index in face) for face in hull_faces)
    normals = [[0.0, 0.0, 0.0] for _ in vertices]
    for a, b, c in faces:
        ax, ay, az = vertices[a]
        bx, by, bz = vertices[b]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = vertices[c][0] - ax, vertices[c][1] - ay, vertices[c][2] - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        for index in (a, b, c):
            normals[index][0] += nx
            normals[index][1] += ny
            normals[index][2] += nz
    lines = ["# HORUS scenery collision hull (per-vertex normals required by dartsim)"]
    for x, y, z in vertices:
        lines.append(f"v {_coord(x)} {_coord(y)} {_coord(z)}")
    for nx, ny, nz in normals:
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length == 0.0:
            nx, ny, nz = 0.0, 0.0, 1.0
        else:
            nx, ny, nz = nx / length, ny / length, nz / length
        lines.append(f"vn {_coord(nx)} {_coord(ny)} {_coord(nz)}")
    for a, b, c in faces:
        lines.append(f"f {a + 1}//{a + 1} {b + 1}//{b + 1} {c + 1}//{c + 1}")
    return "\n".join(lines) + "\n"


def _mesh_obj(mesh: SceneryMesh) -> str:
    lines = [f"# HORUS scenery mesh {mesh.name}", f"o {mesh.name}", f"mtllib {mesh.name}.mtl"]
    lines.extend(f"v {_coord(x)} {_coord(y)} {_coord(z)}" for x, y, z in mesh.positions)
    lines.extend(f"vt {f(u)} {f(v)}" for u, v in mesh.uvs)
    lines.extend(f"vn {_coord(x)} {_coord(y)} {_coord(z)}" for x, y, z in mesh.normals)
    lines.append(f"usemtl {mesh.material}")
    lines.extend(f"f {a + 1}/{a + 1}/{a + 1} {b + 1}/{b + 1}/{b + 1} {c + 1}/{c + 1}/{c + 1}"
                 for a, b, c in mesh.faces)
    return "\n".join(lines) + "\n"


def _mesh_mtl(mesh: SceneryMesh) -> str:
    lines = [f"# HORUS scenery material {mesh.material}", f"newmtl {mesh.material}",
             "Ka 0.2 0.2 0.2", "Kd 0.72 0.72 0.72", "Ks 0.04 0.04 0.04", "Ns 12"]
    if mesh.texture is not None:
        lines.append(f"map_Kd ../materials/textures/{mesh.texture}.png")
    return "\n".join(lines) + "\n"


def _mesh_visual(mesh: SceneryMesh, world_name: str, ox: float, oy: float) -> str:
    uri = f"model://{world_name}_scenery/meshes/{mesh.name}.obj"
    return (f'<visual name="{mesh.name}"><pose>{_coord(-ox)} {_coord(-oy)} 0 0 0 0</pose>'
            f"<geometry><mesh><uri>{uri}</uri></mesh></geometry></visual>")


def _visual_material(visual) -> str:
    """The PBR material block for a registry visual, or "" to use the mesh's own material.

    Textured assets must state the diffuse colour and metalness/roughness explicitly: sdformat
    defaults diffuse to black and metalness to 1.0 (raw metal), either of which renders an
    albedo-only surface black. Untextured assets keep their embedded COLLADA/MTL material.
    Mirrors ``sim.worldgen.assets._material_sdf``.
    """
    if visual.albedo_map is None:
        return ""
    normal = f"<normal_map>{visual.normal_map}</normal_map>" if visual.normal_map else ""
    double_sided = "<double_sided>true</double_sided>" if visual.double_sided else ""
    return (f"<material><diffuse>1 1 1 1</diffuse>"
            f"<pbr><metal><albedo_map>{visual.albedo_map}</albedo_map>{normal}"
            f"<metalness>{f(float(visual.metalness))}</metalness>"
            f"<roughness>{f(float(visual.roughness))}</roughness>"
            f"</metal></pbr>{double_sided}</material>")


def _instance_visuals(instance: SceneryInstance, asset, index: int, ox: float,
                      oy: float) -> str:
    """One instanced ``<visual>`` per registry visual of ``asset``, posed and scaled.

    ``AssetVisual.scale`` (mesh units -> metres) multiplies the instance scale in the mesh
    ``<scale>``; the visual pose translation is multiplied by the instance scale only, then
    rotated by the instance yaw (the registry's ``Asset.proxies`` use the same frame).
    """
    sx, sy, sz = _scale_parts(instance.scale)
    cos_yaw, sin_yaw = math.cos(instance.yaw), math.sin(instance.yaw)
    parts = []
    for slot, visual in enumerate(asset.visuals):
        px, py, pz = (float(value) for value in visual.pose[:3])
        wx = instance.x + (px * sx) * cos_yaw - (py * sy) * sin_yaw
        wy = instance.y + (px * sx) * sin_yaw + (py * sy) * cos_yaw
        wz = instance.z + pz * sz
        yaw = instance.yaw + float(visual.pose[5])
        name = f"{instance.asset}_{index}" if len(asset.visuals) == 1 else f"{instance.asset}_{index}_{slot}"
        mesh_scale = float(visual.scale) * sx, float(visual.scale) * sy, float(visual.scale) * sz
        geometry = f"<mesh><uri>{visual.mesh_uri}</uri>"
        if mesh_scale != (1.0, 1.0, 1.0):
            geometry += f"<scale>{f(mesh_scale[0])} {f(mesh_scale[1])} {f(mesh_scale[2])}</scale>"
        if visual.submesh:
            geometry += f"<submesh><name>{visual.submesh}</name></submesh>"
        geometry += "</mesh>"
        shadows = "" if visual.cast_shadows else "<cast_shadows>false</cast_shadows>"
        parts.append(f'<visual name="{name}"><pose>{_coord(wx - ox)} {_coord(wy - oy)} {_coord(wz)} '
                     f'{_coord(float(visual.pose[3]))} {_coord(float(visual.pose[4]))} {_coord(yaw)}</pose>'
                     f"<geometry>{geometry}</geometry>{_visual_material(visual)}{shadows}</visual>")
    return "".join(parts)


def emit_scenery(world_name: str, instances: Sequence[SceneryInstance],
                 meshes: Sequence[SceneryMesh], *, tile_m: float = DEFAULT_TILE_M,
                 assets: Mapping[str, object] | None = None,
                 textures: Mapping[str, bytes] | None = None,
                 stem: str | None = None) -> SceneryBundle:
    """Emit tile models, mesh/texture files and collision proxies for one generated world.

    ``world_name`` names the mesh library (``models/<world_name>_scenery/``); ``stem`` names
    the sidecar (``<stem>.scenery.json``, default ``world_name``) and must match the world SDF
    stem. ``instances`` and ``meshes`` may be in any order; output keeps tile order
    ``(ix, iy)`` and the caller's order within each tile.
    """
    if not _NAME.fullmatch(world_name):
        raise ValueError(f"world name {world_name!r} must be a plain file name")
    if tile_m <= 0.0:
        raise ValueError("tile_m must be positive")
    stem = stem or world_name
    registry = _registry(assets)
    texture_bank = dict(textures or {})

    seen_meshes: set[str] = set()
    for mesh in meshes:
        if mesh.name in seen_meshes:
            raise ValueError(f"duplicate scenery mesh name {mesh.name!r}")
        seen_meshes.add(mesh.name)

    tile_instances: dict[tuple[int, int], list[tuple[SceneryInstance, object]]] = {}
    tile_meshes: dict[tuple[int, int], list[SceneryMesh]] = {}
    tile_proxies: dict[tuple[int, int], list[tuple[str, ProxyShape]]] = {}
    for instance in instances:
        if instance.asset not in registry:
            raise KeyError(f"unknown scenery asset {instance.asset!r}")
        asset = registry[instance.asset]
        tile_instances.setdefault(_tile_of(instance.x, instance.y, tile_m), []).append(
            (instance, asset))
        for proxy in asset.proxies:
            placed = proxy.transformed(instance.x, instance.y, instance.z, instance.yaw,
                                       instance.scale)
            tile_proxies.setdefault(_tile_of(placed.pose[0], placed.pose[1], tile_m), []).append(
                (instance.asset, placed))
    for mesh in meshes:
        if mesh.texture is not None and mesh.texture not in texture_bank:
            raise KeyError(f"mesh {mesh.name!r} references missing texture {mesh.texture!r}")
        cx = sum(position[0] for position in mesh.positions) / len(mesh.positions)
        cy = sum(position[1] for position in mesh.positions) / len(mesh.positions)
        tile_meshes.setdefault(_tile_of(cx, cy, tile_m), []).append(mesh)
        for proxy in mesh.proxies:
            tile_proxies.setdefault(_tile_of(proxy.pose[0], proxy.pose[1], tile_m), []).append(
                (mesh.name, proxy))

    files: dict[str, bytes | str] = {}
    for mesh in meshes:
        files[f"models/{world_name}_scenery/meshes/{mesh.name}.obj"] = _mesh_obj(mesh)
        files[f"models/{world_name}_scenery/meshes/{mesh.name}.mtl"] = _mesh_mtl(mesh)
        if mesh.texture is not None:
            files[f"models/{world_name}_scenery/materials/textures/{mesh.texture}.png"] = \
                texture_bank[mesh.texture]

    models: list[str] = []
    proxies: list[Primitive] = []
    collision_model = SCENERY_COLLISION_MODEL
    for tile in sorted(set(tile_instances) | set(tile_meshes) | set(tile_proxies)):
        ix, iy = tile
        ox, oy = ix * tile_m, iy * tile_m
        parts: list[str] = []
        for index, (instance, asset) in enumerate(tile_instances.get(tile, ())):
            parts.append(_instance_visuals(instance, asset, index, ox, oy))
        for mesh in tile_meshes.get(tile, ()):
            parts.append(_mesh_visual(mesh, world_name, ox, oy))
        # The proxy label (asset key or mesh name) stays in the oracle name so obstacle
        # classification (campaign hazard tokens: tree/canopy/...) keeps working.
        for label, proxy in tile_proxies.get(tile, ()):
            proxies.append(Primitive(name=f"{collision_model}/scenery_collision/{label}_{len(proxies)}",
                                     model=f"{collision_model}/scenery_collision",
                                     shape=proxy.shape, transform=proxy.pose,
                                     size=proxy.size, static=True))
        if parts:
            models.append(f'<model name="scenery_{ix}_{iy}"><static>true</static>'
                          f"<pose>{_coord(ox)} {_coord(oy)} 0 0 0 0</pose>"
                          f'<link name="scenery">{"".join(parts)}</link></model>')
    if proxies:
        files[f"models/{world_name}_scenery/meshes/{collision_model}.obj"] = collision_obj(
            [ProxyShape(primitive.shape, primitive.transform, primitive.size) for primitive in proxies])
        uri = f"model://{world_name}_scenery/meshes/{collision_model}.obj"
        models.append(f'<model name="{collision_model}"><static>true</static>'
                      '<link name="scenery"><collision name="scenery_collision"><geometry><mesh>'
                      f"<uri>{uri}</uri></mesh></geometry>{collide_surface(SCENERY_COLLIDE_BITMASK)}"
                      "</collision></link></model>")

    files[f"{stem}.scenery.json"] = scenery_sidecar_text(proxies)
    return SceneryBundle("\n".join(models) + ("\n" if models else ""), files, tuple(proxies))


__all__ = ["DEFAULT_TILE_M", "SceneryBundle", "SceneryInstance", "SceneryMesh", "collision_obj",
           "emit_scenery"]
