"""Curvature-bounded vehicle paths shared by the scenario driver and the world validator.

A vehicle route is a polyline of lane waypoints. Every interior corner (and, for loops, the
corner at the first waypoint) is replaced by a tangent circular arc of the vehicle's minimum
turn radius, so the driven trajectory has continuous heading and curvature at most
``1 / radius``. Corners too tight for the radius are rejected instead of silently drawn with
a sharper curve. A waypoint that is rounded into an arc cannot carry a dwell (``hold_s``);
``FilletedPath`` raises instead of dropping it.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

_EPS = 1e-9


@dataclass(frozen=True)
class _Line:
    start: tuple[float, float]
    heading: float
    length: float

    def at(self, s: float) -> tuple[float, float, float, float]:
        return (self.start[0] + math.cos(self.heading) * s, self.start[1] + math.sin(self.heading) * s,
                self.heading, 0.0)


@dataclass(frozen=True)
class _Arc:
    centre: tuple[float, float]
    radius: float
    start_angle: float  # polar angle of the start point about the centre
    turn: float  # signed total heading change (left positive)
    length: float

    def at(self, s: float) -> tuple[float, float, float, float]:
        direction = 1.0 if self.turn > 0 else -1.0
        angle = self.start_angle + direction * s / self.radius
        heading = angle + direction * math.pi / 2
        return (self.centre[0] + self.radius * math.cos(angle), self.centre[1] + self.radius * math.sin(angle),
                math.atan2(math.sin(heading), math.cos(heading)), direction / self.radius)


class FilletedPath:
    """Arc-length parameterised path.

    ``stops`` is a sorted tuple of (arc length, waypoint index, hold seconds) for every
    waypoint that the path passes through exactly (i.e. that is not rounded into an arc).
    """

    def __init__(self, waypoints: Sequence[Sequence[float]], radius: float, *, closed: bool,
                 name: str = "path") -> None:
        points = [(float(p[0]), float(p[1])) for p in waypoints]
        holds = [float(p[2]) if len(p) > 2 else 0.0 for p in waypoints]
        if len(points) < 2:
            raise ValueError(f"{name}: a vehicle path needs at least two waypoints")
        self.closed = closed
        self.radius = radius
        count = len(points)
        corners = range(count) if closed else range(1, count - 1)
        cut: dict[int, float] = {}
        arcs: dict[int, _Arc] = {}
        for index in corners:
            previous, here, following = points[index - 1], points[index], points[(index + 1) % count]
            if math.dist(previous, here) < _EPS or math.dist(here, following) < _EPS:
                raise ValueError(f"{name}: waypoint {index} repeats its neighbour")
            h_in = math.atan2(here[1] - previous[1], here[0] - previous[0])
            h_out = math.atan2(following[1] - here[1], following[0] - here[0])
            turn = math.atan2(math.sin(h_out - h_in), math.cos(h_out - h_in))
            if abs(turn) < 1e-6:
                continue
            if abs(abs(turn) - math.pi) < 1e-6:
                raise ValueError(f"{name}: waypoint {index} reverses direction; vehicles cannot turn in place")
            if holds[index] > 0.0:
                raise ValueError(f"{name}: waypoint {index} is a {math.degrees(abs(turn)):.0f} degree corner "
                                 "and cannot carry hold_s; put the stop on a straight")
            offset = radius * math.tan(abs(turn) / 2)
            cut[index] = offset
            start = (here[0] - math.cos(h_in) * offset, here[1] - math.sin(h_in) * offset)
            side = 1.0 if turn > 0 else -1.0
            centre = (start[0] - side * math.sin(h_in) * radius, start[1] + side * math.cos(h_in) * radius)
            start_angle = math.atan2(start[1] - centre[1], start[0] - centre[0])
            arcs[index] = _Arc(centre, radius, start_angle, turn, radius * abs(turn))
        self.segments: list[_Line | _Arc] = []
        stops: list[tuple[float, int, float]] = []
        if 0 not in cut:
            stops.append((0.0, 0, holds[0]))
        edges = range(count) if closed else range(count - 1)
        s = 0.0
        for index in edges:
            here, following = points[index], points[(index + 1) % count]
            nxt = (index + 1) % count
            length = math.dist(here, following)
            begin, end = cut.get(index, 0.0), cut.get(nxt, 0.0)
            if begin + end > length + 1e-6:
                raise ValueError(f"{name}: segment {index}->{nxt} ({length:.2f} m) is too short for "
                                 f"{radius:.1f} m turns at both ends")
            heading = math.atan2(following[1] - here[1], following[0] - here[0])
            start = (here[0] + math.cos(heading) * begin, here[1] + math.sin(heading) * begin)
            straight = length - begin - end
            if straight > _EPS:
                self.segments.append(_Line(start, heading, straight))
                s += straight
            if nxt in arcs:
                self.segments.append(arcs[nxt])
                s += arcs[nxt].length
            elif not (closed and nxt == 0):
                stops.append((s, nxt, holds[nxt]))
        self.length = s
        self.stops = tuple(stops)
        self._starts: list[float] = []
        total = 0.0
        for segment in self.segments:
            self._starts.append(total)
            total += segment.length

    def sample(self, s: float) -> tuple[float, float, float, float]:
        """(x, y, heading, signed curvature) at arc length ``s`` (wrapped for loops)."""
        if self.closed:
            s %= self.length
        s = min(max(s, 0.0), self.length)
        low, high = 0, len(self.segments) - 1
        while low < high:
            mid = (low + high + 1) // 2
            if self._starts[mid] <= s:
                low = mid
            else:
                high = mid - 1
        segment = self.segments[low]
        return segment.at(min(segment.length, s - self._starts[low]))

    def curvature_ahead(self, s: float, distance: float) -> list[tuple[float, float]]:
        """(distance ahead, |curvature|) of every arc that the next ``distance`` metres touch.

        ``s`` may be any absolute arc length (loops wrap; any number of laps ahead is covered).
        An arc that contains ``s`` reports distance 0.
        """
        result = []
        if self.closed:
            here = s % self.length
        else:
            here = min(max(s, 0.0), self.length)
        for start, segment in zip(self._starts, self.segments):
            if not isinstance(segment, _Arc):
                continue
            if start <= here < start + segment.length:
                result.append((0.0, 1.0 / segment.radius))
                first = start + (self.length if self.closed else math.inf) - here
            else:
                first = start - here
                if first < 0.0:
                    first = first + self.length if self.closed else math.inf
            ahead = first
            while ahead <= distance:
                result.append((ahead, 1.0 / segment.radius))
                if not self.closed:
                    break
                ahead += self.length
        return sorted(result)

    def points(self, spacing: float = 0.5) -> list[tuple[float, float, float]]:
        """(x, y, heading) samples at most ``spacing`` apart, including the closing piece."""
        count = max(2, math.ceil(self.length / spacing))
        return [self.sample(self.length * i / count)[:3] for i in range(count + (0 if self.closed else 1))]
