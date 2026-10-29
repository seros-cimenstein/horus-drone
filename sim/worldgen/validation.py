"""Real-geometry checks used while sampling procedural layouts.

Every check uses the world's actual static collision primitives and the shared triangulated
terrain surface. Routes are checked with each agent's measured footprint along the path the
scenario driver actually executes: closed loops include their closing edge, vehicles their
filleted (curvature-bounded) path with the vehicle's oriented footprint, followers the leader's
loop, wanderers their whole area.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import math
import tempfile
from pathlib import Path
from typing import Any, TypeVar

from sim.worldgen.geometry import CollisionWorld, load_collisions, parse_scenery_sidecar
from sim.worldgen.layout import forest_surface, point_segment_distance
from sim.worldgen.paths import FilletedPath
from sim.worldgen.plan import Ground

STANDING_CLEARANCE_M = 0.1
VEHICLE_CLEARANCE_M = 0.3
ANIMAL_CLEARANCE_M = 0.05
SAMPLE_STEP_M = 0.25

Layout = TypeVar("Layout")


class SampleRejected(Exception):
    """A sampled attempt that cannot be validated; the reason is counted and retried."""


def retry_sample(attempt_fn: Callable[[int], Layout],
                 validate: Callable[[Layout], list[str]], *,
                 attempts: int, kind: str, seed: int) -> Layout:
    """Sample attempts ``1..attempts`` until one passes ``validate``.

    ``attempt_fn(attempt)`` returns a candidate or raises :class:`SampleRejected`;
    ``validate(candidate)`` returns its failures. The first candidate without failures is
    returned.
    """
    rejections: Counter[str] = Counter()
    for attempt in range(1, attempts + 1):
        try:
            layout = attempt_fn(attempt)
        except SampleRejected as exc:
            rejections[str(exc)] += 1
            continue
        failures = validate(layout)
        if not failures:
            return layout
        rejections["validation"] += 1
        rejections[f"validation: {failures[0][:90]}"] += 1
    top = ", ".join(f"{reason} x{count}" for reason, count in rejections.most_common(4))
    raise RuntimeError(f"could not sample valid {kind} layout for seed {seed} after "
                       f"{sum(rejections.values())} attempts; top rejections: {top or 'pre-checks'}")


def in_memory_models(files: dict[str, bytes | str]) -> dict[str, str]:
    """Generated model SDFs (``models/<name>/model.sdf``) keyed by model name."""
    models = {}
    for relative, content in files.items():
        parts = relative.split("/")
        if len(parts) == 3 and parts[0] == "models" and parts[2] == "model.sdf":
            models[parts[1]] = content.decode("utf-8") if isinstance(content, bytes) else content
    return models


def _ground(kind: str, layout: Any) -> Ground:
    if kind == "forest":
        return Ground.from_surface(forest_surface(layout))
    return Ground({})


def _load_world(kind: str, layout: Any, sdf: str, *, model_dir: Path, world_dir: Path,
                files: dict[str, bytes | str] | None = None) -> tuple[CollisionWorld, Ground]:
    # The SDF is validated from a temp file, so the scenery sidecar cannot sit next to it:
    # parse the in-memory `<stem>.scenery.json` and pass its proxies explicitly.
    scenery = []
    for relative, content in (files or {}).items():
        if relative.endswith(".scenery.json"):
            text = content.decode("utf-8") if isinstance(content, bytes) else content
            scenery.extend(parse_scenery_sidecar(text))
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sdf", encoding="utf-8") as stream:
        stream.write(sdf)
        stream.flush()
        primitives = load_collisions(stream.name, resource_paths=(model_dir, world_dir),
                                     models=in_memory_models(files or {}),
                                     scenery_proxies=scenery or None)
    return CollisionWorld(primitives), _ground(kind, layout)


def _closed(points: list[tuple[float, float]], mode: str) -> list[tuple[float, float]]:
    return points + [points[0]] if mode == "loop" and len(points) > 2 else points


def _densify(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) == 1:
        return points
    result = []
    for a, b in zip(points, points[1:]):
        steps = max(1, math.ceil(math.dist(a, b) / SAMPLE_STEP_M))
        result.extend((a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps) for i in range(steps))
    result.append(points[-1])
    return result


def _rect(cx: float, cy: float, yaw: float, hx: float, hy: float) -> list[tuple[float, float]]:
    c, s = math.cos(yaw), math.sin(yaw)
    return [(cx + px * c - py * s, cy + px * s + py * c) for px, py in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))]


def _segment_segment_distance(a, b, c, d) -> float:
    """2-D distance between segments ab and cd (0 when they intersect or overlap)."""

    def cross(o, p, q) -> float:
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    d1, d2, d3, d4 = cross(c, d, a), cross(c, d, b), cross(a, b, c), cross(a, b, d)
    if (((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))
            and all(value != 0.0 for value in (d1, d2, d3, d4))):
        return 0.0
    return min(
        point_segment_distance(a, c, d), point_segment_distance(b, c, d),
        point_segment_distance(c, a, b), point_segment_distance(d, a, b),
    )


def _segment_rectangle_distance(a, b, rectangle) -> float:
    """2-D distance from segment ab to a convex polygon ``rectangle`` (0 on overlap)."""
    return min(_segment_segment_distance(a, b, rectangle[index], rectangle[(index + 1) % len(rectangle)])
               for index in range(len(rectangle)))


def _inside(polygon, point) -> bool:
    sign = None
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        cross = (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])
        if abs(cross) < 1e-12:
            continue
        if sign is None:
            sign = cross > 0
        elif (cross > 0) != sign:
            return False
    return True


def _point_polygon_distance(point, polygon) -> float:
    if _inside(polygon, point):
        return 0.0
    return min(point_segment_distance(point, a, b) for a, b in zip(polygon, polygon[1:] + polygon[:1]))


def _polygons_distance(first, second) -> float:
    """Distance between convex polygons (0 when they overlap; separating-axis test)."""
    separated = False
    for polygon in (first, second):
        for a, b in zip(polygon, polygon[1:] + polygon[:1]):
            nx, ny = b[1] - a[1], a[0] - b[0]
            p1 = [nx * x + ny * y for x, y in first]
            p2 = [nx * x + ny * y for x, y in second]
            if max(p1) < min(p2) or max(p2) < min(p1):
                separated = True
    if not separated:
        return 0.0
    return min(min(_point_polygon_distance(p, second) for p in first),
               min(_point_polygon_distance(p, first) for p in second))


def _box_primitive_distance(rectangle, z0: float, z1: float, primitive) -> float:
    """Distance from an upright oriented box (footprint ``rectangle``, heights z0..z1) to a
    primitive: exact for upright boxes, cylinders and spheres and for horizontal cylinders
    (cables, fallen logs); conservative for cones (their enclosing cylinder) and other tilted
    solids (their bounding sphere). Overlap returns -0.01."""
    from sim.worldgen.geometry import _bound_radius, _matrix

    px, py, pz, roll, pitch, yaw = primitive.transform
    upright = abs(roll) < 1e-6 and abs(pitch) < 1e-6
    if primitive.shape == "sphere" or not upright:
        if primitive.shape == "cylinder":
            rotation = _matrix(roll, pitch, yaw)
            axis_z = rotation[2][2]
            if abs(axis_z) <= 1e-4:  # horizontal axis (SDF poses quantize the quarter turn)
                # A horizontal cylinder is contained in the stadium swept by the XY
                # projection of its axis, extruded over z ± radius. A bounding sphere
                # would be half the cable long and would falsely clip every actor
                # walking underneath.
                radius, length = primitive.size
                half = length / 2
                a = (px - rotation[0][2] * half, py - rotation[1][2] * half)
                b = (px + rotation[0][2] * half, py + rotation[1][2] * half)
                horizontal = max(0.0, _segment_rectangle_distance(a, b, rectangle) - radius)
                vertical = max(0.0, z0 - (pz + radius + abs(axis_z) * half),
                               (pz - radius - abs(axis_z) * half) - z1)
                if horizontal == 0.0 and vertical == 0.0:
                    return -0.01
                return math.hypot(horizontal, vertical)
        radius = primitive.size[0] if primitive.shape == "sphere" else _bound_radius(primitive)
        horizontal = _point_polygon_distance((px, py), rectangle)
        vertical = max(0.0, z0 - pz, pz - z1)
        return max(-0.01, math.hypot(horizontal, vertical) - radius)
    if primitive.shape == "box":
        sx, sy, sz = primitive.size
        horizontal = _polygons_distance(rectangle, _rect(px, py, yaw, sx / 2, sy / 2))
        low, high = pz - sz / 2, pz + sz / 2
    else:
        radius, length = primitive.size
        horizontal = max(0.0, _point_polygon_distance((px, py), rectangle) - radius)
        low, high = pz - length / 2, pz + length / 2
    vertical = max(0.0, z0 - high, low - z1)
    if horizontal == 0.0 and vertical == 0.0:
        return -0.01
    return math.hypot(horizontal, vertical)


def box_clearance(world: CollisionWorld, ground: Ground, x: float, y: float, yaw: float,
                  box: tuple[float, float, float, float, float, float]) -> tuple[float, str | None]:
    """Clearance of an upright oriented body box (model-frame x0, x1, y0, y1, z0, z1 above the
    local ground at the body origin) to static geometry; -0.01 means interpenetration."""
    from sim.worldgen.geometry import _bound_radius

    x0, x1, y0, y1, z0, z1 = box
    c, s = math.cos(yaw), math.sin(yaw)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    cx, cy = x + mx * c - my * s, y + mx * s + my * c
    base = ground.height(x, y)
    bottom, top = base + z0, base + z1
    half_diagonal = math.sqrt(((x1 - x0) / 2) ** 2 + ((y1 - y0) / 2) ** 2 + ((z1 - z0) / 2) ** 2)
    centre = (cx, cy, (bottom + top) / 2)
    nearest, _primitive = world.clearance(centre)
    if nearest - half_diagonal >= 1.0:
        return nearest - half_diagonal, None
    rectangle = _rect(cx, cy, yaw, (x1 - x0) / 2, (y1 - y0) / 2)
    reach = nearest + 2 * half_diagonal + 1.0
    worst, blocker = math.inf, None
    for candidate in world.nearby_primitives(cx - reach, cy - reach, cx + reach, cy + reach):
        if math.dist(candidate.transform[:3], centre) - _bound_radius(candidate) > reach:
            continue
        distance = _box_primitive_distance(rectangle, bottom, top, candidate)
        if distance < worst:
            worst, blocker = distance, candidate.name
    return worst, blocker


def agent_path_points(agent: dict, agents_by_model: dict[str, dict]) -> list[tuple[float, float, float | None]]:
    """(x, y, heading or None) samples of the region an agent can occupy."""
    if agent.get("kind") == "vehicle":
        closed = agent.get("mode", "loop") == "loop"
        path = FilletedPath([tuple(p) for p in agent["waypoints"]], agent["turn_radius"], closed=closed,
                            name=agent["model"])
        return path.points(SAMPLE_STEP_M)
    if agent.get("behavior") == "follow":
        leader = agents_by_model[agent["leader"]]
        points = _closed([tuple(p[:2]) for p in leader["waypoints"]], "loop")
        return [(x, y, None) for x, y in _densify(points)]
    if agent.get("behavior") == "wander":
        polygon = [tuple(p) for p in agent["area"]]
        xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
        from sim.scripts.civilian_spec import point_in_polygon

        samples = [(x, y, None) for x, y in _densify(polygon + [polygon[0]])]
        steps = 8
        for i in range(steps + 1):
            for j in range(steps + 1):
                x = min(xs) + (max(xs) - min(xs)) * i / steps
                y = min(ys) + (max(ys) - min(ys)) * j / steps
                if point_in_polygon((x, y), tuple(polygon)):
                    samples.append((x, y, None))
        return samples
    if agent.get("waypoints"):
        points = _closed([tuple(p[:2]) for p in agent["waypoints"]], agent.get("mode", "loop"))
        result = []
        for a, b in zip(points, points[1:]):
            heading = math.atan2(b[1] - a[1], b[0] - a[0])
            result.extend((x, y, heading) for x, y in _densify([a, b]))
        return result or [(points[0][0], points[0][1], agent.get("yaw", 0.0))]
    return [(agent["position"][0], agent["position"][1], agent.get("yaw", 0.0))]


def agent_box(agent: dict) -> tuple[float, float, float, float, float, float]:
    """Model-frame body box (x forward) from the agent's footprint or measured envelope."""
    if agent.get("kind") == "vehicle":
        footprint = agent["footprint"]
        return (-footprint["length_m"] / 2, footprint["length_m"] / 2, -footprint["width_m"] / 2,
                footprint["width_m"] / 2, 0.15, footprint["height_m"])
    envelope = agent["envelope"]
    return (envelope["x_min_m"], envelope["x_max_m"], envelope["y_min_m"], envelope["y_max_m"],
            max(0.05, envelope.get("z_min_m", 0.0)), envelope["top_z_m"])


def agent_route_clearance(world: CollisionWorld, ground: Ground, agent: dict,
                          agents_by_model: dict[str, dict]) -> tuple[float, str | None]:
    """Worst static clearance of an agent's body box over everything it can traverse.

    Where the heading is not determined by the path (wandering, following a leader's loop),
    the box is widened to the body's full turning radius."""
    box = agent_box(agent)
    reach = max(abs(value) for value in box[:4])
    swept = (-reach, reach, -reach, reach, box[4], box[5])
    worst, blocker = math.inf, None
    for x, y, heading in agent_path_points(agent, agents_by_model):
        clearance, name = box_clearance(world, ground, x, y, heading or 0.0, box if heading is not None else swept)
        if clearance < worst:
            worst, blocker = clearance, name
    return worst, blocker


def _required(agent: dict) -> float | None:
    """Minimum static clearance an agent's footprint must keep (None: not checked)."""
    if agent.get("kind") == "vehicle":
        return VEHICLE_CLEARANCE_M
    if agent.get("kind") == "animal":
        return ANIMAL_CLEARANCE_M
    posture = agent.get("posture", "standing")
    if posture == "standing":
        return STANDING_CLEARANCE_M if agent.get("behavior") != "idle" else 0.0
    return 0.0  # seated/lying figures may rest against benches and terrain features


def validate_world(kind: str, layout: Any, sdf: str, manifest: dict[str, Any], *,
                   agents: list[dict], files: dict[str, bytes | str],
                   model_dir: Path, world_dir: Path) -> list[str]:
    """Every reason a procedural scene is not flight-safe or an actor's footprint does not stay
    clear of static geometry over its whole executed path (accepted when the list is empty)."""
    world, ground = _load_world(kind, layout, sdf, model_dir=model_dir, world_dir=world_dir, files=files)
    return world_report(world, ground, manifest, agents)["failures"]


def _route_clearances(world: CollisionWorld, ground: Ground,
                      agents: list[dict]) -> tuple[dict[str, float], list[str]]:
    """Each checked agent's worst footprint clearance, and the agents that violate it."""
    by_model = {agent["model"]: agent for agent in agents}
    clearances: dict[str, float] = {}
    failures: list[str] = []
    for agent in agents:
        required = _required(agent)
        if required is None:
            continue
        try:
            clearance, blocker = agent_route_clearance(world, ground, agent, by_model)
        except ValueError as exc:
            failures.append(f"{agent['model']}: {exc}")
            continue
        clearances[agent["model"]] = round(clearance, 3)
        # Lying/seated figures rest on the ground; only interpenetration with scenery fails them.
        limit = required if required > 0 else -0.02
        if clearance < limit:
            failures.append(f"{agent['model']} footprint clearance {clearance:.2f} m < {limit:.2f} m ({blocker})")
    return clearances, failures


def world_report(world: CollisionWorld, ground: Ground, manifest: dict[str, Any],
                 agents: list[dict]) -> dict[str, Any]:
    failures: list[str] = []
    transit = float(manifest["transit_altitude_m"])
    home, _ = world.segment_clearance((0.0, 0.0, 3.5), (0.0, 0.0, transit), radius=0.6)
    if home < 2.0:
        failures.append(f"home column clearance {home:.2f} m")
    clearances, route_failures = _route_clearances(world, ground, agents)
    failures += route_failures
    return {"failures": failures, "clearances": clearances}
