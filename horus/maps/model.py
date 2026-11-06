"""The common map schema every provider returns, in local NED metres from the HORUS origin.

Layers:
- ``places``: named points (an optional outline), e.g. a bus stop, a shop, a district.
- ``ways``: polylines of kind road/footway/crossing/path/…, optionally named.
- ``areas``: named or kinded polygons such as districts or parks.
- ``walkable``: polygons people can walk on, of kind footpath, road, …
- ``obstacles``: polygons nobody walks through, such as buildings.

``area`` clips the layers to a circle: places by their point, ways segment by segment, and
polygons that touch it to the circle's bounding square (holes too). ``find_places`` ranks places and named areas against a
free-text query. Names and kinds come from third-party data and are untrusted: tool output quotes
them (``quote_label``). Pure, standard library only.
"""

from __future__ import annotations

import gzip
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Point = tuple[float, float]

SCHEMA = "horus-map/1"


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    kind: str
    north_m: float
    east_m: float
    outline: tuple[Point, ...] = ()


@dataclass(frozen=True, slots=True)
class Way:
    kind: str
    points: tuple[Point, ...]
    name: str = ""


@dataclass(frozen=True, slots=True)
class Area:
    kind: str
    outline: tuple[Point, ...]
    name: str = ""
    # Polygons cut out of the outline (a fountain in a plaza's footpath).
    holes: tuple[tuple[Point, ...], ...] = ()

    def contains(self, point: Point) -> bool:
        return contains(self.outline, point) and not any(contains(hole, point) for hole in self.holes)


@dataclass(frozen=True, slots=True)
class MapData:
    places: tuple[Place, ...] = ()
    ways: tuple[Way, ...] = ()
    areas: tuple[Area, ...] = ()
    walkable: tuple[Area, ...] = ()
    obstacles: tuple[Area, ...] = ()
    attribution: str = ""
    sources: dict[str, Any] = field(default_factory=dict)

    def area(self, north_m: float, east_m: float, radius_m: float) -> MapData:
        """The features within ``radius_m`` of the point (see the module docstring)."""
        centre = (north_m, east_m)
        return MapData(
            places=tuple(p for p in self.places if math.dist((p.north_m, p.east_m), centre) <= radius_m),
            ways=tuple(
                Way(way.kind, piece, way.name)
                for way in self.ways
                for piece in _clip_polyline(way.points, centre, radius_m)
            ),
            areas=_clip_areas(self.areas, centre, radius_m),
            walkable=_clip_areas(self.walkable, centre, radius_m),
            obstacles=_clip_areas(self.obstacles, centre, radius_m),
            attribution=self.attribution,
            sources=self.sources,
        )

    def find_places(self, query: str, near: Point | None = None, limit: int = 5) -> list[tuple[Place, float]]:
        """``(place, score)`` best first: exact name, name prefix, name substring, every query word
        in the name or kind; ties go to the nearest to ``near``. A named area counts as one place
        at its outline's centroid, at distance 0 when ``near`` lies inside it; an area made of
        several outlines with one name (a district of several boxes) is listed once, its nearest."""
        words = query.casefold().split()
        if not words:
            return []
        phrase = " ".join(words)
        ranked: list[tuple[float, float, Place]] = []
        areas: dict[tuple[str, str], tuple[float, float, Place]] = {}
        candidates = [(p, False) for p in self.places] + [(_area_place(a), True) for a in self.areas if a.name]
        for place, from_area in candidates:
            name, kind = place.name.casefold(), place.kind.casefold()
            if name == phrase:
                score = 4.0
            elif name.startswith(phrase):
                score = 3.0
            elif phrase in name:
                score = 2.0
            elif all(word in name or word in kind for word in words):
                score = 1.0
            else:
                continue
            if near is None or (from_area and contains(place.outline, near)):
                distance = 0.0
            else:
                distance = math.dist((place.north_m, place.east_m), near)
            entry = (-score, distance, place)
            if not from_area:
                ranked.append(entry)
            elif (name, kind) not in areas or entry[:2] < areas[(name, kind)][:2]:
                areas[(name, kind)] = entry
        ranked.extend(areas.values())
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [(place, -score) for score, _, place in ranked[:limit]]


def load_map_file(path: str | Path) -> MapData:
    """Read a common-schema JSON map (``schema`` ``horus-map/1``), gzipped when it ends in ``.gz``."""
    path = Path(path)
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a {SCHEMA} map")

    def points(raw: Any) -> tuple[Point, ...]:
        return tuple((float(n), float(e)) for n, e in raw or ())

    def area(a: Any) -> Area:
        return Area(str(a["kind"]), points(a["outline"]), str(a.get("name", "")),
                    tuple(points(hole) for hole in a.get("holes", ())))

    return MapData(
        places=tuple(
            Place(str(p["name"]), str(p.get("kind", "")), float(p["north_m"]), float(p["east_m"]),
                  points(p.get("outline")))
            for p in data.get("places", ())
        ),
        ways=tuple(Way(str(w["kind"]), points(w["points"]), str(w.get("name", ""))) for w in data.get("ways", ())),
        areas=tuple(area(a) for a in data.get("areas", ())),
        walkable=tuple(area(w) for w in data.get("walkable", ())),
        obstacles=tuple(area(o) for o in data.get("obstacles", ())),
        attribution=str(data.get("attribution", "")),
        sources=dict(data.get("sources", {})),
    )


def map_to_json(data: MapData) -> dict[str, Any]:
    """The ``horus-map/1`` JSON document for ``data`` (``load_map_file`` reads it back)."""

    def points(raw: tuple[Point, ...]) -> list[list[float]]:
        return [[round(n, 2), round(e, 2)] for n, e in raw]

    def place(p: Place) -> dict[str, Any]:
        result: dict[str, Any] = {"name": p.name, "kind": p.kind, "north_m": round(p.north_m, 2),
                                  "east_m": round(p.east_m, 2)}
        if p.outline:
            result["outline"] = points(p.outline)
        return result

    def area(a: Area) -> dict[str, Any]:
        return {"kind": a.kind, **({"name": a.name} if a.name else {}), "outline": points(a.outline),
                **({"holes": [points(hole) for hole in a.holes]} if a.holes else {})}

    return {
        "schema": SCHEMA,
        "attribution": data.attribution,
        "sources": data.sources,
        "places": [place(p) for p in data.places],
        "ways": [{"kind": w.kind, **({"name": w.name} if w.name else {}), "points": points(w.points)} for w in data.ways],
        "areas": [area(a) for a in data.areas],
        "walkable": [area(w) for w in data.walkable],
        "obstacles": [area(o) for o in data.obstacles],
    }


def _area_place(area: Area) -> Place:
    n = len(area.outline)
    north = sum(p[0] for p in area.outline) / n if n else 0.0
    east = sum(p[1] for p in area.outline) / n if n else 0.0
    return Place(area.name, area.kind, north, east, area.outline)


def simplify_loop(loop: tuple[Point, ...], tolerance: float) -> tuple[Point, ...]:
    """Douglas-Peucker on a closed outline, split at the vertex farthest from the first."""
    if len(loop) <= 4:
        return loop
    far = max(range(len(loop)), key=lambda i: math.dist(loop[0], loop[i]))
    first = _douglas_peucker(loop[: far + 1], tolerance)
    second = _douglas_peucker(loop[far:] + loop[:1], tolerance)
    return tuple(first[:-1] + second[:-1])


def simplify_line(points: tuple[Point, ...], tolerance: float) -> tuple[Point, ...]:
    """Douglas-Peucker on an open polyline; the end points always stay."""
    return points if len(points) <= 2 else tuple(_douglas_peucker(points, tolerance))


def _douglas_peucker(points: Sequence[Point], tolerance: float) -> list[Point]:
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        a, b = points[lo], points[hi]
        index, distance = max(((i, _segment_distance(points[i], a, b)) for i in range(lo + 1, hi)), key=lambda x: x[1])
        if distance > tolerance:
            keep[index] = True
            stack += [(lo, index), (index, hi)]
    return [p for p, k in zip(points, keep) if k]


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    dn, de = b[0] - a[0], b[1] - a[1]
    length = dn * dn + de * de
    t = 0.0 if length == 0.0 else max(0.0, min(1.0, ((p[0] - a[0]) * dn + (p[1] - a[1]) * de) / length))
    return math.dist(p, (a[0] + t * dn, a[1] + t * de))


def merge_polygons(
    polygons: Iterable[tuple[str, Sequence[Point]]], tolerance_m: float = 0.3, grid_m: float = 0.1
) -> tuple[Area, ...]:
    """Merge each kind's polygons (navmesh polygons, grid cells) into outlines with holes.

    Vertices are snapped to ``grid_m``. An edge two polygons share is interior; the remaining
    edges chain into closed loops. A loop inside an odd number of its kind's other loops is a hole
    of the innermost one around it. Loops are simplified (Douglas-Peucker, ``tolerance_m``), and
    slivers under 1 m² are dropped.
    """
    by_kind: dict[str, list[Sequence[Point]]] = {}
    for kind, outline in polygons:
        by_kind.setdefault(kind, []).append(outline)
    merged: list[Area] = []
    for kind, outlines in by_kind.items():
        loops = [simplify_loop(loop, tolerance_m) for loop in _boundary_loops(outlines, grid_m)]
        loops = [loop for loop in loops if len(loop) >= 3 and abs(signed_area(loop)) >= 1.0]
        boxes = [_bbox(loop) for loop in loops]
        parents: list[list[int]] = []
        for i, loop in enumerate(loops):
            probe = _inner_probe(loop)
            parents.append([
                j for j, other in enumerate(loops)
                if j != i and _bbox_has(boxes[j], probe) and contains(other, probe)
            ])
        holes: dict[int, list[tuple[Point, ...]]] = {}
        for i, around in enumerate(parents):
            if len(around) % 2 == 1:
                # The innermost loop around a hole is the one with the most loops around it.
                owner = max(around, key=lambda j: len(parents[j]))
                holes.setdefault(owner, []).append(loops[i])
        merged.extend(
            Area(kind, loops[i], "", tuple(holes.get(i, ())))
            for i, around in enumerate(parents) if len(around) % 2 == 0
        )
    return tuple(merged)


def _boundary_loops(outlines: Iterable[Sequence[Point]], grid_m: float) -> list[tuple[Point, ...]]:
    def snap(point: Point) -> tuple[int, int]:
        return (round(point[0] / grid_m), round(point[1] / grid_m))

    count: dict[frozenset, int] = {}
    for outline in outlines:
        keys = [snap(p) for p in outline]
        for a, b in zip(keys, keys[1:] + keys[:1]):
            if a != b:
                edge = frozenset((a, b))
                count[edge] = count.get(edge, 0) + 1
    neighbours: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for edge, n in count.items():
        if n % 2 == 1:
            a, b = tuple(edge)
            neighbours.setdefault(a, []).append(b)
            neighbours.setdefault(b, []).append(a)
    used: set[frozenset] = set()
    loops = []
    for start, ends in neighbours.items():
        for first in ends:
            if frozenset((start, first)) in used:
                continue
            used.add(frozenset((start, first)))
            loop, current = [start], first
            while current != start:
                loop.append(current)
                step = next((n for n in neighbours[current] if frozenset((current, n)) not in used), None)
                if step is None:  # an open chain (a vertex snapped onto another loop): drop it
                    loop = []
                    break
                used.add(frozenset((current, step)))
                current = step
            if loop:
                loops.append(tuple((k[0] * grid_m, k[1] * grid_m) for k in loop))
    return loops


def signed_area(loop: Sequence[Point]) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(loop, loop[1:] + loop[:1])) / 2.0


def _inner_probe(loop: tuple[Point, ...]) -> Point:
    """A point just inside the loop next to its first edge (vertices may lie on other loops)."""
    (n1, e1), (n2, e2) = loop[0], loop[1]
    mid, length = ((n1 + n2) / 2, (e1 + e2) / 2), math.dist(loop[0], loop[1]) or 1.0
    normal = (-(e2 - e1) / length, (n2 - n1) / length)
    for side in (1.0, -1.0):
        probe = (mid[0] + side * 0.01 * normal[0], mid[1] + side * 0.01 * normal[1])
        if contains(loop, probe):
            return probe
    return mid


def _bbox(loop: Sequence[Point]) -> tuple[float, float, float, float]:
    ns, es = [p[0] for p in loop], [p[1] for p in loop]
    return min(ns), min(es), max(ns), max(es)


def _bbox_has(box: tuple[float, float, float, float], point: Point) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _clip_areas(areas: tuple[Area, ...], centre: Point, radius: float) -> tuple[Area, ...]:
    box = (centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius)
    clipped = []
    for area in areas:
        if not _polygon_touches(area.outline, centre, radius):
            continue
        outline = _clip_to_box(area.outline, box)
        if len(outline) >= 3:
            holes = tuple(h for h in (_clip_to_box(hole, box) for hole in area.holes) if len(h) >= 3)
            clipped.append(Area(area.kind, outline, area.name, holes))
    return tuple(clipped)


def _clip_to_box(outline: tuple[Point, ...], box: tuple[float, float, float, float]) -> tuple[Point, ...]:
    """Sutherland-Hodgman against an axis-aligned box ``(min_n, min_e, max_n, max_e)``; an outline
    already inside is returned unchanged."""
    if all(box[0] <= n <= box[2] and box[1] <= e <= box[3] for n, e in outline):
        return outline
    points = list(outline)
    for axis, bound, keep_above in ((0, box[0], True), (0, box[2], False), (1, box[1], True), (1, box[3], False)):
        if not points:
            break

        def inside(p: Point) -> bool:
            return p[axis] >= bound if keep_above else p[axis] <= bound

        def cut(a: Point, b: Point) -> Point:
            t = (bound - a[axis]) / (b[axis] - a[axis])
            return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))

        result: list[Point] = []
        for a, b in zip(points, points[1:] + points[:1]):
            if inside(b):
                if not inside(a):
                    result.append(cut(a, b))
                result.append(b)
            elif inside(a):
                result.append(cut(a, b))
        points = result
    return tuple(points)


def _clip_polyline(points: tuple[Point, ...], centre: Point, radius: float) -> list[tuple[Point, ...]]:
    """The parts of a polyline inside the circle, each a polyline of at least two points."""
    pieces: list[tuple[Point, ...]] = []
    current: list[Point] = []
    for a, b in zip(points, points[1:]):
        span = _segment_in_circle(a, b, centre, radius)
        if span is None:
            if len(current) >= 2:
                pieces.append(tuple(current))
            current = []
            continue
        start, end = span
        if current and current[-1] == start:
            current.append(end)
        else:
            if len(current) >= 2:
                pieces.append(tuple(current))
            current = [start, end]
        if end != b:  # the segment leaves the circle
            pieces.append(tuple(current))
            current = []
    if len(current) >= 2:
        pieces.append(tuple(current))
    return pieces


def _segment_in_circle(a: Point, b: Point, centre: Point, radius: float) -> tuple[Point, Point] | None:
    """The part of segment ``a``-``b`` inside the circle, or None (endpoints kept exactly when inside)."""
    dn, de = b[0] - a[0], b[1] - a[1]
    fn, fe = a[0] - centre[0], a[1] - centre[1]
    qa = dn * dn + de * de
    qb = 2.0 * (fn * dn + fe * de)
    qc = fn * fn + fe * fe - radius * radius
    if qa == 0.0:
        return (a, b) if qc <= 0.0 else None
    disc = qb * qb - 4.0 * qa * qc
    if disc < 0.0:
        return None
    root = math.sqrt(disc)
    t0, t1 = max(0.0, (-qb - root) / (2.0 * qa)), min(1.0, (-qb + root) / (2.0 * qa))
    if t0 > t1:
        return None
    start = a if t0 == 0.0 else (a[0] + t0 * dn, a[1] + t0 * de)
    end = b if t1 == 1.0 else (a[0] + t1 * dn, a[1] + t1 * de)
    return start, end


def _polygon_touches(outline: tuple[Point, ...], centre: Point, radius: float) -> bool:
    if not outline:
        return False
    if contains(outline, centre):
        return True
    return any(
        _segment_in_circle(a, b, centre, radius) is not None for a, b in zip(outline, outline[1:] + outline[:1])
    )


def contains(outline: tuple[Point, ...], point: Point) -> bool:
    """Whether ``point`` lies inside the polygon ``outline`` (even-odd rule)."""
    inside = False
    n, e = point
    for (n1, e1), (n2, e2) in zip(outline, outline[1:] + outline[:1]):
        if (e1 > e) != (e2 > e) and n < n1 + (e - e1) * (n2 - n1) / (e2 - e1):
            inside = not inside
    return inside
