"""Map tiles: a whole map cut into squares at three levels of detail, so a control app downloads
only the part it shows (or, on request, every tile once for offline use).

Tile ``(level, x, y)`` covers east ``[x*size, (x+1)*size)`` and north ``[y*size, (y+1)*size)`` in
local NED metres from home:
- level 0, 250 m: every feature (ways, walkable ground, places, areas, obstacles);
- level 1, 1000 m: ways simplified to 2 m, junctions only, areas, and walkable and obstacle
  polygons of at least 2000 m² simplified to 2 m;
- level 2, 4000 m: the overview ways (freeways and main roads) simplified to 10 m, and areas.

A feature is listed, unclipped, in every tile its bounding box touches, with an ``id`` unique per
level: ``w<way>.<piece>`` (a way is cut into pieces at most one tile across; consecutive pieces
share their end point), ``g<n>`` walkable, ``p<n>`` place, ``a<n>`` area, ``o<n>`` obstacle.
Names and kinds are quoted untrusted text (``quote_label``), as in every map tool answer, and
coordinates are rounded to 0.1 m. Empty tiles do not exist. The ``version`` (in the index and
every tile) hashes the provider, attribution, sources and every tile, so it changes only when
the map does, and a rebuild from the same inputs gives the same bytes.

``write_tile_directory`` stores a map as ``index.json``, ``search.json.gz`` (the places and
named areas ``find_place`` ranks) and gzipped ``<level>/<x>_<y>.json.gz`` tiles. ``TiledMap``
serves such a directory without loading its geometry: ``area`` reads the level-0 tiles around a
point (a small LRU), so a whole-world map costs the drone board little memory. ``MemoryMap``
serves a map held in memory (an OSM extract) the same way.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import math
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from horus.maps.model import (
    Area,
    MapData,
    Place,
    Point,
    Way,
    load_map_file,
    map_to_json,
    signed_area,
    simplify_line,
    simplify_loop,
)
from horus.untrusted import quote_label

LEVEL_SIZES_M = (250, 1000, 4000)
LAYERS = ("ways", "walkable", "places", "areas", "obstacles")
# Level -> (simplification tolerance m, smallest walkable/obstacle polygon m²).
_LEVEL_RULES = {1: (2.0, 2000.0), 2: (10.0, math.inf)}
_LEVEL1_PLACE_KINDS = frozenset({"junction"})
# Parsed level-0 tiles a TiledMap keeps for get_area_map (a 300 m circle touches at most 16).
_TILE_CACHE = 32

TileKey = tuple[int, int, int]


def overview_road(way: Way) -> bool:
    """The default level-2 rule: every road (an OSM extract is small)."""
    return way.kind in {"freeway", "road"}


def tiles_touched(level: int, box: tuple[float, float, float, float]) -> Iterable[tuple[int, int]]:
    """The ``(x, y)`` of every tile of ``level`` that the box ``(min_n, min_e, max_n, max_e)`` touches."""
    size = LEVEL_SIZES_M[level]
    for x in range(math.floor(box[1] / size), math.floor(box[3] / size) + 1):
        for y in range(math.floor(box[0] / size), math.floor(box[2] / size) + 1):
            yield x, y


def generalize(data: MapData, level: int, overview: Callable[[Way], bool] = overview_road) -> MapData:
    """The features ``level`` shows (see the module docstring); level 0 is ``data`` itself."""
    if level == 0:
        return data
    tolerance, smallest = _LEVEL_RULES[level]

    def polygons(areas: Iterable[Area], smallest: float) -> tuple[Area, ...]:
        return tuple(
            Area(a.kind, simplify_loop(a.outline, tolerance), a.name, tuple(
                simplify_loop(hole, tolerance) for hole in a.holes if abs(signed_area(hole)) >= smallest))
            for a in areas if abs(signed_area(a.outline)) >= smallest
        )

    return MapData(
        places=tuple(p for p in data.places if level == 1 and p.kind in _LEVEL1_PLACE_KINDS),
        ways=tuple(Way(w.kind, simplify_line(w.points, tolerance), w.name)
                   for w in data.ways if level == 1 or overview(w)),
        areas=polygons(data.areas, 0.0),
        walkable=polygons(data.walkable, smallest),
        obstacles=polygons(data.obstacles, smallest),
        attribution=data.attribution,
        sources=data.sources,
    )


def build_tiles(
    data: MapData, provider: str, overview: Callable[[Way], bool] = overview_road,
) -> tuple[dict[str, Any], dict[TileKey, tuple[bytes, bytes]]]:
    """The index and every non-empty tile as ``(JSON, gzipped JSON)``, deterministic for ``data``."""
    bodies: dict[TileKey, bytes] = {}
    for level in range(len(LEVEL_SIZES_M)):
        for (x, y), layers in sorted(_cut(generalize(data, level, overview), level).items()):
            bodies[(level, x, y)] = _dumps({"level": level, "x": x, "y": y, **layers})
    digest = hashlib.sha256(_dumps({"provider": provider, "attribution": data.attribution, "sources": data.sources}))
    for body in bodies.values():
        digest.update(body)
    version = digest.hexdigest()[:16]
    tiles = {}
    for key, body in bodies.items():
        body = b'{"version":' + _dumps(version) + b"," + body[1:]
        tiles[key] = (body, gzip.compress(body, 6, mtime=0))
    index = {
        "version": version,
        "provider": provider,
        "attribution": data.attribution,
        "sources": data.sources,
        "levels": [{"level": level, "size_m": size} for level, size in enumerate(LEVEL_SIZES_M)],
        "bounds": _bounds(data),
        "tiles": {str(level): [[x, y, len(tiles[(lv, x, y)][1])] for (lv, x, y) in tiles if lv == level]
                  for level in range(len(LEVEL_SIZES_M))},
    }
    index["bytes"] = {level: sum(entry[2] for entry in entries) for level, entries in index["tiles"].items()}
    return index, tiles


def write_tile_directory(
    data: MapData, directory: str | Path, provider: str, overview: Callable[[Way], bool] = overview_road,
) -> dict[str, Any]:
    """Write ``data``'s tile directory (replacing an existing one only once complete); returns the index."""
    directory = Path(directory)
    index, tiles = build_tiles(data, provider, overview)
    staging = directory.with_name(directory.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    for (level, x, y), (_, packed) in tiles.items():
        path = staging / str(level) / f"{x}_{y}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(packed)
    search = MapData(places=data.places, areas=tuple(a for a in data.areas if a.name), attribution=data.attribution)
    (staging / "search.json.gz").write_bytes(gzip.compress(_dumps(map_to_json(search)), 6, mtime=0))
    (staging / "index.json").write_bytes(_dumps(index))
    if directory.exists():
        shutil.rmtree(directory)
    staging.rename(directory)
    return index


class MemoryMap:
    """A provider whose map is in memory; its tiles are cut on first use."""

    def __init__(self, name: str, data: MapData) -> None:
        self.name = name
        self.attribution = data.attribution
        self.searchable = data
        self._data = data

    def area(self, north_m: float, east_m: float, radius_m: float) -> MapData:
        return self._data.area(north_m, east_m, radius_m)

    @functools.cached_property
    def _tiles(self) -> tuple[str, str, dict[TileKey, str]]:
        index, tiles = build_tiles(self._data, self.name)
        return index["version"], _dumps(index).decode("utf-8"), {k: body.decode("utf-8") for k, (body, _) in tiles.items()}

    @property
    def version(self) -> str:
        return self._tiles[0]

    def index_json(self) -> str:
        return self._tiles[1]

    def tile_json(self, level: int, x: int, y: int) -> str | None:
        return self._tiles[2].get((level, x, y))


class TiledMap:
    """A provider over a tile directory (``write_tile_directory``): only the index, the places and
    named areas, and a few level-0 tiles are in memory."""

    def __init__(self, name: str, directory: str | Path) -> None:
        self.name = name
        self._directory = Path(directory)
        self._index = (self._directory / "index.json").read_text(encoding="utf-8")
        index = json.loads(self._index)
        self.version: str = index["version"]
        self.attribution: str = index["attribution"]
        self._level0 = frozenset((x, y) for x, y, _ in index["tiles"]["0"])
        self.searchable = load_map_file(self._directory / "search.json.gz")
        self._features = functools.lru_cache(maxsize=_TILE_CACHE)(self._read_features)

    def index_json(self) -> str:
        return self._index

    def tile_json(self, level: int, x: int, y: int) -> str | None:
        try:
            packed = (self._directory / str(int(level)) / f"{int(x)}_{int(y)}.json.gz").read_bytes()
        except FileNotFoundError:
            return None
        return gzip.decompress(packed).decode("utf-8")

    def area(self, north_m: float, east_m: float, radius_m: float) -> MapData:
        box = (north_m - radius_m, east_m - radius_m, north_m + radius_m, east_m + radius_m)
        layers: dict[str, dict[str, Any]] = {layer: {} for layer in LAYERS}
        for key in tiles_touched(0, box):
            if key in self._level0:
                for layer, features in self._features(key).items():
                    layers[layer].update(features)
        return MapData(
            places=tuple(layers["places"].values()),
            ways=_join_pieces(layers["ways"]),
            areas=tuple(layers["areas"].values()),
            walkable=tuple(layers["walkable"].values()),
            obstacles=tuple(layers["obstacles"].values()),
            attribution=self.attribution,
        ).area(north_m, east_m, radius_m)

    def _read_features(self, key: tuple[int, int]) -> dict[str, dict[str, Any]]:
        tile = json.loads(self.tile_json(0, *key) or "{}")

        def text(feature: dict[str, Any], field: str) -> str:
            return json.loads(feature[field]) if field in feature else ""

        def points(raw: Any) -> tuple[Point, ...]:
            return tuple((n, e) for n, e in raw)

        def area(f: dict[str, Any]) -> Area:
            return Area(text(f, "kind"), points(f["outline"]), text(f, "name"),
                        tuple(points(hole) for hole in f.get("holes", ())))

        return {
            "ways": {f["id"]: Way(text(f, "kind"), points(f["points"]), text(f, "name")) for f in tile.get("ways", ())},
            "places": {f["id"]: Place(text(f, "name"), text(f, "kind"), f["north_m"], f["east_m"],
                                      points(f.get("outline", ()))) for f in tile.get("places", ())},
            **{layer: {f["id"]: area(f) for f in tile.get(layer, ())} for layer in ("walkable", "areas", "obstacles")},
        }


def _cut(data: MapData, level: int) -> dict[tuple[int, int], dict[str, list[dict[str, Any]]]]:
    size = LEVEL_SIZES_M[level]
    tiles: dict[tuple[int, int], dict[str, list[dict[str, Any]]]] = {}

    def add(layer: str, feature: dict[str, Any], points: Iterable[Point]) -> None:
        for key in tiles_touched(level, _bbox(points)):
            tiles.setdefault(key, {name: [] for name in LAYERS})[layer].append(feature)

    for i, way in enumerate(data.ways):
        if len(way.points) < 2:
            continue
        for j, piece in enumerate(_pieces(way.points, size)):
            add("ways", {"id": f"w{i}.{j}", **_labels(way.kind, way.name), "points": _points(piece)}, piece)
    for prefix, layer in (("g", "walkable"), ("a", "areas"), ("o", "obstacles")):
        for i, a in enumerate(getattr(data, layer)):
            if len(a.outline) >= 3:
                feature = {"id": f"{prefix}{i}", **_labels(a.kind, a.name), "outline": _points(a.outline),
                           **({"holes": [_points(h) for h in a.holes]} if a.holes else {})}
                add(layer, feature, a.outline)
    for i, p in enumerate(data.places):
        feature = {"id": f"p{i}", **_labels(p.kind, p.name, always_named=True),
                   "north_m": round(p.north_m, 1), "east_m": round(p.east_m, 1),
                   **({"outline": _points(p.outline)} if p.outline else {})}
        add("places", feature, ((p.north_m, p.east_m),))
    return tiles


def _pieces(points: tuple[Point, ...], size: float) -> list[tuple[Point, ...]]:
    """Cut a polyline into consecutive pieces at most ``size`` across (a longer single segment
    stays one piece), each piece starting where the previous one ends."""
    pieces: list[tuple[Point, ...]] = []
    current = [points[0]]
    low_n = high_n = points[0][0]
    low_e = high_e = points[0][1]
    for point in points[1:]:
        n, e = point
        if len(current) >= 2 and (max(high_n, n) - min(low_n, n) > size or max(high_e, e) - min(low_e, e) > size):
            pieces.append(tuple(current))
            current = [current[-1]]
            low_n = high_n = current[0][0]
            low_e = high_e = current[0][1]
        current.append(point)
        low_n, high_n, low_e, high_e = min(low_n, n), max(high_n, n), min(low_e, e), max(high_e, e)
    pieces.append(tuple(current))
    return pieces


def _join_pieces(pieces: dict[str, Way]) -> tuple[Way, ...]:
    """Rejoin consecutive pieces of one way (``w<way>.<piece>`` ids that meet end to start)."""
    ordered = sorted(pieces.items(), key=lambda item: tuple(int(part) for part in item[0][1:].split(".")))
    joined: list[Way] = []
    previous: tuple[int, int] | None = None
    for identifier, way in ordered:
        way_number, piece = (int(part) for part in identifier[1:].split("."))
        if previous == (way_number, piece - 1) and joined[-1].points[-1] == way.points[0]:
            joined[-1] = Way(way.kind, joined[-1].points + way.points[1:], way.name)
        else:
            joined.append(way)
        previous = (way_number, piece)
    return tuple(joined)


def _labels(kind: str, name: str, always_named: bool = False) -> dict[str, str]:
    return {"kind": quote_label(kind), **({"name": quote_label(name)} if name or always_named else {})}


def _points(points: Iterable[Point]) -> list[list[float]]:
    return [[round(n, 1), round(e, 1)] for n, e in points]


def _bbox(points: Iterable[Point]) -> tuple[float, float, float, float]:
    ns, es = zip(*points)
    return min(ns), min(es), max(ns), max(es)


def _bounds(data: MapData) -> list[float]:
    boxes = [_bbox(w.points) for w in data.ways if w.points]
    boxes += [_bbox(a.outline) for layer in (data.areas, data.walkable, data.obstacles) for a in layer if a.outline]
    boxes += [(p.north_m, p.east_m, p.north_m, p.east_m) for p in data.places]
    if not boxes:
        return [0.0, 0.0, 0.0, 0.0]
    return [round(min(b[0] for b in boxes), 1), round(min(b[1] for b in boxes), 1),
            round(max(b[2] for b in boxes), 1), round(max(b[3] for b in boxes), 1)]


def _dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
