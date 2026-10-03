"""Procedural flight and mission planning over generated worlds.

Everything here is derived from a world's layout manifest plus its static collision
geometry, so the same planner produces vantages, safe legs and missions for the canonical
worlds and for any procedural seed. Frames: Gazebo x east, y north, z up; HORUS NED
north = y, east = x, up = z (home pad at the origin).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sim.worldgen.geometry import CollisionWorld

_Point = tuple[float, float, float]

# Planner limits for the canonical 150 m worlds: callers that plan over a canonical world keep
# these values, while callers that fly a generated map pass limits derived from that world's
# profile (see sim/scripts/campaign_episode.py). Both are explicit parameters below.
CANONICAL_MAX_LEG_M = 60.0
CANONICAL_VANTAGE_BOUNDS_M = 60.0

# Torso point heights above ground by posture; the detector box centre sits near these.
POSTURE_TORSO_M = {"standing": 1.1, "sitting": 0.7, "lying": 0.25}
POSTURE_TOP_M = {"standing": 1.9, "sitting": 1.2, "lying": 0.45}


@dataclass(frozen=True, slots=True)
class Camera:
    """The outdoor gimbal camera: stabilised pitch, yaw locked to the airframe heading."""

    pitch_deg: float = -45.0
    hfov_deg: float = 84.0
    width: int = 640
    height: int = 480

    @property
    def focal_px(self) -> float:
        return (self.width / 2.0) / math.tan(math.radians(self.hfov_deg) / 2.0)

    @property
    def vfov_deg(self) -> float:
        return 2.0 * math.degrees(math.atan((self.height / 2.0) / self.focal_px))

    def depression_band_deg(self, margin_deg: float = 0.0) -> tuple[float, float]:
        """Angles below the horizon that stay inside the frame on the image's vertical centre line."""
        centre = -self.pitch_deg
        half = self.vfov_deg / 2.0
        return centre - half + margin_deg, centre + half - margin_deg

    def project(self, eye: _Point, yaw_rad: float, point: _Point) -> tuple[float, float] | None:
        """Pixel (u, v) of a world point, or None when it is behind the camera."""
        pitch = math.radians(self.pitch_deg)
        cp, sp, cy, sy = math.cos(pitch), math.sin(pitch), math.cos(yaw_rad), math.sin(yaw_rad)
        forward = (cp * cy, cp * sy, sp)
        right = (sy, -cy, 0.0)
        up = (-cy * sp, -sy * sp, cp)
        d = (point[0] - eye[0], point[1] - eye[1], point[2] - eye[2])
        depth = _dot(d, forward)
        if depth <= 1e-6:
            return None
        return (
            self.width / 2.0 + self.focal_px * _dot(d, right) / depth,
            self.height / 2.0 - self.focal_px * _dot(d, up) / depth,
        )

    def in_frame(self, eye: _Point, yaw_rad: float, point: _Point, margin_px: float = 0.0) -> bool:
        pixel = self.project(eye, yaw_rad, point)
        return pixel is not None and (
            margin_px <= pixel[0] <= self.width - margin_px
            and margin_px <= pixel[1] <= self.height - margin_px
        )


def _dot(a: _Point, b: _Point) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def heading_deg(yaw_rad: float) -> float:
    """Gazebo yaw (counter-clockwise from east) to HORUS heading (clockwise from north)."""
    return (90.0 - math.degrees(yaw_rad)) % 360.0


def ned_pose(point: _Point) -> dict[str, float]:
    return {"north_m": round(point[1], 3), "east_m": round(point[0], 3), "up_m": round(point[2], 3)}


class Ground:
    """Terrain heights from a manifest's ground grid (flat when absent), on the same
    triangulated surface that Gazebo renders and collides with (``sim.worldgen.surface``)."""

    def __init__(self, manifest: dict[str, Any], manifest_path: Path | None = None) -> None:
        from sim.worldgen.surface import TriangulatedGrid

        ground = manifest.get("ground")
        self.surface = TriangulatedGrid(None)
        if ground:
            import numpy as np

            base = manifest_path.parent if manifest_path is not None else Path(".")
            rows = np.load(base / ground["file"])
            self.surface = TriangulatedGrid(rows, ground["origin_xy"][0], ground["origin_xy"][1],
                                            float(ground["cell_m"]))

    @classmethod
    def from_surface(cls, surface) -> "Ground":
        ground = cls({})
        ground.surface = surface
        return ground

    def height(self, x: float, y: float) -> float:
        return self.surface.height(x, y)

    def visible(self, a: _Point, b: _Point) -> bool:
        """True when the terrain does not cut the segment a→b (ignoring the last 0.3 m at b)."""
        length = math.dist(a, b)
        if length <= 0.3:
            return True
        t = 1.0 - 0.3 / length
        end = tuple(a[i] + (b[i] - a[i]) * t for i in range(3))
        return self.surface.segment_clearance(a, end) > 0.0


@dataclass(frozen=True, slots=True)
class Vantage:
    target: str
    eye: _Point
    yaw_rad: float
    slant_m: float
    depression_deg: float
    clearance_m: float
    blocker: str | None  # first static collision on the camera→torso line, if any

    @property
    def heading_deg(self) -> float:
        return heading_deg(self.yaw_rad)


def person_point(person: dict[str, Any], ground: Ground, posture: str, *, top: bool = False) -> _Point:
    """Torso (or top) point above the ground, from the actor's measured envelope when known."""
    x, y = person["position"]
    envelope = person.get("envelope")
    if envelope:
        offset = envelope["top_z_m"] if top else envelope["torso_z_m"]
    else:
        offset = (POSTURE_TOP_M if top else POSTURE_TORSO_M)[posture]
    return x, y, ground.height(x, y) + offset


def column_clearance(world: CollisionWorld, x: float, y: float, z_low: float, z_high: float,
                     radius: float) -> float:
    clearance, _ = world.segment_clearance((x, y, z_low), (x, y, z_high), radius=radius)
    return clearance


def plan_vantage(
    world: CollisionWorld,
    ground: Ground,
    camera: Camera,
    target_name: str,
    target_point: _Point,
    rng: random.Random,
    *,
    transit_alt: float,
    slant_m: tuple[float, float] = (5.0, 11.0),
    min_agl_m: float = 3.0,
    min_clearance_m: float = 2.0,
    drone_radius_m: float = 0.6,
    require_los: bool = True,
    bounds: float = CANONICAL_VANTAGE_BOUNDS_M,
    attempts: int = 400,
) -> Vantage | None:
    """Sample a hover point that frames the target inside the camera's depression band.

    The hover point, and its vertical column up to the transit altitude, keep
    ``min_clearance_m`` from every static collision (after the drone radius) so a
    climb-cruise-descend route to it is safe. ``bounds`` is the caller-supplied maximum
    ``|north|``/``|east|`` offset of the hover point: callers over a canonical world keep
    ``CANONICAL_VANTAGE_BOUNDS_M`` (the 60 m actor geofence inset); callers over a generated
    map pass the map's flight-profile geofence extent.
    """
    low, high = camera.depression_band_deg(margin_deg=8.0)
    for _ in range(attempts):
        slant = rng.uniform(*slant_m)
        depression = math.radians(rng.uniform(low, high))
        bearing = rng.uniform(-math.pi, math.pi)
        horizontal = slant * math.cos(depression)
        x = target_point[0] + horizontal * math.cos(bearing)
        y = target_point[1] + horizontal * math.sin(bearing)
        z = target_point[2] + slant * math.sin(depression)
        if max(abs(x), abs(y)) > bounds or z - ground.height(x, y) < min_agl_m or z >= transit_alt:
            continue
        column = column_clearance(world, x, y, z, transit_alt, drone_radius_m)
        if column < min_clearance_m:
            continue
        yaw = math.atan2(target_point[1] - y, target_point[0] - x)
        eye = (x, y, z)
        if not camera.in_frame(eye, yaw, target_point, margin_px=40.0):
            continue
        blocker = world.blocking(eye, target_point)
        blocker_name = None if blocker is None else blocker.name
        if blocker_name is None and not ground.visible(eye, target_point):
            blocker_name = "terrain"
        if require_los and blocker_name is not None:
            continue
        return Vantage(target_name, eye, yaw, slant, math.degrees(depression), column, blocker_name)
    return None


def legs_clearance(world: CollisionWorld, start: _Point, legs: list[tuple[_Point, float]],
                   drone_radius_m: float = 0.6) -> float:
    clearance = math.inf
    previous = start
    for point, _heading in legs:
        leg, _ = world.segment_clearance(previous, point, radius=drone_radius_m)
        clearance = min(clearance, leg)
        previous = point
    return clearance


def cruise_legs(start: _Point, eye: _Point, heading: float, transit_alt: float,
                max_leg_m: float = CANONICAL_MAX_LEG_M) -> list[tuple[_Point, float]]:
    """Climb over the start, cruise at the transit altitude, then descend onto ``eye``.

    Horizontal cruise is split so no leg exceeds ``max_leg_m`` (the executor caps each goto's
    travel at ``max_distance_m``). ``max_leg_m`` is caller-supplied: callers over a canonical
    world keep ``CANONICAL_MAX_LEG_M``; callers over a generated map pass that map's
    flight-profile ``max_distance_m``.
    """
    legs = [((start[0], start[1], transit_alt), heading)]
    distance = math.hypot(eye[0] - start[0], eye[1] - start[1])
    pieces = max(1, math.ceil(distance / max_leg_m))
    for piece in range(1, pieces + 1):
        t = piece / pieces
        legs.append(((start[0] + (eye[0] - start[0]) * t, start[1] + (eye[1] - start[1]) * t,
                      transit_alt), heading))
    legs.append((eye, heading))
    return legs


def _polyline_point(route: list[list[float]], distance: float) -> tuple[float, float, float]:
    """Point and direction (yaw) at an arc length along a polyline."""
    remaining = distance
    for a, b in zip(route, route[1:]):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if remaining <= length or b is route[-1]:
            t = 0.0 if length == 0 else min(1.0, remaining / length)
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, math.atan2(b[1] - a[1], b[0] - a[0])
        remaining -= length
    raise ValueError("route needs at least two points")


def route_length(route: list[list[float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(route, route[1:]))


@dataclass(frozen=True, slots=True)
class FollowPlan:
    stage: _Point
    yaw_rad: float
    slant_m: float
    lane_clearance_m: float
    column_clearance_m: float
    occluded_s: float
    ttl_s: float


def plan_follow(world: CollisionWorld, ground: Ground, manifest: dict[str, Any], *,
                drone_radius_m: float = 0.6, max_speed_m_s: float = 3.0, step_s: float = 0.1) -> FollowPlan:
    """Stage near the follow target and evaluate a pursuit follower against the static geometry.

    The follower mirrors the executor's follow skill: it holds its altitude and closes the
    horizontal gap to ``follower_horizontal_m`` from the target at up to ``max_speed_m_s``.
    ``occluded_s`` is the longest continuous interval in which a static collision blocks the
    camera→torso line; the lane clearance covers the whole pursuit path.
    """
    follow = manifest["follow"]
    route = follow["route"]
    altitude = float(follow["follower_altitude_m"])
    horizontal = float(follow["follower_horizontal_m"])
    speed = float(follow["speed_m_s"])
    transit = float(manifest["transit_altitude_m"])
    total = route_length(route)
    x0, y0, yaw0 = _polyline_point(route, 0.0)
    if follow.get("stage_xy"):
        sx, sy = follow["stage_xy"]
    else:
        sx, sy = x0 - horizontal * math.cos(yaw0), y0 - horizontal * math.sin(yaw0)
    stage = (float(sx), float(sy), altitude)
    stage_yaw = math.atan2(y0 - stage[1], x0 - stage[0])
    column = column_clearance(world, stage[0], stage[1], altitude, transit, drone_radius_m)
    target = next((person for person in manifest.get("people", []) if person["model"] == follow["model"]), None)
    torso_z = (target["envelope"]["torso_z_m"] if target and target.get("envelope")
               else POSTURE_TORSO_M["standing"])
    lane = math.inf
    longest = current = 0.0
    eye = stage
    t = 0.0
    while t * speed <= total + speed * step_s:
        x, y, _ = _polyline_point(route, min(t * speed, total))
        dx, dy = x - eye[0], y - eye[1]
        gap = math.hypot(dx, dy)
        move = max(-max_speed_m_s * step_s, min(max_speed_m_s * step_s, gap - horizontal))
        nxt = eye if gap < 1e-9 else (eye[0] + dx / gap * move, eye[1] + dy / gap * move, altitude)
        leg, _ = world.segment_clearance(eye, nxt, radius=drone_radius_m)
        lane = min(lane, leg, ground.surface.segment_clearance(eye, nxt) - drone_radius_m)
        eye = nxt
        torso = (x, y, ground.height(x, y) + torso_z)
        blocked = world.blocking(eye, torso) is not None or not ground.visible(eye, torso)
        current = current + step_s if blocked else 0.0
        longest = max(longest, current)
        t += step_s
    return FollowPlan(stage, stage_yaw, math.hypot(altitude, horizontal), lane, column,
                      round(longest, 2), math.ceil(total / speed) + 10.0)


def follow_mission(manifest: dict[str, Any], plan: FollowPlan, *, manifest_path: Path, mission_path: Path,
                   repo_root: Path, trace_path: str, output_dir: str) -> dict[str, Any]:
    """A runner mission (select by shirt colour, follow through the occluder, assert re-acquisition).

    The runner resolves ``driver`` against the mission file but passes ``driver_args`` literally,
    so the scenario path is repository-root relative (missions run with the repo root as cwd).
    """
    follow = manifest["follow"]
    transit = float(manifest["transit_altitude_m"])
    heading = heading_deg(plan.yaw_rad)
    scenario = (manifest_path.parent / manifest["scenario"]).resolve().relative_to(repo_root.resolve())
    depth = len(mission_path.parent.resolve().relative_to(repo_root.resolve()).parts)
    driver = Path(*([".."] * depth), "sim", "scripts", "civilian_life.py")
    goto = [
        {"tool": "goto", "arguments": {"pose": ned_pose(point), "heading_deg": round(leg_heading, 1),
                                       "ttl_s": 120.0}, "timeout_s": 125.0}
        for point, leg_heading in cruise_legs((0.0, 0.0, 0.0), plan.stage, heading, transit)[1:]
    ]
    return {
        "name": f"{manifest['world']}-follow-{follow['model']}",
        "driver": str(driver),
        "driver_args": ["--scenario", str(scenario), "--group", follow["group"], "--trace", trace_path],
        "steps": [
            {"tool": "take_off", "arguments": {"altitude_m": transit, "heading_deg": round(heading, 1),
                                               "ttl_s": 180.0}, "timeout_s": 185.0},
            *goto,
            {"select_candidate": {"attribute": "shirt_colour", "equals": follow["shirt_colour_attribute"],
                                  "choose": "largest", "store_as": "target", "timeout_s": 15.0}},
            {"start_driver": {}},
            {"tool": "follow", "arguments": {"id": "$target.id", "dist_m": round(plan.slant_m, 2),
                                             "ttl_s": plan.ttl_s},
             "timeout_s": plan.ttl_s + 5.0, "expect_status": "expired", "continue_on_failure": True,
             "store_as": "follow_result"},
            {"assert_reacquired": {"target": "$target", "follow": "$follow_result", "min_time_s": 1.0,
                                   "max_time_s": 12.0, "output_dir": output_dir}},
        ],
    }
