"""Static collision geometry loading and spatial queries for SDF worlds."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class Primitive:
    """A collision primitive in world coordinates."""

    name: str
    model: str
    shape: str
    transform: tuple[float, float, float, float, float, float]
    size: tuple[float, ...]
    static: bool


def _numbers(text: str | None, count: int, default: Sequence[float]) -> tuple[float, ...]:
    if not text or not text.strip():
        return tuple(default)
    values = tuple(float(value) for value in text.split())
    if len(values) != count:
        raise ValueError(f"expected {count} numbers, got {len(values)} in {text!r}")
    return values


def _pose(element: ET.Element | None) -> tuple[float, float, float, float, float, float]:
    if element is None:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    values = list(_numbers(element.text, 6, (0.0,) * 6))
    if element.get("degrees", "false").strip().lower() == "true":
        values[3:] = (math.radians(value) for value in values[3:])
    return tuple(values)  # type: ignore[return-value]


def _matrix(roll: float, pitch: float, yaw: float) -> tuple[tuple[float, float, float], ...]:
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _compose(
    parent: tuple[float, float, float, float, float, float],
    child: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    """Compose poses using SDF's Rz(yaw) Ry(pitch) Rx(roll) convention."""
    pr, pp, py = parent[3:]
    cr, cp, cy = child[3:]
    rp = _matrix(pr, pp, py)
    rc = _matrix(cr, cp, cy)
    x, y, z = child[:3]
    tx = parent[0] + rp[0][0] * x + rp[0][1] * y + rp[0][2] * z
    ty = parent[1] + rp[1][0] * x + rp[1][1] * y + rp[1][2] * z
    tz = parent[2] + rp[2][0] * x + rp[2][1] * y + rp[2][2] * z
    r = tuple(
        tuple(sum(rp[i][k] * rc[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )
    pitch = math.asin(max(-1.0, min(1.0, -r[2][0])))
    if abs(math.cos(pitch)) > 1e-10:
        roll = math.atan2(r[2][1], r[2][2])
        yaw = math.atan2(r[1][0], r[0][0])
    else:
        roll = 0.0
        yaw = math.atan2(-r[0][1], r[1][1])
    return (tx, ty, tz, roll, pitch, yaw)


def _resolve_model(uri: str, resource_paths: tuple[Path, ...],
                   models: Mapping[str, str]) -> ET.Element | None:
    """The included ``<model>`` element; None for an ``<actor>`` (animated, no collisions)."""
    if not uri.startswith("model://"):
        raise ValueError(f"unsupported SDF include URI {uri!r}; expected model://name")
    model_name = uri[len("model://"):].strip("/")
    if model_name in models:
        return _model_from_text(models[model_name], f"<in-memory {model_name}>")
    for root in resource_paths:
        candidate = root / model_name / "model.sdf"
        if candidate.is_file():
            return _model_from_text(candidate.read_text(encoding="utf-8"), str(candidate))
        # Also permit a resource path that points directly at the model directory.
        candidate = root / "model.sdf"
        if root.name == model_name and candidate.is_file():
            return _model_from_text(candidate.read_text(encoding="utf-8"), str(candidate))
    raise FileNotFoundError(f"could not resolve {uri!r} in {resource_paths!r}")


def _model_from_text(text: str, source: str) -> ET.Element | None:
    root = ET.fromstring(text)
    model = root if root.tag == "model" else root.find("model")
    if model is None:
        if root.find("actor") is not None or root.tag == "actor":
            return None
        raise ValueError(f"SDF include {source} has no top-level <model>")
    return model


def _geometry(collision: ET.Element) -> tuple[str, tuple[float, ...]] | None:
    geometry = collision.find("geometry")
    if geometry is None:
        return None
    for shape in ("box", "sphere", "cylinder", "cone"):
        node = geometry.find(shape)
        if node is None:
            continue
        if shape == "box":
            size = _numbers(node.findtext("size"), 3, (0.0, 0.0, 0.0))
        elif shape == "sphere":
            size = (float(node.findtext("radius", "0")),)
        else:
            size = (float(node.findtext("radius", "0")), float(node.findtext("length", "0")))
        if any(value <= 0.0 for value in size):
            raise ValueError(f"collision geometry has non-positive {shape} size {size!r}")
        return shape, size
    if geometry.find("mesh") is not None:
        return "mesh", ()
    # Planes are unbounded ground; heights come from the shared terrain surface.
    return None


TERRAIN_COLLISION = "terrain_collision"
SCENERY_COLLISION = "scenery_collision"
SCENERY_SIDECAR_SUFFIX = ".scenery.json"


def scenery_sidecar_path(sdf_path: str | Path) -> Path:
    """The ``<stem>.scenery.json`` sidecar that belongs to ``<stem>.sdf``."""
    path = Path(sdf_path)
    return path.parent / f"{path.stem}{SCENERY_SIDECAR_SUFFIX}"


def scenery_sidecar_text(proxies: Sequence[Primitive]) -> str:
    """Serialize world-space scenery proxies for :func:`load_collisions`."""
    payload = {"version": 1, "proxies": [
        {"name": primitive.name, "model": primitive.model, "shape": primitive.shape,
         "transform": [float(value) for value in primitive.transform],
         "size": [float(value) for value in primitive.size]}
        for primitive in proxies
    ]}
    return json.dumps(payload, separators=(",", ":")) + "\n"


def parse_scenery_sidecar(text: str) -> list[Primitive]:
    """Parse a ``<stem>.scenery.json`` sidecar into static primitives (strict, world-space)."""
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("proxies"), list):
        raise ValueError("scenery sidecar must be a JSON object with a 'proxies' array")
    expected = {"box": 3, "cylinder": 2, "sphere": 1}
    primitives: list[Primitive] = []
    for entry in payload["proxies"]:
        if not isinstance(entry, dict):
            raise ValueError(f"scenery sidecar proxy must be an object, got {entry!r}")
        shape = entry.get("shape")
        if shape not in expected:
            raise ValueError(f"scenery sidecar proxy has unsupported shape {shape!r}")
        transform = tuple(float(value) for value in entry.get("transform", ()))
        size = tuple(float(value) for value in entry.get("size", ()))
        if len(transform) != 6:
            raise ValueError(f"scenery sidecar proxy {entry.get('name')!r} needs a 6-number transform")
        if len(size) != expected[shape] or any(value <= 0.0 for value in size):
            raise ValueError(f"scenery sidecar proxy {entry.get('name')!r} has invalid size {size!r}")
        primitives.append(Primitive(name=str(entry.get("name", "scenery_collision/proxy")),
                                    model=str(entry.get("model", "")), shape=shape,
                                    transform=transform, size=size, static=True))
    return primitives


def load_collisions(
    sdf_path: str | Path,
    *,
    resource_paths: Iterable[str | Path] = (),
    include_dynamic: bool = False,
    models: Mapping[str, str] | None = None,
    scenery_proxies: Sequence[Primitive] | None = None,
) -> list[Primitive]:
    """Load bounded collision primitives from an SDF world or model.

    Every static ``<include>`` must resolve (from ``models`` — model name to SDF text — or a
    resource path); an unresolvable include is an error rather than an invisible obstacle.
    Dynamic models (when ``include_dynamic`` is set) are animated agents whose own links carry
    their collision proxies; their nested includes are runtime-provided sub-assemblies that may
    live outside the world package, so they are not followed. Included ``<actor>`` files carry
    no collisions. Collisions named ``terrain_collision`` are the ground, whose height the
    shared terrain surface provides; any other static mesh collider is rejected because it has
    no primitive proxy — except ``scenery_collision``, which is covered by the world-space
    proxies of the ``<stem>.scenery.json`` sidecar next to the SDF (or by ``scenery_proxies``
    when the SDF is generated in memory, as the validator does).
    """
    paths = tuple(Path(path) for path in resource_paths)
    in_memory = dict(models or {})
    root = ET.parse(sdf_path).getroot()
    primitives: list[Primitive] = []

    if scenery_proxies is None:
        sidecar = scenery_sidecar_path(sdf_path)
        scenery_entries = (parse_scenery_sidecar(sidecar.read_text(encoding="utf-8"))
                           if sidecar.is_file() else [])
    else:
        scenery_entries = list(scenery_proxies)
    scenery: dict[str, list[Primitive]] = {}
    for primitive in scenery_entries:
        scenery.setdefault(primitive.model, []).append(primitive)

    def add_model(
        model: ET.Element,
        parent_pose: tuple[float, float, float, float, float, float],
        parent_name: str,
        name_override: str | None = None,
    ) -> None:
        model_name = name_override or model.get("name") or "model"
        model_path = f"{parent_name}/{model_name}" if parent_name else model_name
        world_pose = _compose(parent_pose, _pose(model.find("pose")))
        static = model.findtext("static", "false").strip().lower() == "true"
        if not static and not include_dynamic:
            # All children move with a dynamic parent; do not resolve unrelated
            # external vehicle assets when dynamic geometry was not requested.
            return

        for link in model.findall("link"):
            link_pose = _compose(world_pose, _pose(link.find("pose")))
            for collision in link.findall("collision"):
                collision_geometry = _geometry(collision)
                if collision_geometry is None:
                    continue
                shape, size = collision_geometry
                collision_name = collision.get("name") or "collision"
                if collision_name == TERRAIN_COLLISION:
                    continue  # the ground: heights come from the shared terrain surface
                if shape == "mesh":
                    if collision_name == SCENERY_COLLISION:
                        covering = scenery.get(f"{model_path}/{SCENERY_COLLISION}")
                        if covering:
                            primitives.extend(covering)
                            continue
                        raise ValueError(f"{model_path}/{collision_name}: mesh collision has no "
                                         "primitive proxies (missing sidecar or scenery_proxies)")
                    raise ValueError(f"{model_path}/{collision_name}: mesh collision has no primitive "
                                     "proxy for clearance/line-of-sight checks")
                local_pose = _pose(collision.find("pose"))
                pose = _compose(link_pose, local_pose)
                primitives.append(Primitive(
                    name=f"{model_path}/{collision_name}",
                    model=model_path,
                    shape=shape,
                    transform=pose,
                    size=size,
                    static=static,
                ))

        for child in model:
            if child.tag == "model":
                add_model(child, world_pose, model_path)
            elif child.tag == "include":
                if not static:
                    # A dynamic model is an animated agent: either the drone or one of
                    # the pedestrians/vehicles, whose own link above carries the collision
                    # proxy. Nested includes are the agent's runtime-provided
                    # sub-assemblies (the drone's plugin-supplied airframe and gimbal),
                    # which are not part of the world package and may be absent on a bare
                    # checkout; they are not scenery, so their collisions are not loaded.
                    continue
                included = _resolve_model(child.findtext("uri", "").strip(), paths, in_memory)
                if included is None:
                    continue
                include_pose = _compose(world_pose, _pose(child.find("pose")))
                include_name = child.findtext("name")
                add_model(included, include_pose, model_path, include_name.strip() if include_name else None)

    worlds = root.findall("world")
    for world in worlds:
        for child in world:
            if child.tag == "model":
                add_model(child, (0.0,) * 6, "")
            elif child.tag == "include":
                included = _resolve_model(child.findtext("uri", "").strip(), paths, in_memory)
                if included is None:
                    continue
                include_name = child.findtext("name")
                name = include_name.strip() if include_name else None
                add_model(included, _pose(child.find("pose")), "", name)
    if not worlds:
        model = root if root.tag == "model" else root.find("model")
        if model is not None:
            add_model(model, (0.0,) * 6, "")
    return primitives


def _local_point(point: Sequence[float], transform: tuple[float, ...], rotation: tuple[tuple[float, ...], ...]) -> tuple[float, float, float]:
    dx, dy, dz = point[0] - transform[0], point[1] - transform[1], point[2] - transform[2]
    return (
        rotation[0][0] * dx + rotation[1][0] * dy + rotation[2][0] * dz,
        rotation[0][1] * dx + rotation[1][1] * dy + rotation[2][1] * dz,
        rotation[0][2] * dx + rotation[1][2] * dy + rotation[2][2] * dz,
    )


def _point_segment_distance(x: float, y: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    t = 0.0 if denom == 0.0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / denom))
    return math.hypot(x - ax - t * dx, y - ay - t * dy)


def _distance_local(shape: str, size: tuple[float, ...], point: tuple[float, float, float]) -> float:
    x, y, z = point
    if shape == "sphere":
        return math.sqrt(x * x + y * y + z * z) - size[0]
    if shape == "box":
        qx, qy, qz = abs(x) - size[0] / 2, abs(y) - size[1] / 2, abs(z) - size[2] / 2
        return math.sqrt(max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2 + max(qz, 0.0) ** 2) + min(max(qx, qy, qz), 0.0)
    radial = math.hypot(x, y)
    radius, length = size
    half = length / 2
    if shape == "cylinder":
        qr, qz = radial - radius, abs(z) - half
        return math.hypot(max(qr, 0.0), max(qz, 0.0)) + min(max(qr, qz), 0.0)
    # A solid cone is the revolution of the triangle with base (r, -h)
    # and apex (0, +h). Its signed distance reduces exactly to this 2-D profile.
    base_z, apex_z = -half, half
    outside_distance = min(
        _point_segment_distance(radial, z, radius, base_z, 0.0, apex_z),
        math.hypot(max(radial - radius, 0.0), z - base_z),
    )
    inside = base_z <= z <= apex_z and radial <= radius * (apex_z - z) / length
    return -outside_distance if inside else outside_distance


def _bound_radius(primitive: Primitive) -> float:
    if primitive.shape == "box":
        return math.sqrt(sum((axis / 2) ** 2 for axis in primitive.size))
    if primitive.shape == "sphere":
        return primitive.size[0]
    radius, length = primitive.size
    return math.hypot(radius, length / 2)


@dataclass(slots=True)
class _Entry:
    primitive: Primitive
    rotation: tuple[tuple[float, float, float], ...]
    radius: float
    x: float
    y: float


class CollisionWorld:
    """Collision queries over a uniform XY grid of primitive bounding circles."""

    def __init__(self, primitives: Iterable[Primitive]):
        self.primitives = tuple(primitives)
        self.cell_size = 5.0
        self._entries: list[_Entry] = []
        self._grid: dict[tuple[int, int], list[int]] = {}
        for primitive in self.primitives:
            pose = primitive.transform
            radius = _bound_radius(primitive)
            entry = _Entry(primitive, _matrix(*pose[3:]), radius, pose[0], pose[1])
            index = len(self._entries)
            self._entries.append(entry)
            cell = self.cell_size
            ix0, ix1 = math.floor((pose[0] - radius) / cell), math.floor((pose[0] + radius) / cell)
            iy0, iy1 = math.floor((pose[1] - radius) / cell), math.floor((pose[1] + radius) / cell)
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    # Only cells intersecting the bounding circle are bucketed.
                    dx = max(ix * cell - pose[0], 0.0, pose[0] - (ix + 1) * cell)
                    dy = max(iy * cell - pose[1], 0.0, pose[1] - (iy + 1) * cell)
                    if dx * dx + dy * dy <= radius * radius:
                        self._grid.setdefault((ix, iy), []).append(index)
        if self._grid:
            self._grid_min_x = min(cell[0] for cell in self._grid)
            self._grid_max_x = max(cell[0] for cell in self._grid)
            self._grid_min_y = min(cell[1] for cell in self._grid)
            self._grid_max_y = max(cell[1] for cell in self._grid)
        else:
            self._grid_min_x = self._grid_max_x = 0
            self._grid_min_y = self._grid_max_y = 0

    def _grid_candidates(self, min_x: float, min_y: float, max_x: float, max_y: float) -> tuple[int, ...]:
        if not self._grid:
            return ()
        cell = self.cell_size
        ix0, ix1 = math.floor(min_x / cell), math.floor(max_x / cell)
        iy0, iy1 = math.floor(min_y / cell), math.floor(max_y / cell)
        if ix1 < self._grid_min_x or ix0 > self._grid_max_x or iy1 < self._grid_min_y or iy0 > self._grid_max_y:
            return ()
        ix0, ix1 = max(ix0, self._grid_min_x), min(ix1, self._grid_max_x)
        iy0, iy1 = max(iy0, self._grid_min_y), min(iy1, self._grid_max_y)
        candidates: set[int] = set()
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                candidates.update(self._grid.get((ix, iy), ()))
        return tuple(sorted(candidates))

    def _distance(self, entry: _Entry, point: Sequence[float]) -> float:
        local = _local_point(point, entry.primitive.transform, entry.rotation)
        return _distance_local(entry.primitive.shape, entry.primitive.size, local)

    def nearby_primitives(self, min_x: float, min_y: float, max_x: float,
                          max_y: float) -> tuple[Primitive, ...]:
        """Primitives bucketed in the XY rectangle: a conservative superset of every
        primitive whose bounding circle could reach it. Callers still apply exact tests."""
        return tuple(self._entries[index].primitive
                     for index in self._grid_candidates(min_x, min_y, max_x, max_y))

    def clearance(self, point: Sequence[float]) -> tuple[float, Primitive | None]:
        """Return signed distance to the nearest collision surface."""
        if len(point) != 3:
            raise ValueError("point must contain x, y, z")
        if not self._entries:
            return math.inf, None
        x, y = float(point[0]), float(point[1])
        ix, iy = math.floor(x / self.cell_size), math.floor(y / self.cell_size)
        if ix < self._grid_min_x or ix > self._grid_max_x or iy < self._grid_min_y or iy > self._grid_max_y:
            # Queries outside the world grid are uncommon; use bounding-circle
            # lower bounds to avoid walking arbitrarily many empty cells.
            order = sorted(
                range(len(self._entries)),
                key=lambda i: max(0.0, math.hypot(x - self._entries[i].x, y - self._entries[i].y) - self._entries[i].radius),
            )
            best, nearest = math.inf, None
            for index in order:
                entry = self._entries[index]
                lower = max(0.0, math.hypot(x - entry.x, y - entry.y) - entry.radius)
                if lower > abs(best):
                    break
                distance = self._distance(entry, point)
                if distance < best:
                    best, nearest = distance, entry.primitive
            return best, nearest

        best, nearest = math.inf, None
        checked: set[int] = set()
        max_ring = max(
            ix - self._grid_min_x, self._grid_max_x - ix,
            iy - self._grid_min_y, self._grid_max_y - iy,
        ) + 1
        for ring in range(max_ring + 1):
            if ring == 0:
                cells = ((ix, iy),)
            else:
                cells = (
                    *((ix - ring + dx, iy - ring) for dx in range(2 * ring + 1)),
                    *((ix - ring + dx, iy + ring) for dx in range(2 * ring + 1)),
                    *((ix - ring, iy - ring + dy) for dy in range(1, 2 * ring)),
                    *((ix + ring, iy - ring + dy) for dy in range(1, 2 * ring)),
                )
            for cell in cells:
                for index in self._grid.get(cell, ()):
                    if index in checked:
                        continue
                    checked.add(index)
                    entry = self._entries[index]
                    distance = self._distance(entry, point)
                    if distance < best:
                        best, nearest = distance, entry.primitive
            if nearest is not None:
                left, right = (ix - ring) * self.cell_size, (ix + ring + 1) * self.cell_size
                bottom, top = (iy - ring) * self.cell_size, (iy + ring + 1) * self.cell_size
                lower_bound = min(x - left, right - x, y - bottom, top - y)
                if lower_bound > abs(best):
                    break
            elif ring >= max_ring:
                break
        return best, nearest

    def segment_clearance(
        self,
        a: Sequence[float],
        b: Sequence[float],
        *,
        radius: float = 0.0,
        step: float = 0.2,
    ) -> tuple[float, Primitive | None]:
        """Return sampled minimum clearance along a segment, with step/2 error bound."""
        if step <= 0.0:
            raise ValueError("step must be positive")
        if radius < 0.0:
            raise ValueError("radius must be non-negative")
        if len(a) != 3 or len(b) != 3:
            raise ValueError("segment endpoints must contain x, y, z")
        dx, dy, dz = (float(b[i]) - float(a[i]) for i in range(3))
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        count = max(1, math.ceil(length / step))
        best, nearest = math.inf, None
        for i in range(count + 1):
            fraction = i / count
            point = (float(a[0]) + fraction * dx, float(a[1]) + fraction * dy, float(a[2]) + fraction * dz)
            distance, primitive = self.clearance(point)
            distance -= radius
            if distance < best:
                best, nearest = distance, primitive
        return best, nearest

    def blocking(
        self,
        eye: Sequence[float],
        target: Sequence[float],
        *,
        ignore: frozenset[str] = frozenset(),
    ) -> Primitive | None:
        """Return the first obstacle intersected by the open eye-to-target segment."""
        if len(eye) != 3 or len(target) != 3:
            raise ValueError("segment endpoints must contain x, y, z")
        start = tuple(float(v) for v in eye)
        end = tuple(float(v) for v in target)
        delta = tuple(end[i] - start[i] for i in range(3))
        length = math.sqrt(sum(value * value for value in delta))
        if length == 0.0:
            return None
        # Bounding-sphere ray intervals cheaply reject most primitives and
        # order the exact shape tests from the eye toward the target.
        candidates: list[_Entry] = []
        delta_sq = sum(value * value for value in delta)
        broad_phase = self._grid_candidates(min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1]))
        for index in broad_phase:
            entry = self._entries[index]
            primitive = entry.primitive
            if primitive.name in ignore or primitive.model in ignore:
                continue
            ox, oy, oz = start[0] - entry.x, start[1] - entry.y, start[2] - primitive.transform[2]
            b = ox * delta[0] + oy * delta[1] + oz * delta[2]
            c = ox * ox + oy * oy + oz * oz - entry.radius * entry.radius
            disc = b * b - delta_sq * c
            if disc < 0.0:
                continue
            root = math.sqrt(disc)
            lo, hi = (-b - root) / delta_sq, (-b + root) / delta_sq
            if hi > 0.0 and lo < 1.0:
                candidates.append(entry)
        intersections: list[tuple[float, Primitive]] = []
        for entry in candidates:
            primitive = entry.primitive
            if primitive.shape == "sphere":
                center = primitive.transform[:3]
                offset = tuple(start[i] - center[i] for i in range(3))
                aa = delta_sq
                bb = 2.0 * sum(offset[i] * delta[i] for i in range(3))
                cc = sum(v * v for v in offset) - primitive.size[0] ** 2
                discriminant = bb * bb - 4.0 * aa * cc
                if discriminant < 0.0:
                    continue
                root = math.sqrt(discriminant)
                lo, hi = (-bb - root) / (2.0 * aa), (-bb + root) / (2.0 * aa)
            else:
                p0 = _local_point(start, primitive.transform, entry.rotation)
                local_delta = (
                    entry.rotation[0][0] * delta[0] + entry.rotation[1][0] * delta[1] + entry.rotation[2][0] * delta[2],
                    entry.rotation[0][1] * delta[0] + entry.rotation[1][1] * delta[1] + entry.rotation[2][1] * delta[2],
                    entry.rotation[0][2] * delta[0] + entry.rotation[1][2] * delta[1] + entry.rotation[2][2] * delta[2],
                )
                if primitive.shape == "box":
                    interval = _ray_box(p0, local_delta, tuple(v / 2 for v in primitive.size))
                elif primitive.shape == "cylinder":
                    interval = _ray_cylinder(p0, local_delta, primitive.size[0], primitive.size[1] / 2)
                else:
                    # Conservative cone test against the cone's circumscribing cylinder.
                    interval = _ray_cylinder(p0, local_delta, primitive.size[0], primitive.size[1] / 2)
                if interval is None:
                    continue
                lo, hi = interval
            if hi > 0.0 and lo < 1.0 and lo <= hi:
                intersections.append((max(0.0, lo), primitive))
        return min(intersections, key=lambda hit: hit[0])[1] if intersections else None

    def max_top(self, x: float, y: float, radius: float) -> float:
        """Highest collision altitude within a horizontal disc.

        Upright primitives (including arbitrary-yaw boxes) use their exact XY
        footprints. For tilted solids, the circumscribing sphere gives a
        conservative footprint and height bound.
        """
        if radius < 0.0:
            raise ValueError("radius must be non-negative")
        x, y, radius = float(x), float(y), float(radius)
        highest = -math.inf
        for index in self._grid_candidates(x - radius, y - radius, x + radius, y + radius):
            entry = self._entries[index]
            primitive = entry.primitive
            pose, rotation = primitive.transform, entry.rotation
            dx, dy = x - pose[0], y - pose[1]
            center_distance = math.hypot(dx, dy)
            if primitive.shape == "sphere":
                nearest = max(0.0, center_distance - radius)
                sphere_radius = primitive.size[0]
                if nearest <= sphere_radius:
                    highest = max(highest, pose[2] + math.sqrt(sphere_radius ** 2 - nearest ** 2))
                continue

            upright = (
                math.hypot(rotation[0][2], rotation[1][2]) < 1e-10
                and math.hypot(rotation[2][0], rotation[2][1]) < 1e-10
            )
            if upright and primitive.shape == "box":
                local_x = rotation[0][0] * dx + rotation[1][0] * dy
                local_y = rotation[0][1] * dx + rotation[1][1] * dy
                gap_x = max(abs(local_x) - primitive.size[0] / 2, 0.0)
                gap_y = max(abs(local_y) - primitive.size[1] / 2, 0.0)
                if math.hypot(gap_x, gap_y) <= radius:
                    highest = max(highest, pose[2] + abs(rotation[2][2]) * primitive.size[2] / 2)
                continue
            if upright and primitive.shape == "cylinder":
                shape_radius, length = primitive.size
                if center_distance <= radius + shape_radius:
                    highest = max(highest, pose[2] + abs(rotation[2][2]) * length / 2)
                continue
            if upright and primitive.shape == "cone":
                shape_radius, length = primitive.size
                nearest = max(0.0, center_distance - radius)
                if nearest <= shape_radius:
                    if rotation[2][2] > 0.0:
                        top = pose[2] + length / 2 - length * nearest / shape_radius
                    else:
                        top = pose[2] + length / 2
                    highest = max(highest, top)
                continue

            if center_distance > radius + entry.radius:
                continue
            if primitive.shape == "box":
                half = primitive.size
                z_extent = sum(abs(rotation[2][i]) * half[i] / 2 for i in range(3))
            else:
                shape_radius, length = primitive.size
                z_extent = abs(rotation[2][2]) * length / 2 + shape_radius * math.hypot(rotation[2][0], rotation[2][1])
            highest = max(highest, pose[2] + z_extent)
        return highest


def _ray_box(origin: Sequence[float], direction: Sequence[float], half: Sequence[float]) -> tuple[float, float] | None:
    lo, hi = -math.inf, math.inf
    for axis in range(3):
        if abs(direction[axis]) < 1e-15:
            if abs(origin[axis]) > half[axis]:
                return None
            continue
        first = (-half[axis] - origin[axis]) / direction[axis]
        second = (half[axis] - origin[axis]) / direction[axis]
        if first > second:
            first, second = second, first
        lo, hi = max(lo, first), min(hi, second)
        if lo > hi:
            return None
    return lo, hi


def _ray_cylinder(origin: Sequence[float], direction: Sequence[float], radius: float, half_length: float) -> tuple[float, float] | None:
    # Intersect the infinite radial cylinder and finite end-cap slab, then
    # intersect their parameter intervals.
    ox, oy, oz = origin
    dx, dy, dz = direction
    aa = dx * dx + dy * dy
    bb = 2.0 * (ox * dx + oy * dy)
    cc = ox * ox + oy * oy - radius * radius
    if aa < 1e-30:
        if cc > 0.0:
            return None
        radial = (-math.inf, math.inf)
    else:
        discriminant = bb * bb - 4 * aa * cc
        if discriminant < 0.0:
            return None
        root = math.sqrt(discriminant)
        radial = ((-bb - root) / (2 * aa), (-bb + root) / (2 * aa))
    if abs(dz) < 1e-15:
        if abs(oz) > half_length:
            return None
        axial = (-math.inf, math.inf)
    else:
        first = (-half_length - oz) / dz
        second = (half_length - oz) / dz
        axial = (min(first, second), max(first, second))
    lo, hi = max(radial[0], axial[0]), min(radial[1], axial[1])
    return None if lo > hi else (lo, hi)
