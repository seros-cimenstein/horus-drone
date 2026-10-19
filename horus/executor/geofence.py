"""Geofence geometry and the enforced flight envelope (polygon inset, clamping, scan rasters)."""

from __future__ import annotations

import math
from typing import Any, Mapping

from horus.config import ExecutorSettings
from horus.flight import Pose

_Point = tuple[float, float]
_GEOMETRY_EPSILON = 1e-9


def _cross(left: _Point, right: _Point) -> float:
    return left[0] * right[1] - left[1] * right[0]


def _signed_area(polygon: tuple[_Point, ...]) -> float:
    return 0.5 * sum(
        _cross(polygon[index], polygon[(index + 1) % len(polygon)])
        for index in range(len(polygon))
    )


def _distance_to_segment(point: _Point, start: _Point, end: _Point) -> float:
    edge = (end[0] - start[0], end[1] - start[1])
    length_squared = edge[0] * edge[0] + edge[1] * edge[1]
    if length_squared == 0.0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    projection = (
        (point[0] - start[0]) * edge[0] + (point[1] - start[1]) * edge[1]
    ) / length_squared
    projection = max(0.0, min(1.0, projection))
    closest = (start[0] + projection * edge[0], start[1] + projection * edge[1])
    return math.hypot(point[0] - closest[0], point[1] - closest[1])


def _orientation(a: _Point, b: _Point, c: _Point) -> float:
    return _cross((b[0] - a[0], b[1] - a[1]), (c[0] - a[0], c[1] - a[1]))


def _segments_intersect(a: _Point, b: _Point, c: _Point, d: _Point) -> bool:
    ab_c, ab_d = _orientation(a, b, c), _orientation(a, b, d)
    cd_a, cd_b = _orientation(c, d, a), _orientation(c, d, b)
    epsilon = _GEOMETRY_EPSILON
    if (
        ((ab_c > epsilon and ab_d < -epsilon) or (ab_c < -epsilon and ab_d > epsilon))
        and ((cd_a > epsilon and cd_b < -epsilon) or (cd_a < -epsilon and cd_b > epsilon))
    ):
        return True

    def on_segment(start: _Point, end: _Point, point: _Point, cross: float) -> bool:
        return (
            abs(cross) <= epsilon
            and min(start[0], end[0]) - epsilon <= point[0] <= max(start[0], end[0]) + epsilon
            and min(start[1], end[1]) - epsilon <= point[1] <= max(start[1], end[1]) + epsilon
        )

    return (
        on_segment(a, b, c, ab_c)
        or on_segment(a, b, d, ab_d)
        or on_segment(c, d, a, cd_a)
        or on_segment(c, d, b, cd_b)
    )


def _validate_simple_polygon(polygon: tuple[_Point, ...]) -> float:
    if len(polygon) < 3:
        raise ValueError("geofence_polygon_ned needs at least three vertices")
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        if math.hypot(end[0] - start[0], end[1] - start[1]) <= _GEOMETRY_EPSILON:
            raise ValueError("geofence_polygon_ned cannot contain duplicate adjacent vertices")
    area = _signed_area(polygon)
    if abs(area) <= _GEOMETRY_EPSILON:
        raise ValueError("geofence_polygon_ned must enclose a non-zero area")
    count = len(polygon)
    for first in range(count):
        a, b = polygon[first], polygon[(first + 1) % count]
        for second in range(first + 1, count):
            if second == first + 1 or (first == 0 and second == count - 1):
                continue
            c, d = polygon[second], polygon[(second + 1) % count]
            if _segments_intersect(a, b, c, d):
                raise ValueError("geofence_polygon_ned must be a simple polygon")
    return area


def _point_in_polygon(point: _Point, polygon: tuple[_Point, ...]) -> bool:
    inside = False
    x, y = point
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        if _distance_to_segment(point, start, end) <= _GEOMETRY_EPSILON:
            return True
        if (start[1] > y) != (end[1] > y):
            crossing_x = start[0] + (y - start[1]) * (end[0] - start[0]) / (end[1] - start[1])
            if x < crossing_x:
                inside = not inside
    return inside


def _inset_polygon(polygon: tuple[_Point, ...], margin_m: float) -> tuple[_Point, ...]:
    area = _validate_simple_polygon(polygon)
    if margin_m == 0.0:
        return polygon
    orientation = 1.0 if area > 0.0 else -1.0
    edges: list[tuple[_Point, _Point, _Point]] = []
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        direction = (end[0] - start[0], end[1] - start[1])
        length = math.hypot(*direction)
        inward = (
            -orientation * direction[1] / length,
            orientation * direction[0] / length,
        )
        edges.append((direction, inward, start))

    inset: list[_Point] = []
    for index, point in enumerate(polygon):
        previous_direction, previous_normal, _ = edges[index - 1]
        current_direction, current_normal, _ = edges[index]
        previous_origin = (
            point[0] + previous_normal[0] * margin_m,
            point[1] + previous_normal[1] * margin_m,
        )
        current_origin = (
            point[0] + current_normal[0] * margin_m,
            point[1] + current_normal[1] * margin_m,
        )
        denominator = _cross(previous_direction, current_direction)
        if abs(denominator) <= _GEOMETRY_EPSILON:
            if (
                previous_direction[0] * current_direction[0]
                + previous_direction[1] * current_direction[1]
            ) <= 0.0:
                raise ValueError("geofence_polygon_ned has a degenerate vertex")
            inset.append(previous_origin)
            continue
        offset = (
            current_origin[0] - previous_origin[0],
            current_origin[1] - previous_origin[1],
        )
        distance_along_previous = _cross(offset, current_direction) / denominator
        inset.append(
            (
                previous_origin[0] + distance_along_previous * previous_direction[0],
                previous_origin[1] + distance_along_previous * previous_direction[1],
            )
        )

    result = tuple(inset)
    inset_area = _validate_simple_polygon(result)
    if area * inset_area <= 0.0:
        raise ValueError("geofence_margin_m collapses geofence_polygon_ned")
    for index, start in enumerate(result):
        end = result[(index + 1) % len(result)]
        for point in (
            start,
            ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0),
        ):
            if not _point_in_polygon(point, polygon):
                raise ValueError("geofence_margin_m collapses geofence_polygon_ned")
            clearance = min(
                _distance_to_segment(point, polygon[edge], polygon[(edge + 1) % len(polygon)])
                for edge in range(len(polygon))
            )
            if clearance + _GEOMETRY_EPSILON < margin_m:
                raise ValueError("geofence_margin_m collapses geofence_polygon_ned")
    return result


def _nearest_polygon_point(point: _Point, polygon: tuple[_Point, ...]) -> _Point:
    if _point_in_polygon(point, polygon):
        return point
    nearest: _Point | None = None
    nearest_distance_squared = math.inf
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        edge = (end[0] - start[0], end[1] - start[1])
        length_squared = edge[0] * edge[0] + edge[1] * edge[1]
        projection = (
            0.0 if length_squared == 0.0 else
            max(0.0, min(1.0, ((point[0] - start[0]) * edge[0] + (point[1] - start[1]) * edge[1]) / length_squared))
        )
        candidate = (start[0] + projection * edge[0], start[1] + projection * edge[1])
        distance_squared = (
            (point[0] - candidate[0]) ** 2 + (point[1] - candidate[1]) ** 2
        )
        if distance_squared < nearest_distance_squared:
            nearest, nearest_distance_squared = candidate, distance_squared
    if nearest is None:
        raise ValueError("geofence_polygon_ned contains no projectable edge")
    return nearest


class Envelope:
    """Enforced flight limits, built once from settings: inset geofence, altitude, leg length."""

    def __init__(self, settings: ExecutorSettings) -> None:
        self.settings = settings
        polygon = settings.geofence_polygon_ned
        # Validated and inset once; every clamp and get_map read this polygon.
        self.polygon = None if polygon is None else _inset_polygon(polygon, settings.geofence_margin_m)

    def as_dict(self) -> dict[str, Any]:
        """Operator-facing limits: inset geofence (polygon NED or radius), altitude, speed, leg length."""
        settings = self.settings
        envelope: dict[str, Any] = {
            "altitude_m": [settings.min_altitude_m, settings.max_altitude_m],
            "max_speed_m_s": settings.max_speed_m_s,
            "cruise_speed_m_s": settings.cruise_speed_m_s,
            "cruise_min_altitude_m": settings.cruise_min_altitude_m,
            "max_vertical_speed_m_s": settings.max_vertical_speed_m_s,
            "max_distance_m": settings.max_distance_m,
        }
        if self.polygon is None:
            envelope["geofence_radius_m"] = settings.geofence_radius_m
        else:
            envelope["geofence_polygon_ned"] = [
                [round(north, 3), round(east, 3)] for north, east in self.polygon
            ]
        return envelope

    def clamp_pose(self, requested: Pose, current: Pose | None = None) -> tuple[Pose, list[str]]:
        """Clamp a target to the altitude band, one leg from ``current`` (origin if unknown) and the fence."""
        settings = self.settings
        reasons: list[str] = []
        current = current or Pose(0.0, 0.0, 0.0)
        north, east, up = requested.north_m, requested.east_m, requested.up_m
        altitude = min(settings.max_altitude_m, max(settings.min_altitude_m, up))
        if altitude != up:
            reasons.append("altitude clamped to configured flight envelope")
        distance_from_current = math.hypot(north - current.north_m, east - current.east_m)
        if distance_from_current > settings.max_distance_m:
            scale = settings.max_distance_m / distance_from_current
            north = current.north_m + (north - current.north_m) * scale
            east = current.east_m + (east - current.east_m) * scale
            reasons.append("travel distance clamped to max_distance_m")
        if self.polygon is not None:
            before_geofence = (north, east)
            north, east = _nearest_polygon_point(before_geofence, self.polygon)
            if (north, east) != before_geofence:
                reasons.append(
                    f"horizontal target clamped to polygon geofence "
                    f"(inset margin {settings.geofence_margin_m:g} m)"
                )
        else:
            radius = math.hypot(north, east)
            if radius > settings.geofence_radius_m:
                scale = settings.geofence_radius_m / radius
                north *= scale
                east *= scale
                reasons.append("horizontal target clamped to geofence radius")
        return Pose(north, east, altitude), reasons

    def scan_waypoints(
        self, area: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[Pose], list[str]]:
        """Boustrophedon waypoints over ``area``, each clamped from the origin."""
        required = ("north_min_m", "north_max_m", "east_min_m", "east_max_m")
        if any(key not in area for key in required):
            raise ValueError("scan area needs north_min_m, north_max_m, east_min_m, east_max_m")
        n0, n1, e0, e1 = (float(area[key]) for key in required)
        altitude = float(area.get("altitude_m", 2.0))
        spacing = float(area.get("spacing_m", 2.0))
        if not all(math.isfinite(v) for v in (n0, n1, e0, e1, altitude, spacing)) or spacing <= 0:
            raise ValueError("scan area coordinates must be finite and spacing_m positive")
        n0, n1 = sorted((n0, n1))
        e0, e1 = sorted((e0, e1))
        requested = {
            "north_min_m": n0,
            "north_max_m": n1,
            "east_min_m": e0,
            "east_max_m": e1,
            "altitude_m": altitude,
            "spacing_m": spacing,
        }
        reasons: list[str] = []
        extent = (
            self.settings.max_distance_m
            if self.polygon is not None
            else min(self.settings.max_distance_m, self.settings.geofence_radius_m)
        )
        bounded = tuple(max(-extent, min(extent, value)) for value in (n0, n1, e0, e1))
        if bounded != (n0, n1, e0, e1):
            reasons.append("scan bounds clamped to max-distance/geofence envelope")
        n0, n1 = sorted((bounded[0], bounded[1]))
        e0, e1 = sorted((bounded[2], bounded[3]))
        safe_spacing = min(max(0.5, spacing), self.settings.max_distance_m)
        if safe_spacing != spacing:
            reasons.append("scan spacing clamped to safe range")
        spacing = safe_spacing
        rows = max(1, math.ceil((n1 - n0) / spacing))
        waypoints: list[Pose] = []
        for row in range(rows + 1):
            north = min(n1, n0 + row * spacing)
            east_pair = (e0, e1) if row % 2 == 0 else (e1, e0)
            waypoints.extend((Pose(north, east_pair[0], altitude), Pose(north, east_pair[1], altitude)))
        safe: list[Pose] = []
        for point in waypoints:
            clamped, why = self.clamp_pose(point)
            safe.append(clamped)
            reasons.extend(why)
        if len(set(safe)) < 2:
            raise ValueError("scan area collapses inside configured geofence/distance limits")
        return requested, safe, list(dict.fromkeys(reasons))
