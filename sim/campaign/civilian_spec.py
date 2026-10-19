"""Strict civilian scenario YAML: agent, event and ground specifications plus wander-area geometry."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sim.worldgen.surface import TriangulatedGrid

_EPSILON = 1e-9
KINDS = ("pedestrian", "vehicle", "animal")
SPECIES = ("dog", "cat", "deer", "pigeon", "crow")
EVENT_TYPES = ("hesitate", "speed_change", "breakdown", "startle", "reverse")


@dataclass(frozen=True, slots=True)
class Waypoint:
    x: float
    y: float
    z: float | None = None
    hold_s: float = 0.0


@dataclass(frozen=True, slots=True)
class AgentSpec:
    model: str
    kind: str
    group: str
    behavior: str
    z_offset: float
    speed_m_s: float | None = None
    max_speed_m_s: float | None = None
    mode: str = "loop"
    start_offset_s: float = 0.0
    accel_m_s2: float | None = None
    decel_m_s2: float | None = None
    turn_radius_m: float = 5.0
    radius_m: float = 0.3
    length_m: float = 0.0
    width_m: float = 0.0
    species: str | None = None
    waypoints: tuple[Waypoint, ...] = ()
    area: tuple[tuple[float, float], ...] = ()
    pause_s: tuple[float, float] = (0.0, 0.0)
    avoid_agents: bool = False
    leader: str | None = None
    gap_m: tuple[float, float] = (1.0, 2.0)
    pose: tuple[float, float, float] | None = None
    fidget_deg: float = 0.0
    fidget_period_s: float = 6.0

    @property
    def half_length(self) -> float:
        return self.length_m / 2 if self.kind == "vehicle" else self.radius_m

    @property
    def half_width(self) -> float:
        return self.width_m / 2 if self.kind == "vehicle" else self.radius_m


@dataclass(frozen=True, slots=True)
class Event:
    t_s: float
    model: str
    type: str
    duration_s: float = 0.0
    factor: float = 1.0


@dataclass(frozen=True, slots=True)
class Scenario:
    world: str
    rate_hz: float
    seed: int
    ground: TriangulatedGrid
    agents: tuple[AgentSpec, ...]
    events: tuple[Event, ...] = ()
    interactions: bool = False


# --------------------------------------------------------------------------- parsing

def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must be a mapping with string keys")
    return value


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], field_name: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(f"{field_name} has unknown key {unknown[0]!r}")


def _number(value: Any, field_name: str, *, minimum: float | None = None, strict: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    if minimum is not None and (result <= minimum if strict else result < minimum):
        relation = "greater than" if strict else "at least"
        raise ValueError(f"{field_name} must be {relation} {minimum:g}")
    return result


def _pair(value: Any, field_name: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{field_name} must contain exactly two numbers")
    return _number(value[0], f"{field_name}[0]"), _number(value[1], f"{field_name}[1]")


def _ordered_pair(value: Any, field_name: str) -> tuple[float, float]:
    pair = _pair(value, field_name)
    if pair[0] < 0.0 or pair[1] < pair[0]:
        raise ValueError(f"{field_name} must be an ordered, nonnegative [min, max] pair")
    return pair


def _polygon(value: Any, field_name: str) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, list) or len(value) < 3:
        raise ValueError(f"{field_name} must be a polygon with at least three points")
    points = tuple(_pair(point, f"{field_name}[{index}]") for index, point in enumerate(value))
    area2 = sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )
    if abs(area2) <= _EPSILON:
        raise ValueError(f"{field_name} must enclose a nonzero area")
    return points


def agent_error(model: str, field_name: str, message: str) -> ValueError:
    return ValueError(f"agent {model!r} field {field_name!r} {message}")


_BEHAVIOR_KEYS = {
    "route": {"speed_m_s", "max_speed_m_s", "mode", "start_offset_s", "accel_m_s2", "decel_m_s2",
              "turn_radius_m", "waypoints"},
    "wander": {"speed_m_s", "max_speed_m_s", "area", "pause_s", "accel_m_s2", "decel_m_s2", "avoid_agents"},
    "follow": {"speed_m_s", "max_speed_m_s", "accel_m_s2", "decel_m_s2", "leader", "gap_m"},
    "idle": {"pose", "fidget_deg", "fidget_period_s"},
}
_KIND_BEHAVIORS = {"pedestrian": ("route", "wander", "idle"), "vehicle": ("route",),
                   "animal": ("route", "wander", "idle", "follow")}


def _parse_agent(raw: Any, index: int) -> AgentSpec:
    mapping = _mapping(raw, f"agents[{index}]")
    model_value = mapping.get("model", f"at index {index}")
    model = model_value if isinstance(model_value, str) and model_value.strip() else f"at index {index}"
    if not isinstance(model_value, str) or not model_value.strip():
        raise agent_error(model, "model", "must be a non-empty string")
    kind = mapping.get("kind")
    if kind not in KINDS:
        raise agent_error(model, "kind", f"must be one of {', '.join(KINDS)}")
    behavior = mapping.get("behavior")
    if behavior not in _BEHAVIOR_KEYS:
        raise agent_error(model, "behavior", f"has unknown behavior {behavior!r}")
    if behavior not in _KIND_BEHAVIORS[kind]:
        raise agent_error(model, "behavior", f"{behavior!r} is not supported for kind {kind!r}")

    common = {"model", "kind", "group", "behavior", "z_offset"}
    kind_keys = {"pedestrian": {"radius_m"}, "vehicle": {"footprint"}, "animal": {"radius_m", "species"}}[kind]
    _reject_unknown(mapping, common | kind_keys | _BEHAVIOR_KEYS[behavior], f"agent {model!r}")
    group = mapping.get("group", "ambient")
    if not isinstance(group, str) or not group.strip():
        raise agent_error(model, "group", "must be a non-empty string")
    fields: dict[str, Any] = {"model": model, "kind": kind, "group": group, "behavior": behavior}
    try:
        fields["z_offset"] = _number(mapping.get("z_offset", 0.0), "z_offset")
        if kind == "animal":
            if mapping.get("species") not in SPECIES:
                raise ValueError(f"species must be one of {', '.join(SPECIES)}")
            fields["species"] = mapping["species"]
        if "radius_m" in mapping:
            fields["radius_m"] = _number(mapping["radius_m"], "radius_m", minimum=0.0, strict=True)
        if kind == "vehicle":
            footprint = _mapping(mapping.get("footprint", {"length_m": 4.4, "width_m": 1.9}), "footprint")
            _reject_unknown(footprint, {"length_m", "width_m"}, "footprint")
            if not {"length_m", "width_m"} <= set(footprint):
                raise ValueError("footprint requires length_m and width_m")
            fields["length_m"] = _number(footprint["length_m"], "footprint.length_m", minimum=0.0, strict=True)
            fields["width_m"] = _number(footprint["width_m"], "footprint.width_m", minimum=0.0, strict=True)
        if behavior in ("route", "wander", "follow"):
            fields.update(_motion_fields(mapping, kind))
        fields.update(_BEHAVIOR_PARSERS[behavior](mapping, kind))
    except ValueError as exc:
        field_name = str(exc).split(" ", 1)[0]
        raise agent_error(model, field_name, str(exc)) from exc
    return AgentSpec(**fields)


def _motion_fields(mapping: Mapping[str, Any], kind: str) -> dict[str, Any]:
    if "speed_m_s" not in mapping:
        raise ValueError("speed_m_s is required")
    speed = _number(mapping["speed_m_s"], "speed_m_s", minimum=0.0, strict=True)
    maximum = _number(mapping.get("max_speed_m_s", speed), "max_speed_m_s", minimum=0.0, strict=True)
    if maximum < speed:
        raise ValueError("max_speed_m_s must be at least speed_m_s")
    fields: dict[str, Any] = {"speed_m_s": speed, "max_speed_m_s": maximum}
    accel = None
    if "accel_m_s2" in mapping:
        accel = _number(mapping["accel_m_s2"], "accel_m_s2", minimum=0.0, strict=True)
    elif kind != "pedestrian":
        accel = 1.5
    fields["accel_m_s2"] = accel
    if "decel_m_s2" in mapping:
        if accel is None:
            raise ValueError("decel_m_s2 requires accel_m_s2")
        fields["decel_m_s2"] = _number(mapping["decel_m_s2"], "decel_m_s2", minimum=0.0, strict=True)
    elif accel is not None:
        fields["decel_m_s2"] = max(accel, 1.5)
    return fields


def _route_fields(mapping: Mapping[str, Any], kind: str) -> dict[str, Any]:
    mode = mapping.get("mode", "loop")
    if mode not in ("loop", "pingpong", "once"):
        raise ValueError("mode must be 'loop', 'pingpong', or 'once'")
    if kind == "vehicle" and mode == "pingpong":
        raise ValueError("mode 'pingpong' would reverse a vehicle in place; use 'loop' or 'once'")
    fields: dict[str, Any] = {
        "mode": mode,
        "start_offset_s": _number(mapping.get("start_offset_s", 0.0), "start_offset_s", minimum=0.0),
    }
    if "turn_radius_m" in mapping:
        if kind != "vehicle":
            raise ValueError("turn_radius_m applies to vehicles only")
        fields["turn_radius_m"] = _number(mapping["turn_radius_m"], "turn_radius_m", minimum=0.0, strict=True)
    raw_waypoints = mapping.get("waypoints")
    if not isinstance(raw_waypoints, list) or not raw_waypoints:
        raise ValueError("waypoints must be a non-empty list")
    parsed: list[Waypoint] = []
    for waypoint_index, raw_waypoint in enumerate(raw_waypoints):
        path = f"waypoints[{waypoint_index}]"
        waypoint = _mapping(raw_waypoint, path)
        _reject_unknown(waypoint, {"x", "y", "z", "hold_s"}, path)
        if "x" not in waypoint or "y" not in waypoint:
            raise ValueError(f"{path} requires x and y")
        parsed.append(Waypoint(
            _number(waypoint["x"], f"{path}.x"), _number(waypoint["y"], f"{path}.y"),
            _number(waypoint["z"], f"{path}.z") if "z" in waypoint else None,
            _number(waypoint.get("hold_s", 0.0), f"{path}.hold_s", minimum=0.0)))
    fields["waypoints"] = tuple(parsed)
    return fields


def _wander_fields(mapping: Mapping[str, Any], kind: str) -> dict[str, Any]:
    if "area" not in mapping:
        raise ValueError("area is required")
    fields: dict[str, Any] = {"area": _polygon(mapping["area"], "area")}
    if "pause_s" in mapping:
        fields["pause_s"] = _ordered_pair(mapping["pause_s"], "pause_s")
    avoid = mapping.get("avoid_agents", False)
    if not isinstance(avoid, bool):
        raise ValueError("avoid_agents must be a boolean")
    fields["avoid_agents"] = avoid
    return fields


def _follow_fields(mapping: Mapping[str, Any], kind: str) -> dict[str, Any]:
    leader = mapping.get("leader")
    if not isinstance(leader, str) or not leader.strip():
        raise ValueError("leader must name another agent")
    fields: dict[str, Any] = {"leader": leader}
    if "gap_m" in mapping:
        gap = _ordered_pair(mapping["gap_m"], "gap_m")
        if gap[1] <= 0.0:
            raise ValueError("gap_m maximum must be positive")
        fields["gap_m"] = gap
    return fields


def _idle_fields(mapping: Mapping[str, Any], kind: str) -> dict[str, Any]:
    if "pose" not in mapping:
        raise ValueError("pose is required")
    pose_mapping = _mapping(mapping["pose"], "pose")
    _reject_unknown(pose_mapping, {"x", "y", "yaw_deg"}, "pose")
    if not {"x", "y", "yaw_deg"}.issubset(pose_mapping):
        raise ValueError("pose requires x, y, and yaw_deg")
    return {
        "pose": (_number(pose_mapping["x"], "pose.x"), _number(pose_mapping["y"], "pose.y"),
                 math.radians(_number(pose_mapping["yaw_deg"], "pose.yaw_deg"))),
        "fidget_deg": _number(mapping.get("fidget_deg", 0.0), "fidget_deg", minimum=0.0),
        "fidget_period_s": _number(mapping.get("fidget_period_s", 6.0), "fidget_period_s",
                                   minimum=0.0, strict=True),
    }


_BEHAVIOR_PARSERS = {"route": _route_fields, "wander": _wander_fields, "follow": _follow_fields,
                     "idle": _idle_fields}


def _parse_event(raw: Any, index: int, agents: dict[str, AgentSpec]) -> Event:
    path = f"events[{index}]"
    mapping = _mapping(raw, path)
    _reject_unknown(mapping, {"t_s", "model", "type", "duration_s", "factor"}, path)
    model = mapping.get("model")
    if model not in agents:
        raise ValueError(f"{path} names unknown agent {model!r}")
    kind = mapping.get("type")
    if kind not in EVENT_TYPES:
        raise ValueError(f"{path} has unknown type {kind!r}")
    spec = agents[model]
    if spec.behavior == "idle":
        raise ValueError(f"{path}: idle agent {model!r} cannot take motion events")
    if kind == "breakdown" and spec.kind != "vehicle":
        raise ValueError(f"{path}: breakdown applies to vehicles only")
    if kind == "startle" and spec.behavior != "wander":
        raise ValueError(f"{path}: startle applies to wandering agents only")
    if kind == "reverse" and not (spec.kind == "pedestrian" and spec.behavior == "route"
                                  and spec.mode == "loop"):
        raise ValueError(f"{path}: reverse applies to loop-route pedestrians only")
    t_s = _number(mapping.get("t_s"), f"{path}.t_s", minimum=0.0)
    duration = _number(mapping.get("duration_s"), f"{path}.duration_s", minimum=0.0, strict=True)
    factor = 1.0
    if kind == "speed_change":
        factor = _number(mapping.get("factor"), f"{path}.factor", minimum=0.0, strict=True)
        assert spec.speed_m_s is not None and spec.max_speed_m_s is not None
        if spec.speed_m_s * factor > spec.max_speed_m_s + 1e-9:
            raise ValueError(f"{path}: factor {factor:g} exceeds {model!r}'s max_speed_m_s")
    elif "factor" in mapping:
        raise ValueError(f"{path}: factor applies to speed_change only")
    return Event(t_s, model, kind, duration, factor)


def load_scenario(path: str | Path) -> Scenario:
    """Load and strictly validate a scenario YAML file."""
    scenario_path = Path(path).resolve()
    try:
        with scenario_path.open(encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not read scenario {scenario_path}: {exc}") from exc
    mapping = _mapping(raw, "scenario")
    _reject_unknown(mapping, {"world", "rate_hz", "seed", "ground", "agents", "events", "interactions"}, "scenario")
    world = mapping.get("world")
    if not isinstance(world, str) or not world.strip():
        raise ValueError("scenario field 'world' must be a non-empty string")
    rate_hz = _number(mapping.get("rate_hz", 10.0), "scenario field 'rate_hz'", minimum=0.0, strict=True)
    seed = mapping.get("seed", 7)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("scenario field 'seed' must be an integer")
    interactions = mapping.get("interactions", False)
    if not isinstance(interactions, bool):
        raise ValueError("scenario field 'interactions' must be a boolean")

    ground = TriangulatedGrid(None)
    if "ground" in mapping:
        raw_ground = _mapping(mapping["ground"], "ground")
        _reject_unknown(raw_ground, {"origin_xy", "cell_m", "file"}, "ground")
        if not {"origin_xy", "cell_m", "file"}.issubset(raw_ground):
            raise ValueError("scenario field 'ground' requires origin_xy, cell_m, and file")
        origin = _pair(raw_ground["origin_xy"], "ground.origin_xy")
        cell_m = _number(raw_ground["cell_m"], "ground.cell_m", minimum=0.0, strict=True)
        filename = raw_ground["file"]
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("scenario field 'ground.file' must be a non-empty relative path")
        relative = Path(filename)
        if relative.is_absolute():
            raise ValueError("scenario field 'ground.file' must be relative to the scenario file")
        try:
            import numpy as np

            heights = np.load(scenario_path.parent / relative, allow_pickle=False)
        except (OSError, ValueError, ImportError) as exc:
            raise ValueError(f"scenario ground.file could not be loaded: {exc}") from exc
        if heights.ndim != 2 or min(heights.shape, default=0) < 1:
            raise ValueError("scenario ground.file must contain a non-empty 2D height array")
        if not np.issubdtype(heights.dtype, np.floating):
            raise ValueError("scenario ground.file must contain a 2D float array")
        if not bool(np.isfinite(heights).all()):
            raise ValueError("scenario ground.file must contain only finite heights")
        ground = TriangulatedGrid(heights, origin[0], origin[1], cell_m)

    raw_agents = mapping.get("agents")
    if not isinstance(raw_agents, list):
        raise ValueError("scenario field 'agents' must be a list")
    agents = tuple(_parse_agent(agent, index) for index, agent in enumerate(raw_agents))
    models = [agent.model for agent in agents]
    if len(set(models)) != len(models):
        duplicate = next(model for index, model in enumerate(models) if model in models[:index])
        raise ValueError(f"scenario has duplicate agent model {duplicate!r}")
    by_model = {agent.model: agent for agent in agents}
    for agent in agents:
        if agent.behavior != "follow":
            continue
        leader = by_model.get(agent.leader)
        if leader is None or leader is agent:
            raise agent_error(agent.model, "leader", f"names unknown agent {agent.leader!r}")
        if leader.behavior != "route" or leader.mode != "loop" or leader.kind == "vehicle":
            raise agent_error(agent.model, "leader", "must be a non-vehicle agent on a loop route")
        if leader.group != agent.group:
            raise agent_error(agent.model, "leader", "must be in the follower's group")
    raw_events = mapping.get("events", [])
    if not isinstance(raw_events, list):
        raise ValueError("scenario field 'events' must be a list")
    events = tuple(sorted((_parse_event(event, index, by_model) for index, event in enumerate(raw_events)),
                          key=lambda event: (event.t_s, event.model, event.type)))
    return Scenario(world, rate_hz, seed, ground, agents, events, interactions)


def select_agents(scenario: Scenario, groups: tuple[str, ...] | list[str] | None = None) -> tuple[AgentSpec, ...]:
    selected = {"ambient"} if groups is None else set(groups)
    return tuple(agent for agent in scenario.agents if agent.group in selected)


# --------------------------------------------------------------------------- route geometry

def next_route_index(index: int, direction: int, count: int, mode: str) -> tuple[int | None, int]:
    """Next waypoint index and direction for a route ``mode``; None once a 'once' route ends."""
    if count <= 1:
        return None, direction
    if mode == "once":
        return (index + 1, direction) if index < count - 1 else (None, direction)
    if mode == "loop":
        return (index + direction) % count, direction
    if direction > 0 and index == count - 1:
        direction = -1
    elif direction < 0 and index == 0:
        direction = 1
    return index + direction, direction


def route_cycle_length(spec: AgentSpec) -> float:
    if len(spec.waypoints) < 2:
        return 0.0
    forward = sum(
        math.hypot(spec.waypoints[(index + 1) % len(spec.waypoints)].x - waypoint.x,
                   spec.waypoints[(index + 1) % len(spec.waypoints)].y - waypoint.y)
        for index, waypoint in enumerate(spec.waypoints[:-1] if spec.mode != "loop" else spec.waypoints)
    )
    return 2.0 * forward if spec.mode == "pingpong" else forward


class LoopPolyline:
    """A closed polyline parameterised by arc length (for followers of a loop route)."""

    def __init__(self, waypoints: tuple[Waypoint, ...]) -> None:
        self.points = [(w.x, w.y) for w in waypoints]
        self.cumulative = [0.0]
        for a, b in zip(self.points, self.points[1:] + self.points[:1]):
            self.cumulative.append(self.cumulative[-1] + math.dist(a, b))
        self.length = self.cumulative[-1]

    def sample(self, s: float) -> tuple[float, float, float]:
        s %= self.length
        index = min(len(self.points) - 1, bisect_right(self.cumulative, s) - 1)
        a, b = self.points[index], self.points[(index + 1) % len(self.points)]
        segment = self.cumulative[index + 1] - self.cumulative[index]
        t = 0.0 if segment <= _EPSILON else (s - self.cumulative[index]) / segment
        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, math.atan2(b[1] - a[1], b[0] - a[0])


# --------------------------------------------------------------------------- wander-area geometry

def _point_on_segment(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> bool:
    px, py = point
    cross = (px - a[0]) * (b[1] - a[1]) - (py - a[1]) * (b[0] - a[0])
    if abs(cross) > 1e-8:
        return False
    return (
        min(a[0], b[0]) - _EPSILON <= px <= max(a[0], b[0]) + _EPSILON
        and min(a[1], b[1]) - _EPSILON <= py <= max(a[1], b[1]) + _EPSILON
    )


def point_in_polygon(point: tuple[float, float], polygon: tuple[tuple[float, float], ...]) -> bool:
    """Return true for points inside or on the boundary of a polygon."""
    x, y = point
    inside = False
    for index, a in enumerate(polygon):
        b = polygon[(index + 1) % len(polygon)]
        if _point_on_segment(point, a, b):
            return True
        if (a[1] > y) != (b[1] > y):
            crossing_x = (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]
            if x < crossing_x:
                inside = not inside
    return inside


def segment_stays_in_polygon(
    start: tuple[float, float], end: tuple[float, float], polygon: tuple[tuple[float, float], ...]
) -> bool:
    if not point_in_polygon(start, polygon) or not point_in_polygon(end, polygon):
        return False
    rx, ry = end[0] - start[0], end[1] - start[1]
    parameters = [0.0, 1.0]
    for index, c in enumerate(polygon):
        d = polygon[(index + 1) % len(polygon)]
        sx, sy = d[0] - c[0], d[1] - c[1]
        denominator = rx * sy - ry * sx
        if abs(denominator) <= _EPSILON:
            continue
        qx, qy = c[0] - start[0], c[1] - start[1]
        t = (qx * sy - qy * sx) / denominator
        u = (qx * ry - qy * rx) / denominator
        if -_EPSILON <= t <= 1.0 + _EPSILON and -_EPSILON <= u <= 1.0 + _EPSILON:
            parameters.append(min(max(t, 0.0), 1.0))
    parameters = sorted(set(parameters))
    return all(
        point_in_polygon((start[0] + rx * (left + right) * 0.5, start[1] + ry * (left + right) * 0.5), polygon)
        for left, right in zip(parameters, parameters[1:])
        if right - left > _EPSILON
    )
