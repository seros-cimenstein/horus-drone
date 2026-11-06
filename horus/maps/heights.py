"""The GTA height map: the top surface of the world (buildings, terrain, objects, trees, glass) every
5 m, measured in game by HorusGta's survey (``gta/horus_mod/README.md`` "Survey") and served in
HORUS ``up_m``.

A survey folder holds ``survey.json`` and one ``<x>_<y>.bin`` per 250 m tile of the map tile grid
(``horus.maps.tiles`` level 0): little-endian magic ``HSRV`` | u16 version 1 | u16 n (cells per
side) | i32 tile x | i32 tile y | f32 cell_m | u32 cells hit | n·n × i16 GTA z of the top surface
in decimetres, ``NO_HIT`` (-32768) where the ray from GTA z 1500 down to -50 hit nothing. Cell
(i, j) is column i east from the tile's west edge and row j north from its south edge, stored at
k = j·n + i; tile (x, y) covers east [x·250, (x+1)·250) and north [y·250, (y+1)·250) metres from
SITL home.

``build_height_directory`` checks a survey and writes ``index.json`` (version hash, cell size, NED
bounds, the home ground z, coverage counts) and the tiles gzipped as ``<x>_<y>.bin.gz``, unchanged
inside, so the same survey always gives the same bytes and version. ``HeightMap`` reads such a
directory with a small LRU of tiles and answers in ``up_m``: GTA z - (home ground z + 0.121), the
resting body centre being HORUS's zero. A no-hit cell is the sea (its floor deeper than GTA z -50):
water at GTA z 0. A cell in a tile the survey did not write (outside its box, or unloaded in game)
is unknown: ``None``, and every maximum that touches one is ``None`` too.

Standard library only (the drone board's map code takes no dependency).
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import math
import shutil
import struct
from array import array
from collections.abc import Iterable
from pathlib import Path
from typing import Any

TILE_M = 250.0
HEADER = struct.Struct("<4sHHiifI")
MAGIC = b"HSRV"
TILE_VERSION = 1
NO_HIT = -32768
SURVEY_FORMAT = "horus-gta-survey"
HEIGHTS_FORMAT = "horus-gta-heights"
WATER_Z = 0.0
# Decoded tiles a HeightMap keeps (5 KB each at 5 m cells; a 1.6 km search box touches 49).
_TILE_CACHE = 128

TileXY = tuple[int, int]


class HeightError(ValueError):
    """A survey or height directory that cannot be used."""


def decode_survey_tile(data: bytes) -> tuple[TileXY, int, float, array]:
    """``((x, y), n, cell_m, heights)`` of one tile file; ``HeightError`` for anything malformed."""
    if len(data) < HEADER.size:
        raise HeightError("shorter than the 24-byte header")
    magic, version, n, x, y, cell_m, hits = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise HeightError("not an HSRV tile")
    if version != TILE_VERSION:
        raise HeightError(f"version {version}, not {TILE_VERSION}")
    if len(data) != HEADER.size + 2 * n * n:
        raise HeightError(f"{len(data)} bytes, not {HEADER.size + 2 * n * n} for {n}x{n} cells")
    heights = array("h")
    heights.frombytes(data[HEADER.size:])
    if struct.pack("<h", 1) != array("h", [1]).tobytes():
        heights.byteswap()
    if sum(1 for h in heights if h != NO_HIT) != hits:
        raise HeightError("hit count does not match the cells")
    return (x, y), n, cell_m, heights


def build_height_directory(
    survey: str | Path, out: str | Path, origin: tuple[float, float, float] | None = None,
) -> dict[str, Any]:
    """Check the survey folder and write the height directory ``out`` (replacing it only once
    complete); returns the index. ``origin`` (GTA x, y, heading of ``HorusGta.ini [origin]``), when
    given, must equal the survey's, so the heights line up with the map tiles."""
    survey, out = Path(survey), Path(out)
    try:
        meta = json.loads((survey / "survey.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HeightError(f"{survey / 'survey.json'}: {exc}") from exc
    if meta.get("format") != SURVEY_FORMAT or meta.get("version") != 1:
        raise HeightError(f"{survey / 'survey.json'} is not a version 1 {SURVEY_FORMAT} survey")
    cell_m, n = float(meta["cell_m"]), int(meta["cells_per_side"])
    if float(meta["tile_m"]) != TILE_M or abs(cell_m * n - TILE_M) > 1e-9:
        raise HeightError(f"survey grid {meta['tile_m']} m tiles of {n} x {cell_m} m cells is not the {TILE_M:g} m tile grid")
    survey_origin = (float(meta["origin"]["x"]), float(meta["origin"]["y"]), float(meta["origin"]["heading_deg"]))
    if origin is not None and tuple(map(float, origin)) != survey_origin:
        raise HeightError(f"survey origin {survey_origin} is not HorusGta.ini [origin] {tuple(origin)}")

    tiles: dict[TileXY, bytes] = {}
    hit = no_hit = 0
    for path in sorted(survey.glob("*.bin")):
        raw = path.read_bytes()
        try:
            key, tile_n, tile_cell, heights = decode_survey_tile(raw)
        except HeightError as exc:
            raise HeightError(f"{path.name}: {exc}") from exc
        if path.name != f"{key[0]}_{key[1]}.bin" or tile_n != n or tile_cell != struct.unpack("<f", struct.pack("<f", cell_m))[0]:
            raise HeightError(f"{path.name}: tile {key[0]}_{key[1]} of {tile_n} x {tile_cell} m cells does not belong to this survey")
        tiles[key] = raw
        cells_hit = HEADER.unpack_from(raw)[6]
        hit += cells_hit
        no_hit += n * n - cells_hit
    if not tiles:
        raise HeightError(f"{survey} has no tiles")

    box = [float(v) for v in meta["box"]]
    in_box = {(x, y) for x in range(math.floor(box[1] / TILE_M), math.floor(box[3] / TILE_M) + 1)
              for y in range(math.floor(box[0] / TILE_M), math.floor(box[2] / TILE_M) + 1)}
    unloaded: list[list[int]] = []
    try:
        progress = json.loads((survey / "progress.json").read_text(encoding="utf-8"))
        unloaded = sorted([int(x), int(y)] for x, y in progress.get("unloaded") or () if (int(x), int(y)) not in tiles)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        raise HeightError(f"{survey / 'progress.json'}: {exc}") from exc

    home_z = float(meta["home_ground_z"])
    rest = float(meta["rest_above_ground_m"])
    keys = sorted(tiles)
    digest = hashlib.sha256(_dumps({"format": HEIGHTS_FORMAT, "cell_m": cell_m, "origin": survey_origin,
                                    "home_ground_z": home_z, "rest_above_ground_m": rest}))
    for key in keys:
        digest.update(struct.pack("<ii", *key) + tiles[key])
    packed = {key: gzip.compress(tiles[key], 9, mtime=0) for key in keys}
    xs, ys = [k[0] for k in keys], [k[1] for k in keys]
    index = {
        "format": HEIGHTS_FORMAT,
        "version": digest.hexdigest()[:16],
        "tile_m": TILE_M,
        "cell_m": cell_m,
        "cells_per_side": n,
        "bounds": [min(ys) * TILE_M, min(xs) * TILE_M, (max(ys) + 1) * TILE_M, (max(xs) + 1) * TILE_M],
        "origin": {"x": survey_origin[0], "y": survey_origin[1], "heading_deg": survey_origin[2]},
        "home_ground_z": home_z,
        "rest_above_ground_m": rest,
        "water_z": WATER_Z,
        "survey_box": box,
        "coverage": {
            "tiles": len(keys),
            "tiles_in_box": len(in_box),
            "missing_in_box": len(in_box - set(keys)),
            "unloaded": unloaded,
            "cells": hit + no_hit,
            "no_hit_cells": no_hit,
            "no_hit_share": round(no_hit / (hit + no_hit), 6),
        },
        "tiles": [[x, y, len(packed[(x, y)])] for x, y in keys],
    }
    index["bytes"] = sum(len(p) for p in packed.values())
    staging = out.with_name(out.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for (x, y), body in packed.items():
        (staging / f"{x}_{y}.bin.gz").write_bytes(body)
    (staging / "index.json").write_bytes(_dumps(index))
    if out.exists():
        shutil.rmtree(out)
    staging.rename(out)
    return index


class HeightMap:
    """A height directory (``build_height_directory``): the index in memory, tiles read on demand."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        index = json.loads((self._directory / "index.json").read_text(encoding="utf-8"))
        if index.get("format") != HEIGHTS_FORMAT:
            raise HeightError(f"{self._directory / 'index.json'} is not a {HEIGHTS_FORMAT} index")
        self.index: dict[str, Any] = index
        self.version: str = index["version"]
        self.cell_m = float(index["cell_m"])
        self.cells_per_side = int(index["cells_per_side"])
        self.bounds: tuple[float, float, float, float] = tuple(index["bounds"])
        self.home_ground_z = float(index["home_ground_z"])
        # GTA z of HORUS up 0: the resting body centre above the home ground.
        self.zero_z = self.home_ground_z + float(index["rest_above_ground_m"])
        self._tiles = frozenset((x, y) for x, y, _ in index["tiles"])
        self._tile = functools.lru_cache(maxsize=_TILE_CACHE)(self._read_tile)

    def top_up_m(self, north_m: float, east_m: float) -> float | None:
        """``up_m`` of the top surface in the cell holding the point, or ``None`` when unknown."""
        return self._cell(math.floor(north_m / self.cell_m), math.floor(east_m / self.cell_m))

    def max_in_disc(self, north_m: float, east_m: float, radius_m: float) -> float | None:
        """Highest ``up_m`` of every cell whose square reaches within ``radius_m`` of the point
        (``None`` when any of them is unknown)."""
        return self.max_along_segment((north_m, east_m), (north_m, east_m), radius_m)

    def max_along_segment(
        self, start: tuple[float, float], end: tuple[float, float], radius_m: float = 0.0,
    ) -> float | None:
        """Highest ``up_m`` of every cell whose square reaches within ``radius_m`` of the segment
        ``start``-``end`` (``(north_m, east_m)`` each), ``None`` when any of them is unknown."""
        if radius_m < 0 or not all(map(math.isfinite, (*start, *end, radius_m))):
            raise ValueError("segment ends and radius must be finite, the radius not negative")
        top = -math.inf
        for row, col in self._cells_near(start, end, radius_m):
            up = self._cell(row, col)
            if up is None:
                return None
            top = max(top, up)
        return top

    def _cells_near(self, a: tuple[float, float], b: tuple[float, float], r: float) -> Iterable[tuple[int, int]]:
        """Global (row, column) of every cell whose square is within ``r`` of segment a-b."""
        c = self.cell_m
        (an, ae), (bn, be) = a, b
        # Closed squares: a row (column) whose edge only touches the corridor counts, so the range
        # starts one below ceil.
        for row in range(math.ceil((min(an, bn) - r) / c) - 1, math.floor((max(an, bn) + r) / c) + 1):
            # The segment's east extent over this row's strip widened by r, then each cell exactly.
            low, high = row * c - r, (row + 1) * c + r
            lo_e, hi_e = _east_span(an, ae, bn, be, low, high)
            if lo_e > hi_e:
                continue
            for col in range(math.ceil((lo_e - r) / c) - 1, math.floor((hi_e + r) / c) + 1):
                if _segment_box_distance(a, b, row * c, col * c, (row + 1) * c, (col + 1) * c) <= r:
                    yield row, col

    def _cell(self, row: int, col: int) -> float | None:
        n = self.cells_per_side
        x, i = divmod(col, n)
        y, j = divmod(row, n)
        heights = self._tile(x, y) if (x, y) in self._tiles else None
        if heights is None:
            return None
        h = heights[j * n + i]
        return (WATER_Z if h == NO_HIT else h / 10.0) - self.zero_z

    def _read_tile(self, x: int, y: int) -> array | None:
        try:
            raw = gzip.decompress((self._directory / f"{x}_{y}.bin.gz").read_bytes())
        except FileNotFoundError:
            return None
        key, n, _, heights = decode_survey_tile(raw)
        if key != (x, y) or n != self.cells_per_side:
            raise HeightError(f"{x}_{y}.bin.gz holds tile {key[0]}_{key[1]} of {n} cells per side")
        return heights


def _east_span(an: float, ae: float, bn: float, be: float, low: float, high: float) -> tuple[float, float]:
    """East extent of segment a-b clipped to north in [low, high] (empty: low > high)."""
    if an == bn:
        return (min(ae, be), max(ae, be)) if low <= an <= high else (1.0, 0.0)
    t0, t1 = sorted(((low - an) / (bn - an), (high - an) / (bn - an)))
    t0, t1 = max(t0, 0.0), min(t1, 1.0)
    if t0 > t1:
        return 1.0, 0.0
    e0, e1 = ae + t0 * (be - ae), ae + t1 * (be - ae)
    return min(e0, e1), max(e0, e1)


def _point_box_distance(p: tuple[float, float], n0: float, e0: float, n1: float, e1: float) -> float:
    dn = max(n0 - p[0], 0.0, p[0] - n1)
    de = max(e0 - p[1], 0.0, p[1] - e1)
    return math.hypot(dn, de)


def _point_segment_distance(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dn, de = b[0] - a[0], b[1] - a[1]
    length2 = dn * dn + de * de
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dn + (p[1] - a[1]) * de) / length2))
    return math.hypot(p[0] - a[0] - t * dn, p[1] - a[1] - t * de)


def _segment_box_distance(a: tuple[float, float], b: tuple[float, float], n0: float, e0: float, n1: float, e1: float) -> float:
    """Distance between segment a-b and the box; 0 when they meet. For a segment and a convex box
    the closest pair is at a segment end or a box corner, unless they cross."""
    if _segment_meets_box(a, b, n0, e0, n1, e1):
        return 0.0
    corners = ((n0, e0), (n0, e1), (n1, e0), (n1, e1))
    return min(_point_box_distance(a, n0, e0, n1, e1), _point_box_distance(b, n0, e0, n1, e1),
               *(_point_segment_distance(c, a, b) for c in corners))


def _segment_meets_box(a: tuple[float, float], b: tuple[float, float], n0: float, e0: float, n1: float, e1: float) -> bool:
    """Liang-Barsky clip of segment a-b against the box."""
    t0, t1 = 0.0, 1.0
    for p, q in ((-(b[0] - a[0]), a[0] - n0), (b[0] - a[0], n1 - a[0]),
                 (-(b[1] - a[1]), a[1] - e0), (b[1] - a[1], e1 - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
